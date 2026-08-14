import asyncio
import pathlib
from collections.abc import Callable

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.uakino import UakinoProvider

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "uakino"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeSession:
    """Replaces the browser session: serves fixture bodies per path prefix.

    Implements the extended ``UakinoSessionProtocol`` (issue #194) so the
    adapter stays usable once the session gains warm/ready_event/heartbeat.
    """

    def __init__(self, **routes: tuple[int, str]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, str | None]] = []
        self._ready = asyncio.Event()

    @property
    def ready_event(self) -> asyncio.Event:
        return self._ready

    async def warm(self) -> None:
        self._ready.set()

    async def heartbeat_loop(self, record: Callable[[bool], None]) -> None:
        raise AssertionError("FakeSession has no heartbeat loop")

    async def fetch(
        self, path: str, method: str = "GET", data: str | None = None
    ) -> tuple[int, str]:
        self.calls.append((method, path, data))
        for prefix, (status, text) in self.routes.items():
            if path.startswith(prefix):
                return status, text
        raise AssertionError(f"unexpected fetch {method} {path}")

    async def close(self) -> None:
        pass


def _provider(session: FakeSession) -> UakinoProvider:
    return UakinoProvider(session=session)


# --------------------------------------------------------------------------
# search()
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uakino_search_parses_results_and_filters_junk():
    session = FakeSession(**{"/index.php": (200, _fixture("search_results.html"))})
    async with httpx.AsyncClient(headers={"User-Agent": "test"}) as http:
        results = await _provider(session).search("дюна", http)

    assert len(results) == 5
    assert all(r.id.startswith("uakino:") for r in results)
    # news / franchise / anonsi cards must be dropped
    assert all("news" not in r.url and "franchise" not in r.url for r in results)
    # the Дюна movie (2021) and both Дюна series are present
    movie = next(r for r in results if r.url.endswith("genre-action/12567-dyuna.html"))
    assert movie.form == "movie"
    assert movie.styles == frozenset()
    assert movie.year == 2021
    assert movie.poster is not None
    assert movie.poster.startswith("https://uakino.best/")
    series = next(r for r in results if r.url.endswith("seriesss/10458-dyuna-1-sezon.html"))
    assert series.form == "series"
    assert series.styles == frozenset()
    series2 = next(r for r in results if r.url.endswith("seriesss/drama_series/24872-duna-proroctvo-1-sezon.html"))
    assert series2.form == "series"
    # search must be a same-origin POST to /index.php
    method, path, data = session.calls[0]
    assert method == "POST" and path == "/index.php"
    assert "story=" in (data or "")


@pytest.mark.asyncio
async def test_uakino_search_upstream_error_raises():
    session = FakeSession(**{"/index.php": (403, "Just a moment...")})
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderError) as exc:
            await _provider(session).search("дюна", http)
    assert exc.value.code == "upstream_unreachable"


# --------------------------------------------------------------------------
# content()
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uakino_content_movie_parses_metadata_and_voices():
    session = FakeSession(
        **{
            "/filmy/12567-dyuna.html": (200, _fixture("content_movie.html")),
            "/engine/ajax/playlists.php": (200, _fixture("playlists_movie.json")),
        }
    )
    async with httpx.AsyncClient() as http:
        c = await _provider(session).content("filmy:12567-dyuna", http)

    assert c.form == "movie"
    assert c.title == "Дюна"
    assert c.year == 2021
    assert c.description
    assert c.poster is not None and c.poster.startswith("https://uakino.best/")
    assert c.seasons is None


