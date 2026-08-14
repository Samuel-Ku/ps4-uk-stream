"""Regression: replay Switchfin's exact request surface (debug run #3).

Replays the endpoints the real Switchfin client calls during startup,
home, detail, hierarchy and playback, and pins the wire requirements
that map 1:1 to the "404"/"403" errors that used to appear on the
Switchfin console:

  - ``Result<T>`` envelopes are parsed by Switchfin with
    ``NLOHMANN_JSON_FROM`` (no default): ``Items``, ``TotalRecordCount``
    AND ``StartIndex`` must all be present, or the console raises
    ``out_of_range.403`` — ``/Items/{id}/Similar`` used to answer
    ``{"Items": [], "TotalRecordCount": 0}`` and fired this on every
    detail page.
  - ``GET /Plugins`` is probed on every app start (``checkDanmuku``);
    before the route existed it answered 404 and logged "http status
    404" on the console.
  - ``GET /Users/{id}/Images/Primary`` is loaded as the user avatar in
    the server list; before the placeholder it answered 404 and logged
    "http status 404" on the console.

Everything else in the happy path must stay 2xx/204/302 — never 404.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS
from cs_uk_api.main import _blocklist_cache, _content_cache, _home_cache, _home_sources_cache

jf_router = importlib.import_module("cs_uk_api.jellyfin.router")
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


def _movie() -> ContentResponse:
    return ContentResponse(
        id="p1:dune-1",
        form="movie",
        title="Дюна",
        year=2021,
        description="Епічна науково-фантастична стрічка.",
        poster=_POSTER,
        translations=[Translation(id="uk", label="Дубляж")],
    )


def _serial() -> ContentResponse:
    return ContentResponse(
        id="p1:serial-1",
        form="series",
        title="Сериалал серіал",
        year=2023,
        description="Детективний серіал.",
        poster=_POSTER,
        translations=[Translation(id="uk", label="Дубляж")],
        seasons=[
            Season(
                number=1,
                episodes=[
                    Episode(number=1, id="s1e1", title="Серія 1"),
                    Episode(number=2, id="s1e2", title="Серія 2"),
                ],
            ),
        ],
    )


class _Stub(BaseProvider):
    id = "p1"
    name = "P1"
    types = ("movie", "series")
    newest_section = "page"

    def __init__(self) -> None:
        self.sections: tuple[Any, ...] = ()

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
            return _movie()
        return _serial()

    async def stream(self, content_id: str, translation: str | None, http: Any) -> StreamResponse:
        return StreamResponse(url="https://cdn.example.test/video.mp4", type="mp4")


@pytest.fixture(autouse=True)
def _stub_poster_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canned poster bytes so no upstream fetch leaves the test process."""

    async def _fake(url: str, client: Any) -> tuple[bytes, str]:
        return b"\xff\xd8\xff\xe0jpegbytes", "image/jpeg"

    monkeypatch.setattr(jf_router, "fetch_poster_bytes", _fake)


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


