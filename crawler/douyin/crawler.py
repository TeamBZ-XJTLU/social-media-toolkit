from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from crawler.base import BrowserCrawler, BrowserCrawlerConfig
from storage import DuckDBDatabase
from .storage import DouyinStore
from .utils import (
    DOUYIN_BASE_URL,
    browser_fetch_json,
    get_page_debug_info,
    navigate,
    smart_sleep,
    user_profile_url,
    value_to_bool,
    value_to_int,
    value_to_str,
    video_url,
    wait_for_login,
)


@dataclass(slots=True)
class DouyinCrawlerConfig(BrowserCrawlerConfig):
    login_timeout_ms: int = 3000
    max_empty_pages: int = 5
    max_video_pages: int | None = None
    max_comment_pages: int | None = None
    max_reply_pages: int | None = None
    collect_comments: bool = True
    request_count: int = 20


class DouyinCrawler(BrowserCrawler[DouyinCrawlerConfig, DouyinStore]):
    """Douyin crawler using cloakbrowser and browser-side authenticated fetch calls."""

    db_cls = DuckDBDatabase
    store_cls = DouyinStore

    async def by_keyword(self, keyword: str, **kwargs: Any) -> None:
        raise NotImplementedError("Douyin keyword search is not implemented yet.")

    async def by_author(
        self,
        sec_user_id: str,
        *,
        id_only: bool = False,
        collect_comments: bool | None = None,
        restrict_to_aweme_ids: Iterable[str] | None = None,
        use_local_index: bool = False,
        task_id: str | None = None,
        page: Any | None = None,
    ) -> None:
        page = page or await self._new_page()
        collect_comments = self.config.collect_comments if collect_comments is None else collect_comments

        if not use_local_index:
            await self._prepare_profile_page(page, sec_user_id)
            profile = await self._fetch_author_profile(page, sec_user_id)
            if profile is not None:
                self.store.save_author_profile(sec_user_id, profile)
                logger.info("Saved Douyin author profile {}", sec_user_id)

            discovered_videos = await self._collect_video_list(page, sec_user_id, task_id=task_id)
            logger.info("Douyin discovery saved {} videos", len(discovered_videos))

        restrict_set = set(restrict_to_aweme_ids or [])
        if id_only or not collect_comments:
            logger.info(
                "Douyin scrape stopped before comment collection (id_only={}, collect_comments={})",
                id_only,
                collect_comments,
            )
            return

        pending_videos = self.store.get_unfinished_video_ids(sec_user_id)
        if restrict_set:
            pending_videos = [aweme_id for aweme_id in pending_videos if aweme_id in restrict_set]
        logger.info("Douyin pending videos from local store: {}", len(pending_videos))

        if use_local_index:
            await self._prepare_profile_page(page, sec_user_id)

        await self._collect_comments_for_videos(
            page,
            sec_user_id,
            pending_videos,
            task_id=task_id,
        )
        completed, partial, failed = self.store.count_video_statuses(sec_user_id)
        logger.info(
            "Douyin scrape complete for {} (completed={}, partial={}, failed={})",
            sec_user_id,
            completed,
            partial,
            failed,
        )

    async def scrape_author_posts(self, sec_user_id: str, **kwargs: Any) -> None:
        await self.by_author(sec_user_id, **kwargs)

    async def scrape_author_info(
        self,
        sec_user_id: str,
        *,
        page: Any | None = None,
    ) -> None:
        page = page or await self._new_page()
        await self._prepare_profile_page(page, sec_user_id)
        profile = await self._fetch_author_profile(page, sec_user_id)
        if profile is None:
            raise RuntimeError(f"No Douyin profile response for {sec_user_id}")
        self.store.save_author_profile(sec_user_id, profile)

    async def _prepare_profile_page(self, page: Any, sec_user_id: str) -> None:
        url = user_profile_url(sec_user_id)
        logger.info("Navigating to Douyin profile {}", url)
        await navigate(page, url)
        needed_login = await wait_for_login(
            page,
            timeout_ms=self.config.login_timeout_ms,
            prompt=self.prompt,
        )
        if needed_login:
            logger.info("Reloading Douyin profile after login")
            await navigate(page, url)
        logger.info("Douyin page: {}", await get_page_debug_info(page))

    async def _fetch_author_profile(self, page: Any, sec_user_id: str) -> dict[str, Any] | None:
        url = f"{DOUYIN_BASE_URL}/aweme/v1/web/user/profile/?{urlencode(base_params(sec_user_id=sec_user_id))}"
        logger.info("Fetching Douyin profile API {}", url)
        try:
            response = await browser_fetch_json(page, url)
        except RuntimeError as exc:
            logger.warning("Douyin profile API failed: {}", exc)
            return None
        return response if isinstance(response, dict) else None

    async def _collect_video_list(
        self,
        page: Any,
        sec_user_id: str,
        *,
        task_id: str | None,
    ) -> list[str]:
        logger.info("Phase A: collecting Douyin video list for {}", sec_user_id)
        seen: set[str] = set()
        collected: list[str] = []
        max_cursor = 0
        empty_pages = 0
        page_number = 0

        while True:
            if self.config.max_video_pages is not None and page_number >= self.config.max_video_pages:
                logger.info("Reached max_video_pages={}", self.config.max_video_pages)
                break

            page_number += 1
            response = await self._fetch_video_page(page, sec_user_id, max_cursor)
            aweme_list = response.get("aweme_list") if isinstance(response, dict) else None
            if not isinstance(aweme_list, list) or not aweme_list:
                empty_pages += 1
                logger.info(
                    "No Douyin videos on page {} (empty {}/{})",
                    page_number,
                    empty_pages,
                    self.config.max_empty_pages,
                )
                if empty_pages >= self.config.max_empty_pages:
                    break
                await smart_sleep()
                continue

            empty_pages = 0
            new_count = 0
            for aweme in aweme_list:
                if not isinstance(aweme, dict):
                    continue
                aweme_id = value_to_str(aweme.get("aweme_id"))
                if not aweme_id or aweme_id in seen:
                    continue
                seen.add(aweme_id)
                collected.append(aweme_id)
                self.store.save_video_raw(aweme_id, sec_user_id, aweme, task_id=task_id)
                new_count += 1

            has_more = value_to_bool(response.get("has_more"))
            next_cursor = value_to_int(response.get("max_cursor"))
            logger.info(
                "Douyin video page {} saved {} new videos (total={}, has_more={}, max_cursor={})",
                page_number,
                new_count,
                len(collected),
                has_more,
                next_cursor,
            )
            if not has_more:
                break
            if next_cursor is None or next_cursor == max_cursor:
                empty_pages += 1
                if empty_pages >= self.config.max_empty_pages:
                    break
            else:
                max_cursor = next_cursor
            await smart_sleep()

        logger.info("Phase A complete. Total Douyin videos collected: {}", len(collected))
        return collected

    async def _fetch_video_page(
        self,
        page: Any,
        sec_user_id: str,
        max_cursor: int,
    ) -> dict[str, Any]:
        params = base_params(
            sec_user_id=sec_user_id,
            max_cursor=max_cursor,
            count=self.config.request_count,
            locate_query=False,
            show_live_replay_strategy=1,
            need_time_list=1,
        )
        url = f"{DOUYIN_BASE_URL}/aweme/v1/web/aweme/post/?{urlencode(params)}"
        logger.info("Fetching Douyin video page cursor={}", max_cursor)
        response = await browser_fetch_json(page, url)
        return response if isinstance(response, dict) else {}

    async def _collect_comments_for_videos(
        self,
        page: Any,
        sec_user_id: str,
        aweme_ids: list[str],
        *,
        task_id: str | None,
    ) -> None:
        unfinished = [aweme_id for aweme_id in aweme_ids if aweme_id in set(self.store.get_unfinished_video_ids(sec_user_id))]
        logger.info("Phase B: collecting comments for {} Douyin videos", len(unfinished))
        for aweme_id in unfinished:
            try:
                await self._collect_comments_for_video(page, aweme_id, task_id=task_id)
            except Exception as exc:
                logger.exception("Failed collecting Douyin comments for {}: {}", aweme_id, exc)
                self.store.mark_video_error(aweme_id)

    async def _collect_comments_for_video(
        self,
        page: Any,
        aweme_id: str,
        *,
        task_id: str | None,
    ) -> None:
        logger.info("Collecting Douyin comments for {}", aweme_id)
        await navigate(page, video_url(aweme_id), wait_ms=2500)

        total_saved = 0
        cursor = self.store.get_video_comment_cursor(aweme_id)
        page_number = 0

        while True:
            if self.config.max_comment_pages is not None and page_number >= self.config.max_comment_pages:
                self.store.mark_video_comments_partial(aweme_id)
                logger.info("Reached max_comment_pages={} for {}", self.config.max_comment_pages, aweme_id)
                return

            response = await self._fetch_comment_page(page, aweme_id, cursor)
            comments = response.get("comments") if isinstance(response, dict) else None
            if not isinstance(comments, list) or not comments:
                break

            page_number += 1
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                if self.store.save_comment(aweme_id, comment, task_id=task_id):
                    total_saved += 1
                parent_cid = value_to_str(comment.get("cid"))
                replies = comment.get("reply_comment")
                if isinstance(replies, list):
                    for reply in replies:
                        if isinstance(reply, dict) and self.store.save_comment(
                            aweme_id,
                            reply,
                            parent_cid,
                            task_id=task_id,
                        ):
                            total_saved += 1
                reply_total = value_to_int(comment.get("reply_comment_total")) or 0
                if reply_total > (len(replies) if isinstance(replies, list) else 0) and parent_cid:
                    total_saved += await self._collect_replies_for_comment(
                        page,
                        aweme_id,
                        parent_cid,
                        task_id=task_id,
                    )

            has_more = value_to_bool(response.get("has_more"))
            next_cursor = value_to_int(response.get("cursor"))
            if next_cursor is None:
                next_cursor = value_to_int(response.get("next_cursor"))
            logger.info(
                "Douyin comments {} page {} saved_total={} has_more={} cursor={}",
                aweme_id,
                page_number,
                total_saved,
                has_more,
                next_cursor,
            )
            if next_cursor is not None:
                self.store.update_video_comment_cursor(aweme_id, next_cursor)
                cursor = next_cursor
            if not has_more:
                break
            await smart_sleep()

        self.store.mark_video_comments_done(aweme_id)
        logger.info("Marked Douyin video {} comments done (saved={})", aweme_id, total_saved)

    async def _fetch_comment_page(
        self,
        page: Any,
        aweme_id: str,
        cursor: int,
    ) -> dict[str, Any]:
        params = base_params(
            aweme_id=aweme_id,
            cursor=cursor,
            count=self.config.request_count,
            item_type=0,
        )
        url = f"{DOUYIN_BASE_URL}/aweme/v1/web/comment/list/?{urlencode(params)}"
        response = await browser_fetch_json(page, url)
        return response if isinstance(response, dict) else {}

    async def _collect_replies_for_comment(
        self,
        page: Any,
        aweme_id: str,
        comment_id: str,
        *,
        task_id: str | None,
    ) -> int:
        saved = 0
        cursor = 0
        page_number = 0
        while True:
            if self.config.max_reply_pages is not None and page_number >= self.config.max_reply_pages:
                return saved
            response = await self._fetch_reply_page(page, aweme_id, comment_id, cursor)
            comments = response.get("comments") if isinstance(response, dict) else None
            if not isinstance(comments, list) or not comments:
                return saved
            page_number += 1
            for comment in comments:
                if isinstance(comment, dict) and self.store.save_comment(
                    aweme_id,
                    comment,
                    comment_id,
                    task_id=task_id,
                ):
                    saved += 1
            has_more = value_to_bool(response.get("has_more"))
            next_cursor = value_to_int(response.get("cursor"))
            if next_cursor is None:
                next_cursor = value_to_int(response.get("next_cursor"))
            if not has_more or next_cursor is None or next_cursor == cursor:
                return saved
            cursor = next_cursor
            await smart_sleep(0.5, 1.5)

    async def _fetch_reply_page(
        self,
        page: Any,
        aweme_id: str,
        comment_id: str,
        cursor: int,
    ) -> dict[str, Any]:
        params = base_params(
            item_id=aweme_id,
            aweme_id=aweme_id,
            comment_id=comment_id,
            cursor=cursor,
            count=self.config.request_count,
            item_type=0,
        )
        url = f"{DOUYIN_BASE_URL}/aweme/v1/web/comment/list/reply/?{urlencode(params)}"
        response = await browser_fetch_json(page, url)
        return response if isinstance(response, dict) else {}

def base_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "190500",
        "version_name": "19.5.0",
        "cookie_enabled": "true",
        "screen_width": "1440",
        "screen_height": "900",
        "browser_language": "zh-CN",
        "browser_platform": "MacIntel",
        "browser_name": "Chrome",
        "browser_version": "120.0.0.0",
    }
    params.update({key: value for key, value in overrides.items() if value is not None})
    return params
