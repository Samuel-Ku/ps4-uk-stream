"""Tests for the UFDub provider (issue #17, Group 1)."""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.ufdub import UFDubProvider, _split_external_id

FIX = pathlib.Path(__file__).parent / "fixtures" / "ufdub"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_ufdub_search_parses_results():
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://ufdub.com/index.php").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await UFDubProvider().search("one piece", http)
    # Real search response contains 4 distinct cards (anime, anime,
    # cartoon-serial, dorama). Replaces the previous test that asserted
    # on a homepage-shaped fixture.
    assert len(results) == 4
    titles = [r.title for r in results]
    assert any("Net-juu no Susume" in t for t in titles)
    assert all(r.provider == "ufdub" for r in results)
    anime_one = next(r for r in results if "Net-juu no Susume" in r.title)
    assert anime_one.id.startswith("ufdub:anime-")
    assert anime_one.type == "anime"
    assert anime_one.url.startswith("https://ufdub.com/anime/")


@pytest.mark.asyncio
async def test_ufdub_search_classifies_types_by_url_path():
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://ufdub.com/index.php").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await UFDubProvider().search("one piece", http)
    # The id is `ufdub:<kind>-<slug>`. For hyphened kinds like
    # `cartoon-serial`, splitting on `-` gives `cartoon` as the first
    # chunk, so we use the URL path segment instead.
    types_by_url_kind = {r.url.split("/")[3]: r.type for r in results}
    # Regression: `/cartoon-serial/` must classify as `series`, not
    # `movie` (caught by the code-reviewer — `cartoon` was matching
    # before `cartoon-serial`).
    assert types_by_url_kind.get("cartoon-serial") == "series"
    assert types_by_url_kind.get("anime") == "anime"
    assert types_by_url_kind.get("dorama") == "dorama"


