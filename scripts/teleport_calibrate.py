#!/usr/bin/env python3
"""Repeatable live calibration for ``POST /api/teleport/align``.

Given a reference item id and one candidate id, this operator script:

1. resolves both playable ids through ``/api/stream``;
2. captures the same window-length decoded-audio clip from the reference
   source at each requested position with ``ffmpeg``;
3. posts each clip once to the live align route on the cold path (no prior
   search) and prints one Markdown verdict row per position. ``--warm``
   opts into the historical title-search warm-up and its cold-cache retry
   for backends that predate cold-pair resolution.

The backend's own route does the candidate validation, HLS decode and
Chromaprint alignment. This script never imports ``cs_uk_api``; it treats
the backend as a black box and uses only the Python stdlib plus ``ffmpeg``.

Usage:
    python3 scripts/teleport_calibrate.py ITEM_ID CANDIDATE_ID \
        --position 120 --position 600 --position 1500

Exit codes: 0 = every position returned a 200 verdict; 1 = at least one
position failed or returned an error; 2 = usage/precondition failure
(bad position, missing ffmpeg, unplayable id).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

DEFAULT_BASE_URL = "http://127.0.0.1:8003"
DEFAULT_POSITION_SECONDS = 600.0
DEFAULT_WINDOW_SECONDS = 15.0
MIN_WINDOW_SECONDS = 10.0
MAX_WINDOW_SECONDS = 20.0
DEFAULT_TIMEOUT_S = 75.0
SEARCH_TIMEOUT_S = 120.0
FFMPEG_TIMEOUT_S = 120.0
TICKS_PER_SECOND = 10_000_000
COLD_CACHE_MARKER = "both ids must be series episodes"

#: Episode wire ids end in ``:s1e1`` or ``:e1`` (optionally followed by a
#: provider blob). Mirrors the backend grammar without importing it.
EPISODE_TAIL_RE = re.compile(r":(?:s\d+)?e\d+(?=:|$)")
_ONLINE_SUFFIX_RE = re.compile(r"\s+дивитись\s+онлайн\s*$", re.IGNORECASE)
_BRACKET_YEAR_RE = re.compile(r"\s*[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]\s*$")

VERDICT_STATUSES = frozenset({"matched", "ambiguous", "no_match"})


class CalibrationError(RuntimeError):
    """A precondition or transport failure the operator can act on."""


@dataclass(frozen=True)
class StreamInfo:
    """The playable stream the script needs to capture the reference clip."""

    url: str
    type: str
    headers: dict[str, str]


@dataclass(frozen=True)
class Verdict:
    """One position's request outcome, including transport-only failures."""

    position: float
    status: str
    offset_seconds: float | None
    confidence: float | None
    margin: float | None
    seek_ticks: int | None
    reason: str | None
    latency_s: float
    http_status: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the route returned a recognized three-way verdict."""
        return (
            self.http_status == 200
            and self.status in VERDICT_STATUSES
            and self.error is None
        )


# ---------------------------------------------------------------------------
# Wire helpers: URLs, JSON, multipart
# ---------------------------------------------------------------------------


def _json_body(raw: bytes) -> dict[str, Any]:
    """Decode a JSON object, degrading a non-JSON error body to ``detail``."""
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {"detail": raw.decode("utf-8", "replace")[:500]}
    return payload if isinstance(payload, dict) else {"detail": payload}


def _request_json(
    url: str, *, timeout: float
) -> tuple[int, dict[str, Any]]:
    """GET a JSON object, keeping HTTP error bodies for the caller."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _json_body(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json_body(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CalibrationError(f"request failed: {url}: {exc}") from exc


def error_message(payload: dict[str, Any]) -> str:
    """The route's ``detail`` as one readable line (code + message)."""
    detail = payload.get("detail", payload)
    if isinstance(detail, dict):
        code = detail.get("error")
        message = detail.get("message")
        if code and message:
            return f"{code}: {message}"
        if message:
            return str(message)
        if code:
            return str(code)
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False)[:500]


