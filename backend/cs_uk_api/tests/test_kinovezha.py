"""Tests for the KinoVezha provider (issue #17, Group 1)."""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.kinovezha import KinoVezhaProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "kinovezha"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_kinovezha_search_parses_results():
    """Real search response for query "всесв" contains 3 distinct cards."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://kinovezha.tv/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await KinoVezhaProvider().search("всесв", http)
    # Real captured search response has exactly 3 cards.
    assert len(results) == 3
    titles = [r.title for r in results]
    assert any("Володарі Всесвіту" in t for t in titles)
    assert all(r.provider == "kinovezha" for r in results)
    # The external_id is the URL slug (no kind prefix on KinoVezha):
    # e.g. "2809-volodari-vsesvitu".
    ids = {r.id for r in results}
    assert "kinovezha:2809-volodari-vsesvitu" in ids
    assert all(r.url.startswith("https://kinovezha.tv/") for r in results)


@pytest.mark.asyncio
async def test_kinovezha_sections_match_upstream_main_page():
    """The upstream Kotlin declares four sections:
    films / series / cartoons / s-cartoons."""
    sections = KinoVezhaProvider().sections
    ids = [s.id for s in sections]
    assert ids == ["films", "series", "cartoons", "s-cartoons"]


@pytest.mark.asyncio
async def test_kinovezha_browse_films_parses_results():
    """The /films/ section is upstream's "Фільми". Each card carries the
    "Фільми" Жанр tag → all classify as `movie`."""
    listing_html = _fixture("films_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/films/page/1/").respond(
            200, text=listing_html
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await KinoVezhaProvider().browse("films", 1, http)
    # Real captured listing: 18 .movie-item cards on page 1.
    assert len(results) == 18
    assert all(r.type == "movie" for r in results)
    assert all(r.id.startswith("kinovezha:") for r in results)
    assert all(r.url.startswith("https://kinovezha.tv/") for r in results)
    # The films listing shows 9+ pages of pagination → has_next True.
    assert has_next is True


@pytest.mark.asyncio
async def test_kinovezha_browse_series_parses_results():
    """The /series/ section is upstream's "Серіали". Each card carries
    the "Серіали" Жанр tag → all classify as `series`."""
    listing_html = _fixture("series_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/series/page/1/").respond(
            200, text=listing_html
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await KinoVezhaProvider().browse("series", 1, http)
    assert len(results) == 18
    assert all(r.type == "series" for r in results)
    assert has_next is True


@pytest.mark.asyncio
async def test_kinovezha_browse_cartoons_classifies_movies():
    """The /cartoons/ section is upstream's "Мультфільми" — animated
    films (one-offs), not serials. Cards carry the "Мультфільми" Жанр
    tag, classified as `movie` per the upstream heuristic
    ("Мультсеріали" or "Серіали" → series, else movie)."""
    listing_html = _fixture("cartoons_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/cartoons/page/1/").respond(
            200, text=listing_html
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await KinoVezhaProvider().browse("cartoons", 1, http)
    assert len(results) == 18
    assert all(r.type == "movie" for r in results)
    assert has_next is True


@pytest.mark.asyncio
async def test_kinovezha_browse_scartoons_classifies_series():
    """The /s-cartoons/ section is upstream's "Мультсеріали". Cards
    carry the "Мультсеріали" tag → classify as `series`."""
    listing_html = _fixture("s-cartoons_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/s-cartoons/page/1/").respond(
            200, text=listing_html
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await KinoVezhaProvider().browse(
                "s-cartoons", 1, http
            )
    # Real captured listing: 15 .movie-item cards on page 1.
    assert len(results) == 15
    assert all(r.type == "series" for r in results)
    # The s-cartoons listing has no pagination block — fits on one page.
    assert has_next is False


@pytest.mark.asyncio
async def test_kinovezha_browse_last_page_has_next_false():
    """Regression: when on a page beyond the highest pagination link,
    has_next must be False so the client stops paging."""
    listing_html = _fixture("films_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/films/page/999/").respond(
            200, text=listing_html
        )
        async with httpx.AsyncClient() as http:
            _, has_next = await KinoVezhaProvider().browse("films", 999, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_kinovezha_content_movie_parses_title_poster():
    content_html = _fixture("content_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/2809-volodari-vsesvitu.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await KinoVezhaProvider().content("2809-volodari-vsesvitu", http)
    assert "Володарі Всесвіту" in c.title
    # Жанр contains "Фільми" → movie, not series.
    assert c.type == "movie"
    assert c.poster is not None
    assert c.poster.startswith("https://kinovezha.tv/")
    # Movie content pages expose a single iframe; we surface it as
    # season 1, episode 1 so the client can hand it to /api/stream.
    assert c.seasons is not None
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) == 1


@pytest.mark.asyncio
async def test_kinovezha_content_series_parses_seasons():
    """The series fixture is for "Енн Дроїд". Жанр row contains
    "Серіали" → classify as series. The player page lists one season
    with two episodes (Серія 1, Серія 2)."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/2831-enn-droyid.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2859").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await KinoVezhaProvider().content("2831-enn-droyid", http)
    assert "Енн Дроїд" in c.title
    assert c.type == "series"
    assert c.seasons is not None
    assert [s.number for s in c.seasons] == [1]
    # Captured player JSON lists two episodes under season 1.
    assert len(c.seasons[0].episodes) == 2
    first = c.seasons[0].episodes[0]
    assert first.id.endswith(":s1e1")


