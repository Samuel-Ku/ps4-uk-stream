"""UakinoSession lifecycle tests (issue #194) against a FakePlaywright surface.

All assertions are external-observable — through `ready_event`, the
`record` callback contract, and `fetch()` return values. No internal
attribute reads on `UakinoSession`, no `mock.patch` of private methods.
No real Chromium is required.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from cs_uk_api.uakino_browser import WARM_TIMEOUT_S, SessionError, UakinoSession


class FakePage:
    """Controllable page: goto/evaluate results, 403 cadence, counters.

    `evaluate_results` is popped left-to-right; once exhausted a 200 is
    returned so a happy-path fetch never starves. `evaluate_delay` makes
    an in-flight evaluate observable through `max_evaluate_in_flight`.
    """

    def __init__(
        self,
        *,
        goto_status: int = 200,
        evaluate_results: list[dict[str, Any]] | None = None,
        goto_delay: float = 0.0,
        evaluate_delay: float = 0.0,
    ) -> None:
        self.goto_status = goto_status
        self._evaluate_results = list(evaluate_results or [])
        self.goto_delay = goto_delay
        self.evaluate_delay = evaluate_delay
        self.goto_calls = 0
        self.wait_for_timeout_total = 0.0
        self.evaluate_count = 0
        self.max_evaluate_in_flight = 0
        self._in_flight = 0

    async def goto(self, url: str, **kwargs: Any) -> Any:
        self.goto_calls += 1
        if self.goto_delay:
            await asyncio.sleep(self.goto_delay)
        return SimpleNamespace(status=self.goto_status)

    async def wait_for_timeout(self, ms: int) -> None:
        self.wait_for_timeout_total += ms / 1000.0
        await asyncio.sleep(0)

    async def evaluate(self, js: str, args: dict[str, Any]) -> dict[str, Any]:
        self.evaluate_count += 1
        self._in_flight += 1
        self.max_evaluate_in_flight = max(self.max_evaluate_in_flight, self._in_flight)
        try:
            if self.evaluate_delay:
                await asyncio.sleep(self.evaluate_delay)
            if self._evaluate_results:
                return self._evaluate_results.pop(0)
            return {"status": 200, "text": "<html>ok</html>"}
        finally:
            self._in_flight -= 1


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self._page = page
        self.closed = False

    async def new_context(self, **kwargs: Any) -> FakeContext:
        return FakeContext(self._page)

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def new_page(self) -> FakePage:
        return self._page


class FakeChromium:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def launch(self, **kwargs: Any) -> FakeBrowser:
        return FakeBrowser(self._page)


class FakePlaywright:
    def __init__(
        self, page: FakePage | None = None, *, start_error: Exception | None = None
    ) -> None:
        self._page = page or FakePage()
        self.start_error = start_error
        self.stopped = False

    async def start(self) -> FakePlaywright:
        if self.start_error is not None:
            raise self.start_error
        return self

    @property
    def chromium(self) -> FakeChromium:
        return FakeChromium(self._page)

    async def stop(self) -> None:
        self.stopped = True


def _make_session(
    *,
    goto_status: int = 200,
    evaluate_results: list[dict[str, Any]] | None = None,
    goto_delay: float = 0.0,
    evaluate_delay: float = 0.0,
    start_error: Exception | None = None,
    heartbeat_interval_s: float = 300.0,
    warm_timeout_s: float = WARM_TIMEOUT_S,
) -> tuple[UakinoSession, FakePage, FakePlaywright]:
    page = FakePage(
        goto_status=goto_status,
        evaluate_results=evaluate_results,
        goto_delay=goto_delay,
        evaluate_delay=evaluate_delay,
    )
    pw = FakePlaywright(page, start_error=start_error)

    async def _factory() -> FakePlaywright:
        # mirrors the real factory: launch errors surface out of the
        # playwright-factory call, not a separate `.start()` step
        if start_error is not None:
            raise start_error
        return pw

    session = UakinoSession(
        chromium="/fake/chromium",
        playwright_factory=_factory,
        heartbeat_interval_s=heartbeat_interval_s,
        warm_timeout_s=warm_timeout_s,
    )
    return session, page, pw


async def _until(predicate: Any, timeout: float = 1.0) -> None:
    """Spin until `predicate()` is truthy; fail after `timeout` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"condition not met within {timeout}s")
        await asyncio.sleep(0.005)


