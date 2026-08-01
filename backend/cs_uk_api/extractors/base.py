"""Shared stream-extraction layer (issue #8).

Providers delegate to one of these extractors when their player page
returns HTML or JSON that needs to be parsed into a direct media URL.
All extractors return an :class:`ExtractResult` so providers do not have
to handle the typing differences between mp4 / m3u8 / hls themselves.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import StreamType


class ExtractResult(BaseModel):
    """Resolved stream ready to be returned from /api/stream."""

    url: str
    type: StreamType = "mp4"
    headers: dict[str, str] = Field(default_factory=dict)


class BaseExtractor:
    """Marker base class for stream extractors.

    Subclasses implement ``extract`` with whatever signature fits their
    input shape (HTML only, HTML + HTTP client, or URL + HTTP client).
    The shared contract: ``extract(...) -> ExtractResult | None`` (sync
    extractors) or ``await extract(...) -> ExtractResult`` (async
    extractors that follow links).
    """

    name: str = "base"