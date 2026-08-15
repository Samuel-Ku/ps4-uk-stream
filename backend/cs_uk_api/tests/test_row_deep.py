"""Deep rows — lazy pagination of home rows (spec #305).

Wire-level: a request for a page beyond a home row's snapshot extends
the row's pool from provider browse pages 2..N (bounded by the depth
knob, round-robin + group-key dedupe), the Items route returns NEW cards
for page 2 with an honest ``TotalRecordCount``, a failing extension
degrades to the snapshot slice, and the personalized/genre rails stay
snapshot-bounded. Prior art: test_home.py + test_jellyfin_views.py.
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import catalog_state as cs
from cs_uk_api import config as _config
from cs_uk_api.config import SETTINGS
from cs_uk_api.main import _home_cache, _home_sources_cache
from cs_uk_api.models import SearchResult, Section
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, model_b_axes

jf_router = importlib.import_module("cs_uk_api.jellyfin.router")

TOKEN = SETTINGS.jellyfin_token
USER = "fdc808859fc45eb8ac5aa6faddc12c72"


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
        genres=[],
    )


def _movie(pid: str, n: int) -> SearchResult:
    return _item(pid, f"Фільм {n}", "movie", 2020 + (n % 5), n=str(n))


def _series(pid: str, n: int) -> SearchResult:
    return _item(pid, f"Серіал {n}", "series", 2020 + (n % 5), n=f"s{n}")


class _PagedStub(BaseProvider):
    """A browse-capable provider with per-section paginated listings.

    ``pages`` maps ``section_id -> {page_number: results}``; a page not
    in the map is the honest end (``has_next=False``). ``browse_calls``
    records every (section, page) so tests can assert cache/depth
    behaviour on the extension path.
    """

    def __init__(
        self,
        pid: str,
        *,
        sections: tuple[Section, ...],
        pages: dict[str, dict[int, list[SearchResult]]],
        newest_section: str | None = None,
    ) -> None:
        self.id = pid
        self.name = pid.title()
        self.types = ("movie", "series")
        self.sections = sections
        self.newest_section = newest_section
        self._pages = pages
        self.browse_calls: list[tuple[str, int]] = []

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def content(self, external_id: str, http: Any) -> Any:
        raise NotImplementedError

    async def stream(self, content_id: str, translation: str | None, http: Any) -> Any:
        raise NotImplementedError

    async def browse(self, section: str, page: int, http: Any) -> tuple[list[SearchResult], bool]:
        self.browse_calls.append((section, page))
        page_map = self._pages.get(section)
        if page_map is None:
            raise NotImplementedError(f"section {section} not stubbed")
        results = page_map.get(page, [])
        return list(results), page in page_map


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS and the shared caches (pattern from
    test_jellyfin_views.py) so no real upstream calls leak in."""
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    _home_cache.clear()
    _home_sources_cache.clear()
    cs.row_deep_cache.clear()
    cs.deep_page_cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        _home_cache.clear()
        _home_sources_cache.clear()
        cs.row_deep_cache.clear()
        cs.deep_page_cache.clear()


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


def _auth(client: TestClient) -> None:
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


def _movie_provider() -> _PagedStub:
    """A movie section with 20 cards on page 1 and 20 more on page 2 —
    the deep-rows scenario: page 1 fills the snapshot row, page 2+ only
    exists upstream."""
    return _PagedStub(
        "p",
        sections=(Section(id="movie", title="Фільми", form="movie"),),
        pages={
            "movie": {
                1: [_movie("p", n) for n in range(1, 21)],
                2: [_movie("p", n) for n in range(21, 41)],
            }
        },
    )


# ---------------------------------------------------------------------------
# Wire: the Items route extends a row past the snapshot
# ---------------------------------------------------------------------------


def test_items_page_2_extends_row_with_new_cards_and_honest_count(
    client: TestClient,
) -> None:
    """Page 2 (``startIndex=20``) must serve NEW cards — the extension
    fetched the provider's page 2 and the pool grew — with an honest
    ``TotalRecordCount`` that tells the client more pages exist."""
    PROVIDERS["p"] = _movie_provider()
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))

    page1 = _items_page(client, movie_view, start_index=0, limit=20)
    assert len(page1["Items"]) == 20
    assert page1["TotalRecordCount"] == 20
    page1_ids = {i["Id"] for i in page1["Items"]}

    page2 = _items_page(client, movie_view, start_index=20, limit=20)
    assert len(page2["Items"]) == 20
    assert page2["TotalRecordCount"] == 40  # snapshot 20 + page 2's 20
    assert page1_ids.isdisjoint({i["Id"] for i in page2["Items"]})

    # The honest end: a page beyond the extended pool comes back short.
    beyond = _items_page(client, movie_view, start_index=40, limit=20)
    assert beyond["Items"] == []
    assert beyond["TotalRecordCount"] == 40


