"""Tests for the AnimeON provider (https://animeon.club).

Issue #17, Group 3+. AnimeON is the only provider whose backend is a
pure JSON API — no HTML listing pages. The endpoints used:

  GET /api/anime?search=<q>          search (returns SafeSearchApiResponse)
  GET /api/anime?pageSize=24&pageIndex=N   "Нове аніме" main page
  GET /api/anime/seasons             seasonal list (List<LocalResult>)
  GET /api/stats/anime/<date>?withView=false  popular List<LocalResult>
  GET /api/anime/<id-or-slug>        redirect or full SafeAnimeInfoModel
  GET /api/anime/<slug>/episodes-info    per-episode titles (List<EpisodeInfo>)
  GET /api/player/<id>/translations  studios + players (SafeTranslationsResponse)
  GET /api/player/<id>/episodes?take=N&playerId=P&translationId=T&skip=K   episodes

The Moon player iframe pages host a Playerjs player whose inline
``atob("...")`` blob is XOR-ciphered via a 32-byte sliding key with a
state byte (``moonOuterDecode`` in the upstream Kotlin). The decoded
JS exposes ``var k = "<xor_key>"`` and ``_0xd("<b64>")`` calls; each
``_0xd`` blob is XOR-ciphered with that key. Both ciphers are
reimplemented in ``animeon._moon_outer_decode`` / ``_moon_decrypt``
(plain stdlib, no JS engine).

The Ashdi player's API response already carries the final ``.m3u8`` in
``episodes[].fileUrl``; the fallback ``file:'<url>'.m3u8`` regex on the
iframe page is only needed for the occasional row that ships only a
``videoUrl``. All fixtures captured live on 2026-08-01.
"""

from __future__ import annotations

import base64
import json
import pathlib
import re

import httpx
import pytest
import respx

from cs_uk_api.providers.animeon import AnimeONProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "animeon"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_animeon_provider_metadata():
    """Stable id/name and at least one section. The upstream exposes
    three ``mainPage`` slots: current season, popular, and "Нове аніме"
    — we surface those as JSON-API sections."""
    p = AnimeONProvider()
    assert p.id == "animeon"
    assert p.name == "AnimeON"
    assert "anime" in p.types
    ids = [s.id for s in p.sections]
    assert "seasons" in ids
    assert "popular" in ids
    assert "page" in ids


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_results():
    """The `/api/anime?search=<q>` endpoint returns a
    SafeSearchApiResponse whose `results` we map 1:1 to SearchResult.
    The fixture for `naruto` contains 16 ids — Naruto, Naruto Shippuden,
    Naruto movies, etc."""
    search_json = _fixture("search.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            url=re.compile(r"https://animeon\.club/api/anime\?search=.*")
        ).respond(200, text=search_json)
        async with httpx.AsyncClient() as http:
            results = await AnimeONProvider().search("naruto", http)
    assert len(results) >= 1
    assert results[0].provider == "animeon"
    assert results[0].type == "anime"
    assert "Наруто" in results[0].title


@pytest.mark.asyncio
async def test_search_extracts_external_id():
    """The search endpoint does not expose the slug; the v1 upstream
    uses the bare numeric id for everything. Our external_id mirrors
    that: ``animeon:913`` (no slug suffix)."""
    search_json = _fixture("search.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            url=re.compile(r"https://animeon\.club/api/anime\?search=.*")
        ).respond(200, text=search_json)
        async with httpx.AsyncClient() as http:
            results = await AnimeONProvider().search("naruto", http)
    ids = [r.id for r in results]
    assert "animeon:913" in ids


@pytest.mark.asyncio
async def test_search_5xx_raises_upstream_unreachable():
    """A 5xx upstream must surface as `upstream_unreachable`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get(
            url=re.compile(r"https://animeon\.club/api/anime\?search=.*")
        ).respond(503, text="")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().search("anything", http)
    assert exc.value.code == "upstream_unreachable"


@pytest.mark.asyncio
async def test_search_connection_error_raises_unreachable():
    """A network error must surface as `unreachable`, distinct from a
    5xx server-side error."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False) as router:
        router.get(url=re.compile(r"https://animeon\.club/api/anime\?search=.*")).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().search("anything", http)
    assert exc.value.code == "unreachable"


