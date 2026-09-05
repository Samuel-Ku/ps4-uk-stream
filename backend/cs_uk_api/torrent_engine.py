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

import logging
import os
import urllib.parse
from dataclasses import dataclass
from typing import Protocol

import httpx

from . import config as _config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineStream:
    """A playable session: LAN URL + container (post-remux).

    ``container`` is what the PLAYER receives (e.g. "mp4" after the
    engine's on-the-fly MKV→MP4 remux), not the torrent's original
    container.

    ``seekable`` records whether the player can Range-seek into
    ``url``. Native byte-serving is seekable (BitPlay fronts each file
    with Go ``http.ServeContent``); the remux path is chunked fMP4 with
    ``Accept-Ranges: none`` — progressive but NOT seekable (research
    #367 §1) — so the adapter stamps those streams ``seekable=False``.

    #378 — what the file actually carries, discovered from the engine's
    file listing:

      - ``subtitle_url`` — the VTT conversion endpoint of an external
        ``.srt`` file in the SAME torrent (BitPlay converts on request:
        ``stream/{i}?format=vtt``). None when the torrent carries no
        separate srt — the remux path strips embedded tracks, so only
        external srt can ever play (research #367 limit 6).
    """

    url: str
    container: str
    seekable: bool = True
    subtitle_url: str | None = None


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
    identifier itself. Records calls (ensure count + last identifier +
    last file hint) so route-level orchestration assertions can verify
    session-ensured-before-handoff — and, since #379, that the SEASON
    file hint rode along — without touching internals of the mapping.
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
        self.last_file_hint: str | None = None

    #: #378 convenience: full EngineStreams (subtitle_url / audio_tracks
    #: included) ride the SAME ``streams`` table — no second knob; an
    #: unconfigured identifier synthesizes a bare stream with neither.

    async def ensure_session(
        self, identifier: str, *, file_hint: str | None = None
    ) -> EngineStream:
        self.ensure_count += 1
        self.last_identifier = identifier
        self.last_file_hint = file_hint
        configured = self._streams.get(identifier)
        if configured is not None:
            return configured
        quoted = urllib.parse.quote(identifier, safe="")
        return EngineStream(url=f"http://torrent.fake/{quoted}", container=self._container)


# ---------------------------------------------------------------------------
# BitPlay adapter — ALL engine-specific knowledge lives in this section.
#
# The wire surface below is a best-effort derivation from the upstream
# source (github.com/aculix/bitplay, main.go route registrations): add a
# magnet, get an infohash session id, list its files, stream or remux a
# file index. A parallel effort probes the LIVE service; corrections are
# meant to land as one-place edits to these constants.
#
#   POST /api/v1/torrent/add            {"Magnet": "..."} → {"sessionId": hex}
#   GET  /api/v1/torrent/{sid}          [{index, name, size}, ...]
#   GET  /api/v1/torrent/{sid}/stream/{i}    native bytes (Range-capable)
#   GET  /api/v1/torrent/{sid}/remux/{i}     progressive fMP4 (MKV→MP4)
#
# Error statuses observed upstream: 400 invalid magnet, 504 metadata
# timeout (dead torrent), 401 wrong basic auth, 503 metadata pending,
# 404 unknown session.
# ---------------------------------------------------------------------------

ADD_TORRENT_PATH = "/api/v1/torrent/add"
FILES_PATH_TEMPLATE = "/api/v1/torrent/{session_id}"
STREAM_PATH_TEMPLATE = "/api/v1/torrent/{session_id}/stream/{file_index}"
REMUX_PATH_TEMPLATE = "/api/v1/torrent/{session_id}/remux/{file_index}"

#: Connect budget for every engine call (LAN host — should be instant).
ENGINE_CONNECT_TIMEOUT_S = 5.0
#: ``add`` blocks while BitPlay fetches metadata (its own cap is 3 min);
#: we bail after this window and let the provider's bounded fallback (or
#: the route layer) translate the failure. 30 s is the live-tuned
#: window: healthy swarms yield metadata in seconds, and the #373
#: acceptance showed the only observed slower case (Sintel's near-empty
#: swarm) needed ~90 min — a window between them buys nothing, while a
#: short window keeps the dead-swarm verdict fast.
ADD_TIMEOUT_S = 30.0

