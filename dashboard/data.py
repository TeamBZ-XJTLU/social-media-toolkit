from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import duckdb


WEIBO_COLLECTIONS = {
    "weibo_authors": "Authors",
    "weibo_posts_raw": "Posts",
    "weibo_comments": "Comments",
}

WEIBO_TABLES = {
    "weibo_authors": "weibo_authors",
    "weibo_posts_raw": "weibo_posts",
    "weibo_comments": "weibo_comments",
}

COLLECTION_SEARCH_FIELDS = {
    "weibo_authors": ["$.id", "$.uid", "$.name", "$.location", "$.verified_reason"],
    "weibo_posts_raw": ["$.id", "$.uid", "$.author_id", "$.url", "$.status"],
    "weibo_comments": ["$.id", "$.comment_id", "$.post_id", "$.user_id", "$.text"],
}


@contextmanager
def duckdb_connection(db_path: Path) -> Iterator[tuple[duckdb.DuckDBPyConnection, str]]:
    """Open DuckDB read-only, falling back to a temporary snapshot if locked."""
    temp_dir: TemporaryDirectory[str] | None = None
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
        try:
            conn.close()
        except Exception:
            pass
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


def run_scalar(db_path: Path, sql: str, params: list[Any] | None = None) -> Any:
    rows, _ = run_query(db_path, sql, params)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def table_exists(db_path: Path, table: str) -> bool:
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


def source_select(db_path: Path, collection: str) -> str:
    table = WEIBO_TABLES[collection]
    if table_exists(db_path, table):
        return f"""
            SELECT id, data, updated_at, task_id, author_id, post_id, keyword, status, url
            FROM {table}
        """
    return f"""
        SELECT
            id,
            data,
            updated_at,
            json_extract_string(data, '$.task_id') AS task_id,
            json_extract_string(data, '$.author_id') AS author_id,
            json_extract_string(data, '$.post_id') AS post_id,
            json_extract_string(data, '$.search_keyword') AS keyword,
            json_extract_string(data, '$.status') AS status,
            json_extract_string(data, '$.url') AS url
        FROM records
        WHERE collection = '{collection}'
    """


def database_ready(db_path: Path) -> bool:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False
    return table_exists(db_path, "weibo_posts") or table_exists(db_path, "records")


def collection_counts(db_path: Path) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    backend = "duckdb"
    for collection in WEIBO_COLLECTIONS:
        result, backend = run_query(
            db_path,
            f"""
            SELECT
                '{collection}' AS collection,
                count(*) AS rows,
                min(updated_at) AS first_updated_at,
                max(updated_at) AS last_updated_at
            FROM ({source_select(db_path, collection)}) source
            """,
        )
        rows.extend(result)
    return rows, backend


def overview_stats(db_path: Path) -> dict[str, Any]:
    counts, backend = collection_counts(db_path)
    post_status, _ = run_query(
        db_path,
        f"""
        WITH posts AS ({source_select(db_path, 'weibo_posts_raw')})
        SELECT
            coalesce(nullif(status, ''), 'UNKNOWN') AS status,
            count(*) AS posts
        FROM posts
        GROUP BY status
        ORDER BY posts DESC
        """,
    )
    post_coverage, _ = run_query(
        db_path,
        f"""
        WITH posts AS ({source_select(db_path, 'weibo_posts_raw')})
        SELECT
            count(*) AS posts,
            count(*) FILTER (
                WHERE nullif(url, '') IS NOT NULL
            ) AS posts_with_url,
            count(*) FILTER (
                WHERE nullif(json_extract_string(data, '$.content_html'), '') IS NOT NULL
                   OR nullif(json_extract_string(data, '$.html'), '') IS NOT NULL
            ) AS posts_with_html,
            count(DISTINCT author_id) AS authors_with_posts
        FROM posts
        """,
    )
    comments_per_post, _ = run_query(
        db_path,
        f"""
        WITH per_post AS (
            SELECT
                post_id,
                count(*) AS comment_count
            FROM ({source_select(db_path, 'weibo_comments')}) comments
            GROUP BY post_id
            HAVING post_id IS NOT NULL AND post_id != ''
        )
        SELECT
            count(*) AS commented_posts,
            min(comment_count) AS min_comments,
            avg(comment_count) AS avg_comments,
            median(comment_count) AS median_comments,
            max(comment_count) AS max_comments
        FROM per_post
        """,
    )
    author_numeric, _ = run_query(
        db_path,
        f"""
        SELECT
            metric,
            count(*) AS n,
            min(value) AS min,
            avg(value) AS avg,
            median(value) AS median,
            max(value) AS max
        FROM (
            SELECT 'num_followers' AS metric, try_cast(json_extract_string(data, '$.num_followers') AS DOUBLE) AS value FROM ({source_select(db_path, 'weibo_authors')}) authors
            UNION ALL SELECT 'num_following', try_cast(json_extract_string(data, '$.num_following') AS DOUBLE) FROM ({source_select(db_path, 'weibo_authors')}) authors
            UNION ALL SELECT 'num_posts', try_cast(json_extract_string(data, '$.num_posts') AS DOUBLE) FROM ({source_select(db_path, 'weibo_authors')}) authors
            UNION ALL SELECT 'num_received_comments', try_cast(json_extract_string(data, '$.num_received_comments') AS DOUBLE) FROM ({source_select(db_path, 'weibo_authors')}) authors
            UNION ALL SELECT 'num_received_likes', try_cast(json_extract_string(data, '$.num_received_likes') AS DOUBLE) FROM ({source_select(db_path, 'weibo_authors')}) authors
            UNION ALL SELECT 'num_received_reposts', try_cast(json_extract_string(data, '$.num_received_reposts') AS DOUBLE) FROM ({source_select(db_path, 'weibo_authors')}) authors
        )
        WHERE value IS NOT NULL
        GROUP BY metric
        ORDER BY metric
        """,
    )
    top_commented_posts, _ = run_query(
        db_path,
        f"""
        SELECT
            post_id,
            count(*) AS comments
        FROM ({source_select(db_path, 'weibo_comments')}) comments
        GROUP BY post_id
        HAVING post_id IS NOT NULL AND post_id != ''
        ORDER BY comments DESC, post_id
        LIMIT 12
        """,
    )
    top_authors_by_posts, _ = run_query(
        db_path,
        f"""
        SELECT
            author_id,
            count(*) AS posts,
            count(*) FILTER (
                WHERE status = 'RETRIEVED'
            ) AS retrieved_posts
        FROM ({source_select(db_path, 'weibo_posts_raw')}) posts
        GROUP BY author_id
        HAVING author_id IS NOT NULL AND author_id != ''
        ORDER BY posts DESC, author_id
        LIMIT 12
        """,
    )

    count_map = {row["collection"]: row["rows"] for row in counts}
    coverage = post_coverage[0] if post_coverage else {}
    comment_summary = comments_per_post[0] if comments_per_post else {}
    return {
        "backend": backend,
        "counts": counts,
        "count_map": count_map,
        "post_status": post_status,
        "post_coverage": coverage,
        "comments_per_post": comment_summary,
        "author_numeric": author_numeric,
        "top_commented_posts": top_commented_posts,
        "top_authors_by_posts": top_authors_by_posts,
    }


