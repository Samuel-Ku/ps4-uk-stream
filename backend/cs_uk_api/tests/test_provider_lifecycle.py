"""Provider lifecycle contract (ADR-0004, issue #85).

The registry is the authoritative active-provider list. The lifecycle
contract:

  - Registration is via hardcoded ``register(...)`` calls in
    `cs_uk_api.providers._registry`. There is no hot-reload, no
    config-file-driven registration, no runtime plugin discovery.
  - Retirement is by commenting out the ``register(...)`` call. The
    adapter source stays in the tree for historical context or
    possible reactivation. The retired provider must NOT appear in
    ``/api/providers``, ``/api/sections``, ``/api/search``, or
    ``/api/browse``.
  - Registry order is preserved; ``/api/search`` returns the flattened
    results in ``PROVIDERS.values()`` order.
  - Health tracking is owned by issue #53 (sliding window + startup
    marker), unchanged by ADR-0004.

These tests guard the contract. The "retired" simulation is: remove a
provider from `PROVIDERS` (the runtime observable of a comment-out).
The endpoints should not see it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cs_uk_api.main import _search_cache, app
from cs_uk_api.models import SearchResult, Section
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

client = TestClient(app)


class _StubBase(BaseProvider):
    types = ("movie",)

    async def content(self, external_id, http):
        raise NotImplementedError

    async def stream(self, content_id, translation, http):
        raise NotImplementedError


def _make_provider(pid: str, *, sections: tuple[Section, ...] = ()):
    class _Stub(_StubBase):
        id = pid
        name = pid

        async def search(self, q, http):
            return [SearchResult(id=f"{pid}:x", provider=pid, form="movie", title="X", url="https://x/")]

        async def browse(self, section, page, http):
            return ([SearchResult(id=f"{pid}:x", provider=pid, form="movie", title="X", url="https://x/")], False)

    _Stub.sections = sections
    return _Stub()


# ---------------------------------------------------------------------------
# /api/providers reflects current registration state
# ---------------------------------------------------------------------------


def test_api_providers_lists_only_active_providers():
    """When a provider is removed from the registry, /api/providers must
    not list it. Simulates the operational comment-out retirement."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["alive"] = _make_provider("alive")
        PROVIDERS["retiree"] = _make_provider("retiree")

        r = client.get("/api/providers")
        ids = {p["id"] for p in r.json()}
        assert {"alive", "retiree"} <= ids

        # Operational retirement: remove from registry.
        del PROVIDERS["retiree"]

        r = client.get("/api/providers")
        ids = {p["id"] for p in r.json()}
        assert "alive" in ids
        assert "retiree" not in ids
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_api_sections_excludes_retired_providers():
    """A retired provider with `sections` declared must NOT appear in
    /api/sections once it's removed from the registry."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["alpha"] = _make_provider(
            "alpha", sections=(Section(id="top", title="Top", form="movie"),)
        )
        PROVIDERS["beta"] = _make_provider(
            "beta", sections=(Section(id="hot", title="Hot", form="movie"),)
        )

        r = client.get("/api/sections")
        provider_ids = {s["provider"] for s in r.json()}
        assert {"alpha", "beta"} <= provider_ids

        del PROVIDERS["alpha"]
        r = client.get("/api/sections")
        provider_ids = {s["provider"] for s in r.json()}
        assert "alpha" not in provider_ids
        assert "beta" in provider_ids
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_api_search_ignores_retired_providers():
    """A retired provider must not be queried by /api/search even if
    provider=all is given."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["kept"] = _make_provider("kept")
        PROVIDERS["retiree"] = _make_provider("retiree")

        # v3 (issue #71): /api/search returns groups; flatten sources
        # across all groups to enumerate the providers that responded.
        r = client.get("/api/search?q=q1")
        providers_in_results = {
            s["provider"] for g in r.json()["groups"] for s in g["sources"]
        }
        assert {"kept", "retiree"} <= providers_in_results

        # After retirement: only "kept" results returned.
        del PROVIDERS["retiree"]
        r = client.get("/api/search?q=q2")
        providers_in_results = {
            s["provider"] for g in r.json()["groups"] for s in g["sources"]
        }
        assert "kept" in providers_in_results
        assert "retiree" not in providers_in_results
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_api_browse_rejects_retired_provider():
    """/api/browse?provider=<retired> returns 400 unknown_provider."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["alive"] = _make_provider(
            "alive", sections=(Section(id="top", title="Top", form="movie"),)
        )

        r = client.get("/api/browse?provider=alive&section=top")
        assert r.status_code == 200

        # Operational retirement: remove from registry.
        del PROVIDERS["alive"]

        r = client.get("/api/browse?provider=alive&section=top")
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "unknown_provider"
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_api_search_rejects_retired_provider_in_query():
    """/api/search?provider=<retired> returns 400 unknown_provider."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["alive"] = _make_provider("alive")

        r = client.get("/api/search?q=q&provider=alive")
        assert r.status_code == 200

        del PROVIDERS["alive"]
        r = client.get("/api/search?q=q&provider=alive")
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "unknown_provider"
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


