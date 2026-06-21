from __future__ import annotations

import json
import re
from hashlib import sha256
from html.parser import HTMLParser
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from storage import DuckDBDatabase, DuplicateRecordError, Record


AUTHOR_COLLECTION = "rednote_authors"
POST_RAW_COLLECTION = "rednote_posts_raw"
POST_METADATA_COLLECTION = "rednote_post_metadata"
POST_MEDIA_FILE_COLLECTION = "rednote_post_media_files"
COMMENT_COLLECTION = "rednote_comments"
INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>",
    re.DOTALL,
)
UNDEFINED_VALUE_RE = re.compile(r"(?<=[:\[,])\s*undefined\s*(?=[,\}\]])")


class RednotePostStatus(StrEnum):
    ID_ONLY = "ID_ONLY"
    RETRIEVED = "RETRIEVED"


class RednoteStore:
    """Rednote/XHS storage adapter backed by DuckDB."""

    def __init__(self, db: DuckDBDatabase) -> None:
        self.db = db

    def ensure_author(
        self,
        author_id: str,
        name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Record:
        existing = self.db.read(AUTHOR_COLLECTION, author_id)
        if existing is not None:
            changes: dict[str, Any] = {}
            if name is not None and existing.get("name") != name:
                changes["name"] = name
            for key, value in (extra or {}).items():
                if value is not None and existing.get(key) != value:
                    changes[key] = value
            if changes:
                return self.db.update(AUTHOR_COLLECTION, author_id, changes)
            return existing

        record: Record = {
            "id": author_id,
            "uid": author_id,
            "name": name,
        }
        if extra:
            record.update({key: value for key, value in extra.items() if value is not None})
        return self.db.create(AUTHOR_COLLECTION, record, author_id)

    def save_post_raw(
        self,
        post_id: str,
        author_id: str,
        *,
        url: str | None = None,
        html: str | None = None,
        task_id: str | None = None,
        handler_id: int | None = None,
        extra: dict[str, Any] | None = None,
        parsed: dict[str, Any] | None = None,
    ) -> Record:
        self.ensure_author(author_id)

        retrieved_at = datetime.now(UTC).isoformat()
        status = RednotePostStatus.RETRIEVED if url and html else RednotePostStatus.ID_ONLY
        record: Record = {
            "id": post_id,
            "uid": post_id,
            "url": url,
            "html": html,
            "updated_at": retrieved_at,
            "status": status.value,
            "author_id": author_id,
            "handler_id": handler_id,
            "task_id": task_id,
        }
        if parsed:
            record.update({key: value for key, value in parsed.items() if value is not None})
        if extra:
            record["extra"] = extra

        existing = self.db.read(POST_RAW_COLLECTION, post_id)
        if existing is None:
            try:
                return self.db.create(POST_RAW_COLLECTION, record, post_id)
            except DuplicateRecordError:
                existing = self.db.read(POST_RAW_COLLECTION, post_id)

        if existing is not None and existing.get("html") and not html:
            merged = existing.copy()
            if url and not merged.get("url"):
                merged["url"] = url
            if task_id is not None:
                merged["task_id"] = task_id
            if parsed:
                merged.update({key: value for key, value in parsed.items() if value is not None})
            return self.db.replace(POST_RAW_COLLECTION, post_id, merged)

        merged = existing.copy() if existing is not None else {}
        merged.update(record)
        if existing is not None and existing.get("url") and not url:
            merged["url"] = existing["url"]
        return self.db.replace(POST_RAW_COLLECTION, post_id, merged)

    def save_search_note_metadata(
        self,
        item: dict[str, Any],
        *,
        keyword: str,
        request_url: str | None = None,
        task_id: str | None = None,
    ) -> Record | None:
        record = extract_search_note_metadata(
            item,
            keyword=keyword,
            request_url=request_url,
            task_id=task_id,
        )
        if record is None:
            return None

        author_id = str(record.get("author_id") or "unknown")
        author_name = record.get("author_name")
        self.ensure_author(author_id, str(author_name) if author_name else None)

        post_id = str(record["id"])
        existing = self.db.read(POST_METADATA_COLLECTION, post_id)
        if existing is None:
            try:
                return self.db.create(POST_METADATA_COLLECTION, record, post_id)
            except DuplicateRecordError:
                existing = self.db.read(POST_METADATA_COLLECTION, post_id)

        merged = existing.copy() if existing is not None else {}
        merged.update(record)
        return self.db.replace(POST_METADATA_COLLECTION, post_id, merged)

    def save_search_note_metadata_response(
        self,
        payload: dict[str, Any],
        *,
        keyword: str,
        request_url: str | None = None,
        task_id: str | None = None,
    ) -> int:
        return len(
            self.save_search_note_metadata_records_from_response(
                payload,
                keyword=keyword,
                request_url=request_url,
                task_id=task_id,
            )
        )

    def save_search_note_metadata_records_from_response(
        self,
        payload: dict[str, Any],
        *,
        keyword: str,
        request_url: str | None = None,
        task_id: str | None = None,
    ) -> list[Record]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        saved_records: list[Record] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            record = self.save_search_note_metadata(
                item,
                keyword=keyword,
                request_url=request_url,
                task_id=task_id,
            )
            if record:
                saved_records.append(record)
        return saved_records

    def is_post_already_scraped(self, post_id: str) -> bool:
        record = self.db.read(POST_RAW_COLLECTION, post_id)
        return bool(record and record.get("html"))

    def is_post_detail_parsed(self, post_id: str) -> bool:
        record = self.db.read(POST_RAW_COLLECTION, post_id)
        return bool(record and (record.get("note_detail") or record.get("noteId")))

    def list_posts(self) -> list[Record]:
        return self.db.list(POST_RAW_COLLECTION)

    def list_pending_posts(
        self,
        author_id: str,
        *,
        restrict_to_post_ids: set[str] | None = None,
    ) -> list[Record]:
        posts = [
            post
            for post in self.list_posts()
            if (
                post.get("author_id") == author_id
                and post.get("url")
                and (not post.get("html") or not (post.get("note_detail") or post.get("noteId")))
            )
        ]
        if restrict_to_post_ids:
            posts = [post for post in posts if str(post.get("uid") or post.get("id")) in restrict_to_post_ids]
        return posts

    def list_authors(self) -> list[Record]:
        return self.db.list(AUTHOR_COLLECTION)

    def list_search_note_metadata(self) -> list[Record]:
        return self.db.list(POST_METADATA_COLLECTION)

    def media_file_record_id(self, post_id: str, media_url: str) -> str:
        digest = sha256(f"{post_id}\0{media_url}".encode("utf-8")).hexdigest()
        return f"{post_id}-{digest[:24]}"

    def get_post_media_file(self, post_id: str, media_url: str) -> Record | None:
        return self.db.read(POST_MEDIA_FILE_COLLECTION, self.media_file_record_id(post_id, media_url))

    def save_post_media_file(
        self,
        *,
        post_id: str,
        media_url: str,
        media_type: str,
        local_path: str,
        task_id: str | None = None,
    ) -> Record:
        record_id = self.media_file_record_id(post_id, media_url)
        record: Record = {
            "id": record_id,
            "uid": record_id,
            "post_id": post_id,
            "media_url": media_url,
            "media_type": media_type,
            "local_path": local_path,
            "task_id": task_id,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        existing = self.db.read(POST_MEDIA_FILE_COLLECTION, record_id)
        if existing is None:
            try:
                return self.db.create(POST_MEDIA_FILE_COLLECTION, record, record_id)
            except DuplicateRecordError:
                existing = self.db.read(POST_MEDIA_FILE_COLLECTION, record_id)
        merged = existing.copy() if existing is not None else {}
        merged.update(record)
        return self.db.replace(POST_MEDIA_FILE_COLLECTION, record_id, merged)

    def save_comments_from_response(
        self,
        payload: dict[str, Any],
        *,
        post_id: str,
        parent_comment_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if isinstance(data.get("comments"), list):
            comments = data["comments"]
        elif isinstance(data.get("sub_comments"), list):
            comments = data["sub_comments"]
        else:
            comments = []
        saved_count = 0
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            saved_count += self.save_comment_tree(
                comment,
                parent_post_id=post_id,
                parent_comment_id=parent_comment_id,
                task_id=task_id,
            )
        return saved_count

    def save_comment_tree(
        self,
        comment: dict[str, Any],
        *,
        parent_post_id: str,
        parent_comment_id: str | None,
        task_id: str | None = None,
    ) -> int:
        record = extract_comment_record(
            comment,
            parent_post_id=parent_post_id,
            parent_comment_id=parent_comment_id,
            task_id=task_id,
        )
        saved_count = 0
        current_comment_id = parent_comment_id
        if record is not None:
            user_info = comment.get("user_info") if isinstance(comment.get("user_info"), dict) else {}
            self._save_comment_author(user_info)
            self._upsert_comment(record)
            saved_count += 1
            current_comment_id = str(record["comment_id"])

        sub_comments = comment.get("sub_comments") if isinstance(comment.get("sub_comments"), list) else []
        for sub_comment in sub_comments:
            if isinstance(sub_comment, dict):
                saved_count += self.save_comment_tree(
                    sub_comment,
                    parent_post_id=parent_post_id,
                    parent_comment_id=current_comment_id,
                    task_id=task_id,
                )
        return saved_count

    def _save_comment_author(self, user_info: dict[str, Any]) -> None:
        user_id = value_to_str(user_info.get("user_id")) or value_to_str(user_info.get("userId"))
        if not user_id:
            return
        name = value_to_str(user_info.get("nickname")) or value_to_str(user_info.get("nick_name"))
        self.ensure_author(
            user_id,
            name,
            extra={
                "avatar": value_to_str(user_info.get("image")) or value_to_str(user_info.get("avatar")),
                "xsec_token": value_to_str(user_info.get("xsec_token"))
                or value_to_str(user_info.get("xsecToken")),
                "ai_agent": user_info.get("ai_agent"),
                "raw_user_info": user_info,
            },
        )

    def _upsert_comment(self, record: Record) -> Record:
        comment_id = str(record["id"])
        existing = self.db.read(COMMENT_COLLECTION, comment_id)
        if existing is None:
            try:
                return self.db.create(COMMENT_COLLECTION, record, comment_id)
            except DuplicateRecordError:
                existing = self.db.read(COMMENT_COLLECTION, comment_id)
        merged = existing.copy() if existing is not None else {}
        merged.update(record)
        return self.db.replace(COMMENT_COLLECTION, comment_id, merged)


def extract_search_note_metadata(
    item: dict[str, Any],
    *,
    keyword: str,
    request_url: str | None = None,
    task_id: str | None = None,
) -> Record | None:
    note_id = value_to_str(item.get("id"))
    if note_id is None:
        return None

    note_card = item.get("note_card") if isinstance(item.get("note_card"), dict) else {}
    if item.get("model_type") != "note" or not note_card:
        return None

    user = note_card.get("user") if isinstance(note_card.get("user"), dict) else {}
    interact_info = note_card.get("interact_info") if isinstance(note_card.get("interact_info"), dict) else {}
    cover = note_card.get("cover") if isinstance(note_card.get("cover"), dict) else {}
    image_list = note_card.get("image_list") if isinstance(note_card.get("image_list"), list) else []
    corner_tag_info = note_card.get("corner_tag_info") if isinstance(note_card.get("corner_tag_info"), list) else []
    xsec_token = value_to_str(item.get("xsec_token"))
    now = datetime.now(UTC).isoformat()

    record: Record = {
        "id": note_id,
        "uid": note_id,
        "post_id": note_id,
        "url": search_note_url(note_id, xsec_token),
        "source": "rednote_search_api",
        "search_keyword": keyword,
        "request_url": request_url,
        "task_id": task_id,
        "updated_at": now,
        "model_type": value_to_str(item.get("model_type")),
        "note_type": value_to_str(note_card.get("type")),
        "title": value_to_str(note_card.get("display_title")),
        "xsec_token": xsec_token,
        "author_id": value_to_str(user.get("user_id")) or "unknown",
        "author_name": value_to_str(user.get("nickname")) or value_to_str(user.get("nick_name")),
        "author_avatar": value_to_str(user.get("avatar")),
        "author_xsec_token": value_to_str(user.get("xsec_token")),
        "liked": value_to_bool(interact_info.get("liked")),
        "liked_count": value_to_int(interact_info.get("liked_count")),
        "collected": value_to_bool(interact_info.get("collected")),
        "collected_count": value_to_int(interact_info.get("collected_count")),
        "comment_count": value_to_int(interact_info.get("comment_count")),
        "shared_count": value_to_int(interact_info.get("shared_count")),
        "cover_url": value_to_str(cover.get("url_default")),
        "cover_pre_url": value_to_str(cover.get("url_pre")),
        "cover_height": value_to_int(cover.get("height")),
        "cover_width": value_to_int(cover.get("width")),
        "image_count": len(image_list),
        "publish_time_text": first_corner_tag_text(corner_tag_info, "publish_time"),
        "corner_tags": corner_tag_info,
        "image_list": image_list,
        "raw": item,
    }
    return {key: value for key, value in record.items() if value is not None}


def extract_comment_record(
    comment: dict[str, Any],
    *,
    parent_post_id: str,
    parent_comment_id: str | None,
    task_id: str | None = None,
) -> Record | None:
    comment_id = (
        value_to_str(comment.get("id"))
        or value_to_str(comment.get("comment_id"))
        or value_to_str(comment.get("commentId"))
    )
    note_id = (
        value_to_str(comment.get("note_id"))
        or value_to_str(comment.get("noteId"))
        or parent_post_id
    )
    user_info = comment.get("user_info") if isinstance(comment.get("user_info"), dict) else {}
    user_id = value_to_str(user_info.get("user_id")) or value_to_str(user_info.get("userId"))
    content = value_to_str(comment.get("content"))
    create_time = first_not_none(
        value_to_int(comment.get("create_time")),
        value_to_int(comment.get("createTime")),
    )

    if not comment_id:
        digest_source = "\0".join(
            str(value or "")
            for value in (note_id, parent_comment_id, user_id, content, create_time)
        )
        comment_id = f"{note_id}-{sha256(digest_source.encode('utf-8')).hexdigest()[:24]}"

    user_name = value_to_str(user_info.get("nickname")) or value_to_str(user_info.get("nick_name"))
    user_avatar = value_to_str(user_info.get("image")) or value_to_str(user_info.get("avatar"))
    user_xsec_token = value_to_str(user_info.get("xsec_token")) or value_to_str(
        user_info.get("xsecToken")
    )
    now = datetime.now(UTC).isoformat()

    record: Record = {
        "id": comment_id,
        "uid": comment_id,
        "comment_id": comment_id,
        "post_id": note_id,
        "note_id": note_id,
        "parent_post_id": note_id,
        "parent_comment_id": parent_comment_id,
        "author_id": user_id,
        "user_id": user_id,
        "user_name": user_name,
        "user_avatar": user_avatar,
        "user_xsec_token": user_xsec_token,
        "content": content,
        "liked": value_to_bool(comment.get("liked")),
        "like_count": first_not_none(
            value_to_int(comment.get("like_count")),
            value_to_int(comment.get("likeCount")),
        ),
        "sub_comment_count": first_not_none(
            value_to_int(comment.get("sub_comment_count")),
            value_to_int(comment.get("subCommentCount")),
        ),
        "sub_comment_has_more": value_to_bool(comment.get("sub_comment_has_more")),
        "sub_comment_cursor": value_to_str(comment.get("sub_comment_cursor")),
        "create_time": create_time,
        "status": value_to_str(comment.get("status")),
        "ip_location": value_to_str(comment.get("ip_location"))
        or value_to_str(comment.get("ipLocation")),
        "at_users": comment.get("at_users") if isinstance(comment.get("at_users"), list) else None,
        "pictures": comment.get("pictures") if isinstance(comment.get("pictures"), list) else None,
        "show_tags": comment.get("show_tags") if isinstance(comment.get("show_tags"), list) else None,
        "target_comment": comment.get("target_comment")
        if isinstance(comment.get("target_comment"), dict)
        else None,
        "raw": comment,
        "task_id": task_id,
        "updated_at": now,
    }
    return {key: value for key, value in record.items() if value is not None}


def extract_initial_state_from_html(html: str) -> dict[str, Any] | None:
    match = INITIAL_STATE_RE.search(html)
    if not match:
        return None
    state_text = UNDEFINED_VALUE_RE.sub(" null", match.group(1))
    try:
        state = json.loads(state_text)
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) else None


def extract_post_detail_from_initial_state(
    initial_state: dict[str, Any] | None,
    *,
    fallback_post_id: str | None = None,
    image_urls: list[str] | None = None,
) -> Record:
    if not isinstance(initial_state, dict):
        return {}

    note_store = initial_state.get("note") if isinstance(initial_state.get("note"), dict) else {}
    note_detail_map = (
        note_store.get("noteDetailMap")
        if isinstance(note_store.get("noteDetailMap"), dict)
        else {}
    )
    current_note_id = (
        value_to_str(note_store.get("currentNoteId"))
        or value_to_str(note_store.get("firstNoteId"))
        or fallback_post_id
    )
    detail_entry = None
    if current_note_id and isinstance(note_detail_map.get(current_note_id), dict):
        detail_entry = note_detail_map[current_note_id]
    elif note_detail_map:
        first_entry = next(iter(note_detail_map.values()))
        detail_entry = first_entry if isinstance(first_entry, dict) else None
    if not isinstance(detail_entry, dict):
        return {"initial_state": initial_state, "note_state": note_store}

    note = detail_entry.get("note") if isinstance(detail_entry.get("note"), dict) else {}
    comments = detail_entry.get("comments") if isinstance(detail_entry.get("comments"), dict) else None
    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    interact_info = first_dict(note, "interactInfo", "interactionInfo", "iteractionInfo")

    record: Record = {
        "initial_state": initial_state,
        "note_state": note_store,
        "note_detail": note,
        "comments": comments,
        "title": value_to_str(note.get("title")),
        "content": value_to_str(note.get("desc")),
        "desc": value_to_str(note.get("desc")),
        "time": value_to_int(note.get("time")),
        "lastUpdateTime": value_to_int(note.get("lastUpdateTime")),
        "noteId": value_to_str(note.get("noteId")) or current_note_id,
        "ipLocation": value_to_str(note.get("ipLocation")),
        "note_type": value_to_str(note.get("type")),
        "xsecToken": value_to_str(note.get("xsecToken")),
        "author_id": value_to_str(user.get("userId")),
        "author_name": value_to_str(user.get("nickname")),
        "author_avatar": value_to_str(user.get("avatar")),
        "author_xsec_token": value_to_str(user.get("xsecToken")),
        "images": image_urls or None,
        "imageList": note.get("imageList") if isinstance(note.get("imageList"), list) else None,
        "tags": note.get("tagList") if isinstance(note.get("tagList"), list) else None,
        "tagList": note.get("tagList") if isinstance(note.get("tagList"), list) else None,
        "atUserList": note.get("atUserList") if isinstance(note.get("atUserList"), list) else None,
        "shareInfo": note.get("shareInfo") if isinstance(note.get("shareInfo"), dict) else None,
        "illegalInfo": note.get("illegalInfo") if isinstance(note.get("illegalInfo"), dict) else None,
        "interactInfo": interact_info,
    }
    for key, value in interact_info.items():
        record[key] = value_to_int(value) if key.endswith("Count") else value
    return {key: value for key, value in record.items() if value is not None}


def first_dict(source: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            return value
    return {}


def first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


class OpenGraphImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {key.lower(): value for key, value in attrs if key and value is not None}
        if values.get("name") == "og:image" and values.get("content"):
            self.images.append(str(values["content"]))


def extract_og_image_urls_from_html(html: str) -> list[str]:
    parser = OpenGraphImageParser()
    parser.feed(html)
    seen: set[str] = set()
    urls: list[str] = []
    for image_url in parser.images:
        if image_url not in seen:
            seen.add(image_url)
            urls.append(image_url)
    return urls


def search_note_url(note_id: str, xsec_token: str | None) -> str:
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if not xsec_token:
        return url
    return f"{url}?xsec_token={quote(xsec_token, safe='')}&xsec_source=pc_search"


def first_corner_tag_text(tags: list[Any], tag_type: str) -> str | None:
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        if tag.get("type") == tag_type:
            return value_to_str(tag.get("text"))
    return None


def value_to_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, int | float | bool):
        return str(value)
    return None


def value_to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
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


def value_to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None
