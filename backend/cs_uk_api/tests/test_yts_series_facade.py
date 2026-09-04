"""YTS series — the facade walk (#379, spec #374).

End-to-end assertions over the Jellyfin surface with the REAL
YtsProvider (fixture-mocked upstreams) and the FakeTorrentEngine:

  - a seeded home snapshot carries the English series card (form=series);
  - Seasons → the ``<group_key>:S1`` id; Episodes → the canonical
    ``yts:<imdb>:sNeM`` wire ids the provider emitted;
  - PlaybackInfo on the episode resolves ONE thin source (mp4);
  - ``/Videos/{episode}/stream`` 302s to the ENGINE's LAN URL (the
    direct-redirect posture rides empty StreamResponse headers);
  - a Sessions/Playing/Stopped report under the episode's wire id lands
    in the resume shelf, resolved back through the group map
    (IMDb-derived id stability: the recorded id survives a listing
    refresh because content() is the truth and the id is a pure
    function of provider+imdb+season+episode);
  - NextUp includes the English series' next episode.

Ukrainian-lane behaviour is never touched: every seeded provider here
is a stub EXCEPT yts, whose upstream calls are respx-mocked fixtures.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import quote

import pytest
import respx
from fastapi.testclient import TestClient

from cs_uk_api._catalog_state import blocklist_cache, content_cache, home_cache, sources_cache
from cs_uk_api.config import SETTINGS
from cs_uk_api.models import SearchResult
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider
from cs_uk_api.providers.yts import YtsProvider
from cs_uk_api.torrent_engine import EngineStream, FakeTorrentEngine
from cs_uk_api.wire_identity import episode_wire_id

TOKEN = SETTINGS.jellyfin_token
FIX = pathlib.Path(__file__).parent / "fixtures" / "yts"

_POPCORN = "http://popcorn.lan:9000"
_SHOW_URL = re.compile(rf"{re.escape(_POPCORN)}/show/tt8740758")
_LIST_URL = re.compile(r"https://yts\.gg/api/v2/list_movies\.json\?.*")
_MAGNET_720 = "magnet:?xt=urn:btih:2233445566778899AABBCCDDEEFF001122334455&dn=chernobyl.s01e01.720p&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
_LAN_URL = "http://bitplay.lan:3347/api/v1/torrent/s01/stream/2"
_S1E1 = episode_wire_id("yts", "tt8740758", 1, 1)
_S1E2 = episode_wire_id("yts", "tt8740758", 1, 2)
_S1E3 = episode_wire_id("yts", "tt8740758", 1, 3)
_TITLE = "Чорнобиль англ"


class _EmptyStub(BaseProvider):
    """A registered-but-silent Ukrainian-lane stand-in (nothing to fetch)."""

    id = "p1"
    name = "P1"
    types = ("movie", "series")
    allowed_hosts = frozenset({"p1.example"})

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def content(self, external_id: str, http: Any) -> Any:  # pragma: no cover
        raise AssertionError("not used in this module")

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> Any:  # pragma: no cover
        raise AssertionError("not used in this module")


def _configure(monkeypatch: pytest.MonkeyPatch, base: str = _POPCORN) -> None:
    from dataclasses import replace as dc_replace

    import cs_uk_api.config as config_mod

    monkeypatch.setattr(
        config_mod, "SETTINGS", dc_replace(config_mod.SETTINGS, popcorn_base_url=base)
    )


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
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


def _seed(monkeypatch: pytest.MonkeyPatch) -> FakeTorrentEngine:
    """Registry = the silent stub + the real YTS provider; the engine is
    the deterministic fake."""
    PROVIDERS["p1"] = _EmptyStub()
    engine = FakeTorrentEngine(
        streams={_MAGNET_720: EngineStream(url=_LAN_URL, container="mp4")}
    )
    # The popcorn base is snapshotted at construction — configure FIRST.
    _configure(monkeypatch)
    PROVIDERS["yts"] = YtsProvider(engine=engine)
    return engine


def _warm_home(client: TestClient) -> str:
    """The series browse warms the snapshot; return the show's group key."""
    with respx.mock(assert_all_called=False) as router:
        # The home sweep walks newest + sections; give the movies listing
        # an empty answer and feed the SERIES browse page.
        router.get(url=_LIST_URL).respond(
            200,
            text=json.dumps(
                {
                    "status": "ok",
                    "data": {"movie_count": 0, "limit": 50, "movies": []},
                }
            ),
        )
        router.get(
            url=re.compile(rf"{re.escape(_POPCORN)}/shows/\d+\?.*")
        ).respond(200, text=(FIX / "series_page2_last.json").read_text(encoding="utf-8"))
        r = client.get("/api/home")
    assert r.status_code == 200, r.text
    home = cast("dict[str, Any]", r.json())
    for row in home["rows"]:
        for item in row["items"]:
            if item["title"] == "Chernobyl":
                return cast(str, item["group_key"])
    raise AssertionError("the English series card never reached the home snapshot")