# --------------------------------------------------------------------------
# warm() -> ready_event
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_sets_ready_event() -> None:
    session, page, _ = _make_session()

    await session.warm()

    assert session.ready_event.is_set()
    # exactly one bootstrap (one goto), including the 4s settle wait
    assert page.goto_calls == 1
    assert page.wait_for_timeout_total == 4.0
    # the caller can await ready_event after warm returns
    await asyncio.wait_for(session.ready_event.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_warm_launch_failure_raises_session_error() -> None:
    session, _, _ = _make_session(start_error=SessionError("chromium exploded"))

    with pytest.raises(SessionError):
        await session.warm()

    assert not session.ready_event.is_set()


@pytest.mark.asyncio
async def test_warm_bootstrap_500_does_not_set_ready() -> None:
    session, _, _ = _make_session(goto_status=500)

    with pytest.raises(SessionError):
        await session.warm()

    assert not session.ready_event.is_set()


@pytest.mark.asyncio
async def test_warm_restart_clears_ready_and_does_not_retry() -> None:
    session, page, _ = _make_session()
    await session.warm()
    assert session.ready_event.is_set()

    # a second warm attempt clears the previously-set ready event at
    # `_start()` entry and, on a failed bootstrap, leaves it unset
    page.goto_status = 500
    with pytest.raises(SessionError):
        await session.warm()

    assert not session.ready_event.is_set()
    # warm does not retry: 1 bootstrap from the first warm + exactly 1 from
    # the failed second warm — no hidden re-launch
    assert page.goto_calls == 2


@pytest.mark.asyncio
async def test_warm_timeout_raises_timeout_error() -> None:
    # goto hangs forever; warm must self-timeout and leave ready unset
    session, _, pw = _make_session(goto_delay=999.0, warm_timeout_s=0.01)

    with pytest.raises(asyncio.TimeoutError):
        await session.warm()

    assert not session.ready_event.is_set()
    assert pw.stopped  # partial launch was torn down


@pytest.mark.asyncio
async def test_fetch_lazily_starts_and_sets_ready() -> None:
    session, page, _ = _make_session()

    status, text = await session.fetch("/filmy/")

    assert status == 200
    assert text == "<html>ok</html>"
    assert session.ready_event.is_set()
    assert page.goto_calls == 1


# --------------------------------------------------------------------------
# fetch() 403 -> one re-bootstrap
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_403_retries_once_then_returns_second_403() -> None:
    session, page, _ = _make_session(
        evaluate_results=[
            {"status": 403, "text": "Just a moment..."},
            {"status": 403, "text": "Just a moment..."},
        ]
    )
    await session.warm()

    status, _ = await session.fetch("/filmy/")

    assert status == 403
    # initial bootstrap + exactly one re-bootstrap
    assert page.goto_calls == 2
    assert page.evaluate_count == 2


@pytest.mark.asyncio
async def test_fetch_403_triggers_one_rebootstrap_then_succeeds() -> None:
    session, page, _ = _make_session(
        evaluate_results=[
            {"status": 403, "text": "Just a moment..."},
            {"status": 200, "text": "<html>ok</html>"},
        ]
    )
    await session.warm()

    status, text = await session.fetch("/filmy/")

    assert status == 200
    assert text == "<html>ok</html>"
    assert page.goto_calls == 2  # exactly one re-bootstrap
    assert page.evaluate_count == 2


# --------------------------------------------------------------------------
# heartbeat_loop()
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_records_ok_on_200() -> None:
    session, page, _ = _make_session(heartbeat_interval_s=0.01)
    await session.warm()
    records: list[bool] = []

    task = asyncio.create_task(session.heartbeat_loop(records.append))
    try:
        await _until(lambda: bool(records))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert records and all(records)
    # one /filmy/ fetch (one evaluate) per recorded tick
    assert page.evaluate_count == len(records)


@pytest.mark.asyncio
async def test_heartbeat_records_fail_after_rebootstrap() -> None:
    """A persistent 403 survives fetch's one re-bootstrap; the heartbeat
    must still record the verdict as failed."""
    session, page, _ = _make_session(
        heartbeat_interval_s=0.01,
        evaluate_results=[
            {"status": 403, "text": "Just a moment..."},
            {"status": 403, "text": "Just a moment..."},
        ],
    )
    await session.warm()
    records: list[bool] = []

    task = asyncio.create_task(session.heartbeat_loop(records.append))
    try:
        await _until(lambda: bool(records))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert records[0] is False
    # the one in-loop re-bootstrap did happen before the verdict
    assert page.goto_calls == 2


@pytest.mark.asyncio
async def test_heartbeat_records_ok_after_rebootstrap() -> None:
    """A 403 that resolves on the in-loop re-bootstrap still records ok."""
    session, page, _ = _make_session(
        heartbeat_interval_s=0.01,
        evaluate_results=[
            {"status": 403, "text": "Just a moment..."},
            {"status": 200, "text": "<html>ok</html>"},
        ],
    )
    await session.warm()
    records: list[bool] = []

    task = asyncio.create_task(session.heartbeat_loop(records.append))
    try:
        await _until(lambda: bool(records))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert records[0] is True
    assert page.goto_calls == 2


@pytest.mark.asyncio
async def test_heartbeat_records_fail_on_session_error() -> None:
    """An opaque response (status 0) raises SessionError from fetch; the
    heartbeat must swallow it and record ok=False instead of dying."""
    session, _, _ = _make_session(
        heartbeat_interval_s=0.01,
        evaluate_results=[{"status": 0, "text": ""}],
    )
    await session.warm()
    records: list[bool] = []

    task = asyncio.create_task(session.heartbeat_loop(records.append))
    try:
        await _until(lambda: bool(records))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert records[0] is False


# --------------------------------------------------------------------------
# lock serialization
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_fetches_serialize_evaluate() -> None:
    session, page, _ = _make_session(evaluate_delay=0.001)
    await session.warm()

    results = await asyncio.gather(*[session.fetch("/filmy/") for _ in range(10)])

    assert all(status == 200 for status, _ in results)
    assert page.evaluate_count == 10
    assert page.max_evaluate_in_flight == 1  # no two evaluates overlap


# --------------------------------------------------------------------------
# close()
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_cancels_heartbeat_and_clears_ready() -> None:
    session, _, pw = _make_session(heartbeat_interval_s=0.01)
    await session.warm()
    assert session.ready_event.is_set()
    records: list[bool] = []
    task = asyncio.create_task(session.heartbeat_loop(records.append))
    await _until(lambda: bool(records))

    await session.close()

    assert task.done()  # heartbeat was cancelled, not leaked
    assert not session.ready_event.is_set()
    assert pw.stopped
    # idempotent
    await session.close()


@pytest.mark.asyncio
async def test_close_idempotent_when_never_warmed() -> None:
    session, _, _ = _make_session()

    await session.close()
    await session.close()

    # never started -> nothing was launched, nothing to stop, still safe
    assert not session.ready_event.is_set()
