"""Failure semantics for /api/search (ADR-0002, issue #81).

The search endpoint returns 200 OK with a `failures: list[ProviderFailure]`
array when at least one provider's contribution failed, and 502 with
`ErrorResponse(error="search_timeout", ...)` only when the overall 12s
budget was exceeded for ALL providers (i.e. nothing usable came back in
time).

The contract (per ADR-0002):
- A provider that returns [] from search() with no exception is NOT a
  failure — empty results are a legitimate "no match" answer.
- A provider whose search() raises anything contributes a ProviderFailure
  with code derived from the exception class (timeout vs upstream_unreachable).
- The failures field is omitted from JSON when no provider failed.
- A response with a non-empty failures field is still cached.
- The 502 path is never cached.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from cs_uk_api.main import _search_cache, app
from cs_uk_api.models import ProviderFailure, SearchResponse
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures: stub providers that simulate success / failure / hang
# ---------------------------------------------------------------------------


class _StubBase(BaseProvider):
    """BaseProvider stub for search-only tests.

    The /api/search contract is what we care about; content() and
    stream() are abstract on BaseProvider but unused in these tests.
    """

    types = ("movie",)

    async def content(self, external_id, http):  # pragma: no cover - unused
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # pragma: no cover - unused
        raise NotImplementedError


class _Ok(_StubBase):
    id = "ok-stub"
    name = "OkStub"

    async def search(self, q, http):
        return []


class _OneResult(_StubBase):
    id = "one-result"
    name = "OneResult"

    async def search(self, q, http):
        from cs_uk_api.models import SearchResult

        return [SearchResult(id="one-result:x", provider="one-result", type="movie", title="X", url="https://x/")]


def _fa_provider(provider_id: str, exc: BaseException):
    """Build a provider whose search() raises the given exception."""

    class _Fa(_StubBase):
        id = provider_id
        name = provider_id

        async def search(self, q, http):
            raise exc

    return _Fa()


def _hang_provider(provider_id: str, delay_s: float):
    """Build a provider whose search() sleeps past the budget."""

    class _Hang(_StubBase):
        id = provider_id
        name = provider_id

        async def search(self, q, http):
            await asyncio.sleep(delay_s)
            return []

    return _Hang()


# ---------------------------------------------------------------------------
# The contracts
# ---------------------------------------------------------------------------


def test_search_returns_200_with_failures_when_some_providers_fail():
    """When some providers succeed and some fail, the response is 200 with
    failures populated and results from the successful providers."""
    # Save the original registry state for cleanup
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["ok"] = _OneResult()
        PROVIDERS["boom"] = _fa_provider(
            "boom", RuntimeError("upstream site down")
        )

        _search_cache.clear()
        r = client.get("/api/search?q=test")
        assert r.status_code == 200
        body = r.json()
        # v3 (issue #71): response is grouped. One group, one source
        # (the other provider raised). The provider on the lone source
        # matches the pre-#71 flat-results expectation.
        assert len(body["groups"]) == 1
        assert len(body["groups"][0]["sources"]) == 1
        assert body["groups"][0]["sources"][0]["provider"] == "one-result"
        assert "failures" in body
        assert len(body["failures"]) == 1
        assert body["failures"][0]["provider"] == "boom"
        assert body["failures"][0]["code"] == "upstream_unreachable"
        assert "upstream site down" in body["failures"][0]["message"]
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


def test_search_omits_failures_field_when_all_providers_succeed():
    """When no provider failed, the failures field is omitted from JSON."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["only"] = _Ok()

        _search_cache.clear()
        r = client.get("/api/search?q=test")
        assert r.status_code == 200
        body = r.json()
        assert body["groups"] == []
        assert "failures" not in body
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


def test_search_returns_200_with_failures_when_all_providers_fail_individually():
    """When all providers raise but wait_for doesn't fire, the response is
    200 with empty groups and a populated failures list. 502 is reserved
    for the total-timeout case."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["a"] = _fa_provider("a", RuntimeError("site A down"))
        PROVIDERS["b"] = _fa_provider("b", RuntimeError("site B down"))

        _search_cache.clear()
        r = client.get("/api/search?q=test")
        assert r.status_code == 200
        body = r.json()
        assert body["groups"] == []
        assert len(body["failures"]) == 2
        assert {f["provider"] for f in body["failures"]} == {"a", "b"}
        for f in body["failures"]:
            assert f["code"] == "upstream_unreachable"
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


def test_search_classifies_httpx_timeouts_as_timeout_code():
    """httpx.TimeoutException should map to code='timeout'."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["slow"] = _fa_provider(
            "slow", httpx.ReadTimeout("upstream took too long")
        )

        _search_cache.clear()
        r = client.get("/api/search?q=test")
        assert r.status_code == 200
        body = r.json()
        assert body["failures"][0]["provider"] == "slow"
        assert body["failures"][0]["code"] == "timeout"
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


