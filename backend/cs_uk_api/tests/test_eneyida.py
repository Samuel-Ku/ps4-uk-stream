"""Tests for the Eneyida provider."""

from __future__ import annotations

import pathlib

import httpx
import pytest
import respx

from cs_uk_api.providers.base import ProviderError
from cs_uk_api.providers.eneyida import EneyidaProvider

FIX = pathlib.Path(__file__).parent / "fixtures" / "eneyida"


def _fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# NOTE: The hdvbua.pro player pages are JS-rendered and no longer expose the
# `file:'...'` single-quote payload that EneyidaProvider._file_url() expects.
# The movie page now embeds the URL as `file: "..."` (double-quoted JS), and
# the series embed/9549 endpoint returns "Контент недоступний" with no media
# data. Since the live upstream has changed since the original capture, we
# keep minimal inline HTML stubs that match the expected parser format.
PLAYER_MOVIE_HTML = "<html><body><script>file: 'https://s30.hdvbua.pro/media/movies/dune_part_two/index.m3u8]'</script></body></html>"
PLAYER_SERIES_HTML = '<html><body><script>file: \'[{"folder":[{"folder":[{"file":"https://s30.hdvbua.pro/media/series/dune_prophecy/s01/e01/index.m3u8"}]}]}]\'</script></body></html>'
# Issue #165: a /films/ page whose player payload is a full
# series-structured folder array (seasons → dubs → episodes) — upstream
# serves multi-episode titles under /films/ too.
PLAYER_FILM_WITH_SERIES_HTML = '<html><body><script>file: \'[{"title":"1 сезон","folder":[{"title":"HDrezka Studio","folder":[{"title":"1 серія","file":"https://s11.hdvbua.pro/media/content/stream/2025/1011322/1/1/11108778/index.m3u8"}]}]}]\'</script></body></html>'
# Ticket #331: a per-season series page whose player payload carries one
# top folder PER DUBBING TRACK (live 2026-08-15, «Дім Дракона»: folders
# titled Цікава Ідея / HDrezka Studio / MGG / BaibaKoTV, each holding
# the SAME season-1 episodes in that voiceover). These are translations,
# NOT seasons — the facade must show ONE season, not four.
PLAYER_MULTI_DUB_HTML = (
    "<html><body><script>file: '["
    '{"folder":['
    '{"title":"Цікава Ідея","folder":['
    '{"title":"1 серія","file":"https://s30.hdvbua.pro/media1/hls/serials/hotd.s01e01.ci/index.m3u8"},'
    '{"title":"2 серія","file":"https://s30.hdvbua.pro/media1/hls/serials/hotd.s01e02.ci/index.m3u8"}]},'
    '{"title":"HDrezka Studio","folder":['
    '{"title":"1 серія","file":"https://s30.hdvbua.pro/media2/hls/serials/hotd.s01e01.rezka/index.m3u8"},'
    '{"title":"2 серія","file":"https://s30.hdvbua.pro/media2/hls/serials/hotd.s01e02.rezka/index.m3u8"}]},'
    '{"title":"MGG","folder":['
    '{"title":"1 серія","file":"https://s30.hdvbua.pro/media3/hls/serials/hotd.s01e01.mgg/index.m3u8"},'
    '{"title":"2 серія","file":"https://s30.hdvbua.pro/media3/hls/serials/hotd.s01e02.mgg/index.m3u8"}]},'
    '{"title":"BaibaKoTV","folder":['
    '{"title":"1 серія","file":"https://s30.hdvbua.pro/media4/hls/serials/hotd.s01e01.baibko/index.m3u8"},'
    '{"title":"2 серія","file":"https://s30.hdvbua.pro/media4/hls/serials/hotd.s01e02.baibko/index.m3u8"}]}'
    "]}]'</script></body></html>"
)
# Ticket #331 companion shape: the TRUE multi-season payload (top
# entries titled «N сезон», each season -> dubs -> episodes) must still
# parse into N seasons, not dubs.
PLAYER_TWO_SEASON_HTML = (
    "<html><body><script>file: '["
    '{"title":"1 сезон","folder":[{"title":"HDrezka Studio","folder":['
    '{"title":"1 серія","file":"https://s30.hdvbua.pro/media1/hls/serials/silo.s01e01/index.m3u8"},'
    '{"title":"2 серія","file":"https://s30.hdvbua.pro/media1/hls/serials/silo.s01e02/index.m3u8"}]}]},'
    '{"title":"2 сезон","folder":[{"title":"MGG","folder":['
    '{"title":"1 серія","file":"https://s30.hdvbua.pro/media2/hls/serials/silo.s02e01/index.m3u8"}]}]}'
    "]'</script></body></html>"
)
# Issue #159: upstream template bug — the first iframe's src attribute
# has a doubled quote (`src="data-src="https://...`) which makes
# BeautifulSoup parse the real URL as junk attributes.
MALFORMED_IFRAME_PAGE = (
    "<html><body><h1>Шуґар</h1>"
    '<iframe width="100%" height="400" src="data-src="https://hdvbua.pro/vid/97148" '
    'frameborder="0" allow="encrypted-media" allowfullscreen></iframe>'
    '<iframe src="https://hdvbua.pro/vid/97149?tr=1" width="100%" height="400" '
    'frameborder="0" allowfullscreen></iframe>'
    "</body></html>"
)


