"""Uakino warm/lifecycle wiring + route gating (issues #193/#195/#196).

End-to-end TestClient tests through the full ASGI stack with the browser
session stubbed at the ``cs_uk_api.uakino_browser._session`` singleton
seam — the same seam the running process uses, so main.py's ``get_session()``
and the registered provider's lazy ``session`` property both resolve to the
stub. No real Chromium is launched.

Covers:
  - ``/api/providers`` transient ``warming`` status, ``ok`` once ready,
    ``down`` when a startup marker is present.
  - lifespan background warm + heartbeat task (#195): scheduled on startup
    when Chromium exists, warm failures pinned as deterministic startup
    markers, shutdown drains the task before closing the session.
  - ``provider=all`` fan-out skip (#193/#196): uakino dropped while cold or
    down — no failures entry, no session work — and the cache entry
    distinguishes the cold-uakino state from a warmed one.
  - explicit ``provider=uakino`` / ``uakino:`` content + stream routes:
    marker short-circuit (502, zero session work), bounded wait that returns
    results once ready, 503 ``warming`` on wait timeout.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

import cs_uk_api.catalog_state as catalog_state_mod
from cs_uk_api import main as main_mod
from cs_uk_api import uakino_browser
from cs_uk_api.health import TRACKER
from cs_uk_api.main import _content_cache, _search_cache, app
from cs_uk_api.models import (
    ContentResponse,
    SearchResult,
    StreamResponse,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider
from cs_uk_api.uakino_browser import SessionError

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_session_health_and_registry() -> Iterator[None]:
    """Restore the module session singleton + TRACKER + PROVIDERS + caches
    so these tests never leak state into each other (same pattern as
    test_search_grouping.py / test_api.py)."""
    saved_session = uakino_browser._session
    saved_providers = dict(PROVIDERS)
    TRACKER.reset()
    _search_cache.clear()
    _content_cache.clear()
    PROVIDERS.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved_providers)
        uakino_browser._session = saved_session
        TRACKER.reset()
        _search_cache.clear()
        _content_cache.clear()


class _StubSession:
    """UakinoSessionProtocol stub: controllable ``ready_event`` + counters.

    ``heartbeat_started`` / ``warm_calls`` are written from the app loop and
    read by the test thread (polled with a deadline), the same pattern as
    the existing ``test_lifespan_closes_uakino_session``.
    """

    def __init__(self) -> None:
        self.ready_event = asyncio.Event()
        self.warm_calls = 0
        self.heartbeat_started = False
        self.fetch_calls: list[tuple[str, str, str | None]] = []
        self.close_calls = 0

    async def warm(self) -> None:
        self.warm_calls += 1

    async def heartbeat_loop(self, record: Any) -> None:
        self.heartbeat_started = True
        while True:
            await asyncio.sleep(3600)

    async def fetch(
        self, path: str, method: str = "GET", data: str | None = None
    ) -> tuple[int, str]:
        self.fetch_calls.append((method, path, data))
        return 200, "<html>ok</html>"

    async def close(self) -> None:
        self.close_calls += 1


class _Provider(BaseProvider):
    """Search/content/stream stub that records calls (id ``pid``)."""

    types = ("movie",)

    def __init__(
        self,
        pid: str,
        *,
        results: list[SearchResult] | None = None,
        content: ContentResponse | None = None,
        stream: StreamResponse | None = None,
        calls: list[Any] | None = None,
    ) -> None:
        self.id = pid
        self.name = pid.title()
        self._results = results or []
        self._content = content
        self._stream = stream
        self.calls = calls if calls is not None else []

    async def search(self, q: str, http: Any) -> list[SearchResult]:
        self.calls.append(("search", q))
        return list(self._results)

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        self.calls.append(("content", external_id))
        if self._content is None:
            raise NotImplementedError("content not stubbed")
        return self._content

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> StreamResponse:
        self.calls.append(("stream", content_id, translation))
        if self._stream is None:
            raise NotImplementedError("stream not stubbed")
        return self._stream


def _result(pid: str, title: str, *, year: int | None = None, n: str = "1") -> SearchResult:
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        type=cast(Any, "movie"),
        title=title,
        year=year,
        poster=f"https://{pid}.example/{n}.jpg",
        url=f"https://{pid}.example/{n}",
    )


def _dune_content() -> ContentResponse:
    return ContentResponse(
        id="uakino:filmy:12567-dyuna",
        type=cast(Any, "movie"),
        title="Дюна",
        year=2021,
        translations=[Translation(id="uk", label="Українська")],
    )


def _wait_until(predicate: Any, what: str, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} never became true within {timeout}s")
        time.sleep(0.005)


# --------------------------------------------------------------------------
# /api/providers — transient warming status (#195)
# --------------------------------------------------------------------------


def test_providers_reports_warming_until_ready() -> None:
    stub = _StubSession()  # ready_event unset
    uakino_browser._session = stub  # type: ignore[assignment]
    PROVIDERS["uakino"] = _Provider("uakino")

    r = client.get("/api/providers")
    assert r.status_code == 200
    ua = next(p for p in r.json() if p["id"] == "uakino")
    assert ua["status"] == "warming"

    # Once the warm completes the sliding-window state takes over.
    stub.ready_event.set()
    r2 = client.get("/api/providers")
    ua2 = next(p for p in r2.json() if p["id"] == "uakino")
    assert ua2["status"] == "ok"


def test_providers_startup_marker_beats_warming() -> None:
    stub = _StubSession()  # ready_event unset
    uakino_browser._session = stub  # type: ignore[assignment]
    PROVIDERS["uakino"] = _Provider("uakino")
    TRACKER.mark_startup("uakino", "chromium_missing")

    r = client.get("/api/providers")
    ua = next(p for p in r.json() if p["id"] == "uakino")
    assert ua["status"] == "down"


# --------------------------------------------------------------------------
# lifespan: background warm + heartbeat task (#195)
# --------------------------------------------------------------------------


def _monkeypatch_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lifespan only schedules the warm task when a Chromium binary
    # exists; point it at a path that certainly exists. The stub session
    # never launches a real browser, so the path value itself is unused.
    monkeypatch.setattr(main_mod, "DEFAULT_CHROMIUM", "/bin/true")


def test_lifespan_warms_session_then_closes_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubSession()
    uakino_browser._session = stub  # type: ignore[assignment]
    _monkeypatch_chromium(monkeypatch)

    with TestClient(app):
        _wait_until(lambda: stub.heartbeat_started, "warm task heartbeat")
        assert stub.warm_calls == 1

    # shutdown drained the warm/heartbeat task before closing the session
    assert stub.close_calls == 1


def test_lifespan_warm_failure_pins_startup_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom(_StubSession):
        async def warm(self) -> None:
            raise SessionError("chromium exploded")

    stub = _Boom()
    uakino_browser._session = stub  # type: ignore[assignment]
    _monkeypatch_chromium(monkeypatch)

    with TestClient(app):
        _wait_until(
            lambda: TRACKER.startup_marker("uakino") == "warm_failed",
            "warm_failed marker",
        )

    assert TRACKER.status("uakino") == "down"
    assert stub.close_calls == 1


# --------------------------------------------------------------------------
# provider=all fan-out skip (#193/#196)
# --------------------------------------------------------------------------


def test_fanout_skips_cold_uakino_without_failure() -> None:
    stub = _StubSession()  # ready_event unset
    uakino_browser._session = stub  # type: ignore[assignment]
    uakino_calls: list[Any] = []
    PROVIDERS["eneyida"] = _Provider(
        "eneyida", results=[_result("eneyida", "Дюна", year=2021)]
    )
    PROVIDERS["uakino"] = _Provider(
        "uakino", results=[_result("uakino", "Дюна", year=2021)], calls=uakino_calls
    )

    r = client.get("/api/search?q=дюна")
    assert r.status_code == 200
    body = r.json()
    # dropped from the fan-out: never invoked, no session work, and no
    # failures entry (a cold session is not an upstream error)
    assert uakino_calls == []
    assert stub.fetch_calls == []
    assert all(f["provider"] != "uakino" for f in body.get("failures", []))
    providers = {s["provider"] for g in body["groups"] for s in g["sources"]}
    assert providers == {"eneyida"}


def test_fanout_cache_distinguishes_cold_then_warmed() -> None:
    stub = _StubSession()
    uakino_browser._session = stub  # type: ignore[assignment]
    eneyida_calls: list[Any] = []
    uakino_calls: list[Any] = []
    PROVIDERS["eneyida"] = _Provider(
        "eneyida", results=[_result("eneyida", "Дюна", year=2021)], calls=eneyida_calls
    )
    PROVIDERS["uakino"] = _Provider(
        "uakino", results=[_result("uakino", "Дюна", year=2021)], calls=uakino_calls
    )
    _search_cache.clear()

    r1 = client.get("/api/search?q=дюна")
    assert r1.status_code == 200
    assert uakino_calls == []
    assert eneyida_calls == [("search", "дюна")]

    # The cold entry is cached under a distinct key: readying the session
    # must NOT serve a stale uakino-less response for the same query.
    stub.ready_event.set()
    r2 = client.get("/api/search?q=дюна")
    assert r2.status_code == 200
    assert eneyida_calls == [("search", "дюна"), ("search", "дюна")]
    assert uakino_calls == [("search", "дюна")]
    providers = {s["provider"] for g in r2.json()["groups"] for s in g["sources"]}
    assert "uakino" in providers


# --------------------------------------------------------------------------
# explicit provider=uakino route gating (#196)
# --------------------------------------------------------------------------


def test_explicit_search_502_short_circuit_on_startup_marker() -> None:
    stub = _StubSession()
    uakino_browser._session = stub  # type: ignore[assignment]
    TRACKER.mark_startup("uakino", "chromium_missing")
    uakino_calls: list[Any] = []
    PROVIDERS["uakino"] = _Provider(
        "uakino", results=[_result("uakino", "Дюна", year=2021)], calls=uakino_calls
    )

    r = client.get("/api/search?q=дюна&provider=uakino")
    assert r.status_code == 502
    body = r.json()["detail"]
    assert body["error"] == "upstream_unreachable"
    assert "chromium_missing" in body["message"]
    # short-circuited before any session/provider work
    assert uakino_calls == []
    assert stub.fetch_calls == []


def test_explicit_search_503_warming_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubSession()  # ready never set
    uakino_browser._session = stub  # type: ignore[assignment]
    uakino_calls: list[Any] = []
    PROVIDERS["uakino"] = _Provider(
        "uakino", results=[_result("uakino", "Дюна", year=2021)], calls=uakino_calls
    )
    # WARM_WAIT_S lives in catalog_state (ticket #106: the shared search
    # owns the uakino wait), so the patch targets that module.
    monkeypatch.setattr(catalog_state_mod, "WARM_WAIT_S", 0.05)

    r = client.get("/api/search?q=дюна&provider=uakino")
    assert r.status_code == 503
    body = r.json()["detail"]
    assert body["error"] == "warming"
    assert uakino_calls == []
    assert stub.fetch_calls == []


def test_explicit_search_returns_results_when_ready() -> None:
    stub = _StubSession()
    stub.ready_event.set()
    uakino_browser._session = stub  # type: ignore[assignment]
    PROVIDERS["uakino"] = _Provider(
        "uakino", results=[_result("uakino", "Дюна", year=2021)]
    )

    r = client.get("/api/search?q=дюна&provider=uakino")
    assert r.status_code == 200
    body = r.json()
    assert len(body["groups"]) == 1
    assert body["groups"][0]["sources"][0]["provider"] == "uakino"


@pytest.mark.asyncio
async def test_explicit_search_waits_for_ready_then_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded wait on ``ready_event`` resolves within ``WARM_WAIT_S``:
    the request returns the uakino results instead of timing out."""
    stub = _StubSession()  # ready_event unset
    uakino_browser._session = stub  # type: ignore[assignment]
    monkeypatch.setattr(catalog_state_mod, "WARM_WAIT_S", 5.0)
    PROVIDERS["uakino"] = _Provider(
        "uakino", results=[_result("uakino", "Дюна", year=2021)]
    )
    _search_cache.clear()

    async def _set_ready() -> None:
        await asyncio.sleep(0.05)
        stub.ready_event.set()

    transport = httpx.ASGITransport(app=app)
    task = asyncio.create_task(_set_ready())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/search?q=дюна&provider=uakino")
    await task

    assert r.status_code == 200
    body = r.json()
    assert len(body["groups"]) == 1
    assert body["groups"][0]["sources"][0]["provider"] == "uakino"


