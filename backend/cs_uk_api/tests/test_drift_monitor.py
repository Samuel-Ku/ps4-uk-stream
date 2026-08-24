"""Drift monitor orchestration + issue-flow tests (spec #285, #288).

The monitor's verdict/counter/issue boundary is pinned with a recorder
gateway and stub providers: first failure logs only (no issue), the
second consecutive failure files (deduped — repeated failures reuse the
open issue), a healthy pass resets the counter and closes a previously
open issue, and the report + exit status reflect every provider.
"""

from __future__ import annotations

from typing import Any

import pytest

from cs_uk_api.drift.baseline import BaselineStore
from cs_uk_api.drift.monitor import run_once
from cs_uk_api.models import SearchResult, StreamResponse
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError


class _RecorderGateway:
    """Per-provider issue-tracker recorder. Stateful per provider:
    ``find_open`` returns None until that provider's issue is filed,
    then its number — so dedupe and recovery are exercised per
    provider, exactly as the real tracker behaves."""

    def __init__(self, open_issue: str | None = None) -> None:
        # ``open_issue``: when given, EVERY provider answers it as a
        # pre-existing open issue (for the reuse-path tests).
        self._pre_existing = open_issue
        self._filed: dict[str, str] = {}
        self.opened: list[str] = []
        self.commented: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.find_calls: list[str] = []

    def find_open(self, provider_id: str) -> str | None:
        self.find_calls.append(provider_id)
        if self._pre_existing is not None:
            return self._pre_existing
        return self._filed.get(provider_id)

    def open(self, provider_id: str, body: str) -> str:
        self.opened.append(provider_id)
        self._filed[provider_id] = "42"
        return self._filed[provider_id]

    def comment(self, provider_id: str, issue_number: str, body: str) -> None:
        self.commented.append((provider_id, issue_number))

    def close(self, provider_id: str, issue_number: str, comment: str) -> None:
        self.closed.append((provider_id, issue_number))


class _FakeHttp:
    """Minimal httpx-like client: HEAD answers 200 so deep probes pass."""

    async def head(self, url: str) -> Any:
        class _R:
            status_code = 200

        return _R()


def _card(pid: str, i: int, *, form: str = "movie") -> SearchResult:
    return SearchResult(
        id=f"{pid}:ext-{i}",
        provider=pid,
        form=form,  # type: ignore[arg-type]
        title=f"Title {i}",
        url=f"https://{pid}.example/{i}",
    )


class _FailProvider(BaseProvider):
    """Always fails: zero cards (hard floor) — deterministic drift."""

    id = "p1"
    name = "P1"
    types = ("movie", "series")
    newest_section = "page"

    async def search(self, query: str, http: object) -> list[SearchResult]:
        return []

    async def browse(self, section: str, page: int, http: object) -> tuple[list[SearchResult], bool]:
        return [], False

    async def content(self, external_id: str, http: object) -> object:
        raise ProviderError("not_found", "no content")

    async def stream(self, content_id: str, translation: str | None, http: object) -> StreamResponse:
        raise ProviderError("not_found", "no stream")


class _OkProvider(BaseProvider):
    """Always healthy: a stable 5-card listing (>= hard floor 2)."""

    id = "p2"
    name = "P2"
    types = ("movie", "series")
    newest_section = "page"

    async def search(self, query: str, http: object) -> list[SearchResult]:
        return [_card("p2", i) for i in range(5)]

    async def browse(self, section: str, page: int, http: object) -> tuple[list[SearchResult], bool]:
        return [_card("p2", i) for i in range(5)], False

    async def content(self, external_id: str, http: object) -> object:
        from cs_uk_api.models import ContentResponse, Translation

        return ContentResponse(
            id=f"p2:{external_id}",
            form="movie",  # type: ignore[arg-type]
            title="T",
            translations=[Translation(id="uk", label="Дубляж")],
        )

    async def stream(self, content_id: str, translation: str | None, http: object) -> StreamResponse:
        return StreamResponse(url="https://cdn.example.test/v.mp4", type="mp4", headers={})


class _OkP1Provider(_OkProvider):
    """The healthy provider under p1's id — for the recovery test, which
    must swap the FAILING p1 for a healthy p1 (same provider id)."""

    id = "p1"
    name = "P1"

    async def search(self, query: str, http: object) -> list[SearchResult]:
        return [_card("p1", i) for i in range(5)]

    async def browse(self, section: str, page: int, http: object) -> tuple[list[SearchResult], bool]:
        return [_card("p1", i) for i in range(5)], False


@pytest.fixture(autouse=True)
def _isolate_providers() -> None:
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def _seed(*providers: BaseProvider) -> None:
    for p in providers:
        PROVIDERS[p.id] = p


# ------------------------------------------------------------ run / report


@pytest.mark.asyncio
async def test_run_reports_ok_and_failed_providers(tmp_path) -> None:
    _seed(_FailProvider(), _OkProvider())
    store = BaselineStore(tmp_path / "state.json")
    report = await run_once(
        day_offset=0,
        store=store,
        gateway=_RecorderGateway(),
        report_path=tmp_path / "report.json",
        skip_issues=True,
        http=_FakeHttp(),
    )

    assert report.failed == ["p1"]
    assert "p2" not in report.failed
    assert report.providers["p1"]["consecutive_failures"] == 1
    assert report.providers["p2"]["consecutive_failures"] == 0
    assert report.providers["p2"]["ok"] is True
    # Report file written.
    assert (tmp_path / "report.json").exists()
    # Excluded providers never appear.
    assert "uakino" not in report.providers