def _post(client: TestClient, path: str, **params: str) -> dict[str, Any]:
    r = client.post(path, params=params or None, headers={"X-Emby-Token": TOKEN}, json={})
    assert r.status_code == 200, r.text
    return cast("dict[str, Any]", r.json())


def test_series_season_rail_and_episode_wire_ids(client: TestClient, monkeypatch) -> None:
    """Detail → Seasons → Episodes: the rail carries the provider's
    canonical ``yts:<imdb>:sNeM`` ids (the exact ids /api/stream takes)."""
    _seed(monkeypatch)
    gk = _warm_home(client)
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(
            200, text=(FIX / "series_show_tt8740758.json").read_text(encoding="utf-8")
        )
        seasons = client.get(
            f"/Shows/{quote(gk, safe='')}/Seasons?userId=u", headers={"X-Emby-Token": TOKEN}
        )
        assert seasons.status_code == 200
        season_items = cast("list[dict[str, Any]]", seasons.json()["Items"])
        assert len(season_items) == 1
        season_id = cast(str, season_items[0]["Id"])
        episodes = client.get(
            f"/Shows/{quote(gk, safe='')}/Episodes?userId=u&seasonId={quote(season_id, safe='')}",
            headers={"X-Emby-Token": TOKEN},
        )
    assert episodes.status_code == 200
    eps = cast("list[dict[str, Any]]", episodes.json()["Items"])
    assert [e["Id"] for e in eps] == [_S1E1, _S1E2, _S1E3]


