from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

BASE_URL = "https://uakino.best"

#: Bound on a single ``warm()`` attempt (issue #194). The observed warm
#: cost (playwright launch + ``goto`` 200 + 4s settle) is 5-10s on the dev
#: host; 15s leaves headroom for slower hosts. Distinct from the route-level
#: ``WARM_WAIT_S`` (issue #193) that bounds how long a request waits for
#: ``ready_event`` once the warm task has started.
WARM_TIMEOUT_S: float = 15.0

#: Heartbeat cadence (issue #194): one same-origin ``/filmy/`` probe per
#: interval. Constructor-injectable so tests use a short interval.
HEARTBEAT_INTERVAL_S: float = 300.0

#: Bounded drain for a cooperative heartbeat-task cancel in ``close()`` so
#: a mid-fetch tick cannot hang shutdown.
_CLOSE_DRAIN_S: float = 1.0

# uakino.best sits behind Cloudflare's managed challenge: plain HTTP
# clients (httpx, curl, Playwright's APIRequestContext) receive 403
# "Just a moment..." for everything except the initial document, while
# fetch() executed *inside* a loaded page succeeds. The challenge sets
# no cf_clearance cookie we could persist — it is silent and evaluated
# per request — so the only viable client is a live headless browser
# whose page runs same-origin fetches for us.
#
# This module owns that session. The provider goes through
# `UakinoSession.fetch(path, method, data)` for every uakino.best
# request; the stream CDN (ashdi.vip) is plain httpx and never touches
# this browser.

DEFAULT_CHROMIUM = os.environ.get("UAKINO_CHROMIUM", "/usr/bin/chromium")

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

_BOOTSTRAP_JS = """
async (args) => {
    const {path, method, data} = args;
    const opts = {method, headers: {}};
    if (method === "POST") {
        opts.headers["Content-Type"] = "application/x-www-form-urlencoded";
        opts.body = data;
    }
    if (method === "GET") {
        opts.headers["X-Requested-With"] = "XMLHttpRequest";
    }
    const r = await fetch(path, opts);
    const t = await r.text();
    return {status: r.status, text: t};
}
"""

# Binary variant: fetch the same same-origin URL and return the body
# base64-encoded (blob responses are not JSON-serializable). The chunked
# fromCharCode dance keeps btoa() happy on payloads > 64 KiB.
_BINARY_JS = """
async (args) => {
    const {path} = args;
    const opts = {method: "GET", headers: {"X-Requested-With": "XMLHttpRequest"}};
    const r = await fetch(path, opts);
    const buf = new Uint8Array(await r.arrayBuffer());
    let bin = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < buf.length; i += CHUNK) {
        bin += String.fromCharCode.apply(null, buf.subarray(i, i + CHUNK));
    }
    return {status: r.status, ctype: r.headers.get("Content-Type") || "", b64: btoa(bin)};
}
"""


class SessionError(RuntimeError):
    """Browser session could not load uakino.best."""


