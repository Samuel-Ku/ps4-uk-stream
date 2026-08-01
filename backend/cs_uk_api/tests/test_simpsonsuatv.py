"""Tests for the SimpsonsUATv provider (https://simpsonsua.tv).

Issue #17, Group 3. Ukrainian-dubbed cartoon archive. The site is a
DLE-style CMS with a `div.ep_slider` block on the home page that lists
the latest 15 episode updates, a `/multserialy-ukrainskoyu/` listing of
shows, and a search endpoint at `/?s=QUERY`. The show page has nested
season links; the season page has nested episode links; the episode
page exposes a single `<iframe data-player="..." src="...">` pointing
at ashdi.vip. The player page embeds a plain `file: '...m3u8'`.

External-id shape: a slug matching `[a-z0-9][a-z0-9-]+` that names
either a show (e.g. `simpsony`), a season (e.g. `s35`, `sezon-1`),
or a specific episode (e.g. `4441-37-sezon-17-seriya`). The
provider validates it at both `content()` and `stream()` boundaries
to refuse path traversal.
"""

from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.simpsonsuatv import SimpsonsUATvProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "simpsonsuatv"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_simpsonsuatv_provider_metadata():
    """The provider exposes a stable id/name and at least one section."""
    p = SimpsonsUATvProvider()
    assert p.id == "simpsonsuatv"
    assert p.name == "SimpsonsUA"
    assert "cartoon" in p.types
    assert "series" in p.types
    ids = [s.id for s in p.sections]
    assert "page" in ids


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_results():
    """Search hits the home page with `?s=QUERY` and yields `div.movie_item`
    cards. The captured fixture for «сімпсони» has at least one show."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://simpsonsua.tv/", params={"s": "сімпсони"}).respond(
            200, text=search_html
        )
        async with httpx.AsyncClient() as http:
            results = await SimpsonsUATvProvider().search("сімпсони", http)
    assert len(results) >= 1
    assert all(r.provider == "simpsonsuatv" for r in results)


@pytest.mark.asyncio
async def test_search_uses_titlemap_for_known_shows():
    """The captured fixture has the show URL `/simpsony/`, which must be
    resolved to the titleMap label `Сімпсони` (matches the upstream
    Kotlin titleMap)."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://simpsonsua.tv/", params={"s": "сімпсони"}).respond(
            200, text=search_html
        )
        async with httpx.AsyncClient() as http:
            results = await SimpsonsUATvProvider().search("сімпсони", http)
    titles = [r.title for r in results]
    assert "Сімпсони" in titles


@pytest.mark.asyncio
async def test_search_5xx_raises_upstream_unreachable():
    """A 5xx search response must surface as `upstream_unreachable`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get("https://simpsonsua.tv/").respond(503, text="")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await SimpsonsUATvProvider().search("anything", http)
    assert exc.value.code == "upstream_unreachable"


@pytest.mark.asyncio
async def test_search_connection_error_raises_unreachable():
    """A network error must surface as `unreachable`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False) as router:
        router.get("https://simpsonsua.tv/").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await SimpsonsUATvProvider().search("anything", http)
    assert exc.value.code == "unreachable"


