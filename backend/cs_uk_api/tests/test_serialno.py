"""Tests for the Serialno provider (issue #17, Group 2).

The live site is a DLE-style CMS at https://serialno.tv. The homepage
IS the series listing (no separate /series/ path), so the v2 contract
exposes a single `series` section backed by `/` and `/page/N/`.

The stream chain is two-hop: content page → `tortuga.tw/embed/<id>`
iframe (the first `.fplayer iframe`) → obfuscated `file:` payload
decoded with the upstream torDecrypt algorithm — same shape as
KinoVezha. The second iframe (`.fplayer iframe:nth-of-type(2)`) is a
trailer and is ignored.
"""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.serialno import SerialnoProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "serialno"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_serialno_search_parses_results():
    """Search response for "друзі" contains 6 cards. Every result has
    a `serialno:` id, a title, a poster URL, and a `series` type
    (the provider is series-only per the spec)."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://serialno.tv/index.php?do=search").respond(
            200, text=search_html
        )
        async with httpx.AsyncClient() as http:
            results = await SerialnoProvider().search("друзі", http)
    assert len(results) == 6
    assert all(r.provider == "serialno" for r in results)
    assert all(r.id.startswith("serialno:") for r in results)
    assert all(r.form == "series" for r in results)
    # All posters must be absolute URLs (card data-src is relative).
    assert all(r.poster is not None and r.poster.startswith("https://") for r in results)
    # All URLs must be absolute.
    assert all(r.url.startswith("https://serialno.tv/") for r in results)


@pytest.mark.asyncio
async def test_serialno_browse_series_page1():
    """The homepage is the series listing. The captured `/` listing
    has 20 `.th-item` cards, all of which classify as `series`. The
    pagination block lists pages 2..10 + 103, so has_next is True."""
    listing_html = _fixture("series_listing_page1.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await SerialnoProvider().browse("series", 1, http)
    assert len(results) == 20
    assert all(r.form == "series" for r in results)
    assert all(r.id.startswith("serialno:") for r in results)
    # IDs are bare slugs (no section prefix on serialno).
    assert all("serialno:" in r.id and "/" not in r.id.split(":", 1)[1] for r in results)
    assert has_next is True


@pytest.mark.asyncio
async def test_serialno_browse_series_page2():
    """The captured `/page/2/` listing has cards. `page2` mirror
    confirms pagination works mid-stream."""
    listing_html = _fixture("series_listing_page2.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/page/2/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await SerialnoProvider().browse("series", 2, http)
    assert len(results) >= 1
    assert all(r.form == "series" for r in results)
    # The last page is 103; page 2 has higher pages, so has_next is True.
    assert has_next is True


@pytest.mark.asyncio
async def test_serialno_browse_series_last_page():
    """Requesting a page >= highest link (103 in this listing) must
    yield has_next=False so the client stops paging."""
    listing_html = _fixture("series_listing_page1.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/page/400/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            _, has_next = await SerialnoProvider().browse("series", 400, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_serialno_content_series_parses_title_poster_player():
    """Series content page: title (Cyrillic), poster (absolute URL),
    and the first `.fplayer iframe` data-src pointing to
    tortuga.tw/embed/<id>. The series has at least one season with
    at least one episode decoded from the obfuscated `file:` payload."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_embed.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await SerialnoProvider().content("2075-1670", http)
    assert c.title == "1670"
    assert c.form == "series"
    assert c.poster is not None
    assert c.poster.startswith("https://serialno.tv/")
    assert c.seasons is not None
    assert len(c.seasons) >= 1
    assert all(len(s.episodes) >= 1 for s in c.seasons)
    # First season, first episode from the captured playlist.
    first = c.seasons[0]
    assert first.number == 1
    assert first.episodes[0].id == "serialno:2075-1670:s1e1"


