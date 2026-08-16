"""Disk-backed home snapshot store (ticket #269, spec #267).

The first home read after a backend restart used to pay a full
provider fan-out (17-21s measured, B1) before the facade could answer
``/UserViews`` / ``/Items`` — past the Switchfin client's own request
timeout. This store persists the last successful home build (rows plus
the group resolution map) to a single versioned JSON file, so a cold
start serves the persisted snapshot at ANY age (decided: stale is
acceptable; the startup warm rebuilds and overwrites in the
background). Dead-poster risk on a very old snapshot is accepted and
documented in CONTEXT.md.

Round-2 (spec #323, Store T2 #325): the store is a thin adapter over
the shared ``VersionedFileStore`` — the corrupt-safe load ladder and
the atomic write body live in the module, not here. Same contract as
the other persisted domain objects (ADR-0003 notes, spec #247 / #257):
a versioned file written atomically after every successful home build,
and a corrupt / version-mismatched / unparseable file degrades to a
fresh build with a logged warning — never a crash on startup.
Single-process ownership (one uvicorn worker), full-snapshot writes.

Serves at ANY age by design: the background rebuild heals staleness,
so an age check would only add a cold-start wait the spec explicitly
removed.
"""

from __future__ import annotations

import logging

from .models import HomeResponse, SearchResult
from .versioned_store import VersionedFileStore

log = logging.getLogger("cs_uk_api.snapshot")

#: Format version of the snapshot file (spec #267). Bump whenever the
#: on-disk shape changes; a file with a different version is ignored.
SNAPSHOT_VERSION = 1


def _encode_snapshot(payload: object) -> object:
    """(home, sources) -> the JSON-serializable ``data`` value."""
    if not isinstance(payload, tuple) or len(payload) != 2:
        raise TypeError("snapshot payload must be (home, sources)")
    home, sources = payload
    if not isinstance(home, HomeResponse):
        raise TypeError("snapshot payload must carry a HomeResponse")
    return {
        "rows": [row.model_dump(mode="json") for row in home.rows],
        "sources": {
            gk: {pid: item.model_dump(mode="json") for pid, item in per_provider.items()}
            for gk, per_provider in sources.items()
        },
    }


def _decode_snapshot(
    data: object,
) -> tuple[HomeResponse, dict[str, dict[str, SearchResult]] | None]:
    """The ``data`` value -> (home, sources); raises on any shape
    mismatch (the module degrades to None, never crashes). A bad entry
    inside the sources map is skipped — the good source survives, the
    bad one is dropped (the pre-module behavior)."""
    if not isinstance(data, dict):
        raise TypeError("snapshot envelope must be an object")
    rows_raw = data.get("rows")
    if not isinstance(rows_raw, list):
        raise TypeError("snapshot 'rows' must be a list")
    home = HomeResponse.model_validate({"rows": rows_raw})
    sources: dict[str, dict[str, SearchResult]] = {}
    raw_sources = data.get("sources")
    if isinstance(raw_sources, dict):
        for gk, per_provider in raw_sources.items():
            if not isinstance(gk, str) or not isinstance(per_provider, dict):
                continue
            restored: dict[str, SearchResult] = {}
            for pid, item in per_provider.items():
                if not isinstance(pid, str) or not isinstance(item, dict):
                    continue
                try:
                    restored[pid] = SearchResult.model_validate(item)
                except Exception:  # noqa: BLE001 — skip one bad source
                    log.warning(
                        "home snapshot source skipped: group=%s provider=%s", gk, pid
                    )
                    continue
            if restored:
                sources[gk] = restored
    return home, (sources or None)


class SnapshotStore:
    """The last successful home build, mirrored to a JSON file through
    the shared ``VersionedFileStore``.

    ``save()`` writes the rows + group resolution map atomically;
    ``load()`` returns the persisted ``HomeResponse`` (or None when the
    file is absent/corrupt/version-mismatched). With ``path=None`` the
    store is memory-only — the test-suite default.
    """

    def __init__(self, path: str | None) -> None:
        self._persist: VersionedFileStore | None = None
        if path is not None:
            self._persist = VersionedFileStore(
                path=path,
                supported_versions=(SNAPSHOT_VERSION,),
                encode=_encode_snapshot,
                decode=_decode_snapshot,
            )

    # -- persistence ----------------------------------------------------

    def load(self) -> tuple[HomeResponse | None, dict[str, dict[str, SearchResult]] | None]:
        """(persisted snapshot, group resolution map) in ONE file read.

        Either side is None when the file is absent, corrupt, or
        version-mismatched — a bad file must never take the API down,
        so any parse/shape failure logs a warning and answers None and
        the caller falls back to a fresh build.
        """
        if self._persist is None:
            return None, None
        restored = self._persist.load()
        if not isinstance(restored, tuple) or len(restored) != 2:
            return None, None
        home, sources = restored
        return home, sources

    def save(self, home: HomeResponse, sources: dict[str, dict[str, SearchResult]]) -> None:
        """Full-snapshot atomic write through the module.

        Best-effort: a write failure is logged by the module, never
        raised — the in-memory snapshot keeps serving and the next
        build retries.
        """
        if self._persist is not None:
            self._persist.save((home, sources))
