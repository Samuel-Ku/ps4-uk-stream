from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
import time
from urllib.parse import urljoin, urlparse

import httpx

from . import config as _config
from .cache import TtlCache
from .versioned_store import atomic_write_bytes

log = logging.getLogger("cs_uk_api.poster")

#: The poster cache store (ADR-0003), constructed from the ``SETTINGS``
#: snapshot (Arch T12: stores read settings through the one ``config``
#: binding — the poster policy reads below are lazy at call time).
_cache = TtlCache(default_ttl_s=_config.SETTINGS.cache_poster_s)

_MAX_HOPS = 5

#: Hosts that answer plain HTTP clients with Cloudflare's managed
#: challenge (403) and are therefore fetched through the headless
#: browser session instead (see ``_browser_fetch_bytes``).
_BROWSER_FALLBACK_HOSTS = frozenset({"uakino.best"})

_EXT_FOR_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_TYPE_FOR_EXT = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _disk_basename(u: str) -> str:
    return hashlib.sha256(u.encode()).hexdigest()[:32]


def _disk_get(directory: str, u: str, ttl_s: int) -> tuple[bytes, str] | None:
    """Serve from the disk layer when a fresh entry exists. Never raises."""
    stem = os.path.join(directory, _disk_basename(u))
    for ext, ctype in _TYPE_FOR_EXT.items():
        path = stem + ext
        try:
            st = os.stat(path)
        except OSError:
            continue
        if time.time() - st.st_mtime > ttl_s:
            return None  # expired — force re-fetch (file overwritten on success)
        try:
            with open(path, "rb") as f:
                return f.read(), ctype
        except OSError:
            log.warning("poster disk read failed: %s", path)
            return None
    return None


def _disk_put(directory: str, u: str, body: bytes, ctype: str) -> None:
    """Best-effort atomic disk write through the shared primitive; never
    raises.

    Opaque image bytes stay OUTSIDE any version envelope (ADR-0003's
    confirmed exception): the final name is content-addressed
    (``sha256(url)[:32]`` + type extension) and ``atomic_write_bytes``
    owns the temp+rename body — ``mkstemp`` yields process-unique
    ``O_EXCL`` temps in the target directory, so concurrent
    ``--workers`` writes cannot interleave, and a failed write unlinks
    its own tmp instead of leaking a stale file.
    """
    ext = _EXT_FOR_TYPE.get(ctype, ".jpg")
    final = os.path.join(directory, _disk_basename(u) + ext)
    # Never raises; a failure is logged by the primitive and serving
    # falls back to upstream (the disk layer is best-effort).
    atomic_write_bytes(final, body)


def _host_allowed(host: str) -> bool:
    for entry in _config.SETTINGS.poster_allowed_hosts:
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


async def _browser_fetch_bytes(u: str) -> tuple[bytes, str] | None:
    """Fetch ``u`` through the headless-browser session (aikino.best).

    uakino.best sits behind Cloudflare's managed challenge: every plain
    HTTP client gets 403 for anything past the initial document, while a
    same-origin ``fetch()`` executed inside a loaded page succeeds
    (``uakino_browser``). The poster proxy therefore falls back to that
    session for hosts that need it. Never raises.
    """
    host = urlparse(u).hostname
    if host not in _BROWSER_FALLBACK_HOSTS:
        return None
    try:
        from .uakino_browser import get_session
    except ImportError:
        return None
    path = urlparse(u).path + (f"?{urlparse(u).query}" if urlparse(u).query else "")
    try:
        status, body, ctype = await get_session().fetch_binary(path)
    except Exception:  # noqa: BLE001 — SessionError and any browser failure
        log.warning("browser poster fetch failed: %s", host)
        return None
    if status != 200 or not body:
        return None
    if len(body) > _config.SETTINGS.poster_size_cap_bytes:
        return None
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"
    return body, ctype


async def fetch(u: str, http: httpx.AsyncClient) -> tuple[bytes, str] | None:
    if not await _validated_url(u):
        return None
    cached = _cache.get(u)
    if cached is not None:
        return cached  # type: ignore[return-value]
    if _config.SETTINGS.poster_cache_dir is not None:
        disk = await asyncio.to_thread(
            _disk_get, _config.SETTINGS.poster_cache_dir, u, _config.SETTINGS.poster_disk_ttl_s
        )
        if disk is not None:
            _cache.set(u, disk)
            return disk
    try:
        resp = await _fetch_one(u, http, hops=0)
    except httpx.HTTPError:
        resp = None
    if resp is None or resp.status_code != 200:
        fetched = await _browser_fetch_bytes(u)
        if fetched is None:
            return None
        body, ctype = fetched
    else:
        if len(resp.content) > _config.SETTINGS.poster_size_cap_bytes:
            return None
        body = resp.content
        ctype = resp.headers.get("Content-Type", "image/jpeg")
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"
    if _config.SETTINGS.poster_cache_dir is not None:
        await asyncio.to_thread(_disk_put, _config.SETTINGS.poster_cache_dir, u, body, ctype)
    _cache.set(u, (body, ctype))
    return body, ctype