# ---------------------------------------------------------------------------
# Registry order is preserved (lazy reachability + ordering invariants)
# ---------------------------------------------------------------------------


def test_api_providers_preserves_registry_order():
    """/api/providers returns providers in registry order. The ADR-0004
    contract: registry order is the search order, no priority field,
    no secondary sort. Verify against the wire (not just the dict)
    so a future change that re-orders the response would break."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        # Insert in a deliberate order
        PROVIDERS["z-last"] = _make_provider("z-last")
        PROVIDERS["a-first"] = _make_provider("a-first")
        PROVIDERS["m-middle"] = _make_provider("m-middle")

        r = client.get("/api/providers")
        ids = [p["id"] for p in r.json()]
        assert ids == ["z-last", "a-first", "m-middle"]
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_search_lazy_reachability_does_not_block_startup():
    """Lazy reachability: providers are not pinged at startup. The
    backend boots with a provider that would fail its search() — the
    /api/search call proceeds (the failure surfaces as a ProviderFailure
    per ADR-0002, not as a startup error)."""
    # Trigger bootstrap explicitly. The import is already done by the
    # time any test in this module runs, but this is the cleanest way
    # to assert: "importing the registry does not raise even when any
    # single provider's prerequisites are missing".
    import cs_uk_api.providers._registry  # noqa: F401

    # The bootstrap ran without raising. If uakino's Chromium is
    # missing in this environment, the runtime marker is set:
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        # Import a provider that boots "down" deterministically (the
        # uakino startup marker), then verify a search call proceeds
        # (the failure surfaces as a ProviderFailure, not as a 500).
        from cs_uk_api.providers.uakino import UakinoProvider

        PROV = UakinoProvider()
        PROVIDERS["uakino"] = PROV
        _search_cache.clear()
        r = client.get("/api/search?q=startup-timeout")
        # The uakino provider's search() may raise or return [] depending
        # on the environment; the contract is: a 200 with whatever the
        # provider actually returns, not a 500 from the global handler.
        assert r.status_code in (200, 502)
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


# ---------------------------------------------------------------------------
# Adding a provider: the 8-step workflow checklist produces a working
# /api/providers entry. The wiring itself is a code change; the test
# proves the registry function accepts a new provider.
# ---------------------------------------------------------------------------


def test_register_appendable_at_runtime_for_testing():
    """A provider registered at runtime appears in /api/providers. This
    proves the registry is the single source of truth, independent of
    HOW the entry got there (bootstrap loop vs. test fixture)."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["runtime"] = _make_provider("runtime")
        r = client.get("/api/providers")
        ids = {p["id"] for p in r.json()}
        assert "runtime" in ids
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
