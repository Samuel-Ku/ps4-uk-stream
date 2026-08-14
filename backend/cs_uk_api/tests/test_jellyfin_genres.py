"""Jellyfin genre shelf + filter (ticket #213).

The library's Genres tab used to render nothing: ``GET /Genres``
returned ``{Items: [], TotalRecordCount: 0}`` because no provider exposed
genre metadata. Ticket #213 extracts genres into the catalog and
populates the shelf.

Pinned at the HTTP seam (the real Switchfin client's calls):

  - ``GET /Genres?includeItemTypes=Series&parentId=<view>`` returns the
    aggregated genre list of that view's cards, filtered by item type
    (the client opens the shelf per library with ``includeItemTypes``).
  - Genre items carry ``{Id, Name, ImageTags, ChildCount}`` — the
    ``jellyfin::Genres`` wire shape the client parses.
  - Tapping a genre opens ``MediaCollection(itemId, itemType,
    genresId)`` which requests ``/Items?parentId=<view>&genreIds=<id>``
    — the genre id round-trips as the filter.
  - A provider's ``SearchResult.genres`` (extracted from listing cards)
    flows into the home snapshot; unknown genres are simply absent.

Seeded via the same seam as the detail suite: one stub provider surfaces
cards with ``genres`` through ``/api/home``.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.catalog_state import clear_playback
from cs_uk_api.config import SETTINGS
from cs_uk_api.main import (
    _blocklist_cache,
    _content_cache,
    _home_cache,
    _home_sources_cache,
)
from cs_uk_api.models import ContentResponse, SearchResult
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError, model_b_axes

TOKEN = SETTINGS.jellyfin_token
USER = "fdc808859fc45eb8ac5aa6faddc12c72"


class _GenreStub(BaseProvider):
    """One home-capable provider whose cards carry genres."""

    id = "g1"
    name = "G1"
    types = ("movie", "series")
    newest_section = "page"

    def __init__(self, cards: list[SearchResult]) -> None:
        self._cards = cards
        self.sections: tuple[Any, ...] = ()

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def browse(
        self, section: str, page: int, http: Any
    ) -> tuple[list[SearchResult], bool]:
        if section == "page":
            return list(self._cards), False
        return [], False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        raise ProviderError("not_found", "no canned content")

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> Any:
        raise NotImplementedError


def _card(
    pid: str,
    id_: str,
    title: str,
    media_type: str,
    genres: list[str],
) -> SearchResult:
    mb_form, mb_styles = model_b_axes(cast(Any, media_type))
    return SearchResult(
        id=f"{pid}:{id_}",
        provider=pid,
        form=mb_form,
        styles=mb_styles,
        title=title,
        year=2021 if media_type == "movie" else 2023,
        poster=None,
        url=f"https://{pid}.example/{id_}",
        genres=genres,
    )


def _seed() -> _GenreStub:
    """Movie cards with genres + series cards with (overlapping) genres."""
    return _GenreStub(
        cards=[
            _card("g1", "dune", "Дюна", "movie", ["Фантастика", "Екшн"]),
            _card("g1", "arrival", "Прибуття", "movie", ["Фантастика", "Драма"]),
            _card("g1", "serial-a", "Серіал А", "series", ["Детектив", "Екшн"]),
            _card("g1", "serial-b", "Серіал Б", "series", ["Драма"]),
        ],
    )


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    clear_playback()
    for cache in (_home_cache, _home_sources_cache, _content_cache, _blocklist_cache):
        cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        clear_playback()
        for cache in (_home_cache, _home_sources_cache, _content_cache, _blocklist_cache):
            cache.clear()


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


def _auth(client: TestClient) -> None:
    r = client.get("/api/home")
    assert r.status_code == 200


def _view_id_by_name(client: TestClient, name: str) -> str:
    """The view id of a named home row, as /UserViews echoes.

    spec #263: the stub's ``newest_section = "page"`` feeds every card
    into the form-split «Нещодавно додані» rows, so the shelf is
    exercised per view.
    """
    body = _get(client, "/UserViews", userId=USER)
    for dto in body["Items"]:
        if dto["Name"] == name:
            return cast(str, dto["Id"])
    raise AssertionError(f"view {name!r} not found")


def _get(client: TestClient, path: str, **params: Any) -> dict[str, Any]:
    r = client.get(path, params=params or None, headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    return cast("dict[str, Any]", r.json())


# ---------------------------------------------------------------------------
# /Genres aggregation
# ---------------------------------------------------------------------------

def test_genres_aggregates_all_view_genres(client: TestClient) -> None:
    """Each form-split recent view aggregates its own cards' genres; the
    union across the two views covers every genre of the seed."""
    PROVIDERS["g1"] = _seed()
    _auth(client)
    movie_vid = _view_id_by_name(client, "Нещодавно додані: Фільми")
    series_vid = _view_id_by_name(client, "Нещодавно додані: Серіали")

    body = _get(client, "/Genres", parentId=movie_vid)
    movie_genres = {it["Name"]: it for it in body["Items"]}
    # Movie cards: Дюна (Фантастика, Екшн) + Прибуття (Фантастика, Драма).
    assert set(movie_genres) == {"Фантастика", "Екшн", "Драма"}
    assert body["TotalRecordCount"] == 3

    series_body = _get(client, "/Genres", parentId=series_vid)
    series_genres = {it["Name"] for it in series_body["Items"]}
    # Series cards: Серіал А (Детектив, Екшн) + Серіал Б (Драма).
    assert series_genres == {"Детектив", "Екшн", "Драма"}
    assert set(movie_genres) | series_genres == {
        "Фантастика", "Екшн", "Драма", "Детектив",
    }
    # The genre id round-trips as the filter the client sends back.
    assert all(it["Id"] == it["Name"] for it in body["Items"])


def test_genres_honors_include_item_types(client: TestClient) -> None:
    PROVIDERS["g1"] = _seed()
    _auth(client)
    movie_vid = _view_id_by_name(client, "Нещодавно додані: Фільми")
    series_vid = _view_id_by_name(client, "Нещодавно додані: Серіали")
    # The movie view has no series cards — the filter empties it.
    body = _get(client, "/Genres", parentId=movie_vid, includeItemTypes="Series")
    assert {it["Name"] for it in body["Items"]} == set()
    body_movie = _get(client, "/Genres", parentId=movie_vid, includeItemTypes="Movie")
    assert {it["Name"] for it in body_movie["Items"]} == {"Фантастика", "Екшн", "Драма"}
    body_series = _get(client, "/Genres", parentId=series_vid, includeItemTypes="Series")
    assert {it["Name"] for it in body_series["Items"]} == {"Детектив", "Екшн", "Драма"}


def test_genres_child_count_counts_cards(client: TestClient) -> None:
    PROVIDERS["g1"] = _seed()
    _auth(client)
    vid = _view_id_by_name(client, "Нещодавно додані: Фільми")
    body = _get(client, "/Genres", parentId=vid)
    by_name = {it["Name"]: it for it in body["Items"]}
    # Фантастика appears on both movie cards; Екшн on one movie (the
    # series carrying Екшн lives in the series view now).
    assert by_name["Фантастика"]["ChildCount"] == 2
    assert by_name["Екшн"]["ChildCount"] == 1


def test_genres_empty_view_returns_empty(client: TestClient) -> None:
    PROVIDERS["g1"] = _seed()
    _auth(client)
    # No parentId → no view context → empty shelf (tolerated by client).
    body = _get(client, "/Genres")
    assert body["Items"] == []
    assert body["TotalRecordCount"] == 0


# ---------------------------------------------------------------------------
# genreIds filtering on /Items
# ---------------------------------------------------------------------------

def test_items_filter_by_genre(client: TestClient) -> None:
    PROVIDERS["g1"] = _seed()
    _auth(client)
    movie_vid = _view_id_by_name(client, "Нещодавно додані: Фільми")
    series_vid = _view_id_by_name(client, "Нещодавно додані: Серіали")
    # Only the movie-view cards carrying Фантастика.
    body = _get(client, "/Items", parentId=movie_vid, userId=USER, genreIds="Фантастика")
    assert {it["Name"] for it in body["Items"]} == {"Дюна", "Прибуття"}
    # And the series view's Екшн card (Серіал А) — the per-view split.
    body = _get(client, "/Items", parentId=series_vid, userId=USER, genreIds="Екшн")
    assert {it["Name"] for it in body["Items"]} == {"Серіал А"}


def test_items_genre_filter_is_intersection_safe(client: TestClient) -> None:
    PROVIDERS["g1"] = _seed()
    _auth(client)
    vid = _view_id_by_name(client, "Нещодавно додані: Фільми")
    body = _get(client, "/Items", parentId=vid, userId=USER, genreIds="Фантастика")
    titles = {it["Name"] for it in body["Items"]}
    assert titles == {"Дюна", "Прибуття"}
