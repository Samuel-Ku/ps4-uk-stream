"""Pure helpers for the live gate (issue #30, grilling Q2/Q4).

Kept in a Python module (not inline bash) so the decision rules are
unit-testable. ``scripts/gate.sh`` shells out to these via
``python -m cs_uk_api.gate_tools``.

Two jobs:

1. ``scan_js_markers`` — diagnostic mode (Q2). When mpv fails on a
   provider, the gate captures the player HTML and scans it for markers
   of JS-generated streams (``eval(``, ``Function(``, ``atob(``,
   obfuscated). A "not portable" verdict is only issued on real
   evidence — clean HTML means upstream change / network issue, not
   portability.
2. ``parse_ffprobe`` — playability profile (Q4). ffprobe output
   (codec/resolution/bitrate) is classified against what the PS4's mpv
   software decoder can realistically handle. Anything that is not
   H.264 is flagged ``soft_decode_risk``.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Any

# (marker label, regex). The regexes require a real call/word — the
# `evaluation` / `functionality` false positives must not match.
_JS_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("eval(", re.compile(r"\beval\s*\(")),
    ("Function(", re.compile(r"\bFunction\s*\(")),
    ("atob(", re.compile(r"\batob\s*\(")),
    ("obfuscated", re.compile(r"obfuscated", re.IGNORECASE)),
)


def scan_js_markers(html: str) -> list[str]:
    """Return the JS-generation markers found in ``html``, in scan order."""
    return [label for label, pattern in _JS_MARKERS if pattern.search(html)]


@dataclass(frozen=True)
class PlayabilityProfile:
    codec: str | None
    width: int | None = None
    height: int | None = None
    bitrate_kbps: int | None = None
    soft_decode_risk: bool = False

    def __str__(self) -> str:
        res = f"{self.width}x{self.height}" if self.width and self.height else "?"
        br = f"{self.bitrate_kbps}kbps" if self.bitrate_kbps else "?kbps"
        return f"{self.codec or 'unknown'} {res} {br}"


def _as_kbps(bit_rate: str | int | None) -> int | None:
    if not bit_rate:
        return None
    try:
        return int(int(bit_rate) / 1000)
    except (TypeError, ValueError):
        return None


def parse_ffprobe(payload: dict[str, Any]) -> PlayabilityProfile:
    """Classify ffprobe JSON (``-show_streams -show_format``) for the PS4.

    Risk rule (Q4): anything that is not H.264 is marked
    ``soft_decode_risk`` — the PS4 mpv build decodes in software, and
    H.264 is the only codec we can commit to. Unknown codec also counts
    as a risk (cannot trust it on PS4).
    """
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return PlayabilityProfile(codec=None, soft_decode_risk=True)
    fmt = payload.get("format") or {}
    codec = video.get("codec_name")
    bitrate = _as_kbps(video.get("bit_rate")) or _as_kbps(fmt.get("bit_rate"))
    return PlayabilityProfile(
        codec=codec,
        width=video.get("width"),
        height=video.get("height"),
        bitrate_kbps=bitrate,
        soft_decode_risk=codec != "h264",
    )


def _read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _main(argv: list[str]) -> int:
    """CLI used by scripts/gate.sh.

    Commands:
      scan <html-file|->      — print JS-generation markers (one per line)
      profile <json-file|->   — print the playability profile line
    """
    if len(argv) < 3:
        print("usage: gate_tools.py (scan|profile) <file|->", file=sys.stderr)
        return 2
    cmd, path = argv[1], argv[2]
    text = _read_input(path)
    if cmd == "scan":
        markers = scan_js_markers(text)
        print("\n".join(markers) if markers else "clean")
        return 0
    if cmd == "profile":
        profile = parse_ffprobe(json.loads(text))
        risk = "ps4-soft-decode-risk" if profile.soft_decode_risk else "ok"
        print(f"{profile} | {risk}")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