def test_episode_playback_info_thin_source(client: TestClient, monkeypatch) -> None:
    """PlaybackInfo on the EPISODE wire id: one thin mp4 MediaSource whose
    Id is the episode id — the same single-source shape a Ukrainian
    episode gets."""
    _seed(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(
            200, text=(FIX / "series_show_tt8740758.json").read_text(encoding="utf-8")
        )
        body = _post(client, f"/Items/{quote(_S1E1, safe='')}/PlaybackInfo", userId="u")
    source = body["MediaSources"][0]
    assert source["Id"] == _S1E1
    assert source["Container"] == "mp4"
    assert source["IsDirectStream"] is True


def test_episode_stream_302s_to_engine_url(client: TestClient, monkeypatch) -> None:
    """The facade keeps its direct-302 posture for the torrent lane's
    empty-header envelope: Switchfin plays straight from the engine URL
    with seeking (research #367: native byte-serving is Range-capable)."""
    engine = _seed(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(
            200, text=(FIX / "series_show_tt8740758.json").read_text(encoding="utf-8")
        )
        r = client.get(
            f"/Videos/{quote(_S1E1, safe='')}/stream", follow_redirects=False
        )
    assert r.status_code == 302
    assert r.headers["location"] == _LAN_URL
    assert engine.ensure_count == 1
    assert engine.last_identifier == _MAGNET_720
    assert engine.last_file_hint == "s01e"


def test_episode_resume_round_trip(client: TestClient, monkeypatch) -> None:
    """Play → report position → resume shelf: the English episode reports
    the same provider-scoped wire id it played, resolved back through the
    group map with PlaybackPositionTicks."""
    _seed(monkeypatch)
    gk = _warm_home(client)
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(
            200, text=(FIX / "series_show_tt8740758.json").read_text(encoding="utf-8")
        )
        r = client.post(
            "/Sessions/Playing/Stopped",
            headers={"X-Emby-Token": TOKEN},
            json={"ItemId": _S1E1, "PositionTicks": 120_000_000, "RunTimeTicks": 3_600_000_000},
        )
        assert r.status_code == 204
        resume = client.get(
            "/Users/x/Items/Resume", headers={"X-Emby-Token": TOKEN}
        )
    assert resume.status_code == 200
    items = cast("list[dict[str, Any]]", resume.json()["Items"])
    episode = next(e for e in items if e["Id"] == _S1E1)
    assert episode["SeriesId"] == gk
    assert episode["PlaybackPositionTicks"] == 120_000_000


def test_resume_positions_survive_listing_churn(client: TestClient, monkeypatch) -> None:
    """IMDb-derived stability (the #379 AC): the recorded wire id only
    depends on provider+imdb+season+episode — after an upstream listing
    refresh (changed titles, re-ranked torrents) the SAME id still
    resolves on the resume shelf, and the stream route still serves it."""
    engine = _seed(monkeypatch)
    gk = _warm_home(client)
    # Record the position under the id the first listing produced…
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(
            200, text=(FIX / "series_show_tt8740758.json").read_text(encoding="utf-8")
        )
        client.post(
            "/Sessions/Playing/Stopped",
            headers={"X-Emby-Token": TOKEN},
            json={"ItemId": _S1E2, "PositionTicks": 60_000_000},
        )
    # …then a "churned" upstream (different titles) still resolves it.
    churned = json.loads((FIX / "series_show_tt8740758.json").read_text(encoding="utf-8"))
    churned["title"] = "Chernobyl (remastered listing)"
    for ep in churned["episodes"]:
        ep["title"] = f"Episode {ep['episode']} (new)"
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(200, text=json.dumps(churned))
        resume = client.get("/Users/x/Items/Resume", headers={"X-Emby-Token": TOKEN})
    assert resume.status_code == 200
    items = cast("list[dict[str, Any]]", resume.json()["Items"])
    episode = next(e for e in items if e["Id"] == _S1E2)
    assert episode["SeriesId"] == gk
    assert episode["PlaybackPositionTicks"] == 60_000_000
    # And the stream route hands the SAME engine session it always did.
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(200, text=json.dumps(churned))
        r = client.get(f"/Videos/{quote(_S1E2, safe='')}/stream", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == _LAN_URL
    assert engine.last_identifier == _MAGNET_720


def test_nextup_includes_english_series(client: TestClient, monkeypatch) -> None:
    """NextUp rides the existing composition rules: an in-progress
    English episode's next sibling is the up-next entry."""
    _seed(monkeypatch)
    _warm_home(client)
    with respx.mock(assert_all_called=False) as router:
        router.get(url=_SHOW_URL).respond(
            200, text=(FIX / "series_show_tt8740758.json").read_text(encoding="utf-8")
        )
        client.post(
            "/Sessions/Playing/Stopped",
            headers={"X-Emby-Token": TOKEN},
            json={"ItemId": _S1E1, "PositionTicks": 1_800_000_000, "RunTimeTicks": 3_600_000_000},
        )
        nextup = client.get("/Shows/NextUp", headers={"X-Emby-Token": TOKEN})
    assert nextup.status_code == 200
    items = cast("list[dict[str, Any]]", nextup.json()["Items"])
    assert any(e["Id"] == _S1E2 for e in items)