@pytest.mark.asyncio
async def test_uakino_content_parses_director_and_cast():
    """The `.fi-item` rows carry Режисер and Актори with real links,
    but the parser only read Рік/Жанр/Країна — every uakino detail
    showed an empty People rail (Ticket #229)."""
    session = FakeSession(
        **{
            "/filmy/12567-dyuna.html": (200, _fixture("content_movie.html")),
            "/engine/ajax/playlists.php": (200, _fixture("playlists_movie.json")),
        }
    )
    async with httpx.AsyncClient() as http:
        c = await _provider(session).content("filmy:12567-dyuna", http)
    actors = [p for p in c.people if p.role == "Actor"]
    directors = [p for p in c.people if p.role == "Director"]
    assert len(directors) == 1
    assert directors[0].name == "Денні Вільньов"
    assert len(actors) == 15
    assert actors[0].name == "Ребекка Ферґюсон"
    assert all(p.id.startswith("uakino:") for p in c.people)
    # The Жанр fi-item row is parsed into tags — it must surface as
    # genres (the data is on the page but was dropped).
    assert c.genres
    assert "Фантастика" in c.genres or "фантастика" in c.genres
    # The label-less fi-item carries an IMDb-style `8.0/1118360`
    # score — it must surface as rating (Ticket #231).
    assert c.rating == 8.0
    assert [(t.id, t.label) for t in c.translations] == [
        ("Postmodern", "Postmodern"),
        ("DniproFilm", "DniproFilm"),
    ]


@pytest.mark.asyncio
async def test_uakino_content_parses_metadata_from_suffixed_fi_item_rows():
    """The upstream template renamed the metadata rows from
    ``fi-item clearfix`` to ``fi-item-s clearfix`` (seen live on
    anime-series pages). The parser's ``div.fi-item`` selector matched
    nothing, so year/genres/rating/people silently went empty on every
    page using the new class. Both spellings must parse."""
    html = _fixture("content_movie.html").replace(
        'class="fi-item clearfix"', 'class="fi-item-s clearfix"'
    )
    session = FakeSession(
        **{
            "/filmy/12567-dyuna.html": (200, html),
            "/engine/ajax/playlists.php": (200, _fixture("playlists_movie.json")),
        }
    )
    async with httpx.AsyncClient() as http:
        c = await _provider(session).content("filmy:12567-dyuna", http)

    assert c.year == 2021
    assert c.rating == 8.0
    assert c.genres
    assert "Фантастика" in c.genres or "фантастика" in c.genres
    actors = [p for p in c.people if p.role == "Actor"]
    directors = [p for p in c.people if p.role == "Director"]
    assert len(directors) == 1
    assert directors[0].name == "Денні Вільньов"
    assert len(actors) == 15


@pytest.mark.asyncio
async def test_uakino_content_excludes_section_from_genres():
    """The Жанр row on live uakino pages opens with the SECTION name
    (e.g. `Серіали , Драма , Пригоди , Фантастика` on a series page) —
    a section is not a genre and must be filtered out so the genre row
    doesn't render «Серіали»."""
    html = _fixture("content_movie.html").replace(
        '<a href="https://uakino.best/filmy/genre-action/">Екшн</a> , '
        '<a href="https://uakino.best/filmy/genre_adventure/">Пригоди</a>',
        '<a href="https://uakino.best/seriesss/">Серіали</a> , '
        '<a href="https://uakino.best/filmy/genre_adventure/">Пригоди</a>',
    )
    session = FakeSession(
        **{
            "/seriesss/12567-dyuna.html": (200, html),
            "/engine/ajax/playlists.php": (200, _fixture("playlists_series.json")),
        }
    )
    async with httpx.AsyncClient() as http:
        c = await _provider(session).content("seriesss:12567-dyuna", http)
    assert "Серіали" not in c.genres
    assert "Пригоди" in c.genres


@pytest.mark.asyncio
async def test_uakino_content_movie_without_voice_uses_default_translation():
    """Regression (issue #123, D2): a movie whose playlist rows carry no
    `data-voice` used to produce an empty translations list, which the
    ContentResponse model (min_length=1) rejected with a 500. It must
    surface a playable default translation instead."""
    session = FakeSession(
        **{
            "/filmy/12567-dyuna.html": (200, _fixture("content_movie.html")),
            "/engine/ajax/playlists.php": (
                200,
                _fixture("playlists_movie_no_voice.json"),
            ),
        }
    )
    async with httpx.AsyncClient() as http:
        c = await _provider(session).content("filmy:12567-dyuna", http)
    assert c.form == "movie"
    assert c.seasons is None
    assert [(t.id, t.label) for t in c.translations] == [("uk", "Українська")]


