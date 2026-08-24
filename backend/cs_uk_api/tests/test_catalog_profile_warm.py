"""Internal profile-warming module (spec #309 T5) — taste layer.

Covers ``cs_uk_api._catalog_state.warm`` directly: the profile install
seam, the recommendation-row insertion, the background warm's
home-invalidation contract, and the health counts. The wire-level
behaviour is pinned in test_jellyfin_views / test_jellyfin_detail /
test_llm; these tests exercise the internal module's own contracts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from cs_uk_api import _catalog_state as catalog_state
from cs_uk_api._catalog_state.warm import (
    _warm_profiles,
    _with_recommendation_rows,
    recommendation_stats,
)
from cs_uk_api.models import (
    ContentResponse,
    HomeItem,
    HomeResponse,
    HomeRow,
    SearchResult,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider
from cs_uk_api.recommend import ItemProfile, profile_from_content


def _item(pid: str, external: str, title: str, year: int | None = 2021) -> SearchResult:
    mb_form, _mb_styles = ("movie", frozenset())
    return SearchResult(
        id=f"{pid}:{external}",
        provider=pid,
        form=mb_form,
        styles=frozenset(),
        title=title,
        year=year,
        url=f"https://{pid}.example/{external}",
    )


def _row(row_type: str, *titles: str) -> HomeRow:
    return HomeRow(
        title=row_type,
        type=row_type,
        items=[
            HomeItem(group_key=f"g2:{t}", title=t, form="movie", styles=frozenset())
            for t in titles
        ],
    )


@pytest.fixture(autouse=True)
def isolate() -> Iterator[None]:
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (
        catalog_state.home_cache,
        catalog_state.sources_cache,
        catalog_state.content_cache,
        catalog_state.gated_cache,
        catalog_state.row_deep_cache,
    ):
        cache.clear()
    catalog_state.install_profiles({})
    catalog_state.clear_playback()
    catalog_state.clear_user_state()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        catalog_state.install_profiles({})
        catalog_state.clear_playback()


def test_install_profiles_round_trip() -> None:
    assert catalog_state.get_profiles() == {}
    catalog_state.install_profiles(
        {
            "g2:a": ItemProfile(
                genres=frozenset({"екшн"}), people=frozenset(), year=2021, form="movie", styles=frozenset()
            )
        }
    )
    assert set(catalog_state.get_profiles()) == {"g2:a"}


def test_with_recommendation_rows_inserts_after_newest_and_appends_rails() -> None:
    """With warm profiles + taste signal, the recommendation rows insert
    after the newest row and the genre rails append at the end."""
    catalog_state.install_profiles(
        {
            "g2:Дюна": profile_from_content(
                ContentResponse(
                    id="p1:1", form="movie", title="Дюна",
                    translations=[Translation(id="uk", label="UK")],
                    genres=["фантастика"],
                )
            ),
            "g2:Війна": profile_from_content(
                ContentResponse(
                    id="p1:2", form="movie", title="Війна",
                    translations=[Translation(id="uk", label="UK")],
                    genres=["фантастика"],
                )
            ),
        }
    )
    # Sources so the episode reverse lookup resolves the watched group.
    catalog_state.sources_cache.set(
        catalog_state._SOURCES_KEY,
        {
            "g2:Дюна": {"p1": _item("p1", "1", "Дюна")},
            "g2:Війна": {"p1": _item("p1", "2", "Війна")},
        },
    )
    catalog_state.record_playback("g2:Війна", 1_000_000_000)
    catalog_state.record_search_query("фантастика")

    rows = [_row("newest", "Дюна", "Війна"), _row("movie", "Дюна", "Війна")]
    out = _with_recommendation_rows(rows)
    types = [r.type for r in out]
    # The personalized rows are inserted before the type rows.
    assert "recommended" in types
    assert types.index("recommended") == 1
    # The genre rail is appended at the end (type is ``genre:{slug}``).
    assert any(t.startswith("genre:") for t in types)
    assert types[-1].startswith("genre:")


def test_with_recommendation_rows_cold_profiles_ship_plain_rows() -> None:
    """No profiles → no personalized rows, no rails — the plain rows pass
    through untouched (the pre-#252 shape)."""
    rows = [_row("newest", "Дюна"), _row("movie", "Дюна")]
    out = _with_recommendation_rows(rows)
    assert [r.type for r in out] == ["newest", "movie"]


def test_warm_profiles_invalidates_home_when_new_profile_lands() -> None:
    """The background warm clears the home cache only when it added a
    profile — a steady-state warm (nothing new) never invalidates."""
    home = HomeResponse(rows=[_row("movie", "Дюна")])
    catalog_state.home_cache.set("home:v1", home)
    catalog_state.sources_cache.set(
        catalog_state._SOURCES_KEY,
        {"g2:Дюна": {"p1": _item("p1", "1", "Дюна")}},
    )

    class _ContentStub(BaseProvider):
        id = "p1"
        name = "P1"
        types = ("movie",)

        async def search(self, query, http):  # type: ignore[no-untyped-def]
            return []

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            return ContentResponse(
                id=f"p1:{external_id}", form="movie", title="Дюна",
                translations=[Translation(id="uk", label="UK")],
            )

        async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    PROVIDERS["p1"] = _ContentStub()
    asyncio.run(_warm_profiles(home))
    # A profile landed → the cached home is invalidated.
    assert catalog_state.get_profiles() != {}
    assert catalog_state.home_cache.get("home:v1") is None


def test_recommendation_stats_counts() -> None:
    catalog_state.install_profiles(
        {
            "g2:a": ItemProfile(
                genres=frozenset(), people=frozenset(), year=2021, form="movie", styles=frozenset()
            ),
            "g2:b": ItemProfile(
                genres=frozenset(), people=frozenset(), year=2022, form="series", styles=frozenset()
            ),
        }
    )
    catalog_state.record_search_query("дюна")
    catalog_state.record_playback("g2:a", 1_000)
    assert recommendation_stats() == {"profiles": 2, "queries": 1, "watched": 1}
