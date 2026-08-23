"""HLS/byte proxy for the Jellyfin facade stream routes (ticket #342).

The messy half of D7 lives here: the ``URI=`` manifest rewrite, the
registrable-domain SSRF guard, the redirect-following upstream opener,
the per-item header memo, and the streaming response wrapper. The router
keeps only resolution (``_resolve_stream``) and the route declarations;
every proxy decision is made inside this module.

Call-time dependency rule: the shared ``httpx`` client is a parameter the
router passes (resolved from the router module at call time, so tests can
swap it), and segment re-resolution goes through the injected
``resolve_stream`` callable — this module never imports the client factory.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi.responses import Response, StreamingResponse

from ..models import StreamResponse

log = logging.getLogger("cs_uk_api.jellyfin")

#: ``StreamResponse.type`` → the ``Content-Type`` the client expects (D7).
_STREAM_CTYPE = {
    "mp4": "video/mp4",
    "m3u8": "application/vnd.apple.mpegurl",
    "hls": "application/vnd.apple.mpegurl",
}

#: ``URI="..."`` attributes inside an HLS manifest (EXT-X-KEY, EXT-X-MAP,
#: EXT-X-MEDIA, child playlists) — rewritten like every plain segment line.
_URI_ATTR_RE = re.compile(r'URI="([^"]*)"')

_MAX_PROXY_HOPS = 5

#: Per-item memo of the provider headers + CDN host the byte/segment proxy
#: must use. The manifest is fetched once; segments arrive en masse during
#: playback, and re-running the provider's ``stream()`` (one upstream
#: scrape per call) for every segment would hammer the provider. A short
#: TTL memo fuels segments; expiry falls back to one re-resolution. It
#: deliberately memoizes the provider HEADER MAP and the chosen CDN host
#: — NOT the upstream URL, which stays ADR-0003's "never cached"
#: (session-scoped/token-signed); the fresh ``stream()`` on expiry is
#: exactly the "a miss costs one request" cost the ADR accepts.
#:
#: Re-exported by the router module under the same name — the suite's
#: isolation fixtures clear THIS dict through that binding.
_STREAM_MEMO: dict[str, tuple[float, str, dict[str, str], frozenset[str]]] = {}
_STREAM_MEMO_TTL_S = 15 * 60


def _cdn_host(url: str) -> str | None:
    """The lowercase hostname of an http(s) URL, or None."""
    host = urlparse(url).hostname
    return host.lower() if host else None


def _registrable_domain(host: str) -> str:
    """The last two labels of a hostname — the anchor the stream proxy's
    CDN check compares against.

    Live HLS trees routinely hand child playlists to a SIBLING subdomain
    of the same domain (``api.unimay.media`` hands the tree to
    ``cdn.unimay.media``), and a two-label anchor is exactly stable
    across those siblings while still excluding foreign registrants:
    ``evil.example`` can never equal ``cdn.example``. Co.UK-style
    multi-label public suffixes are out of scope — no UA provider CDN
    uses one.
    """
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def _stream_target_allowed(url: str, cdn_host: str, allowed: frozenset[str] = frozenset()) -> bool:
    """Whether the byte proxy may reach ``url``: a host on the same
    registrable domain as the CDN the provider selected for the item
    (``cdn.example`` and its ``media.``/``api.`` siblings are one CDN),
    or a registrable domain the provider explicitly sanctioned in
    ``StreamResponse.allowed_domains`` (a 302 gateway may hand bytes to
    a foreign CDN the provider picked, e.g. ufdub episodes on Dropbox).

    This is the stream proxy's standing posture: the facade fetches bytes
    only from the CDN a provider picked — a sibling subdomain of the same
    domain is still the picked CDN, a foreign registrable domain is not
    (unless provider-sanctioned), and a client pointing the segment route
    at an arbitrary host fails closed (mirrors the poster proxy's
    allowlist).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname is None:
        return False
    host = parsed.hostname.lower()
    return (
        _registrable_domain(host) == _registrable_domain(cdn_host)
        or _registrable_domain(host) in allowed
    )


def _is_hls_stream(stream: StreamResponse) -> bool:
    return stream.type in ("m3u8", "hls") or stream.url.endswith(".m3u8")


def _segment_url(item_id: str, upstream: str) -> str:
    """Backend URL a rewritten media reference re-enters through."""
    return f"/Videos/{item_id}/segment?url={quote(upstream, safe='')}"


def _rewrite_m3u8(body: str, manifest_url: str, item_id: str) -> str:
    """Rewrite a fetched manifest so every media reference re-enters the
    backend: plain segment/child-playlist lines AND ``URI="..."``
    attributes (key files, EXT-X-MAP init segments) become
    ``/Videos/{item_id}/segment`` fetches. Relative references resolve
    against the manifest URL (urljoin) — the client only ever talks to
    the backend, which owns the provider headers."""
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("#"):
            lines.append(
                _URI_ATTR_RE.sub(
                    lambda m: f'URI="{_segment_url(item_id, urljoin(manifest_url, m.group(1)))}"',
                    line,
                )
            )
        else:
            lines.append(_segment_url(item_id, urljoin(manifest_url, stripped)))
    return "\n".join(lines) + "\n"


