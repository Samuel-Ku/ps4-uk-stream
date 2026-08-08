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
from cs_uk_api.models import ContentResponse, SearchResult
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

TOKEN = SETTINGS.jellyfin_token
WRONG = "definitely-not-the-token"


class _AuthStub(BaseProvider):
    """One stub card so the shell tests see a non-empty home.

    The other jellyfin suites (views/detail) seed through their own
    stubs; this file only needs ONE card to exercise Views/Items/Latest
    delegation without touching the network (a real home build would
    fan out to every live provider — and start Chromium for uakino).
    """

    id = "auth-stub"
    name = "AuthStub"
    types = ("movie", "series")
    # The home build's «Новинки» fan-out asks each provider for its
    # newest-section id; answering "page" feeds the one stub card into
    # the first view, keeping these tests airtight without upstream.
    newest_section = "page"

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return []

    async def browse(
        self, section: str, page: int, http: Any
    ) -> tuple[list[SearchResult], bool]:
        if section == "page":
            return [
                SearchResult(
                    id="auth-stub:one",
                    provider=self.id,
                    type=cast(Any, "movie"),
                    title="Дюна",
                    year=2021,
                    poster=None,
                    url="https://auth-stub.example/one",
                )
            ], False
        return [], False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        raise AssertionError("unused")

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> Any:
        raise AssertionError("unused")


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Empty the provider registry + the facade's read caches before/after
    every test, so no real upstream call or stale snapshot leaks in."""
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
def seeded_home(client: TestClient) -> None:
    """Seed the one stub provider and build the home snapshot once.

    Mirrors the views/detail suites' seeding so the cut-down shell tests
    exercise a real non-empty home without any live provider.
    """
    PROVIDERS["auth-stub"] = _AuthStub()
    r = client.get("/api/home")
    assert r.status_code == 200
    assert r.json()["rows"], "stub must contribute a «Новинки» row"


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
    assert body["ServerName"]
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
    # Switchfin parses UserPolicy without defaults (out_of_range.403 on a
    # missing key) — both keys must be present, plus non-null fillers.
    assert body["User"]["Policy"] == {"IsAdministrator": False, "IsDisabled": False}
    assert body["User"]["Configuration"] == {}
    assert body["User"]["PrimaryImageTag"] == ""
    assert body["User"]["HasPassword"] is False


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


# ---------------------------------------------- real-client compatibility


def test_authenticate_lowercase_path_accepted(client: TestClient) -> None:
    """Switchfin sends ``/Users/authenticatebyname`` all-lowercase.

    Real Jellyfin routes case-insensitively; the facade middleware must
    rewrite the scope path to the canonical spelling or this 404s.
    """
    r = client.post("/Users/authenticatebyname", json={"Username": "lower", "Pw": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["AccessToken"] == TOKEN
    assert body["User"]["Name"] == "lower"


def test_quickconnect_enabled_returns_bare_false(client: TestClient) -> None:
    # Switchfin compares the raw body to "true" before offering QuickConnect.
    r = client.get("/QuickConnect/Enabled")
    assert r.status_code == 200
    assert r.text == "false"


def test_branding_configuration_returns_string_disclaimer(client: TestClient) -> None:
    # Switchfin parses LoginDisclaimer into std::string; null would raise
    # nlohmann type_error.302 on the console.
    r = client.get("/Branding/Configuration")
    assert r.status_code == 200
    body = r.json()
    assert body["LoginDisclaimer"] == ""


def test_user_views_result_has_start_index(client: TestClient) -> None:
    # Switchfin's Result<T> wrapper reads StartIndex via NLOHMANN_JSON_FROM
    # (no default) — a missing key raises out_of_range.403 on the console.
    r = client.get("/Users/user1/Views", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["StartIndex"] == 0
    assert "TotalRecordCount" in body
    assert len(body["Items"]) == body["TotalRecordCount"]


def test_user_views_rows_have_non_null_type(client: TestClient, seeded_home: None) -> None:
    # Item/Collection parse via WITH_DEFAULT which tolerates a MISSING key
    # but crashes on an explicit null (type_error.302). The facade must not
    # emit null for Id/Name/Type.
    r = client.get("/Users/user1/Views", headers={"X-Emby-Token": TOKEN})
    body = r.json()
    for item in body["Items"]:
        assert item.get("Type") is not None
        assert item.get("Name") is not None
        assert item.get("Id") is not None


# ---------------------------------------------- home screen shelves (D5)


def test_items_resume_returns_empty_result(client: TestClient) -> None:
    r = client.get("/Users/user1/Items/Resume", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["Items"] == []
    assert body["TotalRecordCount"] == 0
    assert body["StartIndex"] == 0


def test_shows_nextup_returns_empty_result(client: TestClient) -> None:
    r = client.get("/Shows/NextUp", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["Items"] == []
    assert body["TotalRecordCount"] == 0


def test_items_latest_delegates_to_view_row(client: TestClient, seeded_home: None) -> None:
    # The latest-added shelf is the view's home-row cards — same content
    # as /Items?parentId=<view>. Compare against the row listing. The
    # wire shape is a BARE ARRAY: Switchfin parses /Items/Latest as
    # std::vector<Episode> (getJSON<std::vector<...>>), not the
    # Result<T> envelope, and an envelope triggers type_error.302.
    views = client.get("/Users/user1/Views", headers={"X-Emby-Token": TOKEN}).json()
    assert views["Items"], "expected at least one view for the delegation test"
    view_id = views["Items"][0]["Id"]
    listing = client.get(f"/Items?parentId={view_id}", headers={"X-Emby-Token": TOKEN}).json()
    latest = client.get(
        f"/Users/user1/Items/Latest?parentId={view_id}",
        headers={"X-Emby-Token": TOKEN},
    )
    assert latest.status_code == 200
    body = latest.json()
    assert isinstance(body, list), "Latest must be a bare array, not an envelope"
    assert len(body) == listing["TotalRecordCount"]
    assert [i["Id"] for i in body] == [i["Id"] for i in listing["Items"]]


def test_user_prefixed_items_matches_bare_listing(client: TestClient, seeded_home: None) -> None:
    # Switchfin addresses the library under the user. Ensure the prefixed
    # listing returns the same row content as the bare /Items spelling.
    views = client.get("/Users/user1/Views", headers={"X-Emby-Token": TOKEN}).json()
    assert views["Items"]
    view_id = views["Items"][0]["Id"]
    bare = client.get(f"/Items?parentId={view_id}", headers={"X-Emby-Token": TOKEN}).json()
    prefixed = client.get(
        f"/Users/user1/Items?parentId={view_id}",
        headers={"X-Emby-Token": TOKEN},
    ).json()
    assert prefixed["TotalRecordCount"] == bare["TotalRecordCount"]
    assert [i["Id"] for i in prefixed["Items"]] == [i["Id"] for i in bare["Items"]]


def test_user_prefixed_resume_and_latest_win_over_item_detail(client: TestClient) -> None:
    # The parameterized /Users/{user_id}/Items/{item_id} route must not
    # swallow the literal Resume/Latest segments (registration order).
    r = client.get("/Users/user1/Items/Resume", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200 and r.json()["Items"] == []


def test_genres_returns_empty_result(client: TestClient) -> None:
    r = client.get("/Genres", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["Items"] == []
    assert r.json()["TotalRecordCount"] == 0


def test_display_preferences_neutral_defaults(client: TestClient) -> None:
    r = client.get("/DisplayPreferences/usersettings", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["SortBy"] == "SortName"
    assert body["CustomPrefs"] == {}
    # No explicit null anywhere — Switchfin's WITH_DEFAULT parser crashes
    # on null (type_error.302).
    assert not _contains_null(body)


def test_user_info_confirms_remembered_session(client: TestClient) -> None:
    # Switchfin's checkLogin() calls GET /Users/{id} with the stored
    # token on every start; a 200 keeps the app logged in, anything else
    # bounces back to the login form.
    r = client.get("/Users/abc", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["Id"] == "abc"
    assert body["HasPassword"] is False
    assert not _contains_null(body)


def _contains_null(obj: object) -> bool:
    if obj is None:
        return True
    if isinstance(obj, dict):
        return any(_contains_null(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_null(v) for v in obj)
    return False


# ------------------------------------------------------------------- auth


def test_private_route_allows_x_emby_token(client: TestClient) -> None:
    r = client.get("/System/Info", headers={"X-Emby-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["ServerName"]


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