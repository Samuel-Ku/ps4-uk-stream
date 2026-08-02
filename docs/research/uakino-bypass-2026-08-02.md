# Uakino personal-use bypass — cf_clearance refresher + new-theme stream extraction (2026-08-02)

Ticket: [Uakino personal-use adaptation — cf_clearance refresher + new-theme stream extraction (#51)](https://github.com/Samuel-Ku/ps4-uk-stream/issues/51).
Companion to [Uakino upstream 2026-08-02](uakino-upstream-2026-08-02.md) and [Uakino reachability 2026-08-02](uakino-reachability-2026-08-02.md). All probes pinned 2026-08-02.

## Question

Two-part spec: (1) a cf_clearance persistence layer + refresher script so
the backend can reuse a validated cookie for plain HTTP requests; (2) a
rewrite of the stream-link extraction for the current DLE theme. For
personal, non-commercial use only.

## Method

- Probes via headless Chromium (system `/usr/bin/chromium`, Chrome 138
  Linux UA, Playwright Python) with request tracing, plus plain `httpx`
  clients, against `https://uakino.best`.
- Reference: upstream Kotlin adapter (`UakinoProvider.kt`, sparse clone,
  `mainUrl = uakino.best`, HEAD of CakesTwix master).
- Live targets: search POST `/index.php`, content pages, ajax
  `/engine/ajax/playlists.php`, and the CDN pages referenced by
  `li[data-file]` (`ashdi.vip/vod/{id}`).

## Findings

1. **There is no cf_clearance cookie to persist.** After a successful
   navigation the browser context holds only `PHPSESSID` + analytics
   cookies. Cloudflare's managed challenge here is *silent*: it is
   evaluated per request and never issues a clearance cookie, so the
   ticket's Task 1 as specified (persist the cookie, reuse via httpx)
   is not implementable — verified twice: `httpx` with the full browser
   cookie jar + identical UA still gets 403 "Just a moment…", and even
   Playwright's `APIRequestContext` (same context cookies) gets 403.

2. **Only in-page `fetch()` passes.** `page.evaluate`-executed
   same-origin `fetch()` against `/index.php` (search POST) and
   `/engine/ajax/playlists.php` (playlists) returns 200 consistently,
   while every API-level client gets 403. The discriminator is the JS
   page context, not UA and not cookies (all three were identical in
   the failing probes). Root path `/` itself is only served to
   navigations, not to `fetch()` ("Failed to fetch"), so probes must
   target real endpoints.

3. **The real stream chain on the new theme** (all 200, verified live):
   content page → `div.playlists-ajax[data-news_id]` →
   `/engine/ajax/playlists.php?news_id={id}&xfield=playlist&time={ts}`
   (JSON `{success, response}`) → `div.playlists-videos li[data-file]`:
   - movie: one `li` per voice (`data-voice="Postmodern"`,
     `data-file="https://ashdi.vip/vod/89434"`; `DniproFilm` →
     `193414`), no episode text;
   - series: voice-group header `li` (`data-id="0_0"`, text
     "Yaniam (1-4)") followed by episode `li` whose text is
     "Серія N" (`data-file="//ashdi.vip/vod/273102"` … `274994`,
     protocol-relative — must be prefixed with `https:`).
   `li.voice_crating` is a stats stub, skip it.

4. **The CDN is Cloudflare-free.** `https://ashdi.vip/vod/{id}` opens
   with plain `httpx` (desktop UA + `Referer: https://uakino.best/`):
   200, ~20 KB. The page embeds the playlist URL as
   `file:'https://ashdi.vip/video02/1/films/dune._part_one_2021_…/hls/Da+Xjn6RkuZVhAb3/index.m3u8'`
   — the old-theme `file\s*:\s*["']…["']` regex still matches (single
   quotes). The master playlist itself is fetchable (200) with
   `Referer: https://ashdi.vip/` and yields 1080p/720p variants. So the
   dead part was only the *front* (uakino.best gate + YouTube
   `iframe#pre`), never the parser pattern itself.

5. **Search works only from the page context.** POST `/index.php`
   (`do=search&subaction=search&story=…`) from inside the page returns
   200 with new-theme cards `div.movie-item.short-item`; card hrefs
   carry a section/genre segment before the id
   (`/filmy/genre-action/12567-dyuna.html`,
   `/seriesss/drama_series/24872-duna-proroctvo-1-sezon.html`),
   sometimes without genre (`/seriesss/10458-dyuna-1-sezon.html`).
   Results also contain non-playable sections (`news`, `franchise`,
   `anonsi`) — filtered out. Genre-less content URLs
   (`/{section}/{id}-{slug}.html`) verified 200 for both film and
   series. Pagination is `/filmy/`, `/filmy/page/2/` … (`/filmy/page/`
   without a number is 404); `has_next` = any link `/page/N/` with
   `N > current`.

6. **Upstream search typing is broken** (`newMovieSearchResponse` for
   everything); we type by section/genre segment
   (`seriesss`/`*_series`/`*series` → series) and by structure at
   `content()` (presence of "Серія N" items → series).

## Architecture (as implemented)

- `cs_uk_api/uakino_browser.py` — `UakinoSession`: lazily launches
  headless Chromium (`UAKINO_CHROMIUM` env override), boots on
  `https://uakino.best`, serves every uakino.best request as an
  in-page `fetch()`; one re-bootstrap + retry on 403. The provider
  never touches uakino.best with httpx.
- `cs_uk_api/providers/uakino.py` — rewrite: POST search, card parse +
  junk filter, content = content page + playlists (structural
  movie/series detection), stream = playlists → `ashdi.vip/vod/{id}`
  via httpx → `file:` regex → `StreamResponse(m3u8, Referer ashdi.vip)`.
  External ids: `<section>:<id>-<slug>` (legacy `film|serial-…`
  accepted for cache continuity); episode ids `<news_id>:eN`;
  movies accept `:__movie__` suffix (ps4 client convention).
- `cs_uk_api/scripts/refresh_uakino.py` — the "refresher": warms the
  browser session and probes content + playlists endpoints (exit 0/1).
  Deviation from spec: there is nothing to persist, so this is a
  warm/verify tool, not a cookie refresher.
- `playwright` added to `requirements.txt`; system Chromium binary is
  used (no playwright browser download).

## Security & license note

Challenge evasion of a per-request JS gate is implemented strictly for
the ticket owner's personal, non-commercial use of content the site
already publishes for free. No credentials, no CAPTCHA-solving services,
no distributed access. Nothing in this work enables bypass for other
users or third parties.

## Verdict

- Task 1 (cf_clearance persistence) is **not implementable as
  specified** — replaced by a warm browser session that serves requests
  from the page context; documented and reflected in the ticket.
- Task 2 (stream extraction) is **implemented and verified
  end-to-end**: search → content (movie + series) → playlists →
  ashdi.vip → playable master m3u8 (live probe 200).

## Artifacts

- Trace session: `/tmp/opencode/trace_uakino.py`, `trace_{movie,series,search,playlists_movie,playlists_series,browse_p1,browse_p2}.{html,json}`, `trace_requests.log`.
- Fixtures (committed): `backend/cs_uk_api/tests/fixtures/uakino/` —
  `search_results.html`, `content_movie.html`, `content_series.html`,
  `playlists_{movie,series}.json`, `stream_ashdi_{movie,series}.html`,
  `browse_filmy.html`.
- Live smoke script: `/tmp/opencode/smoke_uakino.py`.
