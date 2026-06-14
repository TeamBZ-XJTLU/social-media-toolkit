from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
import re
from typing import Annotated

import typer

from crawler.douyin import DouyinCrawler, DouyinCrawlerConfig
from crawler.rednote import RednoteCrawler, RednoteCrawlerConfig
from crawler.weibo import WeiboCrawler, WeiboCrawlerConfig
from crawler.weibo.storage import AUTHOR_COLLECTION, COMMENT_COLLECTION, POST_RAW_COLLECTION
from dashboard import create_app
from storage import DuckDBDatabase, JsonCollectionDirectoryDatabase, JsonValue


@dataclass(slots=True)
class CliState:
    db: Path | None = None
    user_data_dir: Path | None = None
    headless: bool = False
    id_only: bool = False
    task_id: str | None = None


class WeiboPostType(StrEnum):
    ALL = "all"
    HOT = "hot"
    ORIGINAL = "original"
    FOLLOWING = "following"
    VERIFIED = "verified"
    MEDIA = "media"
    VIEWPOINT = "viewpoint"


class WeiboContentFilter(StrEnum):
    ALL = "all"
    PICTURE = "picture"
    VIDEO = "video"
    MUSIC = "music"
    LINK = "link"


WEIBO_POST_TYPE_PARAMS: dict[WeiboPostType, dict[str, str | int]] = {
    WeiboPostType.ALL: {"typeall": 1},
    WeiboPostType.HOT: {"xsort": "hot"},
    WeiboPostType.ORIGINAL: {"scope": "ori"},
    WeiboPostType.FOLLOWING: {"atten": 1},
    WeiboPostType.VERIFIED: {"vip": 1},
    WeiboPostType.MEDIA: {"category": 4},
    WeiboPostType.VIEWPOINT: {"viewpoint": 1},
}
WEIBO_CONTENT_FILTER_PARAMS: dict[WeiboContentFilter, dict[str, str | int]] = {
    WeiboContentFilter.ALL: {"suball": 1},
    WeiboContentFilter.PICTURE: {"haspic": 1},
    WeiboContentFilter.VIDEO: {"hasvideo": 1},
    WeiboContentFilter.MUSIC: {"hasmusic": 1},
    WeiboContentFilter.LINK: {"haslink": 1},
}
WEIBO_TIME_BOUND_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<hour>\d{1,2}))?$")


app = typer.Typer(help="Crawl social media posts into a local DuckDB database.", no_args_is_help=True)
rednote_app = typer.Typer(help="Rednote/Xiaohongshu crawler.", no_args_is_help=True)
weibo_app = typer.Typer(help="Weibo crawler.", no_args_is_help=True)
douyin_app = typer.Typer(help="Douyin crawler.", no_args_is_help=True)
TaskIdOption = Annotated[
    str | None,
    typer.Option(help="Optional task id saved on crawled records. Generated automatically if omitted."),
]


def run_async(coro: object) -> None:
    asyncio.run(coro)


def cli_state(ctx: typer.Context) -> CliState:
    if not isinstance(ctx.obj, CliState):
        ctx.obj = CliState()
    return ctx.obj


def task_id_for(
    state: CliState,
    *,
    platform: str,
    scrape_type: str,
    condition: str,
    task_id: str | None = None,
) -> str:
    explicit_task_id = task_id or state.task_id
    if explicit_task_id:
        return explicit_task_id

    normalized_condition = normalize_task_condition(condition)
    timestamp = datetime.now().strftime("%Y%m%d%H%M")
    return f"{platform}-{scrape_type}-{normalized_condition}-{timestamp}"


def normalize_task_condition(condition: str) -> str:
    normalized = re.sub(r"\s+", "_", condition.strip())
    normalized = re.sub(r"[/\\:]+", "-", normalized)
    return normalized or "unknown"


def announce_task(task_id: str) -> None:
    typer.echo(f"Task ID: {task_id}")


