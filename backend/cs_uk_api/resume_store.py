"""Disk-backed playback resume store (ticket #248, spec #247).

The facade's playback positions (recorded from the client's
``/Sessions/Playing|Progress|Stopped`` reports) survive a backend
restart: they live in a single versioned JSON file next to the poster
disk cache. Writes are atomic (temp file + rename in the same
directory), a Stopped report flushes immediately, Progress heartbeats
are debounced, and the app lifespan flushes pending state on shutdown.

A corrupt or version-mismatched file degrades to an empty resume — a
warning is logged and the API keeps serving; startup never crashes on
state. This is the FIRST persisted domain object, deliberately breaking
ADR-0003's "nothing with a domain schema survives process lifetime"
invariant — the ADR's own prescribed remedy (mandatory version token,
mismatched file ignored) is applied here, and the ADR file carries the
note (spec #247).

Single-process assumption (ADR-0003): one uvicorn worker owns the file;
writes are a full-snapshot temp+rename so a torn file is impossible
even across a power cut.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("cs_uk_api.resume")

#: Format version of the resume state file (spec #247). Bump whenever the
#: on-disk shape changes; a file with a different version is ignored.
RESUME_VERSION = 1

#: Debounce window for Progress-heartbeat writes (spec #247): the client
#: heartbeats roughly every 10 s — match that cadence instead of writing
#: per packet. Stopped reports bypass the debounce entirely.
_DEBOUNCE_S = 5.0

#: Finished-marking threshold (spec #247 §7): a position at or above
#: 95% of the item's runtime counts as watched to the end and leaves the
#: shelves.
FINISHED_FRACTION = 0.95

#: Store cap (spec #247): 50 entries, least-recently-updated evicted.
MAX_ITEMS = 50


class ResumeStore:
    """Playback positions as an in-memory dict mirrored to a JSON file.

    ``record()`` updates the in-memory state and schedules a write:
    immediately (synchronously) when the caller asks for a flush — the
    Stopped report path — or debounced by ``_DEBOUNCE_S`` for Progress
    heartbeats. ``flush()`` cancels any pending debounce and writes now
    (the lifespan shutdown path). With ``path=None`` the store is
    memory-only — the test-suite default, which keeps the pre-#248
    semantics exactly.
    """

    def __init__(
        self,
        path: str | None,
        debounce_s: float = _DEBOUNCE_S,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._debounce_s = debounce_s
        self._now = now
        self._items: dict[str, dict[str, int | float]] = {}
        self._timer: asyncio.TimerHandle | None = None
        self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> None:
        """Read the state file at construction (process start).

        Missing file → clean start. Unparseable JSON, a wrong version
        token, or a bad shape → warning + empty resume; never a crash.
        """
        if self._path is None:
            return
        try:
            raw = Path(self._path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            log.warning("resume state unreadable, starting empty: %s", self._path, exc_info=True)
            return
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("v") != RESUME_VERSION:
                log.warning(
                    "resume state ignored: version mismatch or bad shape at %s (expected v%d)",
                    self._path,
                    RESUME_VERSION,
                )
                return
            items = payload.get("items")
            if not isinstance(items, dict):
                log.warning("resume state ignored: 'items' not a map at %s", self._path)
                return
            cleaned: dict[str, dict[str, int | float]] = {}
            for key, entry in items.items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                pos = entry.get("position_ticks")
                if not isinstance(pos, int) or pos <= 0:
                    continue
                kept: dict[str, int | float] = {"position_ticks": pos}
                for field in ("runtime_ticks", "updated_at"):
                    value = entry.get(field)
                    if isinstance(value, (int, float)):
                        kept[field] = value
                cleaned[key] = kept
            self._items = cleaned
        except Exception:  # a bad file must never crash startup
            log.warning("resume state unreadable, starting empty: %s", self._path, exc_info=True)

    def _write_now(self) -> None:
        """Full-snapshot atomic write (temp file + rename, same dir).

        Best-effort: a write failure is logged, never raised — state
        stays correct in memory and the next write retries.
        """
        if self._path is None:
            return
        try:
            path = Path(self._path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".playback-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"v": RESUME_VERSION, "items": self._items}, fh, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:  # never take the API down over a write
            log.warning("resume state write failed: %s", self._path, exc_info=True)

    # -- mutate / read ---------------------------------------------------

    def record(
        self,
        item_id: str,
        position_ticks: int,
        *,
        runtime_ticks: int | None = None,
        flush: bool = False,
    ) -> None:
        """Record the client's playback position (newest report wins).

        Zero/negative positions (a just-started item) are ignored. With
        ``flush=True`` the state is written synchronously before this
        returns (the Stopped-report path); otherwise the write is
        debounced.
        """
        if position_ticks <= 0:
            return
        existing = self._items.get(item_id) or {}
        # Finished-marking (#249, spec #247 §7): a position at >=95% of a
        # KNOWN runtime counts as watched to the end and leaves the
        # shelves (Resume + NextUp). The runtime comes from the report or,
        # when the report omits it (Progress heartbeats), from the entry
        # an earlier report stored. Items with no known runtime are never
        # auto-finished.
        runtime = runtime_ticks if runtime_ticks is not None else existing.get("runtime_ticks")
        if runtime is not None and position_ticks >= FINISHED_FRACTION * runtime:
            self._items.pop(item_id, None)
            self._schedule_write(immediate=flush)
            return
        entry = dict(existing)
        entry["position_ticks"] = position_ticks
        entry["updated_at"] = self._now()
        if runtime_ticks is not None:
            entry["runtime_ticks"] = runtime_ticks
        self._items[item_id] = entry
        self._cap()
        self._schedule_write(immediate=flush)

    def _schedule_write(self, *, immediate: bool) -> None:
        if self._path is None:
            return  # memory-only mode
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if immediate:
            # Inline, in the caller's context: a Stopped report must be on
            # disk before the request returns, deterministically.
            self._write_now()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. direct sync callers in tests): nothing
            # to defer to — write synchronously rather than drop the state.
            self._write_now()
            return
        self._timer = loop.call_later(self._debounce_s, self._write_now)

    def flush(self) -> None:
        """Cancel any pending debounce and write the state now.

        The lifespan shutdown path (acceptance criterion: pending state
        is flushed on shutdown). Safe to call from a sync context.
        """
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._write_now()

    def entries(self) -> dict[str, dict[str, int | float]]:
        """item_id -> entry map ({position_ticks, runtime_ticks?, updated_at})."""
        return dict(self._items)

    def _cap(self) -> None:
        """LRU-50 (#249): evict the least-recently-updated entry.

        Ties on ``updated_at`` (records within one clock tick) break on
        the key so eviction stays deterministic.
        """
        if len(self._items) <= MAX_ITEMS:
            return
        oldest = min(
            self._items.items(),
            key=lambda kv: (float(kv[1].get("updated_at", 0.0)), kv[0]),
        )
        del self._items[oldest[0]]

    def positions(self) -> dict[str, int]:
        """item_id -> position_ticks, most-progressed first (#214).

        NextUp reads this: the spec keeps its "next sibling of the
        most-progressed episode per series" semantics unchanged. The
        resume ROW orders by recency — see ``recent()``.
        """
        return {
            key: int(entry["position_ticks"])
            for key, entry in sorted(
                self._items.items(), key=lambda kv: int(kv[1]["position_ticks"]), reverse=True
            )
        }

    def recent(self, limit: int) -> dict[str, int]:
        """item_id -> position_ticks, most recently updated first, at
        most ``limit`` entries (the resume row, #249)."""
        ordered = sorted(
            self._items.items(),
            key=lambda kv: (float(kv[1].get("updated_at", 0.0)), kv[0]),
            reverse=True,
        )
        return {key: int(entry["position_ticks"]) for key, entry in ordered[:limit]}

    def clear(self) -> None:
        """Drop all recorded positions (test isolation, #214)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._items.clear()
