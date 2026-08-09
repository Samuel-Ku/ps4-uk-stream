"""Tests for the BambooUA provider (issue #17, Group 1, has JSONModel.kt)."""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.bambooua import BambooUAProvider
from cs_uk_api.providers.base import ProviderError

FIX = pathlib.Path(__file__).parent / "fixtures" / "bambooua"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_bambooua_search_parses_results():
    """search() must return exactly the cards visible in the search
    response (cat-item slides, not the featured banner-item slides)."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://bambooua.com/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await BambooUAProvider().search("love", http)
    # Real search response contains 20 distinct cat-item cards.
    assert len(results) == 20
    assert all(r.provider == "bambooua" for r in results)
    titles = [r.title for r in results]
    assert any("Кохання" in t for t in titles)
    # First few are all 'love' matches.
    first = results[0]
    assert first.id.startswith("bambooua:lakorn/") or first.id.startswith("bambooua:dorama/")


@pytest.mark.asyncio
async def test_bambooua_search_classifies_by_url_path():
    """REGRESSION: type must be classified per-card by URL path, not
    hardcoded. The first search results span /lakorn/, /dorama/, etc."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://bambooua.com/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await BambooUAProvider().search("love", http)
    types_by_kind = {r.url.split("/")[3]: r.type for r in results}
    # The upstream maps the URL path segment to a MediaType:
    #   dorama  -> "dorama"
    #   anime   -> "anime"
    #   lakorn  -> "series"
    #   cinema  -> "movie"
    assert types_by_kind.get("dorama") == "dorama"
    assert types_by_kind.get("lakorn") == "series"
    # /zhanr/ is a category index page that the upstream scripts also
    # surface; classify such listings as 'series' (the safe default).
    assert types_by_kind.get("zhanr") is not None