@pytest.mark.asyncio
async def test_eneyida_search_parses_results():
    with respx.mock(assert_all_called=True) as router:
        router.post("https://eneyida.tv/index.php?do=search").respond(
            200, text=_fixture("search.html")
        )
        async with httpx.AsyncClient() as http:
            results = await EneyidaProvider().search("дюна", http)
    assert len(results) == 7
    assert all(r.provider == "eneyida" for r in results)
    # Bare-URL cards are classified by the season/episode label: «Діти
    # Дюни» and one «Дюна» card carry «N сезон M серія» and are series;
    # the rest are films (2026-08-14 upstream drift fix).
    assert [r.form for r in results] == [
        "movie", "movie", "movie", "series", "series", "movie", "movie",
    ]


@pytest.mark.asyncio
async def test_eneyida_browse_films_page1():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/").respond(200, text=_fixture("films_listing.html"))
        async with httpx.AsyncClient() as http:
            results, has_next = await EneyidaProvider().browse("films", 1, http)
    assert len(results) == 24
    assert has_next is True


@pytest.mark.asyncio
async def test_eneyida_browse_series_page1():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/").respond(200, text=_fixture("films_listing.html"))
        async with httpx.AsyncClient() as http:
            results, has_next = await EneyidaProvider().browse("series", 1, http)
    assert len(results) == 24
    assert has_next is True


@pytest.mark.asyncio
async def test_eneyida_browse_films_last_page():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/page/254/").respond(
            200, text=_fixture("films_listing.html")
        )
        async with httpx.AsyncClient() as http:
            _, has_next = await EneyidaProvider().browse("films", 254, http)
    assert has_next is False


