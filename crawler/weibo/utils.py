from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger


WEIBO_BASE_URL = "https://weibo.com"
WEIBO_PROFILE_URL = f"{WEIBO_BASE_URL}/u"
PROFILE_INFO_ENDPOINT_PREFIX = f"{WEIBO_BASE_URL}/ajax/profile/info?uid="

PromptFn = Callable[[str], None | Awaitable[None]]


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def smart_sleep(min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


async def weibo_dynamic_sleep(step: int) -> int:
    next_step = step + 1
    if next_step <= 10:
        await smart_sleep(0.8, 1.8)
    elif next_step <= 30:
        await smart_sleep(1.5, 3.5)
    else:
        await smart_sleep(3.0, 6.0)
    return next_step


async def maybe_cooldown(step: int) -> None:
    if step > 0 and step % 50 == 0:
        logger.info("Cooling down after {} Weibo requests", step)
        await smart_sleep(20.0, 40.0)


async def is_logged_in(page: Any) -> bool:
    return bool(
        await page.evaluate(
            """
            (() => {
                const containers = document.querySelectorAll(
                    '.woo-box-alignCenter.woo-box-justifyCenter'
                );
                for (const container of containers) {
                    const links = container.querySelectorAll('a[class^="_alink_"]');
                    for (const link of links) {
                        const href = link.getAttribute('href');
                        if (href && /^\\/u\\/\\d+$/.test(href)) return true;
                    }
                }

                return Boolean(
                    document.querySelector('a[href^="/u/"], a[href^="https://weibo.com/u/"]')
                );
            })()
            """
        )
    )


async def check_and_wait_for_user_action(
    page: Any,
    *,
    timeout_ms: int = 2000,
    prompt: PromptFn | None = None,
) -> bool:
    await page.wait_for_timeout(timeout_ms)
    if await is_logged_in(page):
        logger.info("Weibo: user is logged in")
        return False

    logger.info("Weibo: user is not logged in. Please log in in the browser window.")
    message = "Press Enter after Weibo login/captcha is complete..."
    if prompt is not None:
        await maybe_await(prompt(message))
    else:
        await asyncio.to_thread(input, message)
    return True


async def wait_until_logged_in(
    page: Any,
    *,
    timeout_ms: int = 2000,
    prompt: PromptFn | None = None,
) -> None:
    while not await is_logged_in(page):
        await check_and_wait_for_user_action(
            page,
            timeout_ms=timeout_ms,
            prompt=prompt,
        )


async def browser_fetch_json(page: Any, url: str) -> dict[str, Any] | list[Any] | None:
    result = await page.evaluate(
        """
        async (url) => {
            try {
                const response = await fetch(url, {
                    method: 'GET',
                    credentials: 'include',
                    headers: { 'accept': 'application/json, text/plain, */*' }
                });
                const text = await response.text();
                let json = null;
                try {
                    json = text ? JSON.parse(text) : null;
                } catch (error) {
                    return {
                        fetch_ok: false,
                        status: response.status,
                        error: `JSON parse failed: ${String(error)}`,
                        text: text.slice(0, 500)
                    };
                }
                return {
                    fetch_ok: response.ok,
                    status: response.status,
                    json
                };
            } catch (error) {
                return {
                    fetch_ok: false,
                    status: null,
                    error: String(error)
                };
            }
        }
        """,
        url,
    )

    if not isinstance(result, dict) or not result.get("fetch_ok"):
        raise RuntimeError(f"Weibo browser fetch failed for {url}: {result}")

    json_value = result.get("json")
    if isinstance(json_value, dict | list):
        return json_value
    return None


def parse_weibo_ids(author_ids: str) -> list[str]:
    normalized = author_ids.replace(",", " ").replace(";", " ")
    return [item.strip() for item in normalized.split() if item.strip()]


def value_to_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return None


def value_to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.replace(",", ""))
        except ValueError:
            return None
    return None


def value_to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes"}:
            return True
        if lowered in {"0", "false", "no"}:
            return False
    return None


def next_max_id(json_value: dict[str, Any]) -> str | None:
    value = json_value.get("max_id")
    if value in (None, "", 0, "0"):
        return None
    return str(value)
