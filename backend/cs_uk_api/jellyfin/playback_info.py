"""PlaybackInfo (the D6 / #276 conversation): the pre-play envelope.

ONE owner for what the client reads before it presses play:

  - ``/Items/{id}/PlaybackInfo`` (GET + POST, one handler): one thin
    MediaSource per playable item (D6), the container learned from the
    provider's actual ``StreamResponse`` — one upstream ``stream()``
    call, the same cost a native client pays for ``/api/stream``;
  - the multi-source dub picker's WIRE SHAPES (spec #276): one
    MediaSource per ordered candidate translation, the
    ``<item_id>::<translation_id>`` source id that survives the round
    trip, and the audio MediaStream ``Index``/``DisplayTitle`` stamps
    (#347 — the ORDER itself is decided behind the
    ``ordered_translation_candidates`` seam in :mod:`catalog`).

The builders are deliberately colocated with the single route that
consumes them (``_container_from_type``, ``_engine_media_streams``,
``_translation_source_id``, ``_multi_source_media_sources``). The
ENCODE side of the ``::`` round trip lives here; the DECODE side (the
stream route's echo read) lives in :mod:`delivery` — one module per
direction of the wire conversation.

Extracted verbatim from :mod:`router` (safe refactor). The route is
declared on the facade's own router by :func:`register` (kept FLAT —
see that docstring), so the wire surface (paths, methods, the
``require_token`` gate, the response model) is unchanged.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..catalog import ordered_translation_candidates, playback_translations
from ..models import StreamResponse, Translation
from .auth import require_token
from .delivery import resolve_stream as _resolve_stream
from .models import (
    MediaSourceInfo,
    MediaStreamInfo,
    PlaybackInfoResponse,
)


def _container_from_type(stream_type: str) -> str:
    """StreamResponse.type → Jellyfin ``Container`` (D6).

    The native types are already Jellyfin container strings (``mp4``,
    ``m3u8``, ``hls``, ``dash``); pass them through verbatim rather than
    inventing a second mapping that could disagree.
    """
    return stream_type


def _engine_media_streams(item_id: str, stream: StreamResponse) -> list[MediaStreamInfo]:
    """#378: media-stream entries for what the file actually carries.

    Torrent-lane truth only — a classic ``StreamResponse`` (subtitle_url
    None) yields the D6 default ``[{Video}]`` list byte-identically, so
    the Ukrainian lane's PlaybackInfo wire never moves (parity gate).
    An enriched one appends ONE ``Subtitle`` entry when the session
    exposes a convertible srt. ``DeliveryUrl`` points at THIS facade
    (``/Stream/{item}/vtt`` re-resolves the stream and 302s to the
    engine) — the raw LAN engine host never reaches the player.

    No ``Audio`` entries: the engine's file listing cannot see audio
    streams inside a file, so any pick would be invented and unselectable
    (lean-build omission; restore if the engine exposes per-file audio
    stream indexes).
    """
    if stream.subtitle_url is None:
        return [MediaStreamInfo()]
    return [
        MediaStreamInfo(Type="Video"),
        MediaStreamInfo(Type="Subtitle", DeliveryUrl=f"/Stream/{item_id}/vtt"),
    ]


def _translation_source_id(item_id: str, translation_id: str) -> str:
    """MediaSource.Id for one translation (spec #276).

    The source id must survive the round trip: the client echoes it as
    ``mediaSourceId`` on the stream request and the stream route decodes
    it back to item + translation. ``item_id`` itself can contain ``:``
    (episode wire ids), so the separator is ``::`` and the decode splits
    on the LAST occurrence (the item id is the prefix).
    """
    return f"{item_id}::{translation_id}"


def _multi_source_media_sources(
    item_id: str, candidates: list[Translation]
) -> list[MediaSourceInfo]:
    """One MediaSource per ordered candidate translation (spec #276).

    Pure wire assembly (#347): the ORDER was chosen behind the seam
    (``ordered_translation_candidates`` — dedupe by label, cap 8,
    picked/remembered re-rank); this builder only stamps the facade
    shapes: the ``<item_id>::<translation_id>`` source id, an audio
    MediaStream with ``Index`` = the response position (1-based, the
    client's default selected index is 1) and ``DisplayTitle`` = the
    dub label so the picker renders names.
    """
    return [
        MediaSourceInfo(
            Id=_translation_source_id(item_id, t.id),
            Container="m3u8",
            MediaStreams=[
                MediaStreamInfo(Type="Video"),
                MediaStreamInfo(Type="Audio", Index=index, DisplayTitle=t.label),
            ],
            Path=f"/Videos/{item_id}/stream",
            PlaySessionId="",
            DisplayTitle=t.label,
        )
        for index, t in enumerate(candidates, start=1)
    ]


async def playback_info(
    item_id: str,
    request: Request,
    audio_stream_index: int | None = Query(default=None, alias="AudioStreamIndex"),
) -> PlaybackInfoResponse:
    """PlaybackInfo: one thin MediaSource per playable item (D6), and
    with multiple translations (spec #276) ONE MediaSource per dub — the
    client's named source picker becomes real.

    The @jellyfin/sdk hits this with POST (capture row 6) and the spec
    declares GET; both spellings serve the identical envelope. The
    container is learned from the provider's actual ``StreamResponse`` —
    one upstream ``stream()`` call, the same cost a native client pays
    for ``/api/stream``. ``Path`` is fictitious (bytes always come from
    ``/Videos/{id}/stream``); ``PlaySessionId`` is a fresh UUID. Unplayed
    ids 404 (D2); a series/season card is not playable and 404s too.

    Multi-source (spec #276): when the item exposes more than one
    translation, the response lists one MediaSource per translation
    (cap 8, deduped by label, first player per label), each with an
    audio MediaStream carrying ``Index`` + ``DisplayTitle`` so the
    picker renders names. ``AudioStreamIndex`` (POST body or query) is
    the picker's selection — the matching source goes FIRST (the client
    plays MediaSources[0]); with the default index the remembered dub
    (spec #276) goes first, so a replay of the series defaults to it.
    Single-translation items stay exactly as before: one source, no
    picker.
    """
    # The body carries AudioStreamIndex (and MediaSourceId) on the
    # source-switch path; the query spelling covers GET.
    picked_index = audio_stream_index
    if picked_index is None and request.method == "POST":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a malformed body keeps the default
            body = {}
        if isinstance(body, dict):
            raw = body.get("AudioStreamIndex")
            if isinstance(raw, int):
                picked_index = raw

    translations, remembered = await playback_translations(item_id)
    if len(translations) <= 1:
        # Single-translation path (D6, unchanged): one thin source.
        stream = await _resolve_stream(item_id)
        if stream is None:
            raise HTTPException(status_code=404, detail="item_unavailable")
        play_session_id = str(uuid.uuid4())
        source = MediaSourceInfo(
            Id=item_id,
            Container=_container_from_type(stream.type),
            MediaStreams=_engine_media_streams(item_id, stream),
            Path=f"/Videos/{item_id}/stream",
            PlaySessionId=play_session_id,
        )
        return PlaybackInfoResponse(MediaSources=[source], PlaySessionId=play_session_id)

    # The ORDER (dedupe by label, cap 8, picked-index / remembered-dub
    # re-rank) is a seam decision (#347); the sources below are its
    # wire shapes.
    candidates = ordered_translation_candidates(
        translations, remembered=remembered, picked_index=picked_index
    )
    sources = _multi_source_media_sources(item_id, candidates)
    play_session_id = str(uuid.uuid4())
    for src in sources:
        src.PlaySessionId = play_session_id
    return PlaybackInfoResponse(MediaSources=sources, PlaySessionId=play_session_id)


def register(parent: APIRouter) -> None:
    """Declare the PlaybackInfo route on the facade router.

    Kept FLAT on the facade's own router — deliberately NOT a nested
    ``include_router``: this FastAPI line wraps nested includes in a
    lazy router object without ``path_format``, which the app's
    case-normalize middleware (``main.jellyfin_case_normalize``, via
    ``router.normalize_jellyfin_path``) reads on every facade request.
    Both methods carry the same ``require_token`` gate and response
    model as the original decorator stack; the two registrations are
    pattern-identical and differ only in method, so their relative
    table order (like their tail position) is inert.
    """
    parent.add_api_route(
        "/Items/{item_id:path}/PlaybackInfo",
        playback_info,
        methods=["GET"],
        response_model=PlaybackInfoResponse,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
    parent.add_api_route(
        "/Items/{item_id:path}/PlaybackInfo",
        playback_info,
        methods=["POST"],
        response_model=PlaybackInfoResponse,
        response_model_exclude_none=True,
        dependencies=[Depends(require_token)],
    )
