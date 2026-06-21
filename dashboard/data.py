from __future__ import annotations

import json
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import duckdb


@dataclass(frozen=True, slots=True)
class CollectionConfig:
    label: str
    table: str
    search_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    label: str
    default_db: Path
    collections: Mapping[str, CollectionConfig]


PLATFORMS: dict[str, PlatformConfig] = {
    "weibo": PlatformConfig(
        label="Weibo",
        default_db=Path("data/weibo.duckdb"),
        collections={
            "weibo_authors": CollectionConfig(
                "Authors",
                "weibo_authors",
                ("$.id", "$.uid", "$.name", "$.location", "$.verified_reason"),
            ),
            "weibo_posts_raw": CollectionConfig(
                "Posts",
                "weibo_posts",
                ("$.id", "$.uid", "$.author_id", "$.url", "$.status", "$.content_text"),
            ),
            "weibo_comments": CollectionConfig(
                "Comments",
                "weibo_comments",
                ("$.id", "$.comment_id", "$.post_id", "$.user_id", "$.comment_text", "$.text"),
            ),
        },
    ),
    "rednote": PlatformConfig(
        label="Rednote",
        default_db=Path("data/rednote.duckdb"),
        collections={
            "rednote_authors": CollectionConfig(
                "Authors",
                "rednote_authors",
                ("$.id", "$.uid", "$.name", "$.nickname"),
            ),
            "rednote_posts_raw": CollectionConfig(
                "Posts",
                "rednote_posts",
                ("$.id", "$.uid", "$.author_id", "$.url", "$.status"),
            ),
            "rednote_post_metadata": CollectionConfig(
                "Search Metadata",
                "rednote_post_metadata",
                ("$.id", "$.title", "$.author_id", "$.author_name", "$.search_keyword", "$.url"),
            ),
            "rednote_post_media_files": CollectionConfig(
                "Media Files",
                "rednote_post_media_files",
                ("$.id", "$.post_id", "$.media_url", "$.media_type", "$.local_path"),
            ),
            "rednote_comments": CollectionConfig(
                "Comments",
                "rednote_comments",
                ("$.id", "$.post_id", "$.user_id", "$.text"),
            ),
        },
    ),
    "douyin": PlatformConfig(
        label="Douyin",
        default_db=Path("data/douyin.duckdb"),
        collections={
            "douyin_authors": CollectionConfig(
                "Authors",
                "douyin_authors",
                ("$.id", "$.sec_user_id", "$.uid", "$.nickname", "$.unique_id"),
            ),
            "douyin_videos_raw": CollectionConfig(
                "Videos",
                "douyin_posts",
                ("$.id", "$.aweme_id", "$.sec_user_id", "$.status", "$.desc", "$.author_nickname"),
            ),
            "douyin_comments_raw": CollectionConfig(
                "Comments",
                "douyin_comments",
                ("$.id", "$.comment_id", "$.aweme_id", "$.user_uid", "$.user_nickname", "$.text"),
            ),
        },
    ),
}


def platform_paths(db_override: Path | None = None) -> dict[str, Path]:
    if db_override is not None:
        return {key: db_override for key in PLATFORMS}
    return {key: config.default_db for key, config in PLATFORMS.items()}


@contextmanager
def duckdb_connection(db_path: Path) -> Iterator[tuple[duckdb.DuckDBPyConnection, str]]:
    """Open DuckDB read-only, falling back to a temporary snapshot if locked."""
    temp_dir: TemporaryDirectory[str] | None = None
    conn: duckdb.DuckDBPyConnection | None = None
    try:
        try:
            conn = duckdb.connect(str(db_path), read_only=True)
            yield conn, "duckdb"
            return
        except Exception as exc:
            message = str(exc)
            if "Could not set lock" not in message and "Conflicting lock" not in message:
                raise

        temp_dir = TemporaryDirectory()
        snapshot_path = Path(temp_dir.name) / db_path.name
        shutil.copy2(db_path, snapshot_path)

        wal_path = db_path.with_suffix(db_path.suffix + ".wal")
        if wal_path.exists():
            shutil.copy2(wal_path, snapshot_path.with_suffix(snapshot_path.suffix + ".wal"))

        conn = duckdb.connect(str(snapshot_path), read_only=True)
        yield conn, "snapshot"
    finally:
        if conn is not None:
            conn.close()
        if temp_dir is not None:
            temp_dir.cleanup()


