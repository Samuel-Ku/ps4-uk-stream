"""Typed catalog interface (spec #309 step 2 / ticket #311) — contracts.

Each accessor's documented shape, tested against the REAL delegate (the
expand phase: existing callers keep working unchanged): snapshot
read/refresh, typed item resolution, search with registration folded in,
typed playback entries, viewer state and profiles. No cache keys or raw
dict shapes on the seam — the tests assert the TYPED shapes.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from cs_uk_api import catalog as catalog_api
from cs_uk_api import _catalog_state as catalog_state, health
from cs_uk_api.merge import item_group_key
from cs_uk_api.models import ContentResponse, SearchResult, Translation
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


def _content(pid: str, ext: str, title: str) -> ContentResponse:
    return ContentResponse(
        id=f"{pid}:{ext}",
        provider=pid,
        form="movie",
        title=title,
        translations=[Translation(id="uk", label="Українська")],
        group_key="",
    )


@pytest.fixture(autouse=True)
def isolate() -> Iterator[None]:
    """Isolated registry + caches + stores (same pattern as test_home)."""
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (
        catalog_state.home_cache,
        catalog_state.search_cache,
        catalog_state.sources_cache,
        catalog_state.content_cache,
        catalog_state.gated_cache,
        catalog_state.blocklist_cache,
        catalog_state.row_deep_cache,
        catalog_state.deep_page_cache,
    ):
        cache.clear()
    saved_profiles = dict(catalog_state.get_profiles())
    catalog_api.install_profiles({})
    catalog_state.clear_playback()
    catalog_state.clear_user_state()
    health.TRACKER.reset()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        catalog_api.install_profiles(saved_profiles)
        health.TRACKER.reset()


class _HomeStub(BaseProvider):
    """newest_section provider whose content() serves a small map."""

    def __init__(
        self,
        pid: str,
        *,
        newest: list[SearchResult],
        content_map: dict[str, ContentResponse] | None = None,
    ) -> None:
        self.id = pid
        self.name = pid.title()
        self.types = ("movie",)
        self.newest_section = "new"
        self._newest = list(newest)
        self._content_map = dict(content_map or {})

    async def search(self, query: str, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id: str, http):  # type: ignore[no-untyped-def]
        if external_id in self._content_map:
            return self._content_map[external_id]
        raise NotImplementedError(f"content {external_id} not stubbed")

    async def stream(self, content_id: str, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def browse(self, section: str, page: int, http):  # type: ignore[no-untyped-def]
        if section == self.newest_section:
            return list(self._newest), False
        raise NotImplementedError(f"section {section} not stubbed")


class _SearchStub(BaseProvider):
    """Minimal search-only stub for the folded-in registration test."""

    def __init__(self, pid: str, results: list[SearchResult]) -> None:
        self.id = pid
        self.name = pid.title()
        self.types = ("movie",)
        self._results = list(results)

    async def search(self, query: str, http):  # type: ignore[no-untyped-def]
        return list(self._results)

    async def content(self, external_id: str, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id: str, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _register(stub: BaseProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(PROVIDERS, stub.id, stub)


# ---------------------------------------------------------------------------
# Snapshot: read / refresh
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_snapshot_cold_cache_is_none() -> None:
    """The read accessor must NOT trigger a build — cold cache → None."""
    assert catalog_api.snapshot() is None
    assert catalog_state.get_home() is None  # nothing was built


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refresh_snapshot_builds_and_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """refresh_snapshot builds the home; the same rows serve via snapshot."""
    stub = _HomeStub("p1", newest=[_item("p1", "Дюна", year=2021, n="dune")])
    _register(stub, monkeypatch)

    home = await catalog_api.refresh_snapshot()
    assert home.rows  # the form-split «Нещодавно додані» row is present
    newest = next(row for row in home.rows if row.type == "recent_movie")
    assert newest.items[0].title == "Дюна"

    # The read accessor now serves the SAME snapshot, no rebuild.
    assert catalog_api.snapshot() is home


# ---------------------------------------------------------------------------
# Item resolution: typed verdicts
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_item_ok_verdict_with_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolvable group resolves to the OK verdict with its content."""
    item = _item("p1", "Дюна", year=2021, n="dune")
    content = _content("p1", "dune", "Дюна")
    stub = _HomeStub("p1", newest=[item], content_map={"dune": content})
    _register(stub, monkeypatch)
    await catalog_api.refresh_snapshot()

    resolution = await catalog_api.resolve_item(item_group_key(item))
    assert resolution.verdict == catalog_api.ItemVerdict.OK
    assert resolution.content is content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_item_unavailable_verdict_for_unknown_key() -> None:
    """An unknown group key is the UNAVAILABLE verdict, never a raise."""
    resolution = await catalog_api.resolve_item("g2:" + "0" * 16)
    assert resolution.verdict == catalog_api.ItemVerdict.UNAVAILABLE
    assert resolution.content is None


