"""Tests for the Coaninet provider (issue #17, Group 1).

Coaninet (https://coani.net) is a JSON API provider, similar to Unimay.
The upstream Kotlin plugin talks to ``https://coani.net/api/v1/...``
endpoints. Search, browse, content, and stream are thin ``httpx``
wrappers around the API; the content HTML page is a Nuxt SSR shell that
loads the same API data on the client.
"""
from __future__ import annotations

import json
import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.coaninet import CoaninetProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "coaninet"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _json(name: str) -> object:
    return json.loads(_fixture(name))


@pytest.mark.asyncio
async def test_coaninet_search_parses_results():
    """A real search hit must yield one SearchResult per API item, with
    the external_id taken from the season's numeric ``id`` (not the SEO
    slug) and the poster URL taken from ``preview.preview_main``."""
    search_json = _fixture("search.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(url__regex=r"https://coani\.net/api/v1/search.*").respond(
            200, text=search_json
        )
        async with httpx.AsyncClient() as http:
            results = await CoaninetProvider().search("Лиходійка", http)
    # search.json contains exactly 1 result for the "Лиходійка" query.
    assert len(results) == 1
    r = results[0]
    assert r.id == "coaninet:173"
    assert r.provider == "coaninet"
    assert r.type == "series"
    assert "Лиходійка" in r.title
    assert r.poster is not None
    assert r.poster.startswith("https://api.coani.net/uploads/resized/preview_main/")
    assert r.url == (
        "https://coani.net/catalog/"
        "the-villainess-is-adored-by-the-prince-of-the-neighbor-kingdom/"
        "the-villainess-is-adored-by-the-prince-of-the-neighbor-kingdom"
    )


@pytest.mark.asyncio
async def test_coaninet_browse_films_page1():
    """The films section is ``/api/v1/films?page=N``. Page 1 of the
    captured catalog fixture returns 10 results."""
    catalog_json = _fixture("catalog_page1.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(url__regex=r"https://coani\.net/api/v1/films.*").respond(
            200, text=catalog_json
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await CoaninetProvider().browse("films", 1, http)
    assert len(results) == 10
    assert all(r.provider == "coaninet" for r in results)
    # Meta paginator says pages=8, so has_next must be True.
    assert has_next is True


@pytest.mark.asyncio
async def test_coaninet_browse_series_page1():
    """The series section is ``/api/v1/series?page=N`` and returns the
    same shape as films (the captured catalog is a serial catalog)."""
    catalog_json = _fixture("catalog_page1.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(url__regex=r"https://coani\.net/api/v1/series.*").respond(
            200, text=catalog_json
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await CoaninetProvider().browse("series", 1, http)
    assert len(results) == 10
    assert all(r.provider == "coaninet" for r in results)
    assert has_next is True


@pytest.mark.asyncio
async def test_coaninet_content_film_parses_title_poster():
    """content() must return the Cyrillic title and the preview_main
    poster URL by hitting ``/api/v1/season/<id>``."""
    season_json = _fixture("season.json")
    series_json = _fixture("series.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://coani.net/api/v1/season/173").respond(
            200, text=season_json
        )
        router.get("https://coani.net/api/v1/season/173/series").respond(
            200, text=series_json
        )
        async with httpx.AsyncClient() as http:
            c = await CoaninetProvider().content("173", http)
    assert c.id == "coaninet:173"
    assert c.type == "series"
    assert "Лиходійка" in c.title
    assert c.year == 2026
    assert c.poster is not None
    assert c.poster.startswith("https://api.coani.net/uploads/resized/preview_main/")


@pytest.mark.asyncio
async def test_coaninet_content_series_parses_seasons():
    """content() must fetch both the season details and the episode
    list and return at least one season with at least one episode."""
    season_json = _fixture("season.json")
    series_json = _fixture("series.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://coani.net/api/v1/season/173").respond(
            200, text=season_json
        )
        router.get("https://coani.net/api/v1/season/173/series").respond(
            200, text=series_json
        )
        async with httpx.AsyncClient() as http:
            c = await CoaninetProvider().content("173", http)
    assert c.seasons is not None
    assert len(c.seasons) >= 1
    episodes = c.seasons[0].episodes
    assert len(episodes) >= 1
    # Coaninet exposes one entry per (episode, voice_type); multiple
    # voice_types per episode surface as per-episode translations.
    assert c.translations_level == "episode"


@pytest.mark.asyncio
async def test_coaninet_stream_resolves_to_media_url():
    """stream() must look up the episode in the series list and return
    its ``video`` m3u8 URL. The voice_type filter selects between
    POLYPHONIC and SUB variants of the same episode number."""
    season_json = _fixture("season.json")
    series_json = _fixture("series.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://coani.net/api/v1/season/173/series").respond(
            200, text=series_json
        )
        async with httpx.AsyncClient() as http:
            s = await CoaninetProvider().stream("173:1", "POLYPHONIC", http)
    assert s.url.endswith(".m3u8")
    assert s.url.startswith("https://s")  # s3.coani.net / s1.coani.net
    assert s.type == "m3u8"
    assert s.headers["Referer"] == "https://coani.net/"


@pytest.mark.asyncio
async def test_coaninet_sections_lists_two():
    """Per the upstream Kotlin's `mainPageOf(...)`, Coaninet exposes
    exactly two sections: films ("Фільми") and series ("Серіали")."""
    sections = CoaninetProvider().sections
    ids = [s.id for s in sections]
    assert ids == ["films", "series"]


@pytest.mark.asyncio
async def test_coaninet_browse_unknown_section_raises():
    """Unknown section ids must raise ProviderError("not_found") without
    hitting the network."""
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError) as exc_info:
            await CoaninetProvider().browse("nonexistent", 1, httpx.AsyncClient())
    assert exc_info.value.code == "not_found"