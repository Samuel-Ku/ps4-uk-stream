from __future__ import annotations

import abc
from typing import Literal

import httpx

from ..models import (
    ContentResponse,
    SearchResult,
    Section,
    StreamResponse,
)

MediaTypeStr = Literal["movie", "series"]


class ProviderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BaseProvider(abc.ABC):
    id: str
    name: str
    types: tuple[MediaTypeStr, ...]
    # Subclasses set this to a non-empty tuple to opt into /api/sections
    # and /api/browse. Default: no section browsing.
    sections: tuple[Section, ...] = ()

    def has_section(self, section_id: str) -> bool:
        return any(s.id == section_id for s in self.sections)

    @abc.abstractmethod
    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]: ...

    @abc.abstractmethod
    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse: ...

    @abc.abstractmethod
    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse: ...

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        """Return one page of results for a section.

        Default implementation: raises NotImplementedError. Providers that
        declare `sections` must override this.
        """
        raise NotImplementedError(f"{self.id} does not support browse")

    async def episode_translations(
        self, content_id: str, http: httpx.AsyncClient
    ) -> list[str] | None:
        """Return allowed translation ids for an episode, or None.

        Providers that support per-episode translations (translation_level
        == "episode") override this. Returning None means "not applicable"
        — the caller falls back to content-level translations. Returning a
        list (possibly empty) means the caller should validate against it.
        """
        return None