async def _default_playwright_factory() -> Any:
    """Zero-arg factory returning a started playwright instance.

    The playwright import stays here (not module scope) so a host that
    never touches uakino never pays for the optional dependency.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise SessionError(
            "playwright is not installed; pip install playwright "
            "(browser binary not needed, system chromium is used)"
        ) from e
    return await async_playwright().start()


class UakinoSessionProtocol(Protocol):
    async def fetch(
        self, path: str, method: str = "GET", data: str | None = None
    ) -> tuple[int, str]: ...
    async def close(self) -> None: ...
    async def warm(self) -> None: ...
    @property
    def ready_event(self) -> asyncio.Event: ...
    async def heartbeat_loop(self, record: Callable[[bool], None]) -> None: ...


class UakinoSession:
    """Headless Chromium session that serves same-origin fetches.

    `fetch` accepts a *relative* path (the page must stay on uakino.best
    so the request is same-origin). A 403 response triggers one
    re-bootstrap + retry in case the silent challenge rotated.

    Lifecycle (issue #194): a single per-instance ``asyncio.Lock``
    serializes ``_start``/``_bootstrap``/``fetch``/``fetch_binary``, so
    concurrent requests and heartbeat ticks can never interleave
    ``page.evaluate`` or double-bootstrap. ``ready_event`` is cleared when
    a session (re)starts and set once a bootstrap succeeds; ``warm()``
    performs that launch once under the lock with a bounded timeout, and
    ``close()`` cancels the heartbeat loop (if any) and tears the browser
    down, idempotently.
    """

    def __init__(
        self,
        chromium: str = DEFAULT_CHROMIUM,
        *,
        playwright_factory: Callable[[], Awaitable[Any]] | None = None,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        warm_timeout_s: float = WARM_TIMEOUT_S,
    ) -> None:
        self._chromium = chromium
        self._playwright_factory = playwright_factory or _default_playwright_factory
        self._heartbeat_interval_s = heartbeat_interval_s
        self._warm_timeout_s = warm_timeout_s
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._heartbeat_task: asyncio.Task[Any] | None = None

    @property
    def ready_event(self) -> asyncio.Event:
        """Set once the page has bootstrapped successfully."""
        return self._ready

    async def _close_resources(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = self._context = self._page = None

    async def _start(self) -> None:
        self._ready.clear()
        # Drop any partial session from a failed/aborted previous start so
        # a retry (or a lazy fetch) launches from a clean slate. Instances
        # are assigned incrementally so warm-timeout cleanup can reach them.
        await self._close_resources()
        pw = await self._playwright_factory()
        self._playwright = pw
        browser = await pw.chromium.launch(
            executable_path=self._chromium,
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        self._browser = browser
        context = await browser.new_context(user_agent=_UA, locale="uk-UA")
        self._context = context
        page = await context.new_page()
        self._page = page
        await self._bootstrap()

    async def _bootstrap(self) -> None:
        assert self._page is not None
        try:
            resp = await self._page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            raise SessionError(f"cannot load {BASE_URL}: {e}") from e
        await self._page.wait_for_timeout(4000)
        if resp is None or resp.status != 200:
            raise SessionError(f"{BASE_URL} answered {getattr(resp, 'status', '?')}")
        self._ready.set()

    async def _ensure_started(self) -> None:
        if self._page is None:
            await self._start()

    async def warm(self) -> None:
        """Launch + bootstrap once, bounded by the warm timeout.

        On success ``ready_event`` is set. On failure the exception
        propagates unchanged so the caller can classify it: a
        launch/bootstrap error is a ``SessionError`` (caller records
        ``warm_failed``); exceeding ``WARM_TIMEOUT_S`` is an
        ``asyncio.TimeoutError`` (caller records ``warm_timeout``). No
        retry — the caller decides.
        """
        async with self._lock:
            try:
                await asyncio.wait_for(self._start(), timeout=self._warm_timeout_s)
            except TimeoutError:
                # The warm was cut short mid-launch; release whatever
                # browser resources `_start` had created, then re-raise so
                # the caller still records `warm_timeout`.
                await self._close_resources()
                raise

    async def heartbeat_loop(self, record: Callable[[bool], None]) -> None:
        """Probe the live session every interval, calling ``record(ok)``.

        Runs until cancelled (by ``close()``). The interval comes from the
        constructor's ``heartbeat_interval_s`` (the #193 test seam). Each
        tick issues one same-origin ``fetch("/filmy/")``; the existing
        in-fetch 403 path retries once via re-bootstrap before the verdict
        is recorded. A non-200 status (after that retry) or a
        ``SessionError`` records ``ok=False``.
        """
        self._heartbeat_task = asyncio.current_task()
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            try:
                status, _ = await self.fetch("/filmy/")
                ok = status == 200
            except SessionError:
                ok = False
            record(ok)

    async def fetch(
        self, path: str, method: str = "GET", data: str | None = None
    ) -> tuple[int, str]:
        async with self._lock:
            return await self._fetch_unlocked(path, method, data)

    async def _fetch_unlocked(
        self, path: str, method: str, data: str | None
    ) -> tuple[int, str]:
        await self._ensure_started()
        assert self._page is not None
        for attempt in (1, 2):
            result = await self._page.evaluate(
                _BOOTSTRAP_JS,
                {"path": path, "method": method, "data": data or ""},
            )
            status = int(result.get("status", 0))
            if status == 403 and attempt == 1:
                await self._bootstrap()
                continue
            if status == 0:
                raise SessionError(f"fetch {method} {path} failed (opaque response)")
            return status, str(result.get("text", ""))
        return 403, ""

    async def fetch_binary(self, path: str) -> tuple[int, bytes, str]:
        """Same-origin binary fetch (images); returns (status, body, ctype).

        Mirrors ``fetch``'s 403-retry: the managed challenge may rotate
        between requests, so one re-bootstrap is attempted before giving
        up. Empty body on non-200 — the caller decides.
        """
        async with self._lock:
            return await self._fetch_binary_unlocked(path)

    async def _fetch_binary_unlocked(self, path: str) -> tuple[int, bytes, str]:
        await self._ensure_started()
        assert self._page is not None
        for attempt in (1, 2):
            result = await self._page.evaluate(_BINARY_JS, {"path": path})
            status = int(result.get("status", 0))
            if status == 403 and attempt == 1:
                await self._bootstrap()
                continue
            if status == 0:
                raise SessionError(f"fetch {path} failed (opaque response)")
            import base64

            body = base64.b64decode(str(result.get("b64", "")))
            ctype = str(result.get("ctype", ""))
            return status, body, ctype
        return 403, b"", ""

    async def close(self) -> None:
        """Cancel the heartbeat task and tear down the browser session.

        Idempotent, and safe to call from ``lifespan`` shutdown even when
        ``warm`` was never started. The heartbeat cancel is cooperative and
        bounded by a short drain so a mid-fetch tick cannot hang shutdown.
        At shutdown an in-flight request fetch may be interrupted by the
        browser teardown — acceptable, the process is exiting.
        """
        task = self._heartbeat_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=_CLOSE_DRAIN_S)
            except (TimeoutError, asyncio.CancelledError):
                pass
        self._heartbeat_task = None
        self._ready.clear()
        await self._close_resources()


_session: UakinoSession | None = None


def get_session() -> UakinoSession:
    """Process-wide default session (lazy)."""
    global _session
    if _session is None:
        _session = UakinoSession()
    return _session
