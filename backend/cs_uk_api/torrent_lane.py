"""The torrent lane conversation (spec #374, tickets #377/#379/#378).

ONE owner for the torrent lane's policy: popcorn ``torrents[]`` dicts →
:class:`TorrentCandidate` parsing, the quality policy ordering, the
bounded dead-swarm fallback window, the fork's magnet convention, and
the #378 wire shape (``EngineStream`` → :class:`StreamResponse`, VTT
subtitles riding verbatim). Pure functions over the
:class:`~cs_uk_api.torrent_engine.TorrentEngine` seam — the adapter
(:class:`~cs_uk_api.torrent_engine.BitPlayClient`, plus its CI
:class:`~cs_uk_api.torrent_engine.FakeTorrentEngine`) stays on the far
side of that seam; this module owns everything the PROVIDER has to
decide before handing the engine a magnet.

Import direction: ``torrent_lane`` → ``torrent_engine`` (the seam) +
``models`` (the wire) + ``providers.base`` (the shared error
vocabulary) — never the provider. The provider (yts) reads popcorn
payloads, records candidates, and calls :func:`ensure_any_session`
here; series-payload candidate parsing stays with the provider's
popcorn dialect.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .models import StreamResponse
from .providers.base import ProviderError
from .torrent_engine import (
    EngineRejected,
    EngineStream,
    TorrentEngine,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Magnet→session selection policy (#377) — PURE functions, no I/O.
#
# Spec #374 quality policy: the provider orders candidates by (quality
# tier, seeds desc) — 1080p preferred over 720p over everything else;
# within a tier more seeders win; full ties keep upstream order
# (deterministic pick). ONE magnet goes to the engine at a time; since
# the #373 live finding, a dead-on-arrival verdict (EngineRejected)
# advances to the NEXT candidate in that order, bounded by
# ``_MAX_SESSION_ATTEMPTS`` — lane-level failures never advance.
# ---------------------------------------------------------------------------

#: Quality tiers in preference order; anything unlisted shares the last
#: tier (2160p is deliberately NOT preferred in v1 — the player floor
#: this lane targets is 1080p).
_QUALITY_PREFERENCE: tuple[str, ...] = ("1080p", "720p")

#: Dead-on-arrival candidates tried per stream before item-level
#: ``not_found``; each attempt costs one engine add window
#: (``ADD_TIMEOUT_S``), so this bounds the slow path (~90 s worst case).
_MAX_SESSION_ATTEMPTS = 3

#: Tracker list convention of the fork, appended to every built magnet:
#: YTS hashes alone carry no announce URLs, so the engine's peer
#: discovery needs public tracker fallbacks alongside its DHT bootstrap
#: (same convention as the fork's own magnets / research #367 probe).
TRACKERS: tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
)


@dataclass(frozen=True)
class TorrentCandidate:
    """One usable ``torrents[]`` entry: quality + info-hash + swarm."""

    quality: str
    info_hash: str
    seeds: int


def _quality_rank(quality: str) -> int:
    try:
        return _QUALITY_PREFERENCE.index(quality)
    except ValueError:
        return len(_QUALITY_PREFERENCE)


def _policy_key(candidate: TorrentCandidate) -> tuple[int, int]:
    """The single policy ordering: quality tier, then seeds desc."""
    return (_quality_rank(candidate.quality), -candidate.seeds)


def select_torrent(candidates: list[TorrentCandidate]) -> TorrentCandidate | None:
    """The server-side pick under the decided policy.

    ``min`` is stable, so equal (tier, seeds) keys keep upstream's first
    listing — the deterministic tiebreak, identical to the fallback
    ordering in :func:`ensure_any_session`. ``None`` when nothing
    usable.
    """
    if not candidates:
        return None
    return min(candidates, key=_policy_key)


async def ensure_any_session(
    engine: TorrentEngine,
    candidates: list[TorrentCandidate],
    *,
    file_hint: str | None,
) -> EngineStream:
    """Try policy-ordered candidates until one yields a live session.

    A dead-on-arrival verdict (:class:`EngineRejected` — metadata never
    arrived in the add window) advances to the next candidate, bounded
    by ``_MAX_SESSION_ATTEMPTS``; each attempt costs the add's metadata
    window (:data:`ADD_TIMEOUT_S`), so the bound keeps the worst case
    fast. Lane-level failures (:class:`EngineUnavailable`) propagate
    immediately — the engine is down for every candidate, retrying
    would only mask the cause.
    """
    for candidate in sorted(candidates, key=_policy_key)[:_MAX_SESSION_ATTEMPTS]:
        identifier = (
            candidate.info_hash
            if candidate.info_hash.startswith("magnet:")
            else build_magnet(candidate.info_hash)
        )
        try:
            return await engine.ensure_session(identifier, file_hint=file_hint)
        except EngineRejected as e:
            log.warning(
                "swarm %s rejected (%s); falling back to the next candidate",
                candidate.info_hash[:8],
                e,
            )
    raise ProviderError("not_found", "no seeders or dead torrent")


def build_magnet(info_hash: str) -> str:
    """``magnet:?xt=urn:btih:<hash>&tr=…`` with the fork's trackers."""
    tr = "&".join(f"tr={quote(t, safe='')}" for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&{tr}"


def torrent_stream_response(result: EngineStream) -> StreamResponse:
    """``EngineStream`` → wire ``StreamResponse`` (#378).

    The engine is the TRUTH about what the file carries: its VTT
    subtitle endpoint rides along verbatim; an empty session (no srt)
    maps to the omitted (None) field — the wire stays byte-identical
    to the pre-#378 shape.
    """
    return StreamResponse(
        url=result.url,
        type="mp4",
        headers={},
        seekable=result.seekable,
        subtitle_url=result.subtitle_url,
    )


def torrent_candidates(torrents: list[dict[str, Any]]) -> list[TorrentCandidate]:
    """Every usable ``torrents[]`` entry, upstream order kept.

    The entries cross the conversation seam RAW (:class:`PopcornMovie`
    carries them verbatim); candidate shaping is policy, so it lives
    here next to the pick/fallback machinery.
    """
    out: list[TorrentCandidate] = []
    for entry in torrents:
        if not isinstance(entry, dict):
            continue
        quality = entry.get("quality")
        info_hash = entry.get("hash")
        seeds = entry.get("seeds")
        if not (isinstance(quality, str) and quality and isinstance(info_hash, str) and info_hash):
            continue
        out.append(
            TorrentCandidate(
                quality=quality,
                info_hash=info_hash,
                seeds=seeds if isinstance(seeds, int) and not isinstance(seeds, bool) else 0,
            )
        )
    return out
