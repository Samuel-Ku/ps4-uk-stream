"""Capture-first contract skeleton (ticket #103).

Replays the frozen real-client request sequence — captured by driving
the official ``@jellyfin/sdk`` (the network layer Jellyfin Web/desktop
and Switchfin use) against the facade — through the TestClient seam.

The fixture at ``fixtures/jellyfin/capture.jsonl`` is the *contract*: it
pins the exact method/path/query surface a real client emits. The 404s
in the fixture are the endpoints the later tickets in the series build;
as each lands, the fixture is regenerated (``npm run capture`` in
``tests/jellyfin_capture/``) and the assertions here tighten.

The skeleton's job today is to prove the facade answers the full real
client surface deterministically — never 5xx, never an unhandled route
crash — and that nothing secret ever lands in the fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cs_uk_api.config import SETTINGS

FIXTURE = Path(__file__).parent / "fixtures" / "jellyfin" / "capture.jsonl"
TOKEN = SETTINGS.jellyfin_token

RECORDS = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
def client() -> TestClient:
    from cs_uk_api import main as main_mod

    return TestClient(main_mod.app)


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: f"{r['method']} {r['path']}")
def test_frozen_client_requests_replay_without_5xx(client: TestClient, record: dict) -> None:
    r = client.request(record["method"], record["path"], params=record["query"])
    assert r.status_code < 500, f"facade crashed answering {record['method']} {record['path']}: {r.status_code}"


def test_capture_records_are_scrubbed_and_complete() -> None:
    for record in RECORDS:
        for header_value in record["headers"].values():
            assert header_value == "<scrubbed>"
        assert set(record) == {"ts", "method", "path", "query", "headers", "status"}
        assert record["method"] and record["path"]


def test_capture_covers_handshake_then_facade_surface() -> None:
    sequence = [(r["method"], r["path"]) for r in RECORDS]
    assert ("GET", "/System/Info/Public") == sequence[0]
    assert ("POST", "/Users/AuthenticateByName") == sequence[1]
    # The five future namespaces the real client touches (Views, Items,
    # PlaybackInfo, stream, sessions, poster) must all appear — a client
    # scenario that vanishes silently would break this list.
    assert any(p.startswith("/UserViews") for _, p in sequence)
    assert any(p == "/Items" for _, p in sequence)
    assert any(p.startswith("/Items/") for _, p in sequence)
    assert any(p.endswith("/PlaybackInfo") for _, p in sequence)
    assert any(p.startswith("/Videos/") for _, p in sequence)
    assert any(p.startswith("/Sessions/") for _, p in sequence)
    assert any(p.endswith("/Images/Primary") for _, p in sequence)


def test_capture_never_contains_live_token() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    assert TOKEN not in raw
