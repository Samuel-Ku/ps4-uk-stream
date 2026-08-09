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
    assert first.id.startswith("uaflix:serials-djuna-proroctvo:s1e")


@pytest.mark.asyncio
async def test_uaflix_content_series_drops_empty_season():
    """Regression (observed live 2026-08-09): the upstream content page
    renders only the LATEST seasons' episode tiles inline — older
    seasons' links point at a separate `/sezon-N/` page and have no
    episode ids on this page. An empty season in the response is
    unplayable (a client picking seasons[0] lands on the main page,
    finds no player iframe and 502s), so it must be dropped."""
    content_html = (
        "<html><body>"
        "<h1 id='ftitle'>Тестовий серіал</h1>"
        "<div class='fusers all-sez'><div class='sez-wr'>"
        "<a href='/serials/test-serial/sezon-1/' class='sect-link'>Сезон 1</a>"
        "<a href='/serials/test-serial/sezon-2/' class='sect-link'>Сезон 2</a>"
        "</div></div>"
        "<div class='frels2'><div class='sers-wr'>"
        "<div class='video-item with-mask'><div class='vi-in'>"
        "<a class='vi-img img-resp-h' href='https://uafix.net/serials/test-serial/season-02-episode-01/'>"
        "<div class='vi-title'>Сезон 2 Серія 1</div>"
        "</a></div></div>"
        "</div></div>"
        "</body></html>"
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uafix.net/serials/test-serial/").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await UAFlixProvider().content("serials-test-serial", http)
    assert c.type == "series"
    assert c.seasons is not None
    # Season 1 (no inline episodes) is dropped; only season 2 survives.
    assert [s.number for s in c.seasons] == [2]
    assert len(c.seasons[0].episodes) == 1
    assert c.seasons[0].episodes[0].id == "uaflix:serials-test-serial:s2e1"


@pytest.mark.asyncio
async def test_uaflix_content_serial_without_links_probes_player():
    """Issue #189: a serial whose content page has NO season/episode
    links (observed live on «Вайлд Пак» — episodes live only inside
    the zetvideo serial player's JSON-folder payload) must surface
    playable season/episode ids by probing the player iframe, so the
    card is not left with an empty seasons list."""
    content_html = _fixture("content_serial_zetvideo.html")
    player_html = _fixture("player_serial_zetvideo.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uafix.net/serials/vajld-pak/").respond(
            200, text=content_html
        )
        router.get("https://zetvideo.net/serial/2258").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await UAFlixProvider().content("serials-vajld-pak", http)
    assert c.type == "series"
    assert c.seasons is not None
    assert len(c.seasons) >= 1
    first = c.seasons[0].episodes[0]
    # Episode ids match what stream()'s `_serial_media_url` indexes.
    assert first.id == "uaflix:serials-vajld-pak:s1e1"


@pytest.mark.asyncio
async def test_uaflix_content_trailer_only_raises_gated():
    """Issue #189: a content page whose only player is a YouTube
    embed (observed live on «КоКомелон у кіно») has no playable
    source — content() must raise `gated` (ADR-0002) so the catalog
    sweep drops the dead card from home/search."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_serial_zetvideo.html").replace(
        'src="https://zetvideo.net/serial/2258',
        'src="https://www.youtube.com/embed/cr_s2OZyUrc',
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://uafix.net/serials/vajld-pak/").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await UAFlixProvider().content("serials-vajld-pak", http)
    assert exc.value.code == "gated"


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


def test_uaflix_allowlist_includes_ashdi():
    """Issue #183: ashdi.vip serial players must be fetchable through the
    shared SSRF-safe `safe_get` — a series whose episode embeds an ashdi
    serial page must not 502 with `disallowed host`."""
    from cs_uk_api.providers.uaflix import _ALLOWED_HOSTS

    assert "ashdi.vip" in _ALLOWED_HOSTS


@pytest.mark.asyncio
async def test_uaflix_stream_serial_ashdi_embed_allowed():
    """Issue #183 + #184: an episode whose content page embeds an
    ashdi.vip serial player (3-level JSON-folder `file:`) resolves its
    m3u8 without a disallowed-host error."""
    content_html = _fixture("content_serial_ashdi.html")
    player_html = _fixture("player_serial_ashdi.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://uafix.net/serials/nevlovimij-samuraj/season-01-episode-01/"
        ).respond(200, text=content_html)
        router.get(
            "https://ashdi.vip/serial/3863?season=1&episode=1"
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UAFlixProvider().stream(
                "serials-nevlovimij-samuraj:s1e1", None, http
            )
    assert s.url.startswith(
        "https://ashdi.vip/video09/2/serials/kaizoku_nevlovimij_samuraj/"
    )
    assert s.url.endswith("index.m3u8")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_uaflix_stream_serial_zetvideo_json_folder_extracts_m3u8():
    """Issue #184: a zetvideo serial player whose `file:` is a JSON-folder
    string (seasons -> episodes) must resolve the episode m3u8 from the
    `s1e1` suffix instead of failing with `parse_failed`."""
    content_html = _fixture("content_serial_zetvideo.html")
    player_html = _fixture("player_serial_zetvideo.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://uafix.net/serials/vajld-pak/season-01-episode-01/"
        ).respond(200, text=content_html)
        router.get(
            "https://zetvideo.net/serial/2258?season=1&episode=1"
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UAFlixProvider().stream(
                "serials-vajld-pak:s1e1", None, http
            )
    assert s.url == (
        "https://zetvideo.net/vid/1/serials/"
        "wylde.pak.s01e01.best.summer.ever.1080p.webdl_59942/hls/index.m3u8"
    )
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_uaflix_stream_serial_falls_back_to_show_page_on_episode_404():
    """Issue #189: serial-player-only titles (e.g. «Вайлд Пак») have no
    per-episode pages — the episode URL 404s but the show page embeds
    the same serial player. stream() must fall back to the show page
    and still resolve the `s<N>e<M>` m3u8 from the JSON-folder."""
    content_html = _fixture("content_serial_zetvideo.html")
    player_html = _fixture("player_serial_zetvideo.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://uafix.net/serials/vajld-pak/season-01-episode-01/"
        ).respond(404, text="not found")
        router.get("https://uafix.net/serials/vajld-pak/").respond(
            200, text=content_html
        )
        router.get(
            "https://zetvideo.net/serial/2258?season=1&episode=1"
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UAFlixProvider().stream(
                "serials-vajld-pak:s1e1", None, http
            )
    assert s.url == (
        "https://zetvideo.net/vid/1/serials/"
        "wylde.pak.s01e01.best.summer.ever.1080p.webdl_59942/hls/index.m3u8"
    )
    assert s.type == "m3u8"


def test_uaflix_serial_json_folder_indexes_season_episode():
    """Issue #184: the `s<N>e<M>` suffix must index season N / episode M
    into the JSON-folder tree exactly like the eneyida reference."""
    from cs_uk_api.providers.uaflix import _serial_media_url

    player_html = _fixture("player_serial_zetvideo.html")
    assert _serial_media_url(player_html, "s1e1") == (
        "https://zetvideo.net/vid/1/serials/"
        "wylde.pak.s01e01.best.summer.ever.1080p.webdl_59942/hls/index.m3u8"
    )
    assert _serial_media_url(player_html, "s1e2") == (
        "https://zetvideo.net/vid/1/serials/"
        "wylde.pak.s01e02.best.summer.ever.1080p.webdl_59942/hls/index.m3u8"
    )
    assert _serial_media_url(player_html, "s2e1") == (
        "https://zetvideo.net/vid/1/serials/"
        "wylde.pak.s02e01.best.summer.ever.1080p.webdl_59942/hls/index.m3u8"
    )
    assert _serial_media_url(player_html, "s3e1") is None
    assert _serial_media_url(player_html, "") is None


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


def test_uaflix_episode_suffix_rejects_garbage():
    """Regression (issue #122): `s1e2garbage` must not be treated as
    `s1e2` — the suffix regex must fullmatch."""
    from cs_uk_api.providers.base import ProviderError
    from cs_uk_api.providers.uaflix import _episode_content_url

    with pytest.raises(ProviderError) as exc_info:
        _episode_content_url("serials-djuna-proroctvo", "s1e2garbage")
    assert exc_info.value.code == "parse_failed"
    # And the valid suffix still builds the canonical URL.
    rebuilt = _episode_content_url("serials-djuna-proroctvo", "s1e2")
    assert rebuilt.endswith("/season-01-episode-02/")


@pytest.mark.asyncio
async def test_uaflix_content_bad_external_id_raises_not_found():
    """Regression (issue #122): a path-traversal external_id must
    surface as `not_found` before any HTTP request."""
    from cs_uk_api.providers.base import ProviderError

    for bad in ("../admin", "serials-../admin", ""):
        with respx.mock(assert_all_called=False):
            async with httpx.AsyncClient() as http:
                with pytest.raises(ProviderError) as exc_info:
                    await UAFlixProvider().content(bad, http)
        assert exc_info.value.code == "not_found", f"unexpected: {bad!r}"


@pytest.mark.asyncio
async def test_uaflix_stream_bad_external_id_raises_not_found():
    """Same boundary as content(): a path-traversal content_id must
    raise `not_found` before any HTTP request."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UAFlixProvider().stream("serials-../admin", None, http)
    assert exc_info.value.code == "not_found"
