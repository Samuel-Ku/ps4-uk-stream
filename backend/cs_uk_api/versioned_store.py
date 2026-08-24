"""Versioned JSON file persistence (spec #323, Store T1 #324).

One module owns the corrupt-safe load ladder and the atomic write for
every persisted store — resume/profile, user-state, snapshot, drift
baseline — so the next store is a thin adapter (~20 lines), not a
4th copy-paste of a byte-parallel implementation.

Wire envelope (every file this module writes):

    {"version": <int>, "data": <adapter-encoded payload>}

The version token answers ADR-0003's obligation: once a domain value is
persisted across process lifetime, a version token stops being a no-op
and becomes mandatory. ``load`` accepts any version in
``supported_versions``; anything else — missing file, unreadable, corrupt
JSON, wrong version, shape-invalid payload — degrades to ``None`` with a
log line. A store never crashes on a bad file.

``save`` is atomic (mkstemp in the same directory + ``os.replace``) and
never raises: the worst case is a log line and the previous file intact.

``DebouncedSave`` is the optional thread-based coalescing wrapper for
adapters with high-frequency writes; ``AsyncDebouncedSave`` (spec #363)
is its event-loop-native sibling — ``loop.call_later`` instead of a
worker thread, so shutdown stays synchronous inside uvicorn's loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("cs_uk_api.versioned_store")

#: payload -> JSON-serializable "data" value (the adapter's shape).
Encode = Callable[[object], object]
#: "data" value -> payload; raises on shape mismatch (degrades to None).
Decode = Callable[[object], object]


def _atomic_write(path: str, blob: bytes) -> None:
    """Shared atomic-write body: mkdir parents, mkstemp + fsync +
    ``os.replace`` in the same directory, unlink the tmp on failure;
    never raises."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
    except OSError as e:
        log.warning("atomic write failed: %s: %s", path, e)
        return
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        # Atomic swap: readers (this or another process) never see a
        # half-written file — they see the old file or the new one.
        os.replace(tmp, target)
    except OSError as e:
        log.warning("atomic write failed: %s: %s", path, e)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def atomic_write_text(path: str, text: str) -> None:
    """Atomic text write (mkstemp + replace in the same directory); never raises.

    The shared write primitive every persisted artifact uses — the
    ``VersionedFileStore`` envelope AND plain files (e.g. the episode-rail
    sweep's Markdown report, spec #323 Store T3 #326). Readers see the
    old file or the new one, never a half-written file; the worst case is
    a log line and the previous file intact.
    """
    try:
        blob = text.encode("utf-8")
    except Exception as e:  # noqa: BLE001 — never raise on a bad payload
        log.warning("atomic write encode failed: %s: %s", path, e)
        return
    _atomic_write(path, blob)


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Atomic binary write — mirror of ``atomic_write_text`` minus the
    utf-8 encode; never raises.

    For opaque payloads that must stay OUTSIDE any version envelope
    (ADR-0003's confirmed exception: poster image bytes under a
    content-addressed name). Same guarantees as the text primitive:
    readers never see a torn file; the worst case is a log line and the
    previous file intact. ``mkstemp`` yields process-unique ``O_EXCL``
    temps in the target directory, so concurrent ``--workers`` writes
    cannot interleave regardless of naming.
    """
    try:
        blob = bytes(data)
    except Exception as e:  # noqa: BLE001 — never raise on a bad payload
        log.warning("atomic write encode failed: %s: %s", path, e)
        return
    _atomic_write(path, blob)


class VersionedFileStore:
    """Versioned JSON persistence for one store (the deep module).

    Constructor takes the file path and the supported version tuple;
    the adapter supplies encode/decode callables. ``load`` never raises
    (missing/corrupt/mismatched/shape-invalid -> None, logged); ``save``
    is atomic and never raises.
    """

    def __init__(
        self,
        *,
        path: str,
        supported_versions: tuple[int, ...],
        encode: Encode,
        decode: Decode,
    ) -> None:
        self._path = Path(path)
        self._supported = supported_versions
        self._encode = encode
        self._decode = decode

    def load(self) -> object | None:
        """Corrupt-safe read: the payload, or None on ANY failure (logged)."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None  # cold store — not an error
        except OSError as e:
            log.warning("versioned store read failed: %s: %s", self._path, e)
            return None
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("versioned store corrupt JSON: %s: %s", self._path, e)
            return None
        if not isinstance(doc, dict) or not isinstance(doc.get("version"), int):
            log.warning("versioned store bad envelope: %s", self._path)
            return None
        if doc["version"] not in self._supported:
            log.warning(
                "versioned store version %s not in %s: %s",
                doc["version"],
                self._supported,
                self._path,
            )
            return None
        try:
            return self._decode(doc.get("data"))
        except Exception as e:  # noqa: BLE001 — shape failures degrade, never raise
            log.warning("versioned store shape invalid: %s: %s", self._path, e)
            return None

    def save(self, payload: object) -> None:
        """Atomic write (mkstemp + replace in the same directory); never raises."""
        try:
            doc = {"version": max(self._supported), "data": self._encode(payload)}
            blob = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        except Exception as e:  # noqa: BLE001 — an adapter encode bug must not crash callers
            log.warning("versioned store encode failed: %s: %s", self._path, e)
            return
        atomic_write_text(str(self._path), blob)


