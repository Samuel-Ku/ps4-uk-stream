"""Tests for the KlonTV provider (issue #17, Group 2)."""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.klontv import KlonTVProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "klontv"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_klontv_search_parses_results():
    """Search response for "дюна" contains 9 cards. Each result
    has a `klontv:` id, a title, a poster URL, and a `series` or
    `movie` type depending on the URL path."""
    search_html = _fixture("search.html")
    with respx.mock(assert_all_called=True) as router:
        router.post("https://klonua.com/").respond(200, text=search_html)
        async with httpx.AsyncClient() as http:
            results = await KlonTVProvider().search("дюна", http)
    assert len(results) == 9
    assert all(r.provider == "klontv" for r in results)
    assert all(r.id.startswith("klontv:") for r in results)
    # All posters must be absolute URLs (card data-src is relative).
    assert all(r.poster is not None and r.poster.startswith("https://") for r in results)
    # All URLs must be absolute.
    assert all(r.url.startswith("https://klonua.com/") for r in results)
    # Type classification: serialy URLs must produce `series`, filmy URLs
    # must produce `movie`. First card is a serialy link.
    types_by_path = {r.url.split("/")[3]: r for r in results}
    assert types_by_path["serialy"].form == "series"
    assert types_by_path["filmy"].form == "movie"