@pytest.mark.asyncio
async def test_uakino_stream_movie_default_uk_falls_back_to_first_file():
    """The synthetic "uk" translation (a movie without voice labels)
    must not surface `translation_missing` when a client streams with
    `?translation=uk` — it falls back to the first playable file."""
    session = FakeSession(
        **{
            "/engine/ajax/playlists.php": (
                200,
                _fixture("playlists_movie_no_voice.json"),
            )
        }
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ashdi.vip/vod/89434").respond(
            200, text=_fixture("stream_ashdi_movie.html")
        )
        async with httpx.AsyncClient() as http:
            s = await _provider(session).stream(
                "filmy:12567-dyuna", "uk", http
            )
    assert s.url == M3U8_MOVIE


ERR_NOT_DATA = '{"success":false,"message":"ERR_NOT_DATA"}'


@pytest.mark.asyncio
async def test_uakino_content_movie_direct_player_not_gated():
    """Regression: uakino moved movie pages off playlists.php to a
    direct player iframe (`ashdi.vip/vod/<id>`); playlists.php answers
    ERR_NOT_DATA. Such a movie must surface as playable (default uk
    translation), not be gated as a dead card."""
    session = FakeSession(
        **{
            "/filmy/12567-dyuna.html": (200, _fixture("content_movie.html")),
            "/engine/ajax/playlists.php": (200, ERR_NOT_DATA),
        }
    )
    async with httpx.AsyncClient() as http:
        c = await _provider(session).content("filmy:12567-dyuna", http)
    assert c.form == "movie"
    assert c.seasons is None
    assert [(t.id, t.label) for t in c.translations] == [("uk", "Українська")]


@pytest.mark.asyncio
async def test_uakino_content_movie_empty_playlists_no_player_gated():
    """Regression (ADR-0002): a movie whose playlists response is empty
    AND whose page has no direct player iframe is a dead card —
    content() gates it so the catalog sweep drops it."""
    page = "<html><body><h1><span class='solototle'>Дюна</span></h1></body></html>"
    session = FakeSession(
        **{
            "/filmy/12567-dyuna.html": (200, page),
            "/engine/ajax/playlists.php": (200, ERR_NOT_DATA),
        }
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ProviderError) as exc:
            await _provider(session).content("filmy:12567-dyuna", http)
    assert exc.value.code == "gated"


@pytest.mark.asyncio
async def test_uakino_content_series_parses_episodes_and_voice_groups():
    session = FakeSession(
        **{
            "/seriesss/35253-kramnycia-dlia-vbyvc-2-sezon.html": (
                200,
                _fixture("content_series.html"),
            ),
            "/engine/ajax/playlists.php": (200, _fixture("playlists_series.json")),
        }
    )
    async with httpx.AsyncClient() as http:
        c = await _provider(session).content(
            "seriesss:35253-kramnycia-dlia-vbyvc-2-sezon", http
        )

    assert c.form == "series"
    # Model B axes: a plain seriesss item → form=series, no style tag.
    assert c.form == "series"
    assert c.styles == frozenset()
    assert c.title == "Крамниця для вбивць 2 сезон"
    assert c.translations_level == "episode"
    assert c.seasons is not None and len(c.seasons) == 1
    episodes = c.seasons[0].episodes
    assert [e.number for e in episodes] == [1, 2, 3, 4]
    assert [e.title for e in episodes] == ["Серія 1", "Серія 2", "Серія 3", "Серія 4"]
    assert [e.id for e in episodes] == [
        "uakino:35253:e1",
        "uakino:35253:e2",
        "uakino:35253:e3",
        "uakino:35253:e4",
    ]
    assert all((e.translations or [])[0].id == "Yaniam" for e in episodes)


