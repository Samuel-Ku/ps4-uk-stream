"""Contract tests for the Jellyfin facade handshake (ticket #102).

The seam is the HTTP surface — a Jellyfin client (Switchfin/web/desktop)
is pointed at ``host:port`` and drives its own handshake against these
routes. So the tests assert the exact Jellyfin shapes a real client
consumes:

  - ``GET /System/Info/Public`` is unauthenticated server discovery.
  - ``POST /Users/AuthenticateByName`` accepts any creds and returns the
    fixed opaque token + a user object.
  - ``GET /System/Info`` (the first private route) rejects missing/wrong
    credentials with 401 and accepts both ``X-Emby-Token`` and
    ``Authorization: MediaBrowser Token="…"`` forms.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS

TOKEN = SETTINGS.jellyfin_token
WRONG = "definitely-not-the-token"


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


# ---------------------------------------------------------------- discovery


def test_system_info_public_is_unauthenticated_and_wellformed(client: TestClient) -> None:
    r = client.get("/System/Info/Public")
    assert r.status_code == 200
    body = r.json()
    # Fields a real client reads when rendering the login screen.
    assert body["ProductName"]
    assert body["SystemName"]
    assert body["Version"]
    assert body["Id"]
    assert body["StartupWizardCompleted"] is True


def test_system_info_public_does_not_leak_token(client: TestClient) -> None:
    body = client.get("/System/Info/Public").json()
    assert TOKEN not in str(body)


# --------------------------------------------------------------------- login


def test_authenticate_any_credentials_returns_fixed_token(client: TestClient) -> None:
    r = client.post("/Users/AuthenticateByName", json={"Username": "view", "Pw": "anything"})
    assert r.status_code == 200
    body = r.json()
    assert body["AccessToken"] == TOKEN
    assert body["ServerId"]
    assert body["User"]["Name"] == "view"
    assert body["User"]["Id"]
    assert body["User"]["ServerId"] == body["ServerId"]


def test_authenticate_echoes_request_username(client: TestClient) -> None:
    r = client.post("/Users/AuthenticateByName", json={"Username": "консоль", "Pw": "x"})
    assert r.status_code == 200
    assert r.json()["User"]["Name"] == "консоль"


def test_authenticate_any_password_accepted(client: TestClient) -> None:
    for pw in ("", "password", "x" * 100):
        r = client.post("/Users/AuthenticateByName", json={"Username": "u", "Pw": pw})
        assert r.status_code == 200
        assert r.json()["AccessToken"] == TOKEN


def test_authenticate_missing_body_accepted(client: TestClient) -> None:
    r = client.post("/Users/AuthenticateByName", json={})
    assert r.status_code == 200


# ------------------------------------------------------------------- auth


def test_private_route_allows_x_emby_token(client: TestClient) -> None:
    r = client.get("/System/Info", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["SystemName"]


def test_private_route_missing_token_is_401(client: TestClient) -> None:
    r = client.get("/System/Info")
    assert r.status_code == 401


def test_private_route_wrong_token_is_401(client: TestClient) -> None:
    r = client.get("/System/Info", headers={"X-Emby-Token": WRONG})
    assert r.status_code == 401


def test_media_browser_token_header_form_accepted(client: TestClient) -> None:
    for header in (f'MediaBrowser Token="{TOKEN}"', f"MediaBrowser Token={TOKEN}"):
        r = client.get("/System/Info", headers={"Authorization": header})
        assert r.status_code == 200


def test_media_browser_client_info_header_accepted(client: TestClient) -> None:
    # The real @jellyfin/sdk (web/desktop/Switchfin) sends the token as
    # the trailing field of a client/device identity header, not as the
    # bare form above. The regex must find Token= anywhere after the
    # MediaBrowser scheme.
    header = (
        f'MediaBrowser Client="SwitchfinLike", Device="Capture PS4", '
        f'DeviceId="capture-ps4-dev", Version="1.0.0", Token="{TOKEN}"'
    )
    r = client.get("/System/Info", headers={"Authorization": header})
    assert r.status_code == 200


def test_media_browser_client_info_header_wrong_token_is_401(client: TestClient) -> None:
    header = f'MediaBrowser Client="x", Device="y", DeviceId="z", Version="1.0.0", Token="{WRONG}"'
    r = client.get("/System/Info", headers={"Authorization": header})
    assert r.status_code == 401


def test_media_browser_token_missing_in_authorization_is_401(client: TestClient) -> None:
    r = client.get("/System/Info", headers={"Authorization": 'MediaBrowser Token=""'})
    assert r.status_code == 401


def test_bearer_token_not_accepted(client: TestClient) -> None:
    r = client.get("/System/Info", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401


def test_wrong_media_browser_token_is_401(client: TestClient) -> None:
    r = client.get("/System/Info", headers={"Authorization": f'MediaBrowser Token="{WRONG}"'})
    assert r.status_code == 401