def _memo_stream(
    item_id: str, cdn_host: str, headers: dict[str, str], allowed: frozenset[str]
) -> None:
    """Writer for the segment memo. Values are returned by reference, so
    the store hands out copies — never the live provider dict."""
    _STREAM_MEMO[item_id] = (time.monotonic(), cdn_host, dict(headers), allowed)


async def _proxy_target(
    item_id: str,
    *,
    resolve_stream: Callable[[str], Awaitable[StreamResponse | None]],
) -> tuple[str, dict[str, str], frozenset[str]] | None:
    """(cdn_host, provider headers, allowed domains) the segment proxy
    must use.

    Serves from the memo when fresh; otherwise re-resolves the stream
    once and memoizes. None → 404 (D2)."""
    hit = _STREAM_MEMO.get(item_id)
    if hit is not None and time.monotonic() - hit[0] < _STREAM_MEMO_TTL_S:
        return hit[1], dict(hit[2]), hit[3]
    stream = await resolve_stream(item_id)
    if stream is None:
        return None
    cdn_host = _cdn_host(stream.url)
    if cdn_host is None:
        return None
    _memo_stream(item_id, cdn_host, stream.headers, stream.allowed_domains)
    return cdn_host, dict(stream.headers), stream.allowed_domains


async def _open_upstream(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    range_header: str | None,
    cdn_host: str,
    hops: int = 0,
    allowed: frozenset[str] = frozenset(),
) -> tuple[httpx.Response, Callable[[], Awaitable[None]]] | None:
    """Open a validating byte stream to ``url``, following redirects by
    hand and re-validating EVERY hop against the item's CDN host.

    Returns ``(resp, closer)`` where the closer releases the upstream
    stream once the caller is done feeding bytes; None fails closed
    (D2 posture — never raises). Only a 2xx response opens a stream: a
    403/416/500 CDN verdict is a playback-grade failure the facade cannot
    meaningfully relay, so it becomes the same 404 an unresolvable id
    gets.
    """
    if hops > _MAX_PROXY_HOPS or not _stream_target_allowed(url, cdn_host, allowed):
        return None
    req_headers = dict(headers)
    if range_header is not None:
        req_headers["Range"] = range_header
    try:
        cm = http.stream("GET", url, headers=req_headers)
        resp = await cm.__aenter__()
    except httpx.HTTPError:
        return None
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location")
        await cm.__aexit__(None, None, None)
        if location is None:
            return None
        return await _open_upstream(
            http, urljoin(url, location), headers, range_header, cdn_host, hops + 1, allowed
        )
    if resp.status_code < 200 or resp.status_code >= 300:
        await cm.__aexit__(None, None, None)
        return None

    async def _closer() -> None:
        await cm.__aexit__(None, None, None)

    return resp, _closer


async def _fetch_manifest(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    cdn_host: str,
    hops: int = 0,
    allowed: frozenset[str] = frozenset(),
) -> httpx.Response | None:
    """Fetch a small HLS manifest with hop revalidation; only a 200 counts."""
    if hops > _MAX_PROXY_HOPS or not _stream_target_allowed(url, cdn_host, allowed):
        return None
    try:
        resp = await http.get(url, headers=headers)
    except httpx.HTTPError:
        return None
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location")
        if location is None:
            return None
        return await _fetch_manifest(
            http, urljoin(url, location), headers, cdn_host, hops + 1, allowed
        )
    if resp.status_code != 200:
        log.warning("jellyfin manifest non-200 url=%s status=%s", url, resp.status_code)
        return None
    return resp


def _streaming_response(
    resp: httpx.Response,
    close: Callable[[], Awaitable[None]],
    fallback_ctype: str,
) -> StreamingResponse:
    """Wrap an upstream byte stream as the facade's response.

    Upstream's own status, ``Content-Type``, ``Content-Range`` and
    ``Accept-Ranges`` ride along (a file proxy must answer a ``Range``
    with the CDN's 206 honestly); only a missing Content-Type falls back
    to the provider type's expected value. The upstream stream is released
    when the response body finishes — or when the client disconnects.
    """
    out_headers: dict[str, str] = {}
    for name in ("Content-Type", "Content-Range", "Accept-Ranges"):
        value = resp.headers.get(name)
        if value is not None:
            out_headers[name] = value
    if "Content-Type" not in out_headers:
        out_headers["Content-Type"] = fallback_ctype

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await close()

    return StreamingResponse(body(), status_code=resp.status_code, headers=out_headers)


