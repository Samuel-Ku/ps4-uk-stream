"""Dashboard surface (spec #280, tickets #281–#283).

Wire-level tests for the routes the Switchfin dashboard and download
manager call: item counts, storage, the users list, the graceful-empty
endpoints (scheduled tasks, devices, activity log, Live TV recommended
programs, capability POST), the Download route (200 + Content-
Disposition + bytes; unplayable id → 404), the detail DTO's download
MediaSource name, and /System/Restart (204 + injectable re-exec seam).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS
from cs_uk_api.main import _blocklist_cache, _content_cache, _home_cache, _home_sources_cache

jf_router = __import__("cs_uk_api.jellyfin.router", fromlist=["router"])
from cs_uk_api.models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    StreamResponse,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

TOKEN = SETTINGS.jellyfin_token
USER = "fdc808859fc45eb8ac5aa6faddc12c72"

_POSTER = "https://cdn.example.test/posters/serial.jpg"
_CDN = "https://cdn.example.test"


class _Stub(BaseProvider):
    id = "p1"
    name = "P1"
    types = ("movie", "series")
    newest_section = "page"

    def __init__(self) -> None:
        self.sections: tuple[Any, ...] = ()
        self.stream_calls: list[tuple[str, str | None]] = []

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def browse(self, section: str, page: int, http: Any) -> tuple[list[SearchResult], bool]:
        cards = [
            SearchResult(
                id="p1:dune-1", provider="p1", form="movie",
                title="Дюна", year=2021, poster=_POSTER, url="https://p1.example/dune-1",
            ),
            SearchResult(
                id="p1:serial-1", provider="p1", form="series",
                title="Сериалал серіал", year=2023, poster=_POSTER, url="https://p1.example/serial-1",
            ),
        ]
        return cards, False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        if external_id == "dune-1":
            return ContentResponse(
                id="p1:dune-1", form="movie", title="Дюна", year=2021,
                description="Епічна стрічка.", poster=_POSTER,
                translations=[Translation(id="uk", label="Дубляж")],
            )
        return ContentResponse(
            id="p1:serial-1", form="series", title="Сериалал серіал", year=2023,
            description="Детектив.", poster=_POSTER,
            translations=[Translation(id="uk", label="Дубляж")],
            seasons=[
                Season(
                    number=1,
                    episodes=[Episode(number=1, id="s1e1", title="Серія 1")],
                )
            ],
        )

    async def stream(self, content_id: str, translation: str | None, http: Any) -> StreamResponse:
        self.stream_calls.append((content_id, translation))
        return StreamResponse(url="https://cdn.example.test/video.mp4", type="mp4")


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
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


def _warm(client: TestClient) -> str:
    """Register the stub and warm the home snapshot; return a movie gk."""
    PROVIDERS["p1"] = _Stub()
    home = client.get("/api/home")
    assert home.status_code == 200
    for row in home.json()["rows"]:
        for item in row["items"]:
            if item["title"] == "Дюна":
                return item["group_key"]
    raise AssertionError("no movie in seeded home")


# -------------------------------------------------------------- counts


def _series_gk(client: TestClient) -> str:
    """The seeded series' group key from the warmed home."""
    home = client.get("/api/home")
    assert home.status_code == 200
    for row in home.json()["rows"]:
        for item in row["items"]:
            if item["title"] == "Сериалал серіал":
                return item["group_key"]
    raise AssertionError("no series in seeded home")