@pytest.mark.asyncio
async def test_uakino_content_accepts_legacy_kind_prefixed_ids():
    session = FakeSession(
        **{
            "/seriesss/35253-kramnycia-dlia-vbyvc-2-sezon.html": (
                200,
                _fixture("content_series.html"),
            ),
            "/engine/ajax/playlists.php": (200, _fixture("playlists_series.json")),
        }
    )
    async with httpx.AsyncClient() as http:
        c = await _provider(session).content("serial-35253-kramnycia-dlia-vbyvc-2-sezon", http)
    assert c.form == "series"


@pytest.mark.asyncio
async def test_uakino_content_bad_external_id_raises_not_found():
    """Regression: `content()` must reject anything that escapes the
    `<section>:<id>-<slug>` shape before interpolating into the URL."""
    with pytest.raises(ProviderError) as exc:
        await UakinoProvider(session=FakeSession()).content("../../etc/passwd", httpx.AsyncClient())
    assert exc.value.code == "not_found"


# --------------------------------------------------------------------------
# stream()
# --------------------------------------------------------------------------

M3U8_MOVIE = "https://ashdi.vip/video02/1/films/dune._part_one_2021_uhdbdrip_1080p_h.265_2xukr_eng_hurtom_89434/hls/Da+Xjn6RkuZVhAb3/index.m3u8"
M3U8_EP1 = "https://ashdi.vip/video02/3/new/a.shop.for.killers.s02e01_273102/hls/Da+Xjn6RkuZVhAb3/index.m3u8"


@pytest.mark.asyncio
async def test_uakino_stream_movie_direct_player_resolves_m3u8():
    """Regression: a movie with no playlists data (upstream moved it to
    a direct player iframe) must resolve its m3u8 from the content
    page's player iframe instead of failing `no playable voices`."""
    session = FakeSession(
        **{
            "/filmy/12567-dyuna.html": (200, _fixture("content_movie.html")),
            "/engine/ajax/playlists.php": (200, ERR_NOT_DATA),
        }
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ashdi.vip/vod/89434").respond(
            200, text=_fixture("stream_ashdi_movie.html")
        )
        async with httpx.AsyncClient() as http:
            s = await _provider(session).stream("filmy:12567-dyuna", None, http)
    assert s.url == M3U8_MOVIE


@pytest.mark.asyncio
async def test_uakino_stream_series_episode_resolves_m3u8():
    session = FakeSession(
        **{"/engine/ajax/playlists.php": (200, _fixture("playlists_series.json"))}
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ashdi.vip/vod/273102").respond(
            200, text=_fixture("stream_ashdi_series.html")
        )
        async with httpx.AsyncClient() as http:
            s = await _provider(session).stream("35253:e1", "Yaniam", http)
    assert s.url == M3U8_EP1
    assert s.type == "m3u8"
    assert s.headers.get("Referer") == "https://ashdi.vip/"


@pytest.mark.asyncio
async def test_uakino_stream_movie_voice_resolves_m3u8():
    session = FakeSession(
        **{"/engine/ajax/playlists.php": (200, _fixture("playlists_movie.json"))}
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ashdi.vip/vod/89434").respond(
            200, text=_fixture("stream_ashdi_movie.html")
        )
        async with httpx.AsyncClient() as http:
            s = await _provider(session).stream("filmy:12567-dyuna:__movie__", "Postmodern", http)
    assert s.url == M3U8_MOVIE


@pytest.mark.asyncio
async def test_uakino_stream_movie_defaults_to_first_voice():
    session = FakeSession(
        **{"/engine/ajax/playlists.php": (200, _fixture("playlists_movie.json"))}
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ashdi.vip/vod/89434").respond(
            200, text=_fixture("stream_ashdi_movie.html")
        )
        async with httpx.AsyncClient() as http:
            s = await _provider(session).stream("filmy:12567-dyuna", None, http)
    assert s.url == M3U8_MOVIE


