from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

import orjson
import duckdb

from .errors import DatabaseCorruptError, DuplicateRecordError, RecordNotFoundError


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
Record = dict[str, JsonValue]

_DUMP_OPTIONS = orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE


_COLLECTION_TABLES = {
    "weibo_authors": "weibo_authors",
    "weibo_posts": "weibo_posts",
    "weibo_posts_raw": "weibo_posts",
    "weibo_comments": "weibo_comments",
    "rednote_authors": "rednote_authors",
    "rednote_posts": "rednote_posts",
    "rednote_posts_raw": "rednote_posts",
    "rednote_post_metadata": "rednote_post_metadata",
    "rednote_comments": "rednote_comments",
    "douyin_authors": "douyin_authors",
    "douyin_posts": "douyin_posts",
    "douyin_videos_raw": "douyin_posts",
    "douyin_comments": "douyin_comments",
    "douyin_comments_raw": "douyin_comments",
}

_PLATFORM_TABLES = sorted(set(_COLLECTION_TABLES.values()))

_REDNOTE_POST_COLUMNS = {
    "title": "TEXT",
    "time": "BIGINT",
    "lastUpdateTime": "BIGINT",
    "noteId": "TEXT",
    "ipLocation": "TEXT",
    "followed": "BOOLEAN",
    "relation": "TEXT",
    "liked": "BOOLEAN",
    "likedCount": "BIGINT",
    "collected": "BOOLEAN",
    "collectedCount": "BIGINT",
    "commentCount": "BIGINT",
    "shareCount": "BIGINT",
}


