"""Tests for the cache-only group content peek (ticket #216).

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
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

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
from cs_uk_api.providers.base import model_b_axes


def _result(pid: str, ext: str, media_type: str = "movie") -> SearchResult:
    mb_form, mb_styles = model_b_axes(cast(Any, media_type))
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
    mb_form, mb_styles = model_b_axes(cast(Any, media_type))
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
