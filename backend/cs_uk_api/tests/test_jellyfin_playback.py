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
  - A movie resolves through its ``g1:`` group key to the group's
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

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS
from cs_uk_api.main import _blocklist_cache, _content_cache, _home_cache, _home_sources_cache
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
        type="movie",
        title="Дюна",
        year=2021,
        description="Епічна науково-фантастична стрічка.",
        poster=_POSTER_MOVIE,
        translations=[Translation(id="uk", label="Дубляж")],
    )


def _serial() -> ContentResponse:
    return ContentResponse(
        id="p1:serial-1",
        type="series",
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
    return SearchResult(
        id=f"{pid}:{id_}",
        provider=pid,
        type=cast(Any, media_type),
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


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Snapshot + restore PROVIDERS and every cache the facade reads so no
    real upstream calls or stale state leak into assertions."""
    saved_providers = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (_home_cache, _home_sources_cache, _content_cache, _blocklist_cache):
        cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        for cache in (_home_cache, _home_sources_cache, _content_cache, _blocklist_cache):
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
    assert source["Path"] == f"/videos/{gk}"
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
        "/Items/g1:deadbeefdeadbeef/PlaybackInfo", headers={"X-Emby-Token": TOKEN}, json={}
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


def test_requires_token_all_spellings(client: TestClient) -> None:
    assert client.post("/Items/g1:deadbeefdeadbeef/PlaybackInfo", json={}).status_code == 401
    assert client.get("/Items/g1:deadbeefdeadbeef/PlaybackInfo").status_code == 401


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