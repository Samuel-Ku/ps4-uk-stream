"""Subscription-gate handling (BambooUA "Для підписників" promo clips).

A gated title's only "stream" is a sponsor placeholder (be_sponsors.mp4).
The contract under test:

  - ``filter_gated_items`` drops KNOWN-gated cards and caches verdicts
    so later sweeps are free.
  - ``load_home`` drops gated cards BEFORE rows / the sources map are
    built, so a merged title keeps its working sources and a gated-only
    title disappears.
  - the search route filters gated results.
  - the native content/stream routes answer ``gated`` with 404 (never
    the promo clip, never a 502).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from cs_uk_api.models import (
    ContentResponse,
    SearchResult,
    Section,
    StreamResponse,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError

FIX = Path(__file__).parent / "fixtures" / "bambooua"


def _item(
    pid: str, n: str, title: str = "Айчаку", year: int = 2024
) -> SearchResult:
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        type="movie",
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
    )


class _GatedStub(BaseProvider):
    """can_gate provider whose content() reports every item gated."""

    id = "gated-stub"
    name = "GatedStub"
    types = ("movie",)
    sections = (Section(id="cinema", title="Фільми", type="movie"),)
    can_gate = True
    _results: ClassVar[list[SearchResult]] = []
    _content_calls: ClassVar[list[str]] = []

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        return list(self._results)

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        return list(self._results), False

    async def content(self, external_id: str, http: httpx.AsyncClient) -> ContentResponse:
        self._content_calls.append(external_id)
        raise ProviderError("gated", "subscription required")

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        raise AssertionError("unused")


class _FreeStub(BaseProvider):
    """Ordinary provider (can_gate=False) that resolves content fine."""

    id = "free-stub"
    name = "FreeStub"
    types = ("movie",)
    sections = (Section(id="cinema", title="Фільми", type="movie"),)
    _results: ClassVar[list[SearchResult]] = []

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        return list(self._results)

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        return list(self._results), False

    async def content(self, external_id: str, http: httpx.AsyncClient) -> ContentResponse:
        return ContentResponse(
            id=f"free-stub:{external_id}",
            type="movie",
            title="Айчаку",
            year=2024,
            translations=[Translation(id="uk", label="UK")],
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        return StreamResponse(url="https://free.example/m.m3u8", type="m3u8")


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS and every cache the sweep/facade reads."""
    from cs_uk_api import catalog_state
    from cs_uk_api import main as main_mod

    saved = dict(PROVIDERS)
    caches = [
        catalog_state.home_cache,
        catalog_state.sources_cache,
        catalog_state.content_cache,
        catalog_state.blocklist_cache,
        catalog_state.gated_cache,
        main_mod._search_cache,
        main_mod._browse_cache,
    ]
    for cache in caches:
        cache.clear()
    yield
    PROVIDERS.clear()
    PROVIDERS.update(saved)
    for cache in caches:
        cache.clear()


@pytest.mark.asyncio
async def test_filter_gated_items_drops_gated_and_caches_verdict() -> None:
    """Gated cards are dropped; the verdict is cached so the next sweep
    is a pure cache hit (no re-resolution)."""
    from cs_uk_api.catalog_state import filter_gated_items, gated_cache

    PROVIDERS["gated-stub"] = _GatedStub()
    PROVIDERS["free-stub"] = _FreeStub()
    items = [_item("gated-stub", "g1"), _item("free-stub", "f1")]
    async with httpx.AsyncClient() as http:
        out = await filter_gated_items(items, http)
    assert [i.id for i in out] == ["free-stub:f1"]
    assert gated_cache.get("content:gated-stub:g1") is True

    # Second sweep hits the cache: content() is not re-invoked.
    PROVIDERS["gated-stub"]._content_calls.clear()  # type: ignore[attr-defined]
    async with httpx.AsyncClient() as http:
        out2 = await filter_gated_items(items, http)
    assert [i.id for i in out2] == ["free-stub:f1"]
    assert PROVIDERS["gated-stub"]._content_calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_filter_gated_items_keeps_transient_upstream_errors() -> None:
    """A non-gated upstream failure must NOT drop the card (dead
    providers are health-tracked elsewhere)."""
    from cs_uk_api.catalog_state import filter_gated_items

    class _Flaky(_GatedStub):
        id = "flaky-stub"

        async def content(self, external_id, http):  # type: ignore[no-untyped-def]
            raise ProviderError("upstream_unreachable", "site down")

    PROVIDERS["flaky-stub"] = _Flaky()
    items = [_item("flaky-stub", "f1")]
    async with httpx.AsyncClient() as http:
        out = await filter_gated_items(items, http)
    assert [i.id for i in out] == ["flaky-stub:f1"]


