"""Tests for the Anitubeinua provider (https://anitube.in.ua).

Issue #17, Group 3. Ukrainian-dubbed and subbed anime catalogue. The
upstream uses a DLE animeCMS with a `/anime/page/N/` listing,
`<article class="story">` cards, and an AJAX-loaded playlist under
`<div class="playlists-ajax" data-xfname="playlist" data-news_id="...">`.

The stream pipeline is content -> AJAX playlist -> iframe hop (ashdi.vip
or moonanime.art) -> m3u8. The player URLs depend on the category tree
(SUBTITLES / DUB) and the studio id, so episode resolution must
explicitly pick a cat_id/studio_id pair.
"""

from __future__ import annotations

import json
import pathlib
import re

import httpx
import pytest
import respx

from cs_uk_api.providers.anitubeinua import AnitubeinuaProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "anitubeinua"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_anitubeinua_provider_metadata():
    """The provider exposes a stable id/name and at least one section."""
    p = AnitubeinuaProvider()
    assert p.id == "anitubeinua"
    assert p.name == "Anitubeinua"
    assert "anime" in p.types
    ids = [s.id for s in p.sections]
    assert "page" in ids


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_results():
    """Search posts to the site root with do/subaction/story fields and
    yields article.story cards. The captured fixture for "військов" has
    10 distinct story articles."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://anitube.in.ua/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await AnitubeinuaProvider().search("військов", http)
    assert len(results) >= 1
    assert all(r.provider == "anitubeinua" for r in results)
    titles = [r.title for r in results]
    assert any("Військов" in t for t in titles)


@pytest.mark.asyncio
async def test_search_classifies_as_anime():
    """Every result is an anime entry — the upstream supports
    TvType.Anime and TvType.AnimeMovie only, so we map both to `anime`."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://anitube.in.ua/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await AnitubeinuaProvider().search("військов", http)
    assert all("anime" in r.styles for r in results)


@pytest.mark.asyncio
async def test_search_extracts_external_id_from_url():
    """The external_id is the `<id>-<slug>` token from the URL path
    (e.g. `5981-vyskova-storya-malenkoyi-dvchinki-2-sezon`)."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://anitube.in.ua/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await AnitubeinuaProvider().search("військов", http)
    ids = [r.id for r in results]
    assert "anitubeinua:5981-vyskova-storya-malenkoyi-dvchinki-2-sezon" in ids
    assert "anitubeinua:2751-vyskova-storya-malenkoyi-dvchinki" in ids


@pytest.mark.asyncio
async def test_search_5xx_raises_upstream_unreachable():
    """A 5xx search response must surface as `upstream_unreachable`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.post("https://anitube.in.ua/").respond(503, text="")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().search("anything", http)
    assert exc.value.code == "upstream_unreachable"


@pytest.mark.asyncio
async def test_search_connection_error_raises_unreachable():
    """A network error must surface as `unreachable`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False) as router:
        router.post("https://anitube.in.ua/").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().search("anything", http)
    assert exc.value.code == "unreachable"


# ---------------------------------------------------------------------------
# browse()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_page_returns_results():
    """The `/anime/page/1/` listing is the only section; page 1 returns
    11 story cards."""
    page_html = _fixture("page1.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://anitube.in.ua/anime/page/1/").respond(200, text=page_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await AnitubeinuaProvider().browse("page", 1, http)
    assert len(results) >= 1
    assert all("anime" in r.styles for r in results)
    # DLE pagination has a page/2 link to trigger `has_next`.
    assert has_next is True


@pytest.mark.asyncio
async def test_browse_page2_returns_results():
    """Page 2 is the same listing shape as page 1 with the same cards."""
    page2_html = _fixture("page2.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://anitube.in.ua/anime/page/2/").respond(200, text=page2_html)
        async with httpx.AsyncClient() as http:
            results, _ = await AnitubeinuaProvider().browse("page", 2, http)
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_browse_unknown_section_raises_not_found():
    """An unknown section must surface as `not_found` before any HTTP
    request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().browse("nonexistent", 1, http)
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------------
# content()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_parses_title_poster_year_description():
    """The content page exposes the title as `.story_c h2`, the poster
    as `.story_c_left span.story_post img`, the year as the
    `/xfsearch/year/` link, and the description in `.my-text`."""
    content_html = _fixture("content.html")
    playlist_json = _fixture("playlist.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://anitube.in.ua/5981-vyskova-storya-malenkoyi-dvchinki-2-sezon.html"
        ).respond(200, text=content_html)
        router.get(url=re.compile(r"https://anitube\.in\.ua/engine/ajax/.*")).respond(
            200, json=json.loads(playlist_json)
        )
        async with httpx.AsyncClient() as http:
            c = await AnitubeinuaProvider().content(
                "5981-vyskova-storya-malenkoyi-dvchinki-2-sezon", http
            )
    assert "Військов" in c.title
    assert "anime" in c.styles
    assert c.year == 2026
    assert c.poster is not None
    assert c.poster.startswith("https://anitube.in.ua/uploads/")
    assert "Таня" in c.description


