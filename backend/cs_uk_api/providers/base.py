from __future__ import annotations

import abc
from typing import Literal

import httpx

from ..models import ContentResponse, SearchResult, StreamResponse

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
