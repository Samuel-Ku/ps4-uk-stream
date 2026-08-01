from __future__ import annotations

import time
from threading import Lock


class TtlCache:
    """A tiny in-memory TTL cache, no extra deps required."""

    def __init__(self, default_ttl_s: int) -> None:
        self._default_ttl_s = default_ttl_s
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = Lock()

    def get(self, key: str) -> object | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < now:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: object, ttl_s: int | None = None) -> None:
        ttl = self._default_ttl_s if ttl_s is None else ttl_s
        expires_at = time.monotonic() + max(ttl, 0)
        with self._lock:
            self._data[key] = (expires_at, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
