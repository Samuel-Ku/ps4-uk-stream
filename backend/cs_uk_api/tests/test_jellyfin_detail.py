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

import hashlib
import importlib
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cs_uk_api import catalog_state
from cs_uk_api.catalog_state import clear_playback, record_playback
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
    Person,
    SearchResult,
    Season,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError, model_b_axes
from cs_uk_api.recommend import profile_from_content
from cs_uk_api.resume_store import ResumeStore

TOKEN = SETTINGS.jellyfin_token
USER = "fdc808859fc45eb8ac5aa6faddc12c72"

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
        translations=[Translation(id="uk", label="Дубляж"), Translation(id="en", label="En")],
        seasons=[
            Season(
                number=1,
                episodes=[
                    # Ticket #223: episode 1 carries the metadata a
                    # provider can expose (animeon's ``aired``); episode
                    # 2 has none — the DTO must emit the field only on
                    # the first.
                    Episode(
                        number=1, id="s1e1", title="Серія 1",
                        premiere_date="2002-10-03",
                    ),
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
    mb_form, mb_styles = model_b_axes(cast(Any, media_type))
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
    clear_playback()
    for cache in (_home_cache, _home_sources_cache, _content_cache, _blocklist_cache):
        cache.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        clear_playback()
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


def test_movie_detail_carries_people(client: TestClient) -> None:
    """Ticket #221: when the resolved provider's content page exposes
    cast, the detail DTO carries ``People`` (Jellyfin's wire shape
    ``{Id, Name, Role}``) so Switchfin renders the rail."""
    dune = _dune()
    dune.people = [
        Person(id="p1:actors:Тімоті Шаламе", name="Тімоті Шаламе"),
        Person(id="p1:actors:Зендея", name="Зендея", role="Actor"),
    ]
    stub = _DetailStub(
        cards=[_card("p1", "dune-1", "Дюна", "movie", poster=_POSTER_MOVIE)],
        content_by_external={"dune-1": dune},
    )
    PROVIDERS["p1"] = stub
    _auth(client)
    gk = _movie_gk(client)

    dto = _get(client, f"/Items/{gk}", userId=USER)
    assert dto["People"] == [
        {"Id": "p1:actors:Тімоті Шаламе", "Name": "Тімоті Шаламе", "Role": "Actor"},
        {"Id": "p1:actors:Зендея", "Name": "Зендея", "Role": "Actor"},
    ]


def test_movie_detail_without_people_omits_rail(client: TestClient) -> None:
    """Ticket #221: no cast on the content page → an empty People list;
    Switchfin hides the rail rather than rendering an empty header."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _movie_gk(client)

    dto = _get(client, f"/Items/{gk}", userId=USER)
    assert dto["People"] == []


def test_movie_detail_carries_community_rating(client: TestClient) -> None:
    """Ticket #222: when the resolved provider's content page exposed a
    rating (klontv's JSON-LD aggregateRating), the detail DTO carries
    ``CommunityRating`` so the badge renders the number."""
    dune = _dune()
    dune.rating = 8.9
    stub = _DetailStub(
        cards=[_card("p1", "dune-1", "Дюна", "movie", poster=_POSTER_MOVIE)],
        content_by_external={"dune-1": dune},
    )
    PROVIDERS["p1"] = stub
    _auth(client)
    gk = _movie_gk(client)

    dto = _get(client, f"/Items/{gk}", userId=USER)
    assert dto["CommunityRating"] == 8.9


def test_movie_detail_without_rating_omits_badge(client: TestClient) -> None:
    """Ticket #222: no rating on the content page → CommunityRating is
    omitted (not 0) so Switchfin hides the badge instead of showing a
    bogus zero."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _movie_gk(client)

    dto = _get(client, f"/Items/{gk}", userId=USER)
    assert "CommunityRating" not in dto


def test_person_detail_returns_person_dto(client: TestClient) -> None:
    """Ticket #221: tapping a person in the People rail opens
    ``GET /Persons/{id}`` — the facade answers a Person-shaped DTO whose
    Name is decoded from the provider-scoped id (the id's final segment
    carries the display name)."""
    _auth(client)

    dto = _get(client, "/Persons/p1:actors:Джозеф Морґан")
    assert dto["Type"] == "Person"
    assert dto["Id"] == "p1:actors:Джозеф Морґан"
    assert dto["Name"] == "Джозеф Морґан"



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


def test_detail_image_tag_agrees_with_poster_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D9 coherence: the detail's ``ImageTags.Primary`` and the poster
    route draw from the same poster source. A card without art falls
    back to ``content.poster`` — so the tag and the route agree: either
    both serve the poster or both empty (a dangling ``ImageTags`` that
    points at an image the facade refuses to serve never happens)."""
    card = _card("p1", "dune-1", "Дюна", "movie", poster=None)
    stub = _DetailStub(
        cards=[card],
        content_by_external={"dune-1": _dune()},
    )
    PROVIDERS["p1"] = stub
    _auth(client)

    jf_router = importlib.import_module("cs_uk_api.jellyfin.router")
    async def _fake_poster(url: str, client: Any) -> tuple[bytes, str]:
        return b"\x89PNG", "image/png"

    monkeypatch.setattr(jf_router, "fetch_poster_bytes", _fake_poster)

    # The home card carries no art, but the resolved content DOES carry
    # a poster — so tag and route agree on serving the content poster.
    gk = _movie_gk(client)
    dto = _get(client, f"/Items/{gk}", userId=USER)
    tag = dto["ImageTags"].get("Primary")
    r = client.get(f"/Items/{gk}/Images/Primary", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    assert tag  # tag present ⟺ route serves
    assert tag == hashlib.sha256(_dune().poster.encode()).hexdigest()[:16]


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
    # Ticket #223: PremiereDate renders only where the provider exposed
    # it — episode 1 has the date, episode 2 has none (omitted, not
    # null).
    assert episodes[0]["PremiereDate"] == "2002-10-03"
    assert "PremiereDate" not in episodes[1]


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


def test_shows_seasons_route(client: TestClient) -> None:
    """Switchfin opens a series via ``/Shows/{id}/Seasons`` (not the
    items-listing ``parentId`` spelling) — the route serves the same D3
    season DTOs, so the series' episodes become reachable."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _serial_gk(client)

    r = client.get(
        f"/Shows/{gk}/Seasons", params={"userId": USER}, headers={"X-Emby-Token": TOKEN}
    )
    assert r.status_code == 200
    seasons = r.json()["Items"]
    assert [s["Id"] for s in seasons] == [f"{gk}:S1", f"{gk}:S2"]
    assert all(s["Type"] == "Season" for s in seasons)


def test_shows_episodes_route(client: TestClient) -> None:
    """The companion ``/Shows/{id}/Episodes?seasonId=`` spelling serves
    the same episode DTOs the season parent lists."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _serial_gk(client)

    r = client.get(
        f"/Shows/{gk}/Episodes",
        params={"seasonId": f"{gk}:S1", "userId": USER},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 200
    episodes = r.json()["Items"]
    assert [e["Id"] for e in episodes] == ["p1:s1e1", "p1:s1e2"]
    assert all(e["Type"] == "Episode" for e in episodes)


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


def test_known_card_detail_degrades_when_upstream_blips(
    client: TestClient,
) -> None:
    """#224: a card that IS in the home snapshot but whose live
    ``content()`` resolution fails transiently (upstream blip — run8:
    animeon ``unreachable`` then 502) still answers the detail with the
    card's own data instead of a hard 404 that blanks the page mid-run.
    """
    stub = _DetailStub(
        cards=[_card("p1", "dune-1", "Дюна", "movie", poster=_POSTER_MOVIE)],
        # content() raises ProviderError for every id — the transient
        # upstream-failure path resolve_group_content retries and gives
        # up on.
        content_by_external={},
    )
    PROVIDERS["p1"] = stub
    _auth(client)
    gk = _movie_gk(client)

    body = _get(client, f"/Items/{gk}", userId=USER)
    # The card data still renders: title, type, year, poster tag.
    assert body["Name"] == "Дюна"
    assert body["Type"] == "Movie"
    assert body["ProductionYear"] == 2021
    assert body["ImageTags"]["Primary"]


def test_degraded_detail_keeps_d2_404_for_hard_unavailable(
    client: TestClient,
) -> None:
    """#224: the degraded card answer must NOT mask deliberate 404
    verdicts. A cold cache (no home) and an unknown group key still 404
    exactly as D2 prescribes."""
    # cold cache — a g2: key with NO home snapshot and NO sources map:
    # the guard must not degrade (there is no card to degrade to).
    assert client.get("/Items/g2:0000000000000000", headers={"X-Emby-Token": TOKEN}).status_code == 404
    # known home, unknown group key — same verdict
    PROVIDERS["p1"] = _seed()
    _auth(client)
    assert client.get("/Items/g2:0000000000000000", headers={"X-Emby-Token": TOKEN}).status_code == 404
    # a season-suffixed id never degrades to card data (no season info
    # in a card)
    gk = _serial_gk(client)
    assert client.get(f"/Items/{gk}:S9", headers={"X-Emby-Token": TOKEN}).status_code == 404


def test_hierarchy_requires_token(client: TestClient) -> None:
    assert client.get("/Items/g1:deadbeefdeadbeef").status_code == 401
    assert client.get("/Items", params={"parentId": "g1:deadbeefdeadbeef"}).status_code == 401


# ------------------------------------------------------------ playback shelf (#214)


def _post_playback(client: TestClient, item_id: str, position: int) -> None:
    r = client.post(
        "/Sessions/Playing/Stopped",
        json={"ItemId": item_id, "PositionTicks": position},
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 204


def test_playback_report_seeds_resume_and_nextup(client: TestClient) -> None:
    """#214: a Stopped report for a played episode lights up Continue
    watching — Resume returns the episode with PlaybackPositionTicks and
    NextUp returns its successor in the season. The played id is the
    provider-prefixed wire id real providers emit
    (``{provider}:{external}:s1e1``)."""
    serial = _serial()
    serial.seasons = [
        Season(
            number=1,
            episodes=[
                Episode(number=1, id="p1:serial-1:s1e1", title="Серія 1"),
                Episode(number=2, id="p1:serial-1:s1e2", title="Серія 2"),
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
    assert episodes[0]["Id"] == "p1:serial-1:s1e1"

    _post_playback(client, "p1:serial-1:s1e1", 600_000_000)

    resume = _get(client, "/Users/user1/Items/Resume")
    assert len(resume["Items"]) == 1
    item = resume["Items"][0]
    assert item["Id"] == "p1:serial-1:s1e1"
    assert item["Type"] == "Episode"
    assert item["SeriesName"] == "Сериалал серіал"
    assert item["PlaybackPositionTicks"] == 600_000_000
    assert resume["TotalRecordCount"] == 1

    nextup = _get(client, "/Shows/NextUp")
    assert len(nextup["Items"]) == 1
    nxt = nextup["Items"][0]
    assert nxt["Id"] == "p1:serial-1:s1e2"
    assert nxt["IndexNumber"] == 2
    assert nxt["SeriesId"] == gk


def test_playback_report_episode_with_nonmatching_prefix_seeds_resume(
    client: TestClient,
) -> None:
    """#214: real providers' episode wire ids do NOT carry the group's
    SearchResult id as prefix — uakino emits ``{provider}:{news_id}:eN``
    while its card id is ``{provider}:{section}:{news_id}-{slug}``, and
    animeon appends a base64 blob (``:e1:eyJ...``). Resume/NextUp must
    still resolve the group: item-id fuzzy match + tolerate the blob
    tail."""
    serial = _serial()
    serial.seasons = [
        Season(
            number=1,
            episodes=[
                # uakino-style: bare numeric prefix, NOT the card id.
                Episode(number=1, id="p1:6268:e1", title="Серія 1"),
                Episode(number=2, id="p1:6268:e2", title="Серія 2"),
            ],
        )
    ]
    stub = _DetailStub(
        cards=[
            _card(
                "p1", "anime-series:6268-narutto-1-sezon",
                "Наруто", "series", poster=_POSTER_SERIES,
            )
        ],
        content_by_external={"anime-series:6268-narutto-1-sezon": serial},
    )
    PROVIDERS["p1"] = stub
    _auth(client)

    _post_playback(client, "p1:6268:e1", 600_000_000)

    resume = _get(client, "/Users/user1/Items/Resume")
    assert len(resume["Items"]) == 1
    assert resume["Items"][0]["Id"] == "p1:6268:e1"
    assert resume["Items"][0]["PlaybackPositionTicks"] == 600_000_000

    nextup = _get(client, "/Shows/NextUp")
    assert len(nextup["Items"]) == 1
    assert nextup["Items"][0]["Id"] == "p1:6268:e2"


def test_playback_report_animeon_encoded_episode_seeds_resume(
    client: TestClient,
) -> None:
    """#214: animeon's episode wire id appends a base64 source blob after
    the ``:eN`` suffix (``animeon:918:e1:eyJ...``). The episode-tail regex
    must tolerate the trailing blob, and the numeric prefix must resolve
    the group."""
    import base64 as _b64

    blob = _b64.b64encode(b'{"id":918,"episode":1,"sources":[]}').decode()
    serial = _serial()
    serial.seasons = [
        Season(
            number=1,
            episodes=[
                Episode(number=1, id=f"p1:918:e1:{blob}", title="Серія 1"),
                Episode(number=2, id=f"p1:918:e2:{blob}", title="Серія 2"),
            ],
        )
    ]
    stub = _DetailStub(
        cards=[_card("p1", "918", "Наруто", "series", poster=_POSTER_SERIES)],
        content_by_external={"918": serial},
    )
    PROVIDERS["p1"] = stub
    _auth(client)

    _post_playback(client, f"p1:918:e1:{blob}", 600_000_000)

    resume = _get(client, "/Users/user1/Items/Resume")
    assert len(resume["Items"]) == 1
    assert resume["Items"][0]["Id"] == f"p1:918:e1:{blob}"
    assert resume["Items"][0]["PlaybackPositionTicks"] == 600_000_000

    nextup = _get(client, "/Shows/NextUp")
    assert len(nextup["Items"]) == 1
    assert nextup["Items"][0]["Id"] == f"p1:918:e2:{blob}"


def test_playback_report_movie_seeds_resume(client: TestClient) -> None:
    """#214: a movie reports its g2 key, so Resume carries the movie
    card with its position (no NextUp — a movie has no successor)."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _movie_gk(client)

    _post_playback(client, gk, 1_500_000_000)

    resume = _get(client, "/Users/user1/Items/Resume")
    assert len(resume["Items"]) == 1
    assert resume["Items"][0]["Id"] == gk
    assert resume["Items"][0]["Type"] == "Movie"
    assert resume["Items"][0]["PlaybackPositionTicks"] == 1_500_000_000

    nextup = _get(client, "/Shows/NextUp")
    assert nextup["Items"] == []


def test_playback_report_ignores_zero_position(client: TestClient) -> None:
    """#214: a just-started report (PositionTicks 0) must not seed the
    shelf — nothing watched yet."""
    serial = _serial()
    serial.seasons = [
        Season(
            number=1,
            episodes=[
                Episode(number=1, id="p1:serial-1:s1e1", title="Серія 1"),
                Episode(number=2, id="p1:serial-1:s1e2", title="Серія 2"),
            ],
        )
    ]
    stub = _DetailStub(
        cards=[_card("p1", "serial-1", "Сериалал серіал", "series", poster=_POSTER_SERIES)],
        content_by_external={"serial-1": serial},
    )
    PROVIDERS["p1"] = stub
    _auth(client)

    _post_playback(client, "p1:serial-1:s1e1", 0)

    resume = _get(client, "/Users/user1/Items/Resume")
    assert resume["Items"] == []
    # The zero report must not block a later real position.
    _post_playback(client, "p1:serial-1:s1e1", 700_000_000)
    resume = _get(client, "/Users/user1/Items/Resume")
    assert len(resume["Items"]) == 1
    assert resume["Items"][0]["PlaybackPositionTicks"] == 700_000_000


def test_playback_report_for_unknown_item_is_ignored(client: TestClient) -> None:
    """#214: a report for an item outside the catalog (cold group key,
    unknown composite) must not break Resume/NextUp — the shelf just
    skips it."""
    PROVIDERS["p1"] = _seed()
    _auth(client)

    _post_playback(client, "p9:unknown-title:s1e7", 1_000_000_000)

    resume = _get(client, "/Users/user1/Items/Resume")
    assert resume["Items"] == []
    nextup = _get(client, "/Shows/NextUp")
    assert nextup["Items"] == []


# ------------------------------------------------------------ finished + cap (#249)


def _post_playback_full(
    client: TestClient, item_id: str, position: int, runtime: int | None = None
) -> None:
    body: dict[str, Any] = {"ItemId": item_id, "PositionTicks": position}
    if runtime is not None:
        body["RunTimeTicks"] = runtime
    r = client.post(
        "/Sessions/Playing/Stopped",
        json=body,
        headers={"X-Emby-Token": TOKEN},
    )
    assert r.status_code == 204


def _episode_serial() -> ContentResponse:
    serial = _serial()
    serial.seasons = [
        Season(
            number=1,
            episodes=[
                Episode(number=1, id="p1:serial-1:s1e1", title="Серія 1"),
                Episode(number=2, id="p1:serial-1:s1e2", title="Серія 2"),
            ],
        )
    ]
    return serial


def test_finished_episode_clears_resume_and_nextup(client: TestClient) -> None:
    """#249: a Stopped report at >=95% of the runtime removes the
    episode from Resume and it stops feeding NextUp."""
    PROVIDERS["p1"] = _DetailStub(
        cards=[_card("p1", "serial-1", "Сериалал серіал", "series", poster=_POSTER_SERIES)],
        content_by_external={"serial-1": _episode_serial()},
    )
    _auth(client)

    _post_playback_full(client, "p1:serial-1:s1e1", 950, runtime=1000)

    assert _get(client, "/Users/user1/Items/Resume")["Items"] == []
    assert _get(client, "/Shows/NextUp")["Items"] == []


def test_finished_movie_clears_resume(client: TestClient) -> None:
    """#249: the finished rule applies to movies (g2: keys) exactly as
    to episodes."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _movie_gk(client)

    _post_playback_full(client, gk, 950, runtime=1000)

    assert _get(client, "/Users/user1/Items/Resume")["Items"] == []


def test_report_without_runtime_never_finished(client: TestClient) -> None:
    """#249: a big position with no known runtime keeps the item on the
    shelf — items without a runtime are never auto-finished."""
    PROVIDERS["p1"] = _DetailStub(
        cards=[_card("p1", "serial-1", "Сериалал серіал", "series", poster=_POSTER_SERIES)],
        content_by_external={"serial-1": _episode_serial()},
    )
    _auth(client)

    _post_playback(client, "p1:serial-1:s1e1", 1_000_000_000)

    resume = _get(client, "/Users/user1/Items/Resume")
    assert len(resume["Items"]) == 1
    assert resume["Items"][0]["PlaybackPositionTicks"] == 1_000_000_000


def test_resume_row_capped_at_20_most_recent(client: TestClient) -> None:
    """#249: the resume read returns at most the 20 most recently
    updated items, most recent first — the row stays scannable. Uses a
    store with an injected clock so recency is deterministic. Episodes
    (provider wire ids) resolve through the group map, so 21 distinct
    items need only one home card."""
    n = 21
    serial = _serial()
    serial.seasons = [
        Season(
            number=1,
            episodes=[
                Episode(number=i, id=f"p1:serial-1:s1e{i}", title=f"Серія {i}")
                for i in range(1, n + 1)
            ],
        )
    ]
    PROVIDERS["p1"] = _DetailStub(
        cards=[_card("p1", "serial-1", "Сериалал серіал", "series", poster=_POSTER_SERIES)],
        content_by_external={"serial-1": serial},
    )
    _auth(client)

    episode_ids = [f"p1:serial-1:s1e{i}" for i in range(1, n + 1)]

    clock = {"t": 1000.0}

    def _now() -> float:
        clock["t"] += 1.0
        return clock["t"]

    original = catalog_state._resume_store
    catalog_state._resume_store = ResumeStore(None, now=_now)
    try:
        for ep_id in episode_ids:
            record_playback(ep_id, 1000)
        resume = _get(client, "/Users/user1/Items/Resume")["Items"]
        assert len(resume) == 20
        assert resume[0]["Name"] == "Серія 21"  # most recent first
        assert resume[-1]["Name"] == "Серія 2"  # the oldest (Серія 1) is outside the 20
    finally:
        catalog_state._resume_store = original


# ------------------------------------------------------------ runtime on the wire (#250)


def test_resume_movie_carries_runtime(client: TestClient) -> None:
    """#250: a movie stopped at ~40% comes back from Resume with both
    PlaybackPositionTicks and RunTimeTicks, so the bar renders
    proportionally."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _movie_gk(client)

    _post_playback_full(client, gk, 40_000_000_000, runtime=100_000_000_000)

    item = _get(client, "/Users/user1/Items/Resume")["Items"][0]
    assert item["Id"] == gk
    assert item["PlaybackPositionTicks"] == 40_000_000_000
    assert item["RunTimeTicks"] == 100_000_000_000


def test_resume_without_runtime_position_only(client: TestClient) -> None:
    """#250: a report without a runtime yields a position-only DTO — no
    fabricated duration on the wire."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    gk = _movie_gk(client)

    _post_playback(client, gk, 40_000_000_000)

    item = _get(client, "/Users/user1/Items/Resume")["Items"][0]
    assert item["PlaybackPositionTicks"] == 40_000_000_000
    assert "RunTimeTicks" not in item


def test_nextup_carries_runtime(client: TestClient) -> None:
    """#250: the NextUp episode DTO carries the recorded runtime the
    same way as Resume."""
    PROVIDERS["p1"] = _DetailStub(
        cards=[_card("p1", "serial-1", "Сериалал серіал", "series", poster=_POSTER_SERIES)],
        content_by_external={"serial-1": _episode_serial()},
    )
    _auth(client)

    _post_playback_full(client, "p1:serial-1:s1e1", 600_000_000, runtime=1_000_000_000)

    nxt = _get(client, "/Shows/NextUp")["Items"][0]
    assert nxt["Id"] == "p1:serial-1:s1e2"
    assert nxt["RunTimeTicks"] == 1_000_000_000


# ------------------------------------------------------------ recommendations (#252)


def _movie_content(ext: str, title: str, genres: list[str], year: int) -> ContentResponse:
    return ContentResponse(
        id=f"p1:{ext}",
        form="movie",
        title=title,
        year=year,
        poster=_POSTER_MOVIE,
        translations=[Translation(id="uk", label="Дубляж")],
        genres=genres,
    )


def test_recommendation_views_serve_rows(client: TestClient) -> None:
    """#252: the personalized rows surface as facade views and serve
    ranked cards through the existing view mechanism — zero client
    changes. A watched item is excluded; a matching query boosts a
    candidate into the row."""
    action1 = _movie_content("action-1", "Боєвик", ["Бойовик"], 2021)
    action2 = _movie_content("action-2", "Боєвик 2", ["Бойовик"], 2022)
    drama = _movie_content("drama-1", "Драма", ["Драма"], 1990)
    stub = _DetailStub(
        cards=[
            _card("p1", "action-1", "Боєвик", "movie", poster=_POSTER_MOVIE),
            _card("p1", "action-2", "Боєвик 2", "movie", poster=_POSTER_MOVIE),
            _card("p1", "drama-1", "Драма", "movie", poster=_POSTER_MOVIE),
        ],
        content_by_external={"action-1": action1, "action-2": action2, "drama-1": drama},
    )
    PROVIDERS["p1"] = stub
    _auth(client)

    home = _get(client, "/api/home")
    gk = {item["title"]: item["group_key"] for row in home["rows"] for item in row["items"]}
    # The background profile warm is off in tests — pre-populate the
    # profiles the same shape it would produce.
    catalog_state._profiles = {
        gk["Боєвик"]: profile_from_content(action1),
        gk["Боєвик 2"]: profile_from_content(action2),
        gk["Драма"]: profile_from_content(drama),
    }
    try:
        # Taste signal: «Боєвик» was watched; «Драма» was searched.
        record_playback(gk["Боєвик"], 1_000_000_000)
        catalog_state.record_search_query("Драма")
        # Rebuild the snapshot so the recommendation rows bake in.
        _home_cache.clear()

        views = _get(client, "/Users/user1/Views")["Items"]
        names = [v["Name"] for v in views]
        assert "Рекомендовано для тебе" in names
        assert "Схоже на Боєвик" in names

        rec = next(v for v in views if v["Name"] == "Рекомендовано для тебе")
        items = _get(client, "/Items", parentId=rec["Id"], userId=USER)["Items"]
        # «Боєвик 2» scores on the anchor; «Драма» rides the query boost;
        # the watched «Боєвик» is excluded.
        assert [i["Name"] for i in items] == ["Боєвик 2", "Драма"]

        sim = next(v for v in views if v["Name"] == "Схоже на Боєвик")
        sim_items = _get(client, "/Items", parentId=sim["Id"], userId=USER)["Items"]
        assert [i["Name"] for i in sim_items] == ["Боєвик 2"]
    finally:
        catalog_state._profiles = {}


def test_recommendation_excludes_any_recorded_position(client: TestClient) -> None:
    """#253 AC4: the scorer excludes items with ANY recorded playback
    position — not just the top-3 recency anchors. Four movies are
    watched; the oldest one (outside the anchor window) must still be
    missing from the recommended row."""
    movies = [
        _movie_content(f"m{i}", f"Бойовик {i}", ["Бойовик"], 2021)
        for i in range(1, 6)
    ]
    stub = _DetailStub(
        cards=[
            _card("p1", f"m{i}", f"Бойовик {i}", "movie", poster=_POSTER_MOVIE)
            for i in range(1, 6)
        ],
        content_by_external={f"m{i}": movies[i - 1] for i in range(1, 6)},
    )
    PROVIDERS["p1"] = stub
    _auth(client)

    home = _get(client, "/api/home")
    gk = {item["title"]: item["group_key"] for row in home["rows"] for item in row["items"]}
    catalog_state._profiles = {
        gk[f"Бойовик {i}"]: profile_from_content(movies[i - 1]) for i in range(1, 6)
    }
    try:
        # All five movies share the «Бойовик» genre; four are watched.
        # Record in ascending order so Бойовик 4 is the most recent
        # anchor and Бойовик 1 falls OUTSIDE the top-3 anchor window.
        for i in range(1, 5):
            record_playback(gk[f"Бойовик {i}"], 1_000_000_000)
        _home_cache.clear()

        views = _get(client, "/Users/user1/Views")["Items"]
        rec = next(v for v in views if v["Name"] == "Рекомендовано для тебе")
        items = _get(client, "/Items", parentId=rec["Id"], userId=USER)["Items"]
        names = {i["Name"] for i in items}
        # Only the unwatched movie may appear — every recorded position
        # excludes its group, including the oldest one.
        assert names == {"Бойовик 5"}
    finally:
        catalog_state._profiles = {}


def test_home_recommended_row_signal_and_no_fetch(client: TestClient) -> None:
    """#254 AC1/AC3/AC6: the NATIVE /api/home carries
    «Рекомендовано для тебе» once there is signal (watched + query),
    omits it with none, and the row computation never fetches — it runs
    off the in-memory profiles only (AC6: recompute without blocking)."""
    action = _movie_content("action-1", "Боєвик", ["Бойовик"], 2021)
    drama = _movie_content("drama-1", "Драма", ["Драма"], 1990)
    stub = _DetailStub(
        cards=[
            _card("p1", "action-1", "Боєвик", "movie", poster=_POSTER_MOVIE),
            _card("p1", "drama-1", "Драма", "movie", poster=_POSTER_MOVIE),
        ],
        content_by_external={"action-1": action, "drama-1": drama},
    )
    PROVIDERS["p1"] = stub
    _auth(client)

    def _row_types() -> list[str]:
        home = _get(client, "/api/home")
        return [r["type"] for r in home["rows"]]

    # AC3: no watch history, no queries → row omitted.
    assert "recommended" not in _row_types()

    # Warm the profiles the same shape the background builder would.
    home = _get(client, "/api/home")
    gk = {item["title"]: item["group_key"] for row in home["rows"] for item in row["items"]}
    catalog_state._profiles = {
        gk["Боєвик"]: profile_from_content(action),
        gk["Драма"]: profile_from_content(drama),
    }
    try:
        record_playback(gk["Боєвик"], 1_000_000_000)
        catalog_state.record_search_query("Драма")
        _home_cache.clear()

        # AC1: home includes the row once there is signal, ranked with
        # the watched item excluded.
        home = _get(client, "/api/home")
        rec = next(r for r in home["rows"] if r["type"] == "recommended")
        titles = [it["title"] for it in rec["items"]]
        assert titles == ["Драма"]

        # AC6: the row computation ran off cached profiles — the provider
        # saw NO content() call from the rebuild (the warm is disabled in
        # tests, and the row itself never fetches).
        assert stub.content_calls == []
    finally:
        catalog_state._profiles = {}


def test_search_records_query_for_taste(client: TestClient) -> None:
    """#252: a facade search records the query as taste signal (both
    surfaces feed the shared ``merged_search``)."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    _get(client, "/Search/Hints", searchTerm="Дюна")
    assert catalog_state.recent_search_queries() == ["Дюна"]


def test_native_search_records_query_for_taste(client: TestClient) -> None:
    """#254 AC2: the NATIVE ``/api/search`` surface records the query
    too — not just the facade (spec says "native/facade search queries").
    ``merged_search`` records before the fan-out, so even an empty
    provider result still feeds the taste signal."""
    PROVIDERS["p1"] = _seed()
    _auth(client)
    r = client.get("/api/search?q=Наруто")
    assert r.status_code == 200
    assert catalog_state.recent_search_queries() == ["Наруто"]