def platform_db_path(state: CliState, platform: str) -> Path:
    if state.db is not None:
        return state.db
    return Path(f"data/{platform}.duckdb")


def save_task(
    state: CliState,
    *,
    platform: str,
    scrape_type: str,
    condition: str,
    task_id: str,
    metadata: dict[str, JsonValue] | None = None,
) -> None:
    db = DuckDBDatabase(platform_db_path(state, platform))
    try:
        db.save_task(
            task_id,
            platform=platform,
            scrape_type=scrape_type,
            condition=condition,
            metadata=metadata,
        )
    finally:
        db.close()


def build_weibo_search_params(
    *,
    post_types: list[WeiboPostType] | None = None,
    content_filters: list[WeiboContentFilter] | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
) -> dict[str, str | int]:
    params: dict[str, str | int] = {}
    params.update(weibo_post_type_params(post_types or []))
    params.update(weibo_content_filter_params(content_filters or []))

    if time_to is not None and time_from is None:
        raise typer.BadParameter("--time-to requires --time-from.", param_hint="--time-to")
    if time_from is not None:
        from_value, from_dt = parse_weibo_time_bound(time_from, "--time-from")
        to_value = ""
        if time_to is not None:
            to_value, to_dt = parse_weibo_time_bound(time_to, "--time-to")
            if to_dt <= from_dt:
                raise typer.BadParameter(
                    "--time-to must be later than --time-from.",
                    param_hint="--time-to",
                )
        params["timescope"] = f"custom:{from_value}:{to_value}"

    return params


def weibo_post_type_params(values: list[WeiboPostType]) -> dict[str, str | int]:
    if WeiboPostType.ALL in values:
        return dict(WEIBO_POST_TYPE_PARAMS[WeiboPostType.ALL])

    params: dict[str, str | int] = {}
    for value in values:
        params.update(WEIBO_POST_TYPE_PARAMS[value])
    return params


def weibo_content_filter_params(values: list[WeiboContentFilter]) -> dict[str, str | int]:
    if WeiboContentFilter.ALL in values:
        return dict(WEIBO_CONTENT_FILTER_PARAMS[WeiboContentFilter.ALL])

    params: dict[str, str | int] = {}
    for value in values:
        params.update(WEIBO_CONTENT_FILTER_PARAMS[value])
    return params


def parse_weibo_time_bound(value: str, param_hint: str) -> tuple[str, datetime]:
    normalized = value.strip()
    match = WEIBO_TIME_BOUND_RE.fullmatch(normalized)
    if match is None:
        raise typer.BadParameter(
            "Use YYYY-MM-DD or YYYY-MM-DD-HH, for example 2026-05-01 or 2026-05-01-13.",
            param_hint=param_hint,
        )

    hour_text = match.group("hour")
    hour = int(hour_text) if hour_text is not None else 0
    if hour > 23:
        raise typer.BadParameter("Hour must be between 0 and 23.", param_hint=param_hint)

    try:
        parsed = datetime.strptime(match.group("date"), "%Y-%m-%d").replace(hour=hour)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc

    return normalized, parsed


def rednote_config(state: CliState, max_no_height_increase: int) -> RednoteCrawlerConfig:
    return RednoteCrawlerConfig(
        db_path=platform_db_path(state, "rednote"),
        headless=state.headless,
        user_data_dir=state.user_data_dir or Path("data/rednote-browser-profile"),
        max_no_height_increase=max_no_height_increase,
    )


def weibo_config(
    state: CliState,
    *,
    max_empty_pages: int,
    no_comments: bool = False,
    max_pages: int | None = None,
    max_comment_pages: int | None = None,
) -> WeiboCrawlerConfig:
    return WeiboCrawlerConfig(
        db_path=platform_db_path(state, "weibo"),
        headless=state.headless,
        user_data_dir=state.user_data_dir or Path("data/weibo-browser-profile"),
        max_empty_pages=max_empty_pages,
        fetch_comments=not no_comments,
        max_pages=max_pages,
        max_comment_pages=max_comment_pages,
    )


