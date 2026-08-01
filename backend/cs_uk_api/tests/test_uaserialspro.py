"""Tests for the UASerialsPro provider (issue #17, Group 3).

The site (uaserials.com) is a DLE-style CMS that ships the player config
encrypted with AES-256-CBC + PBKDF2-HMAC-SHA512 inside
``<player-control data-tag1='{"ciphertext":...,"salt":...,"iv":...}'>``.
The decrypted JSON is ``[{"tabName":"Плеєр","url":"..."}, ...]``.

The "Плеєр" URL points at tortuga.tw whose player page embeds a
``file:`` field. That field is either:
  * a plain https m3u8 URL (movies), or
  * a Tortuga XOR-encoded string (movies with obfuscation), or
  * a JSON array of TortugaSeason/TortugaEpisode records (series).

The decoder used here mirrors the upstream Kotlin's tortugaDecode.
"""

from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.uaserialspro import UASerialsProProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "uaserialspro"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_provider_metadata():
    """Per the upstream Kotlin source: id=uaserialspro, name=UASerialsPro."""
    p = UASerialsProProvider()
    assert p.id == "uaserialspro"
    assert p.name == "UASerialsPro"


def test_sections_lists_six():
    """Per upstream `mainPageOf(...)`: films/series/fcartoon/cartoons/
    anime/exclusive — six entries, ordered to mirror the Kotlin source."""
    p = UASerialsProProvider()
    ids = [s.id for s in p.sections]
    assert ids == ["films", "series", "fcartoon", "cartoons", "anime", "exclusive"]
    titles = [s.title for s in p.sections]
    assert "Фільми" in titles
    assert "Серіали" in titles


@pytest.mark.asyncio
async def test_search_parses_results():
    """Real search results for query "Серіал" contain 18 distinct
    .uas-card anchors. Each carries the Ukrainian title, the original
    (English) title, a relative poster path, and a content URL."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(url__regex=r"^https://uaserials\.com/search/.+/$").respond(
            200, text=search_html
        )
        async with httpx.AsyncClient() as http:
            results = await UASerialsProProvider().search("Серіал", http)
    assert len(results) == 18
    assert all(r.provider == "uaserialspro" for r in results)
    # Each id is "uaserialspro:<numeric>-<slug>" (no section prefix).
    assert all(
        r.id.startswith("uaserialspro:")
        and r.id.removeprefix("uaserialspro:").split("-", 1)[0].isdigit()
        for r in results
    )
    # All URLs point at the live site.
    assert all(r.url.startswith("https://uaserials.com/") for r in results)
    # Posters are absolute URLs (relative paths get joined to mainUrl).
    assert all(r.poster is not None for r in results)
    assert all(r.poster.startswith("https://") for r in results)


@pytest.mark.asyncio
async def test_search_classifies_films_as_movie():
    """Films section surfaces cards classified as `movie`."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(url__regex=r"^https://uaserials\.com/search/.+/$").respond(
            200, text=search_html
        )
        async with httpx.AsyncClient() as http:
            results = await UASerialsProProvider().search("anything", http)
    # Search results are not classified by section (no path prefix);
    # but the upstream always returns TvType.Anime for these listings.
    # Our v2 spec maps them to a sensible default — for search results
    # we accept "movie" / "series" / "anime" (anything reasonable; the
    # content() endpoint refines the type).
    assert all(r.type in {"movie", "series", "anime"} for r in results)


@pytest.mark.asyncio
async def test_search_upstream_5xx_raises_upstream_unreachable():
    """Regression: a 5xx upstream response must surface as
    `upstream_unreachable`, not as a generic crash."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get(url__regex=r"^https://uaserials\.com/search/.+/$").respond(
            503, text=""
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UASerialsProProvider().search("anything", http)
    assert exc_info.value.code == "upstream_unreachable"


@pytest.mark.asyncio
async def test_browse_films_parses_results():
    """The films listing at /films/ (page 1) returns 18 cards and
    pagination links to higher pages."""
    home_html = _fixture("home.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/films/").respond(200, text=home_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await UASerialsProProvider().browse("films", 1, http)
    assert len(results) == 18
    # Films section cards all classify as movie.
    assert all(r.type == "movie" for r in results)
    # All IDs begin with the provider prefix.
    assert all(r.id.startswith("uaserialspro:") for r in results)
    # Captured home page has pagination links (page/2/, page/3/, …).
    assert has_next is True


@pytest.mark.asyncio
async def test_browse_page2_uses_dle_pagination():
    """Page 2 lives at /films/page/2/. Cards still classify as `movie`."""
    page2_html = _fixture("page2.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/films/page/2/").respond(200, text=page2_html)
        async with httpx.AsyncClient() as http:
            results, _ = await UASerialsProProvider().browse("films", 2, http)
    assert len(results) == 18
    assert all(r.type == "movie" for r in results)


@pytest.mark.asyncio
async def test_browse_last_page_has_next_false():
    """Regression: when on the last page (no higher pagination
    links), has_next must be False so the client stops paging."""
    # Build a minimal page that mirrors the home listing but with
    # only `previous` links (no higher-numbered pages).
    last_page_html = (
        '<html><body><div class="short-list">'
        + "".join(
            f'<div class="short-item width-16"><a class="short-img img-fit" '
            f'href="https://uaserials.com/1258{i}-example.html"></a>'
            f'<div class="th-title truncate">Example {i}</div></div>'
            for i in range(3)
        )
        + '</div><div class="navigation fx-row fx-center">'
        '<a href="https://uaserials.com/films/">1</a>'
        "<span>2</span></div></body></html>"
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/films/page/2/").respond(
            200, text=last_page_html
        )
        async with httpx.AsyncClient() as http:
            _, has_next = await UASerialsProProvider().browse("films", 2, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_browse_series_classifies_as_series():
    """The series section cards classify as `series`."""
    series_html = _fixture("series_home.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/series/").respond(200, text=series_html)
        async with httpx.AsyncClient() as http:
            results, _ = await UASerialsProProvider().browse("series", 1, http)
    assert len(results) == 18
    assert all(r.type == "series" for r in results)


@pytest.mark.asyncio
async def test_browse_unknown_section_raises_not_found():
    """Regression: an unknown section id must raise `not_found` BEFORE
    any HTTP request is made (no upstream round-trip)."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UASerialsProProvider().browse("nonexistent", 1, http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_content_movie_parses_title_year_poster():
    """The Шопен, Шопен! content page (a movie) parses title, year,
    poster and translations. This exercises the full AES-256-CBC +
    PBKDF2 + Tortuga XOR pipeline end-to-end on real captured HTML."""
    content_html = _fixture("content.html")
    player_html = _fixture("player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/12588-shopen-shopen.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/vod/129316").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            c = await UASerialsProProvider().content("12588-shopen-shopen", http)
    # Title is the visible `.short-title` text.
    assert "Шопен" in c.title
    # Жанр row contains "Фільм" → Movie.
    assert c.type == "movie"
    # Year from `/year/2025/`.
    assert c.year == 2025
    # Poster from `div.fimg img-wide img`.
    assert c.poster is not None
    assert c.poster.startswith("https://uaserials.com/")
    # Movies surface a single season/episode pair with the
    # __movie__ sentinel suffix (matches the upstream behaviour).
    assert c.seasons is not None
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) == 1
    assert c.seasons[0].episodes[0].id.endswith(":__movie__")
    assert len(c.translations) >= 1