def search_filter_sql(collection: str, query: str) -> tuple[str, list[Any]]:
    if not query:
        return "", []

    like = f"%{query.lower()}%"
    clauses = ["lower(id) LIKE ?"]
    params: list[Any] = [like]
    for column in ["task_id", "author_id", "post_id", "keyword", "status", "url"]:
        clauses.append(f"lower(coalesce({column}, '')) LIKE ?")
        params.append(like)
    for json_path in COLLECTION_SEARCH_FIELDS.get(collection, []):
        clauses.append(f"lower(coalesce(json_extract_string(data, '{json_path}'), '')) LIKE ?")
        params.append(like)
    clauses.append("lower(data) LIKE ?")
    params.append(like)
    return " AND (" + " OR ".join(clauses) + ")", params


def list_records(
    db_path: Path,
    collection: str,
    *,
    query: str = "",
    page: int = 1,
    per_page: int = 25,
) -> dict[str, Any]:
    page = max(page, 1)
    per_page = min(max(per_page, 5), 100)
    offset = (page - 1) * per_page
    where_sql, params = search_filter_sql(collection, query)

    source = source_select(db_path, collection)
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
        ORDER BY updated_at DESC, id
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, offset],
    )
    records = [summarize_record(collection, row) for row in rows]
    return {
        "backend": backend,
        "records": records,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max((total + per_page - 1) // per_page, 1),
    }


def get_record(db_path: Path, collection: str, record_id: str) -> tuple[dict[str, Any] | None, str]:
    rows, backend = run_query(
        db_path,
        f"""
        SELECT id, updated_at, data
        FROM ({source_select(db_path, collection)}) source
        WHERE id = ?
        """,
        [record_id],
    )
    if not rows:
        return None, backend
    row = rows[0]
    row["record"] = json.loads(row["data"])
    row["pretty_json"] = json.dumps(row["record"], ensure_ascii=False, indent=2, default=str)
    return row, backend


def summarize_record(collection: str, row: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(row["data"])
    summary: dict[str, Any] = {
        "id": row["id"],
        "updated_at": row["updated_at"],
        "title": row["id"],
        "subtitle": "",
        "meta": [],
    }

    if collection == "weibo_authors":
        summary["title"] = data.get("name") or data.get("uid") or row["id"]
        summary["subtitle"] = data.get("verified_reason") or data.get("location") or ""
        summary["meta"] = [
            ("Followers", format_number(data.get("num_followers"))),
            ("Following", format_number(data.get("num_following"))),
            ("Posts", format_number(data.get("num_posts"))),
            ("Verified", yes_no(data.get("is_verified"))),
        ]
    elif collection == "weibo_posts_raw":
        summary["title"] = data.get("url") or data.get("uid") or row["id"]
        summary["subtitle"] = compact_text(data.get("content_html") or data.get("html") or "")
        summary["meta"] = [
            ("Author", data.get("author_id")),
            ("Status", data.get("status")),
            ("Task", data.get("task_id")),
            ("Has HTML", yes_no(data.get("content_html") or data.get("html"))),
        ]
    elif collection == "weibo_comments":
        summary["title"] = data.get("text") or data.get("comment_id") or row["id"]
        summary["subtitle"] = f"Post {data.get('post_id')}" if data.get("post_id") else ""
        summary["meta"] = [
            ("Post", data.get("post_id")),
            ("User", data.get("user_id") or data.get("user_name")),
            ("Likes", format_number(data.get("like_count") or data.get("likes"))),
            ("Root", data.get("rootid") or data.get("root_id")),
        ]
    else:
        summary["subtitle"] = compact_text(row["data"])

    summary["meta"] = [(label, value) for label, value in summary["meta"] if value not in (None, "")]
    return summary


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