def test_items_page_2_dedupes_against_the_snapshot(client: TestClient) -> None:
    """A deeper-page card whose group key already sits in the snapshot
    row must not repeat (US3: next page shows NEW cards)."""
    provider = _movie_provider()
    # Page 2 re-emits Фільм 5 (same title/form/year as the snapshot's
    # Фільм 5, which is 2020 = 2020 + (5 % 5) — same group key) plus 20
    # genuinely new cards.
    provider._pages["movie"][2] = [
        _item("p", "Фільм 5", "movie", 2020, n="999"),
        *[_movie("p", n) for n in range(21, 41)],
    ]
    PROVIDERS["p"] = provider
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))

    page1 = _items_page(client, movie_view, start_index=0, limit=20)
    page1_ids = {i["Id"] for i in page1["Items"]}
    page2 = _items_page(client, movie_view, start_index=20, limit=20)

    assert len(page2["Items"]) == 20  # the duplicate folded, 20 new remain
    assert page1_ids.isdisjoint({i["Id"] for i in page2["Items"]})
    assert page2["TotalRecordCount"] == 40


def test_items_extension_degrades_to_snapshot_slice(client: TestClient) -> None:
    """A failing deeper fetch must never break browsing: the request
    past the snapshot serves the honest empty tail of the snapshot
    slice (spec: graceful degradation)."""
    provider = _movie_provider()

    async def _broken_browse(section: str, page: int, http: Any):  # type: ignore[no-untyped-def]
        if page >= 2:
            raise RuntimeError("upstream flap")
        return await _PagedStub.browse(provider, section, page, http)

    provider.browse = _broken_browse  # type: ignore[method-assign]
    PROVIDERS["p"] = provider
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))

    page1 = _items_page(client, movie_view, start_index=0, limit=20)
    assert page1["TotalRecordCount"] == 20
    page2 = _items_page(client, movie_view, start_index=20, limit=20)
    assert page2["Items"] == []
    assert page2["TotalRecordCount"] == 20  # snapshot slice, honest end


def test_items_extension_respects_depth_knob(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CS_UK_ROW_MAX_PAGES`` bounds how deep the extension fetches
    (US6): with 2, only page 2 is fetched and the row ends at 40."""
    monkeypatch.setattr(
        _config, "SETTINGS", dataclasses.replace(_config.SETTINGS, row_max_pages=2)
    )
    provider = _movie_provider()
    # Page 3 exists upstream but must never be fetched.
    provider._pages["movie"][3] = [_movie("p", n) for n in range(41, 61)]
    PROVIDERS["p"] = provider
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))

    _items_page(client, movie_view, start_index=20, limit=20)
    fetched = {p for s, p in provider.browse_calls}
    assert 3 not in fetched
    assert 2 in fetched

    beyond = _items_page(client, movie_view, start_index=40, limit=20)
    assert beyond["Items"] == []
    assert beyond["TotalRecordCount"] == 40


def test_items_extension_is_cached_across_requests(client: TestClient) -> None:
    """Repeated scroll passes stay instant (US5): the second page-2
    request reuses the cached pool without re-fetching upstream."""
    provider = _movie_provider()
    PROVIDERS["p"] = provider
    _auth(client)
    movie_view = _view_id("Фільми", _views(client))

    _items_page(client, movie_view, start_index=20, limit=20)
    first_calls = list(provider.browse_calls)
    assert first_calls

    _items_page(client, movie_view, start_index=20, limit=20)
    _items_page(client, movie_view, start_index=30, limit=10)
    assert provider.browse_calls == first_calls


# ---------------------------------------------------------------------------
# Unit: the extension path
# ---------------------------------------------------------------------------


def test_extend_row_pool_returns_none_for_bounded_rows() -> None:
    """Personalized rows («Нові серії», «Нещодавно переглянуто»,
    recommendation rails) and genre rails stay snapshot-bounded (spec
    #305 scope) — their pool IS the snapshot by design."""
    items = [_item("p", "X", "movie", 2021)]
    for row_type in ("new_episodes", "recently_watched", "recommended", "similar", "genre:драми"):
        assert asyncio_run(cs.extend_row_pool(row_type, items)) is None


def test_extend_row_pool_returns_none_when_every_fetch_fails() -> None:
    PROVIDERS["p"] = _movie_provider()

    async def _broken(section: str, page: int, http: Any):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")

    PROVIDERS["p"].browse = _broken  # type: ignore[method-assign]
    items = [_item("p", "X", "movie", 2021)]
    assert asyncio_run(cs.extend_row_pool("movie", items)) is None


def test_extend_row_pool_recent_row_filters_by_form() -> None:
    """The «Нещодавно додані: Фільми» row extends from the providers'
    newest sections with the form filter applied — series cards on
    deeper pages never leak into a movie row."""
    provider = _PagedStub(
        "p",
        newest_section="new",
        sections=(),
        pages={
            "new": {
                1: [_movie("p", n) for n in range(1, 21)],
                # page 2 mixes forms: 10 movies + 20 series
                2: [_movie("p", n) for n in range(21, 31)] + [_series("p", n) for n in range(1, 21)],
            }
        },
    )
    PROVIDERS["p"] = provider
    # The row is built by the real home fan-out (newest section page 1).
    from cs_uk_api.catalog_state import load_home

    home = asyncio_run(load_home())
    recent_movie = next(r for r in home.rows if r.type == "recent_movie")
    assert len(recent_movie.items) == 20

    pool = asyncio_run(cs.extend_row_pool("recent_movie", recent_movie.items))
    assert pool is not None
    assert len(pool) == 30  # 20 snapshot + 10 new movie-form cards
    assert all(it.form == "movie" for it in pool)


def asyncio_run(awaitable: Any) -> Any:
    import asyncio

    return asyncio.new_event_loop().run_until_complete(awaitable)
