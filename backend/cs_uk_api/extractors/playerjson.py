"""PlayerJson extractor.

Cloudstream's "PlayerJson" pattern: a CDN player endpoint returns a
small JSON object with the resolved media URL, optional headers
(Referer / Cookie), and a type field. This extractor does the HTTP GET
and validates the response.
"""
from __future__ import annotations

import json

import httpx

from .base import BaseExtractor, ExtractResult


class PlayerJsonExtractor(BaseExtractor):
    name = "playerjson"

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}

    async def extract(self, *, initial_url: str, http: httpx.AsyncClient) -> ExtractResult:
        try:
            resp = await http.get(initial_url, headers=self.headers)
        except httpx.HTTPError as e:
            raise RuntimeError(f"playerjson unreachable: {e}") from e
        if resp.status_code != 200:
            raise RuntimeError(f"playerjson status {resp.status_code}")
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"playerjson not json: {e}") from e
        url = data.get("url")
        if not url:
            raise RuntimeError("playerjson missing url field")
        type_ = data.get("type") or ("m3u8" if ".m3u8" in url else "mp4")
        headers = data.get("headers") or {}
        if not isinstance(headers, dict):
            headers = {}
        return ExtractResult(url=url, type=type_, headers=headers)  # type: ignore[arg-type]