#: Containers BitPlay serves byte-native (extension → reported container).
DIRECT_CONTAINER_BY_EXT: dict[str, str] = {
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".webm": "webm",
}
#: Video-ish extensions that must go through the remuxer instead.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {*DIRECT_CONTAINER_BY_EXT, ".mkv", ".avi", ".mov", ".mpg", ".mpeg", ".ts", ".wmv", ".flv"}
)

#: Statuses where the ENGINE blames THE TORRENT (malformed / dead-on-
#: arrival) — everything else non-2xx reads as lane breakage.
TORRENT_BLAME_STATUSES = frozenset({400, 504})


def _select_file(files: list[tuple[int, str]], file_hint: str | None) -> tuple[int, str]:
    """Deterministic file pick: explicit hint match (substring, case-
    insensitive) → first video-like file → first file at all."""
    if file_hint:
        lowered = file_hint.lower()
        for entry in files:
            if lowered in entry[1].lower():
                return entry
    for entry in files:
        if os.path.splitext(entry[1])[1].lower() in VIDEO_EXTENSIONS:
            return entry
    return files[0]


#: Subtitle files BitPlay can convert to VTT on request (`?format=vtt`)
#: — only external ``.srt`` tracks in the SAME torrent (research #367).
SUBTITLE_EXTENSIONS: frozenset[str] = frozenset({".srt"})


def _select_subtitle(files: list[tuple[int, str]]) -> int | None:
    """The external-srt track to surface (or None).

    Preference: an ``.en``-suffixed srt (the English lane's point),
    then the first srt at all. Index is None when the torrent carries
    no separate srt — the honest "no subtitles" verdict (embedded
    tracks are stripped by the remux path, so they can never play).
    """
    srts = [e for e in files if os.path.splitext(e[1])[1].lower() in SUBTITLE_EXTENSIONS]
    if not srts:
        return None
    for entry in srts:
        stem = os.path.splitext(entry[1])[0].lower()
        if stem.endswith(".en") or ".en." in stem:
            return entry[0]
    return srts[0][0]