@pytest.mark.asyncio
async def test_eneyida_browse_series_forces_series_form_on_bare_urls():
    """Upstream drift regression (2026-08-14): the site serves BARE card
    urls (``/8550-....html``) on BOTH listings, so the URL no longer
    carries the kind and every series card was classified as a film —
    the junk movie cards then leaked into the home «Серіали» row. The
    browse SECTION is authoritative: the series listing must yield
    ``form=series`` and ``series/<slug>`` ids."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/").respond(200, text=_fixture("films_listing.html"))
        async with httpx.AsyncClient() as http:
            results, has_next = await EneyidaProvider().browse("series", 1, http)
    assert len(results) == 24
    assert has_next is True
    assert {r.form for r in results} == {"series"}
    assert all(r.id.startswith("eneyida:series/") for r in results)


@pytest.mark.asyncio
async def test_eneyida_search_season_label_classifies_series():
    """Search cards carry no kind in the URL, but series cards carry a
    season/episode label (``<div class="metaBottom label_quel-camrip">1
    сезон 3 серія</div>``) — that is the series signal on the bare-url
    search page."""
    html = """
    <article class="short"><div class="short_in">
      <a class="short_img" href="https://eneyida.tv/8550-taiemnycia-bunkera.html">x</a>
      <a class="short_title" href="https://eneyida.tv/8550-taiemnycia-bunkera.html">Таємниця бункера</a>
      <div class="meta label_quel-hd">FHD 1080p</div>
      <div class="metaBottom label_quel-camrip">3 сезон 6 серія</div>
    </div></article>
    """
    with respx.mock(assert_all_called=True) as router:
        router.post("https://eneyida.tv/index.php?do=search").respond(200, text=html)
        async with httpx.AsyncClient() as http:
            results = await EneyidaProvider().search("бункер", http)
    assert len(results) == 1
    assert results[0].form == "series"
    assert results[0].id == "eneyida:series/8550-taiemnycia-bunkera"


@pytest.mark.asyncio
async def test_eneyida_content_movie_parses_title_poster_player():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/9366-duna-chastyna-druga.html").respond(
            200, text=_fixture("content_movie.html")
        )
        # content() now probes the main player embed to gate removed
        # titles (#158); a live player page passes the check.
        router.get("https://hdvbua.pro/vid/97148").respond(200, text=PLAYER_MOVIE_HTML)
        async with httpx.AsyncClient() as http:
            content = await EneyidaProvider().content("films/9366-duna-chastyna-druga", http)
    assert "Дюна" in content.title
    assert content.poster is not None and content.poster.startswith("https://")
    assert content.seasons and content.seasons[0].episodes[0].id == (
        "eneyida:films/9366-duna-chastyna-druga:__movie__"
    )


@pytest.mark.asyncio
async def test_eneyida_content_movie_gated_when_embed_unavailable() -> None:
    """Issue #158 regression: a movie whose hdvbua embed is the
    «Контент недоступний» page must raise ``gated`` from ``content()``
    (not just ``stream()``) so the catalog sweep drops the dead card."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/9366-duna-chastyna-druga.html").respond(
            200, text=_fixture("content_movie.html")
        )
        router.get("https://hdvbua.pro/vid/97148").respond(
            200, text=_fixture("embed_unavailable.html")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await EneyidaProvider().content("films/9366-duna-chastyna-druga", http)
    assert exc_info.value.code == "gated"
    assert "upstream content removed" in exc_info.value.message


@pytest.mark.asyncio
async def test_eneyida_content_movie_embed_without_token_resolves() -> None:
    """Live-gate regression (2026-08-09): eneyida content pages now
    serve the hdvbua embed iframe src WITHOUT the ``md`` marker token
    (``https://hdvbua.pro/embed/<id>/<hash>``), but the embed endpoint
    answers «Контент недоступний» unless ``?md`` is present — the
    upstream app appends it itself. content() must append the token so
    these titles don't get wrongly gated as upstream-removed."""
    page = (
        '<html><body><h1>Машина війни</h1>'
        '<div class="video-box">'
        '<iframe src="https://hdvbua.pro/embed/11965/b0c42c552"></iframe>'
        '</div></body></html>'
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/10163-mashyna-viiny.html").respond(
            200, text=page
        )
        router.get("https://hdvbua.pro/embed/11965/b0c42c552").respond(
            200, text=PLAYER_MOVIE_HTML
        )
        async with httpx.AsyncClient() as http:
            content = await EneyidaProvider().content(
                "films/10163-mashyna-viiny", http
            )
    assert "Машина війни" in content.title
    assert content.seasons and content.seasons[0].episodes[0].id == (
        "eneyida:films/10163-mashyna-viiny:__movie__"
    )


@pytest.mark.asyncio
async def test_eneyida_content_series_gated_when_embed_unavailable() -> None:
    """Issue #158 regression: the series path (embed fetch inside
    ``_seasons``) also gates a «Контент недоступний» embed."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/9758-duna-proroctvo.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(
            200, text=_fixture("embed_unavailable.html")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await EneyidaProvider().content("series/9758-duna-proroctvo", http)
    assert exc_info.value.code == "gated"
    assert "upstream content removed" in exc_info.value.message


@pytest.mark.asyncio
async def test_eneyida_content_series_parses_seasons():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/9758-duna-proroctvo.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_SERIES_HTML)
        async with httpx.AsyncClient() as http:
            content = await EneyidaProvider().content("series/9758-duna-proroctvo", http)
    assert content.seasons and any(season.episodes for season in content.seasons)


@pytest.mark.asyncio
async def test_eneyida_stream_malformed_iframe_extracts_player_url() -> None:
    """Issue #159 regression: the upstream doubled-quote iframe template
    bug makes BeautifulSoup parse the real player URL as junk attributes;
    ``stream()`` must recover it from the raw tag HTML instead of failing
    with ``disallowed host``. The trailer iframe (``?tr=1``) must not be
    mistaken for the player."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/10128-shugar.html").respond(
            200, text=MALFORMED_IFRAME_PAGE
        )
        router.get("https://hdvbua.pro/vid/97148").respond(200, text=PLAYER_MOVIE_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream("films/10128-shugar:__movie__", None, http)
    assert "https://s30.hdvbua.pro/" in stream.url


@pytest.mark.asyncio
async def test_eneyida_stream_movie_resolves_to_media_url():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/films/9366-duna-chastyna-druga.html").respond(
            200, text=_fixture("content_movie.html")
        )
        router.get("https://hdvbua.pro/vid/97148").respond(200, text=PLAYER_MOVIE_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream(
                "films/9366-duna-chastyna-druga:__movie__", None, http
            )
    assert "https://s30.hdvbua.pro/" in stream.url
    assert stream.url.endswith("]")


@pytest.mark.asyncio
async def test_eneyida_content_film_with_series_payload_classifies_as_series() -> None:
    """Issue #165: a /films/ page whose player payload is a
    series-structured folder array must be classified as a series with
    playable episode rails, not a single movie episode whose stream
    would return the raw JSON blob."""
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://eneyida.tv/films/10103-druga-svitova-viina-z-tomom-genksom.html"
        ).respond(200, text=_fixture("content_movie.html"))
        router.get("https://hdvbua.pro/vid/97148").respond(200, text=PLAYER_FILM_WITH_SERIES_HTML)
        async with httpx.AsyncClient() as http:
            content = await EneyidaProvider().content(
                "films/10103-druga-svitova-viina-z-tomom-genksom", http
            )
    assert content.form == "series"
    assert content.seasons and content.seasons[0].episodes
    ep = content.seasons[0].episodes[0]
    assert ep.id == "eneyida:films/10103-druga-svitova-viina-z-tomom-genksom:s1e1"
    assert not ep.id.endswith(":__movie__")


