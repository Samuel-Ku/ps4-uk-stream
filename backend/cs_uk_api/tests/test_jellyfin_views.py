"""Jellyfin facade views + listing + poster (ticket #104).

Ticket #104's acceptance, pinned at the HTTP seam (the same seam the
handshake tests use):

  - ``GET /UserViews`` (and the server-style ``/Users/{id}/Views``)
    lists one virtual library per ``/api/home`` row, in home-row order,
    with human display names and a stable view ``Id`` that the client
    echoes back as ``parentId``.
  - ``GET /Items?parentId=<view>`` returns that row's cards as
    ``Movie``/``Series`` items carrying ``g1:`` ids, the right Type, and
    ``ImageTags.Primary`` present *iff* the card has a poster.
  - ``GET /Items/{id}/Images/Primary`` serves the poster bytes inline
    with 200 (matching the native ``/api/poster`` seam), ignoring
    ``maxWidth``; unknown item or poster-less item is a 404.
  - All three routes sit behind the same ``require_token`` gate (D4).

The fixture mirrors what a real client does: list views, pick a view by
its display name, open it, then ask for a card's image.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import catalog
from cs_uk_api.catalog_state import content_cache, home_cache, sources_cache
from cs_uk_api.config import SETTINGS
from cs_uk_api.models import ContentResponse, SearchResult, Section, Translation
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

#: The router *module* (the ``cs_uk_api.jellyfin`` package re-exports
#: ``router`` as the APIRouter, shadowing the submodule under that name).
jf_router = importlib.import_module("cs_uk_api.jellyfin.router")

TOKEN = SETTINGS.jellyfin_token
USER = "fdc808859fc45eb8ac5aa6faddc12c72"

#: Poster URLs carried by the seeded items. The poster proxy allowlist
#: does not matter — a 302 issuance never fetches the image.
_POSTER_MOVIE = "https://cdn.example.test/posters/dune.jpg"
_POSTER_SERIES = "https://cdn.example.test/posters/serial.jpg"


def _item(
    pid: str,
    title: str,
    media_type: str,
    year: int | None,
    *,
    n: str = "1",
    poster: str | None = None,
    genres: list[str] | None = None,
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
        poster=poster,
        url=f"https://{pid}.example/{n}",
        genres=genres or [],
    )


class _ViewsStub(BaseProvider):
    """A home-capable provider stub (newest + popular + type sections).

    Mirrors ``test_home._HomeStub`` but seeds per-section results via a
    ``by_section`` map so the Jellyfin tests can drive the real
    ``/api/home`` fan-out deterministically.
    """

    def __init__(
        self,
        pid: str,
        *,
        newest: list[SearchResult] | None = None,
        newest_section: str | None = None,
        popular: list[SearchResult] | None = None,
        sections: tuple[Section, ...] = (),
        by_section: dict[str, list[SearchResult]] | None = None,
    ) -> None:
        self.id = pid
        self.name = pid.title()
        self.types = ("movie", "series")
        self.newest_section = newest_section
        self.sections = sections
        self._newest = newest
        self._popular = popular
        self._by_section = by_section or {}

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def content(self, external_id: str, http: Any) -> Any:
        raise NotImplementedError

    async def stream(self, content_id: str, translation: str | None, http: Any) -> Any:
        raise NotImplementedError

    async def browse(self, section: str, page: int, http: Any) -> tuple[list[SearchResult], bool]:
        if self.newest_section is not None and section == self.newest_section:
            return list(self._newest or []), False
        if section == "popular":
            return list(self._popular or []), False
        results = self._by_section.get(section)
        if results is None:
            raise NotImplementedError(f"section {section} not stubbed")
        return list(results), False


def _seed() -> _ViewsStub:
    """The 6-row snapshot (spec #263): «Нещодавно додані: Фільми»,
    «Нещодавно додані: Серіали» (topped up from the series section),
    «Популярні зараз», Фільми, Серіали, Аніме — with a poster-bearing
    movie, a poster-less movie, and a poster-bearing series."""
    return _ViewsStub(
        "animeon",
        newest_section="page",
        newest=[
            _item("animeon", "Дюна", "movie", 2021, poster=_POSTER_MOVIE),
            # Distinct external id from Дюна's (``animeon:1``): the group
            # resolution map keys content by ``provider:external``, so a
            # colliding id would make two groups peek the same content
            # (#216 re-verification).
            _item("animeon", "Сокіл", "movie", 2019, n="3"),
        ],
        popular=[_item("animeon", "Сериалал серіал", "series", 2023, poster=_POSTER_SERIES)],
        sections=(
            # The home route gates «Популярні зараз» on
            # ``pid == "animeon" and has_section("popular")`` — declare
            # the section so the gate opens and the row appears.
            Section(id="popular", title="Популярні", styles=frozenset({"anime"})),
            Section(id="movie", title="Фільми", form="movie"),
            Section(id="series", title="Серіали", form="series"),
            Section(id="anime", title="Аніме", styles=frozenset({"anime"})),
        ),
        by_section={
            "movie": [
                _item("animeon", "Дюна", "movie", 2021, n="2", poster=_POSTER_MOVIE),
                _item("animeon", "Сокіл", "movie", 2019, n="4"),
            ],
            "series": [
                _item("animeon", "Сериалал серіал", "series", 2023, n="2", poster=_POSTER_SERIES)
            ],
            "anime": [_item("animeon", "Наруто", "anime", 2021, n="2", poster=_POSTER_SERIES)],
        },
    )


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS and the home/sources caches so no
    real upstream calls leak into assertions (pattern from
    test_home.py / test_lazy_group_content.py)."""
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    home_cache.clear()
    sources_cache.clear()
    content_cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        home_cache.clear()
        sources_cache.clear()
        content_cache.clear()


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


def _auth(client: TestClient) -> None:
    """Warm the shared home snapshot so the facade resolves real rows."""
    r = client.get("/api/home")
    assert r.status_code == 200


def _views(client: TestClient) -> list[dict[str, Any]]:
    r = client.get("/UserViews", params={"userId": USER}, headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    return cast("list[dict[str, Any]]", r.json()["Items"])


def _view_id(name: str, views: list[dict[str, Any]]) -> str:
    return cast(str, next(v["Id"] for v in views if v["Name"] == name))


def _items_page(
    client: TestClient, view_id: str, *, start_index: int, limit: int | None = None
) -> dict[str, Any]:
    params: dict[str, object] = {"parentId": view_id, "userId": USER, "startIndex": start_index}
    if limit is not None:
        params["limit"] = limit
    r = client.get("/Items", params=params, headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    return cast("dict[str, Any]", r.json())


def _items(client: TestClient, view_id: str) -> list[dict[str, Any]]:
    r = client.get(
        "/Items",
        params={"parentId": view_id, "userId": USER},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    return cast("list[dict[str, Any]]", r.json()["Items"])


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def test_user_views_lists_home_rows_in_order(client: TestClient) -> None:
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    views = _views(client)
    assert [v["Name"] for v in views] == [
        "Нещодавно додані: Фільми",
        "Нещодавно додані: Серіали",
        "Популярні зараз",
        "Фільми",
        "Серіали",
        "Аніме",
    ]
    assert all(v["Type"] == "CollectionFolder" for v in views)
    assert all(v["Id"] for v in views)


def test_user_views_conditional_row_dropped_when_empty(client: TestClient) -> None:
    """No provider contributes a 'popular' row → the view is absent."""
    PROVIDERS["p"] = _ViewsStub("p")
    _auth(client)
    views = _views(client)
    assert "Популярні зараз" not in [v["Name"] for v in views]
    assert "Фільми" not in [v["Name"] for v in views]


def test_user_views_requires_token(client: TestClient) -> None:
    assert client.get("/UserViews").status_code == 401
    assert client.get("/Users/x/Views").status_code == 401


def test_user_views_server_style_spelling_equivalent(client: TestClient) -> None:
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    sdk = client.get("/UserViews", headers={"X-Emby-Token": TOKEN}).json()
    server = client.get(f"/Users/{USER}/Views", headers={"X-Emby-Token": TOKEN}).json()
    assert sdk["Items"] == server["Items"]


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_items_listing_returns_row_cards(client: TestClient) -> None:
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    items = _items(client, movie_view)
    assert len(items) == 2
    dune = next(i for i in items if i["Name"] == "Дюна")
    assert dune["Type"] == "Movie"
    assert dune["ProductionYear"] == 2021
    assert dune["ParentId"] == movie_view
    assert dune["Id"].startswith("g2:")


def test_items_listing_honors_start_index_and_limit(client: TestClient) -> None:
    """The real client pages a listing with ``startIndex``/``limit`` and
    stops when a page comes back short (device-driving B11: the route
    ignored the slice, page 2 repeated page 1, and the app's infinite
    scroll re-requested page 2 forever). Page 2 must be a *different*
    slice, and ``TotalRecordCount`` must stay the full count so the app
    knows more pages exist.
    """
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    page1 = _items_page(client, movie_view, start_index=0, limit=1)
    page2 = _items_page(client, movie_view, start_index=1, limit=1)

    assert len(page1["Items"]) == 1
    assert len(page2["Items"]) == 1
    assert page1["Items"][0]["Id"] != page2["Items"][0]["Id"]
    assert page1["TotalRecordCount"] == 2
    assert page1["StartIndex"] == 0
    assert page2["StartIndex"] == 1

    # A page beyond the end is empty but keeps the full count (the client
    # reads the short page and stops scrolling).
    beyond = _items_page(client, movie_view, start_index=5, limit=18)
    assert beyond["Items"] == []
    assert beyond["TotalRecordCount"] == 2

    # The Switchfin client spells the listing under the user
    # (``/Users/{id}/Items``, apiUserLibrary) — the same slice must apply
    # there, or the app's page 2 still repeats page 1 (B11).
    prefixed = client.get(
        f"/Users/{USER}/Items",
        params={"parentId": movie_view, "startIndex": 1, "limit": 1},
        headers={"X-Emby-Token": TOKEN},
    )
    assert prefixed.status_code == 200
    body = prefixed.json()
    assert body["Items"] == page2["Items"]
    assert body["StartIndex"] == 1
    assert body["TotalRecordCount"] == 2


def test_items_listing_series_type_mapping(client: TestClient) -> None:
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    series_view = _view_id("Серіали", _views(client))
    items = _items(client, series_view)
    assert items and all(i["Type"] == "Series" for i in items)


def test_items_listing_card_type_reverified_against_resolved_content(
    client: TestClient,
) -> None:
    """#216: the card Type must match what the detail will show.

    The card parser (section/URL heuristic) is a cheap guess; the
    resolved content page is the truth. Once «Дюна»'s content is cached
    (group resolution reads the first-seen source — the
    «Нещодавно додані» card ``animeon:1`` → ``content:animeon:1``) and
    says series while its card says movie, the grid must re-verify to
    Series. An unresolved card («Сокіл») keeps the snapshot form.
    """
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    content_cache.set(
        "content:animeon:1",
        ContentResponse(
            id="animeon:1",
            title="Дюна",
            year=2021,
            form="series",
            translations=[Translation(id="uk", label="Українська")],
        ),
    )
    by_name = {i["Name"]: i for i in _items(client, movie_view)}
    assert by_name["Дюна"]["Type"] == "Series"  # re-verified vs the resolved content
    assert by_name["Сокіл"]["Type"] == "Movie"  # unresolved -> snapshot form


def test_items_listing_image_tags_only_with_poster(client: TestClient) -> None:
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    by_name = {i["Name"]: i for i in _items(client, movie_view)}
    assert set(by_name["Дюна"]["ImageTags"].keys()) == {"Primary"}
    assert by_name["Сокіл"]["ImageTags"] == {}


def test_items_listing_unknown_parent_is_empty(client: TestClient) -> None:
    r = client.get(
        "/Items",
        params={"parentId": "00000000000000000000000000000000"},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    assert r.json() == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}


def _genres_seed() -> _ViewsStub:
    """A snapshot where movies carry genre labels (#213).

    Same-genre pairs so the Similar shelf has something to return: Дюна
    and Війна share «Екшн», Дюна and Інтерстеллар share «Фантастика»,
    Сокіл is genre-less (never similar)."""
    return _ViewsStub(
        "animeon",
        newest_section="page",
        newest=[
            _item(
                "animeon",
                "Дюна",
                "movie",
                2021,
                n="1",
                genres=["Екшн", "Фантастика"],
                poster=_POSTER_MOVIE,
            ),
            _item("animeon", "Війна", "movie", 2019, n="2", genres=["Екшн"]),
        ],
        sections=(Section(id="movie", title="Фільми", form="movie"),),
        by_section={
            "movie": [
                _item(
                    "animeon",
                    "Дюна",
                    "movie",
                    2021,
                    n="1",
                    genres=["Екшн", "Фантастика"],
                    poster=_POSTER_MOVIE,
                ),
                _item("animeon", "Інтерстеллар", "movie", 2014, n="2", genres=["Фантастика"]),
                _item("animeon", "Сокіл", "movie", 2019, n="3"),
            ],
        },
    )


def test_items_similar_returns_same_genre_cards(client: TestClient) -> None:
    """#218: the Similar shelf serves same-genre cards from the snapshot.

    The app fires ``/Items/{gk}/Similar`` on every detail page; the
    shelf was deliberately empty. With genres (#213) the snapshot can
    answer it: cards sharing at least one genre, in the same Movie/
    Series + g2: + ImageTags shape as the view grid, the item itself
    excluded."""
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    by_name = {i["Name"]: i for i in _items(client, movie_view)}
    dune_gk = by_name["Дюна"]["Id"]

    r = client.get(f"/Items/{dune_gk}/Similar", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    names = {i["Name"] for i in body["Items"]}
    # Війна (Екшн) and Інтерстеллар (Фантастика) both share a genre
    assert names == {"Війна", "Інтерстеллар"}
    assert "Дюна" not in names  # the item itself is excluded
    assert all(i["Type"] == "Movie" for i in body["Items"])
    assert all(i["Id"].startswith("g2:") for i in body["Items"])
    assert body["TotalRecordCount"] == 2


def test_items_similar_respects_limit(client: TestClient) -> None:
    """#218: ``limit`` caps the shelf (the app asks for 12)."""
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    by_name = {i["Name"]: i for i in _items(client, movie_view)}
    dune_gk = by_name["Дюна"]["Id"]

    r = client.get(
        f"/Items/{dune_gk}/Similar",
        params={"limit": 1},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["Items"]) == 1
    assert body["TotalRecordCount"] == 1


def test_items_similar_genre_less_item_is_empty(client: TestClient) -> None:
    """#218: a genre-less item (no similarity metadata) stays an empty
    shelf — the same tolerant envelope, not an error."""
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    by_name = {i["Name"]: i for i in _items(client, movie_view)}
    falcon_gk = by_name["Сокіл"]["Id"]

    r = client.get(f"/Items/{falcon_gk}/Similar", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    assert r.json() == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}


def test_items_similar_unknown_item_is_empty(client: TestClient) -> None:
    """#218: an unknown g2: id (not in the snapshot) is an empty shelf."""
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    r = client.get(
        "/Items/g2:0000000000000000/Similar",
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    assert r.json() == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}


def _seed_similar_profiles(client: TestClient, profiles: dict[str, Any]) -> dict[str, str]:
    """Warm the profile store for the snapshot groups (spec #267 T1).

    Returns the ``title -> g2: group key`` map so tests can address the
    Similar route. The background profile warm is off in the suite, so
    this mirrors exactly what ``_warm_profiles`` would have stored.
    """
    import cs_uk_api.catalog_state as cs
    from cs_uk_api.recommend import ItemProfile

    home_cache.clear()
    _auth(client)
    home = cs.get_home()
    assert home is not None
    installed: dict[str, ItemProfile] = {}
    by_title = {}
    for row in home.rows:
        for it in row.items:
            by_title[it.title] = it.group_key
            p = profiles.get(it.title)
            if p is not None:
                installed[it.group_key] = ItemProfile(**p)
    catalog.install_profiles(installed)
    home_cache.clear()
    _auth(client)
    return by_title


def test_items_similar_ranks_by_profile_similarity(client: TestClient) -> None:
    """#268 T1: with warm content profiles the shelf is ranked by the
    weighted similarity score, NOT by listing order — the closest match
    first, the item itself excluded.

    Seed: Дюна is the query item. Війна shares a genre AND a person
    (score ≈ 1.0·cos(екшн) + 0.9·cos(people) + year window) — strictly
    closer than Інтерстеллар (only the shared «Фантастика» genre), so
    Війна must lead even though Інтерстеллар comes later in the
    listing. The genre-fallback shelf (pre-#267) had no notion of this
    ordering."""
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    by_title = _seed_similar_profiles(
        client,
        {
            "Дюна": {
                "genres": frozenset(["екшн", "фантастика"]),
                "people": frozenset(["денис вілленів"]),
                "year": 2021,
                "form": "movie",
                "styles": frozenset(),
            },
            "Війна": {
                "genres": frozenset(["екшн"]),
                "people": frozenset(["денис вілленів"]),
                "year": 2019,
                "form": "movie",
                "styles": frozenset(),
            },
            "Інтерстеллар": {
                "genres": frozenset(["фантастика"]),
                "people": frozenset(),
                "year": 2014,
                "form": "movie",
                "styles": frozenset(),
            },
        },
    )

    r = client.get(
        f"/Items/{by_title['Дюна']}/Similar",
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    names = [i["Name"] for i in r.json()["Items"]]
    # Війна shares a genre AND a person — strictly closer than
    # Інтерстеллар, so it leads despite the listing order.
    assert names == ["Війна", "Інтерстеллар"]
    assert "Дюна" not in names


def test_items_similar_genre_less_item_with_profile_gets_cards(
    client: TestClient,
) -> None:
    """#268 T1 AC3: a genre-less item WITH a warm content profile is no
    longer stuck with an empty shelf — the profile scorer (people/year)
    ranks it against the snapshot. The pre-#267 genre fallback could
    never serve Сокіл (no genres); the profile path can."""
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    by_title = _seed_similar_profiles(
        client,
        {
            "Дюна": {
                "genres": frozenset(["екшн", "фантастика"]),
                "people": frozenset(["денис вілленів"]),
                "year": 2021,
                "form": "movie",
                "styles": frozenset(),
            },
            # Сокіл is genre-less (the listing carries no genres) but
            # its content page shares the director — enough signal.
            "Сокіл": {
                "genres": frozenset(),
                "people": frozenset(["денис вілленів"]),
                "year": 2019,
                "form": "movie",
                "styles": frozenset(),
            },
        },
    )

    r = client.get(
        f"/Items/{by_title['Сокіл']}/Similar",
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    names = [i["Name"] for i in r.json()["Items"]]
    # Дюна shares the director; Війна has no profile (not seeded) and
    # the genre fallback is irrelevant here — only the scored Дюна.
    assert names == ["Дюна"]


def test_items_similar_cold_profiles_fall_back_to_genres(client: TestClient) -> None:
    """#268 T1: a cold profile store (no warm profiles yet) falls back
    to the pre-#267 genre-matching shelf — never an empty 200 during
    the warm-up window."""
    catalog.install_profiles({})
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    by_name = {i["Name"]: i for i in _items(client, movie_view)}
    dune_gk = by_name["Дюна"]["Id"]

    r = client.get(f"/Items/{dune_gk}/Similar", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    names = {i["Name"] for i in r.json()["Items"]}
    assert names == {"Війна", "Інтерстеллар"}


def _filmography_seed() -> _ViewsStub:
    """Movies + series whose profiles carry people (spec #272)."""
    return _ViewsStub(
        "animeon",
        newest_section="page",
        newest=[
            _item("animeon", "Фільм А", "movie", 2021, n="1", poster=_POSTER_MOVIE),
            _item("animeon", "Серіал Б", "series", 2022, n="2", poster=_POSTER_SERIES),
            _item("animeon", "Фільм В", "movie", 2019, n="3", poster=_POSTER_MOVIE),
        ],
        sections=(
            Section(id="movie", title="Фільми", form="movie"),
            Section(id="series", title="Серіали", form="series"),
        ),
        by_section={
            "movie": [_item("animeon", "Фільм А", "movie", 2021, n="1", poster=_POSTER_MOVIE)],
            "series": [_item("animeon", "Серіал Б", "series", 2022, n="2", poster=_POSTER_SERIES)],
        },
    )


def test_items_person_ids_returns_filmography(client: TestClient) -> None:
    """#272: the person page's ``PersonIds`` query returns every
    home-snapshot group whose warm profile carries the person — the
    filmography, as Movie/Series cards."""
    from cs_uk_api.recommend import ItemProfile

    PROVIDERS["animeon"] = _filmography_seed()
    _auth(client)
    home = client.get("/api/home").json()
    gk = {i["title"]: i["group_key"] for r in home["rows"] for i in r["items"]}
    catalog.install_profiles(
        {
            gk["Фільм А"]: ItemProfile(
                genres=frozenset(), people=frozenset(["денис вілленів"]), year=2021, form="movie", styles=frozenset()
            ),
            gk["Серіал Б"]: ItemProfile(
                genres=frozenset(), people=frozenset(["денис вілленів"]), year=2022, form="series", styles=frozenset()
            ),
        }
    )
    try:
        r = client.get(
            "/Items",
            params={"PersonIds": "animeon:actor:Денис Вілленів"},
            headers={"X-Emby-Token": TOKEN},
        )
        assert r.status_code == 200
        names = {i["Name"] for i in r.json()["Items"]}
        assert names == {"Фільм А", "Серіал Б"}
        assert all(i["Type"] in ("Movie", "Series") for i in r.json()["Items"])
    finally:
        catalog.install_profiles({})


def test_items_person_ids_respects_include_item_types(client: TestClient) -> None:
    """#272: the client's person page splits films and series via
    ``IncludeItemTypes`` — each section returns only its form."""
    from cs_uk_api.recommend import ItemProfile

    PROVIDERS["animeon"] = _filmography_seed()
    _auth(client)
    home = client.get("/api/home").json()
    gk = {i["title"]: i["group_key"] for r in home["rows"] for i in r["items"]}
    catalog.install_profiles(
        {
            gk["Фільм А"]: ItemProfile(
                genres=frozenset(), people=frozenset(["денис вілленів"]), year=2021, form="movie", styles=frozenset()
            ),
            gk["Серіал Б"]: ItemProfile(
                genres=frozenset(), people=frozenset(["денис вілленів"]), year=2022, form="series", styles=frozenset()
            ),
        }
    )
    try:
        movies = client.get(
            "/Items",
            params={"PersonIds": "animeon:actor:Денис Вілленів", "includeItemTypes": "Movie"},
            headers={"X-Emby-Token": TOKEN},
        ).json()
        assert [i["Name"] for i in movies["Items"]] == ["Фільм А"]

        series = client.get(
            "/Items",
            params={"PersonIds": "animeon:actor:Денис Вілленів", "includeItemTypes": "Series"},
            headers={"X-Emby-Token": TOKEN},
        ).json()
        assert [i["Name"] for i in series["Items"]] == ["Серіал Б"]
    finally:
        catalog.install_profiles({})


def test_items_person_ids_unknown_or_cold_is_empty(client: TestClient) -> None:
    """#272: an unknown person OR a cold profile store is the tolerant
    empty result, never an error."""
    PROVIDERS["animeon"] = _filmography_seed()
    _auth(client)
    catalog.install_profiles({})

    # Cold store (no profiles at all).
    cold = client.get(
        "/Items",
        params={"PersonIds": "animeon:actor:Денис Вілленів"},
        headers={"X-Emby-Token": TOKEN},
    )
    assert cold.status_code == 200
    assert cold.json() == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}

    # Unknown person with warm profiles for other people.
    from cs_uk_api.recommend import ItemProfile

    home = client.get("/api/home").json()
    gk = {i["title"]: i["group_key"] for r in home["rows"] for i in r["items"]}
    catalog.install_profiles(
        {
            gk["Фільм А"]: ItemProfile(
                genres=frozenset(), people=frozenset(["хтось інший"]), year=2021, form="movie", styles=frozenset()
            ),
        }
    )
    try:
        unknown = client.get(
            "/Items",
            params={"PersonIds": "animeon:actor:Ніхто"},
            headers={"X-Emby-Token": TOKEN},
        )
        assert unknown.status_code == 200
        assert unknown.json() == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
    finally:
        catalog.install_profiles({})

def test_detail_genres_fall_back_to_snapshot_card(client: TestClient) -> None:
    """#219: the detail DTO's Genres fall back to the snapshot card's
    genres when the resolved content page carries none — the genre row
    must render wherever the card parser (#213) found them.
    """
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    by_name = {i["Name"]: i for i in _items(client, movie_view)}
    dune_gk = by_name["Дюна"]["Id"]
    # the resolved content carries NO genres (ufdub-style: the card lists
    # them, the detail page does not) — the DTO must fall back to the card
    content_cache.set(
        "content:animeon:1",
        ContentResponse(
            id="animeon:1",
            title="Дюна",
            year=2021,
            form="movie",
            translations=[Translation(id="uk", label="Українська")],
        ),
    )

    r = client.get(
        f"/Users/{USER}/Items/{dune_gk}",
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["Genres"]) == {"Екшн", "Фантастика"}


def test_detail_production_year_falls_back_to_snapshot_card(client: TestClient) -> None:
    """#220: the detail DTO's ProductionYear falls back to the snapshot
    card's year when the resolved content page carries none — the year
    badge must render wherever either source exposes it."""
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    by_name = {i["Name"]: i for i in _items(client, movie_view)}
    dune_gk = by_name["Дюна"]["Id"]
    # the resolved content carries NO year (a provider whose content
    # page lacks the meta block) — the DTO must fall back to the card's
    # 2021 (the seed's card year)
    content_cache.set(
        "content:animeon:1",
        ContentResponse(
            id="animeon:1",
            title="Дюна",
            year=None,
            form="movie",
            translations=[Translation(id="uk", label="Українська")],
        ),
    )

    r = client.get(
        f"/Users/{USER}/Items/{dune_gk}",
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ProductionYear"] == 2021


def test_detail_production_year_content_wins_when_present(client: TestClient) -> None:
    """#220: when the content page DOES carry a year (ufdub's ``Рік:``
    block), it wins over the snapshot card's — the content page is the
    truth, the card the cheap guess."""
    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    by_name = {i["Name"]: i for i in _items(client, movie_view)}
    dune_gk = by_name["Дюна"]["Id"]
    content_cache.set(
        "content:animeon:1",
        ContentResponse(
            id="animeon:1",
            title="Дюна",
            year=1984,
            form="movie",
            translations=[Translation(id="uk", label="Українська")],
        ),
    )

    r = client.get(
        f"/Users/{USER}/Items/{dune_gk}",
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ProductionYear"] == 1984

    # a genre-less card stays genre-less on the detail too
    falcon_gk = by_name["Сокіл"]["Id"]
    content_cache.set(
        "content:animeon:3",
        ContentResponse(
            id="animeon:3",
            title="Сокіл",
            year=2019,
            form="movie",
            translations=[Translation(id="uk", label="Українська")],
        ),
    )
    r = client.get(
        f"/Users/{USER}/Items/{falcon_gk}",
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    assert r.json()["Genres"] == []


def test_items_listing_requires_token(client: TestClient) -> None:
    assert client.get("/Items").status_code == 401


# ---------------------------------------------------------------------------
# Poster
# ---------------------------------------------------------------------------

#: Poster URLs carried by the seeded items. Inline serving fetches them
#: through ``fetch_poster_bytes``; the tests stub that call so no
#: upstream request leaves the test process.
_POSTER_MOVIE = "https://cdn.example.test/posters/dune.jpg"
_POSTER_SERIES = "https://cdn.example.test/posters/serial.jpg"

#: Body/type handed back by the ``fetch_poster_bytes`` stub.
_POSTER_RESP = (b"\xff\xd8\xff\xe0jpegbytes", "image/jpeg")


def _stub_poster_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``fetch_poster_bytes`` at a canned inline response."""

    async def _fake(url: str, client: Any) -> tuple[bytes, str]:
        return _POSTER_RESP

    monkeypatch.setattr(jf_router, "fetch_poster_bytes", _fake)


def test_poster_primary_serves_poster_inline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_poster_fetch(monkeypatch)
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    dune_id = next(i["Id"] for i in _items(client, movie_view) if i["Name"] == "Дюна")

    r = client.get(
        f"/Items/{dune_id}/Images/Primary",
        headers={"X-Emby-Token": TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.content == _POSTER_RESP[0]
    assert r.headers["content-type"] == _POSTER_RESP[1]


def test_poster_primary_ignores_max_width(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_poster_fetch(monkeypatch)
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    series_view = _view_id("Серіали", _views(client))
    series_id = _items(client, series_view)[0]["Id"]

    plain = client.get(
        f"/Items/{series_id}/Images/Primary",
        headers={"X-Emby-Token": TOKEN},
        follow_redirects=False,
    )
    sized = client.get(
        f"/Items/{series_id}/Images/Primary",
        params={"maxWidth": 400},
        headers={"X-Emby-Token": TOKEN},
        follow_redirects=False,
    )
    assert plain.status_code == sized.status_code == 200
    assert plain.content == sized.content == _POSTER_RESP[0]


def test_poster_primary_transcodes_webp_when_requested(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switchfin always asks ``format=Webp``; a non-WebP original must
    come back as ``image/webp`` (a JPEG answer is undecodable on the
    client, which retries the poster hundreds of times)."""
    import io

    from PIL import Image

    jpeg_bytes: bytes
    with io.BytesIO() as out:
        Image.new("RGB", (64, 40), (200, 30, 30)).save(out, format="JPEG")
        jpeg_bytes = out.getvalue()

    async def _fake(url: str, client: Any) -> tuple[bytes, str]:
        return jpeg_bytes, "image/jpeg"

    monkeypatch.setattr(jf_router, "fetch_poster_bytes", _fake)
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    dune_id = next(i["Id"] for i in _items(client, movie_view) if i["Name"] == "Дюна")

    r = client.get(
        f"/Items/{dune_id}/Images/Primary",
        params={"format": "Webp", "maxWidth": 325},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.content.startswith(b"RIFF")
    assert r.content != jpeg_bytes


def test_poster_primary_missing_poster_is_404(client: TestClient) -> None:
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    falcon_id = next(i["Id"] for i in _items(client, movie_view) if i["Name"] == "Сокіл")
    assert (
        client.get(
            f"/Items/{falcon_id}/Images/Primary", headers={"X-Emby-Token": TOKEN}
        ).status_code
        == 404
    )


def test_poster_primary_unknown_item_is_404(client: TestClient) -> None:
    assert (
        client.get("/Items/g1:unknown/Images/Primary", headers={"X-Emby-Token": TOKEN}).status_code
        == 404
    )


def test_poster_primary_query_is_single_encoded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poster URL that already carries ``%`` escapes must reach
    ``fetch_poster_bytes`` without a second decode.

    There is no ``?u=`` query hop anymore — the bytes are served inline
    — so the percent-escaped poster must pass through verbatim, exactly
    as it was stored on the item.
    """
    escaped = _POSTER_MOVIE.replace("dune", "d%C3%BCne")  # %-escaped path segment
    captured: list[str | None] = []

    async def _capture(url: str, client: Any) -> tuple[bytes, str]:
        captured.append(url)
        return _POSTER_RESP

    monkeypatch.setattr(jf_router, "fetch_poster_bytes", _capture)

    provider = _ViewsStub(
        "animeon",
        newest_section="page",
        newest=[_item("animeon", "Дюна", "movie", 2021, poster=escaped)],
        sections=(
            Section(id="popular", title="Популярні", styles=frozenset({"anime"})),
            Section(id="movie", title="Фільми", form="movie"),
        ),
        by_section={
            "movie": [_item("animeon", "Дюна", "movie", 2021, n="2", poster=escaped)],
            "popular": [_item("animeon", "Сериалал серіал", "series", 2023, poster=_POSTER_SERIES)],
        },
    )
    PROVIDERS["animeon"] = provider
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    dune_id = next(i["Id"] for i in _items(client, movie_view) if i["Name"] == "Дюна")

    r = client.get(
        f"/Items/{dune_id}/Images/Primary",
        headers={"X-Emby-Token": TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.content == _POSTER_RESP[0]
    assert captured == [escaped]


def test_poster_primary_is_public_without_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Image endpoints stay open: Jellyfin clients load images without
    the ``X-Emby-Token`` header (media is addressable by URL), so the
    route must serve art to an anonymous request."""
    _stub_poster_fetch(monkeypatch)
    PROVIDERS["animeon"] = _seed()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))
    dune_id = next(i["Id"] for i in _items(client, movie_view) if i["Name"] == "Дюна")
    r = client.get(f"/Items/{dune_id}/Images/Primary", follow_redirects=False)
    assert r.status_code == 200
    assert r.content == _POSTER_RESP[0]


# ---------------------------------------------------------------------------
# spec #263: the genre rails are views too — their grids must list cards
# ---------------------------------------------------------------------------


def _genre_stub() -> _ViewsStub:
    """One provider whose movie cards all carry the «Пригоди» genre."""
    return _ViewsStub(
        "animeon",
        newest_section="page",
        newest=[
            _item("animeon", "Фільм А", "movie", 2021, n="1", genres=["Пригоди"]),
            _item("animeon", "Фільм Б", "movie", 2022, n="2", genres=["Пригоди"]),
        ],
        sections=(Section(id="movie", title="Фільми", form="movie"),),
        by_section={
            "movie": [
                _item("animeon", "Фільм А", "movie", 2021, n="1", genres=["Пригоди"]),
                _item("animeon", "Фільм Б", "movie", 2022, n="2", genres=["Пригоди"]),
            ],
        },
    )


def _seed_genre_profiles(client: TestClient) -> None:
    """Warm content profiles for the snapshot groups so the genre rails
    appear (spec #263: rails build from the profile store), then rebuild
    the snapshot with them."""
    import cs_uk_api.catalog_state as cs
    from cs_uk_api.recommend import ItemProfile

    home_cache.clear()
    _auth(client)
    home = cs.get_home()
    assert home is not None
    installed: dict[str, ItemProfile] = {}
    for row in home.rows:
        for it in row.items:
            installed[it.group_key] = ItemProfile(
                genres=frozenset(["пригоди"]),
                people=frozenset(),
                year=it.year,
                form=it.form,
                styles=frozenset(it.styles or ()),
            )
    catalog.install_profiles(installed)
    home_cache.clear()
    _auth(client)


def test_new_episodes_row_is_a_view(client: TestClient) -> None:
    """#270 AC4: a watched series in the recent listings forms the
    «Нові серії» row at position 3, and its cards open through the
    existing view/items route — same envelope as any other row.

    The playback store maps the episode wire id (``animeon:1:s1e1``) to
    the merged group via the sources map, so the row appears even
    though the card itself is a series, not an episode."""

    from cs_uk_api.catalog_state import clear_playback, record_playback

    PROVIDERS["animeon"] = _ViewsStub(
        "animeon",
        newest_section="page",
        newest=[
            _item("animeon", "Серіал А", "series", 2023, n="1", poster=_POSTER_SERIES),
            _item("animeon", "Серіал Б", "series", 2022, n="2", poster=_POSTER_SERIES),
            _item("animeon", "Фільм В", "movie", 2021, n="3", poster=_POSTER_MOVIE),
        ],
        sections=(Section(id="series", title="Серіали", form="series"),),
        by_section={
            "series": [
                _item("animeon", "Серіал А", "series", 2023, n="1", poster=_POSTER_SERIES),
                _item("animeon", "Серіал Б", "series", 2022, n="2", poster=_POSTER_SERIES),
            ]
        },
    )
    _auth(client)
    clear_playback()
    try:
        # The viewer watches Серіал А — its episode wire id resolves to
        # the merged group ``animeon:1`` through the sources map.
        record_playback("animeon:1:s1e1", 1_000)
        home_cache.clear()

        home = client.get("/api/home").json()
        row_types = [r["type"] for r in home["rows"]]
        assert "new_episodes" in row_types
        assert row_types.index("new_episodes") == 2  # position 3
        new_ep = next(r for r in home["rows"] if r["type"] == "new_episodes")
        assert new_ep["title"] == "Нові серії"
        assert [i["title"] for i in new_ep["items"]] == ["Серіал А"]

        # The row is a view like any other: open its grid via /Items.
        views = _views(client)
        view = next(v for v in views if v["Name"] == "Нові серії")
        items = _items(client, view["Id"])
        assert {i["Name"] for i in items} == {"Серіал А"}
        assert all(i["Type"] == "Series" for i in items)
    finally:
        clear_playback()


def test_recently_watched_row_is_a_view(client: TestClient) -> None:
    """#272 AC: a recorded playback item forms the «Нещодавно
    переглянуто» row at position 4 and its card opens through the
    existing view/items route."""

    from cs_uk_api.catalog_state import clear_playback, record_playback

    PROVIDERS["animeon"] = _ViewsStub(
        "animeon",
        newest_section="page",
        newest=[
            _item("animeon", "Фільм А", "movie", 2021, n="1", poster=_POSTER_MOVIE),
            _item("animeon", "Фільм Б", "movie", 2022, n="2", poster=_POSTER_MOVIE),
        ],
        sections=(Section(id="movie", title="Фільми", form="movie"),),
        by_section={
            "movie": [
                _item("animeon", "Фільм А", "movie", 2021, n="1", poster=_POSTER_MOVIE),
                _item("animeon", "Фільм Б", "movie", 2022, n="2", poster=_POSTER_MOVIE),
            ]
        },
    )
    _auth(client)
    clear_playback()
    try:
        # The viewer watches Фільм Б (its g2: group key is the wire id).
        home = client.get("/api/home").json()
        gk = {i["title"]: i["group_key"] for r in home["rows"] for i in r["items"]}
        record_playback(gk["Фільм Б"], 1_000)
        home_cache.clear()

        home = client.get("/api/home").json()
        row_types = [r["type"] for r in home["rows"]]
        assert "recently_watched" in row_types
        # Position 4 in the decided order: after the form-split recent
        # rows (and «Нові серії»/«Популярні зараз» when present), before
        # the type rows — this movie-only seed has no series/popular, so
        # it sits right after the movie recent row.
        assert row_types.index("recently_watched") == row_types.index("recent_movie") + 1
        rw = next(r for r in home["rows"] if r["type"] == "recently_watched")
        assert rw["title"] == "Нещодавно переглянуто"
        assert [i["title"] for i in rw["items"]] == ["Фільм Б"]

        views = _views(client)
        view = next(v for v in views if v["Name"] == "Нещодавно переглянуто")
        items = _items(client, view["Id"])
        assert {i["Name"] for i in items} == {"Фільм Б"}
    finally:
        clear_playback()


def test_recently_watched_row_includes_finished(client: TestClient) -> None:
    """#272: a FINISHED item (>=95% of runtime, gone from the resume
    shelf) still appears in «Нещодавно переглянуто» — the row is the
    browsable history, not the continue-watching shelf."""

    from cs_uk_api.catalog_state import clear_playback, record_playback

    PROVIDERS["animeon"] = _ViewsStub(
        "animeon",
        newest_section="page",
        newest=[
            _item("animeon", "Фільм А", "movie", 2021, n="1", poster=_POSTER_MOVIE),
        ],
        sections=(Section(id="movie", title="Фільми", form="movie"),),
        by_section={
            "movie": [
                _item("animeon", "Фільм А", "movie", 2021, n="1", poster=_POSTER_MOVIE),
            ]
        },
    )
    _auth(client)
    clear_playback()
    try:
        home = client.get("/api/home").json()
        gk = {i["title"]: i["group_key"] for r in home["rows"] for i in r["items"]}
        # Finished: position at >=95% of the runtime.
        record_playback(gk["Фільм А"], 950, runtime_ticks=1000)
        home_cache.clear()

        # Gone from Resume, present in «Нещодавно переглянуто».
        resume = client.get(
            "/Users/user1/Items/Resume", headers={"X-Emby-Token": TOKEN}
        ).json()
        assert [i["Name"] for i in resume["Items"]] == []
        home = client.get("/api/home").json()
        rw = next(r for r in home["rows"] if r["type"] == "recently_watched")
        assert [i["title"] for i in rw["items"]] == ["Фільм А"]
    finally:
        clear_playback()


def test_genre_view_id_lists_its_cards(client: TestClient) -> None:
    """#263 T2 / #265 AC1+AC5: the genre rails close /api/home (after
    the type rows), are labeled in Ukrainian, and each is a view —
    opening its grid returns the rail's cards, not an empty library."""
    PROVIDERS["animeon"] = _genre_stub()
    _seed_genre_profiles(client)
    home = client.get("/api/home").json()
    assert home["rows"][-1]["type"] == "genre:пригоди"
    assert home["rows"][-1]["title"] == "Пригоди"
    assert len([r for r in home["rows"] if r["type"].startswith("genre:")]) <= 6

    views = _views(client)
    genre_view = next(v for v in views if v["Name"] == "Пригоди")
    items = _items(client, genre_view["Id"])
    assert {i["Name"] for i in items} == {"Фільм А", "Фільм Б"}
    assert all(i["ParentId"] == genre_view["Id"] for i in items)


def test_genre_view_id_survives_home_cache_invalidation(client: TestClient) -> None:
    """#263 T2 regression: the background profile-warm clears the home
    cache mid-session. A genre view id the client JUST listed must still
    resolve and list its cards on the next /Items — the route resolves
    snapshot view types against the freshly loaded home, not a stale
    ``get_home()`` that the invalidation just emptied."""
    PROVIDERS["animeon"] = _genre_stub()
    _seed_genre_profiles(client)
    genre_view = next(v for v in _views(client) if v["Name"] == "Пригоди")

    # Simulate the profile-warm invalidation: cache cleared, snapshot gone.
    home_cache.clear()
    sources_cache.clear()
    items = _items(client, genre_view["Id"])
    assert {i["Name"] for i in items} == {"Фільм А", "Фільм Б"}


# ---------------------------------------------------------------------------
# LLM idea rows (#293)
# ---------------------------------------------------------------------------


def test_llm_idea_kinds_registered_in_facade_view_vocabulary() -> None:
    """#293 AC5: the fixed idea-row slots join the facade view
    vocabulary — a deterministic 32-hex view id and a CollectionType
    (episodic-ish default), stable across profile refreshes."""
    from cs_uk_api.recommend import LLM_IDEA_ROW_TYPES

    for kind in LLM_IDEA_ROW_TYPES:
        assert jf_router._COLLECTION_TYPE_BY_ROW[kind] == "tvshows"
        vid = jf_router._view_id_for(kind)
        assert len(vid) == 32 and vid == jf_router._view_id_for(kind)
    assert jf_router._view_id_for("llm_idea_1") != jf_router._view_id_for("llm_idea_2")


def test_llm_idea_row_is_a_view(client: TestClient) -> None:
    """#293 AC3+AC5: with an active profile the idea row appears on home
    with only genre-matching cards and opens through the existing view
    mechanism — no client changes (spec #290 user stories 5–6, 13)."""
    from cs_uk_api.llm import RowIdea, TasteProfile, set_active_profile

    PROVIDERS["animeon"] = _genres_seed()
    _auth(client)
    _seed_similar_profiles(
        client,
        {
            "Дюна": {
                "genres": frozenset(["екшн", "фантастика"]),
                "people": frozenset(),
                "year": 2021,
                "form": "movie",
                "styles": frozenset(),
            },
            "Війна": {
                "genres": frozenset(["екшн"]),
                "people": frozenset(),
                "year": 2019,
                "form": "movie",
                "styles": frozenset(),
            },
            "Інтерстеллар": {
                "genres": frozenset(["фантастика"]),
                "people": frozenset(),
                "year": 2014,
                "form": "movie",
                "styles": frozenset(),
            },
        },
    )
    try:
        set_active_profile(
            TasteProfile(
                row_ideas=(
                    RowIdea(
                        title="Космічні епопеї для тебе",
                        genres=("фантастика",),
                        max=5,
                    ),
                )
            )
        )
        home_cache.clear()

        home = client.get("/api/home").json()
        idea = next(r for r in home["rows"] if r["type"] == "llm_idea_1")
        assert idea["title"] == "Космічні епопеї для тебе"
        # ONLY genre-matching cards: Дюна + Інтерстеллар share
        # «Фантастика»; Війна (only «Екшн») must not appear.
        assert {i["title"] for i in idea["items"]} == {"Дюна", "Інтерстеллар"}

        views = _views(client)
        view = next(v for v in views if v["Name"] == "Космічні епопеї для тебе")
        items = _items(client, view["Id"])
        assert {i["Name"] for i in items} == {"Дюна", "Інтерстеллар"}
        assert all(i["Type"] == "Movie" for i in items)
        assert all(i["Id"].startswith("g2:") for i in items)
    finally:
        set_active_profile(None)
        home_cache.clear()