def _replay(client: TestClient) -> list[tuple[str, int, Any]]:
    """Run the Switchfin flow; return (label, status, json-body)."""
    PROVIDERS["p1"] = _Stub()
    results: list[tuple[str, int, Any]] = []

    def hit(label: str, method: str, path: str, **kwargs: Any) -> Any:
        # Do not chase the intentional 302 stream redirect (Switchfin's
        # libcurl follows it; the TestClient would chase it to the fake
        # CDN host and surface a harness 404).
        kwargs.setdefault("follow_redirects", False)
        r = client.request(method, path, **kwargs)
        body: Any = None
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = r.content
        results.append((label, r.status_code, body))
        return r

    # --- startup ---
    hit("public-info", "GET", "/System/Info/Public")
    hit(
        "auth-lowercase",
        "POST",
        "/Users/authenticatebyname",
        json={"Username": "user", "Pw": "pw"},
        headers={"Authorization": f"MediaBrowser Client=\"x\", Token=\"{TOKEN}\""},
    )
    hit("user-info", "GET", f"/Users/{USER}", headers={"X-Emby-Token": TOKEN})
    hit("plugins", "GET", "/Plugins", headers={"X-Emby-Token": TOKEN})
    hit("branding", "GET", "/Branding/Configuration")
    hit("quickconnect", "GET", "/QuickConnect/Enabled")

    # --- home ---
    r = hit("views", "GET", f"/Users/{USER}/Views", headers={"X-Emby-Token": TOKEN})
    view_id: str = r.json()["Items"][0]["Id"]
    hit(
        "latest",
        "GET",
        f"/Users/{USER}/Items/Latest",
        params={"parentId": view_id},
        headers={"X-Emby-Token": TOKEN},
    )
    hit("resume", "GET", f"/Users/{USER}/Items/Resume", headers={"X-Emby-Token": TOKEN})
    hit("nextup", "GET", "/Shows/NextUp", headers={"X-Emby-Token": TOKEN})

    # --- listing / detail ---
    r = hit(
        "library",
        "GET",
        f"/Users/{USER}/Items",
        params={"parentId": view_id},
        headers={"X-Emby-Token": TOKEN},
    )
    item = r.json()["Items"][0]
    gk: str = item["Id"]
    hit("detail", "GET", f"/Users/{USER}/Items/{gk}", headers={"X-Emby-Token": TOKEN})
    hit("similar", "GET", f"/Items/{gk}/Similar", headers={"X-Emby-Token": TOKEN})
    hit(
        "special",
        "GET",
        f"/Users/{USER}/Items/{gk}/SpecialFeatures",
        headers={"X-Emby-Token": TOKEN},
    )
    hit("seasons", "GET", f"/Shows/{gk}/Seasons", headers={"X-Emby-Token": TOKEN})
    hit("genres", "GET", "/Genres", headers={"X-Emby-Token": TOKEN})

    # --- poster / avatar ---
    hit("poster", "GET", f"/Items/{gk}/Images/Primary", params={"format": "Webp"})
    hit("avatar", "GET", f"/Users/{USER}/Images/Primary", params={"format": "Webp"})

    # --- playback ---
    hit(
        "playbackinfo",
        "POST",
        f"/Items/{gk}/PlaybackInfo",
        json={"DeviceProfile": {}},
        headers={"X-Emby-Token": TOKEN},
    )
    hit("stream", "GET", f"/Videos/{gk}/stream", headers={"X-Emby-Token": TOKEN})
    hit("sessions-playing", "POST", "/Sessions/Playing", headers={"X-Emby-Token": TOKEN})
    hit("sessions-progress", "POST", "/Sessions/Playing/Progress", headers={"X-Emby-Token": TOKEN})
    hit("sessions-stopped", "POST", "/Sessions/Playing/Stopped", headers={"X-Emby-Token": TOKEN})
    return results


def test_switchfin_flow_never_4xx(client: TestClient) -> None:
    """The Switchfin happy path must answer 2xx/204/302 — never 404/403."""
    for label, status, _body in _replay(client):
        assert status < 400, (
            f"{label} answered {status} — Switchfin's HTTP layer throws on >= 400 "
            "and logs it on the console"
        )


def test_switchfin_result_envelopes_carry_start_index(client: TestClient) -> None:
    """Every endpoint Switchfin parses as ``Result<T>`` (no defaults)
    must emit Items + TotalRecordCount + StartIndex or the console shows
    ``out_of_range.403``."""
    RESULT_ENVELOPE_ENDPOINTS = ("views", "library", "resume", "nextup", "similar", "seasons", "genres")
    for label, status, body in _replay(client):
        if label not in RESULT_ENVELOPE_ENDPOINTS:
            continue
        assert status == 200, f"{label} status {status}"
        assert isinstance(body, dict), f"{label} not an object: {body!r}"
        for key in ("Items", "TotalRecordCount", "StartIndex"):
            assert key in body, f"{label} missing {key!r} -> out_of_range.403 on Switchfin console"


def test_plugins_listing_is_200(client: TestClient) -> None:
    """Switchfin probes /Plugins on every start (checkDanmuku); a 404 is
    logged on the console as 'http status 404'."""
    for label, status, _body in _replay(client):
        if label == "plugins":
            assert status == 200


def test_user_avatar_is_not_404(client: TestClient) -> None:
    """The server list loads /Users/{id}/Images/Primary as the avatar; a
    404 is logged on the console as 'http status 404'."""
    for label, status, _body in _replay(client):
        if label == "avatar":
            assert status == 200
