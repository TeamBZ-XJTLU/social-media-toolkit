from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4
from urllib.parse import parse_qs, urlencode, urlparse

from loguru import logger

from crawler.base import BrowserCrawler, BrowserCrawlerConfig
from storage import DuckDBDatabase
from .storage import (
    RednoteStore,
    extract_initial_state_from_html,
    extract_og_image_urls_from_html,
    extract_post_detail_from_initial_state,
)
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
COMMENT_PAGE_API_URL = "https://edith.xiaohongshu.com/api/sns/web/v2/comment/page"
COMMENT_SUB_PAGE_API_URL = "https://edith.xiaohongshu.com/api/sns/web/v2/comment/sub/page"
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

    async def download_from_url(
        self,
        url: str,
        *,
        author_id: str = "unknown",
        task_id: str | None = None,
        page: Any | None = None,
    ) -> None:
        page = page or await self._new_page()
        logger.info("Navigating to {} for Rednote login/session check", BASE_URL)
        await page.goto(BASE_URL)
        await self._wait_for_manual_action(page)

        post_id = self._post_id_from_url(url)
        post_url = normalize_post_url(url)
        self.store.save_post_raw(
            post_id,
            author_id,
            url=post_url,
            task_id=task_id,
        )
        await self._scrape_one_post(
            post_id,
            post_url,
            context=page.context,
            author_id=author_id,
            task_id=task_id,
        )

    async def _collect_keyword_search_metadata(
        self,
        page: Any,
        *,
        keyword: str,
        task_id: str | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        response_lock = asyncio.Lock()
        pending_tasks: set[asyncio.Task[None]] = set()
        processed_detail_post_ids: set[str] = set()
        last_request_at = loop.time()
        matching_response_count = 0
        saved_note_count = 0
        detail_attempt_count = 0

        async def process_search_notes_response(response: Any) -> None:
            nonlocal saved_note_count, detail_attempt_count
            try:
                payload = await response.json()
            except Exception as exc:
                logger.warning("Could not parse Rednote search notes response: {}", exc)
                return
            if not isinstance(payload, dict):
                return

            async with response_lock:
                saved_records = self.store.save_search_note_metadata_records_from_response(
                    payload,
                    keyword=keyword,
                    request_url=str(response.url),
                    task_id=task_id,
                )
                saved_note_count += len(saved_records)
                logger.info("Saved {} Rednote search note metadata records", len(saved_records))

                for record in saved_records:
                    post_id = str(record.get("post_id") or record.get("uid") or record.get("id") or "")
                    post_url = str(record.get("url") or "")
                    author_id = str(record.get("author_id") or "unknown")
                    if not post_id or not post_url or post_id in processed_detail_post_ids:
                        continue
                    processed_detail_post_ids.add(post_id)
                    self.store.save_post_raw(
                        post_id,
                        author_id,
                        url=post_url,
                        task_id=task_id,
                    )
                    if (
                        self.store.is_post_already_scraped(post_id)
                        and self.store.is_post_detail_parsed(post_id)
                    ):
                        logger.info("Rednote post {} already has parsed details, skipping", post_id)
                        continue
                    detail_attempt_count += 1
                    await self._scrape_one_post(
                        post_id,
                        post_url,
                        context=page.context,
                        author_id=author_id,
                        task_id=task_id,
                    )

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
            "Rednote keyword search stopped: reason={}, responses={}, notes_saved={}, details_attempted={}",
            "end_container" if end_container_seen else f"{int(SEARCH_NOTES_IDLE_TIMEOUT_SECONDS)}s_idle",
            matching_response_count,
            saved_note_count,
            detail_attempt_count,
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

    def _post_id_from_url(self, url: str) -> str:
        path = urlparse(normalize_post_url(url)).path.rstrip("/")
        post_id = path.rsplit("/", 1)[-1]
        if not post_id:
            raise ValueError(f"Could not extract Rednote post id from URL: {url}")
        return post_id

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
        pending_comment_tasks: set[asyncio.Task[None]] = set()

        async def save_comment_response(response: Any) -> None:
            try:
                payload = await response.json()
            except Exception as exc:
                logger.debug("Could not parse Rednote comment response for {}: {}", post_id, exc)
                return
            if not isinstance(payload, dict):
                return
            saved_count = self.store.save_comments_from_response(
                payload,
                post_id=post_id,
                parent_comment_id=self._comment_parent_id_from_url(str(response.url)),
                task_id=task_id,
            )
            if saved_count:
                logger.info("Saved {} Rednote comments for {}", saved_count, post_id)

        def on_response(response: Any) -> None:
            if not self._is_comment_page_response(response, post_id):
                return
            task = asyncio.create_task(save_comment_response(response))
            pending_comment_tasks.add(task)
            task.add_done_callback(pending_comment_tasks.discard)

        post_page.on("response", on_response)
        try:
            document_html: str | None = None
            response = await post_page.goto(full_url)
            if response is not None:
                try:
                    document_html = await response.text()
                except Exception as exc:
                    logger.debug("Could not read Rednote document response for {}: {}", post_id, exc)
            await post_page.wait_for_timeout(self.config.post_open_delay_ms)

            rate_limited = await post_page.evaluate(
                "Boolean(document.body.textContent.includes('访问频次异常'))"
            )
            if rate_limited:
                logger.info("Rate limit detected, skipping post {}", post_id)
                return

            initial_state = extract_initial_state_from_html(document_html or "")
            if not isinstance(initial_state, dict):
                initial_state = await post_page.evaluate(
                    """
                    () => {
                        const state = window.__INITIAL_STATE__;
                        if (!state) {
                            return null;
                        }
                        try {
                            return JSON.parse(JSON.stringify(state));
                        } catch (error) {
                            return null;
                        }
                    }
                    """
                )
            if not isinstance(initial_state, dict):
                page_content = await post_page.content()
                initial_state = extract_initial_state_from_html(page_content)
            else:
                page_content = ""
            image_urls = extract_og_image_urls_from_html(document_html or "")
            if not image_urls:
                if not page_content:
                    page_content = await post_page.content()
                image_urls = extract_og_image_urls_from_html(page_content)
            parsed = extract_post_detail_from_initial_state(
                initial_state,
                fallback_post_id=post_id,
                image_urls=image_urls,
            )
            if not parsed.get("noteId"):
                logger.warning("Rednote initial state did not contain parsed note data for {}", post_id)
            parsed_author_id = parsed.get("author_id")
            if parsed_author_id:
                author_id = str(parsed_author_id)

            await post_page.wait_for_timeout(self.config.post_load_delay_ms)
            note_exists = await post_page.evaluate(
                "Boolean(document.querySelector('#noteContainer'))"
            )
            if not note_exists:
                logger.info("#noteContainer not found for {}", post_id)
                if parsed:
                    self.store.save_post_raw(
                        post_id,
                        author_id,
                        url=full_url,
                        task_id=task_id,
                        parsed=parsed,
                    )
                    await self._download_post_media_files(
                        post_page,
                        post_id=post_id,
                        parsed=parsed,
                        referer=full_url,
                        task_id=task_id,
                    )
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
                    parsed=parsed,
                )
                await self._download_post_media_files(
                    post_page,
                    post_id=post_id,
                    parsed=parsed,
                    referer=full_url,
                    task_id=task_id,
                )
                logger.info("Captured post {} ({} bytes)", post_id, len(str(html)))
        finally:
            try:
                post_page.remove_listener("response", on_response)
            except Exception:
                pass
            if pending_comment_tasks:
                await asyncio.gather(*pending_comment_tasks, return_exceptions=True)
            await post_page.close()

    def _is_comment_page_response(self, response: Any, post_id: str) -> bool:
        url = str(getattr(response, "url", "") or "")
        if not (url.startswith(COMMENT_PAGE_API_URL) or url.startswith(COMMENT_SUB_PAGE_API_URL)):
            return False
        request = getattr(response, "request", None)
        method = str(getattr(request, "method", "GET") or "GET").upper()
        if method != "GET":
            return False
        parsed = urlparse(url)
        note_ids = parse_qs(parsed.query).get("note_id") or []
        return post_id in note_ids if note_ids else True

    def _comment_parent_id_from_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        for key in ("root_comment_id", "comment_id", "parent_comment_id"):
            values = query.get(key) or []
            if values:
                return values[0]
        return None

    async def _download_post_media_files(
        self,
        page: Any,
        *,
        post_id: str,
        parsed: dict[str, Any],
        referer: str,
        task_id: str | None,
    ) -> None:
        if not self.config.download_media:
            return

        image_urls = parsed.get("images")
        if not isinstance(image_urls, list):
            return

        media_dir = Path(self.config.media_download_dir or "data/media/rednote")
        media_dir.mkdir(parents=True, exist_ok=True)
        downloaded_count = 0

        for image_url_value in image_urls:
            if not isinstance(image_url_value, str) or not image_url_value:
                continue
            existing = self.store.get_post_media_file(post_id, image_url_value)
            if existing and existing.get("local_path") and Path(str(existing["local_path"])).exists():
                continue

            try:
                content, content_type = await self._fetch_media_bytes(
                    page,
                    image_url_value,
                    referer=referer,
                )
            except Exception as exc:
                logger.warning("Could not download Rednote image {}: {}", image_url_value, exc)
                continue

            suffix = self._media_file_suffix(image_url_value, content_type)
            local_path = media_dir / f"{uuid4().hex}{suffix}"
            local_path.write_bytes(content)
            self.store.save_post_media_file(
                post_id=post_id,
                media_url=image_url_value,
                media_type="image",
                local_path=str(local_path),
                task_id=task_id,
            )
            downloaded_count += 1

        if downloaded_count:
            logger.info("Downloaded {} Rednote media files for {}", downloaded_count, post_id)

    async def _fetch_media_bytes(
        self,
        page: Any,
        media_url: str,
        *,
        referer: str,
    ) -> tuple[bytes, str | None]:
        request_context = getattr(page.context, "request", None)
        if request_context is not None:
            response = await request_context.get(media_url, headers={"referer": referer})
            status = int(getattr(response, "status", 0) or 0)
            if status < 400:
                headers = getattr(response, "headers", {}) or {}
                content_type = headers.get("content-type") if isinstance(headers, dict) else None
                return await response.body(), content_type

        result = await page.evaluate(
            """
            async ({ mediaUrl }) => {
                const response = await fetch(mediaUrl, { credentials: "include" });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const contentType = response.headers.get("content-type");
                const bytes = new Uint8Array(await response.arrayBuffer());
                let binary = "";
                const chunkSize = 0x8000;
                for (let index = 0; index < bytes.length; index += chunkSize) {
                    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
                }
                return { body: btoa(binary), contentType };
            }
            """,
            {"mediaUrl": media_url},
        )
        if not isinstance(result, dict) or not isinstance(result.get("body"), str):
            raise RuntimeError("Browser fetch did not return media bytes.")
        content_type = result.get("contentType") if isinstance(result.get("contentType"), str) else None
        return base64.b64decode(result["body"]), content_type

    def _media_file_suffix(self, media_url: str, content_type: str | None) -> str:
        normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
        if normalized_content_type == "image/jpeg":
            return ".jpg"
        if normalized_content_type == "image/png":
            return ".png"
        if normalized_content_type == "image/webp":
            return ".webp"
        if normalized_content_type == "image/gif":
            return ".gif"

        suffix = Path(urlparse(media_url).path).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return ".jpg" if suffix == ".jpeg" else suffix
        return ".bin"

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
