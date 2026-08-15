"""Tests for the cache-only group content peek (ticket #216) and the
single-flight resolution guard (ticket #224).

The facade re-verifies a view card's Type against the item's RESOLVED
content when one is cached — the card parser (URL/section heuristic) is
a cheap guess, the content page is the truth. ``peek_group_content`` is
that re-verification read: it must NEVER trigger a provider fetch (a
cold group answers None without touching PROVIDERS/HTTP).

Seams under test:
- unit: cache hit returns the first-seen provider's ContentResponse.
- unit: cold cache -> None without a fetch (PROVIDERS is empty here; a
  fetch would raise, so None proves the peek is read-only).
- unit: unknown group / gated / blocklisted -> None.
- unit: first-seen provider wins (content cached only for a later
  provider is invisible).
- #224: concurrent resolve_group_content calls for the SAME group key
  share ONE upstream resolution — the app fires detail + seasons +
  playback for one item in the same tick (run8 re-hit animeon 4x in
  ~2s during a 502 storm; run7's cold walk ballooned to 59 fetches).
  Both the success and the failure verdicts are shared.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from cs_uk_api import catalog_state
from cs_uk_api.catalog_state import (
    _SOURCES_KEY,
    blocklist_cache,
    content_cache,
    gated_cache,
    sources_cache,
)
from cs_uk_api.models import ContentResponse, SearchResult, Translation
from cs_uk_api.providers.base import (
    BaseProvider,
    ProviderError,
)


def _result(pid: str, ext: str, media_type: str = "movie") -> SearchResult:
    mb_form, mb_styles = (
        (media_type, frozenset())
        if media_type in ("movie", "series")
        else ("series", frozenset({media_type}))
    )
    return SearchResult(
        id=f"{pid}:{ext}",
        provider=pid,
        form=mb_form,
        styles=mb_styles,
        title=f"{pid}-{ext}",
        year=2024,
        url=f"https://{pid}.example/{ext}",
    )


def _content(pid: str, ext: str, media_type: str = "series") -> ContentResponse:
    mb_form, mb_styles = (
        (media_type, frozenset())
        if media_type in ("movie", "series")
        else ("series", frozenset({media_type}))
    )
    return ContentResponse(
        id=f"{pid}:{ext}",
        title=f"{pid}-{ext}",
        year=2024,
        form=mb_form,
        styles=mb_styles,
        translations=[Translation(id="uk", label="Українська")],
    )


def _seed_sources(gk: str, *items: SearchResult) -> None:
    sources_cache.set(_SOURCES_KEY, {gk: {it.provider: it for it in items}})


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    for cache in (sources_cache, content_cache, gated_cache, blocklist_cache):
        cache.clear()
    try:
        yield
    finally:
        for cache in (sources_cache, content_cache, gated_cache, blocklist_cache):
            cache.clear()


def test_peek_returns_cached_content() -> None:
    _seed_sources("g2:abc", _result("p1", "ext"))
    content_cache.set("content:p1:ext", _content("p1", "ext"))
    got = catalog_state.peek_group_content("g2:abc")
    assert got is not None
    assert got.form == "series"


def test_peek_cold_returns_none_without_fetch() -> None:
    # No content cached; PROVIDERS is empty so a fetch would raise.
    _seed_sources("g2:abc", _result("p1", "ext"))
    assert catalog_state.peek_group_content("g2:abc") is None


def test_peek_unknown_group_returns_none() -> None:
    assert catalog_state.peek_group_content("g2:nope") is None


def test_peek_gated_returns_none() -> None:
    _seed_sources("g2:abc", _result("p1", "ext"))
    content_cache.set("content:p1:ext", _content("p1", "ext"))
    gated_cache.set("content:p1:ext", True)
    assert catalog_state.peek_group_content("g2:abc") is None


def test_peek_blocklisted_returns_none() -> None:
    _seed_sources("g2:abc", _result("p1", "ext"))
    content_cache.set("content:p1:ext", _content("p1", "ext"))
    blocklist_cache.set("content:p1:ext", True)
    assert catalog_state.peek_group_content("g2:abc") is None


def test_peek_prefers_first_seen_provider() -> None:
    # Content is cached only for the SECOND provider — the peek follows
    # the same first-seen resolution as resolve_group_content, so it must
    # not see it.
    _seed_sources("g2:abc", _result("p1", "ext1"), _result("p2", "ext2"))
    content_cache.set("content:p2:ext2", _content("p2", "ext2"))
    assert catalog_state.peek_group_content("g2:abc") is None
    # ... and it IS visible once the first-seen provider is cached.
    content_cache.set("content:p1:ext1", _content("p1", "ext1"))
    assert catalog_state.peek_group_content("g2:abc") is not None


class _SlowResolver(BaseProvider):
    """A provider whose ``content()`` blocks until released, counting
    invocations — the seam for the single-flight test."""

    id = "p1"
    name = "P1"
    types = ("movie",)
    newest_section = "page"

    def __init__(self) -> None:
        self.calls = 0
        self._entered = asyncio.Event()
        self._release = asyncio.Event()

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def browse(
        self, section: str, page: int, http: Any
    ) -> tuple[list[SearchResult], bool]:
        return [], False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        self.calls += 1
        self._entered.set()
        await self._release.wait()
        return _content("p1", external_id)

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> Any:
        raise NotImplementedError


async def test_resolve_group_content_single_flight_shares_one_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#224: two concurrent resolves of the SAME group key run ONE
    upstream ``content()`` call; the second caller waits for the first
    and reads its cached verdict."""
    _seed_sources("g2:abc", _result("p1", "ext"))
    resolver = _SlowResolver()
    catalog_state.PROVIDERS["p1"] = resolver
    try:
        t1 = asyncio.create_task(catalog_state.resolve_group_content("g2:abc"))
        t2 = asyncio.create_task(catalog_state.resolve_group_content("g2:abc"))
        await resolver._entered.wait()
        resolver._release.set()
        r1, r2 = await asyncio.gather(t1, t2)
        assert resolver.calls == 1
        assert r1 is not None and r2 is not None
        assert r1.id == r2.id == "p1:ext"
    finally:
        catalog_state.PROVIDERS.pop("p1", None)


class _FlakyResolver(BaseProvider):
    """A provider whose ``content()`` always fails (upstream blip)."""

    id = "p1"
    name = "P1"
    types = ("movie",)
    newest_section = "page"

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def browse(
        self, section: str, page: int, http: Any
    ) -> tuple[list[SearchResult], bool]:
        return [], False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        self.calls += 1
        raise ProviderError("unreachable", "upstream blip")

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> Any:
        raise NotImplementedError


async def test_resolve_group_content_single_flight_shares_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#224: when the shared resolution FAILS, concurrent callers get
    the leader's None verdict without each re-storming the upstream —
    run8's open step hit animeon 4x in ~2s for one key."""
    monkeypatch.setattr(catalog_state, "CONTENT_RETRY_DELAY_S", 0.0)
    _seed_sources("g2:abc", _result("p1", "ext"))
    resolver = _FlakyResolver()
    catalog_state.PROVIDERS["p1"] = resolver
    try:
        t1 = asyncio.create_task(catalog_state.resolve_group_content("g2:abc"))
        t2 = asyncio.create_task(catalog_state.resolve_group_content("g2:abc"))
        r1, r2 = await asyncio.gather(t1, t2)
        # ONE leader resolution: 2 content() calls = its own two retry
        # attempts. Two parallel resolvers would double that (4 calls).
        assert resolver.calls == 2
        assert r1 is None and r2 is None
    finally:
        catalog_state.PROVIDERS.pop("p1", None)
