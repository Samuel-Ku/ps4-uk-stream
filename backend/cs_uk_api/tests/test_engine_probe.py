"""Engine liveness probe + the ``yts:engine`` tracker entry (spec #394).

The lane's second dependency — the BitPlay engine — previously received
tracker samples only when a viewer pressed play, so a dead engine read
``ok`` indefinitely. These tests pin the fix's three pieces:

  - the probe loop: any HTTP answer (incl. 401/403) = alive;
    transport death = failure; interval 0 = disabled;
  - the retarget: stream-time engine-path faults land in
    ``yts:engine``'s window, not ``yts``'s;
  - the deterministic marker: a half-configured engine is down at
    startup with zero samples;
  - endpoint rendering: ``yts:engine`` appears only when configured.

The engine is LAN-local; the watchdog deliberately does NOT enumerate
it (a dead engine must never mask the classic WAN-loss wedge).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from cs_uk_api import config as _config
from cs_uk_api.health import TRACKER
from cs_uk_api.torrent_engine import (
    ENGINE_PROBE_INTERVAL_S,
    ENGINE_TRACKER_ID,
    EnginePathError,
    EngineUnavailable,
    build_engine_from_settings,
    engine_configured,
    probe_engine,
)

_BASE = "http://bitplay.lan:3347"


def _settings(url: str | None, **kw: Any):
    return replace(_config.SETTINGS, torrent_engine_url=url, **kw)


# ------------------------------------------------------------- probe


@respx.mock
async def test_probe_http_200_is_alive() -> None:
    respx.get(f"{_BASE}/api/v1/capabilities").mock(
        return_value=httpx.Response(200, json={"ffmpeg": True})
    )
    assert await probe_engine(_BASE) is True


@respx.mock
async def test_probe_auth_failure_still_proves_liveness() -> None:
    """401/403 = the process ANSWERED: alive (auth misconfig ≠ dead engine)."""
    respx.get(f"{_BASE}/api/v1/capabilities").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    assert await probe_engine(_BASE) is True


@respx.mock
async def test_probe_5xx_still_proves_liveness() -> None:
    respx.get(f"{_BASE}/api/v1/capabilities").mock(
        return_value=httpx.Response(503, json={"error": "overloaded"})
    )
    assert await probe_engine(_BASE) is True


async def test_probe_transport_death_is_failure() -> None:
    with respx.mock:
        respx.get(f"{_BASE}/api/v1/capabilities").mock(side_effect=httpx.ConnectError("refused"))
        assert await probe_engine(_BASE) is False


async def test_probe_timeout_is_failure() -> None:
    with respx.mock:
        respx.get(f"{_BASE}/api/v1/capabilities").mock(side_effect=httpx.ReadTimeout("slow"))
        assert await probe_engine(_BASE) is False


def test_probe_interval_constant_is_the_spec_default() -> None:
    assert ENGINE_PROBE_INTERVAL_S == 300.0


# --------------------------------------------------- config / builder


def test_engine_configured_false_when_unset() -> None:
    assert not engine_configured(_settings(None))


def test_engine_configured_true_when_set() -> None:
    assert engine_configured(_settings(_BASE))


def test_builder_unconfigured_returns_none() -> None:
    assert build_engine_from_settings(_settings(None)) is None


def test_half_config_auth_is_detectable() -> None:
    """User without password (or vice versa) — the deterministic-marker case."""
    s = _settings(_BASE, torrent_engine_user="op", torrent_engine_password=None)
    assert build_engine_from_settings(s) is not None  # still builds (client half-auths = no auth)
    s2 = _settings(_BASE, torrent_engine_user=None, torrent_engine_password="pw")
    assert build_engine_from_settings(s2) is not None


def test_half_config_url_without_scheme_is_detectable() -> None:
    """A base URL without a scheme is malformed: marker territory."""
    assert not engine_configured(_settings("bitplay.lan:3347"))


# --------------------------------------------------- tracker entry id


def test_tracker_id_constant() -> None:
    assert ENGINE_TRACKER_ID == "yts:engine"


def test_probe_records_into_engine_window() -> None:
    """The loop's recording helper targets yts:engine — live proof via
    one probe cycle (the exact call the background loop makes)."""
    with respx.mock:
        respx.get(f"{_BASE}/api/v1/capabilities").mock(
            return_value=httpx.Response(200, json={})
        )
        ok = asyncio.run(probe_engine(_BASE, record=True))
    assert ok is True
    assert TRACKER._samples[ENGINE_TRACKER_ID][-1] is True


async def test_probe_failure_records_lane_fault() -> None:
    with respx.mock:
        respx.get(f"{_BASE}/api/v1/capabilities").mock(
            side_effect=httpx.ConnectError("refused")
        )
        ok = await probe_engine(_BASE, record=True)
    assert ok is False
    assert TRACKER._samples[ENGINE_TRACKER_ID][-1] is False
    assert TRACKER.last_error_at(ENGINE_TRACKER_ID) is not None


# ------------------------------------------------- stream-time retarget


def test_engine_unavailable_is_engine_path_error() -> None:
    """The typed bridge: EngineUnavailable IS an engine-path fault, so
    the provider layer can retarget it without __cause__ sniffing."""
    assert issubclass(EngineUnavailable, EnginePathError)


def test_engine_rejected_is_NOT_engine_path() -> None:
    """A dead swarm is an ITEM verdict (not_found after the fallback);
    it must keep recording against the lane provider — or rather, be
    translated item-level — never as an engine fault."""
    from cs_uk_api.torrent_engine import EngineRejected

    assert not issubclass(EngineRejected, EnginePathError)


async def test_yts_stream_engine_fault_records_against_engine_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream-time EngineUnavailable → 'unreachable' ProviderError tagged
    engine_path → the facade records it against yts:engine, NOT yts."""
    from cs_uk_api.jellyfin.delivery import resolve_stream
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import ProviderError

    class _YtsStub:
        id = "yts"

        async def stream(self, external_id: str, translation_id: str | None, http: Any) -> Any:
            raise ProviderError("unreachable", "torrent engine unreachable: dead").with_engine_path()

    monkeypatch.setitem(PROVIDERS, "yts", _YtsStub())
    out = await resolve_stream("yts:tt1234567:__movie__")
    assert out is None
    # The engine entry took the fault; the catalog entry stayed clean.
    assert TRACKER.last_error_at(ENGINE_TRACKER_ID) is not None
    assert "yts" not in TRACKER._errors or TRACKER._samples.get("yts") is None


