"""Jellyfin item detail + hierarchy (ticket #105).

Ticket #105's acceptance, pinned at the HTTP seam:

  - ``GET /Items/{g1:...}`` resolves a movie/series group key to its full
    detail via the shared ``resolve_group`` map: ``Type: Movie/Series``,
    ``Name``, ``ProductionYear``, ``Overview`` (description), and
    ``ImageTags.Primary`` iff the item has a poster. Translations are
    hidden on the wire (no ``translations`` field leaks).
  - A Movie has no children: ``GET /Items?parentId=<movie>`` is empty.
  - A Series lists its seasons: ``GET /Items?parentId=<series gk>``
    returns one ``Season`` per ``ContentResponse.seasons``, ids
    ``<gk>:S<n>`` (D2), ``ParentId = <series gk>``, no ImageTags (D9).
  - A Season lists its episodes: ``GET /Items?parentId=<gk:S<n>>``
    returns ``Episode`` items carrying the provider-scoped episode ids
    D2 prescribes (``{provider}:{episode.id}``), plus ``IndexNumber``/
    ``ParentIndexNumber``/``SeriesId`` for breadcrumbs.
  - ``GET /Items/{gk:S<n>}`` returns the single season; an episode id is
    *not* reverse-resolvable on its own (it is served through the season
    listing), so it 404s with the same "item unavailable" semantics as a
    cold group key (D2).
  - Resolution runs exactly ONE upstream ``content()`` call per unique
    group key (cached at native ``content:{provider}:{external}``), and
    passes the BARE external id to the provider — providers like animeon
    reject the composite ``provider:external`` form (``_EXTERNAL_ID_RE``).
  - Cold resolution cache → 404 (item unavailable), which Jellyfin
    clients tolerate.
  - All routes sit behind the same ``require_token`` gate (D4).

Seeded via the same seam as ``test_jellyfin_views``: one stub provider
surfaces a movie and a series through ``/api/home`` (populating the
shared home snapshot + group-key resolution map), and its ``content()``
returns canned detail.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS
from cs_uk_api.main import (
    _blocklist_cache,
    _content_cache,
    _home_cache,
    _home_sources_cache,
)
from cs_uk_api.models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError

TOKEN = SETTINGS.jellyfin_token
USER = "fdc808859fc45eb8ac5aa6faddc12c72"

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
        translations=[Translation(id="uk", label="Дубляж"), Translation(id="en", label="En")],
        seasons=[
            Season(
                number=1,
                episodes=[
                    Episode(number=1, id="s1e1", title="Серія 1"),
                    Episode(number=2, id="s1e2", title="Серія 2"),
                ],
            ),
            Season(
                number=2,
                episodes=[
                    Episode(number=1, id="s2e1", title="Перша серія другого сезону"),
                ],
            ),
        ],
    )


class _DetailStub(BaseProvider):
    """One home-capable provider whose ``content()`` serves canned detail.

    ``browse`` returns the ``cards`` (populating the home snapshot and
    the group-key resolution map); ``content`` records the external id it
    receives so tests can pin the BARE-id contract.
    """

    id = "p1"
    name = "P1"
    types = ("movie", "series")
    newest_section = "page"

    def __init__(
        self,
        cards: list[SearchResult],
        content_by_external: dict[str, ContentResponse],
    ) -> None:
        self._cards = cards
        self._content_by_external = content_by_external
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
    ) -> Any:
        raise NotImplementedError


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


def _seed() -> _DetailStub:
    """One stub surfacing the movie + series cards through /api/home."""
    return _DetailStub(
        cards=[
            _card("p1", "dune-1", "Дюна", "movie", poster=_POSTER_MOVIE),
            _card("p1", "serial-1", "Сериалал серіал", "series", poster=_POSTER_SERIES),
        ],
        content_by_external={"dune-1": _dune(), "serial-1": _serial()},
    )


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


def _auth(client: TestClient) -> None:
    """Warm the shared home snapshot so the resolution map is populated."""
    r = client.get("/api/home")
    assert r.status_code == 200


def _get(client: TestClient, path: str, **params: str) -> dict[str, Any]:
    r = client.get(
        path,
        params=params or None,
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    return cast("dict[str, Any]", r.json())


def _items_of(client: TestClient, parent_id: str) -> list[dict[str, Any]]:
    body = _get(client, "/Items", parentId=parent_id, userId=USER)
    return cast("list[dict[str, Any]]", body["Items"])


def _movie_gk(client: TestClient) -> str:
    body = _get(client, "/api/home")
    for row in body["rows"]:
        for item in row["items"]:
            if item["title"] == "Дюна":
                return cast(str, item["group_key"])
    raise AssertionError("Дюна not in seeded home")


def _serial_gk(client: TestClient) -> str:
    body = _get(client, "/api/home")
    for row in body["rows"]:
        for item in row["items"]:
            if item["title"] == "Сериалал серіал":
                return cast(str, item["group_key"])
    raise AssertionError("Сериалал серіал not in the seeded home")


def test_movie_detail_full_view(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _movie_gk(client)

    dto = _get(client, f"/Items/{gk}", userId=USER)
    assert dto["Id"] == gk
    assert dto["Type"] == "Movie"
    assert dto["Name"] == "Дюна"
    assert dto["ProductionYear"] == 2021
    assert dto["Overview"] == "Епічна науково-фантастична стрічка."
    assert set(dto["ImageTags"].keys()) == {"Primary"}
    # Translations stay server-side (hidden on the wire).
    assert "translations" not in dto


def test_series_detail_full_view(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _serial_gk(client)

    dto = _get(client, f"/Items/{gk}", userId=USER)
    assert dto["Id"] == gk
    assert dto["Type"] == "Series"
    assert dto["Name"] == "Сериалал серіал"
    assert dto["ProductionYear"] == 2023
    assert dto["Overview"] == "Детективний серіал."
    assert set(dto["ImageTags"].keys()) == {"Primary"}
    assert "translations" not in dto


def test_movie_detail_has_no_children(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _movie_gk(client)

    body = _get(client, "/Items", parentId=gk, userId=USER)
    assert body["Items"] == []
    assert body["TotalRecordCount"] == 0


def test_detail_image_tag_agrees_with_poster_route(client: TestClient) -> None:
    """D9 coherence: the detail's ``ImageTags.Primary`` and the poster
    route draw from the same poster source. A card without art yields
    BOTH no tag AND a 404 — never a dangling ``ImageTags`` that points
    at an image the facade will refuse to serve."""
    card = _card("p1", "dune-1", "Дюна", "movie", poster=None)
    stub = _DetailStub(
        cards=[card],
        content_by_external={"dune-1": _dune()},
    )
    PROVIDERS["p1"] = stub
    _auth(client)

    # The detail is fully resolvable, and the content DOES carry a
    # poster — but the home card does not, so the tag and route agree
    # on "no art" (the poster proxy only serves card art, D9).
    gk = _movie_gk(client)
    dto = _get(client, f"/Items/{gk}", userId=USER)
    assert dto["ImageTags"] == {}
    r = client.get(f"/Items/{gk}/Images/Primary", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 404


def test_series_season_listing(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _serial_gk(client)

    seasons = _items_of(client, gk)
    assert [s["Name"] for s in seasons] == ["Сезон 1", "Сезон 2"]
    assert all(isinstance(s["IndexNumber"], int) for s in seasons)
    assert all(s["Type"] == "Season" for s in seasons)
    assert all(s["ParentId"] == gk for s in seasons)
    assert [s["Id"] for s in seasons] == [f"{gk}:S1", f"{gk}:S2"]
    # D9: seasons carry no art.
    assert all(s["ImageTags"] == {} for s in seasons)


def test_season_episode_listing(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _serial_gk(client)
    season_id = f"{gk}:S1"

    episodes = _items_of(client, season_id)
    assert [e["Name"] for e in episodes] == ["Серія 1", "Серія 2"]
    assert all(e["Type"] == "Episode" for e in episodes)
    assert all(e["ParentId"] == season_id for e in episodes)
    assert all(e["SeriesId"] == gk for e in episodes)
    assert [e["IndexNumber"] for e in episodes] == [1, 2]
    assert all(e["ParentIndexNumber"] == 1 for e in episodes)
    # Episode ids keep the provider-scoped suffix D2 requires: they are
    # stable wire ids the PlaybackInfo/stream tickets consume directly.
    assert [e["Id"] for e in episodes] == ["p1:s1e1", "p1:s1e2"]
    assert all(e["ImageTags"] == {} for e in episodes)


def test_episode_ids_reuse_provider_prefixed_id_without_doubling(
    client: TestClient,
) -> None:
    """Some real providers (uakino, kinotron) already embed their ``{provider}:``
    prefix in ``episode.id`` — e.g. ``akino:{news_id}:e{n}``. D2 pins the
    existing id UNCHANGED, so the facade must not double it to
    ``akino:akino:…``; the PlaybackInfo/stream tickets consume the native
    stream id exactly (parent prefix present exactly once)."""
    serial = _serial()
    serial.seasons = [
        Season(
            number=1,
            episodes=[
                Episode(number=1, id="p1:serial-1:s1e1", title="Серія 1"),
                Episode(number=2, id="s1e2", title="Серія 2"),
            ],
        )
    ]
    stub = _DetailStub(
        cards=[_card("p1", "serial-1", "Сериалал серіал", "series", poster=_POSTER_SERIES)],
        content_by_external={"serial-1": serial},
    )
    PROVIDERS["p1"] = stub
    _auth(client)
    gk = _serial_gk(client)

    episodes = _items_of(client, f"{gk}:S1")
    assert [e["Id"] for e in episodes] == ["p1:serial-1:s1e1", "p1:s1e2"]


def test_unknown_season_suffix_404(client: TestClient) -> None:
    """A g1 key with a season suffix that doesn't exist is 404 — same
    "item unavailable" verdict as a cold or unknown group key."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _serial_gk(client)

    r = client.get(f"/Items/{gk}:S9", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 404


def test_episode_id_not_reverse_resolvable_404(client: TestClient) -> None:
    """An episode id on its own is served through the season listing, not
    via /Items/{id}: without its parent season there is no group key, so
    the facade answers 404 like any other unresolvable id (D2)."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    r = client.get("/Items/p1:s1e1", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 404


def test_cold_cache_404(client: TestClient) -> None:
    """Empty home + empty resolution map → /Items/{g1:...} 404s (D2)."""
    r = client.get("/Items/g1:deadbeefdeadbeef", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 404


def test_unknown_group_key_after_home_404(client: TestClient) -> None:
    PROVIDERS["p1"] = _seed()
    _auth(client)
    r = client.get("/Items/g1:0000000000000000", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 404


def test_detail_resolution_single_upstream_call_and_bare_external_id(
    client: TestClient,
) -> None:
    """Detail resolves through the group map in EXACTLY ONE upstream
    ``content()`` call, passing the provider's BARE external id — the
    composite ``provider:external`` form is rejected by real providers
    like animeon."""
    stub = _seed()
    PROVIDERS["p1"] = stub
    _auth(client)
    gk = _movie_gk(client)

    _get(client, f"/Items/{gk}", userId=USER)
    assert stub.content_calls == ["dune-1"]


def test_detail_cached_second_request_no_upstream_call(
    client: TestClient,
) -> None:
    stub = _seed()
    PROVIDERS["p1"] = stub
    _auth(client)
    gk = _movie_gk(client)

    _get(client, f"/Items/{gk}", userId=USER)
    _get(client, f"/Items/{gk}", userId=USER)
    # The content response is cached under the same
    # ``content:{provider}:{external}`` key the native route uses, so a
    # second detail view costs nothing.
    assert len(stub.content_calls) == 1


def test_hierarchy_requires_token(client: TestClient) -> None:
    assert client.get("/Items/g1:deadbeefdeadbeef").status_code == 401
    assert client.get("/Items", params={"parentId": "g1:deadbeefdeadbeef"}).status_code == 401
