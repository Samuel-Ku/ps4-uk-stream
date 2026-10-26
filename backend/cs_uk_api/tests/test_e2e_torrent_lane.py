"""Live e2e: the torrent lane must hand the client a reachable URL and
serve real Range bytes through it.

The facade does not proxy torrent media: it redirects the player to the
BitPlay engine. That makes the redirect target part of the client
contract. A server configured with
``CS_UK_TORRENT_ENGINE_URL=http://127.0.0.1:3347`` still works for curl
on the server, but every PS4/phone gets an address that only resolves on
the server itself. ``deploy/smoke.sh`` cannot catch that on a default
run: it compares the redirect with ``CS_UK_ENGINE_URL``, which is also
loopback by default.

Run against a warmed, deployed stack from a machine that is not the
server (or at least with the host configured for LAN clients):

    CS_UK_E2E_API_URL=http://192.168.2.223:8003 \
        .venv/bin/pytest -m e2e cs_uk_api/tests/test_e2e_torrent_lane.py -q

Env:
    CS_UK_E2E_API_URL  live facade origin (required; without it the test skips)
    CS_UK_JF_TOKEN     facade token (default: the accept-any handshake's)
    CS_UK_E2E_GROUP    pin a torrent-lane group (default: search a title)
    CS_UK_E2E_TITLE    search title when no group is pinned (default "Dune")
    CS_UK_E2E_ATTEMPTS candidates tried (default 5)
    CS_UK_E2E_TIMEOUT  per-request seconds (default 120)

The test mutates nothing shared: PlaybackInfo may add one torrent to the
engine, exactly like ``deploy/smoke.sh`` (idempotent by infohash).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx
import pytest


def _is_unreachable_engine_host(url: str) -> bool:
    """True when a player on another device cannot use this URL's host.

    Covers the literal loopback forms (``127.0.0.0/8``, ``::1``,
    ``localhost``) and the unspecified address (``0.0.0.0``/``::``).
    A hostname that merely *resolves* to loopback is not detected; the
    failure mode here is the configured literal address.
    """
    host = urlsplit(url).hostname
    if not host:
        return False
    host = host.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "unreachable"),
    [
        ("http://127.0.0.1:3347/api/v1/torrent/x/stream/0", True),
        ("http://127.1.2.3:3347/api/v1/torrent/x/stream/0", True),
        ("http://localhost:3347/api/v1/torrent/x/stream/0", True),
        ("http://LOCALHOST.:3347/api/v1/torrent/x/stream/0", True),
        ("http://[::1]:3347/api/v1/torrent/x/stream/0", True),
        ("http://[::ffff:127.0.0.1]:3347/api/v1/torrent/x/stream/0", True),
        ("http://0.0.0.0:3347/api/v1/torrent/x/stream/0", True),
        ("http://[::]:3347/api/v1/torrent/x/stream/0", True),
        ("http://192.168.2.223:3347/api/v1/torrent/x/stream/0", False),
        ("http://10.0.0.5:3347/api/v1/torrent/x/stream/0", False),
        ("http://engine.example:3347/api/v1/torrent/x/stream/0", False),
        ("http://[2001:db8::1]:3347/api/v1/torrent/x/stream/0", False),
    ],
)
def test_unreachable_engine_host_detection(url: str, unreachable: bool) -> None:
    assert _is_unreachable_engine_host(url) is unreachable


@pytest.fixture(scope="module")
def live_client() -> Iterator[tuple[httpx.Client, dict[str, str]]]:
    base = os.environ.get("CS_UK_E2E_API_URL", "").rstrip("/")
    if not base:
        pytest.skip("set CS_UK_E2E_API_URL to a live, warmed facade")

    timeout = float(os.environ.get("CS_UK_E2E_TIMEOUT", "120"))
    token = os.environ.get("CS_UK_JF_TOKEN", "")
    with httpx.Client(
        base_url=base,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        if not token:
            response = client.post(
                "/Users/AuthenticateByName",
                json={"Username": "e2e", "Pw": "e2e"},
            )
            assert response.status_code == 200, (
                f"handshake -> {response.status_code}: {response.text[:300]}"
            )
            token = response.json().get("AccessToken", "")
            assert token, "handshake returned no AccessToken"
        yield client, {"X-Emby-Token": token}


def _candidate_groups(client: httpx.Client, headers: dict[str, str]) -> list[str]:
    pinned = os.environ.get("CS_UK_E2E_GROUP", "").strip()
    if pinned:
        return [pinned]

    title = os.environ.get("CS_UK_E2E_TITLE", "Dune")
    response = client.get(
        "/api/search",
        params={"q": title, "provider": "yts"},
        headers=headers,
    )
    assert response.status_code == 200, (
        f"search {title!r} -> {response.status_code}: {response.text[:300]}"
    )
    groups = [
        group["group_key"]
        for group in response.json().get("groups", [])
        if isinstance(group.get("group_key"), str)
        and group["group_key"].startswith("g3:")
    ]
    assert groups, f"search {title!r} returned no torrent-lane groups"
    return groups[: int(os.environ.get("CS_UK_E2E_ATTEMPTS", "5"))]


def _assert_range_bytes(
    client: httpx.Client,
    url: str,
    range_header: str,
    expected_bytes: int,
    expected_content_range: str,
) -> None:
    """Read one bounded range without ever downloading a whole file.

    The status is asserted before reading, so an engine that ignores
    ``Range`` and answers 200 cannot make the test pull a multi-GB body
    into memory.
    """
    with client.stream("GET", url, headers={"Range": range_header}) as response:
        assert response.status_code == 206, (
            f"Range {range_header} -> {response.status_code}, want 206; "
            "an engine that ignores Range would stream the whole file"
        )
        content_range = response.headers.get("content-range", "")
        assert content_range.startswith(f"bytes {expected_content_range}/"), (
            f"Range {range_header}: Content-Range {content_range!r}"
        )
        body = b"".join(response.iter_bytes())
    assert len(body) == expected_bytes, (
        f"Range {range_header}: got {len(body)} bytes, want {expected_bytes}"
    )


@pytest.mark.e2e
def test_live_torrent_lane_serves_range_bytes_from_a_reachable_url(
    live_client: tuple[httpx.Client, dict[str, str]],
) -> None:
    client, headers = live_client
    failures: list[str] = []

    for group_key in _candidate_groups(client, headers):
        detail = client.get(f"/Items/{group_key}", headers=headers)
        if detail.status_code != 200:
            failures.append(f"{group_key}: detail {detail.status_code}")
            continue

        info = client.post(
            f"/Items/{group_key}/PlaybackInfo",
            headers=headers,
            json={},
        )
        if info.status_code != 200:
            failures.append(f"{group_key}: PlaybackInfo {info.status_code}")
            continue

        sources = info.json().get("MediaSources") or []
        path = sources[0].get("Path") if sources else None
        if not path:
            failures.append(
                f"{group_key}: PlaybackInfo 200 without MediaSources[0].Path"
            )
            continue

        stream = client.get(path, headers=headers)
        if stream.status_code != 302:
            failures.append(
                f"{group_key}: stream {stream.status_code} (want 302)"
            )
            continue

        location = stream.headers.get("location", "")
        assert location, f"{group_key}: 302 without a Location header"
        parsed = urlsplit(location)
        assert parsed.scheme in {"http", "https"} and parsed.hostname, (
            f"{group_key}: Location is not an absolute http(s) URL: {location!r}"
        )
        assert not _is_unreachable_engine_host(location), (
            f"facade handed the client a loopback/unspecified engine URL: "
            f"{location!r}. A PS4/phone on the LAN cannot reach it; set "
            "CS_UK_TORRENT_ENGINE_URL to an address the player can reach "
            "(for example the host's LAN IP), not 127.0.0.1/localhost."
        )

        _assert_range_bytes(client, location, "bytes=0-65535", 65536, "0-65535")
        _assert_range_bytes(
            client,
            location,
            "bytes=1048576-2097151",
            1048576,
            "1048576-2097151",
        )
        return

    pytest.fail(
        "no torrent-lane candidate produced a playable MediaSource: "
        + "; ".join(failures)
    )
