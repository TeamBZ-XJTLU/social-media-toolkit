from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from crawler.base import BrowserCrawler, BrowserCrawlerConfig
from storage import DuckDBDatabase
from .storage import RednoteStore
from .utils import (
    BASE_URL,
    USER_PROFILE_URL,
    check_and_wait_for_user_action,
    collect_author_post_ids,
    expand_all_sub_comments,
    get_author_feeds_height,
    get_comments_container,
    normalize_post_url,
    scroll_to_load_all_comments,
    smart_sleep,
    wait_for_feeds_loading_indicator,
    wait_until_logged_in,
)


SEARCH_RESULT_AI_URL = "https://www.xiaohongshu.com/search_result_ai"
SEARCH_NOTES_API_URL = "https://so.xiaohongshu.com/api/sns/web/v2/search/notes"
SEARCH_NOTES_IDLE_TIMEOUT_SECONDS = 60.0


@dataclass(slots=True)
class RednoteCrawlerConfig(BrowserCrawlerConfig):
    login_timeout_ms: int = 500
    max_no_height_increase: int = 5
    scroll_step_px: int = 500
    post_open_delay_ms: int = 500
    post_load_delay_ms: int = 2000


class RednoteCrawler(BrowserCrawler[RednoteCrawlerConfig, RednoteStore]):
    """Xiaohongshu/Rednote crawler using cloakbrowser and DuckDB."""

    db_cls = DuckDBDatabase
    store_cls = RednoteStore

    async def by_author(
        self,
        author_id: str,
        *,
        id_only: bool = False,
        restrict_to_post_ids: Iterable[str] | None = None,
        use_local_index: bool = False,
        task_id: str | None = None,
        page: Any | None = None,
    ) -> None:
        page = page or await self._new_page()
        restrict_set = set(restrict_to_post_ids or [])

        if not use_local_index:
            url = f"{USER_PROFILE_URL}/{author_id}"
            logger.info("Navigating to {}", url)
            await page.goto(url)
            await self._wait_for_manual_action(page)

            processed: set[tuple[str, str]] = set()
            previous_height = await get_author_feeds_height(page)
            no_height_increase_count = 0

            while True:
                post_ids = await collect_author_post_ids(page)
                self._save_discovered_post_ids(
                    post_ids,
                    processed,
                    author_id=author_id,
                    restrict_set=restrict_set,
                    task_id=task_id,
                )

                await smart_sleep()
                await page.evaluate(f"window.scrollBy(0, {self.config.scroll_step_px})")
                await page.wait_for_timeout(500)
                await wait_for_feeds_loading_indicator(page)

                current_height = await get_author_feeds_height(page)
                if current_height <= previous_height:
                    no_height_increase_count += 1
                    if no_height_increase_count >= self.config.max_no_height_increase:
                        break
                else:
                    no_height_increase_count = 0
                    previous_height = current_height

            logger.info("Total Rednote post IDs discovered: {}", len(processed))

        if id_only:
            return

        if use_local_index:
            logger.info("Navigating to {} for Rednote login/session check", BASE_URL)
            await page.goto(BASE_URL)
            await self._wait_for_manual_action(page)

        await self._scrape_pending_posts_from_store(
            context=page.context,
            author_id=author_id,
            restrict_set=restrict_set,
            task_id=task_id,
        )

    async def scrape_author_posts(self, author_id: str, **kwargs: Any) -> None:
        await self.by_author(author_id, **kwargs)

    async def by_keyword(
        self,
        keyword: str,
        *,
        id_only: bool = False,
        restrict_to_post_ids: Iterable[str] | None = None,
        use_local_index: bool = False,
        task_id: str | None = None,
        page: Any | None = None,
    ) -> None:
        page = page or await self._new_page()
        restrict_set = set(restrict_to_post_ids or [])

        if not use_local_index:
            logger.info("Navigating to {}", BASE_URL)
            await page.goto(BASE_URL)
            await self._wait_for_manual_action(page)

            await self._collect_keyword_search_metadata(
                page,
                keyword=keyword,
                task_id=task_id,
            )
            return

        if id_only:
            return

        if use_local_index:
            logger.info("Navigating to {} for Rednote login/session check", BASE_URL)
            await page.goto(BASE_URL)
            await self._wait_for_manual_action(page)

        await self._scrape_pending_posts_from_store(
            context=page.context,
            author_id="unknown",
            restrict_set=restrict_set,
            task_id=task_id,
        )

    async def scrape_keyword(self, keyword: str, **kwargs: Any) -> None:
        await self.by_keyword(keyword, **kwargs)

    async def _collect_keyword_search_metadata(
        self,
        page: Any,
        *,
        keyword: str,
        task_id: str | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        pending_tasks: set[asyncio.Task[None]] = set()
        last_request_at = loop.time()
        matching_response_count = 0
        saved_note_count = 0

        async def process_search_notes_response(response: Any) -> None:
            nonlocal saved_note_count
            try:
                payload = await response.json()
            except Exception as exc:
                logger.warning("Could not parse Rednote search notes response: {}", exc)
                return
            if not isinstance(payload, dict):
                return

            saved_count = self.store.save_search_note_metadata_response(
                payload,
                keyword=keyword,
                request_url=str(response.url),
                task_id=task_id,
            )
            saved_note_count += saved_count
            logger.info("Saved {} Rednote search note metadata records", saved_count)
            # TODO: After one search/notes request is processed and saved,
            # add detailed post processing here.

        def on_response(response: Any) -> None:
            nonlocal last_request_at, matching_response_count
            if not self._is_search_notes_response(response):
                return
            matching_response_count += 1
            last_request_at = loop.time()
            task = asyncio.create_task(process_search_notes_response(response))
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        page.on("response", on_response)
        search_url = self._keyword_search_url(keyword)
        logger.info("Navigating to Rednote keyword API-backed search {}", search_url)
        await page.goto(search_url)
        await page.wait_for_load_state("domcontentloaded")

        end_container_seen = False
        while loop.time() - last_request_at < SEARCH_NOTES_IDLE_TIMEOUT_SECONDS:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)
            end_container_seen = await page.evaluate(
                """
                Boolean(Array.from(document.querySelectorAll('.end-container')).some((element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                }))
                """
            )
            if end_container_seen:
                logger.info("Rednote keyword search reached visible .end-container")
                break

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        logger.info(
            "Rednote keyword search stopped: reason={}, responses={}, notes_saved={}",
            "end_container" if end_container_seen else f"{int(SEARCH_NOTES_IDLE_TIMEOUT_SECONDS)}s_idle",
            matching_response_count,
            saved_note_count,
        )

    def _is_search_notes_response(self, response: Any) -> bool:
        if str(getattr(response, "url", "")).split("?", 1)[0] != SEARCH_NOTES_API_URL:
            return False
        request = getattr(response, "request", None)
        method = getattr(request, "method", "") if request is not None else ""
        if callable(method):
            method = method()
        return str(method).upper() == "POST"

    def _keyword_search_url(self, keyword: str) -> str:
        params = {"keyword": keyword, "source": "web_explore_feed"}
        return f"{SEARCH_RESULT_AI_URL}?{urlencode(params)}"

    def _save_discovered_post_ids(
        self,
        post_ids: set[tuple[str, str]],
        processed: set[tuple[str, str]],
        *,
        author_id: str,
        restrict_set: set[str],
        task_id: str | None,
    ) -> None:
        new_post_ids = post_ids - processed
        if restrict_set:
            skipped = {item for item in new_post_ids if item[0] not in restrict_set}
            processed.update(skipped)
            new_post_ids -= skipped

        for post_id, post_url in sorted(new_post_ids):
            self.store.save_post_raw(
                post_id,
                author_id,
                url=normalize_post_url(post_url),
                task_id=task_id,
            )
            processed.add((post_id, post_url))

    async def _scrape_pending_posts_from_store(
        self,
        *,
        context: Any,
        author_id: str,
        restrict_set: set[str],
        task_id: str | None,
    ) -> None:
        pending_posts = self.store.list_pending_posts(
            author_id,
            restrict_to_post_ids=restrict_set,
        )
        logger.info("Rednote pending posts from local store: {}", len(pending_posts))
        for post in pending_posts:
            post_id = str(post.get("uid") or post.get("id"))
            post_url = str(post.get("url"))
            await self._scrape_one_post(
                post_id,
                post_url,
                context=context,
                author_id=author_id,
                task_id=task_id,
            )

    async def _scrape_one_post(
        self,
        post_id: str,
        post_url: str,
        *,
        context: Any,
        author_id: str,
        task_id: str | None,
    ) -> None:
        full_url = normalize_post_url(post_url)
        await smart_sleep()
        logger.info("Opening post: {}", full_url)

        post_page = await context.new_page()
        try:
            await post_page.goto(full_url)
            await post_page.wait_for_timeout(self.config.post_open_delay_ms)

            rate_limited = await post_page.evaluate(
                "Boolean(document.body.textContent.includes('访问频次异常'))"
            )
            if rate_limited:
                logger.info("Rate limit detected, skipping post {}", post_id)
                return

            await post_page.wait_for_timeout(self.config.post_load_delay_ms)
            note_exists = await post_page.evaluate(
                "Boolean(document.querySelector('#noteContainer'))"
            )
            if not note_exists:
                logger.info("#noteContainer not found for {}", post_id)
                return

            if await get_comments_container(post_page):
                await scroll_to_load_all_comments(post_page)
                await expand_all_sub_comments(post_page)

            html = await post_page.evaluate("document.querySelector('#noteContainer')?.innerHTML")
            if html:
                self.store.save_post_raw(
                    post_id,
                    author_id,
                    url=full_url,
                    html=str(html),
                    task_id=task_id,
                )
                logger.info("Captured post {} ({} bytes)", post_id, len(str(html)))
        finally:
            await post_page.close()

    async def _wait_for_manual_action(self, page: Any) -> None:
        await check_and_wait_for_user_action(
            page,
            timeout_ms=self.config.login_timeout_ms,
            prompt=self.prompt,
        )
        await wait_until_logged_in(
            page,
            timeout_ms=self.config.login_timeout_ms,
            prompt=self.prompt,
        )
