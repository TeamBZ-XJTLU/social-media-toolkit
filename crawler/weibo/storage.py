from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from storage import DuplicateRecordError, JsonFileDatabase, Record

from .utils import value_to_bool, value_to_int, value_to_str


AUTHOR_COLLECTION = "weibo_authors"
POST_RAW_COLLECTION = "weibo_posts_raw"
COMMENT_COLLECTION = "weibo_comments"


class WeiboPostStatus(StrEnum):
    ID_ONLY = "ID_ONLY"
    RETRIEVED = "RETRIEVED"


class WeiboStore:
    """Weibo storage adapter backed by JsonFileDatabase."""

    def __init__(self, db: JsonFileDatabase) -> None:
        self.db = db

    def ensure_author(self, author_id: str, name: str | None = None) -> Record:
        existing = self.db.read(AUTHOR_COLLECTION, author_id)
        if existing is not None:
            if name is not None and existing.get("name") != name:
                return self.db.update(AUTHOR_COLLECTION, author_id, {"name": name})
            return existing

        return self.db.create(
            AUTHOR_COLLECTION,
            {
                "id": author_id,
                "uid": author_id,
                "name": name,
            },
            author_id,
        )

    def save_author_profile(self, requested_author_id: str, user: dict[str, Any]) -> Record:
        profile = extract_author_profile(requested_author_id, user)
        existing = self.db.read(AUTHOR_COLLECTION, str(profile["uid"]))
        if existing is None:
            return self.db.create(AUTHOR_COLLECTION, profile, str(profile["uid"]))
        merged = existing.copy()
        merged.update(profile)
        return self.db.replace(AUTHOR_COLLECTION, str(profile["uid"]), merged)

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
    ) -> Record:
        self.ensure_author(author_id)

        retrieved_at = datetime.now(UTC).isoformat()
        status = WeiboPostStatus.RETRIEVED if url and html else WeiboPostStatus.ID_ONLY
        record: Record = {
            "id": post_id,
            "uid": post_id,
            "url": url,
            "html": html,
            "content_html": html,
            "updated_at": retrieved_at,
            "status": status.value,
            "author_id": author_id,
            "handler_id": handler_id,
            "task_id": task_id,
        }
        if extra:
            record["extra"] = extra

        existing = self.db.read(POST_RAW_COLLECTION, post_id)
        if existing is None:
            try:
                return self.db.create(POST_RAW_COLLECTION, record, post_id)
            except DuplicateRecordError:
                existing = self.db.read(POST_RAW_COLLECTION, post_id)

        if existing is not None and existing.get("content_html") and not html:
            return existing

        merged = existing.copy() if existing is not None else {}
        merged.update(record)
        return self.db.replace(POST_RAW_COLLECTION, post_id, merged)

    def save_post_meta(
        self,
        item: dict[str, Any],
        author_id: str,
        *,
        task_id: str | None = None,
    ) -> Record | None:
        meta = extract_post_meta(item, author_id)
        if meta is None:
            return None

        post_id = str(meta["uid"])
        self.ensure_author(str(meta["author_id"]))
        existing = self.db.read(POST_RAW_COLLECTION, post_id)
        now = datetime.now(UTC).isoformat()
        if existing is None:
            record: Record = {
                "id": post_id,
                "uid": post_id,
                "status": WeiboPostStatus.ID_ONLY.value,
                "updated_at": now,
                "task_id": task_id,
                "handler_id": None,
                "html": None,
                "content_html": None,
                "raw": item,
            }
            record.update(meta)
            return self.db.create(POST_RAW_COLLECTION, record, post_id)

        merged = existing.copy()
        content_html = merged.get("content_html")
        html = merged.get("html")
        merged.update(meta)
        merged["raw"] = item
        merged["updated_at"] = now
        if task_id is not None:
            merged["task_id"] = task_id
        if content_html:
            merged["content_html"] = content_html
            merged["html"] = html or content_html
            merged["status"] = WeiboPostStatus.RETRIEVED.value
        else:
            merged.setdefault("content_html", None)
            merged.setdefault("html", None)
            merged.setdefault("status", WeiboPostStatus.ID_ONLY.value)
        return self.db.replace(POST_RAW_COLLECTION, post_id, merged)

    def save_post_list_with_meta(
        self,
        items: list[dict[str, Any]],
        author_id: str,
        *,
        task_id: str | None = None,
    ) -> list[Record]:
        saved: list[Record] = []
        for item in items:
            record = self.save_post_meta(item, author_id, task_id=task_id)
            if record is not None:
                saved.append(record)
        return saved

    def save_search_post(
        self,
        post: dict[str, Any],
        *,
        keyword: str,
        page_number: int,
        task_id: str | None = None,
        id_only: bool = False,
    ) -> Record | None:
        post_id = value_to_str(post.get("mid")) or value_to_str(post.get("id")) or value_to_str(post.get("uid"))
        if post_id is None:
            return None

        author_id = value_to_str(post.get("author_id")) or "unknown"
        author_name = value_to_str(post.get("author_name"))
        self.ensure_author(author_id, author_name)

        now = datetime.now(UTC).isoformat()
        url = value_to_str(post.get("url"))
        content_html = None if id_only else value_to_str(post.get("content_html"))
        record: Record = {
            "id": post_id,
            "uid": post_id,
            "url": url,
            "html": content_html,
            "content_html": content_html,
            "updated_at": now,
            "status": WeiboPostStatus.ID_ONLY.value if id_only else WeiboPostStatus.RETRIEVED.value,
            "author_id": author_id,
            "author_name": author_name,
            "task_id": task_id,
            "handler_id": None,
            "source": "weibo_search",
            "search_keyword": keyword,
            "search_page": page_number,
            "raw": minimal_search_post(post) if id_only else post,
        }

        for key in (
            "published_at_text",
            "source_app",
            "content_text",
            "topics",
            "mentions",
            "links",
            "images",
            "videos",
            "stats",
            "is_retweet",
            "retweeted",
            "search_position",
            "search_total_pages",
            "search_url",
            "search_params",
        ):
            if key in post and not id_only:
                record[key] = post[key]

        existing = self.db.read(POST_RAW_COLLECTION, post_id)
        if existing is None:
            return self.db.create(POST_RAW_COLLECTION, record, post_id)

        merged = existing.copy()
        merged.update(record)
        if existing.get("content_html") and id_only:
            merged["content_html"] = existing.get("content_html")
            merged["html"] = existing.get("html")
            merged["status"] = existing.get("status") or WeiboPostStatus.RETRIEVED.value
        return self.db.replace(POST_RAW_COLLECTION, post_id, merged)

    def save_comments(
        self,
        comments: list[dict[str, Any]],
        *,
        post_id: str | None = None,
        task_id: str | None = None,
    ) -> list[Record]:
        saved: list[Record] = []
        for comment in comments:
            normalized = extract_comment(comment, post_id=post_id)
            if normalized is None:
                continue
            if task_id is not None:
                normalized["task_id"] = task_id

            comment_id = str(normalized["comment_id"])
            existing = self.db.read(COMMENT_COLLECTION, comment_id)
            if existing is None:
                saved.append(self.db.create(COMMENT_COLLECTION, normalized, comment_id))
            else:
                merged = existing.copy()
                merged.update(normalized)
                saved.append(self.db.replace(COMMENT_COLLECTION, comment_id, merged))

            user = comment.get("user")
            if isinstance(user, dict):
                user_id = value_to_str(user.get("idstr")) or value_to_str(user.get("id"))
                if user_id:
                    self.save_author_profile(user_id, user)

        return saved

    def is_post_already_scraped(self, post_id: str) -> bool:
        record = self.db.read(POST_RAW_COLLECTION, post_id)
        return bool(record and (record.get("content_html") or record.get("html")))

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
            if post.get("author_id") == author_id
            and post.get("url")
            and not (post.get("content_html") or post.get("html"))
        ]
        if restrict_to_post_ids:
            posts = [post for post in posts if str(post.get("uid") or post.get("id")) in restrict_to_post_ids]
        return posts

    def list_authors(self) -> list[Record]:
        return self.db.list(AUTHOR_COLLECTION)

    def list_comments(self) -> list[Record]:
        return self.db.list(COMMENT_COLLECTION)