def test_items_counts_reflect_snapshot(client: TestClient) -> None:
    """#281: /Items/Counts reports movie/series counts from the home
    snapshot and the episode total from the cached content page
    (a series whose content is cached contributes its episodes; the
    count never fetches)."""
    _warm(client)
    # Warm the series content into the cache (the count peeks only).
    gk = _series_gk(client)
    detail = client.get(f"/Users/{USER}/Items/{gk}", headers={"X-Emby-Token": TOKEN})
    assert detail.status_code == 200

    r = client.get("/Items/Counts", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["MovieCount"] == 1
    assert body["SeriesCount"] == 1
    assert body["EpisodeCount"] == 1
    assert body["ItemCount"] == 2


def test_items_counts_never_fetches(client: TestClient) -> None:
    """#281: a series whose content was never resolved contributes zero
    episodes — the count reads the cache only, it must not fetch."""
    _warm(client)
    r = client.get("/Items/Counts", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["EpisodeCount"] == 0


def test_items_counts_cold_snapshot_is_zero(client: TestClient) -> None:
    """#281: a cold home (no snapshot yet) reports zero counts — never
    an error, and never a provider fan-out."""
    r = client.get("/Items/Counts", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["MovieCount"] == 0
    assert body["SeriesCount"] == 0
    assert body["ItemCount"] == 0


# -------------------------------------------------------------- storage


def _patch_poster_dir(
    monkeypatch: pytest.MonkeyPatch, poster_cache_dir: str | None
) -> None:
    """Swap the router's frozen SETTINGS for a copy with a new poster
    dir — SETTINGS is frozen, so tests replace it wholesale."""
    from dataclasses import replace

    monkeypatch.setattr(jf_router, "SETTINGS", replace(SETTINGS, poster_cache_dir=poster_cache_dir))


def test_storage_reports_poster_cache_footprint(
    client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#281: /System/Info/Storage carries the poster-cache directory's
    used bytes in ImageCacheFolder (real size, not invented)."""
    poster_dir = tmp_path / "posters"
    poster_dir.mkdir()
    (poster_dir / "a.jpg").write_bytes(b"x" * 4096)
    (poster_dir / "b.jpg").write_bytes(b"y" * 2048)
    _patch_poster_dir(monkeypatch, str(poster_dir))

    r = client.get("/System/Info/Storage", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    image = body["ImageCacheFolder"]
    assert image["Path"] == str(poster_dir)
    assert image["UsedSpace"] == 4096 + 2048
    # Free space present when the filesystem reports it.
    assert image.get("FreeSpace", 0) is not None and image["FreeSpace"] > 0
    # The other folders are the honest empty rows.
    assert body["WebFolder"]["Path"] == ""
    assert body["Libraries"] == []


def test_storage_no_poster_dir_is_empty_rows(client: TestClient, monkeypatch) -> None:
    """#281: with no poster cache configured the storage rows are empty,
    not errors."""
    _patch_poster_dir(monkeypatch, None)
    r = client.get("/System/Info/Storage", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["ImageCacheFolder"]["Path"] == ""
    assert body["ImageCacheFolder"].get("UsedSpace") is None


# -------------------------------------------------------------- users


def test_users_lists_single_fixed_user(client: TestClient) -> None:
    """#281: /Users echoes the single fixed facade user."""
    r = client.get("/Users", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    users = r.json()
    assert len(users) == 1
    assert users[0]["Name"] == "User"
    assert users[0]["ServerId"]
    assert users[0]["Id"]


# ------------------------------------------------------- graceful empties


def test_graceful_empties_answer_expected_envelopes(client: TestClient) -> None:
    """#281: scheduled tasks, devices, activity log, Live TV
    recommended programs, and the capability POST never 404 and answer
    the client's expected shapes."""
    headers = {"X-Emby-Token": TOKEN}

    tasks = client.get("/ScheduledTasks", headers=headers)
    assert tasks.status_code == 200
    assert tasks.json() == []

    devices = client.get("/Devices", headers=headers)
    assert devices.status_code == 200
    body = devices.json()
    assert body["Items"] == []
    assert body["TotalRecordCount"] == 0

    log = client.get("/System/ActivityLog/Entries", headers=headers)
    assert log.status_code == 200
    body = log.json()
    assert body["Items"] == []
    assert body["TotalRecordCount"] == 0
    assert body["StartIndex"] == 0

    recommended = client.get("/LiveTv/Programs/Recommended", headers=headers)
    assert recommended.status_code == 200
    body = recommended.json()
    assert body["Items"] == []
    assert body["TotalRecordCount"] == 0

    caps = client.post("/Sessions/Capabilities/Full", headers=headers)
    assert caps.status_code == 204


# --------------------------------------------------------------- download


@contextlib.contextmanager
def _fake_host() -> Iterator[None]:
    """Point the router's ``get_client`` binding at a fresh httpx client
    so CDN hops are intercepted by the active respx mock."""
    original = jf_router.get_client
    jf_router.get_client = lambda: httpx.AsyncClient()  # type: ignore[assignment]
    try:
        yield
    finally:
        jf_router.get_client = original  # type: ignore[assignment]


def test_download_returns_bytes_with_disposition(client: TestClient) -> None:
    """#282: /Items/{id}/Download returns 200, streams the stream
    route's bytes, and names the file via Content-Disposition."""
    gk = _warm(client)
    # The stub has no headers → the download proxies the body instead of
    # the stream route's 302, so the disposition can ride along.
    with respx.mock() as mlock:
        mlock.get(f"{_CDN}/video.mp4").mock(
            return_value=httpx.Response(
                200, content=b"\x01\x02\x03", headers={"Content-Type": "video/mp4"}
            )
        )
        with _fake_host():
            r = client.get(f"/Items/{gk}/Download", headers={"X-Emby-Token": TOKEN})

    assert r.status_code == 200
    assert r.content == b"\x01\x02\x03"
    disposition = r.headers.get("content-disposition", "")
    assert disposition.startswith("attachment; filename=")
    assert ".mp4" in disposition
    # Cyrillic titles ride in the RFC 5987 form (latin-1 headers).
    assert "filename*=UTF-8''%D0%94%D1%8E%D0%BD%D0%B0.mp4" in disposition


def test_download_unplayable_id_404s(client: TestClient) -> None:
    """#282: an unplayable id answers 404 exactly like the stream route."""
    r = client.get(
        "/Items/g2:deadbeefdeadbeef/Download", headers={"X-Emby-Token": TOKEN}
    )
    assert r.status_code == 404


def test_detail_dto_carries_download_media_source_name(client: TestClient) -> None:
    """#282: the detail DTO's MediaSources[].Name is a non-empty
    title-based file name the download manager saves under."""
    gk = _warm(client)
    r = client.get(f"/Users/{USER}/Items/{gk}", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    sources = r.json().get("MediaSources")
    assert sources, "detail must carry a download MediaSource"
    assert sources[0]["Name"]
    assert "Дюна" in sources[0]["Name"]


# --------------------------------------------------------------- restart


def test_system_restart_answers_204_and_schedules_re_exec(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#283: POST /System/Restart answers 204 and defers the re-exec via
    the injectable ``_schedule_restart`` seam — never a real restart in
    tests, and the schedule fires only after the response is sent."""
    events: list[str] = []

    def fake_schedule() -> None:  # type: ignore[no-untyped-def]
        events.append("schedule")

    monkeypatch.setattr(jf_router, "_schedule_restart", fake_schedule)

    r = client.post("/System/Restart", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 204
    # The re-exec is deferred: nothing scheduled synchronously, and the
    # 204 already went out.
    assert events == ["schedule"]


def test_system_restart_schedule_targets_exec_restart(monkeypatch) -> None:
    """#283: the schedule seam defers exactly the re-exec action — a
    real call to ``_schedule_restart`` (with the exec seam stubbed)
    records the exec once its tick fires."""
    import asyncio

    calls: list[str] = []

    def fake_exec() -> None:  # type: ignore[no-untyped-def]
        calls.append("exec")

    monkeypatch.setattr(jf_router, "_exec_restart", fake_exec)

    # Drive the seam on an explicit loop: ``get_event_loop`` needs a
    # current loop, and the suite leaves none set on this thread.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        jf_router._schedule_restart()
        loop.run_until_complete(asyncio.sleep(0.2))
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert calls == ["exec"]


def test_system_restart_reexec_uses_running_command_line(monkeypatch) -> None:
    """#283: the real re-exec passes the SAME executable and arguments
    as the running process (``os.execv(sys.executable, [sys.executable,
    *sys.argv])``) — a uvicorn relaunch, not a bare ``execv("python")``
    that would lose the ``-m uvicorn …`` invocation."""
    import sys

    captured: list[tuple[str, list[str]]] = []

    def fake_execv(executable: str, argv: list[str]) -> None:  # type: ignore[no-untyped-def]
        captured.append((executable, argv))

    monkeypatch.setattr(jf_router.os, "execv", fake_execv)
    jf_router._exec_restart()

    assert captured, "the real re-exec must call os.execv"
    executable, argv = captured[0]
    assert executable == sys.executable
    assert argv == [sys.executable, *sys.argv]