def multipart_body(
    fields: Sequence[tuple[str, str]],
    filename: str,
    file_bytes: bytes,
    boundary: str,
) -> bytes:
    """Build the align route's multipart body (fields first, audio last)."""
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n'
                f"\r\n{value}\r\n"
            ).encode()
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="audio"; '
            f'filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
    )
    chunks.append(file_bytes)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks)


def post_align(
    base_url: str,
    item_id: str,
    candidate_id: str,
    position: float,
    window: float,
    clip: bytes,
    filename: str,
    *,
    timeout: float,
) -> tuple[int, dict[str, Any], float]:
    """POST one clip to the live route and return (status, body, seconds)."""
    boundary = f"----teleport-calibration-{uuid.uuid4().hex}"
    fields = [
        ("item_id", item_id),
        ("candidate_id", candidate_id),
        ("position_ticks", str(round(position * TICKS_PER_SECOND))),
        ("window_seconds", f"{window:g}"),
    ]
    body = multipart_body(fields, filename, clip, boundary)
    request = urllib.request.Request(
        f"{base_url}/api/teleport/align",
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CalibrationError(
            f"align request failed for {position:g}s: {exc}"
        ) from exc
    return status, _json_body(raw), time.monotonic() - started


# ---------------------------------------------------------------------------
# Provider ids, stream resolution, and the legacy warm-up
# ---------------------------------------------------------------------------


def content_id(item_id: str) -> str:
    """Strip an episode tail from a wire id, leaving ``provider:external``."""
    match = EPISODE_TAIL_RE.search(item_id)
    return item_id[: match.start()] if match is not None else item_id


def search_title(raw: str) -> str:
    """A provider content title reduced to a useful search query."""
    title = raw.split(" / ", 1)[0].strip()
    title = _BRACKET_YEAR_RE.sub("", title)
    title = _ONLINE_SUFFIX_RE.sub("", title)
    return title.strip()


def resolve_stream(base_url: str, item_id: str, *, timeout: float) -> StreamInfo:
    """Resolve one playable id through ``/api/stream``."""
    path = "/api/stream/" + urllib.parse.quote(item_id, safe=":/")
    status, payload = _request_json(f"{base_url}{path}", timeout=timeout)
    if status != 200:
        raise CalibrationError(
            f"stream {item_id}: HTTP {status} {error_message(payload)}"
        )
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise CalibrationError(f"stream {item_id}: response has no url")
    raw_headers = payload.get("headers")
    headers = (
        {str(key): str(value) for key, value in raw_headers.items()}
        if isinstance(raw_headers, dict)
        else {}
    )
    return StreamInfo(
        url=url,
        type=str(payload.get("type") or ""),
        headers=headers,
    )


def warm_pair(
    base_url: str,
    item_id: str,
    candidate_id: str,
    *,
    timeout: float,
    log: TextIO | None = None,
) -> bool:
    """Register both works in the route's group map via title searches.

    Legacy compatibility step (``--warm``): the align route resolves a
    cold pair from the two content pages, so a normal calibration does
    not warm anything. Resolving each content title and searching it
    remains the cheapest way to warm exactly these two cards on an older
    backend, and it exercises the search path.

    Returns True when at least one title was searched successfully. A
    known provider-duplicate limit remains: a group with two items from the
    same provider registers only one of them.
    """
    output = log or sys.stderr
    titles: list[str] = []
    ok = False
    for label, wire_id in (("reference", item_id), ("candidate", candidate_id)):
        path = "/api/content/" + urllib.parse.quote(content_id(wire_id), safe=":/")
        try:
            status, payload = _request_json(f"{base_url}{path}", timeout=timeout)
        except CalibrationError as exc:
            print(f"warning: cannot read {label} content for warm-up: {exc}", file=output)
            continue
        if status != 200:
            print(
                f"warning: cannot read {label} content for warm-up: "
                f"HTTP {status} {error_message(payload)}",
                file=output,
            )
            continue
        title = payload.get("title")
        if not isinstance(title, str):
            continue
        cleaned = search_title(title)
        if cleaned and cleaned not in titles:
            titles.append(cleaned)

    if not titles:
        print("warning: no content titles resolved; group map not warmed", file=output)
        return False

    for title in titles:
        query = urllib.parse.urlencode({"q": title})
        print(f"warming group map: {title}", file=output)
        try:
            status, payload = _request_json(
                f"{base_url}/api/search?{query}", timeout=SEARCH_TIMEOUT_S
            )
        except CalibrationError as exc:
            print(f"warning: warm-up search failed: {exc}", file=output)
            continue
        if status != 200:
            print(
                f"warning: warm-up search failed: HTTP {status} "
                f"{error_message(payload)}",
                file=output,
            )
            continue
        ok = True
        if not payload.get("groups"):
            print(f"warning: no search groups for {title!r}", file=output)
    return ok


# ---------------------------------------------------------------------------
# Clip capture
# ---------------------------------------------------------------------------


def ffmpeg_command(
    ffmpeg: str,
    stream: StreamInfo,
    position: float,
    window: float,
    out_path: Path,
) -> list[str]:
    """The exact ffmpeg argv for the reference clip ending at ``position``."""
    headers = "".join(
        f"{key}: {value}\r\n" for key, value in stream.headers.items()
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if headers:
        command += ["-headers", headers]
    command += [
        "-ss",
        f"{position - window:.3f}",
        "-t",
        f"{window:.3f}",
        "-i",
        stream.url,
        "-vn",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(out_path),
    ]
    return command


def validate_clip(path: Path, window: float) -> float:
    """Check the captured WAV is the server's accepted PCM shape/duration."""
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            frame_rate = wav.getframerate()
            duration = wav.getnframes() / frame_rate if frame_rate else 0.0
    except (OSError, EOFError, wave.Error) as exc:
        raise CalibrationError(f"captured clip is not a readable WAV: {exc}") from exc
    if sample_width != 2 or channels not in (1, 2) or frame_rate <= 0:
        raise CalibrationError(
            "captured clip must be 16-bit PCM with 1-2 channels "
            f"(got {sample_width * 8}-bit, {channels} ch, {frame_rate} Hz)"
        )
    if not MIN_WINDOW_SECONDS <= duration <= MAX_WINDOW_SECONDS:
        raise CalibrationError(
            f"captured clip is {duration:.3f}s; the route accepts "
            f"{MIN_WINDOW_SECONDS:g}-{MAX_WINDOW_SECONDS:g}s"
        )
    return duration


def capture_clip(
    ffmpeg: str,
    stream: StreamInfo,
    position: float,
    window: float,
    out_path: Path,
) -> float:
    """Run ffmpeg and return the captured clip's duration."""
    command = ffmpeg_command(ffmpeg, stream, position, window, out_path)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CalibrationError(f"ffmpeg not found: {ffmpeg}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CalibrationError(
            f"ffmpeg timed out capturing {position:g}s"
        ) from exc
    if result.returncode != 0:
        detail = " | ".join(
            line for line in result.stderr.strip().splitlines()[-8:] if line
        )
        raise CalibrationError(
            f"ffmpeg failed for {position:g}s"
            + (f": {detail}" if detail else "")
        )
    return validate_clip(out_path, window)


# ---------------------------------------------------------------------------
# Verdict rows and table rendering
# ---------------------------------------------------------------------------


def _number(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _offset(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f} s"


def _seek(ticks: int | None) -> str:
    return "—" if ticks is None else f"{ticks / TICKS_PER_SECOND:.4f} s"


def _latency(value: float) -> str:
    return "—" if value <= 0 else f"{value:.2f} s"


def format_table(verdicts: Sequence[Verdict]) -> str:
    """Render the operator verdict table as Markdown."""
    headers = [
        "#",
        "Position",
        "Status",
        "Offset",
        "Confidence",
        "Margin",
        "Seek",
        "Request",
    ]
    alignments = ["r", "r", "l", "r", "r", "r", "r", "r"]
    rows: list[list[str]] = []
    for index, verdict in enumerate(verdicts, 1):
        status = (
            f"error {verdict.http_status}"
            if verdict.http_status not in (None, 200)
            else verdict.status
        )
        rows.append(
            [
                str(index),
                f"{verdict.position:g} s",
                status,
                _offset(verdict.offset_seconds),
                _number(verdict.confidence),
                _number(verdict.margin),
                _seek(verdict.seek_ticks),
                _latency(verdict.latency_s),
            ]
        )

    widths = [
        max([len(headers[column]), *(len(row[column]) for row in rows)])
        for column in range(len(headers))
    ]

    def render(row: Sequence[str]) -> str:
        cells = []
        for column, cell in enumerate(row):
            if alignments[column] == "r":
                cells.append(cell.rjust(widths[column]))
            else:
                cells.append(cell.ljust(widths[column]))
        return "| " + " | ".join(cells) + " |"

    separator = "|" + "|".join(
        "---:" if alignment == "r" else "---"
        for alignment in alignments
    ) + "|"
    return "\n".join([render(headers), separator, *(render(row) for row in rows)])


def _range(values: Sequence[float], digits: int) -> str:
    if not values:
        return "—"
    low, high = min(values), max(values)
    if low == high:
        return f"{low:.{digits}f}"
    return f"{low:.{digits}f}–{high:.{digits}f}"


def summarize(verdicts: Sequence[Verdict]) -> str:
    """One-line count/range summary over all requested positions."""
    valid = [verdict for verdict in verdicts if verdict.ok]
    errors = [verdict for verdict in verdicts if not verdict.ok]
    matched = sum(verdict.status == "matched" for verdict in valid)
    ambiguous = sum(verdict.status == "ambiguous" for verdict in valid)
    no_match = sum(verdict.status == "no_match" for verdict in valid)
    parts = [
        f"{len(valid)}/{len(verdicts)} returned 200",
        f"matched={matched}",
        f"ambiguous={ambiguous}",
        f"no_match={no_match}",
    ]
    if errors:
        parts.append(f"errors={len(errors)}")
    confidences = [
        verdict.confidence
        for verdict in valid
        if verdict.confidence is not None
    ]
    margins = [verdict.margin for verdict in valid if verdict.margin is not None]
    latencies = [
        verdict.latency_s for verdict in verdicts if verdict.latency_s > 0
    ]
    if confidences:
        parts.append(f"confidence {_range(confidences, 4)}")
    if margins:
        parts.append(f"margin {_range(margins, 4)}")
    if latencies:
        parts.append(f"request {_range(latencies, 2)} s")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def validate_positions(positions: Sequence[float], window: float) -> list[float]:
    """Reject positions that cannot produce a full reference window."""
    if not positions:
        raise ValueError("at least one --position is required")
    for position in positions:
        if position < window:
            raise ValueError(
                f"position {position:g}s is shorter than the "
                f"{window:g}s window"
            )
    return list(positions)


def _validate_id(parser: argparse.ArgumentParser, label: str, item_id: str) -> None:
    if EPISODE_TAIL_RE.search(item_id) is None:
        parser.error(
            f"{label} id must be a series episode wire id "
            "(…:s1e1 or …:e1)"
        )


def _write_json(
    path: Path,
    base_url: str,
    item_id: str,
    candidate_id: str,
    window: float,
    reference: StreamInfo,
    candidate: StreamInfo,
    verdicts: Sequence[Verdict],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": base_url,
        "item_id": item_id,
        "candidate_id": candidate_id,
        "window_seconds": window,
        "reference": {"type": reference.type},
        "candidate": {"type": candidate.type},
        "results": [
            {
                "position_seconds": verdict.position,
                "status": verdict.status,
                "http_status": verdict.http_status,
                "offset_seconds": verdict.offset_seconds,
                "confidence": verdict.confidence,
                "margin": verdict.margin,
                "seek_ticks": verdict.seek_ticks,
                "reason": verdict.reason,
                "request_seconds": round(verdict.latency_s, 6),
                "error": verdict.error,
            }
            for verdict in verdicts
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Capture reference clips and time the live teleport align route."
        )
    )
    parser.add_argument("item_id", help="reference series-episode wire id (A)")
    parser.add_argument(
        "candidate_id", help="candidate series-episode wire id (B)"
    )
    parser.add_argument(
        "--position",
        action="append",
        type=float,
        default=None,
        help=(
            "playback position in seconds; repeat for a multi-position run "
            f"(default {DEFAULT_POSITION_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--window",
        type=float,
        default=DEFAULT_WINDOW_SECONDS,
        help=f"clip length in seconds (default {DEFAULT_WINDOW_SECONDS:g})",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CS_UK_BASE_URL", DEFAULT_BASE_URL),
        help=f"backend base URL (default {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--ffmpeg",
        default=os.environ.get("FFMPEG", "ffmpeg"),
        help="ffmpeg executable (default: $FFMPEG or ffmpeg)",
    )
    parser.add_argument(
        "--clip-dir",
        type=Path,
        default=None,
        help="keep captured clips in this directory (default: temporary)",
    )
    parser.add_argument(
        "--warm",
        action="store_true",
        help=(
            "warm the group map with title searches before posting and "
            "retry once on a cold-cache 400 (default: cold path, no warm-up)"
        ),
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="write the raw verdicts as JSON",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT_S:g})",
    )
    args = parser.parse_args(argv)

    if not MIN_WINDOW_SECONDS <= args.window <= MAX_WINDOW_SECONDS:
        parser.error(
            f"--window must be {MIN_WINDOW_SECONDS:g}-{MAX_WINDOW_SECONDS:g}"
        )
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    positions = [DEFAULT_POSITION_SECONDS] if args.position is None else args.position
    try:
        positions = validate_positions(positions, args.window)
    except ValueError as exc:
        parser.error(str(exc))
    _validate_id(parser, "reference", args.item_id)
    _validate_id(parser, "candidate", args.candidate_id)

    ffmpeg = shutil.which(args.ffmpeg)
    if ffmpeg is None:
        print(f"error: ffmpeg not found: {args.ffmpeg}", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    print("Teleport calibration")
    print(f"  backend   {base_url}")
    print(f"  reference {args.item_id}")
    print(f"  candidate {args.candidate_id}")
    print(f"  window    {args.window:g} s")
    print(f"  positions {', '.join(f'{position:g}' for position in positions)} s")
    print(f"  path      {'warm-up + cold retry' if args.warm else 'cold'}")

    try:
        reference = resolve_stream(base_url, args.item_id, timeout=args.timeout)
        candidate = resolve_stream(
            base_url, args.candidate_id, timeout=args.timeout
        )
    except CalibrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"  A stream  {reference.type or 'unknown'}")
    print(f"  B stream  {candidate.type or 'unknown'}")

    if args.warm:
        print("warming the route's group map", file=sys.stderr)
        try:
            warm_pair(
                base_url,
                args.item_id,
                args.candidate_id,
                timeout=args.timeout,
            )
        except CalibrationError as exc:
            print(f"warning: warm-up failed: {exc}", file=sys.stderr)

    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.clip_dir is None:
            temporary = tempfile.TemporaryDirectory(
                prefix="teleport-calibration-"
            )
            clip_dir = Path(temporary.name)
        else:
            clip_dir = args.clip_dir
            clip_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot create clip directory: {exc}", file=sys.stderr)
        return 2

    verdicts: list[Verdict] = []
    try:
        for index, position in enumerate(positions, 1):
            print(
                f"[{index}/{len(positions)}] {position:g}s: capturing clip",
                file=sys.stderr,
            )
            clip_path = clip_dir / f"reference-{position:g}s.wav"
            try:
                capture_clip(
                    ffmpeg,
                    reference,
                    position,
                    args.window,
                    clip_path,
                )
                clip = clip_path.read_bytes()
            except (CalibrationError, OSError) as exc:
                verdicts.append(
                    Verdict(
                        position=position,
                        status="error",
                        offset_seconds=None,
                        confidence=None,
                        margin=None,
                        seek_ticks=None,
                        reason=None,
                        latency_s=0.0,
                        error=f"clip capture failed: {exc}",
                    )
                )
                continue

            print(
                f"[{index}/{len(positions)}] {position:g}s: posting "
                f"{len(clip) / 1024:.0f} KiB",
                file=sys.stderr,
            )
            try:
                status, payload, latency = post_align(
                    base_url,
                    args.item_id,
                    args.candidate_id,
                    position,
                    args.window,
                    clip,
                    clip_path.name,
                    timeout=args.timeout,
                )
                if (
                    status == 400
                    and COLD_CACHE_MARKER in error_message(payload)
                    and args.warm
                ):
                    print(
                        "cold group map; warming and retrying once",
                        file=sys.stderr,
                    )
                    warm_pair(
                        base_url,
                        args.item_id,
                        args.candidate_id,
                        timeout=args.timeout,
                    )
                    status, payload, latency = post_align(
                        base_url,
                        args.item_id,
                        args.candidate_id,
                        position,
                        args.window,
                        clip,
                        clip_path.name,
                        timeout=args.timeout,
                    )
            except CalibrationError as exc:
                verdicts.append(
                    Verdict(
                        position=position,
                        status="error",
                        offset_seconds=None,
                        confidence=None,
                        margin=None,
                        seek_ticks=None,
                        reason=None,
                        latency_s=0.0,
                        error=str(exc),
                    )
                )
                continue

            if status != 200:
                message = error_message(payload)
                if status == 400 and COLD_CACHE_MARKER in message:
                    if args.warm:
                        message += (
                            " (group map still cold after warm-up; a group "
                            "with two items from the same provider registers "
                            "only one)"
                        )
                    else:
                        message += (
                            " (cold resolution failed; retry with --warm "
                            "on a backend without cold-pair resolution)"
                        )
                verdicts.append(
                    Verdict(
                        position=position,
                        status="error",
                        offset_seconds=None,
                        confidence=None,
                        margin=None,
                        seek_ticks=None,
                        reason=None,
                        latency_s=latency,
                        http_status=status,
                        error=message,
                    )
                )
                continue

            status_value = payload.get("status")
            if status_value not in VERDICT_STATUSES:
                verdicts.append(
                    Verdict(
                        position=position,
                        status="error",
                        offset_seconds=None,
                        confidence=None,
                        margin=None,
                        seek_ticks=None,
                        reason=None,
                        latency_s=latency,
                        http_status=status,
                        error=f"unrecognized status: {status_value!r}",
                    )
                )
                continue

            offset = payload.get("offset_seconds")
            confidence = payload.get("confidence")
            margin = payload.get("margin")
            seek_ticks = payload.get("seek_ticks")
            verdicts.append(
                Verdict(
                    position=position,
                    status=str(status_value),
                    offset_seconds=(
                        float(offset) if isinstance(offset, (int, float)) else None
                    ),
                    confidence=(
                        float(confidence)
                        if isinstance(confidence, (int, float))
                        else None
                    ),
                    margin=(
                        float(margin)
                        if isinstance(margin, (int, float))
                        else None
                    ),
                    seek_ticks=(
                        int(seek_ticks)
                        if isinstance(seek_ticks, int)
                        else None
                    ),
                    reason=(
                        str(payload["reason"])
                        if payload.get("reason") is not None
                        else None
                    ),
                    latency_s=latency,
                    http_status=status,
                )
            )
    finally:
        if temporary is not None:
            temporary.cleanup()

    print()
    print(format_table(verdicts))
    print()
    print(summarize(verdicts))

    errors = [verdict for verdict in verdicts if not verdict.ok]
    if errors:
        print()
        print("Errors:")
        for verdict in errors:
            print(f"  {verdict.position:g} s: {verdict.error}")

    if args.json_out is not None:
        try:
            _write_json(
                args.json_out,
                base_url,
                args.item_id,
                args.candidate_id,
                args.window,
                reference,
                candidate,
                verdicts,
            )
        except OSError as exc:
            print(f"warning: could not write JSON: {exc}", file=sys.stderr)
            return 1
        print()
        print(f"JSON: {args.json_out}")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
