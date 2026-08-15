"""Internal snapshot module (spec #309 T5) — home build + deep rows.

Covers ``cs_uk_api.catalog_state.snapshot`` directly: the sources-map
projection (member keys resolve the full provider union), the cold →
built ``load_home`` read path, the deep-row pool clearing on rebuild,
and the bounded non-extendable rows. The wire-level behaviour is pinned
elsewhere (test_home / test_row_deep / test_search_grouping); these
tests exercise the internal module's own contracts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest

from cs_uk_api import catalog_state
from cs_uk_api.catalog_state.snapshot import (
    _EXTENDABLE_ROWS,
    _build_sources_map,
    _cache_home,
    extend_row_pool,
    get_home,
    load_home,
)
from cs_uk_api.models import HomeItem, SearchResult
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, model_b_axes


def _item(
    pid: str,
    title: str,
    *,
    media_type: str = "movie",
    year: int | None = None,
    n: str = "1",
) -> SearchResult:
    mb_form, mb_styles = model_b_axes(cast(Any, media_type))
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form=mb_form,
        styles=mb_styles,
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
    )


@pytest.fixture(autouse=True)
def isolate() -> Iterator[None]:
    """Isolated registry + caches + stores (same pattern as test_home)."""
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (
        catalog_state.home_cache,
        catalog_state.sources_cache,
        catalog_state.row_deep_cache,
        catalog_state.deep_page_cache,
        catalog_state.content_cache,
        catalog_state.gated_cache,
    ):
        cache.clear()
    catalog_state.clear_playback()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_build_sources_map_registers_provider_union_under_every_member_key() -> None:
    """#161: merged member keys (yearful + yearless pairs) each resolve
    the FULL first-seen provider union — a chip on any member opens the
    whole group."""
    newest = {
        "p1": [
            _item("p1", "Дюна", year=2021),
            _item("p1", "Дюна", year=None, n="2"),  # yearless member
        ],
        "p2": [
            _item("p2", "Дюна", year=2021, n="3"),
        ],
    }
    sources = _build_sources_map(newest, {}, {})
    assert sources, "the merged group must be registered"
    for key in sources:
        assert {p for p in sources[key]} == {"p1", "p2"}
        # First-seen order preserved: p1 before p2.
        assert list(sources[key]) == ["p1", "p2"]


def test_get_home_cold_is_none_then_load_builds() -> None:
    """The read accessor never builds (cold → None); load_home builds
    and caches the snapshot with the stub's rows."""

    class _Stub(BaseProvider):
        id = "snap"
        name = "Snap"
        types = ("movie",)
        newest_section = "new"

        async def search(self, query, http):  # type: ignore[no-untyped-def]
            return []

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
            return [_item("snap", "Фільм А", year=2021)], False

    PROVIDERS["snap"] = _Stub()
    home = asyncio.run(load_home())
    # The newest listing surfaces a row carrying the stub's card.
    titles = [it.title for r in home.rows for it in r.items]
    assert "Фільм А" in titles
    # Cached now: the read accessor answers without a build.
    assert get_home() is not None


def test_cache_home_clears_deep_row_pools_on_rebuild() -> None:
    """A new snapshot invalidates the snapshot-anchored deep pools."""
    newest = {"p1": [_item("p1", "Дюна", year=2021)]}
    row_deep = catalog_state.row_deep_cache
    row_deep.set("row-deep:movie", [_item("p1", "Старий", n="9")])
    _cache_home(newest, {}, {})
    assert row_deep.get("row-deep:movie") is None


def test_extend_row_pool_bounded_for_personalized_rows() -> None:
    """Non-extendable row kinds (personalized / rails) answer None —
    the caller serves the snapshot slice unchanged (spec #305)."""
    assert "recommended" not in _EXTENDABLE_ROWS
    pool = asyncio.run(
        extend_row_pool(
            "recommended",
            [cast(HomeItem, {"group_key": "g2:x", "title": "X"})],
        )
    )
    assert pool is None


def test_extend_row_pool_requires_snapshot_items() -> None:
    """An empty snapshot row never triggers a fetch — None."""
    pool = asyncio.run(extend_row_pool("movie", []))
    assert pool is None
