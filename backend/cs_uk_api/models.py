from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MediaType = Literal["movie", "series", "anime", "cartoon", "dorama"]
StreamType = Literal["mp4", "m3u8", "hls", "dash"]
TranslationLevel = Literal["content", "episode"]


class SearchResult(BaseModel):
    id: str
    provider: str
    type: MediaType
    title: str
    year: int | None = None
    poster: str | None = None
    url: str


class SearchResponse(BaseModel):
    query: str = Field(min_length=1, max_length=80)
    results: list[SearchResult]


class Translation(BaseModel):
    id: str
    label: str


class Episode(BaseModel):
    number: int
    id: str
    title: str
    # Per-episode translations (v2 spec). When None, fall back to the
    # content-level translations from the parent ContentResponse.
    translations: list[Translation] | None = None


class Season(BaseModel):
    number: int
    episodes: list[Episode]


class ContentResponse(BaseModel):
    id: str
    type: MediaType
    title: str
    year: int | None = None
    description: str = ""
    poster: str | None = None
    translations: list[Translation] = Field(min_length=1)
    seasons: list[Season] | None = None
    translations_level: TranslationLevel = "content"
    country: str | None = None


class StreamResponse(BaseModel):
    url: str
    type: StreamType = "mp4"
    headers: dict[str, str] = Field(default_factory=dict)


HealthStatus = Literal["ok", "degraded", "down"]


class ProviderInfo(BaseModel):
    id: str
    name: str
    types: list[MediaType]
    status: HealthStatus = "ok"
    last_error_at: str | None = None


class Section(BaseModel):
    id: str
    title: str
    type: MediaType


class ProviderSections(BaseModel):
    provider: str
    name: str
    sections: list[Section]


class BrowseResponse(BaseModel):
    provider: str
    section: str
    page: int = Field(ge=1)
    has_next: bool
    results: list[SearchResult]


class ErrorResponse(BaseModel):
    error: str
    message: str