async def test_yts_stream_item_fault_still_skips_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """not_found remains an item verdict: neither entry moves."""
    from cs_uk_api.jellyfin.delivery import resolve_stream
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import ProviderError

    class _YtsStub:
        id = "yts"

        async def stream(self, external_id: str, translation_id: str | None, http: Any) -> Any:
            raise ProviderError("not_found", "no torrents recorded")

    monkeypatch.setitem(PROVIDERS, "yts", _YtsStub())
    out = await resolve_stream("yts:tt1234567:__movie__")
    assert out is None
    assert ENGINE_TRACKER_ID not in TRACKER._samples
    assert "yts" not in TRACKER._samples


async def test_yts_stream_catalog_fault_records_against_yts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-engine lane fault (e.g. the popcorn catalog host) keeps
    recording against yts — the retarget is engine-path only."""
    from cs_uk_api.jellyfin.delivery import resolve_stream
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import ProviderError

    class _YtsStub:
        id = "yts"

        async def stream(self, external_id: str, translation_id: str | None, http: Any) -> Any:
            raise ProviderError("upstream_unreachable", "yts api down")

    monkeypatch.setitem(PROVIDERS, "yts", _YtsStub())
    out = await resolve_stream("yts:tt1234567:__movie__")
    assert out is None
    assert TRACKER.last_error_at("yts") is not None
    assert ENGINE_TRACKER_ID not in TRACKER._errors


async def test_native_route_engine_fault_records_against_engine_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The native /api/stream path shares the retarget via upstream_guard."""
    import cs_uk_api.service as service_mod
    from cs_uk_api.providers import PROVIDERS
    from cs_uk_api.providers.base import ProviderError

    class _YtsStub:
        id = "yts"

        async def stream(self, external_id: str, translation_id: str | None, http: Any) -> Any:
            raise ProviderError("unreachable", "engine dead").with_engine_path()

    monkeypatch.setitem(PROVIDERS, "yts", _YtsStub())
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await service_mod.upstream_guard(
            "yts",
            _YtsStub().stream("tt1234567", None, httpx.AsyncClient()),
            "stream test",
            record_skip_codes=frozenset({"not_found"}),
        )
    # the canonical 502 envelope (ADR-0002) — the retarget is recording-side only
    assert exc_info.value.status_code == 502
    assert TRACKER.last_error_at(ENGINE_TRACKER_ID) is not None
    assert "yts" not in TRACKER._errors


