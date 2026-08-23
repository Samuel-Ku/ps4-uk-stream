"""Tests for the Home API (issue #70).

Seams under test:

  - ``cs_uk_api.home.build_home_rows`` — the pure orchestrator that turns
    pre-collected per-provider listings into the HomeResponse rows
    («Нещодавно додані: Фільми» / «: Серіали» — the form-split rows that
    replaced «Новинки», «Популярні зараз», and the five type rows, spec
    #263). Dedup by groupKey, round-robin ordering, top-up under the cap,
    present-only-when-provider-supplies semantics for «Популярні зараз».

  - ``cs_uk_api.home.build_genre_rows`` — the Netflix-style genre rails
    (spec #263): top genres by profile-store coverage become rows with
    Ukrainian labels, recency-ranked, capped; genres below a coverage
    threshold are skipped.

  - ``GET /api/home`` — the route that calls each provider's
    ``newest_section`` / ``popular`` browse() / type sections, fans out
    in parallel, caches for 30 min, and returns a HomeResponse.

  - ``GET /api/content/{groupKey}`` — the groupKey lookup route that
    returns the merged item + provider list (or 404 for unknown key).
    Discriminated on the ``g2:`` prefix vs the existing
    ``provider:external`` content_id semantics.

  - Cache TTL — 30 min (issue #70 AC: 30-min cache or documented
    staleness behaviour).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import health
from cs_uk_api._catalog_state import home_cache
from cs_uk_api.home import (
    aggregate_by_group_key,
    build_genre_rows,
    build_home_rows,
    round_robin_dedup,
)
from cs_uk_api.main import app
from cs_uk_api.models import (
    ContentResponse,
    HomeItem,
    SearchResult,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider
from cs_uk_api.recommend import ItemProfile

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def item(
    pid: str,
    title: str,
    media_type: str = "movie",
    year: int | None = None,
    n: str = "1",
) -> SearchResult:
    mb_form, mb_styles = (
        (media_type, frozenset())
        if media_type in ("movie", "series")
        else ("series", frozenset({media_type}))
    )
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form=mb_form,
        styles=mb_styles,
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
    )


def _group_key(pid: str, title: str) -> str:
    """The merged group key the builder sees for a seeded series item
    (spec #267 T3) — same derivation as ``item_group_key`` over the
    item the helper builds."""
    from cs_uk_api.merge import item_group_key

    return item_group_key(item(pid, title, media_type="series", n="1"))


def _item_key(it: SearchResult) -> str:
    """The group key of an actual seeded item (spec #272) — for history
    groups the key must match the item that will resolve it."""
    from cs_uk_api.merge import item_group_key

    return item_group_key(it)


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    # /api/home fans out to every provider in ``PROVIDERS``; tests that
    # exercise the route must run against an isolated registry (otherwise
    # real upstream calls leak into the response and the assertions
    # race against real network state). Tests that don't touch /api/home
    # just get the snapshot back unchanged.
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    home_cache.clear()
    health.TRACKER.reset()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        home_cache.clear()
        health.TRACKER.reset()


# A minimal in-test provider that supports both the ``newest_section``
# opt-in (used by «Новинки») and one or more type-bucketed sections.
# The tests register an instance in PROVIDERS via monkeypatch.setitem and
# assert on the resulting /api/home response.


class _HomeStub(BaseProvider):
    """Test provider with a controllable newest + type + popular layout."""

    def __init__(
        self,
        pid: str,
        *,
        newest: list[SearchResult] | None = None,
        popular: list[SearchResult] | None = None,
        sections: tuple[Any, ...] = (),
        newest_section: str | None = None,
    ) -> None:
        self.id = pid
        self.name = pid.title()
        self.types = ("movie",)
        self._newest = newest
        self._popular = popular
        self.sections = sections
        self.newest_section = newest_section

    async def search(self, query: str, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id: str, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id: str, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def browse(self, section: str, page: int, http):  # type: ignore[no-untyped-def]
        if self.newest_section is not None and section == self.newest_section:
            return list(self._newest or []), False
        if section == "popular":
            return list(self._popular or []), False
        raise NotImplementedError(f"section {section} not stubbed")


def _register(stub: BaseProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a stub provider, removing it after the test via the
    fixture cleanup pattern — monkeypatch.setitem handles restore."""
    monkeypatch.setitem(PROVIDERS, stub.id, stub)


# ---------------------------------------------------------------------------
# round_robin_dedup — the algorithm
# ---------------------------------------------------------------------------


def test_round_robin_dedup_round_robins_across_providers() -> None:
    """P1[0], P2[0], P3[0], P1[1], P2[1], P3[1], ... across one pass per round.

    Each title must be unique across the dataset so dedup-by-groupKey
    doesn't collapse them — group_key is derived from the item's own
    title/type/year, so distinct titles yield distinct keys.
    """
    by_provider = {
        "p1": [item("p1", "A"), item("p1", "B"), item("p1", "C")],
        "p2": [item("p2", "X"), item("p2", "Y")],
        "p3": [item("p3", "Q")],
    }
    out = round_robin_dedup(by_provider, limit=20)
    assert [it.title for it in out] == ["A", "X", "Q", "B", "Y", "C"]


def test_round_robin_dedup_dedups_by_group_key() -> None:
    """Same title across two providers collapses to one row with both providers listed."""
    by_provider = {
        "p1": [item("p1", "Дюна", year=2021, n="1")],
        "p2": [item("p2", "Дюна", year=2021, n="2")],
    }
    out = round_robin_dedup(by_provider, limit=20)
    assert len(out) == 1
    assert out[0].title == "Дюна"
    assert set(out[0].providers) == {"p1", "p2"}


def test_round_robin_dedup_respects_limit() -> None:
    by_provider = {
        "p1": [item("p1", f"A{i}") for i in range(10)],
        "p2": [item("p2", f"X{i}") for i in range(10)],
    }
    out = round_robin_dedup(by_provider, limit=5)
    assert len(out) == 5


def test_round_robin_dedup_empty_input() -> None:
    assert round_robin_dedup({}, limit=20) == []


def test_round_robin_dedup_provider_with_empty_list() -> None:
    out = round_robin_dedup(
        {"p1": [item("p1", "A")], "p2": []},
        limit=20,
    )
    assert len(out) == 1
    assert out[0].title == "A"


def test_round_robin_dedup_stops_when_all_providers_exhausted() -> None:
    """One provider with 2 items, the other with 3 unique items: round-robin
    stops when the smallest provider is exhausted."""
    by_provider = {
        "p1": [item("p1", "A"), item("p1", "B")],
        "p2": [item("p2", "X"), item("p2", "Y"), item("p2", "Z")],
    }
    out = round_robin_dedup(by_provider, limit=100)
    # p1 contributes 2 (A, B); p2 contributes 3 (X, Y, Z) = 5 total.
    assert len(out) == 5


def test_round_robin_dedup_dedups_same_pid_same_key() -> None:
    """Regression (HIGH from code review): when the same provider surfaces
    the same groupKey from two listings (e.g. a duplicate in the upstream
    feed), the resulting ``providers`` list must not contain the pid twice.

    The canonical round-robin path's ``providers`` list is the union of
    distinct provider ids; "union" implies unique. This was previously
    leaking duplicates."""
    by_provider = {
        "p1": [
            item("p1", "Дюна", year=2021, n="1"),
            item("p1", "Дюна", year=2021, n="2"),
        ],
    }
    out = round_robin_dedup(by_provider, limit=20)
    assert len(out) == 1
    assert out[0].providers == ["p1"]


def test_round_robin_dedup_year_soft_collapses_to_single_row() -> None:
    """H1 (HIGH from #71 code review): cross-route groupKey divergence.

    ``merge_results`` (used by /api/search) treats "Дюна 2021" and "Дюна"
    (no year anywhere) as the SAME title via the year-soft rule
    (``_years_match``: a None year is treated as compatible), and the
    canonical group_key is the YEARFUL member's ``item_group_key``
    (yearful-preferred-min — see ``merge.py:merge_results``).

    The previous ``round_robin_dedup`` used each item's raw
    ``item_group_key`` as the dedup key. The two items have DIFFERENT
    per-item keys (the digest hashes ``year or ''``), so they ended up
    as two HomeItems with two distinct group_keys — breaking the
    round-trip: a client that picked the group_key from /api/search
    would 404 on /api/content/{group_key} if the same title had only
    surfaced via /api/home, and vice versa.

    Fix: ``round_robin_dedup`` now delegates to ``merge_results`` so the
    two routes share ONE merge core and ONE group_key identity.
    """
    from cs_uk_api.merge import item_group_key, merge_results

    p1 = item("p1", "Дюна", year=2021, n="1")
    p2 = item("p2", "Дюна", year=None, n="2")
    # Sanity: the two items have DIFFERENT per-item keys (the year field
    # is part of the digest). This is the precondition for the H1 bug.
    assert item_group_key(p1) != item_group_key(p2)
    # Sanity: merge_results DOES collapse them (year-soft rule).
    assert len(merge_results([p1, p2])) == 1

    by_provider = {"p1": [p1], "p2": [p2]}
    out = round_robin_dedup(by_provider, limit=20)

    # One row, not two.
    assert len(out) == 1
    # Both providers in the union.
    assert set(out[0].providers) == {"p1", "p2"}
    # Canonical key = the yearful member's item_group_key.
    assert out[0].group_key == item_group_key(p1)


def test_round_robin_dedup_year_soft_same_provider_collapses() -> None:
    """H1 variant: a single provider that emits a yearful + yearless
    listing for the same logical title must produce ONE HomeItem with
    that provider listed once (and the yearful key)."""
    from cs_uk_api.merge import item_group_key

    by_provider = {
        "p1": [
            item("p1", "Дюна", year=2021, n="1"),
            item("p1", "Дюна", year=None, n="2"),
        ],
    }
    out = round_robin_dedup(by_provider, limit=20)
    assert len(out) == 1
    assert out[0].providers == ["p1"]
    assert out[0].group_key == item_group_key(item("p1", "Дюна", year=2021, n="1"))


def test_round_robin_dedup_all_yearless_collapses_to_single_per_item_key() -> None:
    """Sanity edge for H1: when ALL members are yearless (no year
    anywhere), every member's ``item_group_key`` is identical (the
    year field is hashed as ``''`` for everyone), so the yearful-
    preferred-min rule degenerates to "the single shared key". The
    group collapses to one row carrying that key — confirming the
    cross-route invariant holds even at the degenerate tail of the
    yearful-preferred-min rule.

    The ``min()`` over identical keys is no-op; this test pins the
    behaviour at the degenerate case, not the min-comparison logic."""
    from cs_uk_api.merge import item_group_key

    p1 = item("p1", "Дюна", year=None, n="1")
    p2 = item("p2", "Дюна", year=None, n="2")
    # Sanity: the two items share one per-item key (no year field
    # differentiates them).
    assert item_group_key(p1) == item_group_key(p2)

    by_provider = {"p1": [p1], "p2": [p2]}
    out = round_robin_dedup(by_provider, limit=20)
    assert len(out) == 1
    assert set(out[0].providers) == {"p1", "p2"}
    assert out[0].group_key == item_group_key(p1)


def test_round_robin_dedup_year_soft_does_not_collapse_different_years() -> None:
    """Regression guard: the year-soft collapse must NOT swallow distinct
    years (e.g. Дюна 2021 vs Дюна 1984). When both items have raw years
    and they don't match, the dedup keeps them as separate rows.

    This is the floor under the H1 fix — without this guard, a too-eager
    collapse would silently merge different movies into one group_key."""
    by_provider = {
        "p1": [item("p1", "Дюна", year=2021, n="1")],
        "p2": [item("p2", "Дюна", year=1984, n="2")],
    }
    out = round_robin_dedup(by_provider, limit=20)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# aggregate_by_group_key — collision-only step (used inside rows)
# ---------------------------------------------------------------------------


def test_aggregate_by_group_key_combines_providers_in_first_seen_order() -> None:
    """When ``round_robin_dedup`` sees the same groupKey twice (different
    providers), ``aggregate_by_group_key`` collapses them into one row
    carrying both providers, in first-seen order."""
    items = [
        HomeItem(
            group_key="g2:dune", title="Дюна", year=2021, form="movie",
            poster=None, providers=["p1"],
        ),
        HomeItem(
            group_key="g2:dune", title="Дюна", year=2021, form="movie",
            poster=None, providers=["p2"],
        ),
        HomeItem(
            group_key="g2:smol", title="Смолфут", year=2018, form="movie",
            poster=None, providers=["p3"],
        ),
    ]
    agg = aggregate_by_group_key(items)
    assert len(agg) == 2
    dune = next(it for it in agg if it.title == "Дюна")
    assert dune.providers == ["p1", "p2"]


# ---------------------------------------------------------------------------
# build_home_rows — the orchestrator
# ---------------------------------------------------------------------------


def test_build_home_rows_emits_recent_movie_row_when_any_provider_has_newest() -> None:
    """#263 T1: a movie-form newest item lands in the «Нещодавно додані:
    Фільми» row (the form-split rail that replaced «Новинки»)."""
    rows = build_home_rows(
        newest={"p1": [item("p1", "A")]},
        popular={},
        by_type={},
        newest_limit=20,
    )
    assert len(rows) == 1
    assert rows[0].title == "Нещодавно додані: Фільми"
    assert rows[0].type == "recent_movie"
    assert rows[0].items[0].title == "A"


def test_build_home_rows_splits_newest_by_form_into_two_rows() -> None:
    """#263 T1: movies and series each get their OWN recent row — the
    whole point of the form split."""
    rows = build_home_rows(
        newest={
            "p1": [
                item("p1", "Фільм А", media_type="movie"),
                item("p1", "Серіал Б", media_type="series"),
            ]
        },
        popular={},
        by_type={},
        newest_limit=20,
    )
    assert [r.type for r in rows] == ["recent_movie", "recent_series"]
    assert rows[0].title == "Нещодавно додані: Фільми"
    assert [it.title for it in rows[0].items] == ["Фільм А"]
    assert rows[1].title == "Нещодавно додані: Серіали"
    assert [it.title for it in rows[1].items] == ["Серіал Б"]


def test_build_home_rows_omits_recent_rows_when_no_matching_form() -> None:
    """#263 T1: a row whose form has no newest items is omitted — empty
    rows don't ship (a series-only catalog shows only the series rail)."""
    rows = build_home_rows(
        newest={"p1": [item("p1", "Серіал Б", media_type="series")]},
        popular={},
        by_type={},
        newest_limit=20,
    )
    assert [r.type for r in rows] == ["recent_series"]


def test_build_home_rows_recent_row_topped_up_from_type_section() -> None:
    """#263 T1: a recent row under the cap is topped up from the
    form-section page-1 items (the same data the type rows use) —
    Netflix-style overlap accepted, deduped within the row."""
    rows = build_home_rows(
        newest={"p1": [item("p1", "Новий фільм")]},
        popular={},
        by_type={
            "movie": {
                "p1": [
                    item("p1", "Новий фільм", n="dup"),  # same group as newest
                    item("p1", "Старий фільм", n="2"),
                ]
            }
        },
        newest_limit=20,
    )
    recent = next(r for r in rows if r.type == "recent_movie")
    titles = [it.title for it in recent.items]
    # The dup collapses; the section-only film tops the row up.
    assert titles == ["Новий фільм", "Старий фільм"]


def test_build_home_rows_recent_row_respects_form_topup_filter() -> None:
    """#263 T1: the top-up only takes FORM-matching section items — a
    series mis-filed in the movie section must not leak into the movie
    recent row."""
    rows = build_home_rows(
        newest={},
        popular={},
        by_type={
            "movie": {
                "p1": [
                    item("p1", "Фільм"),
                    item("p1", "Серіал-в-секції", media_type="series", n="2"),
                ]
            }
        },
        newest_limit=20,
    )
    recent = next(r for r in rows if r.type == "recent_movie")
    assert [it.title for it in recent.items] == ["Фільм"]


def test_build_home_rows_omits_recent_rows_when_no_form_data_anywhere() -> None:
    """#263 T1: with no newest items AND no form-section items for a
    form, its recent row is omitted (empty rows don't ship)."""
    rows = build_home_rows(
        newest={},
        popular={},
        by_type={"series": {"p1": [item("p1", "S", media_type="series")]}},
        newest_limit=20,
    )
    types_seen = [r.type for r in rows]
    assert "recent_movie" not in types_seen  # no movie data at all
    assert "recent_series" in types_seen  # topped up from the series section


def test_build_home_rows_recent_row_emitted_solely_from_section_topup() -> None:
    """#263 T1: a provider with NO newest section still gets its recent
    row when its form section has items — the top-up fills the whole
    row (Netflix-style overlap with the type row is accepted)."""
    rows = build_home_rows(
        newest={},
        popular={},
        by_type={"movie": {"p1": [item("p1", "M")]}},
        newest_limit=20,
    )
    recent = next(r for r in rows if r.type == "recent_movie")
    assert [it.title for it in recent.items] == ["M"]


def test_build_home_rows_emits_popular_only_when_animeon_returns_data() -> None:
    """«Популярні зараз» appears iff the popular provider returned >=1 item."""
    with_data = build_home_rows(
        newest={},
        popular={"animeon": [item("animeon", "Naruto", media_type="anime")]},
        by_type={},
        newest_limit=20,
    )
    assert any(r.title == "Популярні зараз" and r.type == "popular" for r in with_data)

    empty = build_home_rows(
        newest={},
        popular={"animeon": []},  # animeon returned no items
        by_type={},
        newest_limit=20,
    )
    assert not any(r.title == "Популярні зараз" for r in empty)

    missing = build_home_rows(
        newest={},
        popular={},  # animeon failed entirely
        by_type={},
        newest_limit=20,
    )
    assert not any(r.title == "Популярні зараз" for r in missing)


def test_build_home_rows_emits_five_type_rows_when_all_have_data() -> None:
    rows = build_home_rows(
        newest={},
        popular={},
        by_type={
            "movie": {"p1": [item("p1", "M")]},
            "series": {"p1": [item("p1", "S", media_type="series")]},
            "anime": {"p1": [item("p1", "A", media_type="anime")]},
            "cartoon": {"p1": [item("p1", "C", media_type="cartoon")]},
            "dorama": {"p1": [item("p1", "D", media_type="dorama")]},
        },
        newest_limit=20,
    )
    types_seen = {r.type for r in rows}
    # spec #263: the form-split recent rows ALSO surface (topped up from
    # the same sections) — assert the five type rows are all present.
    assert {"movie", "series", "anime", "cartoon", "dorama"} <= types_seen


def test_build_home_rows_omits_empty_type_rows() -> None:
    """A type with no provider contributing items is not emitted."""
    rows = build_home_rows(
        newest={},
        popular={},
        by_type={
            "movie": {"p1": [item("p1", "M")]},
            "dorama": {},  # no providers contributed
        },
        newest_limit=20,
    )
    types_seen = {r.type for r in rows}
    assert "dorama" not in types_seen
    assert "movie" in types_seen


def test_build_home_rows_round_robin_dedups_within_type() -> None:
    rows = build_home_rows(
        newest={},
        popular={},
        by_type={
            "movie": {
                "p1": [item("p1", "Дюна", year=2021, n="1")],
                "p2": [
                    item("p2", "Дюна", year=2021, n="2"),
                    item("p2", "Смолфут", year=2018, n="3"),
                ],
            },
        },
        newest_limit=20,
    )
    movie_row = next(r for r in rows if r.type == "movie")
    # 1 deduped "Дюна" + 1 "Смолфут" = 2 rows
    assert len(movie_row.items) == 2
    dune = next(it for it in movie_row.items if it.title == "Дюна")
    assert set(dune.providers) == {"p1", "p2"}


def test_build_home_rows_newest_row_caps_at_limit() -> None:
    rows = build_home_rows(
        newest={"p1": [item("p1", f"T{i}", n=str(i)) for i in range(30)]},
        popular={},
        by_type={},
        newest_limit=20,
    )
    assert len(rows[0].items) == 20


def test_build_home_rows_orders_rows_recent_popular_then_types() -> None:
    """Row ordering: «Нещодавно додані: Фільми» → «Популярні зараз» →
    movie → series → anime → cartoon → dorama (spec #263).

    Only the rows that have provider contributions are emitted (issue
    #70 AC: empty rows omitted). The remaining types must keep the
    spec's mandated order — a ``dorama`` provider contributes BEFORE
    ``movie`` in the mapping, but ``movie`` must still come first in the
    output.
    """
    rows = build_home_rows(
        newest={"p1": [item("p1", "N")]},
        popular={"animeon": [item("animeon", "P", media_type="anime")]},
        by_type={
            "dorama": {"p1": [item("p1", "D", media_type="dorama")]},
            "anime": {"p1": [item("p1", "A", media_type="anime")]},
            "movie": {"p1": [item("p1", "M")]},
        },
        newest_limit=20,
    )
    assert [r.title for r in rows] == [
        "Нещодавно додані: Фільми",
        "Популярні зараз",
        "Фільми",
        "Аніме",
        "Дорами",
    ]


def test_build_home_rows_watched_series_get_own_row_position_3() -> None:
    """#270 T3: with ``watched_series`` the series-form NEWEST items the
    viewer watches form a dedicated «Нові серії» row at position 3 —
    right after the two form-split recent rows, before «Популярні
    зараз». Movies in the watched set are NOT eligible (it's a
    series-only row)."""
    rows = build_home_rows(
        newest={
            "p1": [
                item("p1", "Новий серіал", media_type="series"),
                item("p1", "Новий фільм"),
                item("p1", "Інший серіал", media_type="series", n="2"),
            ]
        },
        popular={"animeon": [item("animeon", "P", media_type="anime")]},
        by_type={},
        newest_limit=20,
        watched_series={_group_key("p1", "Новий серіал")},
    )
    assert [r.type for r in rows] == [
        "recent_movie",
        "recent_series",
        "new_episodes",
        "popular",
    ]
    new_ep = rows[2]
    assert new_ep.title == "Нові серії"
    assert [it.title for it in new_ep.items] == ["Новий серіал"]
    # The unwatched series stays in the recent row, not in this one.
    assert [it.title for it in rows[1].items] == ["Новий серіал", "Інший серіал"]


def test_build_home_rows_watched_series_row_omitted_when_empty() -> None:
    """#270 T3: «Нові серії» is omitted when the watched set matches no
    series-form newest item — and NEVER appears when ``watched_series``
    is None (the pure builder's default keeps legacy output unchanged)."""
    rows = build_home_rows(
        newest={"p1": [item("p1", "Серіал", media_type="series")]},
        popular={},
        by_type={},
        newest_limit=20,
        watched_series={_group_key("p1", "Невідомий")},
    )
    assert "new_episodes" not in [r.type for r in rows]

    rows_default = build_home_rows(
        newest={"p1": [item("p1", "Серіал", media_type="series")]},
        popular={},
        by_type={},
        newest_limit=20,
    )
    assert "new_episodes" not in [r.type for r in rows_default]


def test_build_home_rows_watched_series_ranked_by_listing_position() -> None:
    """#270 T3: within «Нові серії» the items are ranked by listing
    position (round-robin across providers preserves newest-first), and
    capped at ``newest_limit``."""
    watched = {
        _group_key("p1", f"Серіал {i}") for i in range(1, 6)
    } | {
        _group_key("p2", f"Серіал {i}") for i in range(6, 11)
    }
    rows = build_home_rows(
        newest={
            "p1": [
                item("p1", f"Серіал {i}", media_type="series", n=str(i)) for i in range(1, 6)
            ],
            "p2": [
                item("p2", f"Серіал {i}", media_type="series", n=str(i)) for i in range(6, 11)
            ],
        },
        popular={},
        by_type={},
        newest_limit=3,
        watched_series=watched,
    )
    new_ep = next(r for r in rows if r.type == "new_episodes")
    # Round-robin: p1 first item, p2 first item, p1 second item — then cap.
    assert [it.title for it in new_ep.items] == ["Серіал 1", "Серіал 6", "Серіал 2"]


def test_build_home_rows_watched_series_deduped_by_group_key() -> None:
    """#270 T3: the same series contributed by two providers appears once
    in «Нові серії» (aggregated by group key), not twice."""
    rows = build_home_rows(
        newest={
            "p1": [item("p1", "Спільний серіал", media_type="series")],
            "p2": [item("p2", "Спільний серіал", media_type="series")],
        },
        popular={},
        by_type={},
        newest_limit=20,
        watched_series={_group_key("p1", "Спільний серіал")},
    )
    new_ep = next(r for r in rows if r.type == "new_episodes")
    assert [it.title for it in new_ep.items] == ["Спільний серіал"]


def test_build_home_rows_recently_watched_row_position_4() -> None:
    """#272: the «Нещодавно переглянуто» row sits at position 4 — after
    the two form-split recent rows and «Нові серії», before «Популярні
    зараз» — listing the history groups in recency order."""
    watched = item("p1", "Переглянутий фільм")
    rows = build_home_rows(
        newest={"p1": [watched]},
        popular={"animeon": [item("animeon", "P", media_type="anime")]},
        by_type={"movie": {"p1": [watched]}},
        newest_limit=20,
        watched_series=set(),
        history_groups=[_item_key(watched)],
    )
    types = [r.type for r in rows]
    assert "recently_watched" in types
    assert types.index("recently_watched") == types.index("popular") - 1
    row = next(r for r in rows if r.type == "recently_watched")
    assert row.title == "Нещодавно переглянуто"
    assert [it.title for it in row.items] == ["Переглянутий фільм"]


def test_build_home_rows_recently_watched_order_and_cap() -> None:
    """#272: history groups keep their recency order and the row is
    capped at ``newest_limit``."""
    newest = {"p1": [item("p1", f"Фільм {i}", n=str(i)) for i in range(1, 6)]}
    rows = build_home_rows(
        newest=newest,
        popular={},
        by_type={},
        newest_limit=3,
        history_groups=[
            _item_key(item("p1", f"Фільм {i}", n=str(i))) for i in (5, 3, 1, 4, 2)
        ],
    )
    row = next(r for r in rows if r.type == "recently_watched")
    assert [it.title for it in row.items] == ["Фільм 5", "Фільм 3", "Фільм 1"]


def test_build_home_rows_recently_watched_resolves_from_type_section() -> None:
    """#272: a finished item is often NOT in the newest page anymore —
    the row resolves history groups against every collected listing
    (type sections included), so a finished movie still appears."""
    old = item("p1", "Старий фільм")
    rows = build_home_rows(
        newest={},
        popular={},
        by_type={"movie": {"p1": [old]}},
        newest_limit=20,
        history_groups=[_item_key(old)],
    )
    row = next(r for r in rows if r.type == "recently_watched")
    assert [it.title for it in row.items] == ["Старий фільм"]


def test_build_home_rows_recently_watched_omitted_when_empty() -> None:
    """#272: the row is omitted with no history groups — and NEVER
    appears when ``history_groups`` is None (the pure builder's default
    keeps every existing caller's output unchanged)."""
    rows = build_home_rows(
        newest={"p1": [item("p1", "Фільм")]},
        popular={},
        by_type={},
        newest_limit=20,
        history_groups=[_group_key("p1", "Невідомий")],
    )
    assert "recently_watched" not in [r.type for r in rows]

    rows_default = build_home_rows(
        newest={"p1": [item("p1", "Фільм")]},
        popular={},
        by_type={},
        newest_limit=20,
    )
    assert "recently_watched" not in [r.type for r in rows_default]


# ---------------------------------------------------------------------------
# /api/home route
# ---------------------------------------------------------------------------


def test_home_route_returns_200_with_expected_rows_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(
        _HomeStub(
            "stub-home",
            newest=[item("stub-home", "A"), item("stub-home", "B")],
            popular=[],
            sections=(
                # section shape only — the route ignores it for type rows
                # when there are no other providers; the test focuses on
                # the response envelope.
                __import__("cs_uk_api.models", fromlist=["Section"]).Section(
                    id="x", title="X", form="movie"
                ),
            ),
            newest_section="page",
        ),
        monkeypatch,
    )
    client = TestClient(app)
    r = client.get("/api/home")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body
    types_seen = [row["type"] for row in body["rows"]]
    assert "recent_movie" in types_seen
    items = next(
        row["items"] for row in body["rows"] if row["type"] == "recent_movie"
    )
    assert items[0]["group_key"].startswith("g2:")


def test_home_route_splits_newest_into_form_pure_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#264 AC1: /api/home starts with the two form-split rows — each
    carrying ONLY its own form's items, ≤20 each (wire-level)."""
    _register(
        _HomeStub(
            "stub-home",
            newest=[
                item("stub-home", "Фільм А", media_type="movie"),
                item("stub-home", "Серіал Б", media_type="series"),
                item("stub-home", "Фільм В", media_type="movie", n="2"),
            ],
            popular=[],
            sections=(),
            newest_section="page",
        ),
        monkeypatch,
    )
    client = TestClient(app)
    rows = client.get("/api/home").json()["rows"]
    assert [r["type"] for r in rows[:2]] == ["recent_movie", "recent_series"]
    movie_items = rows[0]["items"]
    series_items = rows[1]["items"]
    assert all(it["form"] == "movie" for it in movie_items)
    assert all(it["form"] == "series" for it in series_items)
    assert len(movie_items) <= 20
    assert len(series_items) <= 20
    assert [it["title"] for it in movie_items] == ["Фільм А", "Фільм В"]
    assert [it["title"] for it in series_items] == ["Серіал Б"]


def test_home_route_omits_empty_form_row_on_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#264 AC3: a series-only catalog emits only the series recent row —
    the empty movie row never ships (wire-level)."""
    _register(
        _HomeStub(
            "stub-home",
            newest=[item("stub-home", "Серіал Б", media_type="series")],
            popular=[],
            sections=(),
            newest_section="page",
        ),
        monkeypatch,
    )
    client = TestClient(app)
    rows = client.get("/api/home").json()["rows"]
    assert [r["type"] for r in rows] == ["recent_series"]


def test_home_route_omits_popular_row_when_animeon_returns_no_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: «Популярні зараз» present only when animeon provides it."""
    _register(
        _HomeStub(
            "stub-home",
            newest=[],
            popular=[],  # animeon contributed nothing
            sections=(),
            newest_section=None,
        ),
        monkeypatch,
    )
    client = TestClient(app)
    r = client.get("/api/home")
    assert r.status_code == 200
    titles = [row["title"] for row in r.json()["rows"]]
    assert "Популярні зараз" not in titles


def test_home_route_includes_popular_row_when_animeon_returns_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Популярні зараз» appears only when the popular provider (animeon)
    is present in the registry AND its ``popular`` browse returned ≥1 item."""
    # The route gates «Популярні зараз» on ``pid == "animeon"`` —
    # register an animeon-shaped stub.
    class _AnimeonStub(BaseProvider):
        id = "animeon"
        name = "AnimeON-stub"
        types = ("anime",)
        # The route gates «Популярні зараз» on the provider having a
        # ``popular`` section declared; declare it here so the gate
        # opens and ``browse("popular", ...)`` is called.
        sections = (
            __import__("cs_uk_api.models", fromlist=["Section"]).Section(
                id="popular", title="Популярні", styles=frozenset({"anime"})
            ),
        )

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            return []

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
            if section == "popular":
                return [item("animeon", "Naruto", media_type="anime")], False
            return [], False

    monkeypatch.setitem(PROVIDERS, "animeon", _AnimeonStub())
    client = TestClient(app)
    r = client.get("/api/home")
    assert r.status_code == 200
    popular_rows = [
        row for row in r.json()["rows"] if row["title"] == "Популярні зараз"
    ]
    assert len(popular_rows) == 1
    assert popular_rows[0]["items"][0]["title"] == "Naruto"


def test_home_route_second_call_hits_cache_doesnt_re_invoke_browse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 30-min cache absorbs the second /api/home call; providers are not re-invoked."""

    class _Counted(BaseProvider):
        id = "counted"
        name = "Counted"
        types = ("movie",)
        newest_section = "page"

        def __init__(self) -> None:
            self.calls = 0

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            return []

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
            self.calls += 1
            return [item("counted", "T")], False

    counted = _Counted()
    monkeypatch.setitem(PROVIDERS, "counted", counted)
    client = TestClient(app)
    r1 = client.get("/api/home")
    r2 = client.get("/api/home")
    assert r1.status_code == r2.status_code == 200
    assert counted.calls == 1  # cached after first call


def test_home_cache_uses_30_minute_ttl() -> None:
    """AC: 30-min cache. Pin the SETTINGS field — the spec target — and
    assert that the cache is constructed from it (set->get round-trip
    with a short TTL elsewhere covers the wiring)."""
    from cs_uk_api.config import SETTINGS

    # 30 minutes = 1800 seconds — the spec's documented value.
    assert SETTINGS.cache_home_s == 1800


def test_home_cache_set_with_short_ttl_expires() -> None:
    """Manual short-TTL set on the home cache expires promptly — confirms
    the cache primitive is wired and TTL is the only invalidation mechanism."""
    from cs_uk_api._catalog_state import home_cache

    home_cache.set("test:expire", {"v": 1}, ttl_s=0)
    # ttl_s=0 → expires_at == now → already past on the next tick.
    time.sleep(0.01)
    assert home_cache.get("test:expire") is None


def test_home_route_accumulates_multiple_sections_of_same_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (HIGH from code review): when one provider declares two
    sections of the same ``type`` (e.g. animeua's ``page`` + ``ona`` both
    type as ``anime``), both must contribute to that type row. Previously
    the route's accumulator overwrote, dropping the first section.

    Without the fix, the «Мультфільми» row would have only ``M-from-b``
    instead of all three items."""
    from cs_uk_api.models import Section

    class _Multi(BaseProvider):
        id = "multi-sec"
        name = "MultiSec"
        types = ("cartoon",)
        # Two cartoon sections, both type=cartoon. They would map to the
        # same row; the accumulator must extend, not overwrite.
        sections = (
            Section(id="a", title="A", styles=frozenset({"cartoon"})),
            Section(id="b", title="B", styles=frozenset({"cartoon"})),
        )

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            return []

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
            if section == "a":
                return [item("multi-sec", "M-from-a")], False
            if section == "b":
                return [item("multi-sec", "M-from-b")], False
            return [], False

    monkeypatch.setitem(PROVIDERS, "multi-sec", _Multi())
    client = TestClient(app)
    r = client.get("/api/home")
    assert r.status_code == 200
    cartoon_row = next(
        (row for row in r.json()["rows"] if row["type"] == "cartoon"), None
    )
    assert cartoon_row is not None, "cartoon row should be present"
    titles = [it["title"] for it in cartoon_row["items"]]
    assert titles == ["M-from-a", "M-from-b"]


def test_home_route_does_not_hang_on_a_provider_that_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (HIGH from code review): the /api/home fan-out must be
    bounded by an overall timeout so a single hung provider can't drag
    the request out. Per-provider upstream timeouts already cap
    individual ``browse`` calls, but a provider that produces no
    exception yet never returns is the edge this test pins."""
    import cs_uk_api.config as config_mod

    saved_settings = config_mod.SETTINGS
    # Arch T12: patch ONE binding (config.SETTINGS) — no positional
    # Settings reconstruction.
    patched = replace(saved_settings, upstream_timeout_s=10.0, search_total_timeout_s=0.1)
    config_mod.SETTINGS = patched
    try:
        import asyncio

        class _Hanger(BaseProvider):
            id = "hanger"
            name = "Hanger"
            types = ("movie",)
            newest_section = "page"

            async def search(self, q, http):  # type: ignore[no-untyped-def]
                return []

            async def content(self, external_id, http):  # type: ignore[no-untyped-def]
                raise NotImplementedError

            async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
                raise NotImplementedError

            async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
                # Hang for longer than the overall budget.
                await asyncio.sleep(5.0)
                return [], False

        monkeypatch.setitem(PROVIDERS, "hanger", _Hanger())
        client = TestClient(app)
        # If the budget fires, /api/home returns 200 (with no rows since
        # no provider produced data in time). Without the guard this
        # test would block for 5s+ (or fail on the test framework's
        # hard cap).
        import time

        t0 = time.monotonic()
        r = client.get("/api/home")
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert elapsed < 2.0, f"hung past budget: {elapsed:.2f}s"
    finally:
        config_mod.SETTINGS = saved_settings


# ---------------------------------------------------------------------------
# /api/content/{groupKey}
# ---------------------------------------------------------------------------


def test_content_by_group_key_returns_merged_item_with_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(
        _HomeStub(
            "stub-content",
            newest=[item("stub-content", "Дюна", year=2021)],
            popular=[],
            sections=(),
            newest_section="page",
        ),
        monkeypatch,
    )
    client = TestClient(app)
    home = client.get("/api/home").json()
    newest = next(row for row in home["rows"] if row["type"] == "recent_movie")
    gk = newest["items"][0]["group_key"]

    r = client.get(f"/api/content/{gk}")
    assert r.status_code == 200
    body = r.json()
    # GroupContentResponse is {item: HomeItem, providers: [str]} —
    # the spec asks for "the merged item with its source providers".
    assert body["item"]["group_key"] == gk
    assert body["item"]["title"] == "Дюна"
    assert body["item"]["year"] == 2021
    assert "stub-content" in body["providers"]


def test_content_by_group_key_returns_404_for_unknown_key() -> None:
    client = TestClient(app)
    r = client.get("/api/content/g2:0000000000000000")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


def test_content_by_provider_id_still_returns_content_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing /api/content/{provider:external} route is preserved
    by the g2:-prefix discriminator."""

    class _Prov(BaseProvider):
        id = "preserved"
        name = "Preserved"
        types = ("movie",)

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            return []

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            return ContentResponse(
                id=f"preserved:{external_id}",
                form="movie",
                title="Old route preserved",
                translations=[Translation(id="uk", label="UK")],
            )

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    monkeypatch.setitem(PROVIDERS, "preserved", _Prov())
    client = TestClient(app)
    r = client.get("/api/content/preserved:abc")
    assert r.status_code == 200
    assert r.json()["title"] == "Old route preserved"


def test_content_by_group_key_round_trips_merged_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two providers carrying the same title → /api/content/{groupKey}
    returns the merged item with BOTH providers listed."""

    class _P1(BaseProvider):
        id = "p1"
        name = "P1"
        types = ("movie",)
        newest_section = "page"

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            return []

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
            return [item("p1", "Дюна", year=2021)], False

    class _P2(BaseProvider):
        id = "p2"
        name = "P2"
        types = ("movie",)
        newest_section = "page"

        async def search(self, q, http):  # type: ignore[no-untyped-def]
            return []

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
            return [item("p2", "Дюна", year=2021)], False

    monkeypatch.setitem(PROVIDERS, "p1", _P1())
    monkeypatch.setitem(PROVIDERS, "p2", _P2())
    client = TestClient(app)
    home = client.get("/api/home").json()
    newest = next(row for row in home["rows"] if row["type"] == "recent_movie")
    assert len(newest["items"]) == 1
    gk = newest["items"][0]["group_key"]

    r = client.get(f"/api/content/{gk}")
    assert r.status_code == 200
    body = r.json()
    assert set(body["providers"]) == {"p1", "p2"}
    assert body["item"]["title"] == "Дюна"


# ---------------------------------------------------------------------------
# build_genre_rows — the genre rails (spec #263 T2)
# ---------------------------------------------------------------------------


def _home_item(key: str, title: str, genres: list[str]) -> HomeItem:
    return HomeItem(
        group_key=key,
        title=title,
        year=2021,
        form="movie",
        poster=None,
        genres=genres,
        providers=["p1"],
    )


def _profile(genres: list[str]) -> ItemProfile:
    return ItemProfile(
        genres=frozenset(genres),
        people=frozenset(),
        year=2021,
        form="movie",
        styles=frozenset(),
    )


def test_genre_rows_rank_by_profile_coverage() -> None:
    """#263 T2: the top-N genres by coverage across the home snapshot
    become rows; ties break lexicographically for determinism."""
    items = [
        _home_item("g2:a", "A", ["Драми"]),
        _home_item("g2:b", "B", ["Драми", "Екшн"]),
        _home_item("g2:c", "C", ["Екшн"]),
        _home_item("g2:d", "D", ["Комедії"]),
    ]
    profiles = {
        it.group_key: _profile(it.genres) for it in items
    }
    # min_items=2: «Комедії» (one member) is below the threshold, so the
    # ranked rails are Драми and Екшн — both with two members.
    rows = build_genre_rows(home_items=items, profiles=profiles, min_items=2)
    assert [r.type for r in rows] == ["genre:драми", "genre:екшн"]
    assert rows[0].title == "Драми"
    # members are the groups whose profile carries the genre, in
    # snapshot order (recency proxy — newest rows come first).
    assert [it.group_key for it in rows[0].items] == ["g2:a", "g2:b"]
    assert [it.group_key for it in rows[1].items] == ["g2:b", "g2:c"]


def test_genre_rows_skip_below_threshold() -> None:
    """#263 T2: genres with fewer than ``min_items`` members are skipped
    entirely — a one-item genre doesn't get a rail."""
    items = [
        _home_item("g2:a", "A", ["Драми"]),
        _home_item("g2:b", "B", ["Драми"]),
        _home_item("g2:c", "C", ["Комедії"]),  # only one member
    ]
    profiles = {it.group_key: _profile(it.genres) for it in items}
    rows = build_genre_rows(home_items=items, profiles=profiles, min_items=2)
    assert [r.type for r in rows] == ["genre:драми"]


def test_genre_rows_cap_members_per_row() -> None:
    items = [
        _home_item(f"g2:{i}", f"T{i}", ["Драми"]) for i in range(30)
    ]
    profiles = {it.group_key: _profile(it.genres) for it in items}
    rows = build_genre_rows(home_items=items, profiles=profiles, min_items=1, limit=5)
    assert len(rows) == 1
    assert len(rows[0].items) == 5


def test_genre_rows_omit_when_no_warm_profiles() -> None:
    """#263 T2: no profile signal → no rails (the cold-snapshot case)."""
    items = [_home_item("g2:a", "A", ["Драми"])]
    assert build_genre_rows(home_items=items, profiles={}) == []


def test_genre_rows_dedupe_overlap_within_row() -> None:
    """#263 T2: a group surfacing in several snapshot rows appears once
    per rail (deduped by group key, Netflix-style overlap)."""
    items = [
        _home_item("g2:a", "A", ["Драми"]),
        _home_item("g2:b", "B", ["Драми"]),
        _home_item("g2:b", "B", ["Драми"]),  # same group twice in the walk
    ]
    profiles = {it.group_key: _profile(it.genres) for it in items}
    rows = build_genre_rows(home_items=items, profiles=profiles, min_items=1)
    assert [it.group_key for it in rows[0].items] == ["g2:a", "g2:b"]


def test_genre_rows_ukrainian_label_prefers_card_casing() -> None:
    """#263 T2: the rail label is the card's original casing, not the
    lowercased profile key (content pages lowercase; cards keep the
    upstream's title case)."""
    items = [
        _home_item("g2:a", "A", ["Драми"]),
        _home_item("g2:b", "B", ["Драми"]),
    ]
    profiles = {"g2:a": _profile(["драми"]), "g2:b": _profile(["драми"])}
    rows = build_genre_rows(home_items=items, profiles=profiles, min_items=1)
    assert rows[0].title == "Драми"
    assert rows[0].type == "genre:драми"
