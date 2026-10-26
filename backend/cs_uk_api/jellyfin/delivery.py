"""Stream delivery (the D7 conversation): resolution, byte paths, download.

ONE owner for what happens after the client presses play — the seam and
every byte-serving route:

  - :func:`resolve_stream` — THE seam every playback surface crosses:
    the two playable id families (a ``g2:`` movie key → the group's
    member whose content page answered, and on a walkable refusal the
    group's other members after it; a provider-scoped episode wire id →
    its prefix),
    ``provider.stream()``, the health recording, the play attestation
    (one outcome per playback request, keyed by the ``surface`` its
    caller declares) and the None-means-404 posture (D2: never a 5xx for
    unplayable ids).
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
from functools import partial
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

#: Re-exported for the tests that assert the cap from this seam: the
#: value itself has ONE owner (``_catalog_state.resolution``, Q45/Q47).
from ..catalog import MAX_MEMBER_ATTEMPTS as MAX_MEMBER_ATTEMPTS  # noqa: PLC0414
from ..catalog import (
    WALKABLE_VERDICT_CODES,
    episode_group_key,
    group_content_member,
    group_resolution,
    member_attempts,
    peek_group_content,
    record_dub_choice,
    resolve_item,
)
from ..health import ITEM_VERDICT_CODES, TRACKER, record_verdict
from ..http_client import get_client
from ..models import PlaySurface, StreamResponse
from ..play_attestation import PLAY_ATTESTATION
from ..providers import PROVIDERS
from ..providers.base import ProviderError
from ..torrent_engine import ENGINE_TRACKER_ID
from ..wire_identity import is_group_key
from .dto import safe_filename
from .hls_proxy import proxy_download, proxy_stream, segment_target, serve_segment

log = logging.getLogger(__name__)


def provider_for_item(item_id: str) -> str | None:
    """The provider a playable item id names, or None when it names none.

    The attribution rule the seam and the attestation share: a
    provider-scoped episode wire id names its prefix; a ``gN:`` group key
    names the group's first-seen provider (the one the group's chip strip
    shows first; :func:`resolve_stream` may try the member whose content
    page answered before it, and a member after it may end up serving).
    None for a malformed id or a cold group — the attestation
    then counts the fault under ``play_attestation.UNKNOWN_PROVIDER``
    rather than guessing.
    """
    if not is_group_key(item_id):
        provider_id, _, external_id = item_id.partition(":")
        return provider_id if provider_id in PROVIDERS and external_id else None
    group_key, _ = _split_season_suffix(item_id)
    res = group_resolution(group_key)
    if res is None or not res.sources:
        return None
    return res.sources[0].provider


def _refuse(
    provider_id: str | None,
    item_id: str,
    code: str | None,
    surface: PlaySurface | None,
) -> None:
    """Attest one refusal, when this request was a playback request.

    ``surface is None`` means the caller is not a playback surface — the
    subtitle route crosses the same seam to find a track. Its refusal is
    real (the client still gets the standing 404), but it is not a play
    request, so it is not counted on the playback axis.
    """
    if surface is not None:
        PLAY_ATTESTATION.refused(provider_id, item_id, code, surface)


async def _attempt_stream(
    provider_id: str,
    external_id: str,
    translation_id: str | None,
    http: httpx.AsyncClient,
    item_id: str,
    surface: PlaySurface | None,
) -> tuple[StreamResponse | None, str | None]:
    """One member's attempt: the upstream call and both outcome axes.

    Records the HEALTH verdict (``ITEM_VERDICT_CODES`` are the client-side
    semantics ADR-0002 keeps out of the tracker — the #373 note — and
    engine-path faults attribute to the engine's own entry, spec #394)
    and the play ATTESTATION, exactly as the single-member path always
    did. The verdict *code* rides back because the caller needs it: it
    decides whether the group has another member worth asking. ``None``
    means served — or refused by a codeless crash, which the caller walks
    because that is the lane fault it was recorded as.
    """
    provider = PROVIDERS[provider_id]
    try:
        stream = await provider.stream(external_id, translation_id, http)
    except ProviderError as e:
        if e.code in ITEM_VERDICT_CODES:
            # Deterministic item/client-level verdicts (ADR-0002: 404
            # codes are client-side semantics, NOT upstream health — the
            # #373 error-surface note): never flip the tracker — five
            # dead-title clicks must not starve a whole lane's rows.
            # gated = deliberately unavailable; not_found = no playable
            # file / dead torrent; invalid_translation /
            # translation_missing = client semantics.
            log.info(
                "jellyfin playback item verdict=%s provider=%s id=%s",
                e.code,
                provider_id,
                item_id,
            )
            _refuse(provider_id, item_id, e.code, surface)
            return None, e.code
        log.warning(
            "jellyfin playback stream failed provider=%s id=%s err=%s", provider_id, item_id, e
        )
        attributed = ENGINE_TRACKER_ID if e.engine_path else provider_id
        record_verdict(attributed, e.code)
        _refuse(attributed, item_id, e.code, surface)
        return None, e.code
    except Exception as e:  # noqa: BLE001
        log.warning(
            "jellyfin playback stream failed provider=%s id=%s err=%s", provider_id, item_id, e
        )
        record_verdict(provider_id, None)
        _refuse(provider_id, item_id, None, surface)
        return None, None
    TRACKER.record(provider_id, ok=True)
    if surface is not None:
        PLAY_ATTESTATION.served(provider_id)
    return stream, None


async def resolve_stream(
    item_id: str,
    translation_id: str | None = None,
    *,
    surface: PlaySurface | None = None,
) -> StreamResponse | None:
    """The upstream ``StreamResponse`` behind a playable item id, or None.

    Resolves the two playable id families (D2/D3) to their provider, then
    runs ``provider.stream()`` exactly as the native ``/api/stream/{id}``
    route does — same bare external ids, ``translation`` (None = default
    voice, or the picked dub id, spec #276), same shared ``httpx``
    client:

      - a movie's ``g2:`` group key → the group's members in first-seen
        order, LED by the member whose content page answered
        (``group_content_member``: content resolution walks the group
        too, so the page may have come from a member other than the
        first; Q47), each called with its BARE external id; if a member
        refuses, the walk continues through the group's other members
        (below). Playability
        is decided on the content's FORM — ``content.form == "movie"`` —
        the same verdict detail renders as ``Type="Movie"``, NOT the
        card's style literal (``SearchResult.type`` can say ``"anime"``
        for an anime FILM; conflating style with form would 404 a film
        the client just opened as a Movie). The shared
        ``resolve_group_content`` also carries the blocklist verdict, so
        a blocked title never gets a stream.
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

    A group key is carried by several providers, so one member's refusal
    is not yet this title's verdict: the walk asks the next member when
    the verdict is walkable (``WALKABLE_VERDICT_CODES`` — a lane fault or
    ``not_found``), which is what makes the group key a resilient play
    surface rather than a single point of failure. Each attempt records
    its own outcome, so the attestation answers "served, by whom, after
    how many refusals" — while the served member's success is the only
    thing that reaches lane health (a walk that ends in refusal has
    recorded every lane fault on the way, ADR-0002 intact). A walkable
    refusal ends the walk when it is not walkable: ``gated`` and the
    translation verdicts answer the client's own request, and asking the
    group again is not a repair for them.

    ``surface`` names the route family asking (see ``models.PlaySurface``)
    and is what makes this a *playback* request for
    :mod:`cs_uk_api.play_attestation`: the outcome — served, or refused
    with the verdict that refused it — is recorded under that surface.
    Callers that cross the seam for something other than playback (the
    subtitle route, which resolves to find a track) pass nothing and are
    not counted. A play press can therefore record more than once
    (``playbackinfo`` then ``stream``): the axis counts requests, not
    presses, and the surface tells the two apart.
    """
    if is_group_key(item_id):
        # Series/season keys and cold groups: not playable on their own.
        group_key, season_number = _split_season_suffix(item_id)
        if season_number is not None:
            _refuse(provider_for_item(group_key), item_id, "not_found", surface)
            return None
        content = (await resolve_item(group_key)).content
        if content is None or content.form != "movie":
            _refuse(provider_for_item(group_key), item_id, "not_found", surface)
            return None
        res = group_resolution(group_key)
        if res is None or not res.sources:
            _refuse(None, item_id, "not_found", surface)
            return None
        # The member whose content page answered leads the walk: the page
        # may have been served by a member other than the first (content
        # resolution walks the group too, Q47), and THAT member is the one
        # whose translation ids the client is holding, and the one whose
        # detail page we just proved alive — asking the members after it
        # first would spend an upstream call on a member already known
        # dead, and would hand a picked dub's id to members that never
        # listed it (a translation verdict ends the walk).
        attempts = member_attempts(
            res.sources, lead=group_content_member(group_key)
        )
        if not attempts:
            # Members exist on record but none can be asked (no usable
            # ids, or every one names a retired provider): the same
            # no-playable-source verdict a cold group gets.
            _refuse(None, item_id, "not_found", surface)
            return None
    else:
        # Provider-scoped episode wire id — split the prefix and hand the
        # suffix straight to stream(), exactly like /api/stream/{id}.
        # Episodes are not reverse-lookupable (D2), so an episode has
        # exactly one member and no walk.
        provider_id, _, external_id = item_id.partition(":")
        if provider_id not in PROVIDERS or not external_id:
            _refuse(None, item_id, "not_found", surface)
            return None
        attempts = ((provider_id, external_id),)

    http = get_client()
    for position, (provider_id, external_id) in enumerate(attempts):
        stream, code = await _attempt_stream(
            provider_id, external_id, translation_id, http, item_id, surface
        )
        if stream is not None:
            if position:
                log.info(
                    "jellyfin playback member fallback id=%s served_by=%s after=%d refusal(s)",
                    item_id,
                    provider_id,
                    position,
                )
            return stream
        if code is not None and code not in WALKABLE_VERDICT_CODES:
            # gated / the client's own translation verdicts: the walk ends
            # here rather than spending another member's upstream call on
            # an answer the group cannot change.
            return None
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
    # not a play request (``_refuse``).
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
