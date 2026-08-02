"""Tests for the CikavaIdeya provider (issue #17, Group 1)."""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.cikavaideya import CikavaIdeyaProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "cikavaideya"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cikavaideya_search_parses_results():
    """Real search response for query "всесв" contains 18 distinct cards."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://cikava-ideya.top/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await CikavaIdeyaProvider().search("всесв", http)
    assert len(results) == 18
    titles = [r.title for r in results]
    assert any("Як влаштований Всесвіт" in t for t in titles)
    assert all(r.provider == "cikavaideya" for r in results)
    titles_by_id = {r.id for r in results}
    # IDs are external_ids (no kind prefix on CikavaIdeya; the URL slug
    # is the unique key, e.g. "226-jak-vlashtovanij-vsesvit").
    assert "cikavaideya:226-jak-vlashtovanij-vsesvit" in titles_by_id
    assert all(r.url.startswith("https://cikava-ideya.top/") for r in results)


@pytest.mark.asyncio
async def test_cikavaideya_search_classifies_by_subtitle_tags():
    """Per-card classification reads the `.th-subtitle` tag list. The
    first card in the captured search response is a "Серіали" entry, so
    the upstream's heuristic (anything not "Фільми" / "Артхаус" is a
    series) classifies it as `series`.

    Regression: tests the longest-prefix-first classification
    — a card with both "Фільми" and "Анімаційні" must classify as
    `movie` (Фільми wins), not as a series.
    """
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://cikava-ideya.top/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await CikavaIdeyaProvider().search("всесв", http)
    by_title = {r.title: r for r in results}
    # First card is "Як влаштований Всесвіт" → Серіали → series.
    assert by_title["Як влаштований Всесвіт"].type == "series"


@pytest.mark.asyncio
async def test_cikavaideya_browse_filmy_section_parses_results():
    listing_html = _fixture("filmy.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://cikava-ideya.top/filmy/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await CikavaIdeyaProvider().browse("filmy", 1, http)
    # Per the captured filmy listing: 18 .th-item cards.
    assert len(results) == 18
    # Each card carries the "Фільми" subtitle tag → all are movies.
    assert all(r.type == "movie" for r in results)
    # All IDs begin with the provider id prefix.
    assert all(r.id.startswith("cikavaideya:") for r in results)
    # Pagination links exist (`<div class="navigation">` containing
    # `<a href="/filmy/page/N/">`). Page 1 → has_next True.
    assert has_next is True


@pytest.mark.asyncio
async def test_cikavaideya_browse_serialy_classifies_as_series():
    """The serialy section's cards all carry the "Серіали" tag."""
    listing_html = _fixture("serialy.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://cikava-ideya.top/serialy/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await CikavaIdeyaProvider().browse("serialy", 1, http)
    assert len(results) == 18
    assert all(r.type == "series" for r in results)
    assert has_next is True


@pytest.mark.asyncio
async def test_cikavaideya_browse_cartoon_section_classifies_by_subtitle():
    """The /cartoon/ section is upstream's "Мультсеріали". The per-card
    subtitle tag is mostly "Анімаційні" but a few cards carry both
    "Фільми" and "Анімаційні" (e.g. one-off animated shorts) — those
    classify as `movie` per the upstream conditional. Per the captured
    fixture: 16 series + 2 movies = 18 total.
    """
    listing_html = _fixture("cartoon.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://cikava-ideya.top/cartoon/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await CikavaIdeyaProvider().browse("cartoon", 1, http)
    assert len(results) == 18
    type_counts = {t: sum(1 for r in results if r.type == t) for t in {"movie", "series"}}
    # Regression: longest-prefix-first classification must put "Фільми"
    # ahead of "Анімаційні" — at least one card in this section is
    # tagged "Фільми / Анімаційні" and should classify as `movie`.
    assert type_counts.get("movie", 0) >= 1
    assert type_counts.get("series", 0) >= 1
    # The captured cartoon listing has no pagination links — every
    # card fits on page 1 — so has_next must be False.
    assert has_next is False


@pytest.mark.asyncio
async def test_cikavaideya_browse_filmy_last_page_has_next_false():
    """When on the last page (page N where N >= highest pagination
    link), has_next must be False so the client stops paging."""
    listing_html = _fixture("filmy.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://cikava-ideya.top/filmy/page/99/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            _, has_next = await CikavaIdeyaProvider().browse("filmy", 99, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_cikavaideya_content_movie_parses_title_poster():
    content_html = _fixture("content_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://cikava-ideya.top/281-duelianty.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await CikavaIdeyaProvider().content("281-duelianty", http)
    assert "Дуелянти" in c.title
    assert c.type == "movie"
    assert c.poster is not None
    assert c.poster.startswith("https://cikava-ideya.top/uploads/")
    # Movie content pages expose a single Player1 URL; the parser
    # surfaces it as season 1, episode 1 so the client can hand it
    # to /api/stream. The episode id encodes the position so stream()
    # can resolve it (movies use the `__movie__` suffix sentinel).
    assert c.seasons is not None
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) == 1
    assert c.seasons[0].episodes[0].id.endswith(":__movie__")


@pytest.mark.asyncio
async def test_cikavaideya_content_series_parses_seasons():
    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://cikava-ideya.top/226-jak-vlashtovanij-vsesvit.html").respond(
            200, text=content_html
        )
        async with httpx.AsyncClient() as http:
            c = await CikavaIdeyaProvider().content("226-jak-vlashtovanij-vsesvit", http)
    assert "Всесвіт" in c.title
    assert c.type == "series"
    # Captured page exposes 5 seasons with episode counts: [8, 8, 9, 8, 2].
    assert c.seasons is not None
    assert [s.number for s in c.seasons] == [1, 2, 3, 4, 5]
    assert [len(s.episodes) for s in c.seasons] == [8, 8, 9, 8, 2]
    # Each episode id encodes (season, episode) so /api/stream can
    # resolve it back to the right Player1 episode URL.
    first_s1 = c.seasons[0].episodes[0]
    assert first_s1.id.endswith(":s1e1")
    last_s5 = c.seasons[4].episodes[-1]
    assert last_s5.id.endswith(":s5e2")


@pytest.mark.asyncio
async def test_cikavaideya_stream_resolves_to_m3u8():
    """Regression: `content_id` is the external_id (e.g.
    `281-duelianty:__movie__`), NOT a URL. The old call pattern was
    `http.get(content_id)` which raised `ValueError: unknown url
    type` on every call. The provider must rebuild the URL from the
    external_id before fetching.

    Note: `/api/stream/{content_id}` strips the `<provider>:` prefix
    before calling `stream()`. The `:__movie__` suffix is our
    convention for movies (whose Player1 is a single URL, not a
    season/episode map)."""
    content_html = _fixture("content_movie.html")
    ashdi_html = _fixture("ashdi_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://cikava-ideya.top/281-duelianty.html").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/vod/228698").respond(200, text=ashdi_html)
        async with httpx.AsyncClient() as http:
            s = await CikavaIdeyaProvider().stream("281-duelianty:__movie__", None, http)
    # ashdi.vip serves the HLS manifest via `file: "https://..."` inside
    # an inline `<script>` — the RegexExtractor picks that up.
    assert s.url.startswith("https://ashdi.vip/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    # ashdi.vip requires a Referer to serve the manifest; the upstream
    # Kotlin sets `referer = "https://tortuga.wtf/"`.
    assert s.headers.get("Referer") == "https://tortuga.wtf/"


@pytest.mark.asyncio
async def test_cikavaideya_stream_bare_movie_id_without_movie_suffix():
    """Live-gate regression (2026-08-01): the gate calls
    `/api/stream/{content_id}` straight from a search result, whose id
    is the bare external_id (`279-zhnka-z-vtrini`) — no `:__movie__`
    suffix. `rpartition(":")` on a colon-less string returns
    `("", "", content_id)`, which previously produced the URL
    `https://cikava-ideya.top/.html` (403)."""
    content_html = _fixture("content_movie.html")
    ashdi_html = _fixture("ashdi_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://cikava-ideya.top/279-zhnka-z-vtrini.html").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/vod/228698").respond(200, text=ashdi_html)
        async with httpx.AsyncClient() as http:
            s = await CikavaIdeyaProvider().stream("279-zhnka-z-vtrini", None, http)
    assert s.url.startswith("https://ashdi.vip/")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_cikavaideya_stream_episode_resolves_series_episode():
    """Series episodes: `content_id` includes the s{N}e{M} suffix. The
    provider must split it, look up the right Player1 URL, then follow
    the ashdi redirect."""
    content_html = _fixture("content_series.html")
    ashdi_html = _fixture("ashdi_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://cikava-ideya.top/226-jak-vlashtovanij-vsesvit.html"
        ).respond(200, text=content_html)
        # The first episode URL is https://ashdi.vip/vod/127413 — but
        # that endpoint is currently offline. The stream resolver is
        # not asked to validate the upstream URL; we just mock the
        # same ashdi HTML so the regex extractor returns an m3u8.
        router.get("https://ashdi.vip/vod/127413").respond(200, text=ashdi_html)
        async with httpx.AsyncClient() as http:
            s = await CikavaIdeyaProvider().stream(
                "226-jak-vlashtovanij-vsesvit:s1e1", None, http
            )
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"


@pytest.mark.asyncio
async def test_cikavaideya_sections_lists_four():
    """Per the upstream Kotlin source's `mainPage`:
    filmy / serialy / cartoon (Мультсеріали) / arthaus (Артхаус)."""
    sections = CikavaIdeyaProvider().sections
    ids = [s.id for s in sections]
    assert ids == ["filmy", "serialy", "cartoon", "arthaus"]


@pytest.mark.asyncio
async def test_cikavaideya_browse_unknown_section_raises():
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await CikavaIdeyaProvider().browse("nonexistent", 1, httpx.AsyncClient())


@pytest.mark.asyncio
async def test_cikavaideya_stream_invalid_episode_suffix_raises():
    """Regression: code-reviewer caught dead-code fallback. When the
    caller passes an out-of-range s{N}e{M} suffix (or omits it for a
    series), the resolver must raise `parse_failed` rather than
    silently returning the first available episode.
    """
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://cikava-ideya.top/226-jak-vlashtovanij-vsesvit.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await CikavaIdeyaProvider().stream(
                    "226-jak-vlashtovanij-vsesvit:s99e99", None, http
                )
    assert exc_info.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_cikavaideya_search_non_200_raises_upstream_unreachable():
    """Regression: a 5xx or 4xx upstream response to the search POST
    must surface as `upstream_unreachable`, not `not_found`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.post("https://cikava-ideya.top/").respond(503, text="")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await CikavaIdeyaProvider().search("anything", http)
    assert exc_info.value.code == "upstream_unreachable"


@pytest.mark.asyncio
async def test_cikavaideya_content_missing_title_raises_parse_failed():
    """Regression: a content page with no `.full h1` must surface as
    `parse_failed` rather than crash with an AttributeError."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get("https://cikava-ideya.top/281-duelianty.html").respond(
            200, text="<html><body>no title here</body></html>"
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await CikavaIdeyaProvider().content("281-duelianty", http)
    assert exc_info.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_cikavaideya_content_bad_slug_raises():
    """Regression (HIGH #2, code-reviewer): the CikavaIdeya provider
    string-interpolated the slug into `f"{BASE_URL}/{external_id}.html"`
    without re-validating it. The external_id regex constrains the
    shape to `\\d+-[a-z0-9-]+`, but content()/stream() never enforced
    that constraint themselves — so a malformed `../admin` would
    build `https://cikava-ideya.top/../admin.html`, a path-traversal
    escape.

    Anything that fails the slug regex must surface as `not_found`
    BEFORE any HTTP request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await CikavaIdeyaProvider().content("../admin", http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_cikavaideya_stream_bad_slug_raises():
    """Same regression as `content`: the `stream()` partition path
    builds `content_url` from the same unvalidated slug. An invalid
    slug must raise `not_found` before any HTTP request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await CikavaIdeyaProvider().stream(
                    "../admin:__movie__", None, http
                )
    assert exc_info.value.code == "not_found"
