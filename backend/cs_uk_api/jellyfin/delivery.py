"""Stream delivery (the D7 conversation): resolution, byte paths, download.

ONE owner for what happens after the client presses play — the seam and
every byte-serving route:

  - :func:`resolve_stream` — THE seam every playback surface crosses:
    the two playable id families (a ``g2:`` movie key → the group's
    first-seen provider; a provider-scoped episode wire id → its
    prefix), ``provider.stream()``, the health recording and the
    None-means-404 posture (D2: never a 5xx for unplayable ids).
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

import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from ..catalog import (
    card_for_group,
    episode_group_key,
    first_source,
    peek_group_content,
    record_dub_choice,
    resolve_item,
)
from ..health import TRACKER
from ..http_client import get_client
from ..models import StreamResponse
from ..providers import PROVIDERS
from ..providers.base import ProviderError
from ..wire_identity import is_group_key
from .dto import safe_filename
from .hls_proxy import proxy_download, proxy_stream, segment_target, serve_segment

log = logging.getLogger(__name__)


async def resolve_stream(
    item_id: str, translation_id: str | None = None
) -> StreamResponse | None:
    """The upstream ``StreamResponse`` behind a playable item id, or None.

    Resolves the two playable id families (D2/D3) to their provider, then
    runs ``provider.stream()`` exactly as the native ``/api/stream/{id}``
    route does — same bare external ids, ``translation`` (None = default
    voice, or the picked dub id, spec #276), same shared ``httpx``
    client:

      - a movie's ``g2:`` group key → the group's first-seen provider
        (the same provider the detail page shows first), whose BARE
        external id is what stream() consumes. Playability is decided on
        the content's FORM — ``content.form == "movie"`` — the same
        verdict detail renders as ``Type="Movie"``, NOT the card's style
        literal (``SearchResult.type`` can say ``"anime"`` for an anime
        FILM; conflating style with form would 404 a film the client just
        opened as a Movie). The shared ``resolve_group_content`` also
        carries the blocklist verdict, so a blocked title never gets a
        stream.
      - an episode wire id (``p1:s1e1``-style) → the provider is the id
        prefix; the episode suffix is handed to ``stream()`` exactly, no
        group-key resolution (episodes are not reverse-lookupable, D2).

    Unplayable ids (a series/season item — a show is not a playable
    thing, the client plays episodes, D3 — a season suffix, a cold group
    key, a blocked title, an unknown provider prefix) yield None. Rather
    than a middle-man tuple hop, the resolution and the stream call live
    in the same module: this IS the seam the routes cross, so a refusal
    degrades to None → 404, the facade's standing "never 5xx" posture
    (D2), and the provider+health recording stays colocated with it.
    """
    if is_group_key(item_id):
        # Series/season keys and cold groups: not playable on their own.
        group_key, season_number = _split_season_suffix(item_id)
        if season_number is not None:
            return None
        content = (await resolve_item(group_key)).content
        if content is None or content.form != "movie":
            return None
        first = first_source(group_key)
        if first is None:
            return None
        provider_id, result = first
        _, _, external_id = result.id.partition(":")
    else:
        # Provider-scoped episode wire id — split the prefix and hand the
        # suffix straight to stream(), exactly like /api/stream/{id}.
        provider_id, _, external_id = item_id.partition(":")
        if provider_id not in PROVIDERS or not external_id:
            return None

    provider = PROVIDERS[provider_id]
    http = get_client()
    try:
        stream = await provider.stream(external_id, translation_id, http)
        TRACKER.record(provider_id, ok=True)
        return stream
    except ProviderError as e:
        # A `gated` verdict is client-side semantics, NOT an upstream
        # failure (ADR-0002 amendment): the item is deliberately
        # unavailable — degrade to the standing 404 without marking
        # the provider down.
        if e.code == "gated":
            log.info("jellyfin playback gated provider=%s id=%s", provider_id, item_id)
            return None
        log.warning(
            "jellyfin playback stream failed provider=%s id=%s err=%s", provider_id, item_id, e
        )
        TRACKER.record(provider_id, ok=False)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning(
            "jellyfin playback stream failed provider=%s id=%s err=%s", provider_id, item_id, e
        )
        TRACKER.record(provider_id, ok=False)
        return None


def _split_season_suffix(parent_id: str) -> tuple[str, int | None]:
    """(group_key, season_number) for a season id, else (as-is, None).

    Season ids are ``<group_key>:S<n>`` (D2); the group key never
    carries an ``:S<n>`` tail, so ``rpartition`` cleanly separates the
    trailing season marker. A series/movie group key returns itself.
    (Local copy of the router helper's composition: the season-rail
    paths above are the ONLY consumers on this side of the seam.)
    """
    if not is_group_key(parent_id):
        return parent_id, None
    head, sep, tail = parent_id.rpartition(":")
    if sep and tail.startswith("S") and tail[1:].isdigit():
        return head, int(tail[1:])
    return parent_id, None


def decode_translation_source(source_id: str) -> tuple[str, str] | None:
    """Inverse of ``router._translation_source_id``: ``(item_id,
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
    card = card_for_group(item_id)
    if card is not None:
        return card.title
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
    stream = await resolve_stream(item_id, translation_id)
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
    stream = await resolve_stream(item_id)
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
    target = await segment_target(item_id, resolve_stream=resolve_stream)
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
