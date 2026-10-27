"""Stream delivery (the D7 conversation): resolution, byte paths, download.

ONE owner for what happens after the client presses play — the facade's
resolution and every byte-serving route:

  - :func:`resolve_stream` — re-exported from :mod:`cs_uk_api.playback`
    for the established facade seam. The implementation lives there so
    the native teleport route and the facade share ONE provider call,
    health recording and None-means-404 posture (D2: never a 5xx for
    unplayable ids). The two playable id families, the group's member
    walk and the per-member attestation are unchanged.
  - the delivery routes: ``/Videos/{id}/stream`` (302 vs byte proxy),
    ``/Videos|/Stream/{id}/vtt`` (the #378 subtitle 302),
    ``/Items/{id}/Download`` (spec #280: same bytes + a
    Content-Disposition name) and ``/Videos/{id}/segment`` (one
    rewritten HLS reference, D7).

The byte/segment proxy machinery itself — content-type map, ``URI=``
rewrite, registrable-domain SSRF guard, per-item header memo, redirect
following, manifest fetching — lives in :mod:`hls_proxy` (ticket
#342); the routes here stay thin: resolve, decide redirect-vs-proxy,
hand the resolved stream + the shared httpx client to the proxy module.

Extracted verbatim from :mod:`router` (safe refactor). Routes are
declared on the facade's own router by :func:`register` (kept FLAT —
see that docstring), so the wire surface (paths, status codes, the
public no-token posture of the byte routes) is unchanged.
"""

from __future__ import annotations

from functools import partial
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

#: Re-exported for the tests that assert the cap from this seam: the
#: value itself has ONE owner (``_catalog_state.resolution``, Q45/Q47).
from ..catalog import MAX_MEMBER_ATTEMPTS as MAX_MEMBER_ATTEMPTS  # noqa: PLC0414
from ..catalog import (
    episode_group_key,
    group_resolution,
    peek_group_content,
    record_dub_choice,
)
from ..http_client import get_client

#: Re-exported for the established facade seam: the implementation lives
#: with the playable-id grammar in :mod:`cs_uk_api.playback`, so the
#: native teleport route crosses the same provider call.
from ..playback import provider_for_item as provider_for_item  # noqa: PLC0414
from ..playback import resolve_stream as resolve_stream  # noqa: PLC0414
from .dto import safe_filename
from .hls_proxy import proxy_download, proxy_stream, segment_target, serve_segment


def decode_translation_source(source_id: str) -> tuple[str, str] | None:
    """Inverse of ``playback_info._translation_source_id``: ``(item_id,
    translation_id)`` or None for a plain (single-translation) item id.

    The encode side stays with the PlaybackInfo assembly (the builder
    that stamps ``<item_id>::<translation_id>``); this decode is the
    stream-route's read of what the client echoed back.
    """
    if "::" not in source_id:
        return None
    item_id, _, translation_id = source_id.rpartition("::")
    if not item_id or not translation_id:
        return None
    return item_id, translation_id


def title_for(item_id: str) -> str | None:
    """A display title for a playable item id (spec #280 download name).

    ``g2:`` keys resolve via the snapshot card (first) or the cached
    content page; episode wire ids resolve through the series group's
    content cache, matching the episode by its wire suffix. None for an
    unknown/cold id — the download route then falls back to the id
    itself so the filename is still stable and unique.
    """
    res = group_resolution(item_id)
    if res is not None and res.card is not None:
        return res.card.title
    content = peek_group_content(item_id)
    if content is not None:
        return content.title
    group_key = episode_group_key(item_id)
    if group_key is not None:
        content = peek_group_content(group_key)
        if content is not None:
            tail = item_id[len(content.id.partition(":")[0]) + 1 :]
            for season in content.seasons or []:
                for ep in season.episodes:
                    if ep.id == tail or ep.id == item_id:
                        return ep.title or content.title
            return content.title
    return None


def download_filename(item_id: str, stream_type: str) -> str:
    """Content-Disposition filename for ``/Items/{id}/Download``.

    ``<safe-title>.<container>`` where the container is the provider's
    actual stream type (mp4/m3u8…) so the saved file is usable offline.
    The filename-safe rendering lives in ``dto.safe_filename`` (shared
    with the download-source attach, ticket #344).
    """
    title = title_for(item_id) or item_id.rsplit(":", 1)[-1]
    return f"{safe_filename(title)}.{stream_type}"


async def video_stream(
    item_id: str,
    request: Request,
    media_source_id: str | None = Query(default=None, alias="mediaSourceId"),
) -> Response:
    """Conditional stream handler (D7): redirect, or the byte proxy.

    ``StreamResponse`` with no header map → 302 straight to the CDN URL
    (no proxying). With a header map the backend owns the bytes — mp4
    files forward the client's ``Range``, HLS manifests are fetched,
    rewritten, and served as mpegurl so segments stay behind the facade
    too (see ``hls_proxy.proxy_stream``).

    Spec #276: ``mediaSourceId`` is the dub source echoed from
    PlaybackInfo (``<item_id>::<translation_id>``) — it switches the
    stream to that translation and records the pick as per-series dub
    memory (series only; movies never remember, v3). The plain item id
    (single-translation path) stays exactly as before.
    """
    translation_id: str | None = None
    decoded = decode_translation_source(media_source_id) if media_source_id else None
    if decoded is not None:
        # The echoed source id wins over the path item id — the client
        # plays the FIRST source, whose item part is the same item.
        item_id, translation_id = decoded
    stream = await resolve_stream(item_id, translation_id, surface="stream")
    if stream is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    if translation_id is not None:
        await record_dub_choice(item_id, translation_id)
    if not stream.headers:
        return RedirectResponse(stream.url, status_code=302)
    response = await proxy_stream(
        item_id=item_id,
        stream=stream,
        http=get_client(),
        range_header=request.headers.get("range"),
    )
    if response is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    return response


