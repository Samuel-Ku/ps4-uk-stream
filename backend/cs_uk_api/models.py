from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MediaType = Literal["movie", "series"]
StreamType = Literal["mp4", "m3u8", "hls", "dash"]


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


class StreamResponse(BaseModel):
    url: str
    type: StreamType = "mp4"
    headers: dict[str, str] = Field(default_factory=dict)


class ProviderInfo(BaseModel):
    id: str
    name: str
    types: list[MediaType]


class ErrorResponse(BaseModel):
    error: str
    message: str
