# PS4 UK Stream Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a PS4 homebrew (an in-house client) that streams Ukrainian-dubbed content from **all 20 content providers** of cloudstream-extensions-uk via a Linux-side HTTP backend, with a Cloudstream-like catalog (section browsing + search), and the full manual test on a PS4 FW 11.00 + GoldHEN as the Definition of Done.

**Architecture (v2, after grilling):** FastAPI scraper service on a Linux host exposes REST+JSON over the local network; a thin in-house client adds a new "Каталог UA" menu entry with sections/search/result/detail screens that call the backend and hand the resolved URL to the existing MPV player. A shared `extractors/` layer resolves streams (iframe chains, PlayerJson/CDN, regex), since most of the 20 providers do not hand out direct URLs.

**Tech Stack:**
- Backend: Python 3.11+, FastAPI, uvicorn, httpx, beautifulsoup4, lxml, pydantic, cachetools; pytest + respx for tests; `mpv` CLI for the per-provider live gate.
- In-house client: C++17, libcross2d, existing `Browser` (libcurl) — **owned instance + single worker thread** (Browser is synchronous and not thread-safe), cJSON (vendored as a submodule).
- Build: CMake, OpenOrbis-PS4-Toolchain v0.5.2 (clang/lld, `create-fself`, `PkgTool.Core`), Docker image for reproducible builds.
- Target: PS4 firmware 11.00 with GoldHEN.

**Ground rules (agreed in grilling):**
1. All 20 content providers in scope; SyncPlugin is NOT a provider (watch-history sync).
2. Extractor layer mandatory; "JS-free" threshold; providers requiring a JS engine are marked `not portable` and excluded from the ready count.
3. Section browsing (`/api/sections`, `/api/browse`) in scope.
4. Anime per-episode translations in scope (`translations_level`).
5. Provider order: simple-iframe → playerjson → custom → HentaiUkr (last).
6. A provider is `ready` only when the live gate passes: search → content → stream → **plays in mpv** on Linux.
7. Fixtures are captured from live upstream HTML/JSON (capture-first); **no invented HTML**.
8. HentaiUkr enabled by default.