@pytest.mark.asyncio
async def test_content_resolves_episode_level_translations():
    """The playlist AJAX exposes two categories (СУБТИТРИ / ОЗВУЧЕННЯ)
    and several studios — content() must surface them as per-episode
    translations with `translations_level == "episode"`."""
    content_html = _fixture("content.html")
    playlist_json = _fixture("playlist.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://anitube.in.ua/5981-vyskova-storya-malenkoyi-dvchinki-2-sezon.html"
        ).respond(200, text=content_html)
        router.get(url=re.compile(r"https://anitube\.in\.ua/engine/ajax/.*")).respond(
            200, json=json.loads(playlist_json)
        )
        async with httpx.AsyncClient() as http:
            c = await AnitubeinuaProvider().content(
                "5981-vyskova-storya-malenkoyi-dvchinki-2-sezon", http
            )
    assert c.translations_level == "episode"
    assert c.seasons is not None
    # Two seasons: SUB (0_0) and DUB (0_1).
    assert len(c.seasons) == 2
    for season in c.seasons:
        assert len(season.episodes) >= 1
        first = season.episodes[0]
        assert first.translations is not None
        assert len(first.translations) >= 1


@pytest.mark.asyncio
async def test_content_playlist_collapsed_layout_still_parses():
    """Regression (issue #118): the live site briefly served only two
    `.playlists-items` blocks. The parser must classify `<li>` rows by
    their `data-id` depth, not by block position, so a collapsed layout
    still yields episodes instead of silently returning none."""
    from cs_uk_api.providers.anitubeinua import _parse_playlist

    collapsed = """
    <div class="playlists-items">
      <li data-id="0_0">СУБТИТРИ</li>
      <li data-id="0_1">ОЗВУЧЕННЯ</li>
    </div>
    <div class="playlists-items">
      <li data-id="0_0_0_0" data-file="https://ashdi.vip/vod/1">1 серія</li>
      <li data-id="0_0_0_0" data-file="https://ashdi.vip/vod/2">2 серія</li>
      <li data-id="0_1_0_0" data-file="https://moonanime.art/iframe/xyz">1 серія</li>
    </div>
    """
    playlist = _parse_playlist(collapsed)
    assert set(playlist["categories"]) == {"0_0", "0_1"}
    assert len(playlist["episodes"]) == 3
    assert playlist["episodes"][0].file_url == "https://ashdi.vip/vod/1"
    assert playlist["episodes"][0].title == "1 серія"