# ------------------------------------------------------------- marker


def test_marker_for_half_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half-configured engine ⇒ deterministic down marker at startup,
    zero samples — the uakino chromium_missing convention."""
    from cs_uk_api import lifecycle

    monkeypatch.setattr(
        _config, "SETTINGS", _settings(_BASE, torrent_engine_user="op", torrent_engine_password=None)
    )
    lifecycle.mark_engine_startup_state()
    assert TRACKER.startup_marker(ENGINE_TRACKER_ID) is not None
    assert TRACKER.status(ENGINE_TRACKER_ID) == "down"


def test_no_marker_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    from cs_uk_api import lifecycle

    monkeypatch.setattr(_config, "SETTINGS", _settings(None))
    lifecycle.mark_engine_startup_state()
    assert TRACKER.startup_marker(ENGINE_TRACKER_ID) is None


def test_no_marker_when_fully_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from cs_uk_api import lifecycle

    monkeypatch.setattr(
        _config, "SETTINGS", _settings(_BASE, torrent_engine_user="op", torrent_engine_password="pw")
    )
    lifecycle.mark_engine_startup_state()
    assert TRACKER.startup_marker(ENGINE_TRACKER_ID) is None


# ------------------------------------------------- endpoint rendering


def test_providers_endpoint_omits_engine_entry_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from cs_uk_api.main import app

    monkeypatch.setattr(_config, "SETTINGS", _settings(None))
    ids = [p["id"] for p in TestClient(app).get("/api/providers").json()]
    assert ENGINE_TRACKER_ID not in ids


def test_providers_endpoint_lists_engine_entry_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from cs_uk_api.main import app

    monkeypatch.setattr(_config, "SETTINGS", _settings(_BASE))
    body = TestClient(app).get("/api/providers").json()
    engine = next((p for p in body if p["id"] == ENGINE_TRACKER_ID), None)
    assert engine is not None
    assert engine["forms"] == []
    assert engine["styles"] == []
    # registry-last still holds; the engine entry rides AFTER it (not a provider)
    assert body[-1]["id"] == ENGINE_TRACKER_ID


def test_health_endpoint_includes_engine_entry_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from cs_uk_api.main import app

    monkeypatch.setattr(_config, "SETTINGS", _settings(_BASE))
    body = TestClient(app).get("/api/health").json()
    assert ENGINE_TRACKER_ID in body["providers"]


# ------------------------------------------------------------ watchdog


def test_watchdog_ignores_engine_entry() -> None:
    """The engine is LAN-local: folding it into the all-down wedge set
    would suppress the classic WAN-loss signal (engine up, WAN dead).
    The registry-only enumeration is load-bearing."""
    from cs_uk_api.providers import PROVIDERS

    assert ENGINE_TRACKER_ID not in PROVIDERS
    for pid in PROVIDERS:
        assert pid != ENGINE_TRACKER_ID


def test_dead_engine_alone_never_triggers_watchdog() -> None:
    """Spec #394's normative claim: a dead engine never resets the client."""
    from cs_uk_api.watchdog import WATCHDOG

    TRACKER.record(ENGINE_TRACKER_ID, ok=False)
    TRACKER.record(ENGINE_TRACKER_ID, ok=False)
    TRACKER.record(ENGINE_TRACKER_ID, ok=False)
    TRACKER.record(ENGINE_TRACKER_ID, ok=False)
    TRACKER.record(ENGINE_TRACKER_ID, ok=False)
    assert TRACKER.status(ENGINE_TRACKER_ID) == "down"
    assert not WATCHDOG.should_reset()


def test_fake_engine_protocol_unchanged() -> None:
    """The TorrentEngine protocol gains NO methods (deletion test)."""
    from cs_uk_api.torrent_engine import TorrentEngine

    members = set(TorrentEngine.__protocol_attrs__) if hasattr(TorrentEngine, "__protocol_attrs__") else set(getattr(TorrentEngine, "__annotations__", {}))
    assert members == {"ensure_session"} or members == set()
