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
from cs_uk_api.models import ContentResponse, Episode, SearchResult, Season, Translation
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
# Viewer-state derivations (#347): translations / dub choice / pairing
# ---------------------------------------------------------------------------


def _series_card(pid: str, ext: str, title: str) -> SearchResult:
    return SearchResult(
        id=f"{pid}:{ext}",
        provider=pid,
        form="series",
        styles=frozenset(),
        title=title,
        year=2023,
        url=f"https://{pid}.example/{ext}",
    )


def _series_content(
    pid: str,
    ext: str,
    title: str,
    *,
    episode_ids: list[str],
    content_translations: list[Translation] | None = None,
    episode_translations: dict[str, list[Translation]] | None = None,
) -> ContentResponse:
    """A one-season series whose episodes may carry scoped dubs."""
    per_ep = episode_translations or {}
    eps = [
        Episode(
            number=i + 1,
            id=eid,
            title=f"Серія {i + 1}",
            translations=per_ep.get(eid),
        )
        for i, eid in enumerate(episode_ids)
    ]
    return ContentResponse(
        id=f"{pid}:{ext}",
        form="series",
        title=title,
        translations=content_translations or [Translation(id="uk", label="Українська")],
        seasons=[Season(number=1, episodes=eps)],
    )


async def _seed_series(
    monkeypatch: pytest.MonkeyPatch,
    content: ContentResponse,
) -> str:
    """Surface one series through the home build; return its group key."""
    _, ext = content.id.split(":", 1)
    card = _series_card("p1", ext, content.title)
    _register(_HomeStub("p1", newest=[card], content_map={ext: content}), monkeypatch)
    await catalog_api.refresh_snapshot()
    return item_group_key(card)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_playback_translations_episode_dubs_win_and_memory_reranks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An episode's own dubs are the candidate list (content dubs only as
    the fallback); the remembered label comes from the series' dub memory."""
    dubs = [Translation(id="uk", label="Дубляж"), Translation(id="vo", label="Оригінал")]
    content = _series_content(
        "p1",
        "show",
        "Серіал",
        episode_ids=["show:s1e1", "show:s1e2"],
        content_translations=[Translation(id="uk", label="Українська")],
        episode_translations={"show:s1e1": dubs},
    )
    gk = await _seed_series(monkeypatch, content)

    translations, remembered = await catalog_api.playback_translations("p1:show:s1e1")
    assert translations == dubs
    assert remembered is None

    catalog_api.remember_dub(gk, "Оригінал")
    translations, remembered = await catalog_api.playback_translations("p1:show:s1e1")
    assert translations == dubs
    assert remembered == "Оригінал"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_playback_translations_episode_falls_back_to_content_dubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An episode without its own translations serves the content's."""
    content_trans = [Translation(id="uk", label="Українська")]
    content = _series_content(
        "p1",
        "show",
        "Серіал",
        episode_ids=["show:s1e1"],
        content_translations=content_trans,
    )
    await _seed_series(monkeypatch, content)

    translations, remembered = await catalog_api.playback_translations("p1:show:s1e1")
    assert translations == content_trans
    assert remembered is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_playback_translations_movie_never_remembers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A movie group key serves the content translations and NEVER a
    remembered label (v3 decision — films start on the default dub)."""
    item = _item("p1", "Дюна", year=2021, n="dune")
    stub = _HomeStub("p1", newest=[item], content_map={"dune": _content("p1", "dune", "Дюна")})
    _register(stub, monkeypatch)
    await catalog_api.refresh_snapshot()
    gk = item_group_key(item)
    catalog_state.remember_dub(gk, "Дубляж")  # even a stray memory is ignored

    translations, remembered = await catalog_api.playback_translations(gk)
    assert [t.id for t in translations] == ["uk"]
    assert remembered is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_playback_translations_unresolvable_is_empty() -> None:
    """A non-group id with no resolvable group is ([], None), never a raise."""
    assert await catalog_api.playback_translations("p1:missing:e1") == ([], None)


def _t(tid: str, label: str) -> Translation:
    return Translation(id=tid, label=label)


_THREE = [_t("a", "Дубляж"), _t("b", "Оригінал"), _t("c", "Субтитри")]


@pytest.mark.unit
def test_candidates_keep_default_order_and_cap_at_eight() -> None:
    """Default-first: provider order survives untouched, capped at 8."""
    many = [_t(f"t{i}", f"L{i}") for i in range(10)]
    out = catalog_api.ordered_translation_candidates(many)
    assert [t.label for t in out] == [f"L{i}" for i in range(8)]


@pytest.mark.unit
def test_candidates_dedupe_by_label_first_wins() -> None:
    """Duplicate labels collapse to their first player (spec #276)."""
    ts = [_t("a", "Дубляж"), _t("b", "Оригінал"), _t("c", "Дубляж")]
    out = catalog_api.ordered_translation_candidates(ts)
    assert [t.id for t in out] == ["a", "b"]


@pytest.mark.unit
def test_candidates_picked_index_goes_first_one_based() -> None:
    """The picker's echoed AudioStreamIndex (1-based response position)
    moves that candidate first — the switch path."""
    out = catalog_api.ordered_translation_candidates(_THREE, picked_index=3)
    assert [t.id for t in out] == ["c", "a", "b"]


@pytest.mark.unit
def test_candidates_remembered_label_first_without_pick() -> None:
    """With no pick echoed, the remembered dub label goes first."""
    out = catalog_api.ordered_translation_candidates(_THREE, remembered="Оригінал")
    assert [t.id for t in out] == ["b", "a", "c"]


@pytest.mark.unit
def test_candidates_picked_outranks_remembered() -> None:
    """An explicit pick beats the memory (the client said what it wants)."""
    out = catalog_api.ordered_translation_candidates(
        _THREE, remembered="Оригінал", picked_index=1
    )
    assert [t.id for t in out] == ["a", "b", "c"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_dub_choice_maps_id_to_label_for_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The played translation id is stored as its LABEL for the series
    behind the episode wire id (the memory re-ranks by label)."""
    content = _series_content(
        "p1",
        "show",
        "Серіал",
        episode_ids=["show:s1e1"],
        episode_translations={"show:s1e1": [_t("vo", "Оригінал")]},
    )
    gk = await _seed_series(monkeypatch, content)

    await catalog_api.record_dub_choice("p1:show:s1e1", "vo")
    assert catalog_api.dub_for(gk) == "Оригінал"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_dub_choice_movie_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Movies never remember (v3 decision): a group-key item records
    nothing, even with a valid translation id."""
    item = _item("p1", "Дюна", year=2021, n="dune")
    stub = _HomeStub("p1", newest=[item], content_map={"dune": _content("p1", "dune", "Дюна")})
    _register(stub, monkeypatch)
    await catalog_api.refresh_snapshot()

    await catalog_api.record_dub_choice(item_group_key(item), "uk")
    assert catalog_api.dub_memory() == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_dub_choice_unknown_or_unresolvable_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort: an unknown translation id or an unresolvable item
    skips the memory without raising."""
    content = _series_content(
        "p1",
        "show",
        "Серіал",
        episode_ids=["show:s1e1"],
        episode_translations={"show:s1e1": [_t("vo", "Оригінал")]},
    )
    await _seed_series(monkeypatch, content)

    await catalog_api.record_dub_choice("p1:show:s1e1", "nope")
    await catalog_api.record_dub_choice("p1:none:e9", "vo")
    assert catalog_api.dub_memory() == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_playback_episode_pair_locates_episode_and_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pairing resolves the played wire id to its episode, season,
    series context — and the same-season next sibling."""
    content = _series_content(
        "p1", "show", "Серіал Тест", episode_ids=["show:s1e1", "show:s1e2"]
    )
    gk = await _seed_series(monkeypatch, content)

    pair = await catalog_api.playback_episode_pair("p1:show:s1e1")
    assert pair is not None
    assert pair.group_key == gk
    assert pair.provider_id == "p1"
    assert pair.series_title == "Серіал Тест"
    assert pair.season.number == 1
    assert pair.episode.id == "show:s1e1"
    assert pair.next_episode is not None
    assert pair.next_episode.id == "show:s1e2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_playback_episode_pair_last_episode_has_no_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _series_content(
        "p1", "show", "Серіал Тест", episode_ids=["show:s1e1", "show:s1e2"]
    )
    await _seed_series(monkeypatch, content)

    pair = await catalog_api.playback_episode_pair("p1:show:s1e2")
    assert pair is not None
    assert pair.episode.id == "show:s1e2"
    assert pair.next_episode is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_playback_episode_pair_non_episode_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group key (or an unknown id) pairs nothing — shows are not
    playable, D3."""
    content = _series_content("p1", "show", "Серіал Тест", episode_ids=["show:s1e1"])
    gk = await _seed_series(monkeypatch, content)

    assert await catalog_api.playback_episode_pair(gk) is None
    assert await catalog_api.playback_episode_pair("p1:none:e9") is None


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