def run_query(
    db_path: Path,
    sql: str,
    params: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    with duckdb_connection(db_path) as (conn, backend):
        cursor = conn.execute(sql, params or [])
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()], backend


def table_exists(db_path: Path, table: str) -> bool:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False
    rows, _ = run_query(
        db_path,
        """
        SELECT count(*) AS table_count
        FROM information_schema.tables
        WHERE table_name = ?
        """,
        [table],
    )
    return bool(rows and rows[0]["table_count"])


def database_ready(db_paths: Mapping[str, Path]) -> bool:
    return any(platform_ready(platform, db_path) for platform, db_path in db_paths.items())


def platform_ready(platform: str, db_path: Path) -> bool:
    if platform not in PLATFORMS:
        return False
    return any(
        table_exists(db_path, collection.table)
        for collection in PLATFORMS[platform].collections.values()
    )


def source_select(db_path: Path, platform: str, collection: str) -> str:
    config = PLATFORMS[platform].collections[collection]
    if table_exists(db_path, config.table):
        return f"""
            SELECT id, data, updated_at, task_id, author_id, post_id, keyword, status, url
            FROM {config.table}
        """
    return """
        SELECT
            NULL::TEXT AS id,
            NULL::TEXT AS data,
            NULL::TIMESTAMP AS updated_at,
            NULL::TEXT AS task_id,
            NULL::TEXT AS author_id,
            NULL::TEXT AS post_id,
            NULL::TEXT AS keyword,
            NULL::TEXT AS status,
            NULL::TEXT AS url
        WHERE false
    """


def overview_stats(db_paths: Mapping[str, Path]) -> dict[str, Any]:
    platform_rows: list[dict[str, Any]] = []
    collection_rows: list[dict[str, Any]] = []
    task_rows: list[dict[str, Any]] = []
    platform_details: dict[str, dict[str, Any]] = {}
    backend = "duckdb"

    for platform, config in PLATFORMS.items():
        db_path = db_paths[platform]
        ready = platform_ready(platform, db_path)
        counts = collection_counts(db_path, platform) if ready else []
        for row in counts:
            row["platform"] = platform
            row["platform_label"] = config.label
        collection_rows.extend(counts)
        count_map = {row["collection"]: row["rows"] for row in counts}
        total_rows = sum(int(row["rows"] or 0) for row in counts)
        last_updated = max((row["last_updated_at"] for row in counts if row["last_updated_at"]), default=None)
        detail = platform_detail_stats(db_path, platform) if ready else empty_platform_detail(platform)
        platform_details[platform] = detail
        platform_rows.append(
            {
                "platform": platform,
                "label": config.label,
                "db_path": str(db_path),
                "ready": ready,
                "total_rows": total_rows,
                "authors": author_count(platform, count_map),
                "content": content_count(platform, count_map),
                "comments": comment_count(platform, count_map),
                "last_updated_at": last_updated,
            }
        )
        if ready and table_exists(db_path, "tasks"):
            rows, task_backend = run_query(
                db_path,
                """
                SELECT
                    coalesce(nullif(platform, ''), ?) AS platform,
                    coalesce(nullif(scrape_type, ''), 'unknown') AS scrape_type,
                    count(*) AS tasks,
                    max(updated_at) AS last_updated_at
                FROM tasks
                WHERE platform = ? OR platform IS NULL OR platform = ''
                GROUP BY 1, 2
                ORDER BY platform, tasks DESC, scrape_type
                """,
                [platform, platform],
            )
            backend = task_backend
            task_rows.extend(rows)

    totals = {
        "platforms_ready": sum(1 for row in platform_rows if row["ready"]),
        "authors": sum(int(row["authors"] or 0) for row in platform_rows),
        "content": sum(int(row["content"] or 0) for row in platform_rows),
        "comments": sum(int(row["comments"] or 0) for row in platform_rows),
        "tasks": sum(int(row["tasks"] or 0) for row in task_rows),
    }
    return {
        "backend": backend,
        "platforms": platform_rows,
        "collections": collection_rows,
        "tasks": task_rows,
        "totals": totals,
        "platform_details": platform_details,
        "charts": {
            "platform_records": chart_rows(platform_rows, "label", "total_rows"),
            "content_mix": chart_rows(platform_rows, "label", "content"),
            "comment_mix": chart_rows(platform_rows, "label", "comments"),
            "task_types": chart_rows(task_rows, "scrape_type", "tasks"),
        },
    }