async def item_vtt(item_id: str) -> Response:
    """Subtitle delivery (#378): the engine's VTT-converted track.

    PlaybackInfo's ``Subtitle`` MediaStream carries ``DeliveryUrl``
    pointing HERE (``/Stream/{item}/vtt``, both spellings — the client's
    track element follows whatever base URL it already knows). The route
    resolves the SAME stream seam as playback, then hands the player the
    engine's ``stream/{i}?format=vtt`` endpoint — BitPlay converts the
    external srt to WEBVTT on the fly (research #367 §1). No subtitle on
    the session ⇒ 404 ``item_unavailable``, the standing posture.
    """
    # No ``surface``: subtitles cross the seam to find a track, they are
    # not a play request (``play_request.attest_refusal``).
    stream = await resolve_stream(item_id)
    if stream is None or stream.subtitle_url is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    return RedirectResponse(stream.subtitle_url, status_code=302)


async def item_download(item_id: str) -> Response:
    """Original-quality download (spec #280): the SAME bytes the stream
    route serves, with a Content-Disposition filename.

    The download button must fetch exactly what a play would — the same
    :func:`resolve_stream` seam the stream route crosses — so an
    unplayable id 404s identically. The redirect path (no upstream
    headers) is forced through the byte proxy here so the response can
    carry the ``Content-Disposition`` file name the download manager
    saves under; HLS manifests are proxied with the provider's headers
    and their references rewritten to stay behind the facade. The
    filename is ``<safe-title>.<container>`` (:func:`download_filename`).
    """
    stream = await resolve_stream(item_id, surface="download")
    if stream is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    filename = download_filename(item_id, stream.type)
    # HTTP header values are latin-1; a Cyrillic title cannot ride in the
    # bare ``filename=`` (UnicodeEncodeError). ASCII-suffix fallback plus
    # the RFC 5987 ``filename*=UTF-8''`` form keeps the real name for
    # modern clients and a usable ASCII name for the rest.
    ascii_name = filename.encode("ascii", errors="replace").decode("ascii")
    if ascii_name == filename:
        disposition = f'attachment; filename="{filename}"'
    else:
        encoded = quote(filename, safe="")
        disposition = (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'
        )

    # The byte proxy is forced here even when the stream route would 302,
    # so the Content-Disposition name can ride along (spec #280).
    response = await proxy_download(
        item_id=item_id, stream=stream, http=get_client(), content_disposition=disposition
    )
    if response is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    return response


async def video_segment(item_id: str, url: str = Query(...)) -> Response:
    """Proxy one rewritten HLS reference (D7).

    ``url`` is an upstream reference embedded by the manifest rewriter
    (already percent-encoded, decoded once by FastAPI): an ordinary
    segment, or another playlist — a master's variant, or a variant's own
    segment list, recursively re-rewritten so a multi-level tree keeps
    every descendant reference pointed at the backend (see
    ``hls_proxy.serve_segment``). The host must match the item's CDN
    (dot-boundary) — anything else fails closed to 404 — and
    Referer-gated CDNs still serve.
    """
    target = await segment_target(
        item_id, resolve_stream=partial(resolve_stream, surface="segment")
    )
    if target is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    response = await serve_segment(item_id=item_id, url=url, target=target, http=get_client())
    if response is None:
        raise HTTPException(status_code=404, detail="item_unavailable")
    return response


def register(parent: APIRouter) -> None:
    """Declare the delivery routes on the facade router.

    Kept FLAT on the facade's own router — deliberately NOT a nested
    ``include_router``: this FastAPI line wraps nested includes in a
    lazy router object without ``path_format``, which the app's
    case-normalize middleware (``main.jellyfin_case_normalize``, via
    ``router.normalize_jellyfin_path``) reads on every facade request.
    These ``add_api_route`` calls are the same registration the
    ``@router.get`` decorators did in :mod:`router` — same paths, same
    order, same (absent) dependency gate, same endpoints. The byte
    routes are deliberately PUBLIC (no token), exactly as before.
    """
    parent.add_api_route(
        "/Videos/{item_id:path}/stream", video_stream, methods=["GET"]
    )
    parent.add_api_route(
        "/Videos/{item_id:path}/vtt", item_vtt, methods=["GET"]
    )
    parent.add_api_route(
        "/Stream/{item_id:path}/vtt", item_vtt, methods=["GET"]
    )
    parent.add_api_route(
        "/Items/{item_id:path}/Download", item_download, methods=["GET"]
    )
    parent.add_api_route(
        "/Videos/{item_id:path}/segment", video_segment, methods=["GET"]
    )