# ---------------------------------------------------------------------------
# browse()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_page1_returns_shows():
    """The /multserialy-ukrainskoyu/ page 1 has 35 `div.movie_item` cards."""
    page_html = _fixture("page1.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://simpsonsua.tv/multserialy-ukrainskoyu/").respond(
            200, text=page_html
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await SimpsonsUATvProvider().browse("page", 1, http)
    assert len(results) >= 1
    assert all(r.provider == "simpsonsuatv" for r in results)
    # DLE pagination: a /page/2/ link means there is a next page.
    assert has_next is True


@pytest.mark.asyncio
async def test_browse_page2_returns_shows():
    """Page 2 of the multserialy listing is the same shape as page 1."""
    page2_html = _fixture("page2.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://simpsonsua.tv/multserialy-ukrainskoyu/page/2/").respond(
            200, text=page2_html
        )
        async with httpx.AsyncClient() as http:
            results, _ = await SimpsonsUATvProvider().browse("page", 2, http)
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_browse_unknown_section_raises_not_found():
    """An unknown section must surface as `not_found` before any HTTP
    request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await SimpsonsUATvProvider().browse("nonexistent", 1, http)
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------------
# content()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_for_show_follows_to_seasons_and_episodes():
    """For a show external_id, content() must fetch the show page,
    follow the season subitems, and return at least one Season with
    episodes. Each episode's `id` is the full URL of the episode
    page (so stream() can fetch it directly)."""
    show_html = _fixture("content_show.html")
    season_html = _fixture("content_season.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://simpsonsua.tv/simpsony/").respond(200, text=show_html)
        # The provider will follow every season subitem on the show
        # page; we route them all to the same season fixture.
        router.get(url__regex=r"https://simpsonsua\.tv/.*").respond(
            200, text=season_html
        )
        async with httpx.AsyncClient() as http:
            c = await SimpsonsUATvProvider().content("simpsony", http)
    assert "Сімпсони" in c.title or "simpsony" in c.id
    assert c.type in ("cartoon", "series")
    assert c.seasons is not None
    assert len(c.seasons) >= 1
    # At least one season must have at least one episode.
    assert any(len(s.episodes) >= 1 for s in c.seasons)
    # Episode ids must be the full URL of the episode page.
    all_ids = [e.id for s in c.seasons for e in s.episodes]
    assert all(
        i.startswith("https://simpsonsua.tv/") and "-seriya" in i for i in all_ids
    )


@pytest.mark.asyncio
async def test_content_for_season_returns_episodes():
    """For a season external_id, content() must fetch the season page
    directly and return one Season with the episode cards."""
    season_html = _fixture("content_season.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://simpsonsua.tv/s35/").respond(200, text=season_html)
        async with httpx.AsyncClient() as http:
            c = await SimpsonsUATvProvider().content("s35", http)
    assert c.seasons is not None
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) >= 1
    all_ids = [e.id for e in c.seasons[0].episodes]
    assert all(
        i.startswith("https://simpsonsua.tv/") and "-seriya" in i for i in all_ids
    )


@pytest.mark.asyncio
async def test_content_bad_slug_raises_not_found():
    """Defensive: slug regex must reject path traversal before any HTTP
    request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await SimpsonsUATvProvider().content("../admin", http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_content_non_200_raises_not_found():
    """A 404 from the upstream must surface as `not_found`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get("https://simpsonsua.tv/simpsony/").respond(404, text="")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await SimpsonsUATvProvider().content("simpsony", http)
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_resolves_episode_to_m3u8():
    """Stream pipeline: episode page -> ashdi.vip iframe -> m3u8 URL
    with the upstream Referer. The episode URL is the full URL of the
    episode page (e.g. `https://simpsonsua.tv/s37/4441-37-sezon-17-seriya.html`)."""
    content_html = _fixture("content.html")
    player_html = _fixture("player.html")
    episode_url = "https://simpsonsua.tv/s37/4441-37-sezon-17-seriya.html"
    with respx.mock(assert_all_called=True) as router:
        router.get(episode_url).respond(200, text=content_html)
        router.get("https://ashdi.vip/vod/267925").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await SimpsonsUATvProvider().stream(episode_url, None, http)
    assert s.url.startswith("https://ashdi.vip/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    # ashdi.vip requires a Referer; the upstream Kotlin uses the
    # site root.
    assert s.headers.get("Referer") == "https://simpsonsua.tv/"


@pytest.mark.asyncio
async def test_stream_no_iframe_raises_parse_failed():
    """A content page with no `<iframe>` must surface as `parse_failed`."""
    from cs_uk_api.providers.base import ProviderError

    episode_url = "https://simpsonsua.tv/s37/4441-37-sezon-17-seriya.html"
    with respx.mock(assert_all_called=True) as router:
        router.get(episode_url).respond(200, text="<html><body>no iframe</body></html>")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await SimpsonsUATvProvider().stream(episode_url, None, http)
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_stream_ashdi_5xx_raises_not_found():
    """If the ashdi player page returns 5xx, surface as `not_found`."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content.html")
    episode_url = "https://simpsonsua.tv/s37/4441-37-sezon-17-seriya.html"
    with respx.mock(assert_all_called=True) as router:
        router.get(episode_url).respond(200, text=content_html)
        router.get("https://ashdi.vip/vod/267925").respond(503, text="")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await SimpsonsUATvProvider().stream(episode_url, None, http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_stream_connection_error_raises_unreachable():
    """A network error during the episode page fetch must surface as
    `unreachable`."""
    from cs_uk_api.providers.base import ProviderError

    episode_url = "https://simpsonsua.tv/s37/4441-37-sezon-17-seriya.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(episode_url).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await SimpsonsUATvProvider().stream(episode_url, None, http)
    assert exc.value.code == "unreachable"