class BitPlayClient:
    """Real adapter: drives the BitPlay HTTP API with httpx directly.

    Owns exactly one concern — turning (magnet-or-infohash, optional
    file hint) into the LAN URL the player should open, choosing the
    native-stream endpoint for already-playable containers and the
    remux endpoint (→ mp4) for the rest. Transport failures raise
    :class:`EngineUnavailable`; torrent-level refusals raise
    :class:`EngineRejected`. Short timeouts throughout: the caller's
    request path must never hang on a dead engine.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # BitPlay enables basic auth only when BOTH env vars are set;
        # mirror that — a lone credential would silently half-authenticate.
        self._auth = (username, password) if username and password else None
        self._http = http

    async def ensure_session(
        self, identifier: str, *, file_hint: str | None = None
    ) -> EngineStream:
        timeout = httpx.Timeout(ADD_TIMEOUT_S, connect=ENGINE_CONNECT_TIMEOUT_S)
        http = self._http or httpx.AsyncClient(timeout=timeout, auth=self._auth)
        own = self._http is None
        try:
            session_id = await self._add_torrent(http, identifier)
            files = await self._list_files(http, session_id)
            index, name = _select_file(files, file_hint)
            quoted_id = urllib.parse.quote(session_id, safe="")
            direct = DIRECT_CONTAINER_BY_EXT.get(os.path.splitext(name)[1].lower())
            if direct is not None:
                url = f"{self._base_url}{STREAM_PATH_TEMPLATE.format(session_id=quoted_id, file_index=index)}"
            else:
                url = f"{self._base_url}{REMUX_PATH_TEMPLATE.format(session_id=quoted_id, file_index=index)}"
            # Chunked fMP4, Accept-Ranges: none — forward-playable but
            # the player cannot seek into it (research #367 §1).
            seekable = direct is not None
            # #378 — what else the session carries, discovered from the
            # same listing (no extra engine round-trips): the VTT
            # conversion endpoint of an external srt. Audio picks are
            # deliberately NOT surfaced: the file listing cannot see
            # audio streams inside a file, so any pick would be
            # invented (lean-build omission; restore only if the
            # engine exposes per-file audio stream indexes).
            subtitle_url: str | None = None
            srt_index = _select_subtitle(files)
            if srt_index is not None:
                subtitle_url = (
                    f"{self._base_url}"
                    f"{STREAM_PATH_TEMPLATE.format(session_id=quoted_id, file_index=srt_index)}"
                    "?format=vtt"
                )
            return EngineStream(
                url=url,
                container=direct if direct is not None else "mp4",
                seekable=seekable,
                subtitle_url=subtitle_url,
            )
        finally:
            if own:
                await http.aclose()

    async def _add_torrent(self, http: httpx.AsyncClient, identifier: str) -> str:
        try:
            resp = await http.post(f"{self._base_url}{ADD_TORRENT_PATH}", json={"Magnet": identifier})
        except httpx.ReadTimeout as e:
            # The add endpoint BLOCKS until metadata arrives (upstream cap
            # is 3 min, our ADD_TIMEOUT_S is shorter) — a read timeout here
            # means we reached the engine and metadata never came: a dead
            # swarm, not a dead lane. Observed live in the #373 acceptance
            # (fork-mirror pick, zero real seeders).
            raise EngineRejected(
                f"torrent metadata did not arrive within {ADD_TIMEOUT_S:.0f}s (dead swarm?)"
            ) from e
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"engine unreachable: {e}") from e
        self._ensure_ok(resp, "add")
        payload = resp.json()
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise EngineUnavailable(f"malformed add response: {str(payload)[:120]}")
        return session_id

    async def _list_files(self, http: httpx.AsyncClient, session_id: str) -> list[tuple[int, str]]:
        path = FILES_PATH_TEMPLATE.format(session_id=urllib.parse.quote(session_id, safe=""))
        try:
            resp = await http.get(f"{self._base_url}{path}")
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"engine unreachable: {e}") from e
        self._ensure_ok(resp, "file list")
        raw = resp.json()
        if not isinstance(raw, list) or not raw:
            raise EngineUnavailable(f"malformed file list: {str(raw)[:120]}")
        files: list[tuple[int, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise EngineUnavailable(f"malformed file entry: {str(entry)[:120]}")
            index, name = entry.get("index"), entry.get("name")
            if isinstance(index, bool) or not isinstance(index, int):
                raise EngineUnavailable(f"malformed file index: {index!r}")
            if not isinstance(name, str) or not name:
                raise EngineUnavailable(f"malformed file name: {name!r}")
            files.append((index, name))
        return files

    @staticmethod
    def _ensure_ok(resp: httpx.Response, what: str) -> None:
        if 200 <= resp.status_code < 300:
            return
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = str(body.get("error", ""))
        except ValueError:
            detail = resp.text[:120]
        if resp.status_code in TORRENT_BLAME_STATUSES:
            raise EngineRejected(f"engine rejected torrent ({what}): {resp.status_code} {detail}".strip())
        raise EngineUnavailable(
            f"engine failed {what}: {resp.status_code} {detail}".strip()
        )


# ------------------------------------------------------- construction


def build_engine_from_settings() -> TorrentEngine | None:
    """The one construction path (architecture.md §5: single settings
    binding) — reads ``_config.SETTINGS`` at call time, never binds the
    value at import.

    Unset/empty ``torrent_engine_url`` ⇒ None: the lane is DISABLED,
    and the future call site must surface that loudly (a deliberate
    "not configured" verdict), never silently pretend to stream.
    """
    url = (_config.SETTINGS.torrent_engine_url or "").strip()
    if not url:
        return None
    return BitPlayClient(
        base_url=url,
        username=_config.SETTINGS.torrent_engine_user,
        password=_config.SETTINGS.torrent_engine_password,
    )


# ------------------------------------------------ lazy cached singleton


_engine: TorrentEngine | None = None


def get_engine() -> TorrentEngine | None:
    """Lazy module-level singleton over :func:`build_engine_from_settings`,
    mirroring the ``http_client._client`` pattern: built on first use,
    then the SAME instance on every call. Deliberately SILENT here —
    while unconfigured it answers None on every call (re-reading
    settings), so the CALL SITE owns the loud "not configured" verdict
    and a lane configured later needs no process restart.
    :func:`reset_engine` drops the cache (tests / settings swaps).
    """
    global _engine
    if _engine is None:
        _engine = build_engine_from_settings()
    return _engine


def reset_engine() -> None:
    """Drop the cached engine so the next :func:`get_engine` rebuilds
    from current settings."""
    global _engine
    _engine = None