@pytest.mark.asyncio
async def test_uakino_stream_unknown_voice_raises_translation_missing():
    session = FakeSession(
        **{"/engine/ajax/playlists.php": (200, _fixture("playlists_movie.json"))}
    )
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await _provider(session).stream("filmy:12567-dyuna", "NoSuchVoice", http)
    assert exc.value.code == "translation_missing"


@pytest.mark.asyncio
async def test_uakino_stream_bad_content_id_raises_not_found():
    """Regression: `stream()` must not interpolate attacker-controlled
    paths into any URL."""
    with pytest.raises(ProviderError) as exc:
        await UakinoProvider(session=FakeSession()).stream("../../etc/passwd", None, httpx.AsyncClient())
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_uakino_stream_rejects_off_allowlist_stream_page():
    """Regression: the stream page URL must be fetched via safe_get with
    the ashdi.vip host allowlist. A playlist whose `data-file` points
    off-host cannot pivot the backend into a fresh SSRF surface."""
    import json

    from urllib.parse import quote

    payload = json.dumps(
        {
            "success": True,
            "response": (
                '<div class="playlists-videos"><ul>'
                '<li data-file="https://evil.com/vod/1" data-voice="Uk">Uk</li>'
                "</ul></div>"
            ),
        }
    )
    session = FakeSession(
        **{f"/engine/ajax/playlists.php?news_id={quote('99999')}&xfield=playlist&time=": (200, payload)}
    )
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(Exception):
                await _provider(session).stream("99999", None, http)


@pytest.mark.asyncio
async def test_uakino_stream_rejects_off_allowlist_m3u8():
    """Regression: the m3u8 URL extracted from the stream page must
    stay on ashdi.vip — a hostile stream page cannot redirect the PS4
    to an internal address."""
    stream_page_html = (
        "<html><body><script>"
        "file:'https://evil.example/internal.m3u8'"
        "</script></body></html>"
    )
    session = FakeSession(
        **{"/engine/ajax/playlists.php": (200, _fixture("playlists_movie.json"))}
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://ashdi.vip/vod/89434").respond(200, text=stream_page_html)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc:
                await _provider(session).stream("filmy:12567-dyuna", None, http)
    assert exc.value.code == "not_found"


# --------------------------------------------------------------------------
# browse()
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uakino_browse_parses_listing_and_detects_next():
    from cs_uk_api.providers.uakino import UAKINO_SECTIONS

    filmy_id = UAKINO_SECTIONS[0].id
    session = FakeSession(**{"/filmy/": (200, _fixture("browse_filmy.html"))})
    async with httpx.AsyncClient() as http:
        results, has_next = await _provider(session).browse(filmy_id, 1, http)
    assert has_next is True
    assert len(results) == 40
    assert all(r.id.startswith("uakino:") for r in results)
    assert all(r.form == "movie" for r in results)


@pytest.mark.asyncio
async def test_uakino_browse_unknown_section_raises():
    with pytest.raises(ProviderError):
        await UakinoProvider(session=FakeSession()).browse(
            "nonexistent", 1, httpx.AsyncClient()
        )


@pytest.mark.asyncio
async def test_uakino_browse_ignores_cross_site_pagination_links():
    """Regression: pagination discovery must be host-scoped to
    uakino.best. A page that contains a hostile `<a href="https://evil.com/page/2/">`
    link must not flip `has_next` to True. The previous regex matched
    any `href` containing `/page/N/`, so a single cross-site link
    would force a phantom next page."""
    from cs_uk_api.providers.uakino import UAKINO_SECTIONS

    filmy_id = UAKINO_SECTIONS[0].id
    session = FakeSession(
        **{"/filmy/": (200, _fixture("browse_filmy_evil_pagination.html"))}
    )
    async with httpx.AsyncClient() as http:
        results, has_next = await _provider(session).browse(filmy_id, 1, http)
    assert has_next is False
    assert len(results) == 1
