import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.uakino import UakinoProvider

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "uakino"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeSession:
    """Replaces the browser session: serves fixture bodies per path prefix."""

    def __init__(self, **routes: tuple[int, str]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, str | None]] = []

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
    assert movie.type == "movie"
    assert movie.year == 2021
    assert movie.poster is not None
    assert movie.poster.startswith("https://uakino.best/")
    series = next(r for r in results if r.url.endswith("seriesss/10458-dyuna-1-sezon.html"))
    assert series.type == "series"
    series2 = next(r for r in results if r.url.endswith("seriesss/drama_series/24872-duna-proroctvo-1-sezon.html"))
    assert series2.type == "series"
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

    assert c.type == "movie"
    assert c.title == "Дюна"
    assert c.year == 2021
    assert c.description
    assert c.poster is not None and c.poster.startswith("https://uakino.best/")
    assert c.seasons is None
    assert [(t.id, t.label) for t in c.translations] == [
        ("Postmodern", "Postmodern"),
        ("DniproFilm", "DniproFilm"),
    ]


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

    assert c.type == "series"
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
    assert c.type == "series"


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
    assert all(r.type == "movie" for r in results)


@pytest.mark.asyncio
async def test_uakino_browse_unknown_section_raises():
    with pytest.raises(ProviderError):
        await UakinoProvider(session=FakeSession()).browse(
            "nonexistent", 1, httpx.AsyncClient()
        )
