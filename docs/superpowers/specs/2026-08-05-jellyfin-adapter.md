# Switchfin Jellyfin Adapter — Design Spec

**Date:** 2026-08-05 (grilling session)
**Status:** Approved
**Base:** [v3 design](2026-08-02-ps4-uk-stream-v3-design.md) + [CONTEXT.md](../../../CONTEXT.md)
**Author:** Grilling session with the user

## Problem Statement

The user can already run **Switchfin**, a Jellyfin client for PS4 (FW 11.00 + GoldHEN), on the console without problems. But the entire backend of this project (`cs_uk_api`) speaks the project's own `/api/*` JSON contract — Switchfin cannot talk to it because it only speaks the **Jellyfin REST API**. Without an adapter, using Switchfin would require a full Jellyfin server, which is out of proportion for a LAN-scraping catalog.

The user wants to know: can the existing backend, which already works for the pPlay client, be reused as a Switchfin backend with minimal work — and if so, which parts are JSON mapping, which are true proxying, and which need emulation?

## Solution

Add a **Jellyfin facade** to the same FastAPI process (`cs_uk_api`). It emulates the minimal Jellyfin REST surface that Switchfin's navigation and playback require, reusing the existing provider/scraper/merge logic without rewriting any parsing. Playback uses a **conditional stream handler**: emit a 302 redirect when the upstream stream needs no special headers, and act as a byte proxy (with `Range` support and HLS segment rewriting) when it does.

Switchfin replaces both the pPlay catalog UI and the pPlay player. The pPlay fork is no longer on the critical path for catalog delivery; it is retained in the repo as a Linux reference/test surface.

## User Stories

1. As a PS4 owner, I want to enter my LAN backend's address as a Jellyfin server in Switchfin, so that I can browse the catalog without building a Jellyfin server.
2. As a PS4 owner, I want to authenticate with any credentials against the backend, so that the Jellyfin handshake completes without me managing real accounts.
3. As a PS4 owner, I want the backend to expose media libraries ("Views"), so that Switchfin shows a browseable library list.
4. As a PS4 owner, I want the media libraries to mirror the home rows («Новинки», «Популярні зараз», «Фільми», «Серіали», «Аніме», «Мультфільми», «Дорами»), so that the catalog is organized the way the project already merges it.
5. As a PS4 owner, I want movies to appear as flat items, so that I can open a movie and play it directly.
6. As a PS4 owner, I want series to appear as Series → Season → Episode, so that I can navigate a show's episodes.
7. As a PS4 owner, I want episodes to resolve to the correct stream through their existing episode ids, so that playback works without an extra ID-mapping layer.
8. As a PS4 owner, I want each movie/episode to be a single item with the server choosing the default translation, so that the catalog has no duplicate cards (v1; no dubbing chooser).
9. As a PS4 owner, I want `PlaybackInfo` to return a minimal direct-stream media source, so that Switchfin starts playback against the Jellyfin stream endpoint.
10. As a PS4 owner, I want `/Videos/{id}/stream` to redirect straight to the CDN when no headers are needed, so that headerless streams bypass the backend and keep LAN bandwidth low.
11. As a PS4 owner, I want `/Videos/{id}/stream` to proxy through the backend (with Range + HLS segment rewriting) when the CDN requires Referer/User-Agent headers, so that protected CDNs still play.
12. As a PS4 owner, I want posters to be served through the existing poster proxy, so that Switchfin shows box art without a second image pipeline.
13. As a PS4 owner, I want play sessions (Playing/Progress/Stopped) accepted as no-ops, so that playback reporting doesn't error out (and resume/history stay out of scope).
14. As a developer, I want the Jellyfin facade to live in the same process as the existing API, so that it reuses `httpx` client, caches, health tracker, and provider registry without a new deployable.
15. As a developer, I want cross-provider merged items to keep a single stable id, so that a dead provider doesn't break an item card that another provider also carries.
16. As a developer, I want the stream decision to be conditional on `StreamResponse.headers`, so that we neither proxy everything nor redirect streams that need headers.
17. As a developer, I want capture-first fixtures from a real Jellyfin client before finalizing the endpoint set, so that the "minimal 10-endpoint list" is verified against real client behavior instead of guessed.
18. As a developer, I want contract tests at the HTTP surface, so that the facade's endpoints are pinned to the Jellyfin shapes Switchfin actually uses.

## Implementation Decisions

### D1. Architecture: Jellyfin facade inside cs_uk_api

A new module area (e.g. `jellyfin/`) inside the existing `cs_uk_api` FastAPI application exposes Jellyfin routes on the same uvicorn process. No new service, no external Jellyfin server. The native `/api/*` routes are unchanged.

