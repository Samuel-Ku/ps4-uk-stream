"""Tests for the KinoTron HTML provider."""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
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
    """A playable movie page (real player iframe, not trailer-only) must
    parse into a movie ContentResponse (#163: the Месники page is
    trailer-only, so this test uses the Дюна VOD fixture)."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinotron.tv/9728-djuna.html").respond(200, text=_fixture("content_movie_vod.html"))
        async with httpx.AsyncClient() as http:
            content = await KinoTronProvider().content("9728-djuna", http)
    assert content.title.startswith("Дюна")
    assert content.type == "movie"
    assert content.poster and content.poster.startswith("https://kinotron.tv/")


@pytest.mark.asyncio
async def test_kinotron_content_trailer_only_page_raises_gated():
    """Issue #163: a page whose video box carries only a youtube embed
    is trailer-only (upstream has no playable player) — content() must
    raise ``gated`` so the catalog sweep drops the dead card."""
    page = (
        '<html><body><div class="full"><h1>Месники</h1></div>'
        '<div class="fsubtitle">Фільм</div>'
        '<div class="video-box">'
        '<iframe width="560" height="400" data-src="https://www.youtube.com/embed/IRycQ32qo88"></iframe>'
        '</div></body></html>'
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinotron.tv/10496-trailer-only.html").respond(200, text=page)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await KinoTronProvider().content("10496-trailer-only", http)
    assert exc_info.value.code == "gated"


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
    assert content.translations_level == "episode"
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
async def test_kinotron_stream_movie_player_with_direct_m3u8():
    """Live-gate regression (2026-08-01): ashdi movie players stopped
    returning the `file: '[{...}]'` JSON array and now return a direct
    m3u8: `file: 'https://.../index.m3u8'`. The stream must still resolve."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinotron.tv/9728-djuna.html").respond(200, text=_fixture("content_movie_vod.html"))
        router.get("https://ashdi.vip/vod/176240").respond(200, text=_fixture("player_movie.html"))
        async with httpx.AsyncClient() as http:
            # Production shape: /api/stream strips the `kinotron:`
            # prefix before calling stream().
            stream = await KinoTronProvider().stream("9728-djuna", None, http)
    assert stream.url.startswith("https://ashdi.vip/video01/")
    assert stream.url.endswith("index.m3u8")
    assert stream.type == "m3u8"


@pytest.mark.asyncio
async def test_kinotron_series_with_dead_player_keeps_default_translation():
    """Live-gate regression (2026-08-01): a series whose player page no
    longer exposes any playable files (dead ashdi vod) must not crash with
    an empty translations list — content stays readable with the default
    Ukrainian track."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinotron.tv/3663-pervorodn-pradavn-pershonarodzhenn.html").respond(200, text=_fixture("content_series.html"))
        router.get("https://ashdi.vip/serial/3329").respond(200, text=_fixture("player_movie.html"))
        async with httpx.AsyncClient() as http:
            content = await KinoTronProvider().content("3663-pervorodn-pradavn-pershonarodzhenn", http)
    assert content.type == "series"
    assert content.translations and len(content.translations) == 1
    assert content.translations[0].id == "uk"


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
            # Regression (diagnostics 2026-08-08): the route passes the
            # provider-stripped id `slug:sNeM` (2 parts), NOT the
            # provider-prefixed `kinotron:slug:sNeM` (3 parts). The old
            # parser took parts[-1] as the external_id and answered 404
            # for every series episode.
            stream = await KinoTronProvider().stream(
                "3663-pervorodn-pradavn-pershonarodzhenn:s1e2",
                "Bezro Studio",
                http
            )
    assert "s01e02_244619" in stream.url


def test_kinotron_type_classification_checks_mixed_prefixes():
    html = '<div class="fsubtitle">Мультсеріал</div>'
    assert KinoTronProvider._type_from_subtitle(html) == "cartoon"


@pytest.mark.asyncio
async def test_kinotron_content_bad_external_id_raises_not_found():
    r"""Regression: `content()` must reject external_ids that do not
    match the `\d+-[a-z0-9-]+` slug regex — otherwise a caller-supplied
    `../../etc/passwd` would escape the upstream URL path."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await KinoTronProvider().content("../../etc/passwd", http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_kinotron_stream_bad_content_id_raises_not_found():
    """Regression: `stream()` splits `content_id` on `:` and rebuilds
    the content URL from the embedded external_id; reject payload that
    escapes the slug charset before the first HTTP call."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await KinoTronProvider().stream("../../etc/passwd", None, http)
    assert exc.value.code == "not_found"
