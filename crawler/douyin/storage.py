from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from storage import DuckDBDatabase, DuplicateRecordError, Record

from .utils import csv_from_array_key, value_to_bool, value_to_int, value_to_str


AUTHOR_COLLECTION = "douyin_authors"
VIDEO_RAW_COLLECTION = "douyin_videos_raw"
COMMENT_RAW_COLLECTION = "douyin_comments_raw"

COMMENTS_FAILED_PANEL = "COMMENTS_FAILED_PANEL"
COMMENTS_PARTIAL = "COMMENTS_PARTIAL"


class DouyinVideoStatus(StrEnum):
    ID_ONLY = "ID_ONLY"
    RETRIEVED = "RETRIEVED"
    COMMENTS_DONE = "COMMENTS_DONE"
    ERROR = "ERROR"
    COMMENTS_FAILED_PANEL = COMMENTS_FAILED_PANEL
    COMMENTS_PARTIAL = COMMENTS_PARTIAL


class DouyinStore:
    """Douyin storage adapter backed by DuckDB."""

    def __init__(self, db: DuckDBDatabase) -> None:
        self.db = db

    def ensure_author(self, sec_user_id: str) -> Record:
        existing = self.db.read(AUTHOR_COLLECTION, sec_user_id)
        if existing is not None:
            return existing
        return self.db.create(
            AUTHOR_COLLECTION,
            {
                "id": sec_user_id,
                "sec_user_id": sec_user_id,
                "updated_at": now_iso(),
            },
            sec_user_id,
        )

    def save_author_profile(self, sec_user_id: str, profile: dict[str, Any]) -> Record:
        projected = project_author(profile)
        record: Record = {
            "id": sec_user_id,
            "sec_user_id": sec_user_id,
            "profile_json": profile,
            "updated_at": now_iso(),
        }
        record.update(projected)

        existing = self.db.read(AUTHOR_COLLECTION, sec_user_id)
        if existing is None:
            return self.db.create(AUTHOR_COLLECTION, record, sec_user_id)
        merged = existing.copy()
        merged.update(record)
        return self.db.replace(AUTHOR_COLLECTION, sec_user_id, merged)

    def save_video_raw(
        self,
        aweme_id: str,
        sec_user_id: str,
        video_json: dict[str, Any] | None,
        *,
        task_id: str | None = None,
    ) -> Record:
        self.ensure_author(sec_user_id)
        existing = self.db.read(VIDEO_RAW_COLLECTION, aweme_id)
        status = DouyinVideoStatus.RETRIEVED.value if video_json is not None else DouyinVideoStatus.ID_ONLY.value
        record: Record = {
            "id": aweme_id,
            "aweme_id": aweme_id,
            "sec_user_id": sec_user_id,
            "video_json": video_json,
            "updated_at": now_iso(),
            "status": status,
            "comment_cursor": 0,
            "task_id": task_id,
        }
        if video_json is not None:
            record.update(project_video(video_json))

        if existing is None:
            try:
                return self.db.create(VIDEO_RAW_COLLECTION, record, aweme_id)
            except DuplicateRecordError:
                existing = self.db.read(VIDEO_RAW_COLLECTION, aweme_id)

        merged = existing.copy() if existing is not None else {}
        if merged.get("status") == DouyinVideoStatus.COMMENTS_DONE.value and video_json is not None:
            status = DouyinVideoStatus.COMMENTS_DONE.value
        merged.update(record)
        merged["status"] = status
        if existing is not None and existing.get("comment_cursor") and not record.get("comment_cursor"):
            merged["comment_cursor"] = existing["comment_cursor"]
        return self.db.replace(VIDEO_RAW_COLLECTION, aweme_id, merged)

    def save_comment_raw(
        self,
        comment_id: str,
        aweme_id: str,
        parent_comment_id: str | None,
        data: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> Record:
        projected = project_comment(data)
        record: Record = {
            "id": comment_id,
            "comment_id": comment_id,
            "aweme_id": aweme_id,
            "parent_comment_id": parent_comment_id,
            "data": data,
            "updated_at": now_iso(),
        }
        if task_id is not None:
            record["task_id"] = task_id
        record.update(projected)

        existing = self.db.read(COMMENT_RAW_COLLECTION, comment_id)
        if existing is None:
            return self.db.create(COMMENT_RAW_COLLECTION, record, comment_id)
        merged = existing.copy()
        merged.update(record)
        return self.db.replace(COMMENT_RAW_COLLECTION, comment_id, merged)

    def save_comment(
        self,
        aweme_id: str,
        comment: dict[str, Any],
        parent_comment_id: str | None = None,
        *,
        task_id: str | None = None,
    ) -> bool:
        comment_id = value_to_str(comment.get("cid"))
        if not comment_id:
            return False
        parent_id = parent_comment_id or value_to_str(comment.get("reply_id"))
        if parent_id == "0":
            parent_id = None
        self.save_comment_raw(
            comment_id,
            aweme_id,
            parent_id,
            extract_comment_data(comment),
            task_id=task_id,
        )
        return True

    def mark_video_status(self, aweme_id: str, status: str) -> Record | None:
        existing = self.db.read(VIDEO_RAW_COLLECTION, aweme_id)
        if existing is None:
            return None
        updated = existing.copy()
        updated["status"] = status
        updated["updated_at"] = now_iso()
        return self.db.replace(VIDEO_RAW_COLLECTION, aweme_id, updated)

    def mark_video_comments_done(self, aweme_id: str) -> Record | None:
        return self.mark_video_status(aweme_id, DouyinVideoStatus.COMMENTS_DONE.value)

    def mark_video_error(self, aweme_id: str) -> Record | None:
        return self.mark_video_status(aweme_id, DouyinVideoStatus.ERROR.value)

    def mark_video_comments_failed_panel(self, aweme_id: str) -> Record | None:
        return self.mark_video_status(aweme_id, COMMENTS_FAILED_PANEL)

    def mark_video_comments_partial(self, aweme_id: str) -> Record | None:
        return self.mark_video_status(aweme_id, COMMENTS_PARTIAL)

    def is_video_comments_done(self, aweme_id: str) -> bool:
        record = self.db.read(VIDEO_RAW_COLLECTION, aweme_id)
        return bool(record and record.get("status") == DouyinVideoStatus.COMMENTS_DONE.value)

    def count_saved_comments(self, aweme_id: str) -> int:
        return sum(1 for item in self.db.list(COMMENT_RAW_COLLECTION) if item.get("aweme_id") == aweme_id)

    def get_unfinished_video_ids(self, sec_user_id: str) -> list[str]:
        unfinished_statuses = {
            DouyinVideoStatus.ID_ONLY.value,
            DouyinVideoStatus.RETRIEVED.value,
            COMMENTS_PARTIAL,
            COMMENTS_FAILED_PANEL,
        }
        videos = self.db.list(VIDEO_RAW_COLLECTION)
        return [
            str(video["aweme_id"])
            for video in videos
            if video.get("sec_user_id") == sec_user_id and video.get("status") in unfinished_statuses
        ]

    def count_video_statuses(self, sec_user_id: str) -> tuple[int, int, int]:
        videos = [
            video
            for video in self.db.list(VIDEO_RAW_COLLECTION)
            if video.get("sec_user_id") == sec_user_id
        ]
        completed = sum(1 for video in videos if video.get("status") == DouyinVideoStatus.COMMENTS_DONE.value)
        partial = sum(1 for video in videos if video.get("status") in {COMMENTS_PARTIAL, COMMENTS_FAILED_PANEL})
        failed = sum(1 for video in videos if video.get("status") == DouyinVideoStatus.ERROR.value)
        return completed, partial, failed

    def update_video_comment_cursor(self, aweme_id: str, cursor: int) -> Record | None:
        existing = self.db.read(VIDEO_RAW_COLLECTION, aweme_id)
        if existing is None:
            return None
        updated = existing.copy()
        updated["comment_cursor"] = cursor
        updated["updated_at"] = now_iso()
        return self.db.replace(VIDEO_RAW_COLLECTION, aweme_id, updated)

    def get_video_comment_cursor(self, aweme_id: str) -> int:
        existing = self.db.read(VIDEO_RAW_COLLECTION, aweme_id)
        return value_to_int(existing.get("comment_cursor")) if existing else 0

    def list_authors(self) -> list[Record]:
        return self.db.list(AUTHOR_COLLECTION)

    def list_videos(self) -> list[Record]:
        return self.db.list(VIDEO_RAW_COLLECTION)

    def list_comments(self) -> list[Record]:
        return self.db.list(COMMENT_RAW_COLLECTION)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def project_author(profile: dict[str, Any]) -> Record:
    user = profile.get("user") if isinstance(profile.get("user"), dict) else profile
    return {
        "uid": value_to_str(user.get("uid")),
        "short_id": value_to_str(user.get("short_id")),
        "unique_id": value_to_str(user.get("unique_id")),
        "nickname": value_to_str(user.get("nickname")),
        "signature": value_to_str(user.get("signature")),
        "gender": value_to_int(user.get("gender")),
        "ip_location": value_to_str(user.get("ip_location")),
        "verification_type": value_to_int(user.get("verification_type")),
        "custom_verify": value_to_str(user.get("custom_verify")),
        "enterprise_verify_reason": value_to_str(user.get("enterprise_verify_reason")),
        "is_star": value_to_bool(user.get("is_star")),
        "aweme_count": value_to_int(user.get("aweme_count")),
        "favoriting_count": value_to_int(user.get("favoriting_count")),
        "follower_count": value_to_int(user.get("follower_count")),
        "following_count": value_to_int(user.get("following_count")),
        "total_favorited": value_to_int(user.get("total_favorited")),
        "mplatform_followers_count": value_to_int(user.get("mplatform_followers_count")),
        "max_follower_count": value_to_int(user.get("max_follower_count")),
    }


def project_video(video: dict[str, Any]) -> Record:
    author = video.get("author") if isinstance(video.get("author"), dict) else {}
    statistics = video.get("statistics") if isinstance(video.get("statistics"), dict) else {}
    music = video.get("music") if isinstance(video.get("music"), dict) else {}
    aweme_control = video.get("aweme_control") if isinstance(video.get("aweme_control"), dict) else {}
    video_control = video.get("video_control") if isinstance(video.get("video_control"), dict) else {}
    image_list = video.get("image_list") if isinstance(video.get("image_list"), list) else []

    return {
        "author_uid": value_to_str(author.get("uid")),
        "author_sec_uid": value_to_str(author.get("sec_uid")),
        "author_nickname": value_to_str(author.get("nickname")),
        "desc": value_to_str(video.get("desc")),
        "create_time": value_to_int(video.get("create_time")),
        "aweme_type": value_to_int(video.get("aweme_type")),
        "media_type": value_to_int(video.get("media_type")),
        "duration_ms": value_to_int(video.get("duration")),
        "region": value_to_str(video.get("region")),
        "is_top": value_to_int(video.get("is_top")),
        "is_ads": value_to_bool(video.get("is_ads")),
        "is_image_album": bool(image_list),
        "image_count": len(image_list),
        "digg_count": value_to_int(statistics.get("digg_count")),
        "comment_count": value_to_int(statistics.get("comment_count")),
        "share_count": value_to_int(statistics.get("share_count")),
        "collect_count": value_to_int(statistics.get("collect_count")),
        "play_count": value_to_int(statistics.get("play_count")),
        "recommend_count": value_to_int(statistics.get("recommend_count")),
        "admire_count": value_to_int(statistics.get("admire_count")),
        "music_id": value_to_str(music.get("id_str")) or value_to_str(music.get("id")),
        "music_title": value_to_str(music.get("title")),
        "music_author": value_to_str(music.get("author")),
        "hashtag_names_csv": csv_from_array_key(video, "text_extra", "hashtag_name"),
        "can_comment": value_to_bool(aweme_control.get("can_comment")),
        "allow_share": value_to_bool(aweme_control.get("can_share")),
        "allow_download": value_to_bool(video_control.get("allow_download")),
    }


def extract_comment_data(raw: dict[str, Any]) -> Record:
    data: Record = {}
    for key in [
        "text",
        "create_time",
        "digg_count",
        "reply_comment_total",
        "ip_label",
        "level",
        "is_hot",
        "content_type",
        "is_folded",
        "reply_id",
    ]:
        if key in raw:
            data[key] = raw[key]

    user = raw.get("user")
    if isinstance(user, dict):
        data["user"] = {
            key: user[key]
            for key in ["uid", "nickname", "sec_uid", "region"]
            if key in user
        }

    image_list = raw.get("image_list")
    if isinstance(image_list, list):
        normalized_images = []
        for image in image_list:
            if not isinstance(image, dict):
                continue
            origin_url = image.get("origin_url")
            origin_uri = origin_url.get("uri") if isinstance(origin_url, dict) else None
            normalized_images.append({"uri": image.get("uri") or origin_uri})
        data["image_list"] = normalized_images

    return data


def project_comment(data: dict[str, Any]) -> Record:
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return {
        "text": value_to_str(data.get("text")),
        "content_type": value_to_int(data.get("content_type")),
        "create_time": value_to_int(data.get("create_time")),
        "digg_count": value_to_int(data.get("digg_count")),
        "reply_comment_total": value_to_int(data.get("reply_comment_total")),
        "ip_label": value_to_str(data.get("ip_label")),
        "level": value_to_int(data.get("level")),
        "is_hot": value_to_bool(data.get("is_hot")),
        "is_folded": value_to_bool(data.get("is_folded")),
        "user_uid": value_to_str(user.get("uid")),
        "user_sec_uid": value_to_str(user.get("sec_uid")),
        "user_nickname": value_to_str(user.get("nickname")),
        "user_region": value_to_str(user.get("region")),
        "image_uris_csv": csv_from_array_key(data, "image_list", "uri"),
        "reply_id": value_to_str(data.get("reply_id")),
    }