@pytest.mark.asyncio
async def test_ufdub_browse_anime_section_parses_results():
    listing_html = _fixture("anime_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/anime/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await UFDubProvider().browse("anime", 1, http)
    # Regression: `.short-text, .short` selector returned each card
    # twice (32 results for 16 cards). Use only the inner `.short-text`.
    assert len(results) == 16
    assert all(r.type == "anime" for r in results)
    assert all(r.id.startswith("ufdub:anime-") for r in results)
    # Regression: DLE pagination is `<span class="navigation">`, not
    # `<div class="navigation">`, and the marker link is `/page/N/`.
    assert has_next is True


@pytest.mark.asyncio
async def test_ufdub_browse_film_page1_has_next_true():
    listing_html = _fixture("film_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/film/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await UFDubProvider().browse("filmy", 1, http)
    assert len(results) == 16
    # The film section shows related content (3 anime, 12 film, 1
    # serial) — UFDub is not strictly partitioned. The page
    # classification is the URL's path, not the section's declared
    # type.
    assert has_next is True


@pytest.mark.asyncio
async def test_ufdub_browse_film_page_last_has_next_false():
    """When we are on the last page (>= all listed page numbers),
    has_next must be False so the client stops paging."""
    listing_html = _fixture("film_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/film/page/99/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            _, has_next = await UFDubProvider().browse("filmy", 99, http)
    assert has_next is False


def test_ufdub_split_external_id_multi_hyphen_kind():
    """Issue #162: a kind with a hyphen (``cartoon-serial``) must split
    at the digit boundary, not the first hyphen — otherwise
    ``cartoon-serial-308-wondla`` becomes kind="cartoon", slug=
    "serial-308-…" and _SLUG_RE rejects it."""
    assert _split_external_id("cartoon-serial-308-wondla") == (
        "cartoon-serial",
        "308-wondla",
    )
    assert _split_external_id("film-48-fokus-pokus-hocus-pocus") == (
        "film",
        "48-fokus-pokus-hocus-pocus",
    )
    assert _split_external_id("anime-23-rekomendaciji") == ("anime", "23-rekomendaciji")
    assert _split_external_id("no-slug") is None


@pytest.mark.asyncio
async def test_ufdub_content_cartoon_serial_kind_opens():
    """Issue #162 regression: a ``cartoon-serial`` card (search/browse
    id ``cartoon-serial-<slug>``) must open — content() used to reject
    the id as ``bad external_id`` before ever fetching, so every
    mult-serial was unopenable."""
    content_html = _fixture("content_movie.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://ufdub.com/cartoon-serial/308-wondla.html"
        ).respond(200, text=content_html)
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=2780",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            c = await UFDubProvider().content("cartoon-serial-308-wondla", http)
    assert c.title
    assert c.type == "series"


@pytest.mark.asyncio
async def test_ufdub_content_movie_parses_title_poster():
    """content() fetches the player page for every type (issue #164
    gating), so the movie player must be mocked too."""
    content_html = _fixture("content_movie.html")
    player_html = _fixture("player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/film/48-fokus-pokus-hocus-pocus.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=2780",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            c = await UFDubProvider().content("film-48-fokus-pokus-hocus-pocus", http)
    assert "Фокус" in c.title
    assert c.type == "movie"
    assert c.poster is not None
    assert c.poster.startswith("https://ufdub.com")


@pytest.mark.asyncio
async def test_ufdub_content_dead_player_page_raises_gated():
    """Issue #164: a content page whose player page exposes no
    playable media (upstream emits an empty ``var a = []``) is a dead
    card — content() must raise ``gated`` so the catalog sweep drops
    it from home instead of failing only at play time."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/film/48-fokus-pokus-hocus-pocus.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=2780",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text="<html><script>var a = [];</script></html>")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UFDubProvider().content("film-48-fokus-pokus-hocus-pocus", http)
    assert exc_info.value.code == "gated"


@pytest.mark.asyncio
async def test_ufdub_content_anime_classifies_as_anime():
    """Anime is a series-like type: content() now fetches the player
    page too, so it must be mocked."""
    content_html = _fixture("content_anime.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/anime/23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=285",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            c = await UFDubProvider().content("anime-23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume", http)
    assert c.type == "anime"


@pytest.mark.asyncio
async def test_ufdub_content_series_parses_episodes_from_player():
    """Series/anime content surfaces the player page's `var a` array
    as a single season of episodes. Regression (issue #114): previously
    an empty season list was returned, so series had no playable
    episodes in the catalog."""
    content_html = _fixture("content_anime.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/anime/23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=285",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            c = await UFDubProvider().content("anime-23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume", http)
    assert c.seasons is not None and len(c.seasons) == 1
    eps = c.seasons[0].episodes
    assert len(eps) == 37
    assert eps[0].number == 1
    assert eps[0].id == (
        "ufdub:anime-23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume:s1e1"
    )
    assert "Ритм Емоцій" in eps[0].title
    assert eps[-1].id.endswith(":s1e37")


@pytest.mark.asyncio
async def test_ufdub_stream_series_selects_requested_episode():
    """`<external>:s1e<N>` must resolve to the N-th `var a` entry, not
    always the first one (regression, issue #114). POS=5 is the live
    position of episode 3 in the captured player fixture."""
    content_html = _fixture("content_anime.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/anime/23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=285",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UFDubProvider().stream(
                "anime-23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume:s1e3",
                None,
                http,
            )
    assert "POS=5" in s.url
    assert s.type == "mp4"
    assert s.headers["Referer"] == "https://ufdub.com/"


@pytest.mark.asyncio
async def test_ufdub_stream_series_episode_out_of_range_raises():
    """Out-of-range episodes must raise not_found, not silently fall
    back to the first episode."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_anime.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/anime/23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=285",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UFDubProvider().stream(
                    "anime-23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume:s1e99",
                    None,
                    http,
                )
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_ufdub_stream_series_unknown_season_raises():
    """UFDub players are single-season pages: `s2e1` must raise
    not_found."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_anime.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/anime/23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=285",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UFDubProvider().stream(
                    "anime-23-rekomendaciji-dlja-chudovogo-zhittja-onlajn-net-juu-no-susume:s2e1",
                    None,
                    http,
                )
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_ufdub_stream_resolves_to_player_url():
    """Regression: `content_id` is the external_id (`film-48-...`), not
    a URL. The old implementation called `http.get(content_id)` which
    raised `ValueError: unknown url type` on every call."""
    content_html = _fixture("content_movie.html")
    player_html = _fixture("player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/film/48-fokus-pokus-hocus-pocus.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=2780",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UFDubProvider().stream("film-48-fokus-pokus-hocus-pocus", None, http)
    assert s.url.startswith("https://ufdub.com/video/VIDEOS.php?")
    assert s.type == "mp4"
    assert s.headers["Referer"] == "https://ufdub.com/"


@pytest.mark.asyncio
async def test_ufdub_stream_rejects_player_redirect_to_disallowed_host():
    """The player URL comes from upstream HTML, so it must go through
    the SSRF redirect allowlist (issue #121): a player page that
    redirects to an attacker-controlled host fails closed with
    `not_found` instead of being followed."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/film/48-fokus-pokus-hocus-pocus.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=2780",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(302, headers={"Location": "https://evil.example.com/pivot"})
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UFDubProvider().stream("film-48-fokus-pokus-hocus-pocus", None, http)
    assert exc_info.value.code == "not_found"
    assert "disallowed host" in exc_info.value.message


@pytest.mark.asyncio
async def test_ufdub_stream_follows_player_page_to_media_url():
    """Live-gate regression (2026-08-01): the stream URL returned by the
    content page (`VP.php?ID=...`) is an HTML player page, not media. The
    media URL lives in its `var a = [['Серія 1','mp4', url]]` array.
    Returning the player page as `m3u8` made mpv fail on every title."""
    content_html = _fixture("content_movie.html")
    player_html = _fixture("player.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ufdub.com/film/48-fokus-pokus-hocus-pocus.html").respond(
            200, text=content_html
        )
        router.get(
            "https://video.ufdub.com/AT/VP.php?ID=2780",
            headers={"Referer": "https://ufdub.com/"},
        ).respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await UFDubProvider().stream("film-48-fokus-pokus-hocus-pocus", None, http)
    assert s.url.startswith("https://ufdub.com/video/VIDEOS.php?")
    assert s.type == "mp4"
    assert s.headers["Referer"] == "https://ufdub.com/"


@pytest.mark.asyncio
async def test_ufdub_extract_media_url_accepts_quality_labels():
    """Regression (issue #40): the codec position of the `var a=` array
    must tolerate quality-bearing labels (`'720p'`, `'HD'`, `'source'`),
    not just the literal `mp4`/`web` the live site currently emits."""
    cases = [
        ("var a=[['Серія 1','mp4','https://x/v.mp4']];", "https://x/v.mp4"),
        ("var a=[['Фільм','720p','https://x/q720.mp4']];", "https://x/q720.mp4"),
        ("var a=[['Серія 1','HD','https://x/hd.mp4']];", "https://x/hd.mp4"),
        ("var a=[['Серія 1','source','https://x/src.mp4']];", "https://x/src.mp4"),
        ("var a=[['Серія 1','web','https://x/w.webm']];", "https://x/w.webm"),
    ]
    for script, expected in cases:
        html = f"<html><script>{script}</script></html>"
        assert UFDubProvider._extract_media_url(html) == expected


@pytest.mark.asyncio
async def test_ufdub_extract_media_url_returns_none_on_missing_array():
    html = "<html><script>var b = 1;</script></html>"
    assert UFDubProvider._extract_media_url(html) is None


@pytest.mark.asyncio
async def test_ufdub_sections_lists_six():
    sections = UFDubProvider().sections
    ids = [s.id for s in sections]
    # Per the upstream Kotlin source.
    assert ids == ["filmy", "serialy", "doramy", "cartoons", "multserialy", "anime"]


@pytest.mark.asyncio
async def test_ufdub_browse_unknown_section_raises():
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await UFDubProvider().browse("nonexistent", 1, httpx.AsyncClient())


@pytest.mark.asyncio
async def test_ufdub_content_bad_slug_raises():
    """Regression (HIGH #2, code-reviewer): the UFDub provider string-
    interpolated the slug into `f"{BASE_URL}/{kind}/{slug}.html"`
    without re-validating it. The external_id regex constrains the
    shape, but content()/stream() never enforced that constraint
    themselves — so a malformed `film-../admin` would build an
    out-of-bounds URL.

    The suffix after `<kind>-` must match `\\d+-[a-z0-9-]+`. Anything
    else surfaces as `not_found` BEFORE any HTTP request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UFDubProvider().content("film-../admin", http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_ufdub_stream_bad_slug_raises():
    """Same regression as `content`: the `stream()` partition path
    builds `content_url` from the same unvalidated slug. An invalid
    slug must raise `not_found` before any HTTP request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await UFDubProvider().stream("film-../admin", None, http)
    assert exc_info.value.code == "not_found"
