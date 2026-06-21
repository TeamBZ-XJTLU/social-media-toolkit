from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

from loguru import logger

from crawler.base import BrowserCrawler, BrowserCrawlerConfig
from storage import DuckDBDatabase

from .storage import WeiboStore
from .utils import (
    PROFILE_INFO_ENDPOINT_PREFIX,
    WEIBO_BASE_URL,
    WEIBO_PROFILE_URL,
    browser_fetch_json,
    check_and_wait_for_user_action,
    maybe_cooldown,
    next_max_id,
    parse_weibo_ids,
    value_to_int,
    value_to_str,
    wait_until_logged_in,
    weibo_dynamic_sleep,
)


WEIBO_SEARCH_URL = "https://s.weibo.com/weibo"


@dataclass(slots=True)
class WeiboCrawlerConfig(BrowserCrawlerConfig):
    login_timeout_ms: int = 2000
    max_empty_pages: int = 3
    post_open_delay_ms: int = 2000
    fetch_comments: bool = True
    max_pages: int | None = None
    max_comment_pages: int | None = None


class WeiboCrawler(BrowserCrawler[WeiboCrawlerConfig, WeiboStore]):
    """Weibo crawler using cloakbrowser plus browser-side authenticated fetch calls."""

    db_cls = DuckDBDatabase
    store_cls = WeiboStore

    async def by_keyword(
        self,
        keyword: str,
        *,
        max_pages: int | None = None,
        search_params: Mapping[str, str | int] | None = None,
        task_id: str | None = None,
        page: Any | None = None,
    ) -> None:
        page = page or await self._new_page()
        await self._ensure_logged_in(page)

        params = dict(search_params or {})
        first_url = self._keyword_search_url(keyword, extra_params=params)
        logger.info("Navigating to Weibo keyword search {}", first_url)
        await page.goto(first_url)
        if not await self._wait_for_search_feed(page):
            logger.warning("Weibo keyword search page did not load #pl_feedlist_index")
            return

        total_pages = await self._get_keyword_total_pages(page)
        page_limit = max_pages or self.config.max_pages
        if page_limit is not None:
            total_pages = min(total_pages, page_limit)
        logger.info("Weibo keyword {!r} returned {} pages", keyword, total_pages)

        sleep_step = 0
        for page_number in range(1, total_pages + 1):
            search_url = first_url if page_number == 1 else self._keyword_search_url(
                keyword,
                page_number,
                extra_params=params,
            )
            if page_number != 1:
                logger.info("Navigating to Weibo keyword page {}: {}", page_number, search_url)
                await page.goto(search_url)
                await self._wait_for_search_feed(page)

            feed_exists = await page.evaluate(
                "Boolean(document.querySelector('#pl_feedlist_index'))"
            )
            if not feed_exists:
                logger.warning("Weibo keyword page {} has no #pl_feedlist_index", page_number)
                break

            unfolded = await self._unfold_search_posts(page)
            if unfolded:
                logger.info("Unfolded {} Weibo search posts on page {}", unfolded, page_number)
                await page.wait_for_timeout(800)

            posts = await self._extract_search_posts(
                page,
                keyword=keyword,
                page_number=page_number,
                total_pages=total_pages,
                search_url=search_url,
                search_params=params,
            )
            saved_count = 0
            for post in posts:
                if self.store.save_search_post(
                    post,
                    keyword=keyword,
                    page_number=page_number,
                    task_id=task_id,
                ):
                    saved_count += 1
            logger.info(
                "Saved {} Weibo keyword posts from page {}/{}",
                saved_count,
                page_number,
                total_pages,
            )

            sleep_step = await weibo_dynamic_sleep(sleep_step)
            await maybe_cooldown(sleep_step)

    async def scrape_author_info(
        self,
        author_ids: str,
        *,
        page: Any | None = None,
    ) -> None:
        ids = parse_weibo_ids(author_ids)
        if not ids:
            raise ValueError("No author_id provided.")

        page = page or await self._new_page()
        await self._ensure_logged_in(page)

        for author_id in ids:
            self._validate_author_id(author_id)
            profile = await self._fetch_author_profile(page, author_id)
            if profile is not None:
                self.store.save_author_profile(author_id, profile)
                logger.info("Saved Weibo author profile {}", author_id)

    async def by_author(
        self,
        author_id: str,
        *,
        fetch_comments: bool | None = None,
        restrict_to_post_ids: Iterable[str] | None = None,
        use_local_index: bool = False,
        task_id: str | None = None,
        page: Any | None = None,
    ) -> None:
        self._validate_author_id(author_id)
        fetch_comments = self.config.fetch_comments if fetch_comments is None else fetch_comments
        page = page or await self._new_page()

        await self._ensure_logged_in(page)
        restrict_set = set(restrict_to_post_ids or [])
        sleep_step = 0

        if not use_local_index:
            profile = await self._fetch_author_profile(page, author_id)
            statuses_count = None
            if profile is not None:
                self.store.save_author_profile(author_id, profile)
                statuses_count = value_to_int(profile.get("statuses_count"))
                logger.info("Author {} statuses_count={}", author_id, statuses_count)

            target_url = f"{WEIBO_PROFILE_URL}/{author_id}"
            logger.info("Navigating to Weibo profile {}", target_url)
            await page.goto(target_url)
            await page.wait_for_timeout(5000)

            seen_post_ids: set[str] = set()
            discovered_count = 0
            page_number = 1
            consecutive_empty = 0

            while True:
                if self.config.max_pages is not None and page_number > self.config.max_pages:
                    logger.info("Reached max_pages={}", self.config.max_pages)
                    break
                if statuses_count is not None and discovered_count >= statuses_count:
                    logger.info("Collected {} posts, matching statuses_count", discovered_count)
                    break

                response = await self._fetch_posts_page(page, author_id, page_number)
                posts, raw_items = extract_posts_from_response(response, author_id)
                posts = [(post_id, url) for post_id, url in posts if post_id not in seen_post_ids]
                post_id_set = {post_id for post_id, _ in posts}
                raw_items = [
                    item
                    for item in raw_items
                    if (value_to_str(item.get("id")) or value_to_str(item.get("mblogid")) or value_to_str(item.get("mid")))
                    in post_id_set
                ]

                if not posts:
                    consecutive_empty += 1
                    logger.info(
                        "No posts on Weibo page {} (empty {}/{})",
                        page_number,
                        consecutive_empty,
                        self.config.max_empty_pages,
                    )
                    if consecutive_empty >= self.config.max_empty_pages:
                        break
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2000)
                    continue

                consecutive_empty = 0
                self.store.save_post_list_with_meta(raw_items, author_id, task_id=task_id)
                for post_id, _ in posts:
                    seen_post_ids.add(post_id)
                    discovered_count += 1

                logger.info(
                    "Saved {} Weibo post IDs from page {} (total discovered {})",
                    len(posts),
                    page_number,
                    discovered_count,
                )
                sleep_step = await weibo_dynamic_sleep(sleep_step)
                await maybe_cooldown(sleep_step)
                page_number += 1

        pending_posts = self.store.list_pending_posts(
            author_id,
            restrict_to_post_ids=restrict_set,
        )
        logger.info("Weibo pending posts from local store: {}", len(pending_posts))

        for post in pending_posts:
            post_id = str(post.get("uid") or post.get("id"))
            post_url = str(post.get("url"))

            await self._scrape_one_post(
                page,
                post_id,
                post_url,
                author_id=author_id,
                task_id=task_id,
                fetch_comments=fetch_comments,
                sleep_step=sleep_step,
            )
            sleep_step = await weibo_dynamic_sleep(sleep_step)
            await maybe_cooldown(sleep_step)

        logger.info("Weibo scrape completed for author {}", author_id)

    async def scrape_author_posts(self, author_id: str, **kwargs: Any) -> None:
        await self.by_author(author_id, **kwargs)

    async def download_from_url(
        self,
        url: str,
        *,
        author_id: str = "unknown",
        post_id: str | None = None,
        fetch_comments: bool | None = None,
        task_id: str | None = None,
        page: Any | None = None,
    ) -> None:
        page = page or await self._new_page()
        await self._ensure_logged_in(page)

        resolved_post_id = post_id or self._post_id_from_url(url)
        await self._scrape_one_post(
            page,
            resolved_post_id,
            url,
            author_id=author_id,
            task_id=task_id,
            fetch_comments=self.config.fetch_comments if fetch_comments is None else fetch_comments,
            sleep_step=0,
        )

    async def _get_keyword_total_pages(self, page: Any) -> int:
        value = await page.evaluate(
            """
            (() => {
                const feed = document.querySelector('#pl_feedlist_index');
                if (!feed) return 1;

                const pageList = feed.querySelector(
                    'ul.feed_list_page_morelist, ul[action-type="feed_list_page_morelist"], .m-page ul.s-scroll'
                );
                const liCount = pageList ? pageList.querySelectorAll('li').length : 0;
                const hrefPages = Array.from(feed.querySelectorAll('a[href*="page="]'))
                    .map((link) => {
                        try {
                            return Number(new URL(link.href, location.href).searchParams.get('page'));
                        } catch (error) {
                            return 0;
                        }
                    })
                    .filter((pageNumber) => Number.isFinite(pageNumber) && pageNumber > 0);
                return Math.max(1, liCount, ...hrefPages);
            })()
            """
        )
        return value_to_int(value) or 1

    async def _wait_for_search_feed(self, page: Any, timeout_ms: int = 15000) -> bool:
        elapsed_ms = 0
        step_ms = 500
        while elapsed_ms <= timeout_ms:
            if await page.evaluate("Boolean(document.querySelector('#pl_feedlist_index'))"):
                return True
            await page.wait_for_timeout(step_ms)
            elapsed_ms += step_ms
        return False

    async def _unfold_search_posts(self, page: Any) -> int:
        value = await page.evaluate(
            """
            async () => {
                const links = Array.from(document.querySelectorAll('a[action-type="fl_unfold"]'));
                for (const link of links) {
                    link.dispatchEvent(new MouseEvent('click', {
                        bubbles: true,
                        cancelable: true,
                        view: window
                    }));
                    await new Promise((resolve) => setTimeout(resolve, 150));
                }
                return links.length;
            }
            """
        )
        return value_to_int(value) or 0

    async def _extract_search_posts(
        self,
        page: Any,
        *,
        keyword: str,
        page_number: int,
        total_pages: int,
        search_url: str,
        search_params: Mapping[str, str | int],
    ) -> list[dict[str, Any]]:
        posts = await page.evaluate(
            """
            ({ keyword, pageNumber, totalPages, searchUrl, searchParams }) => {
                const feed = document.querySelector('#pl_feedlist_index');
                if (!feed) return [];

                const absoluteUrl = (href) => {
                    if (!href) return null;
                    try {
                        return new URL(href, location.href).href;
                    } catch (error) {
                        return href;
                    }
                };
                const cleanText = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const text = (element) => cleanText(element?.textContent || '');
                const html = (element) => element?.innerHTML || null;
                const attr = (element, name) => element?.getAttribute(name) || null;
                const authorIdFromUrl = (href) => {
                    if (!href) return null;
                    const decoded = decodeURIComponent(href);
                    const match = decoded.match(/weibo\\.com\\/(?:u\\/)?(\\d+)/);
                    return match ? match[1] : null;
                };
                const postIdFromUrl = (href) => {
                    if (!href) return null;
                    const match = href.match(/weibo\\.com\\/(?:u\\/)?\\d+\\/([^/?#]+)/);
                    return match ? match[1] : null;
                };
                const parseCount = (value) => {
                    const raw = cleanText(value);
                    if (!raw || raw === '转发' || raw === '评论' || raw === '赞') return 0;
                    const normalized = raw.replace(/,/g, '');
                    const numeric = Number.parseFloat(normalized);
                    if (!Number.isFinite(numeric)) return null;
                    return raw.includes('万') ? Math.round(numeric * 10000) : Math.round(numeric);
                };
                const linkInfo = (link) => ({
                    text: text(link),
                    href: absoluteUrl(link.getAttribute('href')),
                    title: attr(link, 'title'),
                    action_type: attr(link, 'action-type'),
                    suda_data: attr(link, 'suda-data'),
                });
                const contentLinks = (root) => Array.from(root?.querySelectorAll('a[href]') || [])
                    .map(linkInfo)
                    .filter((link) => link.href || link.text);
                const mediaImages = (item) => Array.from(item.querySelectorAll('.media img, img[action-data]'))
                    .map((image) => ({
                        src: absoluteUrl(image.getAttribute('src')),
                        alt: attr(image, 'alt'),
                        action_data: attr(image, 'action-data'),
                    }))
                    .filter((image) => image.src);
                const mediaVideos = (item) => Array.from(item.querySelectorAll('video, .WB_video_h5, .wbp-video, .media-video-a'))
                    .map((video) => ({
                        tag: video.tagName.toLowerCase(),
                        src: absoluteUrl(video.getAttribute('src')),
                        poster: absoluteUrl(video.getAttribute('poster')),
                        text: text(video),
                        html: video.outerHTML,
                    }));
                const statsFrom = (item) => {
                    const statsRoot = item.querySelector('.card-act');
                    const links = Array.from(statsRoot?.querySelectorAll('a, button') || []);
                    const labels = ['reposts', 'comments', 'likes'];
                    const stats = {};
                    links.slice(0, 3).forEach((link, index) => {
                        const label = labels[index] || `action_${index + 1}`;
                        stats[label] = {
                            text: text(link),
                            count: parseCount(text(link)),
                            href: absoluteUrl(link.getAttribute('href')),
                            action_type: attr(link, 'action-type'),
                            suda_data: attr(link, 'suda-data'),
                        };
                    });
                    return stats;
                };

                return Array.from(feed.querySelectorAll('div[action-type="feed_list_item"]'))
                    .map((item, index) => {
                        const mid = attr(item, 'mid');
                        const authorLink = item.querySelector('a.name[href], .info a.name[href], .avator a[href]');
                        const avatar = item.querySelector('.avator img, .face img');
                        const from = item.querySelector('p.from, .from');
                        const fromLinks = Array.from(from?.querySelectorAll('a[href]') || []);
                        const timeLink = fromLinks.find((link) => /weibo\\.com/.test(link.href)) || fromLinks[0];
                        const sourceLink = fromLinks.find((link) => link !== timeLink);
                        const contentFull = item.querySelector('p[node-type="feed_list_content_full"]');
                        const contentShort = item.querySelector('p[node-type="feed_list_content"]');
                        const content = contentFull || contentShort;
                        const postUrl = absoluteUrl(timeLink?.getAttribute('href'));
                        const authorUrl = absoluteUrl(authorLink?.getAttribute('href'));
                        const contentLinkRows = contentLinks(content);
                        const retweetedCard = item.querySelector('.card-comment');
                        const retweetedAuthorLink = retweetedCard?.querySelector('a.name[href], a[href*="weibo.com"]');
                        const retweetedContent = retweetedCard?.querySelector(
                            'p[node-type="feed_list_content_full"], p[node-type="feed_list_content"], .txt'
                        );

                        return {
                            id: mid || postIdFromUrl(postUrl),
                            uid: mid || postIdFromUrl(postUrl),
                            mid,
                            url: postUrl,
                            author_id: authorIdFromUrl(authorUrl),
                            author_name: text(authorLink),
                            author_url: authorUrl,
                            author_avatar: absoluteUrl(avatar?.getAttribute('src')),
                            published_at_text: text(timeLink),
                            source_app: text(sourceLink),
                            content_text: text(content),
                            content_html: html(content),
                            content_short_text: text(contentShort),
                            content_full_text: text(contentFull),
                            topics: contentLinkRows.filter((link) => link.text.startsWith('#') && link.text.endsWith('#')),
                            mentions: contentLinkRows.filter((link) => link.text.startsWith('@')),
                            links: contentLinkRows,
                            images: mediaImages(item),
                            videos: mediaVideos(item),
                            stats: statsFrom(item),
                            is_retweet: Boolean(retweetedCard),
                            retweeted: retweetedCard ? {
                                author_id: authorIdFromUrl(absoluteUrl(retweetedAuthorLink?.getAttribute('href'))),
                                author_name: text(retweetedAuthorLink),
                                author_url: absoluteUrl(retweetedAuthorLink?.getAttribute('href')),
                                content_text: text(retweetedContent),
                                content_html: html(retweetedContent),
                                links: contentLinks(retweetedContent),
                            } : null,
                            item_attrs: Array.from(item.attributes).reduce((acc, itemAttr) => {
                                acc[itemAttr.name] = itemAttr.value;
                                return acc;
                            }, {}),
                            item_html: item.outerHTML,
                            search_keyword: keyword,
                            search_page: pageNumber,
                            search_position: index + 1,
                            search_total_pages: totalPages,
                            search_url: searchUrl,
                            search_params: searchParams,
                        };
                    })
                    .filter((post) => post.id || post.url);
            }
            """,
            {
                "keyword": keyword,
                "pageNumber": page_number,
                "totalPages": total_pages,
                "searchUrl": search_url,
                "searchParams": dict(search_params),
            },
        )
        return [post for post in posts if isinstance(post, dict)] if isinstance(posts, list) else []

    def _keyword_search_url(
        self,
        keyword: str,
        page_number: int | None = None,
        *,
        extra_params: Mapping[str, str | int] | None = None,
    ) -> str:
        params: dict[str, str | int] = {"q": keyword, "nodup": 1}
        if extra_params:
            params.update(extra_params)
        if page_number is not None:
            params["page"] = page_number
        return f"{WEIBO_SEARCH_URL}?{urlencode(params)}"

    def _post_id_from_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        post_id = path.rsplit("/", 1)[-1]
        if not post_id:
            raise ValueError(f"Could not extract Weibo post id from URL: {url}")
        return post_id

    async def _scrape_one_post(
        self,
        page: Any,
        post_id: str,
        post_url: str,
        *,
        author_id: str,
        task_id: str | None,
        fetch_comments: bool,
        sleep_step: int,
    ) -> None:
        logger.info("Navigating to Weibo post {}", post_url)
        await page.goto(post_url)
        await page.wait_for_timeout(self.config.post_open_delay_ms)
        html = await page.evaluate("document.body?.innerHTML")
        self.store.save_post_raw(
            post_id,
            author_id,
            url=post_url,
            html=str(html) if html else None,
            task_id=task_id,
        )

        if not fetch_comments:
            return

        comments = await self._scrape_post_comments(
            page,
            post_id,
            author_id,
            sleep_step=sleep_step,
        )
        self.store.save_comments(comments, post_id=post_id, task_id=task_id)
        logger.info("Saved post {} with {} comments", post_id, len(comments))

    async def _scrape_post_comments(
        self,
        page: Any,
        post_id: str,
        author_id: str,
        *,
        sleep_step: int,
    ) -> list[dict[str, Any]]:
        all_comments: list[dict[str, Any]] = []
        max_id: str | None = None
        page_number = 1

        while True:
            if (
                self.config.max_comment_pages is not None
                and page_number > self.config.max_comment_pages
            ):
                logger.info("Reached max_comment_pages={}", self.config.max_comment_pages)
                break

            response = await self._fetch_comments(page, post_id, max_id=max_id)
            comments = response.get("data") if isinstance(response, dict) else None
            if not isinstance(comments, list) or not comments:
                break

            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                sub_comments = comment.get("comments")
                if isinstance(sub_comments, list):
                    all_comments.extend(item for item in sub_comments if isinstance(item, dict))
                all_comments.append(comment)

            logger.info("Found {} comments on page {}", len(comments), page_number)
            max_id = next_max_id(response)
            if max_id is None:
                break

            sleep_step = await weibo_dynamic_sleep(sleep_step)
            await maybe_cooldown(sleep_step)
            page_number += 1

        logger.info("Total comments collected for {}: {}", post_id, len(all_comments))
        return all_comments

    async def _fetch_author_profile(self, page: Any, author_id: str) -> dict[str, Any] | None:
        url = f"{PROFILE_INFO_ENDPOINT_PREFIX}{author_id}"
        logger.info("Fetching Weibo profile info {}", url)
        response = await browser_fetch_json(page, url)
        if not isinstance(response, dict):
            return None
        ok = value_to_int(response.get("ok"))
        if ok not in (None, 1):
            logger.warning("Weibo profile response for {} returned ok={}", author_id, ok)
            return None
        data = response.get("data")
        user = data.get("user") if isinstance(data, dict) else response.get("user")
        return user if isinstance(user, dict) else None

    async def _fetch_posts_page(
        self,
        page: Any,
        author_id: str,
        page_number: int,
    ) -> dict[str, Any]:
        url = (
            f"{WEIBO_BASE_URL}/ajax/statuses/mymblog"
            f"?uid={author_id}&page={page_number}&feature=0"
        )
        logger.info("Fetching Weibo posts page {}: {}", page_number, url)
        response = await browser_fetch_json(page, url)
        return response if isinstance(response, dict) else {}

    async def _fetch_comments(
        self,
        page: Any,
        post_id: str,
        *,
        max_id: str | None,
    ) -> dict[str, Any]:
        url = (
            f"{WEIBO_BASE_URL}/ajax/statuses/buildComments"
            f"?is_reload=1&id={post_id}&is_show_bulletin=2&is_mix=1"
        )
        if max_id is not None:
            url = f"{url}&max_id={max_id}"
        logger.info("Fetching Weibo comments: {}", url)
        response = await browser_fetch_json(page, url)
        return response if isinstance(response, dict) else {}

    async def _ensure_logged_in(self, page: Any) -> None:
        logger.info("Navigating to Weibo homepage for login check")
        await page.goto(WEIBO_BASE_URL)
        await page.wait_for_timeout(3000)
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

    def _validate_author_id(self, author_id: str) -> None:
        if not author_id.isnumeric():
            raise ValueError("Weibo author ID must be numeric.")


def extract_posts_from_response(
    json_value: dict[str, Any],
    author_id: str,
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    posts: list[tuple[str, str]] = []
    raw_items: list[dict[str, Any]] = []
    data = json_value.get("data")
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return posts, raw_items

    for item in items:
        if not isinstance(item, dict):
            continue
        post_id = value_to_str(item.get("id")) or value_to_str(item.get("mblogid")) or value_to_str(item.get("mid"))
        if post_id is None:
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        post_author_id = value_to_str(user.get("id")) or author_id
        url = f"https://weibo.com/{post_author_id}/{post_id}"
        posts.append((post_id, url))
        raw_items.append(item)

    return posts, raw_items
