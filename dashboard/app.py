from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from .data import (
    PLATFORMS,
    database_ready,
    get_record,
    list_records,
    overview_stats,
    platform_paths,
    platform_ready,
)


def create_app(db_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_OVERRIDE"] = Path(db_path) if db_path is not None else None
    app.config["DB_PATHS"] = platform_paths(app.config["DB_OVERRIDE"])

    @app.template_filter("number")
    def number_filter(value: object) -> str:
        if value is None or value == "":
            return "-"
        try:
            return f"{float(value):,.0f}"
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("decimal")
    def decimal_filter(value: object) -> str:
        if value is None or value == "":
            return "-"
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)

    @app.context_processor
    def inject_globals() -> dict[str, object]:
        return {
            "platforms": PLATFORMS,
            "db_paths": app.config["DB_PATHS"],
            "db_override": app.config["DB_OVERRIDE"],
        }

    @app.route("/")
    def index() -> str:
        db_paths = app.config["DB_PATHS"]
        if not database_ready(db_paths):
            return render_template("missing.html", db_paths=db_paths), 500
        return render_template("index.html", stats=overview_stats(db_paths))

    @app.route("/browse")
    def browse_redirect():
        return redirect(url_for("browse_collection", platform="weibo", collection="weibo_posts_raw"))

    @app.route("/browse/<platform>/<collection>")
    def browse_collection(platform: str, collection: str) -> str:
        if platform not in PLATFORMS or collection not in PLATFORMS[platform].collections:
            abort(404)
        db_path = app.config["DB_PATHS"][platform]
        if not platform_ready(platform, db_path):
            return render_template("missing.html", db_paths={platform: db_path}), 500
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 25, type=int)
        query = request.args.get("q", "", type=str).strip()
        result = list_records(
            db_path,
            platform,
            collection,
            query=query,
            page=page,
            per_page=per_page,
        )
        return render_template(
            "browse.html",
            platform=platform,
            platform_label=PLATFORMS[platform].label,
            collection=collection,
            collection_label=PLATFORMS[platform].collections[collection].label,
            query=query,
            result=result,
        )

    @app.route("/record/<platform>/<collection>/<path:record_id>")
    def record_detail(platform: str, collection: str, record_id: str) -> str:
        if platform not in PLATFORMS or collection not in PLATFORMS[platform].collections:
            abort(404)
        record, backend = get_record(app.config["DB_PATHS"][platform], platform, collection, record_id)
        if record is None:
            abort(404)
        return render_template(
            "detail.html",
            platform=platform,
            platform_label=PLATFORMS[platform].label,
            collection=collection,
            collection_label=PLATFORMS[platform].collections[collection].label,
            record=record,
            backend=backend,
        )

    @app.route("/api/stats")
    def api_stats():
        db_paths = app.config["DB_PATHS"]
        if not database_ready(db_paths):
            return jsonify({"error": "No platform DuckDB database is available."}), 500
        return jsonify(overview_stats(db_paths))

    @app.route("/api/records/<platform>/<collection>")
    def api_records(platform: str, collection: str):
        if platform not in PLATFORMS or collection not in PLATFORMS[platform].collections:
            abort(404)
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 25, type=int)
        query = request.args.get("q", "", type=str).strip()
        return jsonify(
            list_records(
                app.config["DB_PATHS"][platform],
                platform,
                collection,
                query=query,
                page=page,
                per_page=per_page,
            )
        )

    @app.route("/api/record/<platform>/<collection>/<path:record_id>")
    def api_record(platform: str, collection: str, record_id: str):
        if platform not in PLATFORMS or collection not in PLATFORMS[platform].collections:
            abort(404)
        record, backend = get_record(app.config["DB_PATHS"][platform], platform, collection, record_id)
        if record is None:
            abort(404)
        return jsonify({"backend": backend, **record})

    return app
