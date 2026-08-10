"""Jellyfin facade search mapping (ticket #106).

Ticket #106's acceptance, pinned at the HTTP seam (the same seam the
views/detail tests use):

  - ``GET /Items?searchTerm=<q>`` (and the user-prefixed
    ``/Users/{id}/Items?searchTerm=<q>``) feeds the SHARED ``/api/search``
    merged groups and returns listing-shaped cards: one card per merged
    group, ``g1:`` ids, ``Movie``/``Series`` types, and
    ``ImageTags.Primary`` present *iff* the card has a poster (D9).
  - Cross-provider duplicates collapse into ONE card (the merge core).
  - Opening a search result resolves in the #105 detail surface:
    ``/Items/{g1:...}`` returns the movie/series detail and
    ``/Items?parentId=<g1:...>`` the season hierarchy — the facade
    registers search groups into the shared group-key resolution map,
    because most search results are NOT in the 30-min home snapshot.
  - ``GET /Search/Hints?searchTerm=<q>`` returns the same cards in
    hint shape (``ItemId`` = the ``g1:`` key).
  - Empty/absent term and total upstream failure degrade to an EMPTY
    result (200), never an error — the Jellyfin-tolerant answer (D5).
  - All three surfaces sit behind the same ``require_token`` gate (D4).

Seeded via the same seam as the views/detail tests: a stub provider
whose ``search()`` returns canned hits and whose ``content()`` serves
canned detail.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS
from cs_uk_api.main import (
    _blocklist_cache,
    _content_cache,
    _home_cache,
    _home_sources_cache,
    _search_cache,
)
from cs_uk_api.main import (
    _catalog_gated_cache as _gated_cache,
)
from cs_uk_api.models import ContentResponse, Episode, SearchResult, Season, Translation
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError

TOKEN = SETTINGS.jellyfin_token
USER = "fdc808859fc45eb8ac5aa6faddc12c72"

_POSTER_MOVIE = "https://cdn.example.test/posters/dune.jpg"
_POSTER_SERIES = "https://cdn.example.test/posters/serial.jpg"


def _dune() -> ContentResponse:
    return ContentResponse(
        id="p1:dune-1",
        type="movie",
        title="Дюна",
        year=2021,
        description="Епічна науково-фантастична стрічка.",
        poster=_POSTER_MOVIE,
        translations=[Translation(id="uk", label="Дубляж")],
    )


def _serial() -> ContentResponse:
    return ContentResponse(
        id="p1:serial-1",
        type="series",
        title="Сериалал серіал",
        year=2023,
        description="Детективний серіал.",
        poster=_POSTER_SERIES,
        translations=[Translation(id="uk", label="Дубляж")],
        seasons=[
            Season(
                number=1,
                episodes=[
                    Episode(number=1, id="s1e1", title="Серія 1"),
                    Episode(number=2, id="s1e2", title="Серія 2"),
                ],
            ),
        ],
    )


def _result(
    pid: str,
    external: str,
    title: str,
    media_type: str,
    *,
    year: int | None = None,
    poster: str | None = None,
) -> SearchResult:
    return SearchResult(
        id=f"{pid}:{external}",
        provider=pid,
        type=cast(Any, media_type),
        title=title,
        year=year if year is not None else (2021 if media_type == "movie" else 2023),
        poster=poster,
        url=f"https://{pid}.example/{external}",
    )


class _SearchStub(BaseProvider):
    """Search-capable provider stub: ``search()`` serves canned hits,
    ``content()`` canned detail keyed by external id (the seam the #105
    resolver reads after a search registers the group)."""

    def __init__(
        self,
        pid: str,
        results: list[SearchResult],
        content_by_external: dict[str, ContentResponse] | None = None,
    ) -> None:
        self.id = pid
        self.name = pid.title()
        self.types = ("movie", "series", "anime", "cartoon", "dorama")
        self._results = results
        self._content_by_external = content_by_external or {}
        self.search_calls = 0

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        self.search_calls += 1
        return [r.model_copy(deep=True) for r in self._results]

    async def browse(
        self, section: str, page: int, http: Any
    ) -> tuple[list[SearchResult], bool]:
        return [], False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        content = self._content_by_external.get(external_id)
        if content is None:
            raise ProviderError("not_found", f"no canned content for {external_id}")
        return content.model_copy(deep=True)

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> Any:
        raise NotImplementedError


def _seed() -> _SearchStub:
    """One stub with a poster-bearing movie, a poster-less movie, and a
    poster-bearing series, plus canned content for the movie + series."""
    return _SearchStub(
        "p1",
        [
            _result("p1", "dune-1", "Дюна", "movie", year=2021, poster=_POSTER_MOVIE),
            _result("p1", "falcon-1", "Сокіл", "movie", year=2019),
            _result("p1", "serial-1", "Сериалал серіал", "series", year=2023, poster=_POSTER_SERIES),
        ],
        content_by_external={"dune-1": _dune(), "serial-1": _serial()},
    )


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS and every cache the facade reads
    (including the shared search cache) so no real upstream calls or
    stale state leak into assertions."""
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (
        _search_cache,
        _home_cache,
        _home_sources_cache,
        _content_cache,
        _blocklist_cache,
        _gated_cache,
    ):
        cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        for cache in (
            _search_cache,
            _home_cache,
            _home_sources_cache,
            _content_cache,
            _blocklist_cache,
            _gated_cache,
        ):
            cache.clear()


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