def test_content_503_warming_when_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubSession()  # ready never set
    uakino_browser._session = stub  # type: ignore[assignment]
    PROVIDERS["uakino"] = _Provider("uakino", content=_dune_content())
    monkeypatch.setattr(catalog_state_mod, "WARM_WAIT_S", 0.05)

    r = client.get("/api/content/uakino:filmy:12567-dyuna")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "warming"


def test_content_200_when_ready() -> None:
    stub = _StubSession()
    stub.ready_event.set()
    uakino_browser._session = stub  # type: ignore[assignment]
    PROVIDERS["uakino"] = _Provider("uakino", content=_dune_content())

    r = client.get("/api/content/uakino:filmy:12567-dyuna")
    assert r.status_code == 200
    assert r.json()["title"] == "Дюна"


def test_stream_502_short_circuit_on_startup_marker() -> None:
    stub = _StubSession()
    uakino_browser._session = stub  # type: ignore[assignment]
    TRACKER.mark_startup("uakino", "chromium_missing")
    PROVIDERS["uakino"] = _Provider(
        "uakino", stream=StreamResponse(url="https://ashdi.vip/x.m3u8", type="m3u8")
    )

    r = client.get("/api/stream/uakino:filmy:12567-dyuna")
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "upstream_unreachable"
    assert stub.fetch_calls == []


def test_stream_200_when_ready() -> None:
    stub = _StubSession()
    stub.ready_event.set()
    uakino_browser._session = stub  # type: ignore[assignment]
    PROVIDERS["uakino"] = _Provider(
        "uakino", stream=StreamResponse(url="https://ashdi.vip/x.m3u8", type="m3u8")
    )

    r = client.get("/api/stream/uakino:filmy:12567-dyuna")
    assert r.status_code == 200
    assert r.json()["url"] == "https://ashdi.vip/x.m3u8"
