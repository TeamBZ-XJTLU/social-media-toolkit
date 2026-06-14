from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger


DOUYIN_BASE_URL = "https://www.douyin.com"

PromptFn = Callable[[str], None | Awaitable[None]]


def user_profile_url(sec_user_id: str) -> str:
    return f"{DOUYIN_BASE_URL}/user/{sec_user_id}"


def video_url(aweme_id: str) -> str:
    return f"{DOUYIN_BASE_URL}/video/{aweme_id}"


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def smart_sleep(min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


async def navigate(page: Any, url: str, wait_ms: int = 5000) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(wait_ms)


async def detect_login_popup(page: Any) -> bool:
    return bool(await page.evaluate("Boolean(document.querySelector('#login-panel-new'))"))


async def wait_for_login(
    page: Any,
    *,
    timeout_ms: int = 3000,
    prompt: PromptFn | None = None,
) -> bool:
    await page.wait_for_timeout(timeout_ms)
    if not await detect_login_popup(page):
        logger.info("Douyin: no login popup detected")
        return False

    logger.info("Douyin login popup detected. Please complete QR/login in the browser.")
    message = "Press Enter after Douyin login/captcha is complete..."
    if prompt is not None:
        await maybe_await(prompt(message))
    else:
        await asyncio.to_thread(input, message)

    await page.wait_for_timeout(2000)
    for _ in range(30):
        if not await detect_login_popup(page):
            logger.info("Douyin login popup closed")
            return True
        await page.wait_for_timeout(1000)

    return True


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
        raise RuntimeError(f"Douyin browser fetch failed for {url}: {result}")

    json_value = result.get("json")
    if isinstance(json_value, dict | list):
        return json_value
    return None


async def get_page_debug_info(page: Any) -> dict[str, str]:
    info = await page.evaluate("({ url: location.href, title: document.title })")
    return info if isinstance(info, dict) else {}


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


def csv_from_array_key(value: dict[str, Any], key: str, item_key: str) -> str | None:
    items = value.get(key)
    if not isinstance(items, list):
        return None
    parts = [
        str(item[item_key])
        for item in items
        if isinstance(item, dict) and item.get(item_key) not in (None, "")
    ]
    return ",".join(parts) if parts else None