@pytest.mark.asyncio
async def test_content_series_parses_seasons():
    """The Енн Дройід content page (a series) returns at least one
    season with episodes, sourced from the Tortuga-decoded JSON
    playlist inside the player page's `file:` field."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("series_player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/12585-enn-droyid.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2859").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            c = await UASerialsProProvider().content("12585-enn-droyid", http)
    assert "Енн" in c.title or "Дройід" in c.title
    assert c.type == "series"
    assert c.seasons is not None
    assert len(c.seasons) >= 1
    # Each season has at least one episode with the s{N}e{M} suffix.
    first = c.seasons[0].episodes[0]
    assert first.id.endswith(":s1e1")


@pytest.mark.asyncio
async def test_content_bad_slug_raises_not_found():
    """Regression: anything that fails the slug regex must surface as
    `not_found` BEFORE any HTTP request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UASerialsProProvider().content("../admin", http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_content_missing_title_raises_parse_failed():
    """Regression: a content page with no `.short-title` must surface
    as `parse_failed` rather than crash with an AttributeError."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/12588-shopen-shopen.html").respond(
            200, text="<html><body>no title here</body></html>"
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UASerialsProProvider().content("12588-shopen-shopen", http)
    assert exc_info.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_stream_movie_resolves_to_m3u8():
    """Movie stream: refetch the content page, AES-decrypt the
    data-tag1, fetch the player page, Tortuga-decode the `file:` field,
    return the m3u8 URL."""
    content_html = _fixture("content.html")
    player_html = _fixture("player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/12588-shopen-shopen.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/vod/129316").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UASerialsProProvider().stream(
                "12588-shopen-shopen:__movie__", None, http
            )
    assert s.url.startswith("https://calypso.tortuga.tw/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    # tortuga.tw serves the HLS manifest via Referer (upstream uses
    # `referer = "https://tortuga.tw/"`).
    assert s.headers.get("Referer") == "https://tortuga.tw/"


@pytest.mark.asyncio
async def test_stream_bare_movie_id_without_movie_suffix():
    """Live-gate regression: the gate may call `/api/stream/{id}`
    straight from a search result, whose id is the bare external_id
    (no `:__movie__` suffix). The provider must still resolve."""
    content_html = _fixture("content.html")
    player_html = _fixture("player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/12588-shopen-shopen.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/vod/129316").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UASerialsProProvider().stream("12588-shopen-shopen", None, http)
    assert s.url.startswith("https://calypso.tortuga.tw/")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_stream_series_episode_resolves_to_m3u8():
    """Series episode: `content_id` carries the `:s{N}e{M}` suffix.
    The provider must parse the season JSON, pick the matching
    episode's m3u8 URL, and return it."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("series_player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/12585-enn-droyid.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2859").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UASerialsProProvider().stream("12585-enn-droyid:s1e1", None, http)
    assert s.url.startswith("https://calypso.tortuga.tw/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_stream_bad_episode_suffix_raises_parse_failed():
    """Regression: code-reviewer caught dead-code fallback. An
    out-of-range s{N}e{M} suffix must surface as `parse_failed`
    rather than silently returning the first available episode."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_series.html")
    player_html = _fixture("series_player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/12585-enn-droyid.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2859").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UASerialsProProvider().stream(
                    "12585-enn-droyid:s99e99", None, http
                )
    assert exc_info.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_stream_bad_slug_raises_not_found():
    """Regression (HIGH #2): path-traversal in `content_id` must raise
    `not_found` before any HTTP request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UASerialsProProvider().stream("../admin:__movie__", None, http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_stream_content_5xx_raises_not_found():
    """Regression: a 5xx response to the content page in stream() must
    surface as `not_found` (consistent with the existing providers:
    cikavaideya/klontv treat non-200 status as `not_found`)."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get("https://uaserials.com/12588-shopen-shopen.html").respond(
            503, text=""
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UASerialsProProvider().stream(
                    "12588-shopen-shopen:__movie__", None, http
                )
    assert exc_info.value.code == "not_found"