class DuckDBDatabase:
    """CRUD database persisted to DuckDB platform tables.

    Known social-media collections are stored in dedicated physical tables such
    as `weibo_posts`, `rednote_posts`, and `douyin_comments`. Each table keeps a
    flexible JSON `data` payload plus indexed columns used by dashboards and
    task-level retrieval.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self.path))
        self._init_schema()

    def create(
        self,
        collection: str,
        record: Mapping[str, JsonValue],
        record_id: str | None = None,
    ) -> Record:
        self._validate_collection(collection)
        new_record = self._prepare_record(record, record_id)
        rid = str(new_record["id"])

        with self._lock:
            if self.read(collection, rid) is not None:
                raise DuplicateRecordError(
                    f"Record {rid!r} already exists in collection {collection!r}."
                )
            self._insert_record(collection, rid, new_record)
            return new_record.copy()

    def read(self, collection: str, record_id: str) -> Record | None:
        self._validate_collection(collection)
        rid = self._validate_record_id(record_id)

        with self._lock:
            table = self._table_for_collection(collection)
            if table is None:
                row = self._conn.execute(
                    "SELECT data FROM records WHERE collection = ? AND id = ?",
                    [collection, rid],
                ).fetchone()
            else:
                row = self._conn.execute(
                    f"SELECT data FROM {table} WHERE id = ?",
                    [rid],
                ).fetchone()
            if row is None:
                return None
            return self._load_record(row[0])

    def list(self, collection: str) -> list[Record]:
        self._validate_collection(collection)

        with self._lock:
            table = self._table_for_collection(collection)
            if table is None:
                rows = self._conn.execute(
                    "SELECT data FROM records WHERE collection = ? ORDER BY id",
                    [collection],
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT data FROM {table} ORDER BY id",
                ).fetchall()
            return [self._load_record(row[0]) for row in rows]

    def update(
        self,
        collection: str,
        record_id: str,
        changes: Mapping[str, JsonValue],
    ) -> Record:
        self._validate_collection(collection)
        rid = self._validate_record_id(record_id)

        if not isinstance(changes, Mapping):
            raise TypeError("changes must be a mapping.")
        if "id" in changes and str(changes["id"]) != rid:
            raise ValueError("Record id cannot be changed.")

        with self._lock:
            existing = self.read(collection, rid)
            if existing is None:
                raise RecordNotFoundError(
                    f"Record {rid!r} does not exist in collection {collection!r}."
                )
            updated = existing.copy()
            updated.update(dict(changes))
            updated["id"] = rid
            self._assert_json_serializable(updated)
            self._replace_existing(collection, rid, updated)
            return updated.copy()

    def replace(
        self,
        collection: str,
        record_id: str,
        record: Mapping[str, JsonValue],
    ) -> Record:
        self._validate_collection(collection)
        rid = self._validate_record_id(record_id)
        new_record = self._prepare_record(record, rid)

        with self._lock:
            if self.read(collection, rid) is None:
                raise RecordNotFoundError(
                    f"Record {rid!r} does not exist in collection {collection!r}."
                )
            self._replace_existing(collection, rid, new_record)
            return new_record.copy()

    def delete(self, collection: str, record_id: str) -> bool:
        self._validate_collection(collection)
        rid = self._validate_record_id(record_id)

        with self._lock:
            existing = self.read(collection, rid)
            if existing is None:
                return False
            self._delete_existing(collection, rid)
            return True

    def clear(self, collection: str | None = None) -> None:
        with self._lock:
            if collection is None:
                self._conn.execute("DELETE FROM records")
                for table in _PLATFORM_TABLES:
                    self._conn.execute(f"DELETE FROM {table}")
                self._conn.execute("DELETE FROM tasks")
                return

            self._validate_collection(collection)
            table = self._table_for_collection(collection)
            if table is None:
                self._conn.execute("DELETE FROM records WHERE collection = ?", [collection])
            else:
                self._conn.execute(f"DELETE FROM {table}")

    def save_task(
        self,
        task_id: str,
        *,
        platform: str,
        scrape_type: str,
        condition: str,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> Record:
        rid = self._validate_record_id(task_id)
        record: Record = {
            "id": rid,
            "platform": platform,
            "scrape_type": scrape_type,
            "condition": condition,
            "metadata": dict(metadata or {}),
        }
        self._assert_json_serializable(record)
        payload = self._dump_record(record)
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM tasks WHERE id = ?",
                [rid],
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO tasks (
                        id, platform, scrape_type, condition, data, started_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, current_timestamp, current_timestamp)
                    """,
                    [rid, platform, scrape_type, condition, payload],
                )
            else:
                self._conn.execute(
                    """
                    UPDATE tasks
                       SET platform = ?,
                           scrape_type = ?,
                           condition = ?,
                           data = ?,
                           updated_at = current_timestamp
                     WHERE id = ?
                    """,
                    [platform, scrape_type, condition, payload, rid],
                )
            return record.copy()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS storage_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    scrape_type TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    data TEXT NOT NULL,
                    started_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
                    updated_at TIMESTAMP NOT NULL DEFAULT current_timestamp
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS tasks_platform_idx ON tasks(platform)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS tasks_type_idx ON tasks(scrape_type)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    collection TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
                    PRIMARY KEY (collection, id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS records_collection_idx ON records(collection)"
            )
            for table in _PLATFORM_TABLES:
                self._create_platform_table(table)
            self._migrate_legacy_records_table()

    def _create_platform_table(self, table: str) -> None:
        extra_columns = ""
        if table == "rednote_posts":
            extra_columns = "".join(
                f",\n                {self._quote_identifier(column)} {column_type}"
                for column, column_type in _REDNOTE_POST_COLUMNS.items()
            )
        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
                task_id TEXT,
                author_id TEXT,
                post_id TEXT,
                keyword TEXT,
                status TEXT,
                url TEXT
                {extra_columns}
            )
            """
        )
        if table == "rednote_posts":
            for column, column_type in _REDNOTE_POST_COLUMNS.items():
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                    f"{self._quote_identifier(column)} {column_type}"
                )
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_task_idx ON {table}(task_id)")
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_author_idx ON {table}(author_id)")
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_post_idx ON {table}(post_id)")
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_keyword_idx ON {table}(keyword)")
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_status_idx ON {table}(status)")
        self._conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_updated_idx ON {table}(updated_at)")

    def _replace_existing(self, collection: str, record_id: str, record: Record) -> None:
        self._delete_existing(collection, record_id)
        self._insert_record(collection, record_id, record)

    def _insert_record(
        self,
        collection: str,
        record_id: str,
        record: Record,
        *,
        updated_at: Any | None = None,
    ) -> None:
        payload = self._dump_record(record)
        table = self._table_for_collection(collection)
        if table is None:
            if updated_at is None:
                self._conn.execute(
                    """
                    INSERT INTO records (collection, id, data, updated_at)
                    VALUES (?, ?, ?, current_timestamp)
                    """,
                    [collection, record_id, payload],
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO records (collection, id, data, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [collection, record_id, payload, updated_at],
                )
            return

        indexed = self._indexed_values(collection, record)
        extra_indexed = self._extra_indexed_values(table, record)
        columns = ["id", "data", "updated_at", "task_id", "author_id", "post_id", "keyword", "status", "url"]
        values: list[Any] = [
            record_id,
            payload,
            updated_at,
            indexed["task_id"],
            indexed["author_id"],
            indexed["post_id"],
            indexed["keyword"],
            indexed["status"],
            indexed["url"],
        ]
        if updated_at is None:
            values[2] = None
        for column, value in extra_indexed.items():
            columns.append(column)
            values.append(value)

        columns_sql = ", ".join(self._quote_identifier(column) for column in columns)
        if updated_at is None:
            placeholders = ", ".join(
                "current_timestamp" if column == "updated_at" else "?"
                for column in columns
            )
            bind_values = [
                value
                for column, value in zip(columns, values, strict=True)
                if column != "updated_at"
            ]
            self._conn.execute(
                f"""
                INSERT INTO {table} ({columns_sql})
                VALUES ({placeholders})
                """,
                bind_values,
            )
        else:
            placeholders = ", ".join("?" for _ in columns)
            self._conn.execute(
                f"""
                INSERT INTO {table} ({columns_sql})
                VALUES ({placeholders})
                """,
                values,
            )

    def _delete_existing(self, collection: str, record_id: str) -> None:
        table = self._table_for_collection(collection)
        if table is None:
            self._conn.execute(
                "DELETE FROM records WHERE collection = ? AND id = ?",
                [collection, record_id],
            )
            return
        self._conn.execute(f"DELETE FROM {table} WHERE id = ?", [record_id])

    def _table_for_collection(self, collection: str) -> str | None:
        return _COLLECTION_TABLES.get(collection)

    def _indexed_values(self, collection: str, record: Mapping[str, JsonValue]) -> dict[str, str | None]:
        return {
            "task_id": self._string_or_none(record.get("task_id")),
            "author_id": self._author_id(collection, record),
            "post_id": self._post_id(collection, record),
            "keyword": self._string_or_none(record.get("search_keyword")),
            "status": self._string_or_none(record.get("status")),
            "url": self._string_or_none(record.get("url")),
        }

    def _extra_indexed_values(self, table: str, record: Mapping[str, JsonValue]) -> dict[str, Any]:
        if table != "rednote_posts":
            return {}
        return {
            column: self._extra_column_value(column, record.get(column))
            for column in _REDNOTE_POST_COLUMNS
        }

    def _extra_column_value(self, column: str, value: Any) -> Any:
        if value is None:
            return None
        column_type = _REDNOTE_POST_COLUMNS[column]
        if column_type == "BIGINT":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            if isinstance(value, str):
                normalized = value.strip().replace(",", "")
                if not normalized:
                    return None
                try:
                    return int(float(normalized))
                except ValueError:
                    return None
            return None
        if column_type == "BOOLEAN":
            if isinstance(value, bool):
                return value
            if isinstance(value, int | float):
                return bool(value)
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"true", "1", "yes", "y"}:
                    return True
                if normalized in {"false", "0", "no", "n"}:
                    return False
            return None
        return self._string_or_none(value)

    def _author_id(self, collection: str, record: Mapping[str, JsonValue]) -> str | None:
        if collection.endswith("_authors"):
            return self._string_or_none(record.get("id"))
        for key in ("author_id", "sec_user_id", "author_sec_uid", "uid", "user_id"):
            value = self._string_or_none(record.get(key))
            if value:
                return value
        return None

    def _post_id(self, collection: str, record: Mapping[str, JsonValue]) -> str | None:
        if collection in {
            "weibo_posts",
            "weibo_posts_raw",
            "rednote_posts",
            "rednote_posts_raw",
            "rednote_post_metadata",
            "douyin_posts",
            "douyin_videos_raw",
        }:
            return self._string_or_none(record.get("id"))
        for key in ("post_id", "aweme_id", "uid"):
            value = self._string_or_none(record.get(key))
            if value:
                return value
        return None

    def _string_or_none(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, int | float | bool):
            return str(value)
        return None

    def _quote_identifier(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _table_exists(self, table: str) -> bool:
        row = self._conn.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_name = ?
            """,
            [table],
        ).fetchone()
        return bool(row and row[0])

    def _meta_value(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM storage_meta WHERE key = ?",
            [key],
        ).fetchone()
        return str(row[0]) if row else None

    def _set_meta_value(self, key: str, value: str) -> None:
        self._conn.execute("DELETE FROM storage_meta WHERE key = ?", [key])
        self._conn.execute(
            "INSERT INTO storage_meta (key, value) VALUES (?, ?)",
            [key, value],
        )

    def _migrate_legacy_records_table(self) -> None:
        if self._meta_value("legacy_records_migrated") == "1":
            return
        if not self._table_exists("records"):
            self._set_meta_value("legacy_records_migrated", "1")
            return

        rows = self._conn.execute(
            """
            SELECT collection, id, data, updated_at
            FROM records
            WHERE collection IN (
                'weibo_authors', 'weibo_posts_raw', 'weibo_comments',
                'rednote_authors', 'rednote_posts_raw', 'rednote_post_metadata', 'rednote_comments',
                'douyin_authors', 'douyin_videos_raw', 'douyin_comments_raw'
            )
            """
        ).fetchall()
        for collection, record_id, payload, updated_at in rows:
            table = self._table_for_collection(str(collection))
            if table is None:
                continue
            record = self._load_record(payload)
            self._conn.execute(f"DELETE FROM {table} WHERE id = ?", [str(record_id)])
            self._insert_record(
                str(collection),
                str(record_id),
                record,
                updated_at=updated_at,
            )
        self._set_meta_value("legacy_records_migrated", "1")

    def _prepare_record(
        self,
        record: Mapping[str, JsonValue],
        record_id: str | None,
    ) -> Record:
        if not isinstance(record, Mapping):
            raise TypeError("record must be a mapping.")

        prepared = dict(record)
        rid = record_id if record_id is not None else prepared.get("id")
        if rid is None:
            rid = uuid4().hex

        prepared["id"] = self._validate_record_id(str(rid))
        self._assert_json_serializable(prepared)
        return prepared

    def _dump_record(self, record: Mapping[str, JsonValue]) -> str:
        return orjson.dumps(record, option=_DUMP_OPTIONS).decode("utf-8")

    def _load_record(self, payload: str | bytes) -> Record:
        try:
            record = orjson.loads(payload)
        except orjson.JSONDecodeError as exc:
            raise DatabaseCorruptError("Could not decode DuckDB record JSON.") from exc
        if not isinstance(record, dict):
            raise DatabaseCorruptError("DuckDB record payload must be a JSON object.")
        return record.copy()

    def _assert_json_serializable(self, value: Mapping[str, JsonValue]) -> None:
        try:
            orjson.dumps(value)
        except TypeError as exc:
            raise TypeError("record must contain only JSON-serializable values.") from exc

    def _validate_collection(self, collection: str) -> None:
        if not isinstance(collection, str) or not collection.strip():
            raise ValueError("collection must be a non-empty string.")

    def _validate_record_id(self, record_id: str) -> str:
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("record_id must be a non-empty string.")
        return record_id
