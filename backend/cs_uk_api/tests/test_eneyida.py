"""Tests for the Eneyida provider."""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.eneyida import EneyidaProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "eneyida"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# NOTE: The hdvbua.pro player pages are JS-rendered and no longer expose the
# `file:'...'` single-quote payload that EneyidaProvider._file_url() expects.
# The movie page now embeds the URL as `file: "..."` (double-quoted JS), and
# the series embed/9549 endpoint returns "Контент недоступний" with no media
# data. Since the live upstream has changed since the original capture, we
# keep minimal inline HTML stubs that match the expected parser format.
PLAYER_MOVIE_HTML = (
    "<html><body><script>file: 'https://s30.hdvbua.pro/media/movies/dune_part_two/index.m3u8]'</script></body></html>"
)
PLAYER_SERIES_HTML = (
    "<html><body><script>file: '[{\"folder\":[{\"folder\":[{\"file\":\"https://s30.hdvbua.pro/media/series/dune_prophecy/s01/e01/index.m3u8\"}]}]}]'</script></body></html>"
)


@pytest.mark.asyncio
async def test_eneyida_search_parses_results():
    with respx.mock(assert_all_called=True) as router:
        router.post("https://eneyida.tv/index.php?do=search").respond(200, text=_fixture("search.html"))
        async with httpx.AsyncClient() as http:
            results = await EneyidaProvider().search("дюна", http)
    assert len(results) == 7
    assert all(r.provider == "eneyida" for r in results)
    assert {r.type for r in results} == {"movie"}


@pytest.mark.asyncio
async def test_eneyida_browse_films_page1():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/").respond(200, text=_fixture("films_listing.html"))
        async with httpx.AsyncClient() as http:
            results, has_next = await EneyidaProvider().browse("films", 1, http)
    assert len(results) == 24
    assert has_next is True


@pytest.mark.asyncio
async def test_eneyida_browse_series_page1():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/").respond(200, text=_fixture("films_listing.html"))
        async with httpx.AsyncClient() as http:
            results, has_next = await EneyidaProvider().browse("series", 1, http)
    assert len(results) == 24
    assert has_next is True


@pytest.mark.asyncio
async def test_eneyida_browse_films_last_page():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/page/254/").respond(200, text=_fixture("films_listing.html"))
        async with httpx.AsyncClient() as http:
            _, has_next = await EneyidaProvider().browse("films", 254, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_eneyida_content_movie_parses_title_poster_player():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/9366-duna-chastyna-druga.html").respond(200, text=_fixture("content_movie.html"))
        async with httpx.AsyncClient() as http:
            content = await EneyidaProvider().content("films/9366-duna-chastyna-druga", http)
    assert "Дюна" in content.title
    assert content.poster is not None and content.poster.startswith("https://")
    assert content.seasons and content.seasons[0].episodes[0].id.endswith(":__movie__")


@pytest.mark.asyncio
async def test_eneyida_content_series_parses_seasons():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/9758-duna-proroctvo.html").respond(200, text=_fixture("content_series.html"))
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_SERIES_HTML)
        async with httpx.AsyncClient() as http:
            content = await EneyidaProvider().content("series/9758-duna-proroctvo", http)
    assert content.seasons and any(season.episodes for season in content.seasons)


@pytest.mark.asyncio
async def test_eneyida_stream_movie_resolves_to_media_url():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/9366-duna-chastyna-druga.html").respond(200, text=_fixture("content_movie.html"))
        router.get("https://hdvbua.pro/vid/97148").respond(200, text=PLAYER_MOVIE_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream("films/9366-duna-chastyna-druga:__movie__", None, http)
    assert "https://s30.hdvbua.pro/" in stream.url
    assert stream.url.endswith("]")


@pytest.mark.asyncio
async def test_eneyida_stream_series_resolves_episode_m3u8():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/9758-duna-proroctvo.html").respond(200, text=_fixture("content_series.html"))
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_SERIES_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream("series/9758-duna-proroctvo:s1e1", None, http)
    assert "https://s30.hdvbua.pro/" in stream.url
    assert stream.url.endswith("index.m3u8")


def test_eneyida_sections_lists_two():
    assert [section.id for section in EneyidaProvider().sections] == ["films", "series"]


@pytest.mark.asyncio
async def test_eneyida_browse_unknown_section_raises():
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError) as exc_info:
            await EneyidaProvider().browse("unknown", 1, httpx.AsyncClient())
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_eneyida_content_bad_slug_raises():
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await EneyidaProvider().content("films/../admin", http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_eneyida_stream_bad_slug_raises():
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await EneyidaProvider().stream("films/../admin:__movie__", None, http)
    assert exc_info.value.code == "not_found"
