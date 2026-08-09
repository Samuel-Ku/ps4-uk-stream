"""Tests for the DoramyWorld provider (issue #17, Group 2).

Fixtures captured live from https://doramy.world on 2026-08-01 via curl:

* ``search.html`` — GET /?s=pan returns 2 dorama cards.
* ``dorama_listing.html`` — GET /dorama/page/1/ returns 12 cards.
* ``film_listing.html`` — GET /film/page/1/ returns 12 cards.
* ``show_listing.html`` — GET /show/page/1/ returns 3 cards.
* ``content_dorama.html`` — GET /dorama/koroleva-chorin/ (12 episodes,
  one translation "K'Di (одноголосе озвучення)").
* ``content_film.html`` — GET /film/ekstremalna-robota/ (single vod).
* ``player_vod_film.html`` — ashdi.vip/vod/94600 served with the
  ``file:'...m3u8'`` payload.
* ``player_vod_dorama.html`` — ashdi.vip/vod/167245 served with the
  same shape (one episode per page load).
"""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.doramyworld import DoramyWorldProvider
from cs_uk_api.providers.base import ProviderError

FIX = pathlib.Path(__file__).parent / "fixtures" / "doramyworld"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_doramyworld_search_parses_results():
    """search() returns one SearchResult per article.type-* card on
    the WordPress search page. The /?s=pan capture returns 2 cards."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await DoramyWorldProvider().search("pan", http)
    # Two type-dorama cards on /?s=pan.
    assert len(results) == 2
    assert all(r.provider == "doramyworld" for r in results)
    titles = [r.title for r in results]
    assert any("Пандора" in t for t in titles)
    # Search cards on doramy.world are dorama or film; never generic
    # post type=post.
    assert all(r.type in {"dorama", "movie", "series"} for r in results)


@pytest.mark.asyncio
async def test_doramyworld_search_classifies_by_url_path():
    """Each card is classified by its URL path: /film/ -> movie,
    /dorama/ -> dorama, /show/ -> series. Longest prefix wins."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await DoramyWorldProvider().search("pan", http)
    types_by_kind = {r.url.split("/")[3]: r.type for r in results}
    # The /?s=pan capture only surfaces dorama cards.
    assert types_by_kind.get("dorama") == "dorama"


