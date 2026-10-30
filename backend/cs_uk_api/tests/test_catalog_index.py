"""Local catalog index: FTS5 search, local-first search, hints, crawler.

The index is deliberately exercised over tmp SQLite files. The route tests
clear the provider registry first, so a local-first assertion can never
accidentally fan out to a real provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import cs_uk_api.config as config_mod
from cs_uk_api import _catalog_state as catalog_state
from cs_uk_api import catalog, catalog_index
from cs_uk_api.catalog_index import CatalogIndex, crawl_index
from cs_uk_api.main import app
from cs_uk_api.models import SearchResult, Section
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

client = TestClient(app)


def _item(
    provider: str = "index-live",
    external: str = "1",
    title: str = "Дюна: Частина друга",
    year: int | None = 2021,
    *,
    genres: list[str] | None = None,
    form: str = "movie",
) -> SearchResult:
    return SearchResult(
        id=f"{provider}:{external}",
        provider=provider,
        form=form,  # type: ignore[arg-type]
        title=title,
        year=year,
        url=f"https://{provider}.example/{external}",
        genres=genres or [],
    )


class _SearchStub(BaseProvider):
    """Search-only provider with a call counter (local-first proof)."""

    id = "index-live"
    name = "Index Live"
    types = ("movie",)

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls = 0

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        self.calls += 1
        return list(self.results)

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class _BrowseStub(BaseProvider):
    """Two-page browse provider, enough to exercise the crawler contract."""

    id = "browse-stub"
    name = "Browse Stub"
    types = ("movie",)
    sections = (Section(id="films", title="Фільми", form="movie"),)

    def __init__(self) -> None:
        self.pages: list[tuple[str, int]] = []

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def browse(self, section, page, http):  # type: ignore[no-untyped-def]
        self.pages.append((section, page))
        if page > 2:
            return [], False
        items = [
            _item(self.id, f"{page}-{index}", f"Фільм {page}-{index}")
            for index in range(2)
        ]
        return items, page < 2


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    """No real provider may receive a request from these tests."""
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


@pytest.fixture
def index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CatalogIndex]:
    instance = CatalogIndex(str(tmp_path / "catalog.sqlite3"))
    monkeypatch.setattr(catalog_index, "get_index", lambda: instance)
    try:
        yield instance
    finally:
        instance.close()


def test_fts_finds_cyrillic_prefix_and_both_transliterations(index: CatalogIndex) -> None:
    index.upsert([_item()])
    assert [result.id for result in index.search("дюна")] == ["index-live:1"]
    assert [result.id for result in index.search("дю")] == ["index-live:1"]
    assert [result.id for result in index.search("duna")] == ["index-live:1"]
    assert [result.id for result in index.search("diuna")] == ["index-live:1"]
    assert [result.id for result in index.search("дюна частина")] == ["index-live:1"]
    assert index.search("дюна детектив") == []


def test_fts_finds_latin_titles_and_genres(index: CatalogIndex) -> None:
    index.upsert([_item(external="2", title="Dune", genres=["фантастика"])])
    assert [result.id for result in index.search("dune")] == ["index-live:2"]
    assert [result.id for result in index.search("фантастика")] == ["index-live:2"]
    assert index.search("дюна") == []


def test_alias_titles_index_every_language(index: CatalogIndex) -> None:
    index.upsert([_item(title="Тато / Daddy")])
    assert [result.id for result in index.search("тато")] == ["index-live:1"]
    assert [result.id for result in index.search("daddy")] == ["index-live:1"]


def test_short_queries_do_not_expand_into_broad_latin_prefixes(
    index: CatalogIndex,
) -> None:
    index.upsert(
        [
            _item("p1", "1", "Дюна", 2021),
            _item("p2", "2", "Dune", 2021),
        ]
    )
    assert [result.id for result in index.search("дю")] == ["p1:1"]
    assert {result.id for result in index.search("дюн")} == {"p1:1", "p2:2"}


def test_native_spelling_outranks_transliteration_collision(index: CatalogIndex) -> None:
    """«Дюна» must beat «Дуна!» for «дюна» even when the latter is fresher.
    Native exact → native prefix → transliteration, in that order."""
    index.upsert(
        [
            _item("p1", "1", "Дуна!", 2025),
            _item("p2", "2", "Дюна", 2021),
            _item("p3", "3", "Дюна: Пророцтво", 2024, form="series"),
        ]
    )
    assert [result.title for result in index.search("дюна")] == [
        "Дюна",
        "Дюна: Пророцтво",
        "Дуна!",
    ]


def test_upsert_updates_in_place_and_fts_triggers(index: CatalogIndex) -> None:
    index.upsert([_item(title="Дюна")])
    index.upsert([_item(title="Дюна: Частина друга")])
    assert index.count() == 1
    assert [result.title for result in index.search("частина")] == [
        "Дюна: Частина друга"
    ]


def test_groups_merge_same_title_across_providers(index: CatalogIndex) -> None:
    index.upsert([_item("p1", "1"), _item("p2", "2")])
    groups = catalog_index.groups_from_sources(index.search("дюна"))
    assert len(groups) == 1
    assert {source.provider for source in groups[0].sources} == {"p1", "p2"}


def test_hints_are_distinct_and_limited(index: CatalogIndex) -> None:
    index.upsert(
        [
            _item("p1", "1"),
            _item("p2", "2"),
            _item("p3", "3", "Дюна 1984", 1984),
        ]
    )
    hints = index.hints("дюна", limit=10)
    assert len(hints) == 2
    assert {hint.title for hint in hints} == {"Дюна: Частина друга", "Дюна 1984"}
    assert len(index.hints("дюна", limit=1)) == 1


def test_hints_collapse_yearless_duplicates_but_keep_distinct_years(
    index: CatalogIndex,
) -> None:
    """Real crawl data: yearless cards of one series key differently, so a
    typeahead showed «Дюна» four times. Known years stay as separate works."""
    index.upsert(
        [
            _item("p1", "1", "Дюна", 2000, form="series"),
            _item("p2", "2", "Дюна", None, form="series"),
            _item("p3", "3", "Дюна", None, form="series"),
            _item("p4", "4", "Дюна", 2021, form="series"),
        ]
    )
    hints = index.hints("дюна", limit=10)
    assert len(hints) == 2
    assert {(hint.title, hint.year) for hint in hints} == {
        ("Дюна", 2000),
        ("Дюна", 2021),
    }
    assert all(hint.year is not None for hint in hints)


def test_local_index_answers_without_live_fanout(
    index: CatalogIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.upsert([_item()])
    monkeypatch.setattr(
        config_mod, "SETTINGS", replace(config_mod.SETTINGS, index_local_min=1)
    )
    stub = _SearchStub([_item("other", "9", "Інший результат")])
    PROVIDERS["index-live"] = stub

    response = asyncio.run(catalog.search("дюна"))

    assert stub.calls == 0
    assert [group.title for group in response.groups] == ["Дюна: Частина друга"]


def test_local_search_honours_an_explicit_provider(
    index: CatalogIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.upsert([_item("p1", "1"), _item("p2", "2")])
    monkeypatch.setattr(
        config_mod, "SETTINGS", replace(config_mod.SETTINGS, index_local_min=1)
    )
    p1 = _SearchStub()
    p1.id = "p1"
    p2 = _SearchStub()
    p2.id = "p2"
    PROVIDERS["p1"] = p1
    PROVIDERS["p2"] = p2

    response = asyncio.run(catalog.search("дюна", provider="p1"))

    assert {source.provider for group in response.groups for source in group.sources} == {
        "p1"
    }


def test_empty_index_falls_back_live_and_indexes_results(index: CatalogIndex) -> None:
    stub = _SearchStub([_item(external="7", title="Живий результат")])
    PROVIDERS["index-live"] = stub

    response = asyncio.run(catalog.search("живий"))

    assert stub.calls == 1
    assert [group.title for group in response.groups] == ["Живий результат"]
    assert [result.id for result in index.search("живий")] == ["index-live:7"]


def test_live_results_merge_with_local_when_below_threshold(
    index: CatalogIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.upsert([_item(external="1", title="Дюна")])
    monkeypatch.setattr(
        config_mod, "SETTINGS", replace(config_mod.SETTINGS, index_local_min=5)
    )
    stub = _SearchStub([_item(external="2", title="Дюна: Частина друга")])
    PROVIDERS["index-live"] = stub

    response = asyncio.run(catalog.search("дюна"))

    assert stub.calls == 1
    assert {group.title for group in response.groups} == {
        "Дюна",
        "Дюна: Частина друга",
    }


def test_local_results_survive_a_total_live_failure(
    index: CatalogIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.upsert([_item()])
    monkeypatch.setattr(
        config_mod, "SETTINGS", replace(config_mod.SETTINGS, index_local_min=5)
    )
    PROVIDERS["index-live"] = _SearchStub([])

    async def _timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise HTTPException(502, detail={"error": "search_timeout"})

    monkeypatch.setattr(catalog_state, "merged_search", _timeout)

    response = asyncio.run(catalog.search("дюна"))

    assert [group.title for group in response.groups] == ["Дюна: Частина друга"]


def test_deterministic_provider_failure_is_not_masked_by_local_hits(
    index: CatalogIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.upsert([_item()])
    monkeypatch.setattr(
        config_mod, "SETTINGS", replace(config_mod.SETTINGS, index_local_min=5)
    )
    PROVIDERS["index-live"] = _SearchStub([])

    async def _warming(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise HTTPException(503, detail={"error": "warming"})

    monkeypatch.setattr(catalog_state, "merged_search", _warming)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(catalog.search("дюна"))
    assert raised.value.status_code == 503


def test_hints_route_is_index_only(index: CatalogIndex) -> None:
    index.upsert([_item(title="Дюна")])
    stub = _SearchStub([_item("other", "9", "Живий")])
    PROVIDERS["index-live"] = stub

    response = client.get("/api/search/hints", params={"q": "дю"})

    assert response.status_code == 200
    assert [hint["title"] for hint in response.json()["hints"]] == ["Дюна"]
    assert stub.calls == 0


def test_hints_route_rejects_empty_query() -> None:
    assert client.get("/api/search/hints", params={"q": ""}).status_code == 422


def test_crawler_walks_pages_until_has_next_is_false(index: CatalogIndex) -> None:
    stub = _BrowseStub()
    PROVIDERS["browse-stub"] = stub

    async def _run():
        async with httpx.AsyncClient() as http:
            return await crawl_index(
                ["browse-stub"],
                pages=5,
                index=index,
                http=http,
            )

    stats = asyncio.run(_run())

    assert stub.pages == [("films", 1), ("films", 2)]
    assert stats.providers[0].pages == 2
    assert stats.providers[0].seen == 4
    assert stats.providers[0].indexed == 4
    assert stats.failed_providers == ()
    assert index.count() == 4


def test_crawler_reports_unknown_provider(index: CatalogIndex) -> None:
    async def _run():
        async with httpx.AsyncClient() as http:
            return await crawl_index(["ghost"], pages=1, index=index, http=http)

    stats = asyncio.run(_run())

    assert stats.failed_providers == ("ghost",)
    assert stats.providers[0].errors == ("unknown provider",)