def douyin_config(
    state: CliState,
    *,
    max_empty_pages: int,
    no_comments: bool = False,
    max_video_pages: int | None = None,
    max_comment_pages: int | None = None,
    max_reply_pages: int | None = None,
) -> DouyinCrawlerConfig:
    return DouyinCrawlerConfig(
        db_path=platform_db_path(state, "douyin"),
        headless=state.headless,
        user_data_dir=state.user_data_dir or Path("data/douyin-browser-profile"),
        max_empty_pages=max_empty_pages,
        max_video_pages=max_video_pages,
        max_comment_pages=max_comment_pages,
        max_reply_pages=max_reply_pages,
        collect_comments=not no_comments,
    )


@app.callback()
def root(
    ctx: typer.Context,
    db: Annotated[
        Path | None,
        typer.Option(help="Path to the DuckDB database file. Defaults to data/<platform>.duckdb."),
    ] = None,
    user_data_dir: Annotated[
        Path | None,
        typer.Option(help="Browser profile directory used to persist login state."),
    ] = None,
    headless: Annotated[
        bool,
        typer.Option("--headless", help="Run browser in headless mode. Not recommended for first login."),
    ] = False,
    id_only: Annotated[
        bool,
        typer.Option("--id-only", help="Only collect IDs instead of opening items and saving full content."),
    ] = False,
    task_id: Annotated[
        str | None,
        typer.Option(help="Optional label saved on crawled records."),
    ] = None,
) -> None:
    ctx.obj = CliState(
        db=db,
        user_data_dir=user_data_dir,
        headless=headless,
        id_only=id_only,
        task_id=task_id,
    )


@app.command("author")
def legacy_rednote_author(
    ctx: typer.Context,
    author_id: Annotated[
        str,
        typer.Argument(help="Author ID from https://www.xiaohongshu.com/user/profile/<author_id>."),
    ],
    max_no_height_increase: Annotated[
        int,
        typer.Option(help="Stop after this many scrolls without new page height."),
    ] = 5,
    from_local: Annotated[
        bool,
        typer.Option("--from-local", help="Skip discovery and process pending IDs/URLs from the local database."),
    ] = False,
    task_id: TaskIdOption = None,
) -> None:
    """Crawl Rednote posts from an author profile. Legacy shortcut."""
    state = cli_state(ctx)
    task_id = task_id_for(
        state,
        platform="rednote",
        scrape_type="author",
        condition=author_id,
        task_id=task_id,
    )
    announce_task(task_id)
    save_task(
        state,
        platform="rednote",
        scrape_type="author",
        condition=author_id,
        task_id=task_id,
    )

    async def run() -> None:
        async with RednoteCrawler(rednote_config(state, max_no_height_increase)) as crawler:
            await crawler.by_author(
                author_id,
                id_only=state.id_only,
                use_local_index=from_local,
                task_id=task_id,
            )

    run_async(run())