@pytest.mark.asyncio
async def test_search_encodes_special_chars():
    """Special chars (&, ?, #, space) in the search query must be
    percent-encoded so the upstream receives a single ``search``
    parameter, not an injected extra path/query fragment."""
    search_json = _fixture("search.json")
    with respx.mock(assert_all_called=True) as router:
        route = router.get(
            url=re.compile(r"https://animeon\.club/api/anime\?.*")
        ).respond(200, text=search_json)
        async with httpx.AsyncClient() as http:
            await AnimeONProvider().search("a&b?c#d e", http)
    assert route.call_count == 1
    raw_url = str(route.calls[0].request.url)
    # The raw query arrives percent-encoded; the literals must not
    # appear as extra params (no extra '?', '&b=' as a sibling key,
    # no '#d' fragment).
    assert "&b=" not in raw_url
    assert "?" not in raw_url.split("search=")[1].split("&")[0]
    assert "a%26b" in raw_url or "a%26" in raw_url
    assert "d%20e" in raw_url or "+" in raw_url.split("search=")[1]


# ---------------------------------------------------------------------------
# browse()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browse_page_returns_results():
    """The "page" section calls `/api/anime?pageSize=24&pageIndex=N`;
    page 1 returns 24 results in the captured fixture."""
    page_json = _fixture("page1.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/anime\?pageSize=24&pageIndex=1.*"
            )
        ).respond(200, text=page_json)
        async with httpx.AsyncClient() as http:
            results, has_next = await AnimeONProvider().browse("page", 1, http)
    assert len(results) == 24
    assert results[0].id == "animeon:8086"
    assert has_next is True


@pytest.mark.asyncio
async def test_browse_page2_returns_results():
    """Page 2 hits the same endpoint with `pageIndex=2`."""
    page_json = _fixture("page2.json")
    with respx.mock(assert_all_called=True) as router:
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/anime\?pageSize=24&pageIndex=2.*"
            )
        ).respond(200, text=page_json)
        async with httpx.AsyncClient() as http:
            results, has_next = await AnimeONProvider().browse("page", 2, http)
    assert len(results) >= 1
    # Page 2 is not the last page for the catalogue.
    assert has_next is True


@pytest.mark.asyncio
async def test_browse_seasons_returns_results():
    """The "seasons" section calls `/api/anime/seasons`; the fixture
    has 66 entries."""
    seasons_json = _fixture("seasons.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeon.club/api/anime/seasons").respond(
            200, text=seasons_json
        )
        async with httpx.AsyncClient() as http:
            results, has_next = await AnimeONProvider().browse("seasons", 1, http)
    assert len(results) == 66
    assert has_next is False


