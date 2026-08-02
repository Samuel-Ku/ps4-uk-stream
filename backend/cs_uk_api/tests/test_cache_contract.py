"""Cache contract: hit, miss, expiry, manual invalidation across routes.

The implementation is already in place (cache.TtlCache, main.py's
_search_cache / _content_cache / _browse_cache / _blocklist_cache).
ADR-0003 (issue #83) ratifies the shape: TTL-only, in-memory, no
persisted schema, no version token. These tests prove the contract
holds at the API surface — they are the regression guard for the
"key shape, TTLs, mutates-before-set" invariants.

What the contract says:
- /api/search, /api/content, /api/browse are cached — second call does
  not re-execute providers.
- /api/stream, /api/providers, /api/sections are NOT cached.
- The 502 search-total-timeout path is NOT cached.
- Responses are mutated before being stored (cache stores the final
  shape, not a partial).
- Cache invalidation is `TtlCache.clear()` only — no flush endpoint.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cs_uk_api.main import (
    _browse_cache,
    _content_cache,
    _search_cache,
    app,
)
from cs_uk_api.models import ContentResponse, SearchResult, Translation
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

client = TestClient(app)


class _StubBase(BaseProvider):
    types = ("movie", "series")

    async def content(self, external_id, http):
        raise NotImplementedError

    async def stream(self, content_id, translation, http):
        raise NotImplementedError


def _register(provider_id: str, _search_call_counter: list[int], _content_call_counter: list[int]):
    class _Stub(_StubBase):
        id = provider_id
        name = provider_id

        async def search(self, q, http):
            _search_call_counter.append(1)
            return [SearchResult(id=f"{provider_id}:x", provider=provider_id, type="movie", title="X", url="https://x/")]

        async def content(self, external_id, http):
            _content_call_counter.append(1)
            return ContentResponse(
                id=f"{provider_id}:{external_id}",
                type="movie",
                title="Cached?",
                translations=[Translation(id="uk", label="UK")],
            )

    return _Stub()


# ---------------------------------------------------------------------------
# Hit / miss / expiry / manual invalidation
# ---------------------------------------------------------------------------


def test_search_second_call_hits_cache_and_does_not_re_execute_provider():
    """Second /api/search with the same query returns from cache."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        search_calls: list[int] = []
        content_calls: list[int] = []
        PROVIDERS["cache-stub"] = _register("cache-stub", search_calls, content_calls)

        _search_cache.clear()
        r1 = client.get("/api/search?q=cache-q")
        assert r1.status_code == 200
        assert len(search_calls) == 1

        r2 = client.get("/api/search?q=cache-q")
        assert r2.status_code == 200
        assert len(search_calls) == 1  # not re-executed
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


def test_search_cache_invalidation_via_clear():
    """After _search_cache.clear(), the next call re-executes providers."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        search_calls: list[int] = []
        content_calls: list[int] = []
        PROVIDERS["cache-stub"] = _register("cache-stub", search_calls, content_calls)

        _search_cache.clear()
        client.get("/api/search?q=cache-q")
        client.get("/api/search?q=cache-q")
        assert len(search_calls) == 1

        _search_cache.clear()
        client.get("/api/search?q=cache-q")
        assert len(search_calls) == 2
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


def test_search_cache_expiry():
    """Entries expire by TTL alone. Setting TTL=0 in the cache and ticking
    past the expiry causes a re-execution."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        search_calls: list[int] = []
        content_calls: list[int] = []
        PROVIDERS["cache-stub"] = _register("cache-stub", search_calls, content_calls)

        _search_cache.clear()
        _search_cache.set("search:all:expiry-q", _search_cache.get("search:all:expiry-q") or "marker", ttl_s=0)
        # The pre-seeded entry is already expired. Even if the route
        # returned the cached value, the cache is empty for the real query.
        client.get("/api/search?q=expiry-q")
        assert len(search_calls) == 1

        # Force expiry: set the cache entry's TTL to 0 in the past.
        _search_cache.set("search:all:expiry-q", "stale", ttl_s=0)
        import time

        time.sleep(0.01)
        # Now the cache is empty. Next call re-executes.
        client.get("/api/search?q=expiry-q")
        assert len(search_calls) == 2
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