@pytest.mark.asyncio
async def test_bambooua_search_external_id_preserves_multi_segment():
    """REGRESSION (#139 id-collapse): catalog URLs like
    `/zhanr/romantyka/1049-the_shapes_of_love` must keep the FULL path
    in the external_id so the content URL can be rebuilt verbatim —
    collapsing to the last two segments (`romantyka/1049-...`) produces
    a URL that the site 301-redirects (only `/zhanr/...` is a 200)."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://bambooua.com/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await BambooUAProvider().search("love", http)
    zhanr = [r for r in results if "/zhanr/" in r.url]
    assert len(zhanr) == 1
    assert zhanr[0].id == "bambooua:zhanr/romantyka/1049-the_shapes_of_love"


@pytest.mark.asyncio
async def test_bambooua_content_zhanr_full_path_is_gated_not_301():
    """#139 id-collapse: the /zhanr/drama/1156-personasulli card keeps
    the full path, so content() fetches the verbatim URL (the collapsed
    `drama/1156-personasulli` form 301-redirects -> not_found, a
    health-down). The resolved page is itself subscription-gated
    (`const playlist = []`, captured 2026-08-08) -> gated, never a
    transport/health failure."""
    content_html = _fixture("content_zhanr_drama_1156.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/zhanr/drama/1156-personasulli.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().content(
                    "zhanr/drama/1156-personasulli", http
                )
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_stream_zhanr_full_path_uses_production_form_id():
    """#139 id-collapse: stream() accepts the multi-segment production-form
    id (no `provider:` prefix) and fetches the verbatim URL — a collapsed
    `drama/1156-personasulli` form would 301-redirect -> not_found, a
    health-down. The resolved page is subscription-gated (`const playlist
    = []`, captured 2026-08-08), so stream() answers gated — never a
    transport/health failure."""
    content_html = _fixture("content_zhanr_drama_1156.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/zhanr/drama/1156-personasulli.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().stream(
                    "zhanr/drama/1156-personasulli:s1e1", None, http
                )
    assert exc.value.code == "gated"


def test_bambooua_external_id_from_url_keeps_full_path():
    """#139 id-collapse: `_external_id_from_url` preserves the FULL
    path (root-relative or absolute) so `content()` can rebuild the
    verbatim 200 URL. A bare relative href with no leading slash must be
    rejected (parse_failed) — matching it would silently collapse to the
    301 form this fix exists to prevent."""
    from cs_uk_api.providers.bambooua import _external_id_from_url

    assert (
        _external_id_from_url("/zhanr/drama/1156-personasulli.html")
        == "zhanr/drama/1156-personasulli"
    )
    assert (
        _external_id_from_url("https://bambooua.com/zhanr/romantyka/1049-the_shapes_of_love.html")
        == "zhanr/romantyka/1049-the_shapes_of_love"
    )
    assert _external_id_from_url("/cinema/1159-aichaku.html") == "cinema/1159-aichaku"
    with pytest.raises(ProviderError) as exc:
        _external_id_from_url("zhanr/drama/1156-personasulli.html")
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_bambooua_browse_cinema_parses_results():
    """REGRESSION: `/cinema/` listing returns 21 cat-item cards
    (article.swiper-slide with div.cat-item). The featured banner-item
    slides must be filtered out."""
    listing_html = _fixture("cinema_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/cinema/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await BambooUAProvider().browse("cinema", 1, http)
    assert len(results) == 21
    # Type is per-card, not per-section. The cinema listing also has
    # 3 dorama/ and 1 zhanr/ cards mixed in.
    cinema_count = sum(1 for r in results if r.type == "movie")
    assert cinema_count == 17
    types_by_kind = {r.url.split("/")[3]: r.type for r in results}
    assert types_by_kind["dorama"] == "dorama"
    # The real listing has 13 pages.
    assert has_next is True


@pytest.mark.asyncio
async def test_bambooua_browse_anime_parses_results():
    listing_html = _fixture("anime_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/anime/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await BambooUAProvider().browse("anime", 1, http)
    assert len(results) == 21
    # The anime listing has 2 /voice/ cards and 1 /lgbtq/ card
    # alongside 18 anime cards. Type is per-card, not per-section.
    anime_count = sum(1 for r in results if r.type == "anime")
    assert anime_count == 18
    types_by_kind = {r.url.split("/")[3]: r.type for r in results}
    assert types_by_kind.get("voice") == "series"
    assert types_by_kind.get("lgbtq") == "series"
    assert has_next is True


@pytest.mark.asyncio
async def test_bambooua_browse_last_page_has_next_false():
    """When we are on the last page (>= all listed page numbers),
    has_next must be False so the client stops paging."""
    listing_html = _fixture("cinema_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/cinema/page/99/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            _, has_next = await BambooUAProvider().browse("cinema", 99, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_bambooua_content_free_movie_parses_title_poster():
    """A non-gated movie resolves its title/poster from the JSON-LD
    block (live-captured fixture, cinema/1041)."""
    content_html = _fixture("content_movie_free.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/cinema/1041-you-are-the-apple-of-my-eye.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            c = await BambooUAProvider().content(
                "cinema/1041-you-are-the-apple-of-my-eye", http
            )
    assert c.type == "movie"
    assert c.poster is not None
    assert c.poster.startswith("https://bambooua.com")


@pytest.mark.asyncio
async def test_bambooua_content_single_file_movie_prefixes_episode_id():
    """A movie whose playlist group carries a single folder entry is
    surfaced as season 1, episode 1. The episode id must be prefixed
    with the provider namespace (issue #175) so /api/stream can split
    on the first ':' — a bare `__movie__` id 404s on the real client."""
    html = (
        '<html><head><meta property="og:image" content="/img/poster.jpg">'
        "<script>const playlist = "
        '[{"title":"Озвучення","folder":[{"title":"Фільм",'
        '"file":"https://cdn.example.com/film.m3u8"}]}];'
        "</script></head><body><h1>Фільм тест</h1></body></html>"
    )
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/cinema/1041-you-are-the-apple-of-my-eye.html"
        ).respond(200, text=html)
        async with httpx.AsyncClient() as http:
            c = await BambooUAProvider().content(
                "cinema/1041-you-are-the-apple-of-my-eye", http
            )
    assert c.type == "movie"
    assert c.seasons is not None
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) == 1
    assert c.seasons[0].episodes[0].id == (
        "bambooua:cinema/1041-you-are-the-apple-of-my-eye:__movie__"
    )


@pytest.mark.asyncio
async def test_bambooua_content_gated_movie_raises_gated():
    """GATED: the Aichaku movie's playlist is
    `[{file: "/uploads/be_sponsors.mp4"}]` — the subscription-gate
    promo clip. content() must refuse it with the `gated` verdict so
    the promo never surfaces as playable content."""
    content_html = _fixture("content_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/cinema/1159-aichaku.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().content("cinema/1159-aichaku", http)
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_content_free_series_parses_seasons():
    """A partially-gated series is NOT gated as a whole: Blood River
    (live-captured fixture) has 10 free episodes + 9 "Для підписників"
    placeholders. content() must still expose the season list."""
    content_html = _fixture("content_series_free.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/dorama/1119-blood-river.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await BambooUAProvider().content("dorama/1119-blood-river", http)
    assert c.type == "dorama"
    assert c.seasons is not None
    # One season (Субтитри folder) with all 19 listed episodes.
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) == 19
    assert c.seasons[0].episodes[0].title.startswith("Серія 01")


@pytest.mark.asyncio
async def test_bambooua_content_gated_series_raises_gated():
    """GATED: the dream-to-you dorama fixture has ALL six episodes as
    "Для підписників" sponsor placeholders → the whole item is gated."""
    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/dorama/1158-dream-to-you.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().content("dorama/1158-dream-to-you", http)
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_content_no_playlist_raises_gated():
    """#139 no_episodes: a live title served with a title but NO
    `const playlist` block (subscription-gated to non-subscribers,
    captured 2026-08-08 from dorama/262-legenda-pro-nok-tu) must raise
    `gated`, not fall through to a zero-season listing."""
    content_html = _fixture("content_no_playlist.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/dorama/262-legenda-pro-nok-tu-the-tale-of-nok-du.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().content(
                    "dorama/262-legenda-pro-nok-tu-the-tale-of-nok-du", http
                )
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_content_empty_playlist_array_raises_gated():
    """The `const playlist = []` variant of the same break (captured
    live 2026-08-08 from tv-show/652-his-man-season-1): the array parses
    but is empty — still nothing playable, still gated."""
    content_html = _fixture("content_empty_playlist.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/tv-show/652-his-man-season-1.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().content(
                    "tv-show/652-his-man-season-1", http
                )
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_content_groups_without_files_raises_gated():
    """#139 coverage gap: a playlist parsed into non-empty groups whose
    every group carries NO file and NO folder (e.g. an upstream variant
    `[{title:"Сезон 1",folder:[]},{title:"Сезон 2",folder:[]}]`) is
    neither caught by ``_require_playlist`` (groups is non-empty) nor by
    ``_playlist_fully_gated`` (returns False when ``not files``). Without
    an explicit guard, ``_build_seasons`` returns ``None`` and content()
    surfaces a zero-season ``ContentResponse`` — the exact #139 break
    this gate is meant to prevent. content() must raise ``gated``."""
    content_html = _fixture("content_empty_folders.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/dorama/262-legenda-pro-nok-tu.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().content(
                    "dorama/262-legenda-pro-nok-tu", http
                )
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_stream_groups_without_files_raises_gated():
    """Same coverage gap as the content() variant: stream() on a title
    whose playlist is non-empty groups with zero playable files must
    raise ``gated`` (consistent with ``test_bambooua_stream_no_playlist``),
    not ``not_found`` — so a stale card for a withheld title does not
    pollute the health tracker."""
    content_html = _fixture("content_empty_folders.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/dorama/262-legenda-pro-nok-tu.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().stream(
                    "dorama/262-legenda-pro-nok-tu:s1e1", None, http
                )
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_stream_no_playlist_raises_gated():
    """stream() on a title with no playable manifest must answer gated
    (never parse_failed → health-down) so a stale card for a removed or
    subscription-gated title does not pollute the health tracker."""
    content_html = _fixture("content_no_playlist.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/dorama/262-legenda-pro-nok-tu-the-tale-of-nok-du.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().stream(
                    "dorama/262-legenda-pro-nok-tu-the-tale-of-nok-du:s1e1", None, http
                )
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_extract_playlist_malformed_block_raises_parse_failed():
    """A `const playlist` block that exists but won't parse is a genuine
    parse gap (upstream shape change), not deliberate withholding: it
    must raise `parse_failed` so the health tracker sees the break
    instead of the title being swallowed into gated/zero-season (#139)."""
    from cs_uk_api.providers.bambooua import _extract_playlist

    with pytest.raises(ProviderError) as exc:
        _extract_playlist("const playlist = [{broken];")
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_bambooua_extract_playlist_empty_array_is_not_a_parse_gap():
    """`const playlist = []` parses fine (returns []) — it is the
    deliberate empty-manifest shape content() gates, NOT a parse gap."""
    from cs_uk_api.providers.bambooua import _extract_playlist

    assert _extract_playlist("const playlist = [];") == []


@pytest.mark.asyncio
async def test_bambooua_stream_free_movie_resolves_m3u8():
    """REGRESSION: `content_id` is the external_id (`cinema/...`), not
    a URL. A live movie stream is HLS and must be typed as such."""
    content_html = _fixture("content_movie_free.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://bambooua.com/cinema/1041-you-are-the-apple-of-my-eye.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            s = await BambooUAProvider().stream(
                "cinema/1041-you-are-the-apple-of-my-eye", None, http
            )
    assert s.url.endswith("index.m3u8")
    assert s.type == "m3u8"
    assert s.headers["Referer"] == "https://bambooua.com/"


@pytest.mark.asyncio
async def test_bambooua_stream_gated_movie_raises_gated():
    """GATED: the Aichaku movie's stream IS the sponsor promo clip —
    stream() must refuse instead of handing the ad to the player."""
    content_html = _fixture("content_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/cinema/1159-aichaku.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().stream("cinema/1159-aichaku", None, http)
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_stream_free_series_episode_resolves_m3u8():
    """A free episode of a partially-gated series resolves to its real
    m3u8 (Blood River s1e1)."""
    content_html = _fixture("content_series_free.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/dorama/1119-blood-river.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            s = await BambooUAProvider().stream(
                "dorama/1119-blood-river:s1e1", None, http
            )
    assert s.url == "https://ongoing3.bambooua.com/Blood_River/sub/s1/01/index.m3u8"
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_bambooua_stream_gated_series_episode_raises_gated():
    """A "Для підписників" episode (s1e11 of the Blood River fixture)
    is the sponsor placeholder → gated, never the ad clip."""
    content_html = _fixture("content_series_free.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/dorama/1119-blood-river.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().stream(
                    "dorama/1119-blood-river:s1e11", None, http
                )
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_bambooua_stream_unknown_episode_raises_not_found():
    """REGRESSION: out-of-range episode must raise not_found, not
    silently fall back to the first available episode."""
    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://bambooua.com/dorama/1158-dream-to-you.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().stream(
                    "dorama/1158-dream-to-you:s1e99", None, http
                )
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_bambooua_sections_exclude_dead_world_bl():
    """The upstream Kotlin `mainPageOf(...)` declares nine sections, but
    the `world-bl` («Світ ЛГБТ») listing is dead: `/world-bl/`
    301-redirects to the site root and the homepage no longer links it
    (verified live 2026-08-09). Following that redirect would serve home
    content mislabeled as `world-bl` (wrong data), so the section is
    retired from the exposed set — the eight live sections remain."""
    sections = BambooUAProvider().sections
    ids = [s.id for s in sections]
    assert "world-bl" not in ids
    assert ids == [
        "cinema",
        "dorama",
        "anime",
        "lakorn",
        "voice",
        "tv-show",
        "done",
        "now",
    ]


@pytest.mark.asyncio
async def test_bambooua_browse_unknown_section_raises():
    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await BambooUAProvider().browse("nonexistent", 1, httpx.AsyncClient())


@pytest.mark.asyncio
async def test_bambooua_content_bad_external_id_raises_not_found():
    """Regression: `content()` must reject external_ids that do not
    match the `<category>/<numeric-slug>` regex before interpolating
    into the URL — otherwise a caller-supplied `../../etc/passwd`
    would escape the upstream URL path."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().content("../../etc/passwd", http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_bambooua_stream_bad_content_id_raises_not_found():
    """Regression: `stream()` strips any `s<N>e<M>` suffix and
    rebuilds the content URL from the embedded external_id; reject
    payload that escapes the slug charset before the first HTTP call."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await BambooUAProvider().stream("../../etc/passwd", None, http)
    assert exc.value.code == "not_found"