def collection_counts(db_path: Path, platform: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for collection, config in PLATFORMS[platform].collections.items():
        result, _ = run_query(
            db_path,
            f"""
            SELECT
                '{collection}' AS collection,
                '{config.label}' AS label,
                count(*) AS rows,
                min(updated_at) AS first_updated_at,
                max(updated_at) AS last_updated_at
            FROM ({source_select(db_path, platform, collection)}) source
            """,
        )
        rows.extend(result)
    return rows


def platform_detail_stats(db_path: Path, platform: str) -> dict[str, Any]:
    if platform == "weibo":
        return weibo_detail_stats(db_path)
    if platform == "rednote":
        return rednote_detail_stats(db_path)
    if platform == "douyin":
        return douyin_detail_stats(db_path)
    return empty_platform_detail(platform)


def empty_platform_detail(platform: str) -> dict[str, Any]:
    return {
        "platform": platform,
        "status": [],
        "coverage": {},
        "top_keywords": [],
        "top_authors": [],
        "top_content": [],
        "comment_summary": {},
        "numeric": [],
    }


def weibo_detail_stats(db_path: Path) -> dict[str, Any]:
    status = grouped_count(db_path, "weibo", "weibo_posts_raw", "status", "UNKNOWN", "posts")
    coverage, _ = run_query(
        db_path,
        f"""
        WITH posts AS ({source_select(db_path, 'weibo', 'weibo_posts_raw')})
        SELECT
            count(*) AS posts,
            count(*) FILTER (WHERE nullif(url, '') IS NOT NULL) AS posts_with_url,
            count(*) FILTER (
                WHERE nullif(json_extract_string(data, '$.content_html'), '') IS NOT NULL
                   OR nullif(json_extract_string(data, '$.html'), '') IS NOT NULL
            ) AS posts_with_html,
            count(DISTINCT author_id) AS authors_with_posts
        FROM posts
        """,
    )
    return {
        "platform": "weibo",
        "status": status,
        "coverage": coverage[0] if coverage else {},
        "top_keywords": top_keywords(db_path, "weibo", "weibo_posts_raw"),
        "top_authors": top_authors(db_path, "weibo", "weibo_posts_raw"),
        "top_content": top_content_by_comments(db_path, "weibo"),
        "comment_summary": comments_per_content(db_path, "weibo", "weibo_comments", "post_id"),
        "numeric": author_numeric_stats(
            db_path,
            "weibo",
            "weibo_authors",
            {
                "Followers": "$.num_followers",
                "Following": "$.num_following",
                "Posts": "$.num_posts",
                "Received Likes": "$.num_received_likes",
            },
        ),
    }


def rednote_detail_stats(db_path: Path) -> dict[str, Any]:
    status = grouped_count(db_path, "rednote", "rednote_posts_raw", "status", "UNKNOWN", "posts")
    metadata_summary, _ = run_query(
        db_path,
        f"""
        WITH notes AS ({source_select(db_path, 'rednote', 'rednote_post_metadata')})
        SELECT
            count(*) AS search_notes,
            count(DISTINCT author_id) AS search_authors,
            avg(try_cast(json_extract_string(data, '$.liked_count') AS DOUBLE)) AS avg_likes,
            avg(try_cast(json_extract_string(data, '$.comment_count') AS DOUBLE)) AS avg_comments,
            max(try_cast(json_extract_string(data, '$.liked_count') AS DOUBLE)) AS max_likes
        FROM notes
        """,
    )
    return {
        "platform": "rednote",
        "status": status,
        "coverage": metadata_summary[0] if metadata_summary else {},
        "top_keywords": top_keywords(db_path, "rednote", "rednote_post_metadata"),
        "top_authors": top_authors(db_path, "rednote", "rednote_post_metadata"),
        "top_content": top_rednote_notes(db_path),
        "comment_summary": comments_per_content(db_path, "rednote", "rednote_comments", "post_id"),
        "numeric": numeric_stats(
            db_path,
            "rednote",
            "rednote_post_metadata",
            {
                "Likes": "$.liked_count",
                "Collects": "$.collected_count",
                "Comments": "$.comment_count",
                "Shares": "$.shared_count",
            },
        ),
    }


def douyin_detail_stats(db_path: Path) -> dict[str, Any]:
    status = grouped_count(db_path, "douyin", "douyin_videos_raw", "status", "UNKNOWN", "videos")
    video_summary, _ = run_query(
        db_path,
        f"""
        WITH videos AS ({source_select(db_path, 'douyin', 'douyin_videos_raw')})
        SELECT
            count(*) AS videos,
            count(DISTINCT author_id) AS authors_with_videos,
            avg(try_cast(json_extract_string(data, '$.digg_count') AS DOUBLE)) AS avg_likes,
            avg(try_cast(json_extract_string(data, '$.comment_count') AS DOUBLE)) AS avg_comments,
            max(try_cast(json_extract_string(data, '$.play_count') AS DOUBLE)) AS max_plays
        FROM videos
        """,
    )
    return {
        "platform": "douyin",
        "status": status,
        "coverage": video_summary[0] if video_summary else {},
        "top_keywords": [],
        "top_authors": top_authors(db_path, "douyin", "douyin_videos_raw"),
        "top_content": top_douyin_videos(db_path),
        "comment_summary": comments_per_content(db_path, "douyin", "douyin_comments_raw", "post_id"),
        "numeric": numeric_stats(
            db_path,
            "douyin",
            "douyin_videos_raw",
            {
                "Likes": "$.digg_count",
                "Comments": "$.comment_count",
                "Shares": "$.share_count",
                "Collects": "$.collect_count",
                "Plays": "$.play_count",
            },
        ),
    }


def grouped_count(
    db_path: Path,
    platform: str,
    collection: str,
    column: str,
    default_label: str,
    count_label: str,
) -> list[dict[str, Any]]:
    rows, _ = run_query(
        db_path,
        f"""
        WITH source AS ({source_select(db_path, platform, collection)})
        SELECT
            coalesce(nullif({column}, ''), '{default_label}') AS label,
            count(*) AS {count_label}
        FROM source
        GROUP BY 1
        ORDER BY {count_label} DESC, label
        """,
    )
    return rows


def top_keywords(db_path: Path, platform: str, collection: str) -> list[dict[str, Any]]:
    rows, _ = run_query(
        db_path,
        f"""
        WITH source AS ({source_select(db_path, platform, collection)})
        SELECT keyword, count(*) AS records
        FROM source
        GROUP BY keyword
        HAVING keyword IS NOT NULL AND keyword != ''
        ORDER BY records DESC, keyword
        LIMIT 12
        """,
    )
    return rows


def top_authors(db_path: Path, platform: str, collection: str) -> list[dict[str, Any]]:
    rows, _ = run_query(
        db_path,
        f"""
        WITH source AS ({source_select(db_path, platform, collection)})
        SELECT author_id, count(*) AS records
        FROM source
        GROUP BY author_id
        HAVING author_id IS NOT NULL AND author_id != ''
        ORDER BY records DESC, author_id
        LIMIT 12
        """,
    )
    return rows


def top_content_by_comments(db_path: Path, platform: str) -> list[dict[str, Any]]:
    comment_collection = f"{platform}_comments"
    if platform == "douyin":
        comment_collection = "douyin_comments_raw"
    rows, _ = run_query(
        db_path,
        f"""
        WITH comments AS ({source_select(db_path, platform, comment_collection)})
        SELECT post_id AS content_id, count(*) AS value
        FROM comments
        GROUP BY post_id
        HAVING post_id IS NOT NULL AND post_id != ''
        ORDER BY value DESC, content_id
        LIMIT 12
        """,
    )
    return rows


def top_rednote_notes(db_path: Path) -> list[dict[str, Any]]:
    rows, _ = run_query(
        db_path,
        f"""
        WITH notes AS ({source_select(db_path, 'rednote', 'rednote_post_metadata')})
        SELECT
            id AS content_id,
            coalesce(json_extract_string(data, '$.title'), id) AS title,
            try_cast(json_extract_string(data, '$.liked_count') AS BIGINT) AS value
        FROM notes
        ORDER BY value DESC NULLS LAST, title
        LIMIT 12
        """,
    )
    return rows


def top_douyin_videos(db_path: Path) -> list[dict[str, Any]]:
    rows, _ = run_query(
        db_path,
        f"""
        WITH videos AS ({source_select(db_path, 'douyin', 'douyin_videos_raw')})
        SELECT
            id AS content_id,
            coalesce(json_extract_string(data, '$.desc'), id) AS title,
            try_cast(json_extract_string(data, '$.play_count') AS BIGINT) AS value
        FROM videos
        ORDER BY value DESC NULLS LAST, title
        LIMIT 12
        """,
    )
    return rows


def comments_per_content(
    db_path: Path,
    platform: str,
    collection: str,
    content_key: str,
) -> dict[str, Any]:
    rows, _ = run_query(
        db_path,
        f"""
        WITH per_content AS (
            SELECT {content_key}, count(*) AS comment_count
            FROM ({source_select(db_path, platform, collection)}) comments
            GROUP BY {content_key}
            HAVING {content_key} IS NOT NULL AND {content_key} != ''
        )
        SELECT
            count(*) AS commented_content,
            min(comment_count) AS min_comments,
            avg(comment_count) AS avg_comments,
            median(comment_count) AS median_comments,
            max(comment_count) AS max_comments
        FROM per_content
        """,
    )
    return rows[0] if rows else {}


def author_numeric_stats(
    db_path: Path,
    platform: str,
    collection: str,
    metrics: Mapping[str, str],
) -> list[dict[str, Any]]:
    return numeric_stats(db_path, platform, collection, metrics)


def numeric_stats(
    db_path: Path,
    platform: str,
    collection: str,
    metrics: Mapping[str, str],
) -> list[dict[str, Any]]:
    if not metrics:
        return []
    branches = [
        f"SELECT '{label}' AS metric, try_cast(json_extract_string(data, '{path}') AS DOUBLE) AS value FROM ({source_select(db_path, platform, collection)}) source"
        for label, path in metrics.items()
    ]
    rows, _ = run_query(
        db_path,
        f"""
        SELECT
            metric,
            count(*) AS n,
            min(value) AS min,
            avg(value) AS avg,
            median(value) AS median,
            max(value) AS max
        FROM ({' UNION ALL '.join(branches)})
        WHERE value IS NOT NULL
        GROUP BY metric
        ORDER BY metric
        """,
    )
    return rows


def chart_rows(rows: list[dict[str, Any]], label_key: str, value_key: str) -> list[dict[str, Any]]:
    grouped: dict[str, float] = {}
    for row in rows:
        label = str(row.get(label_key) or "Unknown")
        value = safe_float(row.get(value_key))
        grouped[label] = grouped.get(label, 0.0) + value
    max_value = max(grouped.values(), default=0.0)
    return [
        {"label": label, "value": value, "pct": (value / max_value * 100) if max_value else 0}
        for label, value in sorted(grouped.items(), key=lambda item: item[1], reverse=True)
        if value
    ]


def search_filter_sql(platform: str, collection: str, query: str) -> tuple[str, list[Any]]:
    if not query:
        return "", []

    like = f"%{query.lower()}%"
    clauses = ["lower(id) LIKE ?"]
    params: list[Any] = [like]
    for column in ["task_id", "author_id", "post_id", "keyword", "status", "url"]:
        clauses.append(f"lower(coalesce({column}, '')) LIKE ?")
        params.append(like)
    for json_path in PLATFORMS[platform].collections[collection].search_fields:
        clauses.append(f"lower(coalesce(json_extract_string(data, '{json_path}'), '')) LIKE ?")
        params.append(like)
    clauses.append("lower(data) LIKE ?")
    params.append(like)
    return " AND (" + " OR ".join(clauses) + ")", params


def list_records(
    db_path: Path,
    platform: str,
    collection: str,
    *,
    query: str = "",
    page: int = 1,
    per_page: int = 25,
) -> dict[str, Any]:
    page = max(page, 1)
    per_page = min(max(per_page, 5), 100)
    offset = (page - 1) * per_page
    where_sql, params = search_filter_sql(platform, collection, query)

    source = source_select(db_path, platform, collection)
    count_rows, backend = run_query(
        db_path,
        f"SELECT count(*) AS total FROM ({source}) source WHERE 1 = 1{where_sql}",
        params,
    )
    total = int(count_rows[0]["total"]) if count_rows else 0

    rows, _ = run_query(
        db_path,
        f"""
        SELECT id, updated_at, data
        FROM ({source}) source
        WHERE 1 = 1{where_sql}
        ORDER BY updated_at DESC NULLS LAST, id
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, offset],
    )
    records = [summarize_record(platform, collection, row) for row in rows if row.get("data")]
    return {
        "backend": backend,
        "records": records,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max((total + per_page - 1) // per_page, 1),
    }


def get_record(
    db_path: Path,
    platform: str,
    collection: str,
    record_id: str,
) -> tuple[dict[str, Any] | None, str]:
    rows, backend = run_query(
        db_path,
        f"""
        SELECT id, updated_at, data
        FROM ({source_select(db_path, platform, collection)}) source
        WHERE id = ?
        """,
        [record_id],
    )
    if not rows or rows[0].get("data") is None:
        return None, backend
    row = rows[0]
    row["record"] = json.loads(row["data"])
    row["pretty_json"] = json.dumps(row["record"], ensure_ascii=False, indent=2, default=str)
    return row, backend


def summarize_record(platform: str, collection: str, row: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(row["data"])
    summary: dict[str, Any] = {
        "id": row["id"],
        "updated_at": row["updated_at"],
        "title": row["id"],
        "subtitle": "",
        "meta": [],
    }

    if collection.endswith("_authors"):
        summary["title"] = (
            data.get("name")
            or data.get("nickname")
            or data.get("unique_id")
            or data.get("uid")
            or data.get("sec_user_id")
            or row["id"]
        )
        summary["subtitle"] = data.get("verified_reason") or data.get("signature") or data.get("location") or ""
        summary["meta"] = [
            ("Followers", format_number(data.get("num_followers") or data.get("follower_count"))),
            ("Following", format_number(data.get("num_following") or data.get("following_count"))),
            ("Posts", format_number(data.get("num_posts") or data.get("aweme_count"))),
            ("Verified", yes_no(data.get("is_verified") or data.get("verification_type"))),
        ]
    elif collection == "rednote_post_metadata":
        summary["title"] = data.get("title") or row["id"]
        summary["subtitle"] = data.get("author_name") or data.get("url") or ""
        summary["meta"] = [
            ("Keyword", data.get("search_keyword")),
            ("Likes", format_number(data.get("liked_count"))),
            ("Comments", format_number(data.get("comment_count"))),
            ("Images", format_number(data.get("image_count"))),
        ]
    elif collection in {"weibo_posts_raw", "rednote_posts_raw", "douyin_videos_raw"}:
        summary["title"] = data.get("desc") or data.get("content_text") or data.get("url") or data.get("uid") or row["id"]
        summary["subtitle"] = compact_text(data.get("content_html") or data.get("html") or data.get("desc") or "")
        summary["meta"] = [
            ("Author", data.get("author_id") or data.get("sec_user_id") or data.get("author_nickname")),
            ("Status", data.get("status")),
            ("Task", data.get("task_id")),
            ("Comments", format_number(data.get("comment_count"))),
        ]
    elif collection in {"weibo_comments", "rednote_comments", "douyin_comments_raw"}:
        summary["title"] = data.get("comment_text") or data.get("text") or data.get("comment_id") or row["id"]
        summary["subtitle"] = f"Content {data.get('post_id') or data.get('aweme_id')}" if (data.get("post_id") or data.get("aweme_id")) else ""
        summary["meta"] = [
            ("Content", data.get("post_id") or data.get("aweme_id")),
            ("User", data.get("user_id") or data.get("user_uid") or data.get("user_nickname")),
            ("Likes", format_number(data.get("like_count") or data.get("like_counts") or data.get("digg_count"))),
            ("Task", data.get("task_id")),
        ]
    else:
        summary["subtitle"] = compact_text(row["data"])

    summary["meta"] = [(label, value) for label, value in summary["meta"] if value not in (None, "")]
    return summary


def author_count(platform: str, count_map: Mapping[str, Any]) -> int:
    return int(count_map.get(f"{platform}_authors", 0) or 0)


def content_count(platform: str, count_map: Mapping[str, Any]) -> int:
    if platform == "weibo":
        return int(count_map.get("weibo_posts_raw", 0) or 0)
    if platform == "rednote":
        return int(count_map.get("rednote_posts_raw", 0) or 0) + int(count_map.get("rednote_post_metadata", 0) or 0)
    if platform == "douyin":
        return int(count_map.get("douyin_videos_raw", 0) or 0)
    return 0


def comment_count(platform: str, count_map: Mapping[str, Any]) -> int:
    collection = "douyin_comments_raw" if platform == "douyin" else f"{platform}_comments"
    return int(count_map.get(collection, 0) or 0)


def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def compact_text(value: Any, limit: int = 180) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def format_number(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def yes_no(value: Any) -> str:
    return "Yes" if bool(value) else "No"
