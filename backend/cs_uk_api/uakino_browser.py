from __future__ import annotations

import os
from typing import Any, Protocol

BASE_URL = "https://uakino.best"

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


class SessionError(RuntimeError):
    """Browser session could not load uakino.best."""


class UakinoSessionProtocol(Protocol):
    async def fetch(
        self, path: str, method: str = "GET", data: str | None = None
    ) -> tuple[int, str]: ...

    async def close(self) -> None: ...


class UakinoSession:
    """Headless Chromium session that serves same-origin fetches.

    Lazily launches the browser on first fetch. `fetch` accepts a
    *relative* path (the page must stay on uakino.best so the request
    is same-origin). A 403 response triggers one re-bootstrap + retry
    in case the silent challenge rotated.
    """

    def __init__(self, chromium: str = DEFAULT_CHROMIUM) -> None:
        self._chromium = chromium
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    async def _start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise SessionError(
                "playwright is not installed; pip install playwright "
                "(browser binary not needed, system chromium is used)"
            ) from e

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            executable_path=self._chromium,
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        context = await browser.new_context(user_agent=_UA, locale="uk-UA")
        page = await context.new_page()
        self._playwright, self._browser, self._context, self._page = (
            pw,
            browser,
            context,
            page,
        )
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

    async def _ensure_started(self) -> None:
        if self._page is None:
            await self._start()

    async def fetch(
        self, path: str, method: str = "GET", data: str | None = None
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

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = self._context = self._page = None


_session: UakinoSession | None = None


def get_session() -> UakinoSession:
    """Process-wide default session (lazy)."""
    global _session
    if _session is None:
        _session = UakinoSession()
    return _session