### D2. Identity: `g1:` group keys everywhere

- **View/Movie/Series item ids = `group_key`** (the `g1:…` stateless key). One card per merged title; survives a single provider outage.
- **Season ids = `group_key` + season number** (e.g. `g1:…:S1`). Nesting, not a Jellyfin-mandated format.
- **Episode ids = the existing `provider:external:sXeY`** ids, unchanged. They already carry everything the stream route needs.
- The `g1:` key is not self-resolving: the adapter keeps its **own resolution cache** `g1:key → {provider, external}` (populated as listing/home responses are built). A cold cache yields a 404 on `/Items/{id}`, which Jellyfin clients tolerate ("item unavailable").

### D3. Hierarchy

- Movies: flat `Type: Movie`, no children, `ParentId` = view.
- Series: `Type: Series` → `Type: Season` (ParentId = series) → `Type: Episode` (ParentId = season), using Season/Episode ids from D2.

### D4. Auth

- `POST /Users/AuthenticateByName` (and `Authorization: MediaBrowser Token="…"` / `X-Emby-Token` if the client uses it) accepts **any username/password** and returns a fixed opaque token from an env variable. No real security — the LAN API was already open. Both auth header forms are accepted on subsequent requests.

### D5. Views = home rows

`GET /Users/{id}/Views` returns one virtual library per `/api/home` row. No pagination in v1 (each row is capped at 20); more content is reachable via search.

### D6. PlaybackInfo: minimal media source

`GET /Items/{id}/PlaybackInfo` returns a **thin** `MediaSources[1]`:

