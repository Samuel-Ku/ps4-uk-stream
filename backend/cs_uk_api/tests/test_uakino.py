import pathlib

import pytest
import respx

from cs_uk_api.providers.uakino import UakinoProvider

FIXTURE = (pathlib.Path(__file__).parent / "fixtures" / "uakino" / "search.html").read_text(
    encoding="utf-8"
)


@pytest.mark.asyncio
async def test_uakino_search_parses_results():
    import httpx
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uakino.club/search/").respond(200, text=FIXTURE)
        async with httpx.AsyncClient(headers={"User-Agent": "test"}) as http:
            results = await UakinoProvider().search("дюна", http)
    assert len(results) == 2
    assert results[0].id == "uakino:film-dune-2021"
    assert results[0].title.startswith("Дюна")
    assert results[0].type == "movie"
    assert results[0].poster is not None
    assert results[1].type == "series"


MOVIE_HTML = (pathlib.Path(__file__).parent / "fixtures" / "uakino" / "content_movie.html").read_text(encoding="utf-8")
SERIES_HTML = (pathlib.Path(__file__).parent / "fixtures" / "uakino" / "content_series.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_uakino_content_movie_parses_translations():
    import httpx
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uakino.club/film/dune-2021.html").respond(200, text=MOVIE_HTML)
        async with httpx.AsyncClient(headers={"User-Agent": "test"}) as http:
            c = await UakinoProvider().content("film-dune-2021", http)
    assert c.type == "movie"
    assert c.title == "Дюна"
    assert [t.id for t in c.translations] == ["uk", "en"]


@pytest.mark.asyncio
async def test_uakino_content_series_parses_seasons():
    import httpx
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uakino.club/serial/breaking-bad-s01.html").respond(200, text=SERIES_HTML)
        async with httpx.AsyncClient(headers={"User-Agent": "test"}) as http:
            c = await UakinoProvider().content("serial-breaking-bad-s01", http)
    assert c.type == "series"
    assert c.seasons is not None
    assert c.seasons[0].number == 1
    assert [e.id for e in c.seasons[0].episodes] == ["uakino:s1e1", "uakino:s1e2"]
