"""Tests for the Serialno provider (issue #17, Group 2).

The live site is a DLE-style CMS at https://serialno.tv. The homepage
IS the series listing (no separate /series/ path), so the v2 contract
exposes a single `series` section backed by `/` and `/page/N/`.

The stream chain is two-hop: content page → `tortuga.tw/embed/<id>`
iframe (the first `.fplayer iframe`) → obfuscated `file:` payload
decoded with the upstream torDecrypt algorithm — same shape as
KinoVezha. The second iframe (`.fplayer iframe:nth-of-type(2)`) is a
trailer and is ignored.
"""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.serialno import SerialnoProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "serialno"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_serialno_search_parses_results():
    """Search response for "друзі" contains 6 cards. Every result has
    a `serialno:` id, a title, a poster URL, and a `series` type
    (the provider is series-only per the spec)."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://serialno.tv/index.php?do=search").respond(
            200, text=search_html
        )
        async with httpx.AsyncClient() as http:
            results = await SerialnoProvider().search("друзі", http)
    assert len(results) == 6
    assert all(r.provider == "serialno" for r in results)
    assert all(r.id.startswith("serialno:") for r in results)
    assert all(r.type == "series" for r in results)
    # All posters must be absolute URLs (card data-src is relative).
    assert all(r.poster is not None and r.poster.startswith("https://") for r in results)
    # All URLs must be absolute.
    assert all(r.url.startswith("https://serialno.tv/") for r in results)


@pytest.mark.asyncio
async def test_serialno_browse_series_page1():
    """The homepage is the series listing. The captured `/` listing
    has 20 `.th-item` cards, all of which classify as `series`. The
    pagination block lists pages 2..10 + 103, so has_next is True."""
    listing_html = _fixture("series_listing_page1.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await SerialnoProvider().browse("series", 1, http)
    assert len(results) == 20
    assert all(r.type == "series" for r in results)
    assert all(r.id.startswith("serialno:") for r in results)
    # IDs are bare slugs (no section prefix on serialno).
    assert all("serialno:" in r.id and "/" not in r.id.split(":", 1)[1] for r in results)
    assert has_next is True


@pytest.mark.asyncio
async def test_serialno_browse_series_page2():
    """The captured `/page/2/` listing has cards. `page2` mirror
    confirms pagination works mid-stream."""
    listing_html = _fixture("series_listing_page2.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/page/2/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await SerialnoProvider().browse("series", 2, http)
    assert len(results) >= 1
    assert all(r.type == "series" for r in results)
    # The last page is 103; page 2 has higher pages, so has_next is True.
    assert has_next is True


@pytest.mark.asyncio
async def test_serialno_browse_series_last_page():
    """Requesting a page >= highest link (103 in this listing) must
    yield has_next=False so the client stops paging."""
    listing_html = _fixture("series_listing_page1.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/page/400/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            _, has_next = await SerialnoProvider().browse("series", 400, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_serialno_content_series_parses_title_poster_player():
    """Series content page: title (Cyrillic), poster (absolute URL),
    and the first `.fplayer iframe` data-src pointing to
    tortuga.tw/embed/<id>. The series has at least one season with
    at least one episode decoded from the obfuscated `file:` payload."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_embed.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await SerialnoProvider().content("2075-1670", http)
    assert c.title == "1670"
    assert c.type == "series"
    assert c.poster is not None
    assert c.poster.startswith("https://serialno.tv/")
    assert c.seasons is not None
    assert len(c.seasons) >= 1
    assert all(len(s.episodes) >= 1 for s in c.seasons)
    # First season, first episode from the captured playlist.
    first = c.seasons[0]
    assert first.number == 1
    assert first.episodes[0].id == "2075-1670:s1e1"


@pytest.mark.asyncio
async def test_serialno_content_description_and_translation():
    """The content response carries the page description (from
    `.fdesc`) and a single Ukrainian translation (the only language
    the site ships)."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_embed.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await SerialnoProvider().content("2075-1670", http)
    assert "сатиричн" in c.description
    assert c.translations == [{"id": "uk", "label": "Українська"}] or (
        len(c.translations) == 1 and c.translations[0].id == "uk"
    )


@pytest.mark.asyncio
async def test_serialno_stream_series_resolves_episode_m3u8():
    """Two-hop stream for a series episode: content page -> player
    page (`tortuga.tw/embed/<id>`) -> obfuscated `file:` payload ->
    season/episode JSON list -> per-episode m3u8 URL. Episode id is
    `<external>:s1e1` (1-based)."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_embed.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            s = await SerialnoProvider().stream("2075-1670:s1e1", None, http)
    assert s.url.startswith("https://calypso.tortuga.tw/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    # tortuga.tw requires a Referer to serve the manifest; the
    # upstream Kotlin sets it to the page origin.
    assert s.headers.get("Referer") == "https://serialno.tv/"


@pytest.mark.asyncio
async def test_serialno_stream_series_season2_resolves():
    """The captured playlist has 2 seasons. An episode from season 2
    (s2e1) must resolve to a different m3u8 URL than s1e1, proving
    the season index is honored."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_embed.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            s1 = await SerialnoProvider().stream("2075-1670:s1e1", None, http)
            s2 = await SerialnoProvider().stream("2075-1670:s2e1", None, http)
    assert s1.url != s2.url
    assert "s01e01" in s1.url
    assert "s02e01" in s2.url


def test_serialno_sections_lists_one():
    """The site is series-only per the spec; one section is exposed."""
    sections = SerialnoProvider().sections
    ids = [s.id for s in sections]
    assert ids == ["series"]
    assert all(s.type == "series" for s in sections)


@pytest.mark.asyncio
async def test_serialno_browse_unknown_section_raises():
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError) as exc_info:
            await SerialnoProvider().browse("films", 1, httpx.AsyncClient())
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_serialno_content_bad_slug_raises():
    """Regression: the provider must validate the slug at the
    boundary so malformed inputs surface as `not_found` BEFORE any
    HTTP request is made. Path-traversal attempts like
    `2075-../admin` would otherwise escape the URL space."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await SerialnoProvider().content("2075-../admin", http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_serialno_stream_bad_slug_raises():
    """Same regression for `stream()`: the slug must be validated
    before any HTTP request is made."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await SerialnoProvider().stream("2075-../admin:s1e1", None, http)
    assert exc_info.value.code == "not_found"