@pytest.mark.asyncio
async def test_doramyworld_search_external_id_preserves_slug():
    """external_id must round-trip through `content_url`. The capture's
    dorama URL `https://doramy.world/dorama/koroleva-chorin/` should
    rebuild from `dorama/koroleva-chorin`."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await DoramyWorldProvider().search("pan", http)
    panda = [r for r in results if "panda-i-yizhachok" in r.url]
    assert len(panda) == 1
    assert panda[0].id == "doramyworld:dorama/panda-i-yizhachok"


@pytest.mark.asyncio
async def test_doramyworld_browse_dorama_parses_results():
    """REGRESSION: /dorama/page/1/ has 12 type-dorama cards."""
    listing_html = _fixture("dorama_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/dorama/page/1/").respond(
            200, text=listing_html
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await DoramyWorldProvider().browse(
                "dorama", 1, http
            )
    assert len(results) == 12
    assert all(r.type == "dorama" for r in results)
    # The site has 19 pages of dorama.
    assert has_next is True


@pytest.mark.asyncio
async def test_doramyworld_browse_film_parses_results():
    """REGRESSION: /film/page/1/ has 12 type-film cards."""
    listing_html = _fixture("film_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/film/page/1/").respond(
            200, text=listing_html
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await DoramyWorldProvider().browse(
                "film", 1, http
            )
    assert len(results) == 12
    assert all(r.type == "movie" for r in results)
    assert has_next is True


@pytest.mark.asyncio
async def test_doramyworld_browse_follows_page_one_canonical_redirect():
    """REGRESSION (#171): the upstream 301s `/film/page/1/` to the
    canonical `/film/`. browse() must follow the same-host redirect via
    safe_get and parse the canonical page instead of raising not_found
    on the 301 status."""
    listing_html = _fixture("film_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/film/page/1/").respond(
            301, headers={"Location": "https://doramy.world/film/"}
        )
        router.get("https://doramy.world/film/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await DoramyWorldProvider().browse("film", 1, http)
    assert len(results) == 12
    assert all(r.type == "movie" for r in results)
    assert has_next is True


@pytest.mark.asyncio
async def test_doramyworld_browse_last_page_has_next_false():
    """When the requested page is the last page, has_next is False."""
    listing_html = _fixture("dorama_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/dorama/page/99/").respond(
            200, text=listing_html
        )
        async with httpx.AsyncClient() as http:
            _, has_next = await DoramyWorldProvider().browse("dorama", 99, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_doramyworld_content_dorama_parses_title_poster():
    content_html = _fixture("content_dorama.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/dorama/koroleva-chorin/").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await DoramyWorldProvider().content("dorama/koroleva-chorin", http)
    assert "Пан королева" in c.title
    assert c.type == "dorama"
    assert c.poster is not None
    assert c.poster.startswith("https://doramy.world")
    # K'Di is the single translation surfaced in the data-player JSON.
    assert any(t.id == "k-di" for t in c.translations)
    # Year is 2020 (the first year in the Рік: row).
    assert c.year == 2020


@pytest.mark.asyncio
async def test_doramyworld_content_dorama_parses_seasons():
    """Series pages expose the data-player JSON as Season[] per
    translation. The koroleva-chorin fixture has one translation with
    one season of 12 episodes."""
    content_html = _fixture("content_dorama.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/dorama/koroleva-chorin/").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await DoramyWorldProvider().content("dorama/koroleva-chorin", http)
    assert c.seasons is not None
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) == 12
    # Episodes are numbered 1..N and the ids encode the position so the
    # stream() resolver can pick the right ashdi URL.
    ep = c.seasons[0].episodes[0]
    assert ep.number == 1
    assert ep.id == "dorama/koroleva-chorin:s1e1"
    # Cyrillic "серія" is rendered with capital initial letter because
    # the implementation generates "Серія N" -- assert case-insensitively.
    assert "серія" in ep.title.lower()


@pytest.mark.asyncio
async def test_doramyworld_content_film_parses_seasons():
    """Film pages surface a single season with one episode."""
    content_html = _fixture("content_film.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/film/ekstremalna-robota/").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await DoramyWorldProvider().content("film/ekstremalna-robota", http)
    assert c.type == "movie"
    assert "Екстремальна робота" in c.title
    assert c.seasons is not None
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) == 1
    ep = c.seasons[0].episodes[0]
    assert ep.id == "film/ekstremalna-robota:s1e1"


@pytest.mark.asyncio
async def test_doramyworld_stream_resolves_to_m3u8():
    """REGRESSION: stream() must follow the iframe to ashdi.vip and
    pull the file: '...m3u8...' URL out of the inline JS."""
    content_html = _fixture("content_film.html")
    player_html = _fixture("player_vod_film.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/film/ekstremalna-robota/").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/vod/94600").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await DoramyWorldProvider().stream(
                "film/ekstremalna-robota:s1e1", None, http
            )
    # The fixture's player page has file:'https://ashdi.vip/video01/.../index.m3u8'.
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    assert s.headers["Referer"] == "https://ashdi.vip/"


@pytest.mark.asyncio
async def test_doramyworld_stream_rejects_player_redirect_to_disallowed_host():
    """The player URL comes from upstream HTML, so it must go through
    the SSRF redirect allowlist (issue #126): a player page that
    redirects to an attacker-controlled host fails closed with
    `not_found` instead of being followed."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_film.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/film/ekstremalna-robota/").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/vod/94600").respond(
            302, headers={"Location": "https://evil.example.com/pivot"}
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await DoramyWorldProvider().stream(
                    "film/ekstremalna-robota:s1e1", None, http
                )
    assert exc_info.value.code == "not_found"
    assert "disallowed host" in exc_info.value.message


@pytest.mark.asyncio
async def test_doramyworld_stream_series_episode_resolves_m3u8():
    """Series stream: content_id encodes season+episode; the resolver
    walks the data-player JSON to pick the right ashdi.vip URL."""
    content_html = _fixture("content_dorama.html")
    player_html = _fixture("player_vod_dorama.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/dorama/koroleva-chorin/").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/vod/167245").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await DoramyWorldProvider().stream(
                "dorama/koroleva-chorin:s1e1", None, http
            )
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_doramyworld_stream_unknown_episode_raises_not_found():
    """Out-of-range episode must raise not_found, not silently fall
    back to the first available episode."""
    content_html = _fixture("content_dorama.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://doramy.world/dorama/koroleva-chorin/").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await DoramyWorldProvider().stream(
                    "dorama/koroleva-chorin:s1e99", None, http
                )
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_doramyworld_sections_lists_three():
    """Per the upstream Kotlin `mainPage = mainPageOf(...)` declaration."""
    sections = DoramyWorldProvider().sections
    ids = [s.id for s in sections]
    assert ids == ["film", "dorama", "show"]


@pytest.mark.asyncio
async def test_doramyworld_browse_unknown_section_raises():
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await DoramyWorldProvider().browse(
                "nonexistent", 1, httpx.AsyncClient()
            )


@pytest.mark.asyncio
async def test_doramyworld_content_bad_slug_raises_not_found():
    """REGRESSION: external_id must be validated at the boundary."""
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError) as exc:
            await DoramyWorldProvider().content(
                "../../etc/passwd", httpx.AsyncClient()
            )
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_doramyworld_stream_bad_slug_raises_not_found():
    """REGRESSION: stream() must validate the external_id portion."""
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError) as exc:
            await DoramyWorldProvider().stream(
                "../bad:slug", None, httpx.AsyncClient()
            )
    assert exc.value.code == "not_found"
