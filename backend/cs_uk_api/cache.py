"""Cache contract: TTL-only, in-memory, no persisted schema (ADR-0003).

This module is the only cache primitive in the backend. The contract is
ratified by `docs/adr/0003-cache-contract.md` and applies to every
endpoint that consults a `TtlCache`:

  - **Key format**: a flat colon-joined ``"{namespace}:{discriminants...}"``
    string. The discriminants are *every* request parameter that can
    change the response (provider axis, query, page, etc.). No
    structured-tuple key, no query normalization.
  - **TTL**: per-endpoint, configured via `Settings.cache_*_s`. Search
    and browse share 5m; content and blocklist share 30m; posters 1h
    in memory + 7d on disk (separate module).
  - **Invalidation**: TTL-only. No flush endpoint. No event-driven
    invalidation. A process restart is the global flush, and it is
    free because the store is in-memory.
  - **No stampede protection**: no per-key single-flight lock. The
    fan-out this would prevent requires concurrent misses on an
    identical key, which this deployment does not produce.
  - **Scope**: in-memory, single-process. No Redis, no SQLite, no
    shared memory. The poster disk layer is the deliberate exception
    — it is cross-process and cross-restart by design.
  - **No version token**: the invariant is "no value carrying a domain
    schema is ever persisted beyond process lifetime". A code change
    is a restart is an empty cache. The poster disk caches satisfy
    the invariant because they store opaque image bytes under a
    content-addressed key.
  - **Mutate before caching, never after**: handlers set
    ``resp.group_key`` (or any other post-fetch field) BEFORE the
    `set` call, so the cache holds the final shape.

What is NOT cached anywhere:
  - `/api/stream/{id}` — session-scoped upstream URLs.
  - `/api/providers` — embeds `TRACKER.status(p.id)`, live health.
  - `/api/sections` — a dict comprehension over the in-process registry.

Error responses (e.g. 404 not_found, 502 upstream_unreachable) are
NEVER cached. The deliberate exception is the Russian-content blocklist
(`/api/content/{id}` short-circuit), which is a deterministic property
of the item, not a failure.
"""

from __future__ import annotations

import time
from threading import Lock


class TtlCache:
    """A tiny in-memory TTL cache, no extra deps required.

    See module docstring for the wire-level contract (key format, TTL,
    invalidation, scope). The class implements the contract; the
    module docstring is its authoritative description.
    """

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