@pytest.mark.asyncio
async def test_eneyida_stream_film_with_series_payload_resolves_first_file() -> None:
    """Issue #165: a movie id whose player payload is a series folder
    array must resolve the first playable file instead of returning the
    raw JSON blob as the stream URL."""
    with respx.mock(assert_all_called=True) as router:
        router.get(
            "https://eneyida.tv/films/10103-druga-svitova-viina-z-tomom-genksom.html"
        ).respond(200, text=_fixture("content_movie.html"))
        router.get("https://hdvbua.pro/vid/97148").respond(200, text=PLAYER_FILM_WITH_SERIES_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream(
                "films/10103-druga-svitova-viina-z-tomom-genksom:__movie__", None, http
            )
    assert stream.url == (
        "https://s11.hdvbua.pro/media/content/stream/2025/1011322/1/1/11108778/index.m3u8"
    )
    assert stream.url.endswith("index.m3u8")


@pytest.mark.asyncio
async def test_eneyida_stream_series_resolves_episode_m3u8():
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/9758-duna-proroctvo.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_SERIES_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream("series/9758-duna-proroctvo:s1e1", None, http)
    assert "https://s30.hdvbua.pro/" in stream.url
    assert stream.url.endswith("index.m3u8")


@pytest.mark.asyncio
async def test_eneyida_stream_dead_embed_raises_gated_not_parse_failed() -> None:
    """Regression (issue #137): when the hdvbua embed is the upstream's
    «Контент недоступний» page (upstream-removed content, captured live
    2026-08-08), ``stream()`` must raise a ``gated`` verdict — the
    facade's standing deliberate-unavailable path (404, health stays
    green) — NOT ``parse_failed: media missing``, which would mark the
    provider down for an upstream content removal."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/9758-duna-proroctvo.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(
            200, text=_fixture("embed_unavailable.html")
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await EneyidaProvider().stream("series/9758-duna-proroctvo:s1e1", None, http)
    assert exc_info.value.code == "gated"
    assert "upstream content removed" in exc_info.value.message


def test_eneyida_sections_lists_two():
    assert [section.id for section in EneyidaProvider().sections] == ["films", "series"]


@pytest.mark.asyncio
async def test_eneyida_content_multi_dub_payload_collapses_to_one_season() -> None:
    """Ticket #331: per-dub top folders are translations, not seasons.

    Live 2026-08-15 «Дім Дракона»: the player payload has four folders
    titled by dubbing studio, each holding the same S1 episodes in that
    voiceover. The facade must see ONE season whose translations list
    the studios — not four phantom seasons (and certainly not "the
    wrong series" when the client drills into a dub folder).
    """
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/7878-dim-drakona-2022-v2.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_MULTI_DUB_HTML)
        async with httpx.AsyncClient() as http:
            content = await EneyidaProvider().content("series/7878-dim-drakona-2022-v2", http)
    assert content.seasons is not None and len(content.seasons) == 1
    assert [e.number for e in content.seasons[0].episodes] == [1, 2]
    assert content.seasons[0].episodes[0].id == "eneyida:series/7878-dim-drakona-2022-v2:s1e1"
    assert [t.label for t in content.translations] == [
        "Цікава Ідея",
        "HDrezka Studio",
        "MGG",
        "BaibaKoTV",
    ]


@pytest.mark.asyncio
async def test_eneyida_stream_multi_dub_suffix_indexes_dub_folder() -> None:
    """Ticket #331: legacy ``:s<i>e<j>`` wire ids keep their dub-folder
    semantics — ``s3e2`` plays episode 2 of the THIRD dub (MGG), so
    already-recorded playback positions keep working after the collapse."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/7878-dim-drakona-2022-v2.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_MULTI_DUB_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream(
                "series/7878-dim-drakona-2022-v2:s3e2", None, http
            )
    assert stream.url.endswith("hotd.s01e02.mgg/index.m3u8")


@pytest.mark.asyncio
async def test_eneyida_content_multi_season_payload_parses_seasons() -> None:
    """Ticket #331 companion: top entries titled «N сезон» (season ->
    dubs -> episodes) stay N real seasons, first dub's episodes."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/8550-taiemnycia-bunkera.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_TWO_SEASON_HTML)
        async with httpx.AsyncClient() as http:
            content = await EneyidaProvider().content("series/8550-taiemnycia-bunkera", http)
    assert content.seasons is not None
    assert [s.number for s in content.seasons] == [1, 2]
    assert content.seasons[0].episodes[0].id == "eneyida:series/8550-taiemnycia-bunkera:s1e1"
    assert content.seasons[1].episodes[0].id == "eneyida:series/8550-taiemnycia-bunkera:s2e1"
    assert [t.label for t in content.seasons[0].episodes[0].translations or []] == ["HDrezka Studio"]


@pytest.mark.asyncio
async def test_eneyida_stream_multi_season_suffix_navigates_season() -> None:
    """Ticket #331 companion: ``s2e1`` on a real multi-season payload
    resolves season 2's first dub, episode 1."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/8550-taiemnycia-bunkera.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_TWO_SEASON_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream(
                "series/8550-taiemnycia-bunkera:s2e1", None, http
            )
    assert stream.url.endswith("silo.s02e01/index.m3u8")