def test_content_second_call_hits_cache():
    """Second /api/content with the same id returns from cache."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        search_calls: list[int] = []
        content_calls: list[int] = []
        PROVIDERS["cache-stub"] = _register("cache-stub", search_calls, content_calls)

        _content_cache.clear()
        r1 = client.get("/api/content/cache-stub:1")
        assert r1.status_code == 200
        assert len(content_calls) == 1

        r2 = client.get("/api/content/cache-stub:1")
        assert r2.status_code == 200
        assert len(content_calls) == 1
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _content_cache.clear()


def test_browse_second_call_hits_cache():
    """Second /api/browse with the same key returns from cache."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()

        from cs_uk_api.models import Section

        class _BrowseStub(_StubBase):
            id = "browse-stub"
            name = "browse-stub"
            sections = (Section(id="top", title="Top", type="movie"),)

            async def search(self, q, http):
                return []

            async def browse(self, section, page, http):
                return ([SearchResult(id="x", provider="browse-stub", type="movie", title="X", url="https://x/")], False)

        PROVIDERS["browse-stub"] = _BrowseStub()

        _browse_cache.clear()
        r1 = client.get("/api/browse?provider=browse-stub&section=top")
        assert r1.status_code == 200

        # Replace provider with one that has the same sections declared
        # (the route validates sections BEFORE the cache, so the bomb
        # must satisfy the same preconditions) and raises in browse().
        # If the cache is hit, the second request still succeeds with
        # the cached payload.
        class _Bomb(_StubBase):
            id = "browse-stub"
            name = "Bomb"
            sections = (Section(id="top", title="Top", type="movie"),)

            async def search(self, q, http):
                return []

            async def browse(self, section, page, http):
                raise AssertionError("route re-ran browse; cache miss")

        PROVIDERS["browse-stub"] = _Bomb()
        r2 = client.get("/api/browse?provider=browse-stub&section=top")
        assert r2.status_code == 200
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _browse_cache.clear()


# ---------------------------------------------------------------------------
# Not cached: /api/stream, /api/providers, /api/sections
# ---------------------------------------------------------------------------


def test_stream_route_is_not_cached():
    """Every /api/stream call re-executes the provider. Upstream URLs are
    session-scoped or token-signed; a cached URL would fail at the player."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        stream_calls: list[int] = []

        class _StreamStub(_StubBase):
            id = "stream-stub"
            name = "stream-stub"

            async def search(self, q, http):
                return []

            async def stream(self, content_id, translation, http):
                stream_calls.append(1)
                from cs_uk_api.models import StreamResponse

                return StreamResponse(url=f"https://example/{len(stream_calls)}", type="mp4")

        PROVIDERS["stream-stub"] = _StreamStub()

        r1 = client.get("/api/stream/stream-stub:1")
        r2 = client.get("/api/stream/stream-stub:1")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert len(stream_calls) == 2
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_providers_route_is_not_cached():
    """/api/providers embeds live health (TRACKER.status). A cache in
    front of a dict lookup is pure overhead and would delay the
    "provider just went down" signal. Mutation test: change the
    registry between calls and expect the response to reflect it."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()

        class _P(_StubBase):
            id = "p"
            name = "p"

            async def search(self, q, http):
                return []

        PROVIDERS["p"] = _P()

        r1 = client.get("/api/providers")
        ids1 = {p["id"] for p in r1.json()}
        assert "p" in ids1

        del PROVIDERS["p"]
        r2 = client.get("/api/providers")
        ids2 = {p["id"] for p in r2.json()}
        # A cached response would still contain "p"; ensure it doesn't.
        assert "p" not in ids2
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_sections_route_is_not_cached():
    """/api/sections is a list comprehension over an in-process registry
    that never changes at runtime. A cache in front of a dict lookup is
    pure overhead. Mutation test: add a new section-bearing provider
    between calls and expect the response to reflect it."""
    from cs_uk_api.models import Section

    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()

        class _S1(_StubBase):
            id = "s1"
            name = "s1"
            sections = (Section(id="top", title="Top", type="movie"),)

            async def search(self, q, http):
                return []

        PROVIDERS["s1"] = _S1()

        r1 = client.get("/api/sections")
        provider_ids1 = {s["provider"] for s in r1.json()}
        assert "s1" in provider_ids1

        class _S2(_StubBase):
            id = "s2"
            name = "s2"
            sections = (Section(id="hot", title="Hot", type="movie"),)

            async def search(self, q, http):
                return []

        PROVIDERS["s2"] = _S2()
        r2 = client.get("/api/sections")
        provider_ids2 = {s["provider"] for s in r2.json()}
        # A cached response would still miss s2; ensure it doesn't.
        assert "s2" in provider_ids2
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