@pytest.mark.asyncio
async def test_kinovezha_stream_resolves_to_m3u8():
    """Regression: `content_id` is the external_id (e.g.
    `2809-volodari-vsesvitu`), NOT a URL. The old call pattern was
    `http.get(content_id)` which raised `ValueError: unknown url type`
    on every call. The provider must rebuild the URL from the
    external_id before fetching.

    Movie content pages embed a single iframe whose src is the player
    URL. The player URL's HTML carries an obfuscated `file:` value
    that decodes (via the upstream `Decoder.decodeAndReverse`) to a
    direct m3u8 URL."""
    content_html = _fixture("content_movie.html")
    player_html = _fixture("player_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/2809-volodari-vsesvitu.html").respond(
            200, text=content_html
        )
        # The movie page embeds `<iframe ... src="https://tortuga.tw/vod/129293">`.
        router.get("https://tortuga.tw/vod/129293").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            s = await KinoVezhaProvider().stream(
                "2809-volodari-vsesvitu", None, http
            )
    # The Decoder.decodeAndReverse resolution yields a direct m3u8.
    assert s.url.startswith("https://")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_kinovezha_stream_series_episode_resolves_to_m3u8():
    """Series episode: `content_id` includes the s{N}e{M} suffix. The
    provider splits it, fetches the player page, decodes the
    season/episode JSON, and returns the direct m3u8."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/2831-enn-droyid.html").respond(
            200, text=content_html
        )
        # The series content page embeds two iframes; the first is
        # `https://tortuga.tw/embed/2859` — the player for the episode
        # JSON list.
        router.get("https://tortuga.tw/embed/2859").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            s = await KinoVezhaProvider().stream(
                "2831-enn-droyid:s1e1", None, http
            )
    assert s.url.startswith("https://")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_kinovezha_browse_unknown_section_raises():
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await KinoVezhaProvider().browse(
                "nonexistent", 1, httpx.AsyncClient()
            )


@pytest.mark.asyncio
async def test_kinovezha_content_missing_title_raises_parse_failed():
    """Regression: a content page with no `.inner-page__title` must
    surface as `parse_failed` rather than crash with AttributeError."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/2809-volodari-vsesvitu.html").respond(
            200, text="<html><body>no title here</body></html>"
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await KinoVezhaProvider().content(
                    "2809-volodari-vsesvitu", http
                )
    assert exc_info.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_kinovezha_stream_invalid_episode_suffix_raises():
    """Regression: code-reviewer caught dead-code fallback. When the
    caller passes an out-of-range s{N}e{M} suffix, the resolver must
    raise `parse_failed` rather than silently returning the first
    available episode."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_series.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://kinovezha.tv/2831-enn-droyid.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2859").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await KinoVezhaProvider().stream(
                    "2831-enn-droyid:s99e99", None, http
                )
    assert exc_info.value.code == "parse_failed"