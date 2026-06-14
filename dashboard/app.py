from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from .data import (
    WEIBO_COLLECTIONS,
    database_ready,
    get_record,
    list_records,
    overview_stats,
)


def create_app(db_path: str | Path = "data/weibo.duckdb") -> Flask:
    app = Flask(__name__)
    app.config["WEIBO_DB_PATH"] = Path(db_path)

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
            "collections": WEIBO_COLLECTIONS,
            "db_path": app.config["WEIBO_DB_PATH"],
        }

    @app.route("/")
    def index() -> str:
        db = app.config["WEIBO_DB_PATH"]
        if not database_ready(db):
            return render_template("missing.html", db_path=db), 500
        return render_template("index.html", stats=overview_stats(db))

    @app.route("/browse")
    def browse_redirect():
        return redirect(url_for("browse_collection", collection="weibo_posts_raw"))

    @app.route("/browse/<collection>")
    def browse_collection(collection: str) -> str:
        if collection not in WEIBO_COLLECTIONS:
            abort(404)
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 25, type=int)
        query = request.args.get("q", "", type=str).strip()
        result = list_records(
            app.config["WEIBO_DB_PATH"],
            collection,
            query=query,
            page=page,
            per_page=per_page,
        )
        return render_template(
            "browse.html",
            collection=collection,
            collection_label=WEIBO_COLLECTIONS[collection],
            query=query,
            result=result,
        )

    @app.route("/record/<collection>/<path:record_id>")
    def record_detail(collection: str, record_id: str) -> str:
        if collection not in WEIBO_COLLECTIONS:
            abort(404)
        record, backend = get_record(app.config["WEIBO_DB_PATH"], collection, record_id)
        if record is None:
            abort(404)
        return render_template(
            "detail.html",
            collection=collection,
            collection_label=WEIBO_COLLECTIONS[collection],
            record=record,
            backend=backend,
        )

    @app.route("/api/stats")
    def api_stats():
        db = app.config["WEIBO_DB_PATH"]
        if not database_ready(db):
            return jsonify({"error": f"Database is missing or not ready: {db}"}), 500
        return jsonify(overview_stats(db))

    @app.route("/api/records/<collection>")
    def api_records(collection: str):
        if collection not in WEIBO_COLLECTIONS:
            abort(404)
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 25, type=int)
        query = request.args.get("q", "", type=str).strip()
        return jsonify(
            list_records(
                app.config["WEIBO_DB_PATH"],
                collection,
                query=query,
                page=page,
                per_page=per_page,
            )
        )

    @app.route("/api/record/<collection>/<path:record_id>")
    def api_record(collection: str, record_id: str):
        if collection not in WEIBO_COLLECTIONS:
            abort(404)
        record, backend = get_record(app.config["WEIBO_DB_PATH"], collection, record_id)
        if record is None:
            abort(404)
        return jsonify({"backend": backend, **record})

    return app
