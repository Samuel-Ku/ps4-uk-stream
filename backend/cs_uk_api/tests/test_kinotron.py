"""Tests for the KinoTron HTML provider."""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.kinotron import KinoTronProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "kinotron"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_kinotron_search_parses_real_response():
    with respx.mock(assert_all_called=True) as router:
        router.post("https://kinotron.tv/index.php").respond(200, text=_fixture("search.html"))
        async with httpx.AsyncClient() as http:
            results = await KinoTronProvider().search("one piece", http)
    assert len(results) == 3
    assert results[0].title == "Ван Піс / Великий куш"
    assert results[0].provider == "kinotron"
    assert results[0].type == "series"
    assert results[0].id.startswith("kinotron:4808")


@pytest.mark.asyncio
async def test_kinotron_sections_match_upstream_main_page():
    assert [s.id for s in KinoTronProvider().sections] == [
        "films", "serials", "cartoons", "cartoon-series", "anime"
    ]


@pytest.mark.asyncio
async def test_kinotron_browse_films_has_exact_cards_and_next():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinotron.tv/films/page/1/").respond(200, text=_fixture("films_listing.html"))
        async with httpx.AsyncClient() as http:
            results, has_next = await KinoTronProvider().browse("films", 1, http)
    assert len(results) == 18
    assert all(result.type == "movie" for result in results)
    assert has_next is True
    assert all(r.url.startswith("https://kinotron.tv/") for r in results)


@pytest.mark.asyncio
async def test_kinotron_browse_last_page_has_no_next():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinotron.tv/films/page/19/").respond(
            200, text=_fixture("films_listing_last.html")
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await KinoTronProvider().browse("films", 19, http)
    assert len(results) == 14
    assert has_next is False


@pytest.mark.asyncio
async def test_kinotron_content_movie_parses_title_poster():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinotron.tv/10496-mesniki-shodzhennja-doktora-duma.html").respond(200, text=_fixture("content_movie.html"))
        async with httpx.AsyncClient() as http:
            content = await KinoTronProvider().content("10496-mesniki-shodzhennja-doktora-duma", http)
    assert content.title.startswith("Месники: Сходження Доктора Дума")
    assert content.type == "movie"
    assert content.poster and content.poster.startswith("https://kinotron.tv/")


@pytest.mark.asyncio
async def test_kinotron_series_parses_seasons_and_type():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinotron.tv/3663-pervorodn-pradavn-pershonarodzhenn.html").respond(200, text=_fixture("content_series.html"))
        router.get("https://ashdi.vip/serial/3329").respond(200, text=_fixture("player_series.html"))
        async with httpx.AsyncClient() as http:
            content = await KinoTronProvider().content("3663-pervorodn-pradavn-pershonarodzhenn", http)
    assert content.type == "series"
    assert content.seasons and len(content.seasons) == 2
    assert len(content.seasons[0].episodes) == 22
    assert content.seasons[1].episodes[0].number == 1
    assert content.translation_level == "episode"
    assert len(content.seasons[0].episodes[0].translations or []) == 3


@pytest.mark.asyncio
async def test_kinotron_stream_rebuilds_url_from_external_id():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinotron.tv/3663-pervorodn-pradavn-pershonarodzhenn.html").respond(200, text=_fixture("content_series.html"))
        router.get("https://ashdi.vip/serial/3329").respond(200, text=_fixture("player_series.html"))
        async with httpx.AsyncClient() as http:
            stream = await KinoTronProvider().stream("3663-pervorodn-pradavn-pershonarodzhenn", None, http)
    assert stream.url.endswith("index.m3u8")
    assert stream.type == "m3u8"


@pytest.mark.asyncio
async def test_kinotron_stream_selects_requested_episode():
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://kinotron.tv/3663-pervorodn-pradavn-pershonarodzhenn.html"
        ).respond(200, text=_fixture("content_series.html"))
        router.get("https://ashdi.vip/serial/3329").respond(
            200, text=_fixture("player_series.html")
        )
        async with httpx.AsyncClient() as http:
            stream = await KinoTronProvider().stream(
                "kinotron:3663-pervorodn-pradavn-pershonarodzhenn:s1e2",
                "Bezro Studio",
                http
            )
    assert "s01e02_244619" in stream.url


def test_kinotron_type_classification_checks_mixed_prefixes():
    html = '<div class="fsubtitle">Мультсеріал</div>'
    assert KinoTronProvider._type_from_subtitle(html) == "cartoon"