@pytest.mark.asyncio
async def test_serialno_content_description_and_translation():
    """The content response carries the page description (from
    `.fdesc`) and the dubbing-studio name decoded from the player
    payload (ticket #332 — the dub-wrapped shape's top entry title)."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_embed.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await SerialnoProvider().content("2075-1670", http)
    assert "сатиричн" in c.description
    assert [t.label for t in c.translations] == ["ТакТребаПродакшн"]


@pytest.mark.asyncio
async def test_serialno_content_parses_year_and_people():
    """The `.flist` block carries `Рік:` and `В ролях:`/`Режисер:`
    rows with the data present — the provider must surface them.
    Regression: year was always None and people always [] even
    though the live-captured fixture has Рік: 2023, 6 actors and
    2 directors (Ticket #227)."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_embed.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await SerialnoProvider().content("2075-1670", http)
    assert c.year == 2023
    assert len(c.people) == 8
    actors = [p for p in c.people if p.role == "Actor"]
    directors = [p for p in c.people if p.role == "Director"]
    assert len(actors) == 6
    assert len(directors) == 2
    assert actors[0].name == "Бартломей Топа"
    assert directors[0].name == "Мацей Бухвальд"
    assert all(p.id.startswith("serialno:") for p in c.people)
    # The Жанр row is parsed but dropped — it must surface as genres.
    assert c.genres
    assert "Комедія" in c.genres


@pytest.mark.asyncio
async def test_serialno_stream_series_resolves_episode_m3u8():
    """Two-hop stream for a series episode: content page -> player
    page (`tortuga.tw/embed/<id>`) -> obfuscated `file:` payload ->
    season/episode JSON list -> per-episode m3u8 URL. Episode id is
    `<external>:s1e1` (1-based)."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_embed.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            s = await SerialnoProvider().stream("2075-1670:s1e1", None, http)
    assert s.url.startswith("https://calypso.tortuga.tw/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    # tortuga.tw requires a Referer to serve the manifest; the
    # upstream Kotlin sets it to the page origin.
    assert s.headers.get("Referer") == "https://serialno.tv/"


@pytest.mark.asyncio
async def test_serialno_stream_rejects_player_redirect_to_disallowed_host():
    """The player URL comes from upstream HTML, so it must go through
    the SSRF redirect allowlist (issue #126): a player page that
    redirects to an attacker-controlled host fails closed with
    `not_found` instead of being followed."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            302, headers={"Location": "https://evil.example.com/pivot"}
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await SerialnoProvider().stream("2075-1670:s1e1", None, http)
    assert exc_info.value.code == "not_found"
    assert "disallowed host" in exc_info.value.message


@pytest.mark.asyncio
async def test_serialno_stream_series_season2_resolves():
    """The captured playlist has 2 seasons. An episode from season 2
    (s2e1) must resolve to a different m3u8 URL than s1e1, proving
    the season index is honored."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_embed.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            s1 = await SerialnoProvider().stream("2075-1670:s1e1", None, http)
            s2 = await SerialnoProvider().stream("2075-1670:s2e1", None, http)
    assert s1.url != s2.url
    assert "s01e01" in s1.url
    assert "s02e01" in s2.url


@pytest.mark.asyncio
async def test_serialno_content_flat_live_payload_parses_seasons():
    """Live-gate regression (2026-08-08): the Tortuga player payload
    no longer wraps seasons in a "dub" object — data IS the season
    list. The adapter must surface episodes from the flat shape."""
    content_html = _fixture("content_series_live.html")
    player_html = _fixture("player_embed_flat.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/1398-dyuna.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/1400").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await SerialnoProvider().content("1398-dyuna", http)
    assert c.seasons is not None
    assert len(c.seasons) >= 1
    assert len(c.seasons[0].episodes) >= 1
    assert c.seasons[0].episodes[0].id == "serialno:1398-dyuna:s1e1"


@pytest.mark.asyncio
async def test_serialno_stream_flat_live_payload_strips_label_and_subtitle():
    """Live-gate regression (2026-08-08): live episode `file` values
    carry a `{КІНО}` label prefix and a `(subtitle:)` tail. The stream
    URL must be a bare m3u8, or mpv would try to fetch a garbage URL."""
    content_html = _fixture("content_series_live.html")
    player_html = _fixture("player_embed_flat.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/1398-dyuna.html").respond(
            200, text=content_html
        )
        router.get("https://tortuga.tw/embed/1400").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            s = await SerialnoProvider().stream("1398-dyuna:s1e1", None, http)
    assert s.url.startswith("https://calypso.tortuga.tw/")
    assert s.url.endswith(".m3u8")
    assert "{" not in s.url
    assert "(subtitle" not in s.url


def test_serialno_sections_lists_one():
    """The site is series-only per the spec; one section is exposed."""
    sections = SerialnoProvider().sections
    ids = [s.id for s in sections]
    assert ids == ["series"]
    assert all(s.form == "series" for s in sections)


@pytest.mark.asyncio
async def test_serialno_browse_unknown_section_raises():
    with respx.mock(assert_all_called=False), pytest.raises(ProviderError) as exc_info:
        await SerialnoProvider().browse("films", 1, httpx.AsyncClient())
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_serialno_content_bad_slug_raises():
    """Regression: the provider must validate the slug at the
    boundary so malformed inputs surface as `not_found` BEFORE any
    HTTP request is made. Path-traversal attempts like
    `2075-../admin` would otherwise escape the URL space."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await SerialnoProvider().content("2075-../admin", http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_serialno_stream_bad_slug_raises():
    """Same regression for `stream()`: the slug must be validated
    before any HTTP request is made."""
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await SerialnoProvider().stream("2075-../admin:s1e1", None, http)
    assert exc_info.value.code == "not_found"


# ---------------------------------------------------------------------------
# Ticket #332: dubbing-studio names (multi-dub payloads)
# ---------------------------------------------------------------------------

def _encode_payload(payload: str, salt: int = 42) -> str:
    """Tortuga-encode a decoded JSON payload (the XOR is symmetric)."""
    import base64 as b64

    raw = bytes([salt]) + bytes(
        b ^ ((salt + 7 * i + 13) % 256) for i, b in enumerate(payload.encode("utf-8"))
    )
    return b64.b64encode(raw).decode()


def _player_page(payload: str) -> str:
    return f"<html><body><script>file: \"{_encode_payload(payload)}\"</script></body></html>"


TWO_DUB_WRAPPED_PAYLOAD = (
    '[{"title":"ТакТребаПродакшн","folder":[{"title":" Сезон 1","folder":['
    '{"title":"Серія 1","file":"https://calypso.tortuga.tw/hls/serials/x.s01e01.ttp/index.m3u8"},'
    '{"title":"Серія 2","file":"https://calypso.tortuga.tw/hls/serials/x.s01e02.ttp/index.m3u8"}]}]},'
    '{"title":"Інша Студія","folder":[{"title":" Сезон 1","folder":['
    '{"title":"Серія 1","file":"https://calypso.tortuga.tw/hls/serials/x.s01e01.other/index.m3u8"},'
    '{"title":"Серія 2","file":"https://calypso.tortuga.tw/hls/serials/x.s01e02.other/index.m3u8"}]}]}'
    "]"
)


@pytest.mark.asyncio
async def test_serialno_content_multi_dub_wrapped_exposes_dub_translations():
    """Ticket #332: the dub-wrapped payload's top entries are dubbing
    studios — they must surface as content translations, not a uk stub."""
    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(200, text=content_html)
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=_player_page(TWO_DUB_WRAPPED_PAYLOAD)
        )
        async with httpx.AsyncClient() as http:
            c = await SerialnoProvider().content("2075-1670", http)
    assert [t.label for t in c.translations] == ["ТакТребаПродакшн", "Інша Студія"]
    assert c.seasons is not None and len(c.seasons) == 1
    ep = c.seasons[0].episodes[0]
    assert [t.label for t in (ep.translations or [])] == ["ТакТребаПродакшн", "Інша Студія"]


@pytest.mark.asyncio
async def test_serialno_stream_multi_dub_wrapped_honors_translation():
    """Ticket #332: the picked studio plays ITS folder's episode."""
    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(200, text=content_html)
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=_player_page(TWO_DUB_WRAPPED_PAYLOAD)
        )
        async with httpx.AsyncClient() as http:
            default = await SerialnoProvider().stream("2075-1670:s1e1", None, http)
            picked = await SerialnoProvider().stream("2075-1670:s1e1", "Інша Студія", http)
    assert "s01e01.ttp" in default.url
    assert "s01e01.other" in picked.url


