"""TorrentEngine seam (spec #374, ticket #375) — the ONE new seam of
the English-content lane.

A narrow in-process interface: ensure a playable session for an opaque
identifier (the BitPlay adapter treats it as magnet-or-infohash) and
receive back a plain LAN URL plus its container ("mp4" post-remux).
Two adapters justify it: the real BitPlay HTTP client and a
deterministic in-memory fake that lets route-level tests verify
orchestration without Docker or network. Remuxing, subtitle conversion
and audio handling are engine internals — deliberately not seams.

The interface is IssueGateway-style (``drift/issues.py``): a small
``typing.Protocol`` so adapters stay structural — nothing imports an
adapter base class. Construction goes through the configuration
module's single settings binding (``build_engine_from_settings``,
architecture.md §5); there is no second config pathway.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EngineStream:
    """A playable session: LAN URL + container (post-remux).

    ``container`` is what the PLAYER receives (e.g. "mp4" after the
    engine's on-the-fly MKV→MP4 remux), not the torrent's original
    container.
    """

    url: str
    container: str


# ------------------------------------------------------------- errors


class TorrentEngineError(Exception):
    """Base for every torrent-engine failure."""


class EngineUnavailable(TorrentEngineError):
    """The engine itself is unreachable / broken (transport failure,
    auth misconfiguration, malformed response, unexpected status).

    Route layers map this onto lane-level failure codes later
    (`unreachable` per the failure-semantics taxonomy).
    """


class EngineRejected(TorrentEngineError):
    """The engine refused THIS torrent (malformed magnet, dead-on-
    arrival: metadata timeout / zero seeders).

    Deliberately distinct from :class:`EngineUnavailable` — a bad item
    must not read as a dead lane (spec #374 error-surface note).
    """


# --------------------------------------------------------- interface


class TorrentEngine(Protocol):
    async def ensure_session(
        self, identifier: str, *, file_hint: str | None = None
    ) -> EngineStream:
        """Ensure a streamable session for `identifier`.

        `identifier` is opaque to the interface; `file_hint` is an
        optional fragment of the wanted file's name (best-effort
        selection). Idempotent in spirit — engines reuse live sessions
        for the same infohash.
        """
        ...


class FakeTorrentEngine:
    """Deterministic in-memory engine for CI (no Docker, no network).

    Maps identifier→EngineStream from an optional configured table;
    unknown identifiers get a stable synthesized URL derived from the
    identifier itself. Records calls (ensure count + last identifier)
    so route-level orchestration assertions can verify session-ensured-
    before-handoff without touching internals of the mapping.
    """

    def __init__(
        self,
        *,
        streams: dict[str, EngineStream] | None = None,
        container: str = "mp4",
    ) -> None:
        self._streams = dict(streams) if streams else {}
        self._container = container
        self.ensure_count = 0
        self.last_identifier: str | None = None

    async def ensure_session(
        self, identifier: str, *, file_hint: str | None = None
    ) -> EngineStream:
        self.ensure_count += 1
        self.last_identifier = identifier
        configured = self._streams.get(identifier)
        if configured is not None:
            return configured
        quoted = urllib.parse.quote(identifier, safe="")
        return EngineStream(url=f"http://torrent.fake/{quoted}", container=self._container)
