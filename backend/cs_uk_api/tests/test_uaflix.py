"""Tests for the UAFlix provider (issue #17, Group 1)."""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.uaflix import UAFlixProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "uaflix"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# Section id -> (URL path the provider will hit, listing fixture filename).
# 5 of 6 sections use the natural root path; `multserialy` aliases to the
# site-internal /serials/multseial/ because the upstream Kotlin's
# /multserialy/ returns 404 on the live mirror.
_SECTIONS = [
    ("filmy", "https://uafix.net/films/", "films_listing.html"),
    ("serialy", "https://uafix.net/serials/", "serials_listing.html"),
    ("doramy", "https://uafix.net/dorama/", "dorama_listing.html"),
    ("cartoons", "https://uafix.net/cartoons/", "cartoons_listing.html"),
    ("multserialy", "https://uafix.net/serials/multseial/", "serials_listing.html"),
    ("anime", "https://uafix.net/anime/", "anime_listing.html"),
]


@pytest.mark.asyncio
async def test_uaflix_search_parses_results():
    """Search fixture contains 5 result cards (`<a class="sres-wrap">`)."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://uafix.net/index.php").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await UAFlixProvider().search("дюна", http)
    assert len(results) == 5
    assert all(r.provider == "uaflix" for r in results)
    titles = [r.title for r in results]
    # The fixture contains "Дюна / Dune" and "Дюна. Пророцтво".
    assert any("Дюна" in t for t in titles)


@pytest.mark.asyncio
async def test_uaflix_search_classifies_types_by_url_path():
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://uafix.net/index.php").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await UAFlixProvider().search("дюна", http)
    # URL's path segment determines the type: /films/ -> movie,
    # /serials/ -> series. The fixture mixes both.
    types_by_path = {r.url.split("/")[3]: r.type for r in results}
    assert types_by_path.get("films") == "movie"
    assert types_by_path.get("serials") == "series"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section_id,url,fixture_name",
    _SECTIONS,
    ids=[s[0] for s in _SECTIONS],
)
async def test_uaflix_browse_section_page1(section_id, url, fixture_name):
    listing_html = _fixture(fixture_name)
    with respx.mock(assert_all_called=True) as router:
        router.get(url).respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await UAFlixProvider().browse(section_id, 1, http)
    # All 5 listing fixtures capture exactly 12 cards on page 1.
    assert len(results) == 12
    assert has_next is True
    # Every result must be tagged with this provider.
    assert all(r.provider == "uaflix" for r in results)
    # The external_id encodes the URL's first path segment + slug.
    assert all(r.id.startswith(f"uaflix:") for r in results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section_id,url,fixture_name",
    _SECTIONS,
    ids=[s[0] for s in _SECTIONS],
)
async def test_uaflix_browse_section_last_page_has_next_false(
    section_id, url, fixture_name
):
    """When we request a page beyond the highest pagination link the
    live site lists, has_next must be False so the client stops
    paging. The captured fixtures cap out at page 66-1054; we ask for
    page 9999 so every fixture's max link is below it."""
    listing_html = _fixture(fixture_name)
    with respx.mock(assert_all_called=True) as router:
        router.get(url + "page/9999/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            _, has_next = await UAFlixProvider().browse(section_id, 9999, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_uaflix_content_movie_parses_title_poster_player():
    content_html = _fixture("content_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uafix.net/films/djuna-1984/").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await UAFlixProvider().content("films-djuna-1984", http)
    assert "Дюна" in c.title
    assert c.type == "movie"
    assert c.poster is not None
    assert c.poster.startswith("https://uafix.net/uploads/")
    # Movies have no seasons.
    assert c.seasons is None
    # Player URL is found (extracted in the iframe).
    # The Provider.content() contract does not surface player_url
    # directly, but stream() will use it — see the stream test below.


@pytest.mark.asyncio
async def test_uaflix_content_series_parses_seasons():
    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uafix.net/serials/djuna-proroctvo/").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await UAFlixProvider().content("serials-djuna-proroctvo", http)
    assert c.type == "series"
    assert "Пророцтво" in c.title
    assert c.seasons is not None
    assert len(c.seasons) >= 1
    # At least one episode in the first season.
    assert len(c.seasons[0].episodes) >= 1
    # Episode id encodes the (season, episode) position.
    first = c.seasons[0].episodes[0]
    assert first.number == 1
    assert first.id.startswith("serials-djuna-proroctvo:s1e")


@pytest.mark.asyncio
async def test_uaflix_stream_movie_resolves_to_media_url():
    """Two-hop: content page -> player iframe -> m3u8 on zetvideo.net."""
    content_html = _fixture("content_movie.html")
    player_html = _fixture("player_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uafix.net/films/djuna-1984/").respond(
            200, text=content_html
        )
        router.get(
            "https://zetvideo.net/vod/22992",
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UAFlixProvider().stream("films-djuna-1984", None, http)
    assert s.url.startswith(
        "https://zetvideo.net/vid/1/films/dune_1984_theatrical_remastered_bdremux_1080p_hurtom_22992/"
    )
    assert s.url.endswith("index.m3u8")
    assert s.type == "m3u8"
    assert "Referer" in s.headers


@pytest.mark.asyncio
async def test_uaflix_stream_series_resolves_episode_m3u8():
    content_html = _fixture("episode.html")
    player_html = _fixture("player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://uafix.net/serials/djuna-proroctvo/season-01-episode-01/"
        ).respond(200, text=content_html)
        router.get(
            "https://zetvideo.net/vod/9109",
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UAFlixProvider().stream(
                "serials-djuna-proroctvo:s1e1", None, http
            )
    assert s.url.startswith(
        "https://zetvideo.net/vid/1/serials/dune__prophecy_2024_s01e01__the_hidden_hand_9109/"
    )
    assert s.url.endswith("index.m3u8")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_uaflix_sections_lists_six():
    sections = UAFlixProvider().sections
    ids = [s.id for s in sections]
    assert ids == ["filmy", "serialy", "doramy", "cartoons", "multserialy", "anime"]


@pytest.mark.asyncio
async def test_uaflix_browse_unknown_section_raises():
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await UAFlixProvider().browse("nonexistent", 1, httpx.AsyncClient())


def test_uaflix_external_id_round_trips_to_url():
    """Regression: `_external_id_from_url` must produce an id that
    `_content_url` can turn back into the same URL for movie and
    series-root URLs. Episode URLs are encoded with a separate
    `_episode_content_url` + `s<N>e<M>` suffix and are covered by the
    series stream test."""
    from cs_uk_api.providers.uaflix import (
        _content_url,
        _episode_content_url,
        _external_id_from_url,
    )

    for href in (
        "https://uafix.net/films/djuna-1984/",
        "https://uafix.net/serials/djuna-proroctvo/",
    ):
        ext_id = _external_id_from_url(href)
        rebuilt = _content_url(ext_id)
        assert rebuilt.rstrip("/") == href.rstrip("/"), (ext_id, rebuilt, href)
    # Episode URL round-trips through the dedicated helper.
    href = "https://uafix.net/serials/djuna-proroctvo/season-01-episode-01/"
    ext_id = _external_id_from_url(href)
    rebuilt = _episode_content_url(ext_id, "s1e1")
    assert rebuilt.rstrip("/") == href.rstrip("/"), (ext_id, rebuilt, href)


@pytest.mark.asyncio
async def test_uaflix_content_bad_external_id_raises_not_found():
    """REGRESSION: `content()` must validate the external_id slug
    before building the URL. A slug containing `/` or `..` would
    otherwise turn into a path-traversal request that
    `follow_redirects=True` (in `stream()`) could steer into a
    hostile host."""
    from cs_uk_api.providers.base import ProviderError

    bad_ids = (
        "../../etc/passwd",
        "films-../../etc/passwd",
        "-bad",
        "films-",
        "evil-",
        "filmy-djuna-1984",  # section id (filmy), not URL path (films)
    )
    with respx.mock(assert_all_called=False):
        for bad in bad_ids:
            with pytest.raises(ProviderError) as exc:
                await UAFlixProvider().content(bad, httpx.AsyncClient())
            assert exc.value.code == "not_found", (bad, exc.value.code)


@pytest.mark.asyncio
async def test_uaflix_stream_bad_content_id_raises_not_found():
    """REGRESSION: `stream()` must validate the external_id portion
    of the content_id before following any redirect. Same SSRF
    surface as the content test, exercised through the stream path."""
    from cs_uk_api.providers.base import ProviderError

    bad_ids = (
        "../../etc/passwd:bad",
        "films-../etc/passwd",
        "serialy-djuna:bad",
        "evil-bad:s1e1",
    )
    with respx.mock(assert_all_called=False):
        for bad in bad_ids:
            with pytest.raises(ProviderError) as exc:
                await UAFlixProvider().stream(bad, None, httpx.AsyncClient())
            assert exc.value.code == "not_found", (bad, exc.value.code)