# ---------------------------------------------------------------------------
# Search: registration folded in (US3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_folds_group_registration_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """A searched card resolves through the group-key map WITHOUT a manual
    register_search_groups call — the fold-in (US3)."""
    hit = _item("p1", "Смолфут", year=2018, n="smol")
    _register(_SearchStub("p1", [hit]), monkeypatch)

    resp = await catalog_api.search("смолфут")
    assert len(resp.groups) == 1
    gk = resp.groups[0].group_key
    assert gk == item_group_key(hit)

    # Registration happened as part of search(): the detail surface can
    # now resolve the searched card via the shared resolution map.
    per_provider = catalog_state.resolve_group(gk)
    assert per_provider is not None
    assert per_provider["p1"].id == hit.id


# ---------------------------------------------------------------------------
# Playback: typed entries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_playback_positions_typed_shape() -> None:
    """record_position + playback_positions round-trip as the typed
    PlaybackPosition (no bare tuple on the seam)."""
    catalog_api.record_position("p1:s1e1", 600, runtime_ticks=1_000)
    catalog_api.record_position("g2:0000000000000000", 300)

    positions = catalog_api.playback_positions()
    assert positions["p1:s1e1"] == catalog_api.PlaybackPosition(
        position_ticks=600, runtime_ticks=1_000
    )
    assert positions["g2:0000000000000000"] == catalog_api.PlaybackPosition(
        position_ticks=300, runtime_ticks=None
    )


@pytest.mark.unit
def test_recent_playback_orders_and_caps() -> None:
    """Most recently updated first, capped at ``limit``."""
    catalog_api.record_position("p1:e1", 100)
    catalog_api.record_position("p1:e2", 200)
    catalog_api.record_position("p1:e3", 300)

    recent = catalog_api.recent_playback(limit=2)
    assert list(recent) == ["p1:e3", "p1:e2"]
    assert recent["p1:e3"].position_ticks == 300


@pytest.mark.unit
def test_recent_history_most_recent_first() -> None:
    """recent_history returns played ids most-recently-seen, active AND
    finished (the «Нещодавно переглянуто» row's input)."""
    catalog_api.record_position("p1:e1", 100)
    catalog_api.record_position("p1:e2", 0)  # zero positions are ignored
    catalog_api.record_position("p1:e2", 200)
    assert catalog_api.recent_history() == ["p1:e2", "p1:e1"]


# ---------------------------------------------------------------------------
# Viewer state: favorites / played / dub memory
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_favorites_and_played_round_trip() -> None:
    catalog_api.set_favorite("g2:0000000000000000", True)
    catalog_api.set_played("p1:s1e1", True)
    assert catalog_api.is_favorite("g2:0000000000000000") is True
    assert catalog_api.is_played("p1:s1e1") is True
    assert catalog_api.is_favorite("p1:s1e1") is False
    catalog_api.set_favorite("g2:0000000000000000", False)
    assert catalog_api.is_favorite("g2:0000000000000000") is False


@pytest.mark.unit
def test_dub_memory_round_trip() -> None:
    catalog_api.remember_dub("g2:0000000000000000", "Українська (дубляж)")
    assert catalog_api.dub_for("g2:0000000000000000") == "Українська (дубляж)"
    assert catalog_api.dub_for("g2:1111111111111111") is None
    assert catalog_api.dub_memory() == {"g2:0000000000000000": "Українська (дубляж)"}


# ---------------------------------------------------------------------------
# Profiles: get / install
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_profiles_cold_store_is_empty() -> None:
    """A cold profile store is the honest empty mapping — callers fall
    back, never branch on a sentinel."""
    assert catalog_api.profiles() == {}


@pytest.mark.unit
async def test_refresh_profile_degrades_without_llm_knobs() -> None:
    """Without LLM configuration the refresh returns False and never
    raises — the previous (absent) profile stays active."""
    assert await catalog_api.refresh_profile() is False