def test_search_returns_502_with_search_timeout_when_overall_budget_fires():
    """When the overall 12s budget is exceeded AND no provider returned any
    results, the route returns 502 with ErrorResponse(error="search_timeout")."""

    import cs_uk_api.config as config_mod
    import cs_uk_api.main as main_mod

    saved = dict(PROVIDERS)
    saved_settings = config_mod.SETTINGS
    patched = type(saved_settings)(
        host=saved_settings.host,
        port=saved_settings.port,
        upstream_timeout_s=0.05,
        search_total_timeout_s=0.1,  # 100ms total budget
        poster_size_cap_bytes=saved_settings.poster_size_cap_bytes,
        poster_allowed_hosts=saved_settings.poster_allowed_hosts,
        cache_search_s=saved_settings.cache_search_s,
        cache_content_s=saved_settings.cache_content_s,
        cache_home_s=saved_settings.cache_home_s,
        cache_poster_s=saved_settings.cache_poster_s,
        cache_gated_s=saved_settings.cache_gated_s,
        poster_cache_dir=saved_settings.poster_cache_dir,
        poster_disk_ttl_s=saved_settings.poster_disk_ttl_s,
        providers=saved_settings.providers,
        block_russian=saved_settings.block_russian,
        home_row_limit=saved_settings.home_row_limit,
    )
    config_mod.SETTINGS = patched
    main_mod.SETTINGS = patched
    try:
        PROVIDERS.clear()
        PROVIDERS["hanga"] = _hang_provider("hanga", 5.0)
        PROVIDERS["hangb"] = _hang_provider("hangb", 5.0)

        _search_cache.clear()
        r = client.get("/api/search?q=test")
        assert r.status_code == 502
        assert r.json()["detail"]["error"] == "search_timeout"
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        config_mod.SETTINGS = saved_settings
        main_mod.SETTINGS = saved_settings
        _search_cache.clear()


def test_search_caches_200_responses_with_failures():
    """A 200 response with a populated failures list is cached and returned
    by reference on subsequent requests (no second search round)."""
    saved = dict(PROVIDERS)
    try:
        PROVIDERS.clear()
        PROVIDERS["ok"] = _OneResult()
        PROVIDERS["boom"] = _fa_provider("boom", RuntimeError("explicit"))

        _search_cache.clear()

        # First request: triggers the actual search
        r1 = client.get("/api/search?q=cached")
        assert r1.status_code == 200
        first_failures = r1.json()["failures"]
        assert len(first_failures) == 1

        # Second request: should hit the cache (no re-execution of providers)
        # We register a different provider on the same id to detect re-execution:
        # if the route re-runs search(), the new provider's behaviour shows up.
        class _Different(_StubBase):
            id = "ok"
            name = "Different"

            async def search(self, q, http):
                raise AssertionError("route re-ran search; cache miss")

        PROVIDERS["ok"] = _Different()
        r2 = client.get("/api/search?q=cached")
        assert r2.status_code == 200
        assert r2.json()["failures"] == first_failures
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        _search_cache.clear()


