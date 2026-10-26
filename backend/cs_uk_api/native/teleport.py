"""Native Semantic Source Teleport route (spec #443).

The client uploads the last seconds of decoded audio and two playable
ids. This module is the HTTP/orchestration layer only: validate the
candidate, resolve it through the shared playback seam, hand the clip to
the aligner, and map the verdict to the native error vocabulary.

No Jellyfin facade route and no session registry: the request is
self-contained, and the clip lives only for the duration of the request.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import wave
from io import BytesIO

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .. import catalog
from .. import config as _config
from ..health import ITEM_VERDICT_CODES
from ..models import ErrorResponse
from ..playback import resolve_playable_target, resolve_stream
from ..providers import PROVIDERS
from ..providers.base import ProviderError
from ..teleport import (
    ALIGNMENT_TIMEOUT_S,
    MAX_CLIP_BYTES,
    MAX_CONCURRENT_ALIGNMENTS,
    MAX_WINDOW_SECONDS,
    MIN_WINDOW_SECONDS,
    ChromaprintAligner,
    TeleportAligner,
    TeleportCandidate,
    TeleportResponse,
    TeleportStream,
    TeleportUnavailable,
)
from ..wire_identity import parse_episode_tail, split_episode_tail

log = logging.getLogger(__name__)

#: The route's process-wide worker seam. Tests replace this with a fake;
#: production construction stays here, not in the request body.
ALIGNER: TeleportAligner = ChromaprintAligner()

#: At most two media jobs at once. The semaphore is process-wide so a
#: second viewer queues rather than starving the API event loop.
_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_ALIGNMENTS)

_BARE_EPISODE_RE = re.compile(r"^:e(\d+)(?::.*)?$")


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorResponse(error=code, message=message).model_dump(),
    )


def _trim_clip(raw: bytes, window_seconds: float) -> bytes:
    """Validate a WAV upload and return its last ``window_seconds``.

    Validation happens before any subprocess: RIFF/WAVE magic, 16-bit
    PCM, 1-2 channels, and a 10-20 second source clip. The channel count
    is preserved so the trim stays byte-exact; the aligner downmixes.
    """
    if len(raw) > MAX_CLIP_BYTES:
        raise _error(400, "audio_too_large", f"audio exceeds {MAX_CLIP_BYTES} bytes")
    if not raw.startswith(b"RIFF") or raw[8:12] != b"WAVE":
        raise _error(400, "invalid_audio", "audio must be a RIFF/WAVE file")
    try:
        with wave.open(BytesIO(raw), "rb") as src:
            channels = src.getnchannels()
            sample_width = src.getsampwidth()
            frame_rate = src.getframerate()
            frames = src.getnframes()
            if sample_width != 2 or channels not in (1, 2) or frame_rate <= 0:
                raise _error(400, "invalid_audio", "audio must be 16-bit PCM")
            duration = frames / frame_rate
            if not MIN_WINDOW_SECONDS <= duration <= MAX_WINDOW_SECONDS:
                raise _error(
                    400,
                    "invalid_audio",
                    f"audio must be {MIN_WINDOW_SECONDS:.0f}-{MAX_WINDOW_SECONDS:.0f}s",
                )
            wanted = min(frames, max(1, round(window_seconds * frame_rate)))
            start = frames - wanted
            src.setpos(start)
            payload = src.readframes(wanted)
            params = src.getparams()
    except HTTPException:
        raise
    except (EOFError, wave.Error, OSError) as exc:
        raise _error(400, "invalid_audio", "audio must be a readable WAV file") from exc

    out = BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(params.nchannels)
        dst.setsampwidth(params.sampwidth)
        dst.setframerate(params.framerate)
        dst.writeframes(payload)
    return out.getvalue()


async def _episode_coords(item_id: str) -> tuple[int, int] | None:
    """(season, episode) for a provider-scoped episode wire id.

    The wire tail is the fast path. Providers that emit ``:eN`` instead of
    ``:sNeM`` need the cached content page to recover the season; that is
    the same content read the detail/source-switch surfaces already use.
    The bare form is matched on the episode NUMBER, not its wire id: an
    episode id may be provider-scoped (``uakino:123:e5``) or bare
    (``:e5``), and comparing id strings across providers would reject
    every valid pair.
    """
    split = split_episode_tail(item_id)
    if split is None:
        return None
    composite, tail = split
    parsed = parse_episode_tail(tail)
    if parsed is not None:
        return parsed

    bare = _BARE_EPISODE_RE.fullmatch(tail)
    provider_id, _, external_id = composite.partition(":")
    if bare is None or provider_id not in PROVIDERS or not external_id:
        return None
    result = await catalog.provider_content(provider_id, external_id)
    content = result.content
    if content is None:
        return None
    wanted = int(bare.group(1))
    for season in content.seasons or []:
        for episode in season.episodes:
            if episode.number == wanted:
                return season.number, episode.number
    return None


async def _same_episode(item_id: str, candidate_id: str) -> bool:
    current = await _episode_coords(item_id)
    candidate = await _episode_coords(candidate_id)
    return current is not None and current == candidate


def _provider_label(provider_id: str) -> str:
    provider = PROVIDERS.get(provider_id)
    name = getattr(provider, "name", None) if provider is not None else None
    return str(name or provider_id)


def register(app: FastAPI) -> None:
    """Attach the native teleport conversation to the app."""

    @app.post("/api/teleport/align", response_model=TeleportResponse)
    async def align(
        item_id: str = Form(...),
        candidate_id: str = Form(...),
        position_ticks: int = Form(...),
        audio: UploadFile | None = File(None),  # noqa: B008 — FastAPI's marker
        window_seconds: float = Form(15.0),
    ) -> TeleportResponse:
        """Align the client's reference clip against one alternate source.

        ``matched`` is the only status that permits the client to switch
        automatically. ``ambiguous`` and ``no_match`` are 200 answers:
        they carry the candidate and let the client show a picker rather
        than treating a low-confidence guess as a server fault.
        """
        if not _config.SETTINGS.teleport_enabled:
            raise _error(503, "teleport_unavailable", "teleport is disabled")
        if audio is None:
            raise _error(400, "invalid_request", "audio is required")
        if position_ticks < 0:
            raise _error(400, "invalid_request", "position_ticks must be >= 0")
        if not MIN_WINDOW_SECONDS <= window_seconds <= MAX_WINDOW_SECONDS:
            raise _error(
                400,
                "invalid_request",
                f"window_seconds must be {MIN_WINDOW_SECONDS:.0f}-{MAX_WINDOW_SECONDS:.0f}",
            )

        # An id the playable seam cannot resolve is a 404, not a mismatch:
        # that is the same posture /api/stream and the facade already keep.
        item_target = await resolve_playable_target(item_id)
        candidate_target = await resolve_playable_target(candidate_id)
        if item_target is None or candidate_target is None:
            raise _error(404, "not_found", "playable item not found")

        current_group = catalog.episode_group_key(item_id)
        candidate_group = catalog.episode_group_key(candidate_id)
        if current_group is None or candidate_group is None:
            raise _error(400, "invalid_request", "both ids must be series episodes")
        if current_group != candidate_group:
            raise _error(400, "candidate_mismatch", "candidate is a different work")
        try:
            same_episode = await _same_episode(item_id, candidate_id)
        except ProviderError as exc:
            log.warning(
                "teleport candidate validation failed item=%s candidate=%s code=%s",
                item_id,
                candidate_id,
                exc.code,
            )
            raise _error(
                502, "candidate_unavailable", "candidate content unavailable"
            ) from exc
        except Exception as exc:
            log.warning(
                "teleport candidate validation failed item=%s candidate=%s error=%s",
                item_id,
                candidate_id,
                type(exc).__name__,
            )
            raise _error(
                502, "candidate_unavailable", "candidate content unavailable"
            ) from exc
        if not same_episode:
            raise _error(400, "candidate_mismatch", "candidate is a different episode")

        clip = _trim_clip(await audio.read(MAX_CLIP_BYTES + 1), window_seconds)
        position_seconds = position_ticks / 10_000_000

        try:
            candidate_stream = await resolve_stream(candidate_id, raise_errors=True)
        except ProviderError as exc:
            if exc.code in ITEM_VERDICT_CODES:
                raise _error(404, "not_found", exc.message) from exc
            log.warning(
                "teleport candidate resolution failed candidate=%s code=%s",
                candidate_id,
                exc.code,
            )
            raise _error(502, "candidate_unavailable", exc.message) from exc
        except Exception as exc:
            log.warning(
                "teleport candidate resolution failed candidate=%s error=%s",
                candidate_id,
                type(exc).__name__,
            )
            raise _error(502, "candidate_unavailable", "candidate stream failed") from exc
        if candidate_stream is None:
            raise _error(404, "not_found", "candidate has no playable stream")

        started = time.monotonic()
        try:
            # The 45 s budget covers the queue as well as the work: waiting
            # for a slot must not extend a request's wait indefinitely.
            async with asyncio.timeout(ALIGNMENT_TIMEOUT_S):
                async with _SEMAPHORE:
                    result = await ALIGNER.align(
                        reference_wav=clip,
                        candidate=candidate_stream,
                        position_seconds=position_seconds,
                    )
        except TimeoutError as exc:
            raise _error(504, "alignment_timeout", "alignment exceeded its budget") from exc
        except TeleportUnavailable as exc:
            raise _error(503, "teleport_unavailable", str(exc)) from exc
        except Exception as exc:
            log.warning(
                "teleport alignment failed candidate=%s error=%s",
                candidate_id,
                type(exc).__name__,
            )
            raise _error(503, "teleport_unavailable", "alignment failed") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        stream: TeleportStream | None = None
        seek_ticks: int | None = None
        offset_seconds: float | None = None
        if result.status != "no_match" and result.offset_seconds is not None:
            stream = TeleportStream(
                url=candidate_stream.url,
                type=candidate_stream.type,
                headers=dict(candidate_stream.headers),
            )
            offset_seconds = result.offset_seconds
            seek_ticks = max(
                0, position_ticks + round(offset_seconds * 10_000_000)
            )

        candidate = TeleportCandidate(
            id=candidate_id,
            provider=candidate_target.provider_id,
            label=_provider_label(candidate_target.provider_id),
            confidence=result.confidence or 0.0,
        )
        response = TeleportResponse(
            status=result.status,
            source_id=candidate_id,
            stream=stream,
            seek_ticks=seek_ticks,
            offset_seconds=offset_seconds,
            confidence=result.confidence,
            margin=result.margin,
            candidates=[candidate],
            reason=result.reason,
        )
        log.info(
            "teleport status=%s item=%s candidate=%s offset_s=%s confidence=%s"
            " margin=%s latency_ms=%d reason=%s",
            response.status,
            item_id,
            candidate_id,
            offset_seconds,
            result.confidence,
            result.margin,
            latency_ms,
            result.reason,
        )
        return response