@pytest.mark.asyncio
async def test_load_home_drops_gated_only_title() -> None:
    """A title available ONLY through a gated source disappears from the
    home rows (the sweep drops it before the rows are built)."""
    from cs_uk_api.catalog_state import load_home

    PROVIDERS.clear()
    gated = _GatedStub()
    gated._results = [_item("gated-stub", "g1", title="Лише Гейт")]
    PROVIDERS["gated-stub"] = gated
    home = await load_home()
    assert home.rows == []


@pytest.mark.asyncio
async def test_load_home_keeps_group_with_working_source() -> None:
    """A merged title survives when ANOTHER provider carries it: the
    gated source stops contributing, the group keeps the free source."""
    from cs_uk_api.catalog_state import load_home, resolve_group

    PROVIDERS.clear()
    gated = _GatedStub()
    gated._results = [_item("gated-stub", "g1")]
    free = _FreeStub()
    free._results = [_item("free-stub", "f1")]
    PROVIDERS["gated-stub"] = gated
    PROVIDERS["free-stub"] = free

    home = await load_home()
    movie_row = next(r for r in home.rows if r.type == "movie")
    assert len(movie_row.items) == 1
    item = movie_row.items[0]
    assert item.providers == ["free-stub"]
    per_provider = resolve_group(item.group_key)
    assert per_provider is not None
    assert set(per_provider) == {"free-stub"}


def test_search_route_drops_gated_results() -> None:
    """/api/search filters a can_gate provider's gated cards out of the
    merged groups."""
    from cs_uk_api.main import app

    gated = _GatedStub()
    gated._results = [_item("gated-stub", "g1")]
    PROVIDERS["gated-stub"] = gated
    r = TestClient(app).get("/api/search?q=Айчаку&provider=gated-stub")
    assert r.status_code == 200
    assert r.json()["groups"] == []


def test_search_route_keeps_group_with_free_source() -> None:
    """With both a gated and a free source for the same title, the group
    survives with only the free source in it."""
    from cs_uk_api.main import app

    PROVIDERS.clear()
    gated = _GatedStub()
    gated._results = [_item("gated-stub", "g1")]
    free = _FreeStub()
    free._results = [_item("free-stub", "f1")]
    PROVIDERS["gated-stub"] = gated
    PROVIDERS["free-stub"] = free

    r = TestClient(app).get("/api/search?q=Айчаку")
    assert r.status_code == 200
    groups = r.json()["groups"]
    assert len(groups) == 1
    sources = [s["provider"] for s in groups[0]["sources"]]
    assert sources == ["free-stub"]


def _aichaku_fixture() -> str:
    return (FIX / "content_movie.html").read_text(encoding="utf-8")


def test_content_route_gated_returns_404() -> None:
    """/api/content on a gated item answers 404 ``gated`` — the item is
    deliberately unavailable, not an upstream failure."""
    from cs_uk_api.main import app

    with respx.mock:
        respx.get("https://bambooua.com/cinema/1159-aichaku.html").respond(
            200, text=_aichaku_fixture()
        )
        r = TestClient(app).get("/api/content/bambooua:cinema/1159-aichaku")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "gated"


def test_stream_route_gated_returns_404() -> None:
    """/api/stream on a gated item answers 404 ``gated`` — the promo
    clip is never handed to the player."""
    from cs_uk_api.main import app

    with respx.mock:
        respx.get("https://bambooua.com/cinema/1159-aichaku.html").respond(
            200, text=_aichaku_fixture()
        )
        r = TestClient(app).get("/api/stream/bambooua:cinema/1159-aichaku:__movie__")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "gated"


def test_stream_route_free_movie_returns_m3u8() -> None:
    """A non-gated movie streams normally (HLS typed by its URL)."""
    from cs_uk_api.main import app

    free_html = (FIX / "content_movie_free.html").read_text(encoding="utf-8")
    with respx.mock:
        respx.get(
            "https://bambooua.com/cinema/1041-you-are-the-apple-of-my-eye.html"
        ).respond(200, text=free_html)
        r = TestClient(app).get(
            "/api/stream/bambooua:cinema/1041-you-are-the-apple-of-my-eye"
        )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "m3u8"
    assert body["url"].endswith("index.m3u8")