def _search_items(client: TestClient, term: str, path: str = "/Items") -> list[dict[str, Any]]:
    r = client.get(
        path,
        params={"searchTerm": term, "userId": USER},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    return cast("list[dict[str, Any]]", r.json()["Items"])


def _detail(client: TestClient, item_id: str) -> dict[str, Any]:
    r = client.get(f"/Items/{item_id}", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    return cast("dict[str, Any]", r.json())


# ---------------------------------------------------------------------------
# /Items?searchTerm= — the SDK search surface
# ---------------------------------------------------------------------------


def test_search_term_returns_merged_cards(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    items = _search_items(client, "дюна")
    assert len(items) == 3
    by_name = {i["Name"]: i for i in items}
    dune = by_name["Дюна"]
    assert dune["Type"] == "Movie"
    assert dune["ProductionYear"] == 2021
    assert dune["Id"].startswith("g1:")
    assert set(dune["ImageTags"].keys()) == {"Primary"}
    # Poster-less card: no ImageTags (D9).
    assert by_name["Сокіл"]["ImageTags"] == {}
    serial = by_name["Сериалал серіал"]
    assert serial["Type"] == "Series"
    assert set(serial["ImageTags"].keys()) == {"Primary"}


def test_search_merges_cross_provider_duplicates(client: TestClient) -> None:
    PROVIDERS["p1"] = _SearchStub(
        "p1",
        [_result("p1", "dune-1", "Дюна", "movie", year=2021, poster=_POSTER_MOVIE)],
    )
    PROVIDERS["p2"] = _SearchStub(
        "p2",
        [_result("p2", "dune-2", "Дюна", "movie", year=2021, poster=_POSTER_MOVIE)],
    )
    items = _search_items(client, "дюна")
    assert len(items) == 1
    assert items[0]["Name"] == "Дюна"
    assert items[0]["Id"].startswith("g1:")
    assert items[0]["Type"] == "Movie"


def test_search_user_items_spelling_equivalent(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    bare = _search_items(client, "дюна")
    user = _search_items(client, "дюна", path=f"/Users/{USER}/Items")
    assert user == bare


def test_search_term_absent_keeps_listing(client: TestClient) -> None:
    """Without searchTerm the /Items route stays a plain listing — the
    capture-first surface from ticket #103 is untouched (AC: fixtures
    from 03 pass for the search path)."""
    from cs_uk_api.models import Section

    class _HomeStub(BaseProvider):
        id = "animeon"
        name = "Animeon"
        types = ("movie", "series")
        newest_section = "page"
        sections = (Section(id="movie", title="Фільми", type="movie"),)

        async def search(self, query: str, http: Any) -> list[SearchResult]:
            return []

        async def browse(
            self, section: str, page: int, http: Any
        ) -> tuple[list[SearchResult], bool]:
            if section == "page":
                return [_result("animeon", "1", "Дюна", "movie", year=2021, poster=_POSTER_MOVIE)], False
            if section == "movie":
                return [_result("animeon", "2", "Дюна", "movie", year=2021, poster=_POSTER_MOVIE)], False
            return [], False

        async def content(self, external_id: str, http: Any) -> ContentResponse:
            raise NotImplementedError

        async def stream(self, content_id: str, translation: str | None, http: Any) -> Any:
            raise NotImplementedError

    PROVIDERS["animeon"] = _HomeStub()
    # Warm the home snapshot (same call the views tests use).
    assert client.get("/api/home").status_code == 200
    views = client.get("/UserViews", headers={"X-Emby-Token": TOKEN}).json()["Items"]
    movie_view = next(v["Id"] for v in views if v["Name"] == "Фільми")
    listing = client.get(
        "/Items",
        params={"parentId": movie_view, "userId": USER},
        headers={"X-Emby-Token": TOKEN},
    ).json()["Items"]
    assert len(listing) == 1 and listing[0]["Name"] == "Дюна"


# ---------------------------------------------------------------------------
# Search → detail (the #105 surface)
# ---------------------------------------------------------------------------


def test_search_result_opens_in_movie_detail(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    dune_id = next(i["Id"] for i in _search_items(client, "дюна") if i["Name"] == "Дюна")
    detail = _detail(client, dune_id)
    assert detail["Type"] == "Movie"
    assert detail["Overview"] == "Епічна науково-фантастична стрічка."
    assert detail["Id"] == dune_id
    assert set(detail["ImageTags"].keys()) == {"Primary"}


def test_search_result_series_hierarchy_resolves(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    serial_id = next(
        i["Id"] for i in _search_items(client, "дюна") if i["Name"] == "Сериалал серіал"
    )
    detail = _detail(client, serial_id)
    assert detail["Type"] == "Series"

    r = client.get(
        "/Items",
        params={"parentId": serial_id, "userId": USER},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    seasons = r.json()["Items"]
    assert [s["Type"] for s in seasons] == ["Season"]
    assert seasons[0]["Id"] == f"{serial_id}:S1"
    assert seasons[0]["ParentId"] == serial_id


# ---------------------------------------------------------------------------
# /Search/Hints — the search-box surface
# ---------------------------------------------------------------------------


def test_search_hints_shape(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    r = client.get(
        "/Search/Hints",
        params={"searchTerm": "дюна"},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["TotalRecordCount"] == 3
    by_name = {h["Name"]: h for h in body["SearchHints"]}
    dune = by_name["Дюна"]
    assert dune["ItemId"].startswith("g1:")
    assert dune["Id"] == dune["ItemId"]
    assert dune["Type"] == "Movie"
    assert set(dune["ImageTags"].keys()) == {"Primary"}
    # Poster-less card: no ImageTags (D9).
    assert by_name["Сокіл"]["ImageTags"] == {}
    assert by_name["Сериалал серіал"]["Type"] == "Series"


def test_search_hints_requires_token(client: TestClient) -> None:
    assert client.get("/Search/Hints", params={"searchTerm": "дюна"}).status_code == 401


# ---------------------------------------------------------------------------
# Degradation + gate
# ---------------------------------------------------------------------------


def test_search_empty_term_is_empty_result(client: TestClient) -> None:
    r = client.get(
        "/Items",
        params={"searchTerm": "  ", "userId": USER},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    assert r.json() == {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}

    hints = client.get("/Search/Hints", headers={"X-Emby-Token": TOKEN}).json()
    assert hints == {"SearchHints": [], "TotalRecordCount": 0}


def test_search_total_failure_degrades_to_empty(client: TestClient) -> None:
    class _Failing(_SearchStub):
        async def search(self, query: str, http: Any) -> list[SearchResult]:
            raise ProviderError("upstream_unreachable", "boom")

    PROVIDERS["p1"] = _Failing("p1", [])
    items = _search_items(client, "дюна")
    assert items == []
    hints = client.get(
        "/Search/Hints", params={"searchTerm": "дюна"}, headers={"X-Emby-Token": TOKEN}
    ).json()
    assert hints == {"SearchHints": [], "TotalRecordCount": 0}


def test_search_requires_token(client: TestClient) -> None:
    assert client.get("/Items", params={"searchTerm": "дюна"}).status_code == 401
    assert client.get(f"/Users/{USER}/Items", params={"searchTerm": "дюна"}).status_code == 401


# ---------------------------------------------------------------------------
# Search cards → poster (D9)
# ---------------------------------------------------------------------------


def test_search_result_poster_served_inline(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    jf_router = importlib.import_module("cs_uk_api.jellyfin.router")
    poster_bytes = b"\xff\xd8\xff\xe0jpegbytes"

    async def _fake(url: str, client: Any) -> tuple[bytes, str]:
        return poster_bytes, "image/jpeg"

    monkeypatch.setattr(jf_router, "fetch_poster_bytes", _fake)
    PROVIDERS["p1"] = _seed()
    dune_id = next(i["Id"] for i in _search_items(client, "дюна") if i["Name"] == "Дюна")
    r = client.get(f"/Items/{dune_id}/Images/Primary", follow_redirects=False)
    assert r.status_code == 200
    assert r.content == poster_bytes
    assert r.headers["content-type"] == "image/jpeg"
