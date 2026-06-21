from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Generic, Self, TypeVar

import cloakbrowser

from storage import DuckDBDatabase


PromptFn = Callable[[str], None | Awaitable[None]]


@dataclass(slots=True)
class BrowserCrawlerConfig:
    db_path: str | Path
    media_download_dir: str | Path | None = None
    download_media: bool = True
    headless: bool = False
    user_data_dir: str | Path | None = None
    storage_state: str | Path | None = None
    locale: str | None = "zh-CN"
    timezone: str | None = "Asia/Shanghai"
    humanize: bool = True
    viewport: dict[str, int] | None = None


ConfigT = TypeVar("ConfigT", bound=BrowserCrawlerConfig)
StoreT = TypeVar("StoreT")


class BrowserCrawler(Generic[ConfigT, StoreT], ABC):
    """Shared cloakbrowser + DuckDB lifecycle for platform crawlers."""

    db_cls: ClassVar[type[Any]] = DuckDBDatabase
    store_cls: ClassVar[type[Any]]

    def __init__(
        self,
        config: ConfigT,
        *,
        prompt: PromptFn | None = None,
    ) -> None:
        if not hasattr(self, "store_cls"):
            raise TypeError(f"{type(self).__name__} must define store_cls.")

        self.config = config
        if config.media_download_dir is not None:
            Path(config.media_download_dir).mkdir(parents=True, exist_ok=True)
        self.db = self.db_cls(config.db_path)
        self.store: StoreT = self.store_cls(self.db)
        self.prompt = prompt
        self._context: Any | None = None
        self._owns_context = False

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._context is not None:
            return

        launch_kwargs = self._launch_kwargs()
        if self.config.user_data_dir is not None:
            self._context = await cloakbrowser.launch_persistent_context_async(
                self.config.user_data_dir,
                **launch_kwargs,
            )
        else:
            if self.config.storage_state is not None:
                launch_kwargs["storage_state"] = str(self.config.storage_state)
            self._context = await cloakbrowser.launch_context_async(**launch_kwargs)

        self._owns_context = True

    async def close(self) -> None:
        if self._context is not None and self._owns_context:
            await self._context.close()
        self._context = None
        self._owns_context = False

    async def new_page(self) -> Any:
        await self.start()
        return await self.context.new_page()

    async def _new_page(self) -> Any:
        return await self.new_page()

    @abstractmethod
    async def by_author(self, author_id: str, **kwargs: Any) -> None:
        """Crawl content for one platform-specific author identifier."""

    @abstractmethod
    async def by_keyword(self, keyword: str, **kwargs: Any) -> None:
        """Crawl content from one platform-specific keyword search."""

    @abstractmethod
    async def download_from_url(self, url: str, **kwargs: Any) -> None:
        """Download one platform-specific content item from its public URL."""

    @property
    def context(self) -> Any:
        if self._context is None:
            raise RuntimeError("Crawler has not been started.")
        return self._context

    def _launch_kwargs(self) -> dict[str, Any]:
        launch_kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "locale": self.config.locale,
            "timezone": self.config.timezone,
            "humanize": self.config.humanize,
        }
        if self.config.viewport is not None:
            launch_kwargs["viewport"] = self.config.viewport
        return launch_kwargs