TWO_DUB_FLAT_PAYLOAD = (
    '[{"title":"Сезон 1","folder":['
    '{"title":"Серія 1","file":"{КІНО}https://calypso.tortuga.tw/hls/serials/x.s01e01.kino/index.m3u8"},'
    '{"title":"Серія 1","file":"{HDrezka Studio}https://calypso.tortuga.tw/hls/serials/x.s01e01.rezka/index.m3u8"}]}]'
)


@pytest.mark.asyncio
async def test_serialno_content_flat_exposes_prefix_dub_labels():
    """Ticket #332: flat payloads carry the dub in the ``{...}`` file
    prefix — those labels are the translations."""
    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(200, text=content_html)
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=_player_page(TWO_DUB_FLAT_PAYLOAD)
        )
        async with httpx.AsyncClient() as http:
            c = await SerialnoProvider().content("2075-1670", http)
    assert [t.label for t in c.translations] == ["КІНО", "HDrezka Studio"]


@pytest.mark.asyncio
async def test_serialno_stream_flat_honors_translation():
    """Ticket #332: on the flat shape the picked dub label selects the
    episode entry whose ``{...}`` prefix matches."""
    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://serialno.tv/2075-1670.html").respond(200, text=content_html)
        router.get("https://tortuga.tw/embed/2083").respond(
            200, text=_player_page(TWO_DUB_FLAT_PAYLOAD)
        )
        async with httpx.AsyncClient() as http:
            default = await SerialnoProvider().stream("2075-1670:s1e1", None, http)
            picked = await SerialnoProvider().stream("2075-1670:s1e1", "HDrezka Studio", http)
    assert "s01e01.kino" in default.url
    assert "s01e01.rezka" in picked.url
