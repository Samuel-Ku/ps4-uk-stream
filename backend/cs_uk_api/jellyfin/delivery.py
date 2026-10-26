"""Stream delivery (the D7 conversation): resolution, byte paths, download.

ONE owner for what happens after the client presses play — the facade's
resolution and every byte-serving route:

  - :func:`resolve_stream` — the facade's playable-id resolver and member
    walk, layered over :func:`cs_uk_api.play_request.attempt_play`, the
    shared play-request seam native ``/api/stream`` also crosses. It owns
    the two playable id families (a ``g2:`` movie key → the group's
    first-seen provider, and on a walkable refusal the group's other
    members after it; a provider-scoped episode wire id → its prefix),
    the group walk, and the None-means-404 posture (D2: never a 5xx for
    unplayable ids). The provider call, translation gate, health recording
    and play attestation live in the shared seam.
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
from collections.abc import Sequence
from functools import partial
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from ..catalog import (
    episode_group_key,
    group_resolution,
    peek_group_content,
    record_dub_choice,
    resolve_item,
)
from ..health import LANE_VERDICT_CODES
from ..http_client import get_client
from ..models import PlaySurface, SearchResult, StreamResponse
from ..play_request import attempt_play, attest_refusal, playable_target
from ..providers import PROVIDERS
from ..wire_identity import is_group_key, split_season_key
from .dto import safe_filename
from .hls_proxy import proxy_download, proxy_stream, segment_target, serve_segment

log = logging.getLogger(__name__)


def provider_for_item(item_id: str) -> str | None:
    """The provider a playable item id names, or None when it names none.

    The attribution rule the seam and the attestation share: a
    provider-scoped episode wire id names its prefix; a ``gN:`` group key
    names the group's first-seen provider (the one
    :func:`resolve_stream` tries first; a member after it may end up
    serving). None for a malformed id or a cold group — the attestation
    then counts the fault under ``play_attestation.UNKNOWN_PROVIDER``
    rather than guessing.
    """
    if not is_group_key(item_id):
        target = playable_target(item_id)
        return target[0] if target is not None else None
    group_key, _ = split_season_key(item_id)
    res = group_resolution(group_key)
    if res is None or not res.sources:
        return None
    return res.sources[0].provider


#: Refusals that mean "ask the group's next member": a lane fault (the
#: member could not answer) or ``not_found`` (it answered with no
#: playable file). Derived from the health taxonomy rather than copied,
#: so a new lane code becomes walkable the moment it lands in
#: ``LANE_VERDICT_CODES`` — the classification keeps its one owner and
#: the walk cannot drift away from it. Deliberately NOT walkable:
#: ``gated`` (the title is deliberately unavailable; another member's
#: answer is not a repair for what the client asked) and the translation
#: verdicts (``invalid_translation`` / ``translation_missing`` — the
#: client picked that track, and the group's other members are not more
#: likely to carry it). A codeless crash (``None``) IS walkable: it is
#: recorded as a lane fault, so the walk treats it like one.
WALKABLE_VERDICT_CODES = frozenset(LANE_VERDICT_CODES | {"not_found"})

#: How many of a group's members one play press may walk. Upstream
#: etiquette, the #305 lesson applied to playback: a work carried by
#: eight providers must not spend eight upstream calls — each up to the
#: client timeout — to answer one request. The walk follows the group's
#: first-seen order (the order the detail page lists, so the member the
#: viewer sees first is the one tried first) and stops here.
MAX_MEMBER_ATTEMPTS = 3


def _member_attempts(sources: Sequence[SearchResult]) -> tuple[tuple[str, str], ...]:
    """A group's members as ``(provider, bare external id)`` attempts.

    First-seen order, so the walk degrades from the member the viewer saw
    toward the rest of the union; at most one attempt per provider, and
    never more than ``MAX_MEMBER_ATTEMPTS``. A card whose bare id is
    empty, or whose provider is no longer registered, cannot be asked at
    all and is dropped — the remaining attempts (or the final refusal)
    still record this request's outcome.
    """
    attempts: list[tuple[str, str]] = []
    asked: set[str] = set()
    for result in sources:
        if result.provider in asked or result.provider not in PROVIDERS:
            continue
        external_id = result.id.partition(":")[2]
        if not external_id:
            continue
        asked.add(result.provider)
        attempts.append((result.provider, external_id))
        if len(attempts) == MAX_MEMBER_ATTEMPTS:
            break
    return tuple(attempts)


async def resolve_stream(
    item_id: str,
    translation_id: str | None = None,
    *,
    surface: PlaySurface | None = None,
) -> StreamResponse | None:
    """The upstream ``StreamResponse`` behind a playable item id, or None.

    Resolves the two playable id families (D2/D3) to their provider, then
    asks :func:`cs_uk_api.play_request.attempt_play` per member — the
    same seam native ``/api/stream/{id}`` crosses, with the same bare
    external ids, ``translation`` (None = default voice, or the picked
    dub id, spec #276) and shared ``httpx`` client. This function owns
    only the id families and the member walk:

      - a movie's ``g2:`` group key → the group's first-seen provider
        (the same provider the detail page shows first), whose BARE
        external id is what stream() consumes; if it refuses, the walk
        continues through the group's other members (below). Playability
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
    key, a blocked title, an unknown provider prefix) yield None. A
    refusal degrades to None → 404, the facade's standing "never 5xx"
    posture (D2). The provider call, translation gate, health recording
    and attestation are delegated to
    :func:`cs_uk_api.play_request.attempt_play`; this resolver owns only
    the id families and the group walk.

    A group key is carried by several providers, so one member's refusal
    is not yet this title's verdict: the walk asks the next member when
    the verdict is walkable (``WALKABLE_VERDICT_CODES`` — a lane fault or
    ``not_found``), which is what makes the group key a resilient play
    surface rather than a single point of failure. Each attempt records
    its own outcome, so the attestation answers "served, by whom, after
    how many refusals" — while the served member's success is the only
    thing that reaches lane health (a walk that ends in refusal has
    recorded every lane fault on the way, ADR-0002 intact). A walkable
    refusal that is not walkable ends the walk: ``gated`` and the
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
        group_key, season_number = split_season_key(item_id)
        if season_number is not None:
            attest_refusal(provider_for_item(group_key), item_id, "not_found", surface)
            return None
        content = (await resolve_item(group_key)).content
        if content is None or content.form != "movie":
            attest_refusal(provider_for_item(group_key), item_id, "not_found", surface)
            return None
        res = group_resolution(group_key)
        if res is None or not res.sources:
            attest_refusal(None, item_id, "not_found", surface)
            return None
        attempts = _member_attempts(res.sources)
        if not attempts:
            # Members exist on record but none can be asked (no usable
            # ids, or every one names a retired provider): the same
            # no-playable-source verdict a cold group gets.
            attest_refusal(None, item_id, "not_found", surface)
            return None
    else:
        # Provider-scoped episode wire id — split the prefix and hand the
        # suffix straight to stream(), exactly like /api/stream/{id}.
        # Episodes are not reverse-lookupable (D2), so an episode has
        # exactly one member and no walk.
        target = playable_target(item_id)
        if target is None:
            attest_refusal(None, item_id, "not_found", surface)
            return None
        attempts = (target,)

    http = get_client()
    for position, (provider_id, external_id) in enumerate(attempts):
        outcome = await attempt_play(
            provider_id,
            external_id,
            translation_id,
            http,
            item_id=item_id,
            surface=surface,
        )
        if outcome.stream is not None:
            if position:
                log.info(
                    "jellyfin playback member fallback id=%s served_by=%s after=%d refusal(s)",
                    item_id,
                    provider_id,
                    position,
                )
            return outcome.stream
        if outcome.code is not None and outcome.code not in WALKABLE_VERDICT_CODES:
            # gated / the client's own translation verdicts: the walk ends
            # here rather than spending another member's upstream call on
            # an answer the group cannot change.
            return None
    return None


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
