from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .cache import TtlCache
from .config import SETTINGS

_cache = TtlCache(default_ttl_s=SETTINGS.cache_poster_s)


def is_allowed(u: str) -> bool:
    p = urlparse(u)
    return p.scheme in ("http", "https") and bool(p.netloc)


async def fetch(u: str, http: httpx.AsyncClient) -> tuple[bytes, str] | None:
    if not is_allowed(u):
        return None
    cached = _cache.get(u)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        resp = await http.get(u, timeout=5.0, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    if len(resp.content) > SETTINGS.poster_size_cap_bytes:
        return None
    body = resp.content
    ctype = resp.headers.get("Content-Type", "image/jpeg")
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"
    _cache.set(u, (body, ctype))
    return body, ctype