@pytest.mark.asyncio
async def test_content_playlist_unreachable_propagates():
    """Regression (issue #118, D5): a network failure on the AJAX
    playlist must propagate as `unreachable` so the health tracker sees
    the provider as down — not be swallowed into an empty-season 200."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://anitube.in.ua/5981-vyskova-storya-malenkoyi-dvchinki-2-sezon.html"
        ).respond(200, text=content_html)
        # AJAX endpoint unreachable (connection refused).
        router.get(url=re.compile(r"https://anitube\.in\.ua/engine/ajax/.*")).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().content(
                    "5981-vyskova-storya-malenkoyi-dvchinki-2-sezon", http
                )
    assert exc.value.code == "unreachable"


@pytest.mark.asyncio
async def test_content_bad_slug_raises_not_found():
    """Defensive: slug regex must reject path traversal before any HTTP
    request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().content("../admin", http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_content_non_200_raises_not_found():
    """A 404 from the upstream must surface as `not_found`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://anitube.in.ua/5981-vyskova-storya-malenkoyi-dvchinki-2-sezon.html"
        ).respond(404, text="")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().content(
                    "5981-vyskova-storya-malenkoyi-dvchinki-2-sezon", http
                )
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_content_missing_title_raises_parse_failed():
    """A content page with no `.story_c h2` must surface as `parse_failed`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://anitube.in.ua/5981-vyskova-storya-malenkoyi-dvchinki-2-sezon.html"
        ).respond(200, text="<html><body>no title</body></html>")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().content(
                    "5981-vyskova-storya-malenkoyi-dvchinki-2-sezon", http
                )
    assert exc.value.code == "parse_failed"


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_resolves_ashdi_episode_to_m3u8():
    """Stream pipeline: content -> AJAX playlist -> ashdi.vip iframe ->
    m3u8 URL with the upstream Referer. Season 2 (DUB) episode 1 with
    studio "FanVoxUA" must resolve to the ashdi.vip player (preferred
    over moonanime)."""
    content_html = _fixture("content.html")
    playlist_json = _fixture("playlist.json")
    player_html = _fixture("player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://anitube.in.ua/5981-vyskova-storya-malenkoyi-dvchinki-2-sezon.html"
        ).respond(200, text=content_html)
        router.get(url=re.compile(r"https://anitube\.in\.ua/engine/ajax/.*")).respond(
            200, json=json.loads(playlist_json)
        )
        router.get(url=re.compile(r"https://ashdi\.vip/.*")).respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            s = await AnitubeinuaProvider().stream(
                "5981-vyskova-storya-malenkoyi-dvchinki-2-sezon:s2e1",
                "FanVoxUA",
                http,
            )
    assert s.url.startswith("https://ashdi.vip/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    # ashdi.vip requires the upstream Referer to serve the manifest.
    assert s.headers.get("Referer") == "https://qeruya.cyou"


@pytest.mark.asyncio
async def test_stream_unknown_translation_raises_parse_failed():
    """When the requested translation is not in the playlist, we must
    surface as `parse_failed` rather than silently pick the first
    available episode."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content.html")
    playlist_json = _fixture("playlist.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://anitube.in.ua/5981-vyskova-storya-malenkoyi-dvchinki-2-sezon.html"
        ).respond(200, text=content_html)
        router.get(url=re.compile(r"https://anitube\.in\.ua/engine/ajax/.*")).respond(
            200, json=json.loads(playlist_json)
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().stream(
                    "5981-vyskova-storya-malenkoyi-dvchinki-2-sezon:s2e1",
                    "NonExistentStudio",
                    http,
                )
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_stream_garbage_episode_suffix_raises_parse_failed():
    """Regression (issue #122): `s1e2garbage` must be rejected, not
    silently treated as `s1e2`. The suffix regex must fullmatch."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().stream(
                    "5820-koli-ya-pererodivsya-slizom-4-sezon:s1e2garbage", None, http
                )
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_stream_bad_external_id_raises_not_found():
    """Defensive: the stream boundary must reject path traversal."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().stream("../admin:s2e1", "FanVoxUA", http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_stream_malformed_episode_suffix_raises_parse_failed():
    """An episode suffix that doesn't fit the expected `s<N>e<M>` shape
    must surface as `parse_failed`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().stream(
                    "5981-vyskova-storya-malenkoyi-dvchinki-2-sezon:bogus",
                    None,
                    http,
                )
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_stream_ashdi_5xx_raises_not_found():
    """If the ashdi player page returns 5xx, surface as `not_found`."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content.html")
    playlist_json = _fixture("playlist.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://anitube.in.ua/5981-vyskova-storya-malenkoyi-dvchinki-2-sezon.html"
        ).respond(200, text=content_html)
        router.get(url=re.compile(r"https://anitube\.in\.ua/engine/ajax/.*")).respond(
            200, json=json.loads(playlist_json)
        )
        router.get(url=re.compile(r"https://ashdi\.vip/.*")).respond(
            503, text="upstream down"
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnitubeinuaProvider().stream(
                    "5981-vyskova-storya-malenkoyi-dvchinki-2-sezon:s2e1",
                    "FanVoxUA",
                    http,
                )
    assert exc.value.code == "not_found"