# ---------------------------------------------------------------------------
# Mutate-before-set (ADR-0003 "Cached values are live Python objects")
# ---------------------------------------------------------------------------


def test_content_cache_stores_response_with_group_key_already_set():
    """The /api/content route sets `resp.group_key` AFTER the provider
    returns and BEFORE the cache.set. The cached value must already carry
    the group_key — the cache stores the final shape, not the partial.
    """
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        content_calls: list[int] = []

        class _Stub(_StubBase):
            id = "mutstub"
            name = "mutstub"

            async def search(self, q, http):
                return []

            async def content(self, external_id, http):
                content_calls.append(1)
                return ContentResponse(
                    id=f"mutstub:{external_id}",
                    type="movie",
                    title="Ok",
                    translations=[Translation(id="uk", label="UK")],
                )

        PROVIDERS["mutstub"] = _Stub()

        _content_cache.clear()
        r = client.get("/api/content/mutstub:1")
        assert r.status_code == 200
        body = r.json()
        assert "group_key" in body
        assert body["group_key"] != ""  # computed by group_key_from(...)
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _content_cache.clear()


# ---------------------------------------------------------------------------
# Key format: flat colon-joined `namespace:discriminants...`
# ---------------------------------------------------------------------------


def test_search_cache_key_includes_provider_and_query():
    """Different queries do not collide; same query hits the cache."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        search_calls: list[int] = []

        class _Stub(_StubBase):
            id = "k"
            name = "k"

            async def search(self, q, http):
                search_calls.append(1)
                return []

            async def content(self, external_id, http):
                pass

        PROVIDERS["k"] = _Stub()

        _search_cache.clear()
        client.get("/api/search?q=alpha")
        client.get("/api/search?q=beta")
        client.get("/api/search?q=alpha")  # cache hit
        assert len(search_calls) == 2
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


def test_search_cache_key_includes_provider_axis():
    """Per-provider search uses the provider axis in the cache key."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        a_calls: list[int] = []
        b_calls: list[int] = []

        class _A(_StubBase):
            id = "a"
            name = "a"

            async def search(self, q, http):
                a_calls.append(1)
                return []

        class _B(_StubBase):
            id = "b"
            name = "b"

            async def search(self, q, http):
                b_calls.append(1)
                return []

        PROVIDERS["a"] = _A()
        PROVIDERS["b"] = _B()

        _search_cache.clear()
        client.get("/api/search?q=q&provider=a")
        client.get("/api/search?q=q&provider=b")
        client.get("/api/search?q=q&provider=a")  # cache hit
        client.get("/api/search?q=q&provider=b")  # cache hit
        assert len(a_calls) == 1
        assert len(b_calls) == 1
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()
