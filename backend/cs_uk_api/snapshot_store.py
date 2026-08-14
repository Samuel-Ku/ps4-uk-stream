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

Same contract as the other persisted domain objects (ADR-0003 notes,
spec #247 / #257): a versioned file written atomically (temp + rename
in the same directory) after every successful home build, and a
corrupt / version-mismatched / unparseable file degrades to a fresh
build with a logged warning — never a crash on startup. Single-process
ownership (one uvicorn worker), full-snapshot writes.

Serves at ANY age by design: the background rebuild heals staleness,
so an age check would only add a cold-start wait the spec explicitly
removed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .models import HomeResponse, SearchResult

log = logging.getLogger("cs_uk_api.snapshot")

#: Format version of the snapshot file (spec #267). Bump whenever the
#: on-disk shape changes; a file with a different version is ignored.
SNAPSHOT_VERSION = 1

#: Versions ``load`` accepts. Anything else is ignored (warn + None).
_SUPPORTED_VERSIONS = (1,)


class SnapshotStore:
    """The last successful home build, mirrored to a JSON file.

    ``save()`` writes the rows + group resolution map atomically;
    ``load()`` returns the persisted ``HomeResponse`` (or None when the
    file is absent/corrupt/version-mismatched). With ``path=None`` the
    store is memory-only — the test-suite default.
    """

    def __init__(self, path: str | None) -> None:
        self._path = path

    # -- persistence ----------------------------------------------------

    def load(self) -> tuple[HomeResponse | None, dict[str, dict[str, SearchResult]] | None]:
        """(persisted snapshot, group resolution map) in ONE file read.

        Either side is None when the file is absent, corrupt, or
        version-mismatched — a bad file must never take the API down,
        so any parse/shape failure logs a warning and answers None and
        the caller falls back to a fresh build.
        """
        if self._path is None:
            return None, None
        try:
            raw = Path(self._path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, None
        except OSError:
            log.warning("home snapshot unreadable, ignoring: %s", self._path, exc_info=True)
            return None, None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("v") not in _SUPPORTED_VERSIONS:
                log.warning(
                    "home snapshot ignored: version mismatch or bad shape at %s (expected v%d)",
                    self._path,
                    SNAPSHOT_VERSION,
                )
                return None, None
            rows_raw = payload.get("rows")
            if not isinstance(rows_raw, list):
                log.warning("home snapshot ignored: 'rows' not a list at %s", self._path)
                return None, None
            home = HomeResponse.model_validate({"rows": rows_raw})
            sources: dict[str, dict[str, SearchResult]] = {}
            raw_sources = payload.get("sources")
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
        except Exception:  # a bad file must never crash startup
            log.warning("home snapshot unreadable, ignoring: %s", self._path, exc_info=True)
            return None, None

    def save(self, home: HomeResponse, sources: dict[str, dict[str, SearchResult]]) -> None:
        """Full-snapshot atomic write (temp file + rename, same dir).

        Best-effort: a write failure is logged, never raised — the
        in-memory snapshot keeps serving and the next build retries.
        """
        if self._path is None:
            return
        payload = {
            "v": SNAPSHOT_VERSION,
            "rows": [row.model_dump(mode="json") for row in home.rows],
            "sources": {
                gk: {pid: item.model_dump(mode="json") for pid, item in per_provider.items()}
                for gk, per_provider in sources.items()
            },
        }
        try:
            path = Path(self._path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".home-snapshot-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:  # never take the API down over a write
            log.warning("home snapshot write failed: %s", self._path, exc_info=True)
