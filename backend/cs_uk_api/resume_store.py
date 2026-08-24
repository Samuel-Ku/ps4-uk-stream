"""Disk-backed playback resume store (ticket #248, spec #247; spec #363).

The facade's playback positions (recorded from the client's
``/Sessions/Playing|Progress|Stopped`` reports) survive a backend
restart: they live in a single versioned JSON file next to the poster
disk cache. Since spec #363 the store is a thin adapter over the
shared ``versioned_store`` module: ``VersionedFileStore`` owns the
corrupt-safe load ladder and the atomic write, and
``AsyncDebouncedSave`` owns the Progress-heartbeat debounce —
event-loop-native (``loop.call_later``, latest wins), with a sync-
caller fallback that writes immediately when no loop is running. A
Stopped report bypasses the debounce and writes synchronously before
the request returns; the app lifespan flushes pending state on
shutdown.

Wire envelope (spec #363): the file carries the shared outer envelope

    {"version": 1, "data": {"items": {...}, "queries": [...],
                            "finished": {...}}}

with the payload shape unchanged inside ``data``. The legacy
pre-envelope shape (a bare top-level ``v`` token, specs #247–#272) is
NOT backward-read: a legacy, corrupt, or mismatched file degrades to a
fresh EMPTY resume with a logged warning — exactly ADR-0003's
prescribed remedy ("a mismatched or unparseable file is ignored"). That
trades a one-time loss of a <=50-entry shelf that naturally
re-accumulates from continued viewing for a decode path that stays
simple forever; the next write replaces the legacy file.

Single-process assumption (ADR-0003): one uvicorn worker owns the file;
writes are a full-snapshot temp+rename so a torn file is impossible
even across a power cut.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from .versioned_store import AsyncDebouncedSave, VersionedFileStore

log = logging.getLogger("cs_uk_api.resume")

#: Search queries persisted beside the playback state (spec #252):
#: newest first, deduped, bounded at 50 — the «Рекомендовано для тебе»
#: taste signal.
MAX_QUERIES = 50

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


def _clean_entry(raw: object) -> dict[str, int | float] | None:
    """One persisted position entry, or None on any shape mismatch."""
    if not isinstance(raw, dict):
        return None
    pos = raw.get("position_ticks")
    if not isinstance(pos, int) or pos <= 0:
        return None
    kept: dict[str, int | float] = {"position_ticks": pos}
    for field in ("runtime_ticks", "updated_at"):
        value = raw.get(field)
        if isinstance(value, (int, float)):
            kept[field] = value
    return kept


def _decode_resume(data: object) -> dict[str, object]:
    """The ``data`` value -> the state sections; raises on any shape
    mismatch (the module ladder degrades to empty, never crashes)."""
    if not isinstance(data, dict):
        raise TypeError("resume payload must be an object")
    items_raw = data.get("items")
    if not isinstance(items_raw, dict):
        raise TypeError("'items' must be a map")
    items: dict[str, dict[str, int | float]] = {}
    for key, raw in items_raw.items():
        entry = _clean_entry(raw)
        if entry is not None and isinstance(key, str):
            items[key] = entry
    queries_raw = data.get("queries", [])
    queries: list[str] = (
        [q for q in queries_raw if isinstance(q, str)][:MAX_QUERIES]
        if isinstance(queries_raw, list)
        else []
    )
    finished_raw = data.get("finished", {})
    finished: dict[str, float] = (
        {
            k: float(v)
            for k, v in finished_raw.items()
            if isinstance(k, str) and isinstance(v, (int, float))
        }
        if isinstance(finished_raw, dict)
        else {}
    )
    return {"items": items, "queries": queries, "finished": finished}


def _encode_resume(payload: object) -> object:
    """ResumeStore -> the JSON-serializable ``data`` value."""
    if not isinstance(payload, ResumeStore):
        raise TypeError("ResumeStore payload expected")
    return {
        "items": payload._items,
        "queries": payload._queries,
        "finished": payload._finished,
    }


class ResumeStore:
    """Playback positions as an in-memory dict mirrored to a JSON file
    through the shared ``VersionedFileStore`` + ``AsyncDebouncedSave``.

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
        self._queries: list[str] = []
        # Finished-history (spec #272): item_id -> finished_at, the items
        # that crossed the >=95% threshold. They leave the shelves but
        # stay browsable in «Нещодавно переглянуто».
        self._finished: dict[str, float] = {}
        self._persist: VersionedFileStore | None = None
        self._saver: AsyncDebouncedSave | None = None
        if path is not None:
            existed = Path(path).exists()
            self._persist = VersionedFileStore(
                path=path,
                supported_versions=(1,),
                encode=_encode_resume,
                decode=_decode_resume,
            )
            restored = self._persist.load()
            if isinstance(restored, dict):
                self._items = restored["items"]
                self._queries = restored["queries"]
                self._finished = restored["finished"]
            elif existed:
                # Missing file is a clean start; anything else (corrupt
                # JSON, unknown version, legacy pre-envelope shape, bad
                # payload) degrades to fresh — visible in the log,
                # never a crash.
                log.warning(
                    "resume state ignored (corrupt, unknown version, or legacy "
                    "pre-envelope shape) at %s — starting empty",
                    path,
                )
            self._saver = AsyncDebouncedSave(self._persist, delay_s=debounce_s)

    # -- persistence ----------------------------------------------------

    def _snapshot(self) -> object:
        """The debounce payload: the store itself. Encoding reads the
        live state at write time, so the latest mutation always wins."""
        return self

    def _write_now(self) -> None:
        """Full-snapshot atomic write through the shared store.

        Best-effort: a write failure is logged by the module, never
        raised — state stays correct in memory and the next write
        retries.
        """
        if self._persist is not None:
            self._persist.save(self)

    def _schedule_write(self, *, immediate: bool) -> None:
        if self._saver is None:
            return  # memory-only mode
        if immediate:
            # Inline, in the caller's context: a Stopped report must be
            # on disk before the request returns, deterministically.
            self._write_now()
            return
        # Deferred via loop.call_later when a loop is running; a sync
        # caller falls back to an immediate write inside request().
        self._saver.request(self._snapshot())

    def flush(self) -> None:
        """Cancel any pending debounce and write the state now.

        The lifespan shutdown path (acceptance criterion: pending state
        is flushed on shutdown). Safe to call from a sync context.
        """
        if self._saver is not None:
            self._saver.flush()

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
            # Finished (#249): leaves the shelves — but the finished
            # history (spec #272) keeps it browsable.
            self._items.pop(item_id, None)
            self._finished[item_id] = self._now()
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

    @staticmethod
    def _pos_runtime(entry: dict[str, int | float]) -> tuple[int, int | None]:
        """(position_ticks, runtime_ticks|None) — the wire pair (#250)."""
        runtime = entry.get("runtime_ticks")
        return int(entry["position_ticks"]), int(runtime) if runtime is not None else None

    def positions_entries(self) -> dict[str, tuple[int, int | None]]:
        """item_id -> (position_ticks, runtime_ticks|None), most-
        progressed first — feeds NextUp (#250)."""
        ordered = sorted(
            self._items.items(), key=lambda kv: int(kv[1]["position_ticks"]), reverse=True
        )
        return {key: self._pos_runtime(entry) for key, entry in ordered}

    def recent_entries(self, limit: int) -> dict[str, tuple[int, int | None]]:
        """item_id -> (position_ticks, runtime_ticks|None), most recently
        updated first, at most ``limit`` — feeds the resume row (#250)."""
        ordered = sorted(
            self._items.items(),
            key=lambda kv: (float(kv[1].get("updated_at", 0.0)), kv[0]),
            reverse=True,
        )
        return {key: self._pos_runtime(entry) for key, entry in ordered[:limit]}

    def history(self, limit: int = 20) -> list[str]:
        """item_ids in most-recently-seen order, active AND finished
        (spec #272 «Нещодавно переглянуто»): the union of the resume
        entries (by ``updated_at``) and the finished history (by
        ``finished_at``), newest first, capped at ``limit``."""
        timed: list[tuple[float, str]] = [
            (float(entry.get("updated_at", 0.0)), key) for key, entry in self._items.items()
        ]
        timed += [(stamp, key) for key, stamp in self._finished.items()]
        timed.sort(key=lambda kv: (kv[0], kv[1]), reverse=True)
        return [key for _stamp, key in timed[:limit]]

    def record_query(self, query: str) -> None:
        """Record a search query (spec #252): newest first, deduped (a
        repeat moves to the front), bounded at ``MAX_QUERIES``. Blank
        queries are ignored. The write is debounced like a Progress
        heartbeat — queries share the state file.
        """
        q = query.strip()
        if not q:
            return
        self._queries = [q] + [x for x in self._queries if x != q]
        del self._queries[MAX_QUERIES:]
        self._schedule_write(immediate=False)

    def recent_queries(self) -> list[str]:
        """Search queries, newest first (spec #252)."""
        return list(self._queries)

    def clear(self) -> None:
        """Drop all recorded positions + queries + finished history (test
        isolation, #214)."""
        self._items.clear()
        self._queries.clear()
        self._finished.clear()
        if self._saver is not None:
            # Replace any pending snapshot with the cleared one so a
            # later debounced write cannot resurrect pre-clear state.
            self._saver.request(self._snapshot())