@pytest.mark.asyncio
async def test_eneyida_stream_multi_dub_honors_translation_choice() -> None:
    """Ticket #331: the dub picker's translation id (a studio name)
    selects that voiceover's folder — ``MGG`` on a collapsed multi-dub
    payload plays MGG's file, not dub 1's."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/7878-dim-drakona-2022-v2.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_MULTI_DUB_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream(
                "series/7878-dim-drakona-2022-v2:s1e2", "MGG", http
            )
    assert stream.url.endswith("hotd.s01e02.mgg/index.m3u8")


@pytest.mark.asyncio
async def test_eneyida_stream_unknown_translation_falls_back_to_suffix() -> None:
    """Ticket #331: an unmatched translation id must not 500 — the
    suffix dub indexing stays the fallback (tolerant degradation)."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/7878-dim-drakona-2022-v2.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_MULTI_DUB_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream(
                "series/7878-dim-drakona-2022-v2:s3e2", "nonexistent-dub", http
            )
    assert stream.url.endswith("hotd.s01e02.mgg/index.m3u8")


@pytest.mark.asyncio
async def test_eneyida_stream_multi_season_honors_translation_choice() -> None:
    """Ticket #331 companion: on a season-top payload the translation
    picks the dub INSIDE the season (S2's MGG track)."""
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/8550-taiemnycia-bunkera.html").respond(
            200, text=_fixture("content_series.html")
        )
        router.get("https://hdvbua.pro/embed/9549").respond(200, text=PLAYER_TWO_SEASON_HTML)
        async with httpx.AsyncClient() as http:
            stream = await EneyidaProvider().stream(
                "series/8550-taiemnycia-bunkera:s2e1", "MGG", http
            )
    assert stream.url.endswith("silo.s02e01/index.m3u8")


@pytest.mark.asyncio
async def test_eneyida_browse_unknown_section_raises():
    with respx.mock(assert_all_called=False), pytest.raises(ProviderError) as exc_info:
        await EneyidaProvider().browse("unknown", 1, httpx.AsyncClient())
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_eneyida_content_bad_slug_raises():
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await EneyidaProvider().content("films/../admin", http)
    assert exc_info.value.code == "not_found"


@pytest.mark.asyncio
async def test_eneyida_stream_bad_slug_raises():
    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as http:
            with pytest.raises(ProviderError) as exc_info:
                await EneyidaProvider().stream("films/../admin:__movie__", None, http)
    assert exc_info.value.code == "not_found"
