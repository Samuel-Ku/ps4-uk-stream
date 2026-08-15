"""Tests proving the existing Russian-content blocking behaviour (issue #79).

The behaviour itself is committed in ``cs_uk_api/main.py`` (read-only for
this ticket): when ``config.SETTINGS.block_russian`` is on and a provider
returns a content whose ``country`` is blocked (see ``cs_uk_api.country``
READ-ONLY), ``/api/content`` answers 404 and records the content id in the
30-min ``_blocklist_cache`` so a repeat request short-circuits without
hitting the provider again.

These tests only *prove* that behaviour with a stubbed provider — no network.
"""
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

import cs_uk_api.config as config_mod
import cs_uk_api.main as main_mod
from cs_uk_api.main import app
from cs_uk_api.models import ContentResponse, Translation
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

client = TestClient(app)


class _BlockedStub(BaseProvider):
    """Provider whose content() always returns Russian-country content."""

    id = "blocked-stub"
    name = "BlockedStub"
    types = ("movie",)
    calls = 0

    async def search(self, query, http):
        return []

    async def content(self, external_id, http):
        self.calls += 1
        return ContentResponse(
            id=f"blocked-stub:{external_id}",
            form="movie",
            title="Блокований контент",
            translations=[Translation(id="uk", label="UK")],
            country="росія",
        )

    async def stream(self, content_id, translation, http):
        raise AssertionError("unused")


def _register_stub(pid: str) -> _BlockedStub:
    stub = _BlockedStub()
    stub.id = pid
    stub.calls = 0  # instance counter, reset per test
    PROVIDERS[pid] = stub  # type: ignore[assignment]
    return stub


def _unregister(pid: str) -> None:
    PROVIDERS.pop(pid, None)
    main_mod._blocklist_cache.clear()
    main_mod._content_cache.clear()


def test_blocked_content_returns_404_and_provider_was_called() -> None:
    stub = _register_stub("blocked-a")
    try:
        r = client.get("/api/content/blocked-a:12345")
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "not_found"
        assert stub.calls == 1
    finally:
        _unregister("blocked-a")


def test_blocklist_cached_30min_short_circuits_second_request() -> None:
    # Cache TTL: the blocklist store is built from config.SETTINGS.cache_content_s
    # (Arch T12: the ONE settings binding), whose default is 1800 s = 30 min.
    # Assert the structural invariant (cache built from the setting) plus the
    # documented default.
    assert main_mod._blocklist_cache._default_ttl_s == config_mod.SETTINGS.cache_content_s
    assert config_mod.SETTINGS.cache_content_s == 1800

    stub = _register_stub("blocked-b")
    try:
        r1 = client.get("/api/content/blocked-b:12345")
        assert r1.status_code == 404
        assert stub.calls == 1
        # Second request must NOT reach the provider: the blocklist cache
        # short-circuits it before the provider lookup.
        r2 = client.get("/api/content/blocked-b:12345")
        assert r2.status_code == 404
        assert stub.calls == 1
    finally:
        _unregister("blocked-b")


def test_block_russian_disabled_returns_200() -> None:
    stub = _register_stub("blocked-c")
    original = config_mod.SETTINGS
    # Arch T12: patch the ONE binding (config.SETTINGS).
    config_mod.SETTINGS = replace(original, block_russian=False)
    try:
        r = client.get("/api/content/blocked-c:12345")
        assert r.status_code == 200
        assert r.json()["country"] == "росія"
        assert stub.calls == 1
        # And with the flag off, nothing is written to the blocklist cache.
        assert main_mod._blocklist_cache.get("content:blocked-c:12345") is None
    finally:
        config_mod.SETTINGS = original
        _unregister("blocked-c")