class DebouncedSave:
    """Optional coalescing wrapper: at most one write per idle window.

    ``request(payload)`` records the latest payload and (re)starts the
    idle timer; a write happens only after ``delay_s`` without a new
    request — the latest request wins. ``flush()`` writes pending
    changes synchronously; ``close()`` flushes and joins the worker
    thread. Never raises (the underlying save never raises), so it is
    safe to fire from background tasks.
    """

    def __init__(self, store: VersionedFileStore, delay_s: float) -> None:
        self._store = store
        self._delay = delay_s
        self._cond = threading.Condition()
        self._pending: object | None = None
        self._has_pending = False
        self._closed = False
        self._thread: threading.Thread | None = None

    def _worker(self) -> None:
        while True:
            with self._cond:
                if self._closed and not self._has_pending:
                    return
                if not self._has_pending:
                    self._cond.wait()
                    continue
                if self._closed:
                    # Shutdown with pending state: write it now (close()
                    # must not wait out the idle window).
                    self._has_pending = False
                    payload = self._pending
                else:
                    # Wait out the idle window (or until a new request /
                    # flush / close notifies).
                    self._cond.wait(self._delay)
                    if not self._has_pending:
                        continue  # flushed/consumed meanwhile
                    self._has_pending = False
                    payload = self._pending
            self._store.save(payload)

    def request(self, payload: object) -> None:
        """Record the latest payload; (re)schedule the debounced write."""
        with self._cond:
            if self._closed:
                immediate = True
            else:
                immediate = False
                self._pending = payload
                self._has_pending = True
                self._cond.notify_all()
                if self._thread is None:
                    self._thread = threading.Thread(
                        target=self._worker,
                        name="versioned-store-debounce",
                        daemon=True,
                    )
                    self._thread.start()
        if immediate:
            # After close: fall back to an immediate best-effort write.
            self._store.save(payload)

    def flush(self) -> None:
        """Write any pending payload synchronously (latest wins)."""
        with self._cond:
            payload = self._pending if self._has_pending else None
            self._has_pending = False
            self._cond.notify_all()
        if payload is not None:
            self._store.save(payload)

    def close(self) -> None:
        """Flush pending state and join the worker thread."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join()


class AsyncDebouncedSave:
    """Event-loop-native coalescing wrapper (spec #363).

    Mirrors ``DebouncedSave``'s request/flush/close semantics without the
    worker thread: ``request(payload)`` records the latest payload and
    arms a ``loop.call_later`` timer, so the write happens on the loop
    after ``delay_s`` without a new request — latest request wins. A
    sync caller (no running loop) falls back to writing immediately
    rather than dropping the state. ``flush()`` cancels the timer and
    writes any pending payload now; ``close()`` == flush — there is
    nothing to join, which is exactly why uvicorn's shutdown can call it
    synchronously where the thread-based wrapper would stall. After
    close, requests degrade to immediate best-effort writes. Never
    raises (the underlying save never raises).
    """

    def __init__(self, store: VersionedFileStore, delay_s: float) -> None:
        self._store = store
        self._delay = delay_s
        self._pending: object | None = None
        self._has_pending = False
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False

    def _write_pending(self) -> None:
        """Consume the pending payload (the timer callback / flush body)."""
        self._timer = None
        if not self._has_pending:
            return  # flushed/consumed meanwhile
        payload = self._pending
        self._has_pending = False
        self._pending = None
        if payload is None:
            return
        self._store.save(payload)

    def request(self, payload: object) -> None:
        """Record the latest payload; (re)schedule the debounced write.

        With a running event loop the write is deferred to
        ``loop.call_later``; without one (a direct sync caller) it
        happens immediately.
        """
        if self._closed:
            # After close: fall back to an immediate best-effort write.
            self._store.save(payload)
            return
        self._pending = payload
        self._has_pending = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_pending()
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer = loop.call_later(self._delay, self._write_pending)

    def flush(self) -> None:
        """Cancel the pending timer and write the payload now (latest wins)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._write_pending()

    def close(self) -> None:
        """Flush pending state; nothing to join (no worker thread)."""
        self.flush()
        self._closed = True
