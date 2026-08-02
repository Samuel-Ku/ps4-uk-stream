# Uakino upstream — streaming verification via JS-capable client (2026-08-02)

Ticket: [Uakino upstream — verify streaming via JS-capable client (headless browser) (#50)](https://github.com/Samuel-Ku/ps4-uk-stream/issues/50).
Companion to [Uakino reachability 2026-08-02](uakino-reachability-2026-08-02.md). All probes pinned 2026-08-02.

## Question

Does uakino.best's content/player Turnstile challenge fall to a JS-capable
client (headless Chromium)? If content loads, does a stream playlist come
out, and does it match what the upstream Kotlin adapter would parse on the
current DLE theme?

## Method

- Client A (no JS): `httpx`, UA `cs-uk-api/0.1 (+https://github.com/)`.
- Client B (JS-capable): headless Chromium (`--headless=new`, Chrome UA,
  `--virtual-time-budget=25000`, `--dump-dom`).
- Targets: content page `/filmy/genre-action/12567-dyuna.html`, legacy
  player page `/player/12567.html`, ajax endpoint
  `/engine/ajax/playlists.php?news_id=12567&xfield=playlist`, and the
  iframe target `https://www.youtube.com/embed/Ljzu52GMytk`.
- Reference: upstream Kotlin adapter `UakinoProvider.kt` (sparse clone,
  `mainUrl = "https://uakino.best"`, HEAD of CakesTwix master).

## Findings

1. **Turnstile falls to a JS-capable client.** Client B loaded the content
   page: HTTP 200, 214 KB, `<title>Фільм Дюна (2021) онлайн українською
   мовою в HD</title>`. The challenge is JS-gated, not IP-gated — a real
   browser passes it on a datacenter-class network. Client A gets 403
   "Just a moment…" on the same path. Consistent with the #45 verdict:
   the portability boundary is exactly the JS engine.

2. **Legacy player route is gone.** `/player/12567.html` (old theme, still
   used by the adapter's URL scheme) returns **404 — сторінка не знайдена**
   on the new theme, even through Client B.

3. **Movie stream path is dead in the upstream adapter too.** The live
   content page contains exactly one video iframe: `iframe#pre` (class
   `vdd-element`) → `https://www.youtube.com/embed/Ljzu52GMytk`. The
   adapter's movie flow (`extractPlayerJs(iframeUrl, ...)`) fetches that
   URL with a plain HTTP GET (CloudStream `app.get`, no JS) and regexes
   `file\s*:\s*['"]([^'",]+?)['"]` out of its `<script>` data. The embed
   page contains **no `file:` match, no `streamingData`, no
   `hlsManifestUrl`, no `lengthSeconds`** — verified via plain HTTP (200,
   139 KB shell) and via real headless Chromium (same shell, 0 hits).
   `fileRegex` therefore returns nothing and `extractPlayerJs` emits no
   links. The `file:` pattern is old-theme; the new theme fronts its
   streams with YouTube embeds, which the adapter's regex cannot parse.
   (Trailer path is also off: the adapter reads `iframe#pre` as
   `data-src`; the live DOM puts the URL in `src`.)

4. **Series ajax path is Turnstile-gated.** `/engine/ajax/playlists.php`
   returns 403 "Just a moment…" to Client A. The adapter's series flow
   fetches it with plain `app.get`, so it would hit the same wall; behind
   the challenge it would additionally depend on the same old-theme
   `file:` regex over `li[data-file]` player URLs.

## Verdict

- "Upstream works" is **not confirmed**; on the evidence it cannot
  produce a movie stream today: the adapter's movie path resolves to a
  YouTube embed that never contains its `file:` pattern, and its series
  path is Turnstile-gated for plain HTTP.
- The keep-as-known-broken verdict (#45) **stands** and is now
  double-rooted: (a) our backend has no JS engine → Turnstile blocks
  content; (b) even the JS-capable path has no working stream extraction
  in the upstream adapter on the current theme.
- No code changes to the repo; existing docs already describe the state
  honestly.

## Artifacts

Probe dumps (ephemeral, not committed): `uakino-live-content.html`,
`yt-embed-browser.html` in `/tmp/opencode/`.