def extract_author_profile(requested_author_id: str, user: dict[str, Any]) -> Record:
    status_total_counter = user.get("status_total_counter")
    if not isinstance(status_total_counter, dict):
        status_total_counter = {}

    uid = value_to_str(user.get("idstr")) or value_to_str(user.get("id")) or requested_author_id
    gender_value = user.get("gender")
    gender = None
    if isinstance(gender_value, str):
        gender = {"m": 1, "M": 1, "f": 2, "F": 2}.get(gender_value)
    if gender is None:
        gender = value_to_int(gender_value)

    return {
        "id": uid,
        "uid": uid,
        "name": value_to_str(user.get("screen_name")),
        "domain": value_to_str(user.get("domain")),
        "icons": user.get("icon_list") if isinstance(user.get("icon_list"), list) else [],
        "num_followers": value_to_int(user.get("followers_count")),
        "num_following": value_to_int(user.get("friends_count")),
        "gender": gender,
        "is_muted": value_to_bool(user.get("is_muteuser")),
        "is_star": value_to_bool(user.get("is_star")),
        "location": value_to_str(user.get("location")),
        "member_rank": value_to_int(user.get("mbrank")),
        "member_type": value_to_int(user.get("mbtype")),
        "num_received_comments": value_to_int(status_total_counter.get("comment_cnt")),
        "num_received_likes": value_to_int(status_total_counter.get("like_cnt")),
        "num_received_reposts": value_to_int(status_total_counter.get("repost_cnt")),
        "num_posts": value_to_int(user.get("statuses_count")),
        "is_svip": value_to_bool(user.get("svip")),
        "is_top_user": value_to_bool(user.get("top_user")),
        "user_type": value_to_int(user.get("user_type")),
        "is_v_plus": value_to_bool(user.get("v_plus")),
        "is_verified": value_to_bool(user.get("verified")),
        "verified_reason": value_to_str(user.get("verified_reason")),
        "verified_type": value_to_int(user.get("verified_type")),
        "verified_type_ext": value_to_int(user.get("verified_type_ext")),
        "is_vvip": value_to_bool(user.get("vvip")),
        "raw": user,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def minimal_search_post(post: dict[str, Any]) -> Record:
    return {
        key: post.get(key)
        for key in (
            "id",
            "uid",
            "mid",
            "url",
            "author_id",
            "author_name",
            "author_url",
            "published_at_text",
            "source_app",
            "search_keyword",
            "search_page",
            "search_position",
            "search_total_pages",
            "search_url",
            "search_params",
        )
        if post.get(key) is not None
    }


def extract_post_meta(item: dict[str, Any], author_id: str) -> Record | None:
    uid = value_to_str(item.get("id")) or value_to_str(item.get("mblogid")) or value_to_str(item.get("mid"))
    if uid is None:
        return None

    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    post_author_id = value_to_str(user.get("id")) or author_id
    retweeted = item.get("retweeted_status") if isinstance(item.get("retweeted_status"), dict) else {}
    retweeted_user = retweeted.get("user") if isinstance(retweeted.get("user"), dict) else {}
    topic_struct = item.get("topic_struct") if isinstance(item.get("topic_struct"), dict) else {}
    tag_struct = item.get("tag_struct") if isinstance(item.get("tag_struct"), list) else []
    first_tag = tag_struct[0] if tag_struct and isinstance(tag_struct[0], dict) else {}

    return {
        "uid": uid,
        "author_id": post_author_id,
        "url": f"https://weibo.com/{post_author_id}/{uid}",
        "rid": value_to_str(item.get("rid")),
        "cardid": value_to_str(item.get("cardid")),
        "pic_ids": item.get("pic_ids") if isinstance(item.get("pic_ids"), list) else None,
        "display_text": value_to_str(item.get("display_text")),
        "reposts_count": value_to_int(item.get("reposts_count")),
        "comments_count": value_to_int(item.get("comments_count")),
        "attitudes_count": value_to_int(item.get("attitudes_count")),
        "is_long_text": value_to_bool(item.get("isLongText", item.get("is_long_text"))),
        "comment_sort_type": value_to_int(item.get("comment_sort_type")),
        "repost_type": value_to_int(item.get("repost_type")),
        "share_repost_type": value_to_int(item.get("share_repost_type")),
        "topic_url": value_to_str(topic_struct.get("url")),
        "topic_name": value_to_str(topic_struct.get("topic_title")),
        "oid": value_to_str(item.get("oid")),
        "uuid": value_to_str(item.get("uuid")),
        "fid": value_to_str(item.get("fid")),
        "tag_icon_url": value_to_str(first_tag.get("icon_url")),
        "tag_desc": value_to_str(first_tag.get("tag_name")),
        "mblog_type": value_to_int(item.get("mblogtype", item.get("mblog_type"))),
        "text": value_to_str(item.get("text")),
        "retweeted_id": value_to_str(retweeted.get("id")),
        "retweeted_created_at": value_to_str(retweeted.get("created_at")),
        "retweeted_user_id": value_to_str(retweeted_user.get("id")),
        "retweeted_user_name": value_to_str(retweeted_user.get("screen_name")),
    }


def extract_comment(comment: dict[str, Any], *, post_id: str | None = None) -> Record | None:
    comment_id = value_to_int(comment.get("id"))
    if comment_id is None:
        return None

    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    reply_comment = comment.get("reply_comment") if isinstance(comment.get("reply_comment"), dict) else {}
    inferred_post_id = post_id
    analysis_extra = value_to_str(comment.get("analysis_extra"))
    if inferred_post_id is None and analysis_extra:
        for part in analysis_extra.split("|"):
            if part.startswith("mid:"):
                inferred_post_id = part[4:]
                break

    return {
        "id": str(comment_id),
        "comment_id": comment_id,
        "comment_id_str": value_to_str(comment.get("idstr")),
        "post_id": inferred_post_id,
        "created_at": value_to_str(comment.get("created_at")),
        "root_id": value_to_int(comment.get("rootid", comment.get("root_id"))),
        "root_id_str": value_to_str(comment.get("rootidstr")),
        "reply_comment_id": value_to_int(reply_comment.get("id")),
        "floor_number": value_to_int(comment.get("floor_number")),
        "comment_text": value_to_str(comment.get("text")),
        "comment_text_raw": value_to_str(comment.get("text_raw", comment.get("textRaw"))),
        "like_counts": value_to_int(comment.get("like_counts")),
        "liked": value_to_bool(comment.get("liked")),
        "is_liked_by_mblog_author": value_to_bool(comment.get("is_liked_by_mblog_author")),
        "total_number": value_to_int(comment.get("total_number")),
        "disable_reply": value_to_bool(comment.get("disable_reply")),
        "restrict_operate": value_to_int(comment.get("restrictOperate", comment.get("restrict_operate"))),
        "source": value_to_str(comment.get("source")),
        "source_type": value_to_int(comment.get("source_type")),
        "source_allowclick": value_to_int(comment.get("source_allowclick")),
        "rid": value_to_str(comment.get("rid")),
        "item_category": value_to_str(comment.get("item_category")),
        "degrade_type": value_to_str(comment.get("degrade_type")),
        "analysis_extra": analysis_extra,
        "cmt_ext": comment.get("cmt_ext") if isinstance(comment.get("cmt_ext"), dict) else None,
        "pic_num": value_to_int(comment.get("pic_num")),
        "user_id": value_to_int(user.get("id")),
        "user_screen_name": value_to_str(user.get("screen_name")),
        "allow_follow": value_to_bool(comment.get("allow_follow")),
        "is_expand": value_to_bool(comment.get("isExpand", comment.get("is_expand"))),
        "url_objects": comment.get("url_objects") if isinstance(comment.get("url_objects"), list) else [],
        "pic": comment.get("pic") if isinstance(comment.get("pic"), dict) else {},
        "raw": comment,
        "updated_at": datetime.now(UTC).isoformat(),
    }
