from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger


BASE_URL = "https://www.xiaohongshu.com"
USER_PROFILE_URL = f"{BASE_URL}/user/profile"

PromptFn = Callable[[str], None | Awaitable[None]]


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def smart_sleep(min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


async def check_and_wait_for_user_action(
    page: Any,
    *,
    timeout_ms: int = 500,
    prompt: PromptFn | None = None,
) -> bool:
    await page.wait_for_timeout(timeout_ms)

    login_required = await page.evaluate("Boolean(document.querySelector('#login-btn'))")
    captcha_required = await page.evaluate("Boolean(document.querySelector('#red-captcha'))")

    if not login_required and not captcha_required:
        return False

    if login_required:
        logger.info("Login button detected. Please log in.")
    if captcha_required:
        logger.info("Captcha detected. Please complete the captcha.")

    message = "Press Enter after login/captcha is complete..."
    if prompt is not None:
        await maybe_await(prompt(message))
    else:
        await asyncio.to_thread(input, message)

    return True


async def is_logged_in(page: Any) -> bool:
    return bool(await page.evaluate("Boolean(document.querySelector('.user.side-bar-component'))"))


async def wait_until_logged_in(
    page: Any,
    *,
    timeout_ms: int = 500,
    prompt: PromptFn | None = None,
) -> None:
    while not await is_logged_in(page):
        await check_and_wait_for_user_action(
            page,
            timeout_ms=timeout_ms,
            prompt=prompt,
        )


async def collect_author_post_ids(page: Any) -> set[tuple[str, str]]:
    feed_exists = await page.evaluate("Boolean(document.querySelector('#userPostedFeeds'))")
    if not feed_exists:
        return set()

    return await _collect_post_ids(
        page,
        "Array.from(document.querySelectorAll('#userPostedFeeds section.note-item'))",
        skip_query_wrappers=False,
    )


async def collect_search_post_ids(page: Any) -> set[tuple[str, str]]:
    note_exists = await page.evaluate("Boolean(document.querySelector('section.note-item'))")
    if not note_exists:
        return set()

    return await _collect_post_ids(
        page,
        "Array.from(document.querySelectorAll('section.note-item'))",
        skip_query_wrappers=True,
    )


async def _collect_post_ids(
    page: Any,
    items_expression: str,
    *,
    skip_query_wrappers: bool,
) -> set[tuple[str, str]]:
    skip_query = "if (noteItem.querySelector('.query-note-wrapper')) return null;" if skip_query_wrappers else ""
    posts = await page.evaluate(
        f"""
        (() => {{
            return {items_expression}.map((noteItem) => {{
                try {{
                    {skip_query}
                    const anchors = noteItem.querySelectorAll('a');
                    if (anchors.length < 2) return null;

                    const hiddenHref = anchors[0].getAttribute('href');
                    const visibleHref = anchors[1].getAttribute('href');
                    if (!hiddenHref || !hiddenHref.startsWith('/explore/')) return null;

                    const match = hiddenHref.match(/^\\/explore\\/([A-Za-z0-9_-]+)/);
                    if (!match) return null;

                    return {{ postId: match[1], url: visibleHref || hiddenHref }};
                }} catch (_) {{
                    return null;
                }}
            }}).filter(Boolean);
        }})()
        """
    )

    collected: set[tuple[str, str]] = set()
    for post in posts or []:
        post_id = post.get("postId")
        url = post.get("url")
        if post_id and url:
            collected.add((str(post_id), str(url)))
    return collected


async def get_author_feeds_height(page: Any) -> int:
    value = await page.evaluate("parseInt(document.querySelector('#userPostedFeeds')?.style.height || '0')")
    return int(value or 0)


async def get_document_height(page: Any) -> int:
    value = await page.evaluate("document.documentElement.scrollHeight")
    return int(value or 0)


async def wait_for_feeds_loading_indicator(page: Any) -> None:
    for _ in range(10):
        is_loading = await page.evaluate(
            "Boolean(document.querySelector('.feeds-loading-indicator, .loading, [class*=\"loading\"]'))"
        )
        if not is_loading:
            return
        await page.wait_for_timeout(100)


async def get_comments_container(page: Any) -> bool:
    if await page.evaluate("Boolean(document.querySelector('.comments-container'))"):
        return True

    for _ in range(6):
        await page.evaluate("window.scrollBy(0, 1500)")
        await page.wait_for_timeout(400)
        if await page.evaluate("Boolean(document.querySelector('.comments-container'))"):
            return True

    return False


async def scroll_to_load_all_comments(page: Any) -> None:
    total_parent_comments = 0
    no_change_count = 0

    while True:
        has_end = await page.evaluate(
            "document.querySelectorAll('.comments-container .end-container').length > 0"
        )
        if has_end:
            logger.info("End of comments reached")
            return

        current_count = int(
            await page.evaluate(
                "document.querySelectorAll('.comments-container .parent-comment').length"
            )
            or 0
        )
        if current_count > total_parent_comments:
            total_parent_comments = current_count
            no_change_count = 0
            logger.info("Found {} parent comments", current_count)
        else:
            no_change_count += 1

        if no_change_count >= 5:
            logger.info("No new comments loaded after 5 attempts")
            return

        await move_mouse_to_element_center(page, ".interaction-container")
        await page.mouse.wheel(0, 500)
        await page.wait_for_timeout(500)


async def expand_all_sub_comments(page: Any) -> None:
    parent_count = int(
        await page.evaluate(
            "document.querySelectorAll('.comments-container .parent-comment').length"
        )
        or 0
    )

    for index in range(parent_count):
        while True:
            has_show_more = await page.evaluate(
                f"""
                (() => {{
                    const parentComments = document.querySelectorAll('.comments-container .parent-comment');
                    const parent = parentComments[{index}];
                    return Boolean(parent?.querySelector('.reply-container .show-more'));
                }})()
                """
            )
            if not has_show_more:
                break

            await page.evaluate(
                f"""
                (() => {{
                    const parentComments = document.querySelectorAll('.comments-container .parent-comment');
                    const parent = parentComments[{index}];
                    const showMore = parent?.querySelector('.reply-container .show-more');
                    if (showMore) {{
                        showMore.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                        showMore.click();
                    }}
                }})()
                """
            )
            await page.wait_for_timeout(400)


async def move_mouse_to_element_center(page: Any, selector: str) -> bool:
    position = await page.evaluate(
        """
        (selector) => {
            const element = document.querySelector(selector);
            if (!element) return null;
            const rect = element.getBoundingClientRect();
            return {
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2
            };
        }
        """,
        selector,
    )
    if not position:
        return False

    await page.mouse.move(float(position["x"]), float(position["y"]))
    return True


def normalize_post_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if not url.startswith("/"):
        url = f"/{url}"
    return f"{BASE_URL}{url}"
