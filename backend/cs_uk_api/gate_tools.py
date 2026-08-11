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


def fallback_episode_cid(content: dict[str, Any], provider: str) -> str:
    """First streamable episode id of a content payload, caller-prefixed.

    Series-only providers (serialno, anitubeinua, doramyworld,
    simpsonsuatv) reject a bare search id at /api/stream — the client
    must send an episode id. When the bare-id stream fails, gate.sh
    falls back to the first episode exposed by content()'s ``seasons``
    (ticket #142, issue #127).

    The returned wire id follows the same ``<provider>:<external>``
    shape the client sends, so episode ids that already carry the
    ``<provider>:`` prefix (simpsonsuatv's full episode-page URL) pass
    through unchanged, and bare ids get the CALLER's provider prefix
    (the caller owns the stream URL, not the content payload). Empty
    string when there is no usable episode — the caller advances to the
    next search result.
    """
    seasons = content.get("seasons") or []
    episodes = seasons[0].get("episodes") or [] if seasons else []
    if not episodes:
        return ""
    ep_id = episodes[0].get("id") or ""
    if not ep_id:
        return ""
    prefix = f"{provider}:"
    return ep_id if ep_id.startswith(prefix) else prefix + ep_id


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
      fallback <json-file|-> <provider>
                              — print the first-episode wire cid, or an
                                empty line when there is no episode
    """
    if len(argv) < 3:
        print("usage: gate_tools.py (scan|profile|fallback) …", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "scan":
        if len(argv) < 3:
            print("usage: gate_tools.py scan <file|->", file=sys.stderr)
            return 2
        markers = scan_js_markers(_read_input(argv[2]))
        print("\n".join(markers) if markers else "clean")
        return 0
    if cmd == "profile":
        if len(argv) < 3:
            print("usage: gate_tools.py profile <file|->", file=sys.stderr)
            return 2
        profile = parse_ffprobe(json.loads(_read_input(argv[2])))
        risk = "ps4-soft-decode-risk" if profile.soft_decode_risk else "ok"
        print(f"{profile} | {risk}")
        return 0
    if cmd == "fallback":
        if len(argv) < 4:
            print("usage: gate_tools.py fallback <json-file|-> <provider>", file=sys.stderr)
            return 2
        cid = fallback_episode_cid(json.loads(_read_input(argv[2])), argv[3])
        print(cid)
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