@pytest.mark.asyncio
async def test_klontv_browse_films_page1():
    """Captured /filmy/ listing has 48 `.short-news__slide-item` cards
    but 1 of them is a `/multserialy/` link (out of v2 scope) and
    another is a `/serialy/` recommendation card. The v2 contract
    exposes only `films` and `series` — the multserialy card is
    dropped, the serialy card is kept and classified as `series`.
    The pagination block lists pages 2..10 + 354, so has_next is True.
    """
    listing_html = _fixture("films_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://klonua.com/filmy/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await KlonTVProvider().browse("films", 1, http)
    assert len(results) == 47
    # All returned ids are films/* or series/* — no multfilmy/multserialy.
    assert all(r.id.startswith("klontv:films/") or r.id.startswith("klontv:series/") for r in results)
    # All URLs are absolute and point to the site's filmy/serialy paths.
    assert all(
        r.url.startswith("https://klonua.com/filmy/") or r.url.startswith("https://klonua.com/serialy/")
        for r in results
    )
    # Type classification is consistent with the URL path.
    type_by_path = {r.url.split("/")[3]: r for r in results}
    assert type_by_path["filmy"].form == "movie"
    assert type_by_path["serialy"].form == "series"
    assert has_next is True


@pytest.mark.asyncio
async def test_klontv_browse_series_page1():
    """Captured /serialy/ listing has 48 cards. has_next must be
    True (page 2..10 + 73 are listed)."""
    listing_html = _fixture("serialy_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://klonua.com/serialy/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            results, has_next = await KlonTVProvider().browse("series", 1, http)
    assert len(results) == 48
    assert all(r.form == "series" for r in results)
    assert all(r.id.startswith("klontv:series/") for r in results)
    assert all(r.url.startswith("https://klonua.com/serialy/") for r in results)
    assert has_next is True


@pytest.mark.asyncio
async def test_klontv_browse_films_last_page():
    """The /filmy/ pagination block lists pages 2..10 + 354, so
    requesting page 400 (>= highest link) must yield has_next=False
    so the client stops paging."""
    listing_html = _fixture("films_listing.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://klonua.com/filmy/page/400/").respond(200, text=listing_html)
        async with httpx.AsyncClient() as http:
            _, has_next = await KlonTVProvider().browse("films", 400, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_klontv_content_movie_parses_title_poster_player():
    """Movie content page: title (Cyrillic), poster (absolute URL),
    and the player iframe data-src pointing to ashdi.vip/vod/..."""
    content_html = _fixture("content_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://klonua.com/filmy/11719-duna-chastyna-druga.html"
        ).respond(200, text=content_html)
        async with httpx.AsyncClient() as http:
            c = await KlonTVProvider().content("films/11719-duna-chastyna-druga", http)
    assert "Дюна" in c.title
    assert c.form == "movie"
    assert c.poster is not None
    assert c.poster.startswith("https://klonua.com/uploads/")
    # Movies expose a single playable URL; surface it as season 1
    # episode 1 so the client can hand it to /api/stream.
    assert c.seasons is not None
    assert len(c.seasons) == 1
    assert len(c.seasons[0].episodes) == 1
    assert c.seasons[0].episodes[0].id.endswith(":__movie__")


@pytest.mark.asyncio
async def test_klontv_content_series_parses_seasons():
    """Series content page: at least one season with at least one
    episode. Player URL is an ashdi.vip/serial/<id> iframe.
    Seasons/episodes come from the player page's playlist JSON.
    """
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://klonua.com/serialy/8431-duna.html").respond(
            200, text=content_html
        )
        # The provider strips `?multivoice` before fetching (matches
        # the upstream Kotlin's behaviour).
        router.get("https://ashdi.vip/serial/6212").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await KlonTVProvider().content("series/8431-duna", http)
    assert "Дюна" in c.title
    assert c.form == "series"
    assert c.seasons is not None
    assert len(c.seasons) >= 1
    assert all(len(s.episodes) >= 1 for s in c.seasons)
    # First season from the captured playlist has 3 episodes.
    first = c.seasons[0]
    assert first.number == 1
    assert len(first.episodes) >= 1


@pytest.mark.asyncio
async def test_klontv_content_parses_cast():
    """Ticket #221: the content page's schema.org JSON-LD carries an
    ``actor[]`` (and ``director[]``) of ``{@type: Person, name}`` — parse
    them into ``ContentResponse.people`` with role labels."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://klonua.com/serialy/8431-duna.html").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/serial/6212").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await KlonTVProvider().content("series/8431-duna", http)
    actors = [p for p in c.people if p.role == "Actor"]
    directors = [p for p in c.people if p.role == "Director"]
    assert len(actors) == 20
    assert actors[0].name == "Вільям Гарт"
    assert actors[0].id == "klontv:actor:0"
    assert directors == [
        p for p in c.people if p.name == "Джон Гаррісон" and p.role == "Director"
    ]


@pytest.mark.asyncio
async def test_klontv_content_follows_section_redirect():
    """Regression (observed live 2026-08-09): a title moved between
    sections answers 301 (`/filmy/...` -> `/serialy/...`). content()
    must follow the same-host redirect (safe_get) and resolve the
    title's player page instead of surfacing a dead not_found."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://klonua.com/filmy/10905-serial.html").respond(
            301, headers={"Location": "/serialy/10905-serial.html"}
        )
        router.get("https://klonua.com/serialy/10905-serial.html").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/serial/6212").respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            c = await KlonTVProvider().content("films/10905-serial", http)
    assert "Дюна" in c.title
    assert c.form == "series"
    assert c.seasons is not None
    assert len(c.seasons) >= 1


@pytest.mark.asyncio
async def test_klontv_stream_bare_series_id_refuses_json_blob():
    """Regression (class #165): stream() for a bare series id (no
    episode suffix — reachable when content() surfaced empty seasons)
    must NOT hand the client the raw PlayerJS playlist JSON as a
    stream URL; it raises parse_failed instead."""
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://klonua.com/serialy/8431-duna.html").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/serial/6212").respond(
            200, text=player_html
        )
        from cs_uk_api.providers.base import ProviderError

        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await KlonTVProvider().stream("series/8431-duna", None, http)
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_klontv_stream_movie_resolves_to_media_url():
    """Two-hop stream for a movie: content page -> player page
    (`ashdi.vip/vod/<id>`) -> `file:'https://.../index.m3u8'`.

    Regression: the content_id arriving in `stream()` is the bare
    external_id (e.g. `films/11719-duna-chastyna-druga`); the
    provider must rebuild the content URL from it rather than
    passing it directly to `http.get()`.
    """
    content_html = _fixture("content_movie.html")
    player_html = _fixture("player_movie.html")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://klonua.com/filmy/11719-duna-chastyna-druga.html"
        ).respond(200, text=content_html)
        # The upstream strips `?multivoice` before fetching the player
        # page; the bare path is what we mock here.
        router.get("https://ashdi.vip/vod/125331").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await KlonTVProvider().stream(
                "films/11719-duna-chastyna-druga:__movie__", None, http
            )
    assert s.url.startswith("https://ashdi.vip/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    # ashdi.vip requires a Referer to serve the manifest; the upstream
    # Kotlin sets `referer = "https://tortuga.wtf/"`.
    assert s.headers.get("Referer") == "https://tortuga.wtf/"


@pytest.mark.asyncio
async def test_klontv_stream_series_resolves_episode_m3u8():
    """Two-hop stream for a series episode: content page -> player
    page (`ashdi.vip/serial/<id>`) -> playlist JSON -> per-episode
    m3u8 URL. Episode id is `<external>:s1e1` (1-based).
    """
    content_html = _fixture("content_series.html")
    player_html = _fixture("player_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://klonua.com/serialy/8431-duna.html").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/serial/6212").respond(200, text=player_html)
        async with httpx.AsyncClient() as http:
            s = await KlonTVProvider().stream(
                "series/8431-duna:s1e1", None, http
            )
    assert s.url.startswith("https://ashdi.vip/")
    assert s.url.endswith(".m3u8")
    assert s.type == "m3u8"
    assert s.headers.get("Referer") == "https://tortuga.wtf/"


@pytest.mark.asyncio
async def test_klontv_sections_lists_two():
    """Per the upstream Kotlin `mainPage` and the spec: only
    `films` and `series` are exposed (no multfilmy, multserialy,
    anime sections in the v2 contract)."""
    sections = KlonTVProvider().sections
    ids = [s.id for s in sections]
    assert ids == ["films", "series"]


@pytest.mark.asyncio
async def test_klontv_browse_unknown_section_raises():
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        with pytest.raises(ProviderError):
            await KlonTVProvider().browse("nonexistent", 1, httpx.AsyncClient())


@pytest.mark.asyncio
async def test_klontv_strip_query_param_drops_multivoice_safely():
    """Regression: `?multivoice.replace("?multivoice", "")` mangled the
    boundary cases. A naive str.replace loses the leading `?` when
    other params follow (e.g. `?multivoice&foo=bar` -> `foo=bar`) and
    leaves dangling `=1` when the param has a value
    (e.g. `?multivoice=1` -> `=1`). The new helper must produce
    URL-safe output for all three shapes."""
    from cs_uk_api.providers.klontv import _strip_query_param

    # Bare `?multivoice` — the trailing `?` collapses cleanly.
    assert _strip_query_param(
        "https://ashdi.vip/serial/6212?multivoice", "multivoice"
    ) == "https://ashdi.vip/serial/6212"
    # `?multivoice&foo=bar` — keep `?foo=bar`, do NOT lose the `?`.
    assert _strip_query_param(
        "https://ashdi.vip/serial/6212?multivoice&foo=bar", "multivoice"
    ) == "https://ashdi.vip/serial/6212?foo=bar"
    # `?multivoice=1` — empty query, no dangling `=1`.
    assert _strip_query_param(
        "https://ashdi.vip/serial/6212?multivoice=1", "multivoice"
    ) == "https://ashdi.vip/serial/6212"
    # Other params stay put when the target param is absent.
    assert _strip_query_param(
        "https://ashdi.vip/serial/6212?foo=bar", "multivoice"
    ) == "https://ashdi.vip/serial/6212?foo=bar"


@pytest.mark.asyncio
async def test_klontv_content_bad_slug_raises():
    """Regression (HIGH #2, code-reviewer): the provider string-
    interpolated the slug into a URL without re-validating it. An
    external_id like `films/../admin` would produce
    `https://klonua.com/filmy/../admin.html` — a path-traversal escape.

    The slug regex `\\d+-[a-z0-9-]+` must be enforced at the provider
    boundary so malformed inputs surface as `not_found` BEFORE any
    HTTP request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await KlonTVProvider().content("films/../admin", http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_klontv_stream_bad_slug_raises():
    """Same regression as `content`: the `stream()` partition path
    builds `content_url` from the same unvalidated slug. An invalid
    slug must raise `not_found` before any HTTP request is made."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await KlonTVProvider().stream(
                    "films/../admin:__movie__", None, http
                )
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_klontv_stream_rejects_player_redirect_to_disallowed_host():
    """The player URL comes from upstream HTML, so it must go through
    the SSRF redirect allowlist (issue #117): a player page that
    redirects to an attacker-controlled host fails closed with
    `not_found` instead of being followed."""
    from cs_uk_api.providers.base import ProviderError

    content_html = _fixture("content_series.html")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://klonua.com/serialy/8431-duna.html").respond(
            200, text=content_html
        )
        router.get("https://ashdi.vip/serial/6212").respond(
            302, headers={"Location": "https://evil.example.com/pivot"}
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await KlonTVProvider().stream(
                    "series/8431-duna:s1e1", None, http
                )
    assert exc_info.value.code == "not_found"
    assert "disallowed host" in exc_info.value.message


@pytest.mark.asyncio
async def test_klontv_select_episode_url_rejects_garbage_suffix():
    """Regression (issue #122): `s1e2garbage` must not be treated as
    `s1e2` — the suffix regex must fullmatch."""
    raw = (
        '[{"title":"Дуб","folder":[{"title":"Сезон 1","folder":['
        '{"title":"Серія 1","file":"https://ashdi.vip/vod/1.m3u8"},'
        '{"title":"Серія 2","file":"https://ashdi.vip/vod/2.m3u8"}]}]}]'
    )
    assert KlonTVProvider._select_episode_url(raw, "s1e2garbage") is None
    assert KlonTVProvider._select_episode_url(raw, "s1e2") is not None
