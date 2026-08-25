"""Internal snapshot module (spec #309 T5) — home build + deep rows.

Covers ``cs_uk_api._catalog_state.snapshot`` directly: the sources-map
projection (member keys resolve the full provider union), the cold →
built ``load_home`` read path, the deep-row pool clearing on rebuild,
and the bounded non-extendable rows. The wire-level behaviour is pinned
elsewhere (test_home / test_row_deep / test_search_grouping); these
tests exercise the internal module's own contracts.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import respx

from cs_uk_api import _catalog_state as catalog_state
from cs_uk_api._catalog_state.snapshot import (
    _build_sources_map,
    _cache_home,
    extend_row_pool,
    get_home,
    load_home,
)
from cs_uk_api.home import build_home_rows
from cs_uk_api.models import HomeItem, SearchResult, Section
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider
from cs_uk_api.providers.yts import YtsProvider


def _item(
    pid: str,
    title: str,
    *,
    media_type: str = "movie",
    year: int | None = None,
    n: str = "1",
) -> SearchResult:
    mb_form, mb_styles  = {"movie": ("movie", frozenset()), "series": ("series", frozenset()), "anime": ("series", frozenset({"anime"})), "cartoon": ("series", frozenset({"cartoon"})), "dorama": ("series", frozenset({"dorama"}))}[media_type]
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
    """Non-extendable row kinds (personalized / rails / unknown) answer
    None — the caller serves the snapshot slice unchanged (spec #305,
    #362 C). The gate reads the row-kind table: a non-table kind is
    bounded via its absent entry (never a KeyError)."""
    from cs_uk_api.row_kinds import ROW_KINDS

    # «Рекомендовано для тебе» is recipe-inserted, not a table kind.
    assert "recommended" not in ROW_KINDS
    pool = asyncio.run(
        extend_row_pool(
            "recommended",
            [cast(HomeItem, {"group_key": "g2:x", "title": "X"})],
        )
    )
    assert pool is None


def test_extend_row_pool_bounded_for_non_extendable_table_kinds() -> None:
    """Table kinds pinned extendable=False stay bounded too (spec #362):
    the LLM idea slots and the personalized rows never page."""
    for kind in ("llm_idea_1", "llm_idea_2", "new_episodes", "recently_watched"):
        pool = asyncio.run(
            extend_row_pool(kind, [cast(HomeItem, {"group_key": "g2:x"})])
        )
        assert pool is None


def test_extend_row_pool_requires_snapshot_items() -> None:
    """An empty snapshot row never triggers a fetch — None."""
    pool = asyncio.run(extend_row_pool("movie", []))
    assert pool is None


# ---------------------------------------------------------------------------
# English lane on home (spec #374, ticket #380)
# ---------------------------------------------------------------------------

_YTS_FIX = Path(__file__).parent / "fixtures" / "yts"
_YTS_LIST_URL = re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")


class _UkrPeer(BaseProvider):
    """A Ukrainian-shaped peer: a newest listing plus form-declared
    sections — what every pre-#376 registry member looks like to the
    home build."""

    id = "ukr-peer"
    name = "UkrPeer"
    types = ("movie", "series")
    newest_section = "new"
    sections = (
        Section(id="films", title="Фільми", form="movie"),
        Section(id="serials", title="Серіали", form="series"),
    )

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
        if section == self.newest_section:
            return [
                _item(self.id, "Example Recent EN", year=2026),
                _item(self.id, "Оновлений серіал", media_type="series", n="u2"),
            ], False
        if section == "films":
            return [_item(self.id, "Example Recent EN", year=2026)], False
        if section == "serials":
            return [_item(self.id, "Оновлений серіал", media_type="series", n="u2")], False
        return [], False


def test_yts_newest_flows_into_recent_movie_under_existing_rules() -> None:
    """Ticket #380 AC1 (composition): with the REAL YtsProvider among
    providers, its fixture-pinned newest listing flows into «Нещодавно
    додані: Фільми» through the snapshot build path untouched — form-
    split admission, cross-provider dedupe against a Ukrainian peer's
    identical title, ∅ styles on the plain-English cards."""
    PROVIDERS.clear()
    PROVIDERS["ukr-peer"] = _UkrPeer()
    # Registered LAST, mirroring _registry.bootstrap()'s order-is-priority.
    PROVIDERS["yts"] = YtsProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_YTS_LIST_URL).respond(
            200, text=(_YTS_FIX / "newest_page1.json").read_text(encoding="utf-8")
        )
        home = asyncio.run(load_home())
    recent_movie = next(r for r in home.rows if r.type == "recent_movie")
    titles = [it.title for it in recent_movie.items]
    # Both fixture cards surface on the row…
    assert "Project SEKAI the Movie: Broken SEKAI and the Miku Who Couldn't Sing" in titles
    # …and the title the Ukrainian peer ALSO listed collapsed into ONE
    # merged card carrying both providers (the dedupe rule unchanged).
    assert titles.count("Example Recent EN") == 1
    shared = next(it for it in recent_movie.items if it.title == "Example Recent EN")
    assert set(shared.providers) == {"ukr-peer", "yts"}
    assert shared.form == "movie"
    assert shared.styles == frozenset()
    # The form split is intact: the English lane contributes no series,
    # so «Нещодавно додані: Серіали» stays the peer's alone.
    recent_series = next(r for r in home.rows if r.type == "recent_series")
    assert [it.title for it in recent_series.items] == ["Оновлений серіал"]


def test_yts_shaped_newest_interleaves_and_caps_like_any_provider() -> None:
    """Ticket #380 AC1 (cap leg): the recent-movie cap binds the English
    listings exactly like any provider's — round-robin interleave with
    the peer's newest, then the ``newest_limit`` cut."""
    rows = build_home_rows(
        newest={
            "ukr-peer": [
                _item("ukr-peer", "Фільм А", year=2021),
                _item("ukr-peer", "Фільм Б", year=2022, n="2"),
            ],
            "yts": [
                _item("yts", "Dune", year=2021),
                _item("yts", "Arrival", year=2016, n="2"),
            ],
        },
        popular={},
        by_type={},
        newest_limit=3,
    )
    recent = next(r for r in rows if r.type == "recent_movie")
    assert [it.title for it in recent.items] == ["Фільм А", "Dune", "Фільм Б"]
