import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
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


STREAM_HTML = (pathlib.Path(__file__).parent / "fixtures" / "uakino" / "stream.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_uakino_stream_resolves_iframe_url():
    import httpx
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uakino.club/player/s1e1.html").respond(200, text=STREAM_HTML)
        async with httpx.AsyncClient(headers={"User-Agent": "test"}) as http:
            s = await UakinoProvider().stream("s1e1", "uk", http)
    assert s.url == "https://cdn.uakino.club/player/abc123/index.m3u8"
    assert s.type == "m3u8"
    assert s.headers.get("Referer", "").startswith("https://uakino.club")


BROWSE_HTML = (pathlib.Path(__file__).parent / "fixtures" / "uakino" / "browse_filmy.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_uakino_browse_parses_listing_and_detects_next():
    import httpx
    from cs_uk_api.providers.uakino import UAKINO_SECTIONS, _section_url

    filmy_id = UAKINO_SECTIONS[0].id
    with respx.mock(assert_all_called=True) as router:
        router.get(_section_url(filmy_id, 1)).respond(200, text=BROWSE_HTML)
        async with httpx.AsyncClient(headers={"User-Agent": "test"}) as http:
            results, has_next = await UakinoProvider().browse(filmy_id, 1, http)
    assert has_next is True
    assert len(results) == 2
    assert all(r.id.startswith("uakino:film-") for r in results)
    assert all(r.type == "movie" for r in results)


@pytest.mark.asyncio
async def test_uakino_browse_unknown_section_raises():
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await UakinoProvider().browse("nonexistent", 1, httpx.AsyncClient())


@pytest.mark.asyncio
async def test_uakino_content_bad_external_id_raises_not_found():
    """Regression: `content()` must reject anything that does not match
    the `<kind>-<slug>` slug regex before interpolating into the URL —
    otherwise a caller-supplied `../../etc/passwd` would let httpx
    fetch from an attacker-controlled path."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await UakinoProvider().content("../../etc/passwd", http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_uakino_stream_bad_content_id_raises_not_found():
    """Regression: `stream()` interpolates `content_id` into
    `/player/{content_id}.html`; reject anything that is not a safe
    `[a-zA-Z0-9_-]+` episode code."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await UakinoProvider().stream("../../etc/passwd", None, http)
    assert exc.value.code == "not_found"