async def proxy_stream(
    *,
    item_id: str,
    stream: StreamResponse,
    http: httpx.AsyncClient,
    range_header: str | None = None,
    content_disposition: str | None = None,
) -> Response | None:
    """Proxy one resolved stream behind the facade (D7): an HLS tree is
    fetched and rewritten (every media reference re-enters through the
    segment route); anything else is a validating byte proxy that forwards
    the client's ``Range`` and echoes the CDN's 206/Content-Range/
    Accept-Ranges back. ``content_disposition`` rides along on both
    branches (the download route names the file).

    Returns None on any failure — missing CDN host, failed manifest,
    refused/hopping-too-far upstream — and the caller turns that into the
    standing 404 (D2). The provider header map is memoized for the
    segment route on every successful entry.
    """
    cdn_host = _cdn_host(stream.url)
    if cdn_host is None:
        return None
    _memo_stream(item_id, cdn_host, stream.headers, stream.allowed_domains)

    extra_headers = (
        {"Content-Disposition": content_disposition} if content_disposition is not None else {}
    )

    if _is_hls_stream(stream):
        manifest = await _fetch_manifest(
            http, stream.url, stream.headers, cdn_host, allowed=stream.allowed_domains
        )
        if manifest is None:
            return None
        body = _rewrite_m3u8(
            manifest.content.decode("utf-8", errors="replace"), stream.url, item_id
        )
        # Whatever the upstream claims, a served manifest is a playlist
        # body and gets the mpegurl content-type (D7). If the provider
        # ever mislabels type (mp4) on a .m3u8 URL, the playlist
        # detection above decides, so ctype must not follow the label.
        return Response(content=body, media_type=_STREAM_CTYPE["m3u8"], headers=extra_headers)

    opened = await _open_upstream(
        http,
        stream.url,
        stream.headers,
        range_header,
        cdn_host,
        allowed=stream.allowed_domains,
    )
    if opened is None:
        return None
    upstream, closer = opened
    resp = _streaming_response(
        upstream, closer, _STREAM_CTYPE.get(stream.type, "application/octet-stream")
    )
    if content_disposition is not None:
        resp.headers["Content-Disposition"] = content_disposition
    return resp


async def proxy_download(
    *,
    item_id: str,
    stream: StreamResponse,
    http: httpx.AsyncClient,
    content_disposition: str,
) -> Response | None:
    """The download variant (spec #280): the SAME bytes the stream route
    serves, forced through the byte proxy even when the stream route
    would 302, so the response can carry the ``Content-Disposition``
    file name. No client ``Range`` is forwarded."""
    if not stream.headers:
        # No provider headers: stream the upstream body directly (the
        # stream route would 302; a download needs the file itself).
        cdn_host = _cdn_host(stream.url)
        if cdn_host is None:
            return None
        opened = await _open_upstream(
            http, stream.url, {}, None, cdn_host, allowed=stream.allowed_domains
        )
        if opened is None:
            return None
        upstream, closer = opened
        resp = _streaming_response(
            upstream, closer, _STREAM_CTYPE.get(stream.type, "application/octet-stream")
        )
        resp.headers["Content-Disposition"] = content_disposition
        return resp
    return await proxy_stream(
        item_id=item_id,
        stream=stream,
        http=http,
        range_header=None,
        content_disposition=content_disposition,
    )


async def segment_target(
    item_id: str,
    *,
    resolve_stream: Callable[[str], Awaitable[StreamResponse | None]],
) -> tuple[str, dict[str, str], frozenset[str]] | None:
    """Public memo lookup for the segment route (fresh memo hit, else one
    re-resolution through the injected resolver). None → 404 (D2)."""
    return await _proxy_target(item_id, resolve_stream=resolve_stream)


async def serve_segment(
    *,
    item_id: str,
    url: str,
    target: tuple[str, dict[str, str], frozenset[str]],
    http: httpx.AsyncClient,
) -> Response | None:
    """Proxy one rewritten HLS reference (D7).

    ``url`` is an upstream reference embedded by the manifest rewriter
    (already percent-encoded, decoded once by FastAPI): an ordinary
    segment, or another playlist — a master's variant, or a variant's own
    segment list. Segment bytes flow through the byte proxy; a playlist
    reference is fetched and re-rewritten exactly like the top manifest,
    so a multi-level playlist tree keeps every descendant reference
    pointed at the backend (the client's requests always carry the
    provider headers). The host was already validated when the target was
    resolved; a refused hop fails closed to None → 404.
    """
    cdn_host, headers, allowed = target
    if url.rstrip("/").lower().endswith(".m3u8"):
        manifest = await _fetch_manifest(http, url, headers, cdn_host, allowed=allowed)
        if manifest is None:
            return None
        body = _rewrite_m3u8(manifest.content.decode("utf-8", errors="replace"), url, item_id)
        return Response(content=body, media_type=_STREAM_CTYPE["m3u8"])
    opened = await _open_upstream(http, url, headers, None, cdn_host, allowed=allowed)
    if opened is None:
        return None
    upstream, closer = opened
    return _streaming_response(upstream, closer, "application/octet-stream")


__all__ = [
    "proxy_download",
    "proxy_stream",
    "segment_target",
    "serve_segment",
]
