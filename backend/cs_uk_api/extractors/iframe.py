"""Iframe-chain extractor.

Given an initial HTML (or URL), walk down iframe chains up to
``max_depth``. If the final page contains no iframe, treat its URL as the
direct media URL and classify it by extension.
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from .base import BaseExtractor, ExtractResult

_IFRAME_SRC_RE = re.compile(
    r"""<\s*iframe[^>]*?src\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_M3U8_RE = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)


def _classify(url: str) -> str:
    if _M3U8_RE.search(url):
        return "m3u8"
    if url.endswith(".mp4") or ".mp4?" in url:
        return "mp4"
    if url.endswith(".mpd") or ".mpd?" in url:
        return "dash"
    return "hls"  # unknown — most likely an HLS player landing page


class IframeExtractor(BaseExtractor):
    """Walk iframe chains, return final media URL."""

    name = "iframe"

    def __init__(self, max_depth: int = 5, headers: dict[str, str] | None = None) -> None:
        self.max_depth = max_depth
        self.headers = headers or {}

    async def extract(self, html: str, http: httpx.AsyncClient) -> ExtractResult:
        """Resolve an initial HTML page to a direct media URL.

        The first call parses ``html`` for an ``<iframe src=...>`` and,
        if present, follows it (recursively up to ``max_depth`` times).
        If no iframe is found in the first page, returns the page URL
        itself (treated as a direct media URL).
        """
        m = _IFRAME_SRC_RE.search(html)
        if not m:
            # Caller passed raw HTML but no iframe in it; let the caller
            # provide a real initial URL via the alternate entry point.
            return ExtractResult(url="", type="hls")
        return await self._resolve_url(m.group(1), http, depth=self.max_depth)

    async def _resolve_url(self, url: str, http: httpx.AsyncClient, *, depth: int) -> ExtractResult:
        if depth <= 0:
            return ExtractResult(url=url, type=_classify(url))  # type: ignore[arg-type]
        try:
            resp = await http.get(url, headers=self.headers)
        except httpx.HTTPError:
            return ExtractResult(url=url, type=_classify(url))  # type: ignore[arg-type]
        if resp.status_code != 200:
            return ExtractResult(url=url, type=_classify(url))  # type: ignore[arg-type]
        # Search the response for another iframe.
        soup = BeautifulSoup(resp.text, "lxml")
        iframe = soup.select_one("iframe")
        if iframe is None or not iframe.get("src"):
            # Final page: treat its URL as the direct media URL.
            return ExtractResult(url=url, type=_classify(url))  # type: ignore[arg-type]
        next_url = str(iframe["src"])
        if next_url.startswith("/"):
            # Iframe src is relative to the page we just fetched.
            from urllib.parse import urljoin

            next_url = urljoin(url, next_url)
        return await self._resolve_url(next_url, http, depth=depth - 1)