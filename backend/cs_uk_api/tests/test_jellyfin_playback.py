"""Jellyfin PlaybackInfo (ticket #106, spec D6).

Ticket #106's acceptance, pinned at the HTTP seam:

  - ``POST /Items/{id}/PlaybackInfo`` (and the server-style GET spelling)
    returns a THIN ``MediaSources`` envelope with exactly one source:
    ``Id`` = the item id, ``Container`` = the provider's
    ``StreamResponse.type`` (mp4/m3u8/hls), a single
    ``MediaStreams: [{"Type": "Video"}]`` — NO codec fields (lying about
    codecs risks forcing a transcode path we can't serve) —
    ``IsDirectStream: true``, a fictitious stable ``Path``, and a fresh
    ``PlaySessionId`` UUID.
  - A movie resolves through its ``g2:`` group key to the group's
    first-seen provider, whose ``stream()`` is called with the BARE
    external id (translation=None → the first default) — the same id a
    native client hands ``/api/stream``.
  - An episode resolves by its own id (``p1:s1e1``-style wire id, D2):
    the provider prefix is split off and ``stream()`` is called with the
    episode suffix — no group-key resolution, no reverse lookup.
  - A series/season item is NOT directly playable → 404 (matches D2
    "item unavailable"; the client plays episodes, not the show).
  - Cold resolution cache / unknown group key → 404, never 5xx.
  - A ``stream()`` upstream failure degrades to 404 (the facade never
    surfaces a 502, D2).
  - Both spellings sit behind the same ``require_token`` gate (D4).

Seeded via the same seam as ``test_jellyfin_detail``: one stub provider
surfaces a movie and a series through ``/api/home``; its ``stream()``
returns a canned ``StreamResponse`` and records the calls so the BARE-id
contract is pinned.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.catalog_state import blocklist_cache, content_cache, home_cache, sources_cache
from cs_uk_api.config import SETTINGS
from cs_uk_api.models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    StreamResponse,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError

TOKEN = SETTINGS.jellyfin_token

_POSTER_MOVIE = "https://cdn.example.test/posters/dune.jpg"
_POSTER_SERIES = "https://cdn.example.test/posters/serial.jpg"


def _dune() -> ContentResponse:
    return ContentResponse(
        id="p1:dune-1",
        form="movie",
        title="Дюна",
        year=2021,
        description="Епічна науково-фантастична стрічка.",
        poster=_POSTER_MOVIE,
        translations=[Translation(id="uk", label="Дубляж")],
    )


def _serial() -> ContentResponse:
    return ContentResponse(
        id="p1:serial-1",
        form="series",
        title="Сериалал серіал",
        year=2023,
        description="Детективний серіал.",
        poster=_POSTER_SERIES,
        translations=[Translation(id="uk", label="Дубляж")],
        seasons=[
            Season(
                number=1,
                episodes=[
                    Episode(number=1, id="s1e1", title="Серія 1"),
                    Episode(number=2, id="s1e2", title="Серія 2"),
                ],
            )
        ],
    )


class _PlaybackStub(BaseProvider):
    """One home-capable provider; ``stream()`` serves canned responses.

    ``stream`` records ``(content_id, translation)`` and returns the
    ``StreamResponse`` registered for the content id — so tests pin both
    the exact id handed over (bare external for movies, episode suffix
    for episodes, translation always None) and the container mapping.
    """

    id = "p1"
    name = "P1"
    types = ("movie", "series")
    newest_section = "page"

    def __init__(
        self,
        cards: list[SearchResult],
        content_by_external: dict[str, ContentResponse],
        streams: dict[str, StreamResponse],
    ) -> None:
        self._cards = cards
        self._content_by_external = content_by_external
        self._streams = streams
        self.stream_calls: list[tuple[str, str | None]] = []
        self.content_calls: list[str] = []
        self.sections: tuple[Any, ...] = ()

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def browse(
        self, section: str, page: int, http: Any
    ) -> tuple[list[SearchResult], bool]:
        if section == "page":
            return list(self._cards), False
        return [], False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        self.content_calls.append(external_id)
        content = self._content_by_external.get(external_id)
        if content is None:
            raise ProviderError("not_found", f"no canned content for {external_id}")
        return content.model_copy(deep=True)

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> StreamResponse:
        self.stream_calls.append((content_id, translation))
        stream = self._streams.get(content_id)
        if stream is None:
            raise ProviderError("not_found", f"no canned stream for {content_id}")
        return stream.model_copy(deep=True)


def _card(
    pid: str,
    id_: str,
    title: str,
    media_type: str,
    *,
    poster: str | None = None,
) -> SearchResult:
    mb_form, mb_styles = (
        (media_type, frozenset())
        if media_type in ("movie", "series")
        else ("series", frozenset({media_type}))
    )
    return SearchResult(
        id=f"{pid}:{id_}",
        provider=pid,
        form=mb_form,
        styles=mb_styles,
        title=title,
        year=2021 if media_type == "movie" else 2023,
        poster=poster,
        url=f"https://{pid}.example/{id_}",
    )


def _seed(streams: dict[str, StreamResponse]) -> _PlaybackStub:
    """One stub surfacing the movie + series cards through /api/home."""
    return _PlaybackStub(
        cards=[
            _card("p1", "dune-1", "Дюна", "movie", poster=_POSTER_MOVIE),
            _card("p1", "serial-1", "Сериалал серіал", "series", poster=_POSTER_SERIES),
        ],
        content_by_external={"dune-1": _dune(), "serial-1": _serial()},
        streams=streams,
    )


def _movie_stream() -> StreamResponse:
    return StreamResponse(url="https://cdn.example.test/dune.mp4", type="mp4")


def _episode_stream() -> StreamResponse:
    return StreamResponse(url="https://cdn.example.test/serial/s1e1.m3u8", type="m3u8")


def _seeded(req: TestClient) -> tuple[_PlaybackStub, str, str]:
    """Register the seeded stub, warm the home snapshot, return
    (stub, movie_gk, episode_wire_id)."""
    stub = _seed(
        streams={
            "dune-1": _movie_stream(),
            "s1e1": _episode_stream(),
        }
    )
    PROVIDERS["p1"] = stub
    r = req.get("/api/home")
    assert r.status_code == 200
    home = cast("dict[str, Any]", r.json())
    movie_gk = ""
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Дюна":
                movie_gk = cast(str, item["group_key"])
    assert movie_gk, "no movie group key in seeded home"
    return stub, movie_gk, "p1:s1e1"


def _multi_serial() -> ContentResponse:
    """A series whose episodes carry several dubs (spec #276): the
    picker must list them as named sources."""
    return ContentResponse(
        id="p1:serial-1",
        form="series",
        title="Сериалал серіал",
        year=2023,
        description="Детективний серіал.",
        poster=_POSTER_SERIES,
        translations=[Translation(id="uk", label="Дубляж")],
        seasons=[
            Season(
                number=1,
                episodes=[
                    Episode(
                        number=1,
                        id="serial-1:s1e1",
                        title="Серія 1",
                        translations=[
                            Translation(id="uk", label="Дубляж"),
                            Translation(id="vo", label="Оригінал"),
                            Translation(id="sub", label="Субтитри"),
                            Translation(id="duplicate", label="Дубляж"),  # dedup
                        ],
                    ),
                ],
            )
        ],
    )


def _multi_seed() -> _PlaybackStub:
    """The movie (single translation) + the multi-dub series."""
    return _PlaybackStub(
        cards=[
            _card("p1", "dune-1", "Дюна", "movie", poster=_POSTER_MOVIE),
            _card("p1", "serial-1", "Сериалал серіал", "series", poster=_POSTER_SERIES),
        ],
        content_by_external={"dune-1": _dune(), "serial-1": _multi_serial()},
        streams={
            "dune-1": _movie_stream(),
            "serial-1:s1e1": _episode_stream(),
        },
    )


def _seeded_multi(req: TestClient) -> tuple[_PlaybackStub, str, str]:
    """Register the multi-dub seed, warm the home, return
    (stub, movie_gk, episode_wire_id)."""
    stub = _multi_seed()
    PROVIDERS["p1"] = stub
    r = req.get("/api/home")
    assert r.status_code == 200
    home = cast("dict[str, Any]", r.json())
    movie_gk = ""
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Дюна":
                movie_gk = cast(str, item["group_key"])
    assert movie_gk
    return stub, movie_gk, "p1:serial-1:s1e1"


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS and every cache the facade reads so no
    real upstream calls or stale state leak into assertions."""
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (home_cache, sources_cache, content_cache, blocklist_cache):
        cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        for cache in (home_cache, sources_cache, content_cache, blocklist_cache):
            cache.clear()


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


def _post(client: TestClient, path: str, **params: str) -> dict[str, Any]:
    r = client.post(
        path,
        params=params or None,
        headers={"X-Emby-Token": TOKEN},
        json={},
    )
    assert r.status_code == 200, r.text
    return cast("dict[str, Any]", r.json())


def _source(body: dict[str, Any]) -> dict[str, Any]:
    assert len(body["MediaSources"]) == 1
    return cast("dict[str, Any]", body["MediaSources"][0])


def test_movie_post_playback_info_thin_envelope(client: TestClient) -> None:
    """POST /Items/{g1_movie}/PlaybackInfo returns exactly one thin
    MediaSource: no codec fields, IsDirectStream, fictitious Path, a
    fresh PlaySessionId, and the container from the provider's type."""
    stub, gk, _ = _seeded(client)

    body = _post(client, f"/Items/{gk}/PlaybackInfo", userId="u")
    source = _source(body)

    assert source["Id"] == gk
    assert source["Container"] == "mp4"
    assert source["MediaStreams"] == [{"Type": "Video"}]
    assert source["IsDirectStream"] is True
    assert source["Path"] == f"/Videos/{gk}/stream"
    # Never linger on a leaked upstream URL: Path is fictitious.
    assert ".mp4" not in source["Path"]
    uuid.UUID(source["PlaySessionId"])  # must be a well-formed UUID
    # The response envelope carries the session id I created.
    assert body["PlaySessionId"] == source["PlaySessionId"]
    # 4xx: the "thin" source carries no codec / size / bitrate fields.
    assert "Codec" not in source
    assert "Width" not in source
    assert "Height" not in source
    # The movie resolved via the group's first-seen provider with the
    # BARE external id and the default translation.
    assert stub.stream_calls == [("dune-1", None)]


def test_movie_playback_info_get_spelling(client: TestClient) -> None:
    """D6 declares GET; the real SDK uses POST (capture row 6). Both
    spellings serve the same thin envelope."""
    _, gk, _ = _seeded(client)

    r = client.get(f"/Items/{gk}/PlaybackInfo", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = cast("dict[str, Any]", r.json())
    assert _source(body)["Id"] == gk
    assert _source(body)["Container"] == "mp4"


def test_movie_m3u8_container_from_stream_type(client: TestClient) -> None:
    """Container is taken from the provider's StreamResponse.type, not
    hardcoded: an m3u8 source must surface as an m3u8 container."""
    stub = _seed(
        streams={
            "dune-1": StreamResponse(url="https://cdn.example/hls/master.m3u8", type="m3u8"),
            "s1e1": _episode_stream(),
        }
    )
    PROVIDERS["p1"] = stub
    client.get("/api/home")
    home_gk = ""
    home = cast("dict[str, Any]", client.get("/api/home").json())
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Дюна":
                home_gk = cast(str, item["group_key"])

    body = _post(client, f"/Items/{home_gk}/PlaybackInfo")
    assert _source(body)["Container"] == "m3u8"


def test_anime_film_card_playable_despite_anime_style(client: TestClient) -> None:
    """A film whose CARD carries the anime style tag must still be
    playable: PlaybackInfo reads the content's FORM (``ContentResponse.
    type == "movie"``), the same verdict detail renders as
    ``Type="Movie"``, not the card's ``SearchResult.type`` style literal
    (anime providers tag films and series alike ``"anime"``).

    Regression for the #106 review: gating on the style literal 404'd
    anime films that the client had already opened as Movies.
    """
    stub = _seed(streams={"dune-1": _movie_stream(), "s1e1": _episode_stream()})
    stub._cards = [_card("p1", "dune-1", "Дюна", "anime", poster=_POSTER_MOVIE)]
    PROVIDERS["p1"] = stub
    gk = ""
    home = cast("dict[str, Any]", client.get("/api/home").json())
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Дюна":
                gk = cast(str, item["group_key"])
    assert gk

    body = _post(client, f"/Items/{gk}/PlaybackInfo")
    source = _source(body)

    assert source["Id"] == gk
    assert source["Container"] == "mp4"
    # Content form "movie" is what the detail page renders as a Movie.
    assert stub.stream_calls == [("dune-1", None)]


def test_episode_playback_info_uses_episode_wire_id(client: TestClient) -> None:
    """An episode resolves by its provider-scoped wire id (D2), NOT by a
    group key: the facade splits the provider prefix and hands the
    episode suffix straight to stream(). This is the id a native client
    gives with /Videos/{id}/stream."""
    stub, _, ep_id = _seeded(client)

    body = _post(client, f"/Items/{ep_id}/PlaybackInfo")
    source = _source(body)

    assert source["Id"] == ep_id
    assert source["Container"] == "m3u8"
    assert source["MediaStreams"] == [{"Type": "Video"}]
    assert stub.stream_calls == [("s1e1", None)]
    uuid.UUID(source["PlaySessionId"])


def test_episode_playback_info_wire_id_containing_slashes(client: TestClient) -> None:
    """Provider episode wire ids can embed a full upstream page URL
    (``p1:https://…/s/1/ep.html``); the client percent-encodes the id into
    the path, and the path-converter route must still reach the handler."""
    stub, _, _ = _seeded(client)
    ep_url = "https://cdn.example.test/s/s1/e1.m3u8"
    stub._streams[ep_url] = _episode_stream()
    ep_id = f"p1:{ep_url}"

    body = _post(client, f"/Items/{quote(ep_id, safe='')}/PlaybackInfo")

    assert _source(body)["Container"] == "m3u8"
    assert stub.stream_calls == [("https://cdn.example.test/s/s1/e1.m3u8", None)]


def test_series_item_not_directly_playable_404(client: TestClient) -> None:
    """A Series/Season card is not itself playable: the client plays
    episodes (D3). PlaybackInfo on the show answers the same "item
    unavailable" 404 a cold key gets."""
    _, _, _ = _seeded(client)
    serial_gk = ""
    home = cast("dict[str, Any]", client.get("/api/home").json())
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Сериалал серіал":
                serial_gk = cast(str, item["group_key"])
    assert serial_gk

    r = client.post(f"/Items/{serial_gk}/PlaybackInfo", headers={"X-Emby-Token": TOKEN}, json={})
    assert r.status_code == 404
    season_gk = f"{serial_gk}:S1"
    r2 = client.post(f"/Items/{season_gk}/PlaybackInfo", headers={"X-Emby-Token": TOKEN}, json={})
    assert r2.status_code == 404


def test_cold_cache_404_and_unknown_ids_404(client: TestClient) -> None:
    """Cold resolution map and unknown ids both 404 — never 5xx."""
    r = client.post(
        "/Items/g2:deadbeefdeadbeef/PlaybackInfo", headers={"X-Emby-Token": TOKEN}, json={}
    )
    assert r.status_code == 404
    r2 = client.post(
        "/Items/00000000000000000000000000000000/PlaybackInfo",
        headers={"X-Emby-Token": TOKEN},
        json={},
    )
    assert r2.status_code == 404


def test_stream_failure_degrades_to_404(client: TestClient) -> None:
    """If stream() raises, PlaybackInfo degrades to a 404 (facade never
    502s), matching how the native route would be surfaced to a Jellyfin
    client."""
    stub = _seed(
        streams={
            "dune-1": _movie_stream(),
            "s1e1": _episode_stream(),
        }
    )
    stub._streams.pop("dune-1")  # simulate the provider failing/unknown slug
    PROVIDERS["p1"] = stub
    gk = ""
    home = cast("dict[str, Any]", client.get("/api/home").json())
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Дюна":
                gk = item["group_key"]

    r = client.post(f"/Items/{gk}/PlaybackInfo", headers={"X-Emby-Token": TOKEN}, json={})
    assert r.status_code == 404


def test_gated_stream_degrades_to_404_without_health_impact(client: TestClient) -> None:
    """A `gated` verdict (BambooUA subscription-gate promo clip) is
    client-side semantics, not an upstream failure (ADR-0002 amendment):
    PlaybackInfo 404s like any unavailable item, and the provider health
    tracker is NOT marked down."""
    from cs_uk_api.health import TRACKER

    class _Gated(_PlaybackStub):
        async def stream(  # type: ignore[override]
            self, content_id: str, translation: str | None, http: Any
        ) -> StreamResponse:
            raise ProviderError("gated", "subscription required")

    stub = _Gated(
        cards=[_card("p1", "dune-1", "Дюна", "movie", poster=_POSTER_MOVIE)],
        content_by_external={"dune-1": _dune()},
        streams={},
    )
    PROVIDERS["p1"] = stub
    home = cast("dict[str, Any]", client.get("/api/home").json())
    gk = next(
        item["group_key"]
        for row in home["rows"]
        for item in row["items"]
        if item["title"] == "Дюна"
    )

    r = client.post(
        f"/Items/{gk}/PlaybackInfo", headers={"X-Emby-Token": TOKEN}, json={}
    )
    assert r.status_code == 404
    # The gated verdict must not move the provider's health needle.
    assert TRACKER.status("p1") == "ok"


def test_requires_token_all_spellings(client: TestClient) -> None:
    assert client.post("/Items/g2:deadbeefdeadbeef/PlaybackInfo", json={}).status_code == 401
    assert client.get("/Items/g2:deadbeefdeadbeef/PlaybackInfo").status_code == 401


@pytest.mark.asyncio
async def test_eneyida_dead_embed_playback_info_404_health_ok(
    client: TestClient,
) -> None:
    """End-to-end (issue #137): the REAL eneyida provider, whose embed
    page is the upstream's «Контент недоступний» shape (captured fixture),
    resolves PlaybackInfo to a gated verdict → the facade 404s and the
    provider health needle does NOT move — upstream content removal is
    not a provider failure (ADR-0002 amendment)."""
    import contextlib
    import importlib
    from pathlib import Path

    import httpx
    import respx

    from cs_uk_api.health import TRACKER
    from cs_uk_api.providers.eneyida import EneyidaProvider

    TRACKER.reset()

    router_mod = importlib.import_module("cs_uk_api.jellyfin.router")

    @contextlib.contextmanager
    def _fake_host() -> Iterator[None]:
        original = router_mod.get_client
        router_mod.get_client = lambda: httpx.AsyncClient()  # type: ignore[assignment]
        try:
            yield
        finally:
            router_mod.get_client = original  # type: ignore[assignment]

    PROVIDERS["eneyida"] = EneyidaProvider()
    fix = Path(__file__).parent / "fixtures" / "eneyida"
    with respx.mock(assert_all_called=True) as router:
        router.get("https://eneyida.tv/series/9758-duna-proroctvo.html").respond(
            200,
            text=(fix / "content_series.html").read_text(encoding="utf-8"),
        )
        router.get("https://hdvbua.pro/embed/9549").respond(
            200,
            text=(fix / "embed_unavailable.html").read_text(encoding="utf-8"),
        )
        with _fake_host():
            r = client.post(
                "/Items/eneyida:series/9758-duna-proroctvo:s1e1/PlaybackInfo",
                headers={"X-Emby-Token": TOKEN},
                json={},
            )
    assert r.status_code == 404
    # The gated verdict must not move eneyida's health needle. Assert on
    # `last_error_at`, not `status` — `status` reads "ok" below
    # `min_samples` even after a failure is recorded, so it can't
    # discriminate the gated (no-record) path from a parse_failed
    # regression. Only `record(ok=False)` sets `_errors`.
    assert TRACKER.status("eneyida") == "ok"
    assert TRACKER.last_error_at("eneyida") is None


# ------------------------------------------------------ multi-source dubs (#276)


def test_playback_info_multi_source_lists_named_dubs(client: TestClient) -> None:
    """#276 T1: a series with several dubs returns one MediaSource per
    translation — each with an audio MediaStream carrying Index +
    DisplayTitle (the picker renders names), deduped by label, capped.
    The movie (single translation) stays a single thin source."""
    _stub, movie_gk, ep_id = _seeded_multi(client)

    ep_body = _post(client, f"/Items/{ep_id}/PlaybackInfo")
    sources = ep_body["MediaSources"]
    # uk, vo, sub — the duplicate label collapses.
    assert [s["DisplayTitle"] for s in sources] == ["Дубляж", "Оригінал", "Субтитри"]
    for src in sources:
        audio = next(m for m in src["MediaStreams"] if m["Type"] == "Audio")
        assert audio["DisplayTitle"] == src["DisplayTitle"]
        # Index = the source's position in the response (1-based), the
        # value the picker echoes back as AudioStreamIndex.
        assert audio["Index"] == sources.index(src) + 1
        assert src["Id"].startswith(f"{ep_id}::")
        assert src["Container"] == "m3u8"
    # First source is the default (remembered or first) — Index 1.
    assert sources[0]["MediaStreams"][1]["Index"] == 1

    movie_body = _post(client, f"/Items/{movie_gk}/PlaybackInfo")
    assert len(movie_body["MediaSources"]) == 1
    assert movie_body["MediaSources"][0]["Id"] == movie_gk
    assert "DisplayTitle" not in movie_body["MediaSources"][0]


def test_playback_info_picked_index_source_goes_first(client: TestClient) -> None:
    """#276 T1: the picker echoes its selection as AudioStreamIndex; the
    matching source goes FIRST (the client plays MediaSources[0]) — the
    switch path."""
    _stub, _movie_gk, ep_id = _seeded_multi(client)

    body = _post(client, f"/Items/{ep_id}/PlaybackInfo", AudioStreamIndex="3")
    sources = body["MediaSources"]
    # Index 3 in the default ordering = «Субтитри» (1 Дубляж, 2
    # Оригінал, 3 Субтитри) — now first.
    assert sources[0]["DisplayTitle"] == "Субтитри"


def test_playback_info_remembered_dub_orders_first(client: TestClient) -> None:
    """#276 T2: after a dub is remembered for a series, the next
    PlaybackInfo defaults to it — the remembered source is first and
    carries Index 1 (the client's default selected index)."""
    import cs_uk_api.catalog_state as cs

    _stub, _movie_gk, ep_id = _seeded_multi(client)
    # Remember «Оригінал» for the series (the group behind the episode).
    gk = cs.episode_group_key(ep_id)
    assert gk is not None
    cs.remember_dub(gk, "Оригінал")
    try:
        body = _post(client, f"/Items/{ep_id}/PlaybackInfo")
        sources = body["MediaSources"]
        assert sources[0]["DisplayTitle"] == "Оригінал"
        assert sources[0]["MediaStreams"][1]["Index"] == 1
        # The rest follow, indexes 2..N.
        assert [s["MediaStreams"][1]["Index"] for s in sources] == [1, 2, 3]
    finally:
        cs.clear_user_state()


def test_stream_source_id_switches_translation(client: TestClient) -> None:
    """#276 T2: a stream request echoing a multi-source id resolves that
    translation — the provider's stream() receives the picked translation
    id, and the series dub choice is remembered."""
    import cs_uk_api.catalog_state as cs

    stub, _movie_gk, ep_id = _seeded_multi(client)
    cs.clear_user_state()
    source_id = f"{ep_id}::vo"

    r = client.get(
        f"/Videos/{ep_id}/stream",
        params={"mediaSourceId": source_id},
        headers={"X-Emby-Token": TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert ("serial-1:s1e1", "vo") in stub.stream_calls
    # The series dub is remembered («Оригінал» = id "vo").
    gk = cs.episode_group_key(ep_id)
    assert gk is not None
    assert cs.dub_for(gk) == "Оригінал"


def test_stream_plain_id_keeps_default_and_no_memory(client: TestClient) -> None:
    """#276 T2: the plain item id (single-translation path) streams the
    default translation and records nothing — unchanged D6 behaviour."""
    import cs_uk_api.catalog_state as cs

    stub, _movie_gk, ep_id = _seeded_multi(client)
    cs.clear_user_state()
    r = client.get(
        f"/Videos/{ep_id}/stream",
        headers={"X-Emby-Token": TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert ("serial-1:s1e1", None) in stub.stream_calls
    assert cs.dub_memory() == {}


def test_stream_movie_never_records_dub(client: TestClient) -> None:
    """#276 v3: a movie stream with a source id never records dub memory
    — films always start on the default dub."""
    import cs_uk_api.catalog_state as cs

    stub, movie_gk, _ep_id = _seeded_multi(client)
    cs.clear_user_state()
    source_id = f"{movie_gk}::uk"

    r = client.get(
        f"/Videos/{movie_gk}/stream",
        params={"mediaSourceId": source_id},
        headers={"X-Emby-Token": TOKEN},
        follow_redirects=False,
    )
    assert r.status_code == 302
    # The movie resolved the group's first-seen provider, bare external.
    assert ("dune-1", "uk") in stub.stream_calls
    assert cs.dub_memory() == {}


def test_playback_info_translation_list_needs_no_extra_fetch(client: TestClient) -> None:
    """#276 T1 AC5: the multi-source translation list comes from the
    episode blob / already-fetched content — a second PlaybackInfo for
    the same item performs NO extra provider.content() call (the
    content cache serves it)."""
    stub, _movie_gk, ep_id = _seeded_multi(client)

    # First PlaybackInfo warms the content cache (one content fetch).
    _post(client, f"/Items/{ep_id}/PlaybackInfo")
    first_fetches = list(stub.content_calls)
    assert first_fetches, "the first PlaybackInfo must have resolved content"

    # Second PlaybackInfo: same translation list, zero new content fetches.
    stub.content_calls.clear()
    body = _post(client, f"/Items/{ep_id}/PlaybackInfo")
    assert stub.content_calls == []
    assert [s["DisplayTitle"] for s in body["MediaSources"]] == [
        "Дубляж",
        "Оригінал",
        "Субтитри",
    ]