> **Superseded (2026-08-09):** this plan implemented the original in-house
> PS4 catalog client ("the client" below). The project moved fully to
> **Switchfin**, a Jellyfin client, against the backend's Jellyfin facade
> (spec #100); the current architecture is in
> [2026-08-05-jellyfin-adapter.md](../specs/2026-08-05-jellyfin-adapter.md).
> The tasks below are the historical record of the abandoned approach.

## Status snapshot (live)

The tasks below have been started or finished. This snapshot rolls up
the work done since the plan was written; tick boxes in the per-task
sections themselves are still the source of truth.

| Task | Scope | Status |
| ---- | ----- | ------ |
| 1 | Monorepo skeleton | DONE (commit `…` — see GitHub) |
| 2 | Backend skeleton + v2 Pydantic models | DONE |
| 3 | TTL cache, HTTP client | DONE |
| 4 | `BaseProvider` v2 + `ProviderError` | DONE |
| 5 | Extractor layer (`iframe`, `playerjson`, `regex`) | DONE |
| 6 | Uakino reference adapter | DONE (capture-first, respx tests) |
| 7 | FastAPI routes (search, content, stream, sections, browse, poster, providers) | DONE |
| 8 | Provider triage (`PROVIDERS.md`) | DONE — `docs/provider-triage.md` (updated 2026-08-02) |
| 9 | Group 1 — simple-iframe providers | DONE — ufdub, unimay, kinotron, cikavaideya, animeua, uaflix, kinovezha, bambooua, coaninet |
| 10 | Group 2 — playerjson providers | DONE — klontv, serialno, doramyworld |
| 11 | Group 3 — custom extractors / API clients | DONE — eneyida, uaserialspro, anitubeinua, simpsonsuatv, animeon |
| 12 | Group 4 — HentaiUkr | DONE |
| 13 | Live gate tooling + PROVIDERS.md finalization | DONE — `backend/scripts/live_gate.py` |
| 14 | In-house client — vendored cJSON + Json wrapper | DONE |
| 15 | In-house client — `CatalogApi` parsing skeleton (browser wire-up deferred) | DONE (browser wire-up landed in Task 18) |
| 16 | In-house client — `OnscreenKeyboard` | DONE (issue #15 bug fixed) |
| 17 | In-house client — config + main menu entry | DONE — «Пошук UA» menu entry (see status.md Task 18) |
| 18 | In-house client — full catalog screens | DONE — wired through shared `CatalogContext` (see status.md Task 18) |
| 19 | PS4 PKG build via OpenOrbis Docker | PARTIAL — scripts/Dockerfile verified (`bash -n`); final Docker build blocked on user hardware |
| 20 | On-console test and report | PARTIAL — `docs/switchfin-test-report.md` checklist updated; on-console run blocked on user hardware |

GitHub tracking is the live source of truth — see
[`docs/superpowers/specs/2026-08-01-ps4-uk-stream-design.md` § Tracking]
and the open issues labelled `status:needs-toolchain` /
`status:needs-hardware`.

---

## File Structure

```
ps4-uk-stream/
├── README.md
├── .gitignore
├── Dockerfile.ps4
├── docs/
│   └── switchfin-test-report.md
├── backend/
│   ├── pyproject.toml
│   ├── requirements.txt
│   ├── PROVIDERS.md                     ← per-provider status table (triage + gates)
│   ├── cs_uk_api/
│   │   ├── __init__.py
│   │   ├── main.py                      ← FastAPI app, all routes
│   │   ├── config.py
│   │   ├── models.py                    ← v2 models (types, sections, translations_level)
│   │   ├── cache.py
│   │   ├── http_client.py
│   │   ├── poster_proxy.py
│   │   ├── providers/
│   │   │   ├── __init__.py              ← registry + register()
│   │   │   ├── _registry.py
│   │   │   ├── base.py                  ← BaseProvider v2 (main, translations_level)
│   │   │   ├── uakino.py                ← reference adapter (real site)
│   │   │   ├── serialno.py  simpsonsua.py  ufdub.py  cikavaideya.py
│   │   │   ├── animeua.py  banderakino.py  eneyida.py  kinotron.py
│   │   │   ├── kinovezha.py  klontv.py  bambooua.py  doramyworld.py
│   │   │   ├── uaserialspro.py  unimay.py
│   │   │   ├── anitubeinua.py  coaninet.py  animeon.py  uaflix.py
│   │   │   └── hentaiukr.py
│   │   ├── extractors/
│   │   │   ├── __init__.py
│   │   │   ├── base.py                  ← BaseExtractor, ExtractResult
│   │   │   ├── iframe.py
│   │   │   ├── playerjson.py
│   │   │   ├── regex.py
│   │   │   └── anitube.py               ← Ashdi/Moon/csst ports (custom)
│   │   ├── scripts/
│   │   │   ├── smoke.sh
│   │   │   ├── gate.sh                  ← per-provider live gate (mpv)
│   │   │   └── triage.py                ← classify remaining providers
│   │   └── tests/
│   │       ├── conftest.py
│   │       ├── fixtures/<provider>/…    ← captured, sanitized
│   │       ├── test_models.py
│   │       ├── test_cache.py
│   │       ├── test_base_provider.py
│   │       ├── test_extractors.py
│   │       ├── test_uakino.py
│   │       ├── test_api.py
│   │       └── test_<provider>.py       ← one per provider
│   └── tools/
│       └── capture.py                   ← capture real HTML/JSON into fixtures
└── client/
    ├── external/cJSON/
    ├── src/catalog/
    │   ├── CatalogApi.{h,cpp}
    │   ├── Json.{h,cpp}
    │   ├── OnscreenKeyboard.{h,cpp}
    │   ├── ScreenSections.{h,cpp}
    │   ├── ScreenSearch.{h,cpp}
    │   ├── ScreenResults.{h,cpp}
    │   └── ScreenContent.{h,cpp}
    ├── tests/catalog/
    │   ├── CMakeLists.txt
    │   ├── test_json.cpp
    │   ├── test_keyboard.cpp            ← UTF-8 Cyrillic cases
    │   └── test_catalog_api.cpp         ← parsing + mocked Browser
    ├── scripts/build-ps4-docker.sh
    ├── scripts/ffmpeg-ps4.sh
    └── (existing client tree: src/, data/, libcross2d/, pscrap/, libsmb2/, …)
```

---

## Task 1: Initialize monorepo

Same as before — create the directory tree, `README.md`, `.gitignore`, `git init`.

- [ ] **Step 1:** `mkdir -p ps4-uk-stream/{backend,client,docs}` (already done — verify).
- [ ] **Step 2:** `README.md` — updated quick start (backend on Linux, `mpv` gate note, link to spec v2 + PROVIDERS.md).
- [ ] **Step 3:** `.gitignore` (same as before).
- [ ] **Step 4:** `git init` + commit `chore: initialize monorepo skeleton`.

---

## Task 2: Backend skeleton — package layout and v2 Pydantic models

**Files:** `pyproject.toml`, `requirements.txt`, `__init__.py`, `config.py`, `models.py`, `tests/conftest.py`, `tests/test_models.py`

- [ ] **Step 1:** `pyproject.toml` — same as before (fastapi, uvicorn, httpx, bs4, lxml, pydantic, cachetools; dev: pytest, pytest-asyncio, respx, ruff, mypy).
- [ ] **Step 2:** `requirements.txt` — same.
- [ ] **Step 3:** `config.py` — same as before; `providers` default: empty string → all registered.
- [ ] **Step 4:** Write `models.py` (v2 contract):

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MediaType = Literal["movie", "series", "anime", "cartoon", "dorama"]
StreamType = Literal["mp4", "m3u8", "hls", "dash"]
TranslationLevel = Literal["content", "episode"]


class SearchResult(BaseModel):
    id: str
    provider: str
    type: MediaType
    title: str
    year: int | None = None
    poster: str | None = None
    url: str


class SearchResponse(BaseModel):
    query: str = Field(min_length=1, max_length=80)
    results: list[SearchResult]


class Section(BaseModel):
    id: str
    title: str
    type: MediaType


class ProviderSections(BaseModel):
    provider: str
    name: str
    sections: list[Section]


class BrowseResponse(BaseModel):
    provider: str
    section: str
    page: int
    has_next: bool
    results: list[SearchResult]


class Translation(BaseModel):
    id: str
    label: str


class Episode(BaseModel):
    number: int
    id: str
    title: str
    translations: list[Translation] = Field(default_factory=list)


class Season(BaseModel):
    number: int
    episodes: list[Episode]


class ContentResponse(BaseModel):
    id: str
    type: MediaType
    title: str
    year: int | None = None
    description: str = ""
    poster: str | None = None
    translations_level: TranslationLevel = "content"
    translations: list[Translation]
    seasons: list[Season] | None = None


class StreamResponse(BaseModel):
    url: str
    type: StreamType = "mp4"
    headers: dict[str, str] = Field(default_factory=dict)


class ProviderInfo(BaseModel):
    id: str
    name: str
    types: list[MediaType]


class ErrorResponse(BaseModel):
    error: str
    message: str
```

- [ ] **Step 5:** `tests/test_models.py` — cover: search round-trip; empty query rejected; content with `translations_level="episode"` requires non-empty episode translations (test: an episode without translations under `episode` level fails validation); browse `has_next` round-trip; media types accepted.
- [ ] **Step 6:** venv + `pip install -e ".[dev]"` + `pytest` — all pass.
- [ ] **Step 7:** commit `feat(backend): package skeleton, config, v2 models`.

---

## Task 3: Backend — TTL cache, HTTP client

Identical to the old plan (`cache.py`, `http_client.py`, `test_cache.py`). No changes.

- [ ] Steps 1–6 of the old plan verbatim.
- [ ] Commit `feat(backend): TTL cache and shared httpx client`.

---

## Task 4: Backend — BaseProvider v2 and ProviderError

**Files:** `providers/__init__.py` (registry), `providers/base.py`, `tests/test_base_provider.py`

- [ ] **Step 1:** `providers/__init__.py`:

```python
from __future__ import annotations

from .base import BaseProvider, ProviderError

PROVIDERS: dict[str, BaseProvider] = {}


def register(provider: BaseProvider) -> None:
    PROVIDERS[provider.id] = provider


__all__ = ["BaseProvider", "ProviderError", "PROVIDERS", "register"]
```

- [ ] **Step 2:** `providers/base.py` (v2 — adds `main` and `translations_level`):

```python
from __future__ import annotations

import abc
from typing import Literal

import httpx

from ..models import (
    BrowseResponse,
    ContentResponse,
    MediaType,
    SearchResult,
    StreamResponse,
    TranslationLevel,
)


class ProviderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BaseProvider(abc.ABC):
    id: str
    name: str
    types: tuple[MediaType, ...]
    translations_level: TranslationLevel = "content"
    has_sections: bool = True

    @abc.abstractmethod
    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]: ...

    @abc.abstractmethod
    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse: ...

    @abc.abstractmethod
    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse: ...

    async def sections(self) -> list[Section]:  # noqa: F821
        return []

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> BrowseResponse:
        raise ProviderError("not_implemented", f"{self.id}: browse")
```

- [ ] **Step 3:** tests: `Dummy` provider raising `ProviderError`; `sections()` default empty; `browse()` raises `not_implemented`.
- [ ] **Step 4:** commit `feat(backend): BaseProvider v2 and ProviderError`.

---

## Task 5: Backend — extractor layer

**Files:** `extractors/__init__.py`, `extractors/base.py`, `extractors/iframe.py`, `extractors/playerjson.py`, `extractors/regex.py`, `tests/test_extractors.py`, fixtures `tests/fixtures/extractors/*.html`

The extractor layer is what makes "all 20 providers" realistic: most upstream sources do not expose direct media URLs; Cloudstream resolves them through extractor functions. We port the three dominant patterns.

- [ ] **Step 1:** `extractors/base.py`:

```python
from __future__ import annotations

import abc

import httpx

from ..models import StreamResponse


class BaseExtractor(abc.ABC):
    #: short id used in error messages and logs
    id: str

    @abc.abstractmethod
    async def extract(
        self, url: str, http: httpx.AsyncClient, referer: str | None = None
    ) -> StreamResponse | None: ...


class ExtractorError(Exception):
    pass
```

- [ ] **Step 2:** `extractors/iframe.py` — follow iframe chain with depth limit:

```python
from __future__ import annotations

from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..models import StreamResponse
from .base import BaseExtractor, ExtractorError

MAX_DEPTH = 3


class IframeExtractor(BaseExtractor):
    """Follow <iframe src> up to MAX_DEPTH, return the first direct media URL.

    Media detection: ends with .m3u8/.mp4/.webm or URL contains an HLS
    manifest marker (/hls/, /playlist.m3u8)."""

    id = "iframe"

    async def extract(self, url, http, referer=None):  # noqa: ANN001
        current = url
        base = referer or url
        for _ in range(MAX_DEPTH):
            headers = {"Referer": base} if base else {}
            try:
                resp = await http.get(current, headers=headers)
            except httpx.HTTPError as e:
                raise ExtractorError(f"iframe: {e}") from e
            if resp.status_code != 200:
                raise ExtractorError(f"iframe: status {resp.status_code}")
            if _looks_like_media(str(resp.url)):
                return StreamResponse(url=str(resp.url), type=_stream_type(str(resp.url)), headers=headers)
            soup = BeautifulSoup(resp.text, "lxml")
            iframe = soup.select_one("iframe")
            if iframe is None or not iframe.get("src"):
                return None
            next_url = str(iframe["src"])
            if next_url.startswith("/"):
                next_url = urljoin(str(resp.url), next_url)
            if next_url == current:
                return None
            base = str(resp.url)
            current = next_url
        return None


def _looks_like_media(u: str) -> bool:
    low = u.lower()
    return low.endswith((".m3u8", ".mp4", ".webm")) or "/hls/" in low or "playlist" in low


def _stream_type(u: str) -> str:
    return "m3u8" if u.lower().endswith(".m3u8") else ("mp4" if u.lower().endswith((".mp4", ".webm")) else "hls")
```

- [ ] **Step 3:** `extractors/playerjson.py` — the Cloudstream "PlayerJson" pattern: player page or CDN endpoint returns a JSON document with the stream; port of the shape used by AnimeUA/KinoVezha/KlonTV/Eneyida/Banderakino (players like `video.cdn...` / `player.videoseed...`). Because each CDN differs slightly, the extractor is **configurable**:

```python
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

import httpx

from ..models import StreamResponse
from .base import BaseExtractor, ExtractorError


class PlayerJsonExtractor(BaseExtractor):
    """Fetch a page/endpoint, find embedded JSON (window.__data / raw <script>),
    then walk `paths` to locate the media URL. Mirrors the Cloudstream
    PlayerJson handling used by AnimeUA, KinoVezha, KlonTV, Eneyida, Banderakino."""

    id = "playerjson"

    def __init__(self, paths: tuple[str, ...] = ("player", "sources", "file")):
        self.paths = paths

    async def extract(self, url, http, referer=None):  # noqa: ANN001
        headers = {"Referer": referer or url, "X-Requested-With": "XMLHttpRequest"}
        try:
            resp = await http.get(url, headers=headers)
        except httpx.HTTPError as e:
            raise ExtractorError(f"playerjson: {e}") from e
        if resp.status_code != 200:
            raise ExtractorError(f"playerjson: status {resp.status_code}")
        doc = _extract_json(resp.text)
        if doc is None:
            return None
        target = _walk(doc, self.paths)
        if not target:
            return None
        if isinstance(target, dict):
            url_str = target.get("url") or target.get("file") or target.get("src")
            hdr = {k: str(v) for k, v in (target.get("headers") or {}).items()}
        else:
            url_str = str(target)
            hdr = {}
        if not url_str or not isinstance(url_str, str):
            return None
        if url_str.startswith("/"):
            url_str = urljoin(str(resp.url), url_str)
        return StreamResponse(url=url_str, type=_guess(url_str), headers=hdr)


def _extract_json(text: str):
    # 1) whole body is JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2) window.__data = {...};
    for pat in (r"window\.__data\s*=\s*(\{.*?\});", r"var\s+data\s*=\s*(\{.*?\});"):
        m = re.search(pat, text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def _walk(node, paths):  # noqa: ANN001
    for i, key in enumerate(paths):
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and node:
            node = node[0]
        else:
            return None
    return node


def _guess(u: str) -> str:
    low = u.lower()
    if low.endswith(".m3u8"):
        return "m3u8"
    if low.endswith((".mp4", ".webm")):
        return "mp4"
    return "hls"
```

- [ ] **Step 4:** `extractors/regex.py` — Uakino-style `file: "..."` regex:

```python
from __future__ import annotations

import re
from urllib.parse import urljoin

import httpx

from ..models import StreamResponse
from .base import BaseExtractor, ExtractorError

FILE_RE = re.compile(r"""file\s*:\s*["']([^"']+?)["']""")


class RegexExtractor(BaseExtractor):
    """Pull the first `file: "url"` occurrence from a page (Uakino player)."""

    id = "regex"

    async def extract(self, url, http, referer=None):  # noqa: ANN001
        headers = {"Referer": referer or url, "X-Requested-With": "XMLHttpRequest"}
        try:
            resp = await http.get(url, headers=headers)
        except httpx.HTTPError as e:
            raise ExtractorError(f"regex: {e}") from e
        if resp.status_code != 200:
            raise ExtractorError(f"regex: status {resp.status_code}")
        m = FILE_RE.search(resp.text)
        if not m:
            return None
        target = m.group(1)
        if target.startswith("/"):
            target = urljoin(str(resp.url), target)
        return StreamResponse(url=target, type=_guess(target), headers={"Referer": referer or str(resp.url)})


def _guess(u: str) -> str:
    return "m3u8" if u.lower().endswith(".m3u8") else ("mp4" if u.lower().endswith((".mp4", ".webm")) else "hls")
```

- [ ] **Step 5:** `tests/test_extractors.py` + frozen fixtures: iframe chain (2 hops → .m3u8), iframe to direct .mp4, playerjson (window.__data), playerjson whole-body JSON, regex `file:` — each with a PASS case and a no-match → `None` case.
- [ ] **Step 6:** commit `feat(backend): extractor layer (iframe/playerjson/regex)`.

---

## Task 6: Backend — Uakino reference adapter (REAL site, capture-first)

**This replaces the fictional adapter of the old plan.** Facts verified from `UakinoProvider.kt` (upstream):
- Domain: `https://uakino.best` (not `.club`).
- Search: **POST** `$mainUrl/ua/` with form `do=search`, `subaction=search`, `story=<query with + for spaces>`, headers `Referer`, `X-Requested-With: XMLHttpRequest`, mobile UA.
- Result cards: `div.movie-item.short-item`, title `a.movie-title`, poster `img`, URL `a.movie-title`/`a.full-movie`.
- Detail page marker: `h1 span.solototle`, `div.film-poster`, `div.playlists-ajax`.
- Stream: player page contains `file: "..."` (regex) — via `RegexExtractor`; subtitle field exists (ignored).
- Sections (mainPage): `/filmy/page/`, `/seriesss/page/`, `/seriesss/doramy/page/`, `/cartoon/page/`, `/cartoon/cartoonseries/page/`, `/animeukr/page/`.

- [ ] **Step 1:** Write `tools/capture.py` — helper that fetches a URL with the provider headers, saves body to `tests/fixtures/<provider>/<name>.html`, and redacts nothing (fixtures are upstream HTML; they are committed frozen).
- [ ] **Step 2:** Capture real fixtures (run from a machine with network access):
  - `search.html` — POST search for "дюна" (use the POST variant of capture).
  - `content_movie.html` — a movie detail page.
  - `content_series.html` — a series detail page.
  - `player.html` — the player page containing `file:`.
  - `section_filmy.html` — first page of `/filmy/page/`.
- [ ] **Step 3:** Write tests against the captured bytes (`test_uakino.py`): search parses N cards (assert on real observed structure — titles start with the query, `type` derived from URL `/serial/` vs `/film/`); content parses translations (the real `playlists-ajax`/select structure — assert what the capture actually shows); stream resolves via `RegexExtractor`; sections lists 6 entries; browse parses `div.movie-item` cards + `has_next` from pagination marker (e.g. `div.pagi-nav` / `.pages` — verify against capture).
- [ ] **Step 4:** Implement `providers/uakino.py` (search POST, content, stream, sections, browse). Use `httpx` with headers; UA string from upstream (mobile Chrome).
- [ ] **Step 5:** Run unit tests (offline, frozen fixtures) — pass.
- [ ] **Step 6:** Live gate for the reference adapter:
  ```bash
  cd backend && . .venv/bin/activate
  bash cs_uk_api/scripts/gate.sh uakino        # see Task 13 for the script
  ```
  Expected: search finds results; content parses; stream returns a URL; `mpv --no-video <url> --frames=1` exits 0 (or plays ≥1 frame). If the site changed, update the capture + adapter and re-run.
- [ ] **Step 7:** Mark Uakino `✅` in `backend/PROVIDERS.md`.
- [ ] **Step 8:** commit `feat(backend): Uakino adapter against real site (capture-first)`.

---

## Task 7: Backend — FastAPI routes (search, content, stream, sections, browse, poster, providers)

**Files:** `poster_proxy.py`, `main.py`, `providers/_registry.py`, `tests/test_api.py`, `scripts/smoke.sh`

- [ ] **Step 1:** `poster_proxy.py` — as before.
- [ ] **Step 2:** `providers/_registry.py`:

```python
from __future__ import annotations

from . import register
from .uakino import UakinoProvider


def bootstrap() -> None:
    for p in (UakinoProvider(),):
        register(p)


bootstrap()
```

(Additional providers are registered as their tasks land.)
- [ ] **Step 3:** `main.py` — routes (as before) plus:

```python
@app.get("/api/sections")
async def sections() -> list[ProviderSections]:
    out = []
    for p in PROVIDERS.values():
        secs = await p.sections()
        if secs:
            out.append(ProviderSections(provider=p.id, name=p.name, sections=secs))
    return out


@app.get("/api/browse")
async def browse(
    provider: str = Query(...),
    section: str = Query(...),
    page: int = Query(1, ge=1),
) -> BrowseResponse:
    if provider not in PROVIDERS:
        raise HTTPException(400, detail=ErrorResponse(error="unknown_provider", message=provider).model_dump())
    cache_key = f"browse:{provider}:{section}:{page}"
    cached = _browse_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        resp = await PROVIDERS[provider].browse(section, page, get_client())
    except Exception as e:
        log.warning("browse failed provider=%s err=%s", provider, e)
        raise HTTPException(502, detail=ErrorResponse(error="upstream_unreachable", message=str(e)).model_dump()) from e
    _browse_cache.set(cache_key, resp)
    return resp
```

`_browse_cache = TtlCache(default_ttl_s=SETTINGS.cache_search_s)` (5 min).
- [ ] **Step 4:** `main.py` `/api/search` — also filter providers by `CS_UK_PROVIDERS` setting; unknown provider → 400 (as before).
- [ ] **Step 5:** Contract tests (`test_api.py`): `/api/providers` lists uakino; empty `q` → 422; unknown provider → 400; unknown content id → 404; `/api/sections` 200 with uakino sections; `/api/browse` unknown provider → 400; `/api/browse` unknown section → 400.
- [ ] **Step 6:** `smoke.sh` — adds `curl /api/sections`.
- [ ] **Step 7:** commit `feat(backend): FastAPI routes incl. sections/browse, poster proxy, smoke`.

---

## Task 8: Backend — provider triage (remaining 19)

**Files:** `scripts/triage.py`, `PROVIDERS.md`

Goal: classify each remaining provider into a family and produce the JS-free verdict, from the upstream Kotlin sources + a live probe.

- [ ] **Step 1:** `scripts/triage.py` — reads upstream repo file list (git clone depth 1 of `cloudstream-extensions-uk` into `backend/tools/upstream/`), prints per-provider: presence of `PlayerJson`/`Extractor` files, extractor class names, `app.get/post` call sites, `loadExtractor(` calls.
- [ ] **Step 2:** Run triage for the 19 providers; record in `PROVIDERS.md` the provisional family from the spec table (§6) with a ✓/✗ against the evidence.
- [ ] **Step 3:** For each provider, one live probe (rate-limited, 1 req/10 s) of the search endpoint to confirm the site is reachable and the search selector shape; record `reachable: yes/no`.
- [ ] **Step 4:** Classify `not portable` (JS-only player) or `broken upstream` (site down) — these get `⛔`/`⚠️` and are skipped by later tasks.
- [ ] **Step 5:** commit `docs(backend): provider triage table` (update PROVIDERS.md).

---

## Task 9: Group 1 — simple-iframe providers

**Providers (provisional):** Serialno, SimpsonsUATv, UFDub, CikavaIdeya. (Adjust per triage.)

**Per provider (template task, one commit each):**
- [ ] Capture fixtures: `search.html`, `content_movie.html`/`content_series.html`, `player.html`, `section.html` via `tools/capture.py`.
- [ ] Write `tests/test_<provider>.py` against captured bytes (search card structure, content translations/seasons — assert what the capture shows, including whether translations are per-episode for any series).
- [ ] Implement `providers/<provider>.py` using `IframeExtractor` (stream: player page → iframe chain); `sections`/`browse` if the site has section pages; `translations_level` from evidence.
- [ ] Register in `_registry.py`.
- [ ] Run unit tests offline → pass.
- [ ] Live gate: `bash cs_uk_api/scripts/gate.sh <provider>` → search → content → stream → mpv plays. Mark `✅`/`⚠️`/`⛔` in PROVIDERS.md.
- [ ] Commit per provider: `feat(backend): <Provider> adapter (group 1)`.

---

## Task 10: Group 2 — playerjson providers

**Providers (provisional):** AnimeUA, Banderakino, Eneyida, KinoTron, KinoVezha, KlonTV, BambooUA, DoramyWorld, UASerialsPro, Unimay.

- [ ] **Step 0 (shared):** for each provider, port its specific `PlayerJson` shape into a config: extract from the upstream `PlayerJson.kt` the exact JSON path/fields used (`player`, `sources`, `file`, `url`, `headers`, `subs`…) and instantiate `PlayerJsonExtractor(paths=...)` accordingly. Where the upstream uses a bespoke CDN (e.g. a tokenized `file:` inside a JS object), fall back to `RegexExtractor`.
- [ ] Per provider: capture fixtures → tests → adapter → register → live gate → status in PROVIDERS.md → commit (same template as Task 9).
- [ ] Anime providers (AnimeUA, BambooUA, Unimay): set `translations_level = "episode"` and populate `Episode.translations` from the upstream episode JSON (`episode.translations` in Cloudstream). Unit tests must assert episode-level translations survive round-trip.

---

## Task 11: Group 3 — custom extractors / API clients

**Providers (provisional):** Anitubeinua, Coaninet, AnimeON, UAFlix.

- [ ] **Anitubeinua:** port the three extractors (Ashdi/Moon/csst) into `extractors/anitube.py`, following the upstream logic (AJAX endpoints, token extraction, `videoConstructor`). Unit tests with captured player pages per extractor. If any extractor requires JS execution, mark that path `not portable` and keep the others.
- [ ] **Coaninet:** JSON API client (models: `AnimeItem`, `SeasonResponse`, `SeriesData`…) — port the endpoint calls; tests with captured JSON fixtures.
- [ ] **AnimeON:** port per upstream `AnimeInfoModel`/`PlayerEpisodes` flow; `translations_level="episode"`.
- [ ] **UAFlix:** triage verdict decides iframe vs playerjson; implement accordingly.
- [ ] Each: capture → tests → adapter → register → live gate → status → commit.

---

## Task 12: Group 4 — HentaiUkr (last)

- [ ] Same template (capture → tests → adapter → register → live gate → status → commit). No hiding flag (user decision).
- [ ] commit `feat(backend): HentaiUkr adapter (group 4)`.

---

## Task 13: Live gate tooling + PROVIDERS.md finalization

**Files:** `scripts/gate.sh`, `PROVIDERS.md`

- [ ] **Step 1:** `scripts/gate.sh <provider> [query]`:

```bash
#!/usr/bin/env bash
set -euo pipefail
PROVIDER="${1:?usage: gate.sh <provider> [query]}"
QUERY="${2:-Дюна}"
PORT="${PORT:-8002}"
. "$(dirname "$0")/../../.venv/bin/activate"
uvicorn cs_uk_api.main:app --port "$PORT" --host 127.0.0.1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
sleep 1
BASE="http://127.0.0.1:${PORT}"
RESULTS=$(curl -sS "$BASE/api/search?q=$QUERY&provider=$PROVIDER")
COUNT=$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])['results']))" "$RESULTS")
echo "search: $COUNT results"
if [ "$COUNT" = "0" ]; then echo "GATE FAIL: no results"; exit 1; fi
CID=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['results'][0]['id'])" "$RESULTS")
CONTENT=$(curl -sS "$BASE/api/content/$CID")
echo "content: $(python3 -c "import json,sys; print(json.loads(sys.argv[1])['title'])" "$CONTENT")"
STREAM=$(curl -sS "$BASE/api/stream/$CID")
URL=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['url'])" "$STREAM")
echo "stream: $URL"
timeout 30 mpv --no-video --frames=1 --no-config --msg-level=all=error "$URL" \
  && echo "GATE PASS" || { echo "GATE FAIL: mpv"; exit 1; }
```

- [ ] **Step 2:** Run `gate.sh` for every provider marked ready; fix fallout; update `PROVIDERS.md` to the final table (✅ ready / ⚠️ broken upstream / ⛔ not portable) with dates.
- [ ] **Step 3:** commit `feat(backend): live gate script + final PROVIDERS.md`.

---

## Task 14: In-house client — vendor cJSON + Json wrapper

Same as old plan Tasks 9–10 verbatim (`external/cJSON` submodule, `Json.{h,cpp}`, `tests/catalog/test_json.cpp`, CMake hook). No changes.

- [ ] Commit `feat(client): vendor cJSON, Json wrapper`.

---

## Task 15: In-house client — CatalogApi with honest Browser integration

**Files:** `src/catalog/CatalogApi.{h,cpp}`, `tests/catalog/test_catalog_api.cpp`

This task fixes the old plan's fiction: `Browser` (verified in `src/filer/Browser/Browser.hpp`) has **no** static `lastResponse()/lastError()`; it is a synchronous, single-handle class. Correct design:

- [ ] **Step 1:** `CatalogApi.h` — same structs as before plus:

```cpp
struct Section { std::string id, title, type; };
struct ProviderSections { std::string provider, name; std::vector<Section> sections; };
struct BrowseItem { std::vector<SearchItem> results; bool hasNext; };
struct Episode { int number; std::string id, title; std::vector<std::pair<std::string,std::string>> translations; };
struct Season { int number; std::vector<Episode> episodes; };
// ContentItem gains: std::string translationsLevel; // "content" | "episode"
```

Callbacks: `sectionsAsync`, `browseAsync`, `searchAsync`, `contentAsync`, `streamAsync`, `loadPoster`.

- [ ] **Step 2:** `CatalogApi.cpp` — worker-thread core:

```cpp
#include "filer/Browser/Browser.hpp"
#include "Json.h"
#include <deque>
#include <mutex>
#include <thread>
#include <condition_variable>

namespace cs {
namespace {

struct Job {
    std::function<void()> fn;
};

}  // namespace

class CatalogApi::Impl {
public:
    Impl(std::string baseUrl) : base_(std::move(baseUrl)) {
        worker_ = std::thread([this] { loop(); });
    }
    ~Impl() { { std::lock_guard<std::mutex> lk(m_); done_ = true; } cv_.notify_one(); worker_.join(); }

    template <typename F> void post(F f) {
        { std::lock_guard<std::mutex> lk(m_); queue_.push_back(Job{std::move(f)}); }
        cv_.notify_one();
    }

    // All HTTP goes through this single worker thread; Browser is not
    // thread-safe (one CURL handle, one response buffer).
    std::string httpGet(const std::string &url) {
        browser_.open_novisit(url, 12);
        if (browser_.error()) return {};
        return browser_.response();
    }
    std::string httpPost(const std::string &url, const std::string &data) {
        browser_.open(url, data, 12);
        if (browser_.error()) return {};
        return browser_.response();
    }

private:
    void loop() {
        for (;;) {
            Job job;
            { std::unique_lock<std::mutex> lk(m_);
              cv_.wait(lk, [this] { return done_ || !queue_.empty(); });
              if (done_ && queue_.empty()) return;
              job = std::move(queue_.front()); queue_.pop_front(); }
            job.fn();
        }
    }

    std::string base_;
    Browser browser_;
    std::deque<Job> queue_;
    std::mutex m_;
    std::condition_variable cv_;
    bool done_ = false;
    std::thread worker_;
};

CatalogApi::CatalogApi(std::string baseUrl)
    : baseUrl_(std::move(baseUrl)), impl_(new Impl(baseUrl_)) {}
CatalogApi::~CatalogApi() = default;
```

- [ ] **Step 3:** Async methods — each posts a job that performs the HTTP call and invokes the callback **on the worker thread** (UI code marshals to its own thread via `main->getThread()`/`addAction` when needed — see Screen tasks):

```cpp
void CatalogApi::searchAsync(const std::string &query, SearchCb cb) {
    impl_->post([this, query, cb = std::move(cb)]() {
        auto body = impl_->httpGet(baseUrl_ + "/api/search?q=" + urlEncode(query));
        if (body.empty()) { cb(false, {}, "error_network"); return; }
        cb(true, parseSearch(body), "");
    });
}
// contentAsync / streamAsync / sectionsAsync / browseAsync — same pattern.
```

- [ ] **Step 4:** `loadPoster(url, PosterCb)` — worker thread fetches bytes (Browser with `write_bytes` to a temp file, or a raw curl handle guarded by the same mutex — prefer a second raw CURL handle used ONLY on the worker thread), then the callback receives decoded bytes; an in-memory LRU (~50 entries) caches them.

**Decision (implementation detail, already made):** poster bytes are fetched on the same worker thread with a dedicated `CURL*` handle guarded by the queue (never concurrent with `Browser` calls), so the existing `Browser` file-writing path is not touched.
- [ ] **Step 5:** Parsing — `parseSearch`/`parseContent`/`parseStream`/`parseSections`/`parseBrowse` via `Json.{h,cpp}`; `parseContent` must read `translations_level` and per-episode `translations`.
- [ ] **Step 6:** Tests: `test_catalog_api.cpp` — parsing tests (frozen JSON, incl. episode-level translations) as before; **no network in unit tests**.
- [ ] **Step 7:** Linux integration (manual, optional): run the backend, run a small CLI harness against the real API.
- [ ] **Step 8:** commit `feat(client): CatalogApi with worker-thread Browser integration`.

---

## Task 16: In-house client — OnscreenKeyboard (UTF-8 correct)

**Files:** `src/catalog/OnscreenKeyboard.{h,cpp}`, `tests/catalog/test_keyboard.cpp`

Fixes the old plan's dead-Cyrillic bug (`label.size() == 1` ASCII-only append).

- [ ] **Step 1:** Header same as before (grid + `append`/`backspace`/`clear`/`isAction`).
- [ ] **Step 2:** Layout: 4 rows of Cyrillic + digits, bottom row actions (as before).
- [ ] **Step 3:** Implementation — decode the label's first UTF-8 codepoint properly:

```cpp
#include "OnscreenKeyboard.h"
#include <cassert>

namespace cs {

namespace {
const char *kLayout[kRows][kCols] = {
    {"А","Б","В","Г","Ґ","Д","Е","Є","Ж","З"},
    {"И","І","Ї","Й","К","Л","М","Н","О","П"},
    {"Р","С","Т","У","Ф","Х","Ц","Ч","Ш","Щ"},
    {"Ь","Ю","Я","0","1","2","3","4","5","6"},
    {"7","8","9","space","back","clear","done","","",""}
};

char32_t decodeUtf8(const std::string &s) {
    // returns U+FFFD for invalid/empty input
    if (s.empty()) return 0xFFFD;
    unsigned char c0 = (unsigned char)s[0];
    if (c0 < 0x80) return (char32_t)c0;
    int len = 0; char32_t cp = 0;
    if ((c0 & 0xE0) == 0xC0) { len = 2; cp = c0 & 0x1F; }
    else if ((c0 & 0xF0) == 0xE0) { len = 3; cp = c0 & 0x0F; }
    else if ((c0 & 0xF8) == 0xF0) { len = 4; cp = c0 & 0x07; }
    else return 0xFFFD;
    if ((int)s.size() < len) return 0xFFFD;
    for (int i = 1; i < len; ++i) {
        if (((unsigned char)s[i] & 0xC0) != 0x80) return 0xFFFD;
        cp = (cp << 6) | ((unsigned char)s[i] & 0x3F);
    }
    return cp;
}

void appendUtf8(std::string &out, char32_t cp) {
    char buf[4];
    int n;
    if (cp < 0x80) { buf[0] = (char)cp; n = 1; }
    else if (cp < 0x800) { buf[0] = (char)(0xC0 | (cp >> 6)); buf[1] = (char)(0x80 | (cp & 0x3F)); n = 2; }
    else if (cp < 0x10000) { buf[0] = (char)(0xE0 | (cp >> 12)); buf[1] = (char)(0x80 | ((cp >> 6) & 0x3F)); buf[2] = (char)(0x80 | (cp & 0x3F)); n = 3; }
    else { buf[0] = (char)(0xF0 | (cp >> 18)); buf[1] = (char)(0x80 | ((cp >> 12) & 0x3F)); buf[2] = (char)(0x80 | ((cp >> 6) & 0x3F)); buf[3] = (char)(0x80 | (cp & 0x3F)); n = 4; }
    out.append(buf, n);
}

}  // namespace

void OnscreenKeyboard::append(const std::string &label) {
    appendUtf8(text_, decodeUtf8(label));
}

void OnscreenKeyboard::backspace() {
    if (text_.empty()) return;
    size_t i = text_.size() - 1;
    while (i > 0 && ((unsigned char)text_[i] & 0xC0) == 0x80) --i;
    text_.erase(i);
}

}  // namespace cs
```

- [ ] **Step 4:** Tests (`test_keyboard.cpp`): append "А" (2-byte), "Є" (2-byte), "Ґ" (2-byte), "Ж" — text matches; backspace removes exactly one codepoint; space/back/clear/done are actions; digits append.
- [ ] **Step 5:** `ScreenSearch` uses `kb_.append(label)` (the label string), never `label[0]`.
- [ ] **Step 6:** commit `feat(client): UTF-8-correct OnscreenKeyboard`.

---

## Task 17: In-house client — config, menu entry, screen scaffolding

**Files:** `src/client_config.{h,cpp}`, `src/menus/menu_main.cpp`, `src/main.{h,cpp}`, `data/common/client.cfg`, placeholder screens

Verified integration points (read from the actual tree):
- `src/client_config.h` — add `#define OPT_CATALOG_URL "CATALOG_URL"` next to the other `OPT_*` string macros.
- `src/client_config.cpp` — after `addOption({OPT_NETWORK, "http://samples.ffmpeg.org/"});` add `addOption({OPT_CATALOG_URL, "http://192.168.2.223:8000"});`.
- `src/main.cpp` (items block, ~line 107): `items.emplace_back("Каталог UA", "catalog.png", MenuItem::Position::Top);` — also handle the missing icon (fall back to `network.png` if no catalog.png asset yet).
- `src/main.h` — extend `enum class MenuType { ... }` with `Catalog`.
- `src/menus/menu_main.cpp` `MenuMain::onOptionSelection` — add `else if (item->name == "Каталог UA") { setVisibility(Visibility::Hidden, true); main->show(Main::MenuType::Catalog); }`.
- `src/main.cpp` `Main::show(...)` — branch: `case MenuType::Catalog: push(new ScreenSections(this, catalogApi)); break;` where `catalogApi` is created once in `Main` constructor from `OPT_CATALOG_URL`.
- Placeholder `ScreenSections.h/cpp` (compiles, blank scene) so the build links.
- Build Linux: `cmake -B build -DCMAKE_BUILD_TYPE=Debug -DPLATFORM_LINUX=ON && cmake --build build -- -j` → PASS.
- Commit `feat(client): config option, Каталог UA menu entry, catalogApi wiring`.

---

## Task 18: In-house client — full catalog screens

**Files:** `ScreenSections.{h,cpp}`, `ScreenSearch.{h,cpp}` (full), `ScreenResults.{h,cpp}` (full), `ScreenContent.{h,cpp}` (full)

- [ ] **Step 1:** `ScreenSections` — two-column list (providers ←, sections →); `sectionsAsync` → populate; enter → `browseAsync(provider, section, 1)` → `ScreenResults` in browse mode; R1/L1 page navigation; Square back.
- [ ] **Step 2:** `ScreenSearch` — as before, but: Cyrillic input via `kb_.append(label)`; busy spinner; results → `ScreenResults` in search mode.
- [ ] **Step 3:** `ScreenResults` — full rendering: rows of poster + title + year + type badge (Фільм/Серіал/Аніме/Мультфільм/Дорама); `loadPoster` wired with lazy loading (poster request only when row becomes visible); empty state "Нічого не знайдено" + "Назад"; pagination in browse mode.
- [ ] **Step 4:** `ScreenContent` — poster, description, translations; season strip + episode list; if `translations_level == "episode"`, translation chooser binds to the focused episode (episode's own `translations`); Fire1 → `streamAsync(id, translationId)` → hand `url`/`headers` to the existing `Player` (see old plan's handoff note: the exact call site in `main.cpp` is `Player::load` — keep the resolved URL log line as fallback until confirmed).
- [ ] **Step 5:** Callbacks arrive on the CatalogApi worker thread — marshal UI updates via `c2d::Main::addAction`/the existing event mechanism; never touch c2d widgets off the UI thread.
- [ ] **Step 6:** Build Linux → PASS; manual Linux run against live backend (optional).
- [ ] **Step 7:** commit `feat(client): Sections/Search/Results/Content screens with posters`.

---

## Task 19: PS4 PKG build via OpenOrbis Docker

Same as old plan Task 16 verbatim (`Dockerfile.ps4`, `scripts/build-ps4-docker.sh`, `scripts/ffmpeg-ps4.sh`, artifact validation: PKG magic `\x7FCNT` — `7f 43 4e 54`, per `docs/switchfin-test-report.md`; `readoelf` NID table, `auth_id 0x3800000000000011`).

- [ ] Commit `build(ps4): OpenOrbis Docker pipeline and PS4 ffmpeg.sh`.

---

## Task 20: On-console test and report

Same as old plan Task 17, checklist updated for the v2 scope:

- [ ] App launches, main menu visible.
- [ ] "Каталог UA" entry present and focusable.
- [ ] Sections screen lists providers + sections; browse returns posters.
- [ ] On-screen keyboard accepts Cyrillic input and submits a search.
- [ ] Results render with posters.
- [ ] A movie plays through MPV without sync glitches.
- [ ] A series: choose season, choose episode, play several in a row.
- [ ] An anime: episode-level translation chooser works (choose dub/sub, plays).
- [ ] At least 3 different providers verified on the console.
- [ ] Write `docs/switchfin-test-report.md` (PASS/FAIL required).

- [ ] Commit `docs: PS4 test report (FW 11.00 + GoldHEN)`.

---

## Spec Coverage Self-Review (v2)

- **Scope (spec §1):** Tasks 1, 6–13 (all 20 providers), Tasks 14–18 (catalog UI), Task 20 (console DoD); HentaiUkr = Task 12; SyncPlugin excluded.
- **Extractor layer (spec §2.1):** Task 5; per-provider ports in Tasks 9–12.
- **Sections/browse (spec §3.3–3.4):** Task 4 (`sections`/`browse` on BaseProvider), Task 7 (routes), Task 18 (`ScreenSections`).
- **Episode-level translations (spec §3.6):** Task 2 (models), Task 10 (anime providers), Task 15 (parse), Task 18 (UI chooser).
- **Provider families/order (spec §6):** Tasks 8–12; live gate = Task 13; statuses in `PROVIDERS.md`.
- **Capture-first fixtures:** `tools/capture.py` (Task 6 Step 1) used by every provider task.
- **In-house client honest Browser integration (spec §4.7):** Task 15 (worker thread, no invented statics).
- **UTF-8 keyboard (spec §4.6):** Task 16.
- **Poster loading (spec §4.4):** Task 15 (`loadPoster`) + Task 18 (lazy rows).
- **PS4 build (spec §7.2):** Task 19; **console test:** Task 20.

No placeholders remain except where explicitly gated by triage/live-capture (selectors for providers not yet captured — by design; every such task begins with its capture step).