@pytest.mark.asyncio
async def test_run_writes_report_file(tmp_path) -> None:
    _seed(_OkProvider())
    report_path = tmp_path / "report.json"
    await run_once(
        day_offset=0,
        store=BaselineStore(tmp_path / "state.json"),
        gateway=_RecorderGateway(),
        report_path=report_path,
        skip_issues=True,
        http=_FakeHttp(),
    )
    import json

    # The report is parseable JSON written atomically — no half-written
    # file or temp leftover can survive a run.
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    assert "p2" in raw["providers"]
    assert "run_at" in raw
    leftovers = [f.name for f in tmp_path.iterdir() if ".tmp" in f.name]
    assert leftovers == []


# ------------------------------------------------------------ issue flow


@pytest.mark.asyncio
async def test_first_failure_logs_only_no_issue(tmp_path) -> None:
    """Ticket #288 AC: the FIRST failure files nothing."""
    _seed(_FailProvider())
    gateway = _RecorderGateway()
    await run_once(
        day_offset=0,
        store=BaselineStore(tmp_path / "state.json"),
        gateway=gateway,
        report_path=None,
        http=_FakeHttp(),
    )

    assert gateway.opened == []
    assert gateway.closed == []


@pytest.mark.asyncio
async def test_second_consecutive_failure_files_issue(tmp_path) -> None:
    """The SECOND consecutive failure files (opens) the issue."""
    _seed(_FailProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    await run_once(day_offset=0, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == []

    await run_once(day_offset=1, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == ["p1"]


@pytest.mark.asyncio
async def test_repeated_failures_reuse_open_issue(tmp_path) -> None:
    """Repeated failures do NOT duplicate — the open issue is reused.
    The stateful gateway returns None until the issue is actually
    filed, then the filed number, so a third failure finds it open."""
    _seed(_FailProvider())
    gateway = _RecorderGateway()  # stateful: no pre-existing issue
    store = BaselineStore(tmp_path / "state.json")

    # Two failures to reach the threshold, then a third failure while an
    # issue is already open.
    await run_once(day_offset=0, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    await run_once(day_offset=1, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == ["p1"]

    await run_once(day_offset=2, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == ["p1"]  # no second open
    # find_open was consulted on the filing run AND the reuse run (the
    # first failing run is below the threshold and never looks).
    assert gateway.find_calls.count("p1") >= 2


@pytest.mark.asyncio
async def test_recovery_resets_counter_and_closes_issue(tmp_path) -> None:
    """A healthy pass after failures resets the counter and closes the
    previously open issue (ticket #288 AC: recovery comments + closes)."""
    gateway = _RecorderGateway()  # stateful: issue opens on 2nd failure
    store = BaselineStore(tmp_path / "state.json")

    # Fail twice with p1 only, then swap in a healthy p1.
    _seed(_FailProvider())
    await run_once(day_offset=0, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    await run_once(day_offset=1, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == ["p1"]

    # Now p1 is healthy — recovery path finds the open issue and closes.
    _seed(_OkP1Provider())
    await run_once(day_offset=2, store=store, gateway=gateway, report_path=None, http=_FakeHttp())

    assert gateway.closed == [("p1", "42")]
    assert gateway.commented == [("p1", "42")]
    state = store.get("p1")
    assert state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_skip_issues_never_touches_gateway(tmp_path) -> None:
    """--no-issues runs probes and verdicts without tracker calls."""
    _seed(_FailProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    await run_once(day_offset=0, store=store, gateway=gateway, report_path=None, skip_issues=True, http=_FakeHttp())
    await run_once(day_offset=1, store=store, gateway=gateway, report_path=None, skip_issues=True, http=_FakeHttp())

    assert gateway.opened == []
    assert gateway.find_calls == []
    # Counters still tracked (report + next real run use them).
    assert store.get("p1").consecutive_failures == 2


@pytest.mark.asyncio
async def test_failed_deep_probe_fails_provider(tmp_path) -> None:
    """A deep-probe failure fails the provider even with a healthy
    listing (animeon's lost stream URLs — spec #285's regression case)."""

    class _DeepFail(_OkProvider):
        async def stream(self, content_id: str, translation: str | None, http: object) -> StreamResponse:
            raise ProviderError("not_found", "no stream")

    _seed(_DeepFail())
    # day_offset must include p1 (renamed to p1 id is already p2 — use a
    # deep-every large enough to guarantee the provider is deep-probed).
    report = await run_once(
        day_offset=0,
        deep_every_n_days=1,  # every provider every run
        store=BaselineStore(tmp_path / "state.json"),
        gateway=_RecorderGateway(),
        report_path=None,
        skip_issues=True,
        http=_FakeHttp(),
    )

    assert "p2" in report.failed
    assert "deep probe failed" in report.providers["p2"]["reason"]
