"""Semantic Source Teleport — alignment domain (spec #443).

The client sends the last few seconds of *decoded soundtrack* it just
played. This module decides whether that anchor occurs in the alternate
source's audio window, and where. Everything here is backend-side: the
client captures the reference and applies the switch, but never computes
a fingerprint.

The alignment floor is Chromaprint. We decode both sides to mono 11,025 Hz
WAV with ffmpeg, run ``fpcalc -raw -json``, and compare the raw 32-bit
fingerprint sequences at every sliding offset. The public seam is
:class:`TeleportAligner`; :class:`ChromaprintAligner` is the production
implementation, and tests inject a fake at the route seam or exercise
:func:`match_fingerprints` directly.

The MVP is a constant-offset matcher. It handles a longer intro/recap/ad
pad; it does not pretend to handle a re-cut episode, a different dub, or
a mid-roll insert. Those correctly land in ``no_match`` / ``ambiguous``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .models import StreamResponse, StreamType

#: One alignment window. The client captures the 15 s ending at the
#: failure position; the server trims longer uploads to the last 15 s.
WINDOW_SECONDS = 15.0
MIN_WINDOW_SECONDS = 10.0
MAX_WINDOW_SECONDS = 20.0
MAX_CLIP_BYTES = 4 * 1024 * 1024

#: The alternate source is decoded from this far before the position to
#: this far after it, plus one extra window for the anchor at the edge.
SEARCH_RADIUS_SECONDS = 300.0

#: Chromaprint has no reliable context at the very edges of a clip.
TRIM_SECONDS = 2.0

#: Hard confidence gate. The threshold and margin are deliberately
#: constants, not operator knobs: a soft gate would make the feature
#: unpredictable. Calibrate them on the live pair (the log line carries
#: both numbers), not per request.
MATCHED_CONFIDENCE = 0.80
MATCHED_MARGIN = 0.05
AMBIGUOUS_FLOOR = 0.60
MIN_REFERENCE_HASHES = 30
MIN_LOW_ENTROPY_POPCOUNT = 6
MAX_LOW_ENTROPY_POPCOUNT = 26
MIN_VALID_HASH_FRACTION = 0.60

#: Hard bounds for one alignment job.
ALIGNMENT_TIMEOUT_S = 45.0
COMMAND_TIMEOUT_S = 40.0
MAX_CONCURRENT_ALIGNMENTS = 2

TeleportStatus = Literal["matched", "ambiguous", "no_match"]


class TeleportUnavailable(Exception):
    """The alignment tools or the worker itself are not available."""


class TeleportStream(BaseModel):
    """The alternate stream the client should load (spec #443 contract)."""

    url: str
    type: StreamType = "mp4"
    headers: dict[str, str] = Field(default_factory=dict)


class TeleportCandidate(BaseModel):
    """One alternate source considered for this request."""

    id: str
    provider: str
    label: str
    confidence: float


class TeleportResponse(BaseModel):
    """``POST /api/teleport/align`` 200 body.

    ``matched`` is the only status that permits an automatic switch.
    ``ambiguous`` may carry the suggested stream/seek for the client's
    confirmation UI; ``no_match`` carries neither.
    """

    status: TeleportStatus
    source_id: str
    stream: TeleportStream | None = None
    seek_ticks: int | None = None
    offset_seconds: float | None = None
    confidence: float | None = None
    margin: float | None = None
    candidates: list[TeleportCandidate] = Field(default_factory=list)
    reason: str | None = None


@dataclass(frozen=True)
class AlignmentResult:
    """The worker's verdict, before the route maps it to the wire."""

    status: TeleportStatus
    offset_seconds: float | None = None
    confidence: float | None = None
    margin: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class FingerprintMatch:
    """Relative fingerprint alignment (used by the pure matcher tests)."""

    status: TeleportStatus
    candidate_offset_seconds: float | None
    confidence: float | None
    margin: float | None
    reason: str | None = None


class TeleportAligner(Protocol):
    """The one seam the route crosses; fakes implement this."""

    async def align(
        self,
        *,
        reference_wav: bytes,
        candidate: StreamResponse,
        position_seconds: float,
    ) -> AlignmentResult: ...


def _popcount(value: int) -> int:
    return (int(value) & 0xFFFFFFFF).bit_count()


def _fingerprint_rate(
    reference: Sequence[int],
    candidate: Sequence[int],
    *,
    reference_duration: float,
    candidate_duration: float,
) -> float:
    """Fingerprint frames per second, shared by both sequences.

    Chromaprint emits its first hash only after a fixed context window,
    so ``len(fingerprint) / duration`` under-reports the rate, by more
    on the 15 s reference than on the minutes-long candidate. Dividing
    the frame *difference* by the duration difference cancels that
    context offset and gives both sides one rate; a candidate that is
    not meaningfully longer falls back to the plain ratio.
    """
    duration_delta = candidate_duration - reference_duration
    frame_delta = len(candidate) - len(reference)
    if duration_delta > 0.5 and frame_delta > 0:
        return frame_delta / duration_delta
    return len(candidate) / candidate_duration


def match_fingerprints(
    reference: Sequence[int],
    candidate: Sequence[int],
    *,
    reference_duration: float,
    candidate_duration: float,
) -> FingerprintMatch:
    """Align a reference fingerprint inside a candidate fingerprint.

    Returns the candidate offset (seconds, relative to the candidate
    window's start) of the **reference clip start**, plus confidence and
    margin. The caller adds the window's absolute start and the reference
    duration to turn this into the viewer-facing offset.

    A silence/static reference must never win on a perfect zero-hash
    match; the low-entropy guard rejects it before the search.
    """
    if reference_duration <= 0 or candidate_duration <= 0:
        return FingerprintMatch("no_match", None, None, None, "decode_failed")
    if not reference or not candidate:
        return FingerprintMatch("no_match", None, None, None, "reference_quiet")

    rate = _fingerprint_rate(
        reference,
        candidate,
        reference_duration=reference_duration,
        candidate_duration=candidate_duration,
    )
    if rate <= 0:
        return FingerprintMatch("no_match", None, None, None, "decode_failed")

    trim = max(1, round(TRIM_SECONDS * rate))
    if len(reference) <= 2 * trim + MIN_REFERENCE_HASHES:
        return FingerprintMatch("no_match", None, None, None, "decode_failed")
    ref_core = list(reference[trim : len(reference) - trim])
    if len(ref_core) < MIN_REFERENCE_HASHES:
        return FingerprintMatch("no_match", None, None, None, "reference_quiet")

    valid = [
        h
        for h in ref_core
        if MIN_LOW_ENTROPY_POPCOUNT <= _popcount(h) <= MAX_LOW_ENTROPY_POPCOUNT
    ]
    if len(valid) < MIN_VALID_HASH_FRACTION * len(ref_core) or len(set(ref_core)) < MIN_REFERENCE_HASHES:
        return FingerprintMatch("no_match", None, None, None, "reference_quiet")

    if len(candidate) < len(ref_core):
        return FingerprintMatch("no_match", None, None, None, "candidate_no_audio")

    total_bits = 32 * len(ref_core)
    scores: list[tuple[float, int]] = []
    for j in range(trim, len(candidate) - len(ref_core) + 1):
        distance = sum(
            _popcount(a ^ b)
            for a, b in zip(ref_core, candidate[j : j + len(ref_core)], strict=True)
        )
        scores.append((1.0 - distance / total_bits, j))
    if not scores:
        return FingerprintMatch("no_match", None, None, None, "candidate_no_audio")

    best_confidence, best_j = max(scores)
    distinct = [s for s in scores if abs(s[1] - best_j) > 2 * rate]
    second_confidence = max((s[0] for s in distinct), default=0.0)
    margin = best_confidence - second_confidence
    candidate_offset = (best_j - trim) / rate

    if best_confidence >= MATCHED_CONFIDENCE and margin >= MATCHED_MARGIN:
        return FingerprintMatch(
            "matched", candidate_offset, best_confidence, margin, None
        )
    if best_confidence >= AMBIGUOUS_FLOOR:
        return FingerprintMatch(
            "ambiguous", candidate_offset, best_confidence, margin, "low_confidence"
        )
    return FingerprintMatch(
        "no_match", None, best_confidence, margin, "low_confidence"
    )


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as src:
            frame_rate = src.getframerate()
            if frame_rate <= 0:
                return 0.0
            return src.getnframes() / frame_rate
    except (OSError, EOFError, wave.Error):
        return 0.0


async def _run_command(args: list[str], *, timeout: float) -> tuple[int, bytes, bytes]:
    """Run one media tool with a hard timeout and no shell.

    The child is killed on timeout so a slow upstream cannot leave an
    ffmpeg process behind after the route's budget expires.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b"", b"timeout"
    except asyncio.CancelledError:
        # The route's 45 s budget can fire mid-tool; kill the child so a
        # cancelled request never leaves ffmpeg running.
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout or b"", stderr or b""


#: The external binaries :class:`ChromaprintAligner` shells out to, in
#: report order. Pinned against the constructor defaults by the suite.
ALIGNER_TOOLS: tuple[str, ...] = ("ffmpeg", "fpcalc")


def tool_paths() -> dict[str, str | None]:
    """Resolve the aligner's external tools the way ``align`` does.

    ``shutil.which`` against THIS process's own PATH — the answer the
    service itself will get, not the operator's shell. systemd's default
    service PATH excludes ``~/.local/bin``, which is how a user-local
    ``fpcalc`` that works interactively stays invisible to the aligner
    (the drop-in fix is DEPLOY.md §9.5). ``/api/health`` reports this so
    an operator sees the state without running ``deploy/smoke.sh``.
    """
    return {tool: shutil.which(tool) for tool in ALIGNER_TOOLS}


class ChromaprintAligner:
    """The production aligner: ffmpeg decode + Chromaprint fingerprints."""

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        fpcalc: str = "fpcalc",
        command_timeout_s: float = COMMAND_TIMEOUT_S,
    ) -> None:
        self._ffmpeg = ffmpeg
        self._fpcalc = fpcalc
        self._command_timeout_s = command_timeout_s

    async def align(
        self,
        *,
        reference_wav: bytes,
        candidate: StreamResponse,
        position_seconds: float,
    ) -> AlignmentResult:
        if shutil.which(self._ffmpeg) is None or shutil.which(self._fpcalc) is None:
            raise TeleportUnavailable("ffmpeg and fpcalc are required for teleport")

        with tempfile.TemporaryDirectory(prefix="cs-uk-teleport-") as tmp:
            workdir = Path(tmp)
            os.chmod(workdir, 0o700)
            reference_path = workdir / "reference.wav"
            reference_path.write_bytes(reference_wav)
            os.chmod(reference_path, 0o600)

            reference_decoded = workdir / "reference.pcm.wav"
            if not await self._decode_reference(reference_path, reference_decoded):
                return AlignmentResult("no_match", reason="decode_failed")
            os.chmod(reference_decoded, 0o600)

            reference_duration = _wav_duration(reference_decoded)
            candidate_start = max(0.0, position_seconds - SEARCH_RADIUS_SECONDS)
            candidate_decoded = workdir / "candidate.pcm.wav"
            if not await self._decode_candidate(
                candidate,
                candidate_decoded,
                start_seconds=candidate_start,
            ):
                return AlignmentResult("no_match", reason="candidate_no_audio")
            os.chmod(candidate_decoded, 0o600)

            candidate_duration = _wav_duration(candidate_decoded)
            reference_fingerprint = await self._fingerprint(reference_decoded)
            candidate_fingerprint = await self._fingerprint(candidate_decoded)
            if reference_fingerprint is None or candidate_fingerprint is None:
                return AlignmentResult("no_match", reason="decode_failed")

            # The sliding comparison is CPU-bound; keep it off the loop so
            # a second viewer's request is not blocked by this one.
            match = await asyncio.to_thread(
                match_fingerprints,
                reference_fingerprint,
                candidate_fingerprint,
                reference_duration=reference_duration,
                candidate_duration=candidate_duration,
            )
            if match.status == "no_match" or match.candidate_offset_seconds is None:
                return AlignmentResult(
                    match.status,
                    confidence=match.confidence,
                    margin=match.margin,
                    reason=match.reason,
                )

            # The matched reference start relative to the candidate
            # window; the anchor is the reference clip's END, which is
            # the viewer's failure position.
            offset_seconds = (
                candidate_start
                + match.candidate_offset_seconds
                + reference_duration
                - position_seconds
            )
            return AlignmentResult(
                match.status,
                offset_seconds=offset_seconds,
                confidence=match.confidence,
                margin=match.margin,
                reason=match.reason,
            )

    async def _decode_reference(self, source: Path, target: Path) -> bool:
        return await self._decode(
            source,
            target,
            start_seconds=None,
        )

    async def _decode_candidate(
        self,
        candidate: StreamResponse,
        target: Path,
        *,
        start_seconds: float,
    ) -> bool:
        return await self._decode(
            candidate.url,
            target,
            start_seconds=start_seconds,
            duration_seconds=SEARCH_RADIUS_SECONDS * 2 + WINDOW_SECONDS,
            headers=candidate.headers,
        )

    async def _decode(
        self,
        source: Path | str,
        target: Path,
        *,
        start_seconds: float | None,
        duration_seconds: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> bool:
        args = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
        ]
        if start_seconds is not None:
            args += ["-ss", f"{start_seconds:.3f}"]
        if duration_seconds is not None:
            args += ["-t", f"{duration_seconds:.3f}"]
        if headers:
            header_lines = "".join(
                f"{key}: {value}\r\n"
                for key, value in headers.items()
                if key.lower() != "user-agent"
            )
            if header_lines:
                args += ["-headers", header_lines]
            user_agent = next(
                (value for key, value in headers.items() if key.lower() == "user-agent"),
                None,
            )
            if user_agent:
                args += ["-user_agent", user_agent]
        args += [
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "11025",
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
        code, _stdout, _stderr = await _run_command(
            args, timeout=self._command_timeout_s
        )
        return code == 0 and target.exists() and target.stat().st_size > 44

    async def _fingerprint(self, path: Path) -> list[int] | None:
        args = [
            self._fpcalc,
            "-raw",
            "-json",
            "-rate",
            "11025",
            "-channels",
            "1",
            # fpcalc defaults to the first 120 s only. The candidate
            # window is ~10 minutes, so without this the reference at
            # position-300..position+315 is simply not searched.
            "-length",
            "0",
            str(path),
        ]
        code, stdout, _stderr = await _run_command(
            args, timeout=self._command_timeout_s
        )
        if code != 0:
            return None
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError):
            return None
        fingerprint = payload.get("fingerprint")
        if not isinstance(fingerprint, list) or not fingerprint:
            return None
        if not all(isinstance(value, int) for value in fingerprint):
            return None
        # fpcalc's raw hashes are uint32; normalise defensively so a
        # signed producer cannot corrupt the XOR/popcount comparison.
        return [value & 0xFFFFFFFF for value in fingerprint]
