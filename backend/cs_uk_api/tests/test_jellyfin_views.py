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

from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

import importlib

from cs_uk_api.config import SETTINGS
from cs_uk_api.main import _home_cache, _home_sources_cache
from cs_uk_api.models import SearchResult, Section
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, model_b_axes

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
) -> SearchResult:
    mb_form, mb_styles = model_b_axes(cast(Any, media_type))
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form=mb_form,
        styles=mb_styles,
        title=title,
        year=year,
        poster=poster,
        url=f"https://{pid}.example/{n}",
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
    """The 5-row snapshot: «Новинки», «Популярні зараз», Фільми,
    Серіали, Аніме — with a poster-bearing movie, a poster-less movie,
    and a poster-bearing series."""
    return _ViewsStub(
        "animeon",
        newest_section="page",
        newest=[
            _item("animeon", "Дюна", "movie", 2021, poster=_POSTER_MOVIE),
            _item("animeon", "Сокіл", "movie", 2019),
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
                _item("animeon", "Сокіл", "movie", 2019, n="2"),
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
    _home_cache.clear()
    _home_sources_cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        _home_cache.clear()
        _home_sources_cache.clear()


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
        "Новинки",
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


def test_poster_primary_serves_poster_inline(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_poster_primary_ignores_max_width(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert client.get(f"/Items/{falcon_id}/Images/Primary", headers={"X-Emby-Token": TOKEN}).status_code == 404


def test_poster_primary_unknown_item_is_404(client: TestClient) -> None:
    assert client.get("/Items/g1:unknown/Images/Primary", headers={"X-Emby-Token": TOKEN}).status_code == 404


def test_poster_primary_query_is_single_encoded(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_poster_primary_is_public_without_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
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