def test_search_does_not_cache_502_responses():
    """A 502 response (total timeout) is never cached; a subsequent request
    that succeeds returns 200."""

    import cs_uk_api.config as config_mod
    import cs_uk_api.main as main_mod

    saved = dict(PROVIDERS)
    saved_settings = config_mod.SETTINGS
    patched = type(saved_settings)(
        host=saved_settings.host,
        port=saved_settings.port,
        upstream_timeout_s=0.05,
        search_total_timeout_s=0.05,
        poster_size_cap_bytes=saved_settings.poster_size_cap_bytes,
        poster_allowed_hosts=saved_settings.poster_allowed_hosts,
        cache_search_s=saved_settings.cache_search_s,
        cache_content_s=saved_settings.cache_content_s,
        cache_home_s=saved_settings.cache_home_s,
        cache_poster_s=saved_settings.cache_poster_s,
        cache_gated_s=saved_settings.cache_gated_s,
        poster_cache_dir=saved_settings.poster_cache_dir,
        poster_disk_ttl_s=saved_settings.poster_disk_ttl_s,
        providers=saved_settings.providers,
        block_russian=saved_settings.block_russian,
        home_row_limit=saved_settings.home_row_limit,
    )
    config_mod.SETTINGS = patched
    main_mod.SETTINGS = patched
    try:
        PROVIDERS.clear()
        PROVIDERS["hang"] = _hang_provider("hang", 1.0)

        _search_cache.clear()
        r1 = client.get("/api/search?q=once")
        assert r1.status_code == 502

        # Replace the provider with a fast, successful one. If the route
        # re-ran search(), we get a 200 with results. If it cached the 502,
        # we still get 502.
        PROVIDERS["hang"] = _Ok()
        r2 = client.get("/api/search?q=once")
        assert r2.status_code == 200
        assert r2.json()["groups"] == []
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        config_mod.SETTINGS = saved_settings
        main_mod.SETTINGS = saved_settings
        _search_cache.clear()


def test_search_returns_200_with_partial_results_when_overall_budget_fires():
    """ADR-0002 deal-breaker seam: when the overall budget fires but some
    providers had ALREADY completed, the response is 200 with results
    AND synthetic timeout rows for the in-flight providers. 502 is
    reserved for the all-timeout case."""
    import cs_uk_api.config as config_mod
    import cs_uk_api.main as main_mod

    saved = dict(PROVIDERS)
    saved_settings = config_mod.SETTINGS
    patched = type(saved_settings)(
        host=saved_settings.host,
        port=saved_settings.port,
        upstream_timeout_s=0.05,
        search_total_timeout_s=0.1,  # 100ms total budget
        poster_size_cap_bytes=saved_settings.poster_size_cap_bytes,
        poster_allowed_hosts=saved_settings.poster_allowed_hosts,
        cache_search_s=saved_settings.cache_search_s,
        cache_content_s=saved_settings.cache_content_s,
        cache_home_s=saved_settings.cache_home_s,
        cache_poster_s=saved_settings.cache_poster_s,
        cache_gated_s=saved_settings.cache_gated_s,
        poster_cache_dir=saved_settings.poster_cache_dir,
        poster_disk_ttl_s=saved_settings.poster_disk_ttl_s,
        providers=saved_settings.providers,
        block_russian=saved_settings.block_russian,
        home_row_limit=saved_settings.home_row_limit,
    )
    config_mod.SETTINGS = patched
    main_mod.SETTINGS = patched
    try:
        PROVIDERS.clear()
        # Fast provider: returns immediately.
        PROVIDERS["fast"] = _OneResult()
        # Slow provider: hangs past the 100ms budget.
        PROVIDERS["slow"] = _hang_provider("slow", 5.0)

        _search_cache.clear()
        r = client.get("/api/search?q=mixed")
        assert r.status_code == 200
        body = r.json()
        # Fast provider contributes a result, surfaced as one group with
        # one source (v3 issue #71: groups replace the flat results list).
        assert len(body["groups"]) == 1
        assert len(body["groups"][0]["sources"]) == 1
        assert body["groups"][0]["sources"][0]["provider"] == "one-result"
        # Slow provider contributes a synthetic timeout row.
        assert "failures" in body
        timeout_rows = [f for f in body["failures"] if f["provider"] == "slow"]
        assert len(timeout_rows) == 1
        assert timeout_rows[0]["code"] == "timeout"
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        config_mod.SETTINGS = saved_settings
        main_mod.SETTINGS = saved_settings
        _search_cache.clear()


def test_search_provider_failure_carries_provider_id_and_code():
    """ProviderFailure is a wire-level Pydantic model with the right fields."""
    f = ProviderFailure(provider="uakino", code="timeout", message="x")
    assert f.provider == "uakino"
    assert f.code == "timeout"
    assert f.message == "x"


def test_exclude_unset_omits_failures_when_not_set():
    """SearchResponse(failures=[]) default produces an empty list, but
    ``exclude_unset`` semantics (used by the route via FastAPI's
    ``response_model_exclude_unset``) must omit the field entirely
    when no failures were passed during construction."""
    resp = SearchResponse(query="q", groups=[])
    assert resp.failures == []
    assert resp.model_dump(exclude_unset=True) == {"query": "q", "groups": []}
