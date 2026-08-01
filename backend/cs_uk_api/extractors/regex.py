"""Generic regex-based stream extractor.

Handles the common patterns seen in Cloudstream-UK-style players:
- PlayerJS ``file: "URL"``
- PlayerJS ``sources: [{src: "URL", type: "..."}]``
- HTML ``<video src="URL">`` / ``<source src="URL">``

The extractor prefers the most specific match in this order: ``file:``,
then ``sources:``, then ``<video/src>`` and ``<source>``.
"""
from __future__ import annotations

import re

from .base import BaseExtractor, ExtractResult

# file:"..."  /  file: "..."  /  file: '...' (PlayerJS)
_FILE_RE = re.compile(
    r"""file\s*:\s*["']([^"']+\.(?:mp4|m3u8|m4v|webm|mpd))["']""",
    re.IGNORECASE,
)

# sources: [{src: "...", type: "..."}] (PlayerJS sources array)
_SOURCES_RE = re.compile(
    r"""sources\s*:\s*\[\s*\{\s*src\s*:\s*["']([^"']+\.(?:mp4|m3u8|m4v|webm|mpd))["']""",
    re.IGNORECASE,
)

# <video src="URL">  /  <source src="URL">
_TAG_RE = re.compile(
    r"""<\s*(?:video|source)[^>]*?src\s*=\s*["']([^"']+\.(?:mp4|m3u8|m4v|webm|mpd))["']""",
    re.IGNORECASE,
)

_M3U8_RE = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)


def _classify(url: str) -> str:
    if _M3U8_RE.search(url):
        return "m3u8"
    return "mp4"


class RegexExtractor(BaseExtractor):
    """Synchronous HTML scanner — does not issue HTTP requests."""

    name = "regex"

    def extract(self, html: str) -> ExtractResult | None:
        for rx in (_FILE_RE, _SOURCES_RE, _TAG_RE):
            m = rx.search(html)
            if m:
                url = m.group(1)
                return ExtractResult(url=url, type=_classify(url))  # type: ignore[arg-type]
        return None