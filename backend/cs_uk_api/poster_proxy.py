from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

from .cache import TtlCache
from .config import SETTINGS

_cache = TtlCache(default_ttl_s=SETTINGS.cache_poster_s)

_MAX_HOPS = 5


def _host_allowed(host: str) -> bool:
    for entry in SETTINGS.poster_allowed_hosts:
        if host == entry or host.endswith("." + entry):
            return True
    return False


def is_allowed(u: str) -> bool:
    p = urlparse(u)
    if p.scheme not in ("http", "https"):
        return False
    host = p.hostname
    if host is None:
        return False
    return _host_allowed(host)


def _ip_is_private(ip: str) -> bool:
    """True for private/loopback/link-local/reserved ranges AND unparseable input."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return not addr.is_global


async def _host_has_public_ip(host: str) -> bool:
    """True only if every resolved address of `host` is globally routable.

    A single private/loopback/link-local answer refuses the host, which
    also covers DNS rebinding on allowlisted domains.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror:
        return False
    for info in infos:
        sockaddr = info[4][0]
        if not isinstance(sockaddr, str) or _ip_is_private(sockaddr):
            return False
    return True


async def _validated_url(u: str) -> bool:
    """Host allowlist AND public-IP check for `u` (fail closed)."""
    host = urlparse(u).hostname
    if host is None or not _host_allowed(host):
        return False
    return await _host_has_public_ip(host)


async def _fetch_one(u: str, http: httpx.AsyncClient, hops: int) -> httpx.Response | None:
    if not await _validated_url(u):
        return None
    resp = await http.get(u, timeout=5.0, follow_redirects=False)
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location")
        if not location or hops >= _MAX_HOPS:
            return None
        return await _fetch_one(urljoin(u, location), http, hops + 1)
    return resp


async def fetch(u: str, http: httpx.AsyncClient) -> tuple[bytes, str] | None:
    if not await _validated_url(u):
        return None
    cached = _cache.get(u)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        resp = await _fetch_one(u, http, hops=0)
    except httpx.HTTPError:
        return None
    if resp is None or resp.status_code != 200:
        return None
    if len(resp.content) > SETTINGS.poster_size_cap_bytes:
        return None
    body = resp.content
    ctype = resp.headers.get("Content-Type", "image/jpeg")
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"
    _cache.set(u, (body, ctype))
    return body, ctype
