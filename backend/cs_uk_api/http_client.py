from __future__ import annotations

import httpx

from .config import SETTINGS

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(SETTINGS.upstream_timeout_s),
            headers={"User-Agent": "cs-uk-api/0.1 (+https://github.com/)"},
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
