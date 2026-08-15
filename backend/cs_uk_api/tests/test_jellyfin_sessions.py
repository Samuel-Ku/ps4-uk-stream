"""Jellyfin sessions no-op endpoints (ticket #108, spec D8).

D8 — verbatim:

> ``POST /Sessions/Playing``, ``/Sessions/Progress``, ``/Sessions/Stopped``
> accept the request body and return **204 No Content**. No state is
> stored; resume/history stay out of scope.

The capture (ticket #103) freezes the real surface: the SDK's
``reportPlaybackStart`` posts ``/Sessions/Playing`` with a full
PlaybackStartInfo body, and the client's session-end reports to
``POST /Sessions/Logout`` — which the SDK treats as the SignedOut
signal and stalls on anything but a 204, so the facade answers it with
the same no-op 204.

All four sit behind ``require_token`` (D4): the session namespace is
private, and a client that never authenticated cannot silence its own
reports — but the routes accept ANY body (the SDK sends full objects;
the response must not depend on their shape).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS
from cs_uk_api.profile_store import Profile, profile_store

TOKEN = SETTINGS.jellyfin_token


@pytest.fixture(autouse=True)
def _isolate_playback() -> None:
    """Keep the session reports out of other test modules' state: the
    resume shelf is global in-memory (single-user facade, #214). Reset
    through the store's seam, never by mutating it."""
    profile_store.install(Profile())
    yield
    profile_store.install(Profile())

#: The full PlaybackStartInfo/ProgressInfo/PlaybackStopInfo shapes the
#: @jellyfin/sdk hands over (capture row 6 + playback-progress).
_SDK_BODIES = {
    "playing": {
        "ItemId": "00000000000000000000000000000000",
        "MediaSourceId": "0",
        "IsPaused": False,
        "CanSeek": False,
        "PositionTicks": 0,
        "PlayMethod": "DirectStream",
    },
    "progress": {
        "ItemId": "00000000000000000000000000000000",
        "PositionTicks": 123456789,
        "IsPaused": True,
        "EventName": "timeupdate",
    },
    "stopped": {
        "ItemId": "00000000000000000000000000000000",
        "PositionTicks": 987654321,
    },
}


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


def test_d8_post_sessions_playback_report_204(client: TestClient) -> None:
    """The three report POSTs accept the SDK bodies and answer 204 with
    an empty body — nothing is stored."""
    for path, body in (
        ("/Sessions/Playing", _SDK_BODIES["playing"]),
        ("/Sessions/Progress", _SDK_BODIES["progress"]),
        ("/Sessions/Stopped", _SDK_BODIES["stopped"]),
    ):
        r = client.post(path, json=body, headers={"X-Emby-Token": TOKEN})
        assert r.status_code == 204, path
        assert r.content == b"", path


def test_d8_sessions_accept_arbitrary_bodies(client: TestClient) -> None:
    """No state is stored, so no body shape can be 'wrong': garbage,
    empty, and query-spelled reports all answer 204."""
    for path in ("/Sessions/Playing", "/Sessions/Progress", "/Sessions/Stopped"):
        assert client.post(path, json={"garbage": True}, headers={"X-Emby-Token": TOKEN}).status_code == 204
        assert client.post(path, headers={"X-Emby-Token": TOKEN}).status_code == 204
        assert client.post(path, params={"ItemId": "x"}, headers={"X-Emby-Token": TOKEN}).status_code == 204


def test_sessions_logout_204(client: TestClient) -> None:
    """Session-end: the SDK's SignedOut signal must see a 204 or it
    stalls; the facade reports the logout as accepted."""
    r = client.post("/Sessions/Logout", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 204
    assert r.content == b""


def test_sessions_require_token(client: TestClient) -> None:
    for path in (
        "/Sessions/Playing",
        "/Sessions/Progress",
        "/Sessions/Stopped",
        "/Sessions/Logout",
    ):
        assert client.post(path, json={}).status_code == 401, path