@pytest.mark.asyncio
async def test_browse_unknown_section_raises_not_found():
    """An unknown section must surface as `not_found` before any HTTP
    request is made — the upstream does the same (returns an empty
    page)."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().browse("nonexistent", 1, http)
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------------
# content()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_parses_title_description_and_translations():
    """content() must resolve the bare-id redirect first, then fetch
    the canonical /api/anime/<slug> JSON, then list translations.
    Narutō's fixtures expose three studios: QTV AI Remaster (Moon),
    QTV (Ashdi), Sweet Sound Studio (Ashdi). The fixture exports
    only episode 1 + 2 from the Ashdi player and episode 1 from the
    Moon player; both player endpoints must be mocked for the test
    to be `assert_all_called=True`."""
    content_json = _fixture("content.json")
    redirect_json = _fixture("content_redirect.json")
    translations_json = _fixture("translations.json")
    episodes_ashdi_json = _fixture("episodes_ashdi.json")
    episodes_moon_json = _fixture("episodes_moon.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeon.club/api/anime/913").respond(
            200, text=redirect_json
        )
        router.get("https://animeon.club/api/anime/913-naruto").respond(
            200, text=content_json
        )
        router.get("https://animeon.club/api/player/913/translations").respond(
            200, text=translations_json
        )
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/913/episodes\?.*playerId=1052.*"
            )
        ).respond(200, text=episodes_ashdi_json)
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/913/episodes\?.*playerId=3847.*"
            )
        ).respond(200, text=episodes_moon_json)
        async with httpx.AsyncClient() as http:
            c = await AnimeONProvider().content("913", http)
    assert c.id == "animeon:913"
    assert c.type == "anime"
    assert c.title == "Наруто"
    assert c.poster is not None
    assert c.poster.startswith("https://animeon.club/api/uploads/images/")
    # Description must be a non-empty Ukrainian string.
    assert c.description
    # Per-episode translations because each translation is a separate
    # studio and the JSON gives one player per translation.
    assert c.translations_level == "episode"
    assert c.seasons is not None
    assert len(c.seasons) == 1
    first = c.seasons[0].episodes[0]
    assert first.number == 1
    assert first.id.startswith("913:e1:")
    # The translation list should include all three studios (labels
    # come straight from the JSON `translation.name` field).
    labels = [t.id for t in first.translations or []]
    assert "QTV" in labels
    assert "QTV AI Remaster" in labels
    assert "Sweet Sound Studio" in labels


@pytest.mark.asyncio
async def test_content_movie_returns_movie_without_seasons():
    """Movies (`type: "movie"` on the info JSON) carry no episode list
    upstream; detail must render as a Movie card instead of 404ing.
    Fixtures captured live 2026-08-08 (Плюпен III: Перший)."""
    redirect_json = _fixture("movie_redirect.json")
    movie_json = _fixture("movie.json")
    translations_json = _fixture("movie_translations.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeon.club/api/anime/8100").respond(
            200, text=redirect_json
        )
        router.get("https://animeon.club/api/anime/8100-lyupen-iii-pershyy").respond(
            200, text=movie_json
        )
        router.get("https://animeon.club/api/player/8100/translations").respond(
            200, text=translations_json
        )
        async with httpx.AsyncClient() as http:
            c = await AnimeONProvider().content("8100", http)
    assert c.type == "movie"
    assert c.seasons is None
    assert c.translations_level == "content"
    assert c.title == "Люпен III: Перший"
    assert [t.id for t in c.translations] == ["AlsikUA"]
    assert c.poster.startswith("https://animeon.club/api/uploads/images/")


@pytest.mark.asyncio
async def test_stream_movie_resolves_direct_source():
    """Movies stream by bare id. The episode walk comes up empty (as
    it does live), so the provider must fall back to the direct player
    endpoint `/api/player/<playerId>/<translationId>` and resolve the
    Ashdi iframe (upstream `loadMovieLinks`). Regression (issue #115):
    previously a bare id hit the 3-part episode-id check and raised
    `not_found bad content_id` on every movie."""
    from cs_uk_api.providers.base import ProviderError

    translations_json = _fixture("movie_translations.json")
    direct_json = _fixture("movie_direct.json")
    ashdi_html = _fixture("player_ashdi_movie.html")
    empty_eps = '{"episodes":[],"anotherPlayer":null}'
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeon.club/api/player/8100/translations").respond(
            200, text=translations_json
        )
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/8100/episodes\?.*playerId=8293.*"
            )
        ).respond(200, text=empty_eps)
        router.get("https://animeon.club/api/player/8293/1793").respond(
            200, text=direct_json
        )
        router.get(
            "https://ashdi.vip/vod/276624?player=animeon.club",
            headers={
                "Referer": "https://animeon.club/",
                "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36",
            },
        ).respond(200, text=ashdi_html)
        async with httpx.AsyncClient() as http:
            s = await AnimeONProvider().stream("8100", None, http)
    assert s.url.startswith("https://ashdi.vip/video08/3/new/gotovo_lyupen_iii_pershij_276624/")
    assert s.url.endswith("/index.m3u8")
    assert s.type == "m3u8"
    assert s.headers["Referer"] == "https://ashdi.vip/"


@pytest.mark.asyncio
async def test_stream_movie_explicit_movie_suffix():
    """The `:__movie__` suffix form must be accepted too — some clients
    hand over the explicit suffix rather than the bare search id."""
    translations_json = _fixture("movie_translations.json")
    direct_json = _fixture("movie_direct.json")
    ashdi_html = _fixture("player_ashdi_movie.html")
    empty_eps = '{"episodes":[],"anotherPlayer":null}'
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeon.club/api/player/8100/translations").respond(
            200, text=translations_json
        )
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/8100/episodes\?.*playerId=8293.*"
            )
        ).respond(200, text=empty_eps)
        router.get("https://animeon.club/api/player/8293/1793").respond(
            200, text=direct_json
        )
        router.get("https://ashdi.vip/vod/276624?player=animeon.club").respond(
            200, text=ashdi_html
        )
        async with httpx.AsyncClient() as http:
            s = await AnimeONProvider().stream("8100:__movie__", None, http)
    assert s.url.endswith("/index.m3u8")


@pytest.mark.asyncio
async def test_stream_movie_named_translation_missing_raises():
    """A translation the movie doesn't offer must surface as
    `translation_missing`, not silently play the first one."""
    from cs_uk_api.providers.base import ProviderError

    translations_json = _fixture("movie_translations.json")
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeon.club/api/player/8100/translations").respond(
            200, text=translations_json
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().stream("8100", "No Such Studio", http)
    assert exc.value.code == "translation_missing"


@pytest.mark.asyncio
async def test_stream_movie_bad_id_raises_not_found():
    """A bare id that isn't a pure integer must surface as `not_found`
    before any HTTP request."""
    from cs_uk_api.providers.base import ProviderError

    for bad in ["../admin", "913-extra", ""]:
        with respx.mock(assert_all_called=False):
            async with httpx.AsyncClient() as http:
                with pytest.raises(ProviderError) as exc:
                    await AnimeONProvider().stream(bad, None, http)
        assert exc.value.code == "not_found", f"unexpected: {bad!r}"


@pytest.mark.asyncio
async def test_content_bad_external_id_raises_not_found():
    """Defensive: anything that isn't a pure integer must surface as
    `not_found` before any HTTP request is made — same security
    boundary as the other providers."""
    from cs_uk_api.providers.base import ProviderError

    for bad in ["../admin", "913-extra", "abc", "", "913/path"]:
        with respx.mock(assert_all_called=False):
            async with httpx.AsyncClient() as http:
                with pytest.raises(ProviderError) as exc:
                    await AnimeONProvider().content(bad, http)
        assert exc.value.code == "not_found", f"unexpected: {bad!r}"


@pytest.mark.asyncio
async def test_content_non_200_raises_not_found():
    """A 404 from the redirect endpoint must surface as `not_found`."""
    from cs_uk_api.providers.base import ProviderError

    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeon.club/api/anime/913").respond(404, text="")
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().content("913", http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_content_missing_title_raises_parse_failed():
    """A redirect to a slug whose main JSON omits `titleUa` must
    surface as `parse_failed`."""
    from cs_uk_api.providers.base import ProviderError

    redirect_json = _fixture("content_redirect.json")
    bad_content = json.dumps({"id": 913, "episodes": 220})  # no titleUa
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeon.club/api/anime/913").respond(
            200, text=redirect_json
        )
        router.get("https://animeon.club/api/anime/913-naruto").respond(
            200, text=bad_content
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().content("913", http)
    assert exc.value.code == "parse_failed"


@pytest.mark.asyncio
async def test_content_bad_redirect_slug_raises_not_found():
    """A malicious redirect ``slug`` (path traversal, scheme injection)
    must be rejected with ``not_found`` before we build the next URL.
    ``assert_all_called=True`` ensures no follow-up GET was issued."""
    from cs_uk_api.providers.base import ProviderError

    bad_redirect = json.dumps({"moved": True, "slug": "../../etc/passwd"})
    with respx.mock(assert_all_called=True) as router:
        router.get("https://animeon.club/api/anime/913").respond(
            200, text=bad_redirect
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().content("913", http)
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_content_specials_5xx_propagates_upstream_unreachable():
    """A 5xx on the optional ``skip=-1`` specials fetch must surface
    as ``upstream_unreachable`` — not be silently swallowed and
    misattributed to ``parse_failed`` further down the call stack."""
    from cs_uk_api.providers.base import ProviderError

    redirect_json = _fixture("content_redirect.json")
    content_json = _fixture("content.json")
    translations_json = _fixture("translations.json")
    episodes_ashdi_json = _fixture("episodes_ashdi.json")
    episodes_moon_json = _fixture("episodes_moon.json")
    with respx.mock(assert_all_called=False) as router:
        router.get("https://animeon.club/api/anime/913").respond(
            200, text=redirect_json
        )
        router.get("https://animeon.club/api/anime/913-naruto").respond(
            200, text=content_json
        )
        router.get("https://animeon.club/api/player/913/translations").respond(
            200, text=translations_json
        )
        # Moon (playerId=3847) gets the 5xx on its specials fetch.
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/913/episodes\?.*playerId=3847.*skip=-1.*"
            )
        ).respond(503, text="")
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/913/episodes\?.*playerId=3847.*skip=0.*"
            )
        ).respond(200, text=episodes_moon_json)
        # Ashdi behaves normally — both specials and the paginated
        # walk succeed so the test stays isolated to the Moon player.
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/913/episodes\?.*playerId=1052.*"
            )
        ).respond(200, text=episodes_ashdi_json)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().content("913", http)
    assert exc.value.code == "upstream_unreachable"


@pytest.mark.asyncio
async def test_content_specials_4xx_is_swallowed_silently():
    """A 4xx on the optional ``skip=-1`` specials fetch means "no
    specials for this show" — that is a valid empty state and the
    caller must not see an error."""
    redirect_json = _fixture("content_redirect.json")
    content_json = _fixture("content.json")
    translations_json = _fixture("translations.json")
    episodes_ashdi_json = _fixture("episodes_ashdi.json")
    episodes_moon_json = _fixture("episodes_moon.json")
    with respx.mock(assert_all_called=False) as router:
        router.get("https://animeon.club/api/anime/913").respond(
            200, text=redirect_json
        )
        router.get("https://animeon.club/api/anime/913-naruto").respond(
            200, text=content_json
        )
        router.get("https://animeon.club/api/player/913/translations").respond(
            200, text=translations_json
        )
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/913/episodes\?.*playerId=3847.*skip=-1.*"
            )
        ).respond(404, text="")
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/913/episodes\?.*playerId=3847.*skip=0.*"
            )
        ).respond(200, text=episodes_moon_json)
        router.get(
            url=re.compile(
                r"https://animeon\.club/api/player/913/episodes\?.*playerId=1052.*"
            )
        ).respond(200, text=episodes_ashdi_json)
        async with httpx.AsyncClient() as http:
            c = await AnimeONProvider().content("913", http)
    assert c.id == "animeon:913"
    assert c.seasons is not None
    assert len(c.seasons[0].episodes) >= 1


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_ashdi_returns_m3u8():
    """For the QTV Ashdi player, the API returns a `fileUrl` whose
    ``.m3u8`` is already final — no iframe fetch, no decoding. The
    header must carry the upstream ashdi.vip Referer so the manifest
    is actually served."""
    # Build the encoded Episode.id from the same source list the
    # provider would assemble for the QTV Ashdi entry of Narutō e1.
    ep_blob = json.dumps(
        {
            "id": 913,
            "episode": 1,
            "sources": [
                {
                    "translation_name": "QTV",
                    "player_name": "Ashdi",
                    "video_url": "https://ashdi.vip/vod/221709",
                    "file_url": (
                        "https://ashdi.vip/video16/1/new/s01e01_221709"
                        "/hls/AK6Xi3CHjuJZhA78Ag==/index.m3u8"
                    ),
                }
            ],
        },
        separators=(",", ":"),
    )
    encoded_ep_id = (
        f"913:e1:{base64.b64encode(ep_blob.encode('utf-8')).decode('ascii')}"
    )
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            s = await AnimeONProvider().stream(encoded_ep_id, "QTV", http)
    assert s.type == "m3u8"
    assert s.url.startswith("https://ashdi.vip/")
    assert s.url.endswith(".m3u8")
    assert s.headers["Referer"].startswith("https://ashdi.vip")


@pytest.mark.asyncio
async def test_stream_moon_decodes_iframe_to_m3u8():
    """For the QTV AI Remaster Moon player, `fileUrl` is empty so we
    have to fetch the iframe HTML, run the ``moonOuterDecode`` XOR on
    the ``atob("...")`` blob, extract the JS, and apply the inner
    ``_0xd(...)`` decode to reach the `.m3u8`. The captured iframe
    fixture's first `_0xd` blob is the Narutō ep1 manifest URL."""
    player_html = _fixture("player_moon.html")
    # stream() needs a content() episode blob to encode the per-episode
    # sources. We construct one by hand: episode 1's Moon iframe URL.
    ep_blob = json.dumps(
        {
            "id": 913,
            "episode": 1,
            "sources": [
                {
                    "translation_name": "QTV AI Remaster",
                    "player_name": "Moon",
                    "video_url": "https://moonanime.art/iframe/kydhteltajoouemnv/",
                    "file_url": "",
                }
            ],
        },
        separators=(",", ":"),
    )
    encoded_ep_id = (
        f"913:e1:{base64.b64encode(ep_blob.encode('utf-8')).decode('ascii')}"
    )
    with respx.mock(assert_all_called=True) as router:
        router.get(url=re.compile(r"https://moonanime\.art/iframe/.*")).respond(
            200, text=player_html
        )
        async with httpx.AsyncClient() as http:
            s = await AnimeONProvider().stream(encoded_ep_id, "QTV AI Remaster", http)
    assert s.type == "m3u8"
    assert "s.moonanime.art/content" in s.url
    assert "manifest.m3u8" in s.url
    assert s.headers["Referer"] == "https://moonanime.art/"


@pytest.mark.asyncio
async def test_stream_unknown_translation_raises_translation_missing():
    """If the requested translation is not in the encoded source list,
    we surface ``translation_missing`` (per v2 spec → HTTP 404) rather
    than silently picking a different studio."""
    from cs_uk_api.providers.base import ProviderError

    ep_blob = json.dumps(
        {
            "id": 913,
            "episode": 1,
            "sources": [
                {
                    "translation_name": "QTV",
                    "player_name": "Ashdi",
                    "video_url": "https://ashdi.vip/vod/221709",
                    "file_url": "https://ashdi.vip/foo/index.m3u8",
                }
            ],
        },
        separators=(",", ":"),
    )
    encoded_ep_id = (
        f"913:e1:{base64.b64encode(ep_blob.encode('utf-8')).decode('ascii')}"
    )
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().stream(encoded_ep_id, "NonexistentStudio", http)
    assert exc.value.code == "translation_missing"


@pytest.mark.asyncio
async def test_stream_bad_external_id_raises_not_found():
    """Anything that doesn't fit ``<int>:e<int>`` must surface as
    ``not_found`` before any HTTP request is made."""
    from cs_uk_api.providers.base import ProviderError

    for bad in ["../admin:e1", "913:bogus", "913-e1", "e1", ""]:
        with respx.mock(assert_all_called=False):
            async with httpx.AsyncClient() as http:
                with pytest.raises(ProviderError) as exc:
                    await AnimeONProvider().stream(bad, "QTV", http)
        assert exc.value.code == "not_found", f"unexpected: {bad!r}"


@pytest.mark.asyncio
async def test_stream_upstream_error_raises_unreachable():
    """A 5xx from the iframe fetch must surface as ``unreachable`` /
    ``upstream_unreachable`` (the player CDN is the upstream)."""
    from cs_uk_api.providers.base import ProviderError

    ep_blob = json.dumps(
        {
            "id": 913,
            "episode": 1,
            "sources": [
                {
                    "translation_name": "QTV AI Remaster",
                    "player_name": "Moon",
                    "video_url": "https://moonanime.art/iframe/kydhteltajoouemnv/",
                    "file_url": "",
                }
            ],
        },
        separators=(",", ":"),
    )
    encoded_ep_id = (
        f"913:e1:{base64.b64encode(ep_blob.encode('utf-8')).decode('ascii')}"
    )
    with respx.mock(assert_all_called=True) as router:
        router.get(url=re.compile(r"https://moonanime\.art/iframe/.*")).respond(
            503, text=""
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await AnimeONProvider().stream(encoded_ep_id, "QTV AI Remaster", http)
    assert exc.value.code in {"unreachable", "upstream_unreachable"}


# ---------------------------------------------------------------------------
# Decode primitives (pure functions, no upstream)
# ---------------------------------------------------------------------------


def test_moon_outer_decode_reproduces_upstream_kotlin():
    """The XOR cipher used by MoonAnime's Playerjs embeds a 1-byte
    state + 32-byte key in the first 33 bytes of the base64-decoded
    blob; data follows and is XORed against ``key[i%32] ^ state``
    with ``state = (data + key) & 0xFF`` after each byte. The first
    252 decoded bytes of the Narutō ep1 iframe start with
    ``b'\\n\\t\\t\\tlet  player_url, referrer, vid'``."""
    from cs_uk_api.providers.animeon import _moon_outer_decode

    # First 168 base64 chars of the Narutō ep1 player iframe (3
    # base64 quartets = 189 bytes of data after the 33-byte header).
    blob = (
        "H02mzuP+V+Tsn7CZuvsuLbgM2UhtssA6o5JRergnl7kwWAp3rwDMs1v4V/JQiMipMZA3ND04WEdE"
        "EIHaiRSAjgEVoO03u5tkyBAVVV3luKITohjmNmO5YwMUk/GwOpvu+RDV2SuTp27XMwwLdrn/ZhDO"
    )
    head = _moon_outer_decode(blob)[:32]
    assert head.startswith(b"\n\t\t\tlet  player_url")


def test_moon_decrypt_reproduces_upstream_kotlin():
    """The inner cipher takes a base64 blob, decodes it, and XORs
    every byte against ``k[i % len(k)]``. The Narutō ep1 iframe
    has XOR key ``YsElwOFSpj7n`` and the first decoded `_0xd`
    payload is the MoonAnime manifest ending in ``manifest.m3u8``."""
    from cs_uk_api.providers.animeon import _moon_decrypt

    inner = (
        "MQcxHAR1aXwDRFoBNh0kAh4iI30RGENBOhwrGBIhMnwDHkULOB5qDRkmKzZfWwBBMgohBAMqKicR"
        "AFgBLBYoAgFgLj8DRUEHPRYqVhkuNCYEBWgLNwcgHighJyEFHlgxLAkkARYkLwwHC14ILCw9Mw8Q"
        "KzgGNURfBiwgXSgeEgUvK34xCxYoDQQ7IyEvXw5aa1wtAAR1KzIeA1ELKgdrAUQ6fmwVEkcHKxY2"
        "UUZ4fmZGWwJdbUdjHx4oe2QRUgRbYEd9WkMscDUSCw4="
    )
    decoded = _moon_decrypt(inner, "YsElwOFSpj7n")
    assert decoded.startswith("https://s.moonanime.art/content/")
    assert "manifest.m3u8" in decoded
