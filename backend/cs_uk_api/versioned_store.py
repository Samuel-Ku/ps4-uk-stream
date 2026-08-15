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

``DebouncedSave`` is the optional coalescing wrapper for adapters with
high-frequency writes (e.g. resume reports): ``request()`` records the
latest payload and writes at most once per idle window.
"""

from __future__ import annotations

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
