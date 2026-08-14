# Uakino reachability — 2026-08-02

Research for wayfinder ticket #42 (feeds the decision ticket #45:
retire, adapt, or keep Uakino).

## Method

Live probes with `curl` against the exact conditions the backend uses:
User-Agent `cs-uk-api/0.1 (+https://github.com/)` (the shared client
default in `http_client.py`), no cookies, no JS, `follow_redirects`
disabled (backend policy), plus a one-shot run of the real adapter
through a local uvicorn (`/api/search?q=дюна&provider=uakino`).
Upstream Kotlin (`UakinoProvider.kt` from cloudstream-extensions-uk
master) consulted for the current upstream domain. All checks run
2026-08-02.

## Findings

### Domain state

| Domain | Result |
| ------ | ------ |
| `uakino.club` (adapter `BASE_URL`) | 301 → `uakino.best` (path preserved) on every request |
| `uakino.best` | live, serves real HTML to the adapter's UA — **no JS challenge on root/listing/search** |
| `uakino.me` | live alias, redirects to `uakino.best` |
| `uakino.net` | parked page (`ww80.uakino.net/?subid1=…`), not the site |
| `animeukr.info` (hardcoded anime section base) | **NXDOMAIN — dead** |
| `uakino.pp.ua`, `uakino.tv`, `uakino.zone`, `animeukr.org` | NXDOMAIN |

Upstream Kotlin has already migrated: `mainUrl = "https://uakino.best"`,
anime section = `$mainUrl/animeukr/page/` (same site, not `animeukr.info`).

### Path-level behaviour on `uakino.best` (plain httpx, no JS)

| Path | Status | What comes back |
| ---- | ------ | --------------- |
| `/` | 302 → `/ua/` → 200 | new theme homepage |
| `/search/?q=<q>` (adapter's method) | 200 | **zero query results** — generic site content only (0 hits for `дюна`) |
| `POST /index.php?do=search` + `story=` (DLE engine) | 200 | real results: 12 for `дюна` |
| `/filmy/`, `/animeukr/` (listings) | 200 | new DLE theme cards |
| `/filmy/{genre}/{id}-{slug}.html` (content) | **403** | Cloudflare Turnstile — *"Just a moment…"* (5.5 KB challenge page) |
| `/player/{id}.html` (stream) | **403** | Cloudflare Turnstile |

The challenge is path-level, not session/IP-level: root/listing/search
stay 200 while content and player paths 403 for the same client.

### Markup drift (why `BASE_URL` alone cannot fix the adapter)

`uakino.best` runs a completely new DLE theme. Every selector the
adapter depends on is gone:

| Adapter expectation (`uakino.py`) | `uakino.best` reality |
| --------------------------------- | --------------------- |
| `div.short-story` card | `div.movie-item.short-item` |
| `h3.short-title a` | `a.movie-title` |
| `div.short-img img`, `div.short-meta` | `img[src=/uploads/mini/…]`, `div.movie-desc` |
| `h1.hname`, `div.fulldesc`, `div.poster img` | absent (content pages unreachable anyway) |
| `select#translations`, `#series-list` | absent |
| external id `film-<slug>` / `serial-<slug>` from `/(film|serial)/…` | URLs are `/{filmy,seriesss,anonsi,…}/{genre}/{id}-{slug}.html` |

### Adapter behaviour through the real API

`GET /api/search?q=дюна&provider=uakino` on the live backend: **0
results, HTTP 200, no error**. Root cause: `BASE_URL = uakino.club` 301s to
`uakino.best`, the shared client has `follow_redirects=False`
(SSRF hardening, `http_client.py:19`), `safe_get` is not used by this
provider, so every request returns an empty 3xx body that the parser
turns into zero results.

## Implication for the adapter / decision inputs

1. Not fixable by a one-line `BASE_URL` swap: parser selectors, external-id
   grammar, and the search method (GET → POST) all changed upstream.
2. Even fully re-written for the new DLE theme, `content()` and `stream()`
   fetch pages behind **Cloudflare Turnstile** — a JS challenge that plain
   HTTP cannot clear. Listing and search are parseable; stream resolution
   is not (as of this research date).
3. `animeukr.info` hardcode is dead; the anime section lives at
   `uakino.best/animeukr/` (reachable, but its content pages challenge).

### Verdict for #45 (superseded 2026-08-09 by #193/#195)

At the time: recommend **keep with a plain-HTTP limitation** (register +
docs flag, gate stays red) or **retire** (delete provider/fixtures/tests;
Uakino is currently the documented "reference implementation", so
retiring means re-labelling the reference). **Adapt** was not viable
without a JS engine / Cloudflare bypass, which was out of v2 scope.

**Superseded:** the headless-Chromium session (issue #193) shipped
2026-08-09 resolves the Turnstile challenge with in-page `fetch()`;
uakino now serves live requests (search/content/stream → playable m3u8)
and is marked `ready` in the triage table. See the ADR-0002 / ADR-0004
amendments.

## Evidence

- Probe artifacts (ephemeral): `search.html` (GET /search, 0 hits),
  `postsearch.html` (POST do=search, 12 hits), `filmy.html` (new-theme
  listing), `content.html` / `player.html` (Turnstile challenge pages).
- Upstream Kotlin: `UakinoProvider.kt` — `mainUrl = "https://uakino.best"`.
- Backend run: `/api/search?q=дюна&provider=uakino` → 0 results, rc 0.