@app.command("dashboard")
def run_dashboard(
    ctx: typer.Context,
    host: Annotated[
        str,
        typer.Option(help="Host interface for the Flask dashboard."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(help="Port for the Flask dashboard."),
    ] = 5000,
    debug: Annotated[
        bool,
        typer.Option("--debug", help="Run Flask in debug mode."),
    ] = False,
) -> None:
    """Run the local Weibo DuckDB dashboard."""
    state = cli_state(ctx)
    flask_app = create_app(db_path=platform_db_path(state, "weibo"))
    flask_app.run(host=host, port=port, debug=debug)


@app.command("ui")
def run_textual_ui(ctx: typer.Context) -> None:
    """Open the Textual terminal UI for building and running crawler tasks."""
    from ui import run_ui

    state = cli_state(ctx)
    run_ui(
        default_db=state.db,
        default_user_data_dir=state.user_data_dir,
        default_headless=state.headless,
        default_id_only=state.id_only,
        default_task_id=state.task_id,
    )


@app.command("keyword")
def legacy_rednote_keyword(
    ctx: typer.Context,
    keyword: Annotated[str, typer.Argument(help="Keyword to search for.")],
    max_no_height_increase: Annotated[
        int,
        typer.Option(help="Stop after this many scrolls without new page height."),
    ] = 5,
    from_local: Annotated[
        bool,
        typer.Option("--from-local", help="Skip discovery and process pending IDs/URLs from the local database."),
    ] = False,
    task_id: TaskIdOption = None,
) -> None:
    """Crawl Rednote posts from search results. Legacy shortcut."""
    state = cli_state(ctx)
    task_id = task_id_for(
        state,
        platform="rednote",
        scrape_type="keyword",
        condition=keyword,
        task_id=task_id,
    )
    announce_task(task_id)
    save_task(
        state,
        platform="rednote",
        scrape_type="keyword",
        condition=keyword,
        task_id=task_id,
    )

    async def run() -> None:
        async with RednoteCrawler(rednote_config(state, max_no_height_increase)) as crawler:
            await crawler.by_keyword(
                keyword,
                id_only=state.id_only,
                use_local_index=from_local,
                task_id=task_id,
            )

    run_async(run())


@rednote_app.command("author")
def rednote_author(
    ctx: typer.Context,
    author_id: Annotated[
        str,
        typer.Argument(help="Author ID from https://www.xiaohongshu.com/user/profile/<author_id>."),
    ],
    max_no_height_increase: Annotated[
        int,
        typer.Option(help="Stop after this many scrolls without new page height."),
    ] = 5,
    from_local: Annotated[
        bool,
        typer.Option("--from-local", help="Skip discovery and process pending IDs/URLs from the local database."),
    ] = False,
    task_id: TaskIdOption = None,
) -> None:
    """Crawl Rednote author posts."""
    legacy_rednote_author(ctx, author_id, max_no_height_increase, from_local, task_id)


@rednote_app.command("keyword")
def rednote_keyword(
    ctx: typer.Context,
    keyword: Annotated[str, typer.Argument(help="Keyword to search for.")],
    max_no_height_increase: Annotated[
        int,
        typer.Option(help="Stop after this many scrolls without new page height."),
    ] = 5,
    from_local: Annotated[
        bool,
        typer.Option("--from-local", help="Skip discovery and process pending IDs/URLs from the local database."),
    ] = False,
    task_id: TaskIdOption = None,
) -> None:
    """Crawl Rednote keyword results."""
    legacy_rednote_keyword(ctx, keyword, max_no_height_increase, from_local, task_id)


@weibo_app.command("author")
def weibo_author(
    ctx: typer.Context,
    author_id: Annotated[str, typer.Argument(help="Numeric Weibo author ID.")],
    no_comments: Annotated[
        bool,
        typer.Option("--no-comments", help="Skip Weibo comment API pagination."),
    ] = False,
    max_pages: Annotated[
        int | None,
        typer.Option(help="Maximum Weibo post-list pages to fetch."),
    ] = None,
    max_comment_pages: Annotated[
        int | None,
        typer.Option(help="Maximum Weibo comment pages per post."),
    ] = None,
    max_empty_pages: Annotated[
        int,
        typer.Option(help="Stop after this many empty Weibo post pages."),
    ] = 3,
    from_local: Annotated[
        bool,
        typer.Option("--from-local", help="Skip discovery and process pending IDs/URLs from the local database."),
    ] = False,
    task_id: TaskIdOption = None,
) -> None:
    """Crawl Weibo posts by author ID."""
    state = cli_state(ctx)
    task_id = task_id_for(
        state,
        platform="weibo",
        scrape_type="author",
        condition=author_id,
        task_id=task_id,
    )
    announce_task(task_id)
    save_task(
        state,
        platform="weibo",
        scrape_type="author",
        condition=author_id,
        task_id=task_id,
    )

    async def run() -> None:
        async with WeiboCrawler(
            weibo_config(
                state,
                max_empty_pages=max_empty_pages,
                no_comments=no_comments,
                max_pages=max_pages,
                max_comment_pages=max_comment_pages,
            )
        ) as crawler:
            await crawler.by_author(
                author_id,
                id_only=state.id_only,
                fetch_comments=not no_comments,
                use_local_index=from_local,
                task_id=task_id,
            )

    run_async(run())


@weibo_app.command("author-info")
def weibo_author_info(
    ctx: typer.Context,
    author_ids: Annotated[
        list[str],
        typer.Argument(help="One or more numeric author IDs."),
    ],
) -> None:
    """Fetch Weibo author profile info."""
    state = cli_state(ctx)

    async def run() -> None:
        async with WeiboCrawler(weibo_config(state, max_empty_pages=3)) as crawler:
            await crawler.scrape_author_info(" ".join(author_ids))

    run_async(run())


@weibo_app.command("keyword")
def weibo_keyword(
    ctx: typer.Context,
    keyword: Annotated[str, typer.Argument(help="Keyword to search for.")],
    max_pages: Annotated[
        int | None,
        typer.Option(help="Maximum Weibo search result pages to fetch."),
    ] = None,
    post_type: Annotated[
        list[WeiboPostType] | None,
        typer.Option(
            "--post-type",
            case_sensitive=False,
            help="Repeatable Weibo post type filter: all, hot, original, following, verified, media, viewpoint. If all is set, other post types are ignored.",
        ),
    ] = None,
    content_filter: Annotated[
        list[WeiboContentFilter] | None,
        typer.Option(
            "--content-filter",
            case_sensitive=False,
            help="Repeatable Weibo content filter: all, picture, video, music, link. If all is set, other content filters are ignored.",
        ),
    ] = None,
    time_from: Annotated[
        str | None,
        typer.Option("--time-from", help="Weibo search start time: YYYY-MM-DD or YYYY-MM-DD-HH."),
    ] = None,
    time_to: Annotated[
        str | None,
        typer.Option("--time-to", help="Weibo search end time: YYYY-MM-DD or YYYY-MM-DD-HH. Must be later than --time-from."),
    ] = None,
    task_id: TaskIdOption = None,
) -> None:
    """Crawl Weibo posts from keyword search results."""
    state = cli_state(ctx)
    search_params = build_weibo_search_params(
        post_types=post_type,
        content_filters=content_filter,
        time_from=time_from,
        time_to=time_to,
    )
    task_id = task_id_for(
        state,
        platform="weibo",
        scrape_type="keyword",
        condition=keyword,
        task_id=task_id,
    )
    announce_task(task_id)
    save_task(
        state,
        platform="weibo",
        scrape_type="keyword",
        condition=keyword,
        task_id=task_id,
        metadata={
            "search_params": search_params,
            "post_types": [value.value for value in post_type or []],
            "content_filters": [value.value for value in content_filter or []],
            "time_from": time_from,
            "time_to": time_to,
        },
    )

    async def run() -> None:
        async with WeiboCrawler(weibo_config(state, max_empty_pages=3, max_pages=max_pages)) as crawler:
            await crawler.by_keyword(
                keyword,
                id_only=state.id_only,
                max_pages=max_pages,
                search_params=search_params,
                task_id=task_id,
            )

    run_async(run())


@weibo_app.command("split-db")
def weibo_split_db(
    ctx: typer.Context,
) -> None:
    """Write legacy weibo.json collections into split collection files."""
    state = cli_state(ctx)
    db = JsonCollectionDirectoryDatabase(state.db or Path("data/weibo.json"))
    counts = {
        "weibo_authors": len(db.list("weibo_authors")),
        "weibo_posts_raw": len(db.list("weibo_posts_raw")),
        "weibo_comments": len(db.list("weibo_comments")),
    }
    for collection in counts:
        records = db.list(collection)
        db.clear(collection)
        for record in records:
            db.create(collection, record, str(record["id"]))
    typer.echo(
        "Wrote split Weibo collections: "
        + ", ".join(f"{collection}={count}" for collection, count in counts.items())
    )


@weibo_app.command("import-json")
def weibo_import_json(
    ctx: typer.Context,
    source: Annotated[
        Path,
        typer.Option(help="Legacy Weibo JSON path or split-collection directory."),
    ] = Path("data/weibo.json"),
) -> None:
    """Import legacy Weibo JSON collections into DuckDB."""
    state = cli_state(ctx)
    source_db = JsonCollectionDirectoryDatabase(source)
    target_db = DuckDBDatabase(platform_db_path(state, "weibo"))
    collections = [AUTHOR_COLLECTION, POST_RAW_COLLECTION, COMMENT_COLLECTION]
    counts: dict[str, int] = {}

    for collection in collections:
        records = source_db.list(collection)
        counts[collection] = len(records)
        for record in records:
            record_id = str(record["id"])
            existing = target_db.read(collection, record_id)
            if existing is None:
                target_db.create(collection, record, record_id)
            else:
                target_db.replace(collection, record_id, record)

    typer.echo(
        "Imported Weibo JSON into DuckDB: "
        + ", ".join(f"{collection}={count}" for collection, count in counts.items())
    )


@douyin_app.command("author")
def douyin_author(
    ctx: typer.Context,
    sec_user_id: Annotated[str, typer.Argument(help="Douyin sec_user_id from /user/<sec_user_id>.")],
    no_comments: Annotated[
        bool,
        typer.Option("--no-comments", help="Skip Douyin comment API pagination."),
    ] = False,
    max_video_pages: Annotated[
        int | None,
        typer.Option(help="Maximum Douyin video-list pages to fetch."),
    ] = None,
    max_comment_pages: Annotated[
        int | None,
        typer.Option(help="Maximum Douyin top-level comment pages per video."),
    ] = None,
    max_reply_pages: Annotated[
        int | None,
        typer.Option(help="Maximum Douyin reply pages per top-level comment."),
    ] = None,
    max_empty_pages: Annotated[
        int,
        typer.Option(help="Stop after this many empty Douyin video pages."),
    ] = 5,
    from_local: Annotated[
        bool,
        typer.Option("--from-local", help="Skip discovery and process pending video IDs from the local database."),
    ] = False,
    task_id: TaskIdOption = None,
) -> None:
    """Crawl Douyin videos by sec_user_id."""
    state = cli_state(ctx)
    task_id = task_id_for(
        state,
        platform="douyin",
        scrape_type="author",
        condition=sec_user_id,
        task_id=task_id,
    )
    announce_task(task_id)
    save_task(
        state,
        platform="douyin",
        scrape_type="author",
        condition=sec_user_id,
        task_id=task_id,
    )

    async def run() -> None:
        async with DouyinCrawler(
            douyin_config(
                state,
                max_empty_pages=max_empty_pages,
                no_comments=no_comments,
                max_video_pages=max_video_pages,
                max_comment_pages=max_comment_pages,
                max_reply_pages=max_reply_pages,
            )
        ) as crawler:
            await crawler.by_author(
                sec_user_id,
                id_only=state.id_only,
                collect_comments=not no_comments,
                use_local_index=from_local,
                task_id=task_id,
            )

    run_async(run())


@douyin_app.command("author-info")
def douyin_author_info(
    ctx: typer.Context,
    sec_user_id: Annotated[str, typer.Argument(help="Douyin sec_user_id from /user/<sec_user_id>.")],
) -> None:
    """Fetch Douyin author profile info."""
    state = cli_state(ctx)

    async def run() -> None:
        async with DouyinCrawler(douyin_config(state, max_empty_pages=5)) as crawler:
            await crawler.scrape_author_info(sec_user_id)

    run_async(run())


app.add_typer(rednote_app, name="rednote")
app.add_typer(rednote_app, name="xhs")
app.add_typer(weibo_app, name="weibo")
app.add_typer(weibo_app, name="wb")
app.add_typer(douyin_app, name="douyin")
app.add_typer(douyin_app, name="dy")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