- `Id` = item id; `Container` = `mp4` / `m3u8` / `hls` (from `StreamResponse.type` via `/api/stream`).
- `MediaStreams`: a single `{"Type": "Video"}` — **no codec fields** (lying about codecs risks forcing a transcode path we can't serve).
- `IsDirectStream: true`; `Path` = a fictitious stable string (`/videos/<id>`).
- `PlaySessionId` = a generated UUID. Bytestream always comes from `/Videos/{id}/stream`, never from `Path`.

### D7. Conditional stream handler — the center of the spec

`GET /Videos/{id}/stream`:

- If `StreamResponse.headers` is empty → **302 Found** to the CDN URL. No byte proxying.
- Otherwise → **full byte proxy** through the backend, adding all headers from `StreamResponse.headers` to the upstream CDN request:
  - file streams: forward the client's `Range` to the CDN, return correct `206 Partial Content` / `Content-Range` / `Accept-Ranges`;
  - HLS (`.m3u8`): fetch the manifest with headers, **rewrite every segment URI** to the backend (`/Videos/{id}/segment?...`), so the client's segments go through the proxy with headers too;
  - preserve `Content-Type` (`video/mp4` / `application/vnd.apple.mpegurl`).

This resolves the "JSON mapping + redirect vs full proxy" question: **neither alone** — a conditional handler.

### D8. Sessions: no-op

`POST /Sessions/Playing`, `/Sessions/Progress`, `/Sessions/Stopped` accept the request body and return **204 No Content**. No state is stored; resume/history stay out of scope.

### D9. Posters

- `ImageTags.Primary` is set iff the item has a poster (`poster != null`).
- `GET /Items/{id}/Images/Primary` → **302** to the existing `/api/poster?u=<encoded>`, which already has memory+disk cache, 4 MB cap, and timeouts.
- Only Series/content items get `ImageTags.Primary`. Season/Episode get none (clients show a placeholder).
- `maxWidth` query params are ignored; the original image is served (no resize in v1).

### D10. Configuration

- Jellyfin facade listens on the same host/port as the existing app (no new port).
- Fixed token backend value configured via env; documented in the adapter's section of `docs/status.md`-adjacent documentation.

## Testing Decisions

- **The seam is the HTTP surface**: the top seam is the FastAPI `TestClient` against the Jellyfin routes, asserting Jellyfin JSON shapes — the same seam the project already uses for `/api/*` contract tests.
- **Capture-first fixtures**: before freezing the endpoint set, point a real Jellyfin client (desktop/web, same protocol family as Switchfin) at the facade, drive the base scenarios (login, open library, list items, item detail, playback start, poster), record the exact request sequences (the existing request-logging middleware makes this cheap), and freeze them as fixtures. Contract tests are then written against exactly that captured surface — not the guessed 10-endpoint list.
- **Adapter resolution-cache tests**: unit tests for `g1: → {provider, external}` resolution, including the cold-cache → 404 path.
- **Stream handler tests**: conditional branch coverage — redirect when headers empty; Range + 206 proxying for MP4; HLS manifest segment rewriting for `.m3u8`; content-type preservation.
- **Auth tests**: handshake via any credentials, both token header forms accepted.
- **PS4 on-console test** remains the final integration validation point (not the primary dev loop).
- Prior art for the style: existing `test_api.py`, `poster_proxy` tests, and the capture-first provider fixtures already in `backend/cs_uk_api/tests/`.

## Out of Scope

- Switching/fixing the actual client: Switchfin itself is not modified.
- Dubbing/translation chooser in the UI (v1 picks `translations[0]` server-side; upgrade path to MediaSources exists if Switchfin is proven to expose them).
- Pagination of views (browse beyond `StartIndex`/`Limit` on home rows).
- Subtitles: the contract has no subtitle data; no `/Videos/{id}/Subtitles/…` endpoint. The player shows no subtitle controls.
- Real transcoding: everything is direct stream; `IsDirectStream: true` always.
- Resume / "continue watching": sessions are no-ops; no resume state endpoint.
- Security beyond a fixed opaque token; the LAN API stays open as today.
- Replacing the pPlay fork deliverable: it is retired from the critical path but kept as a reference/Linux test surface.

## Further Notes

- The "minimal 10-endpoint list" from the task is a starting hypothesis, not a contract: real clients request more (e.g. `GET /System/Info`, `ItemsResult.TotalRecordCount`, `/Users/{id}`-prefixed variants, possible `/emby` prefix). Capture-first (D-testing) is the verb that pins the actual surface.
- Depends on `/api/stream/{id}` with the existing `translation=None` → first-default semantics, so D6/D7 need no translation plumbing.
- ADR-0003 (cache contract) is respected: the new facade adds no persisted domain schema; the resolution cache is in-memory like `_home_sources_cache`.

## Capture Report (ticket #103) — frozen real-client surface

Approved before implementation, so we pinned the endpoint set against a real client instead of guessing. The `@jellyfin/sdk` v0.13 network layer (the exact protocol family Switchfin/Jellyfin Web use) was driven against the live facade for the base scenarios; every request it emitted was recorded by the request middleware and frozen at `backend/cs_uk_api/tests/fixtures/jellyfin/capture.jsonl`.

### What the real client actually requests (10 requests, in order)

| # | Method | Path | Query | Status (today) |
| --- | --- | --- | --- | --- |
| 1 | GET | `/System/Info/Public` | — | 200 |
| 2 | POST | `/Users/AuthenticateByName` | — | 200 |
| 3 | GET | `/UserViews` | `userId=<uuid>` | 404 |
| 4 | GET | `/Items` | `userId`, `parentId` | 404 |
| 5 | GET | `/Items/{id}` | `userId` | 404 |
| 6 | POST | `/Items/{id}/PlaybackInfo` | — | 404 |
| 7 | GET | `/Videos/{id}/stream` | — | 404 |
| 8 | POST | `/Sessions/Playing` | — | 404 |
| 9 | GET | `/Items/{id}/Images/Primary` | — | 404 |
| 10 | POST | `/Sessions/Logout` | — | 404 |

Capture verdicts vs the guessed 10-endpoint list:

- **`/Users/{id}/Views` (D5's spelling) is wrong — the SDK sends bare `/UserViews?userId=…`.** The client discovers views against the *signed-in user id*, and the id it echoes back is the `User.Id` from the login response (deterministic `uuid5` of the username, pinned by the auth tests). Both spellings are kept in the facade namespace filter so neither a server-style nor SDK-style client breaks capture.
- **`/Items` (bare, no trailing path id) is the library-list call** — `parentId` = the view id, `userId` = the login user id. This is the home-rows listing (D5), reached with the view's `Id` echoed as `parentId`.
- **`/Items/{id}?userId=`** is item detail, `PlaybackInfo`/`stream`/poster/sessions map 1:1 to the guesses. Sessions also includes `Logout` → 204 (no-op, same family as D8).
- Header shape is a scrubbed copy of `Authorization: MediaBrowser Client=..., Device=..., DeviceId=..., Version=..., Token="<scrubbed>"` on every request after login; discovery is the only unauthenticated call.

### Fetch of the driver

`backend/cs_uk_api/tests/jellyfin_capture/` holds `capture.mjs` (+ `package.json`) — the `@jellyfin/sdk` driver that reproduced the above. Running it against a live facade (`CS_UK_JF_CAPTURE_DIR` set, `npm run capture`) regenerates `capture.jsonl`; the contract test `tests/test_jellyfin_capture.py` replays the frozen sequence through the TestClient seam and asserts the facade never 5xxes on the real client surface (today every non-handshake call is a clean 404, tightening as each endpoint lands).