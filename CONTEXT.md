# Project Glossary — ps4-uk-stream

> Single source of truth for domain terms. Implementation details live in code; this file holds the *what* and *why*, not the *how*.
>
> The reasoning behind this glossary's current shape lives in [`docs/adr/0001-catalog-taxonomy-form-and-style.md`](docs/adr/0001-catalog-taxonomy-form-and-style.md).

---

> **Architecture notes (spec #309, deepening wave):** the module seams —
> catalog accessors, wire identity, provider vocabulary, profile store,
> configuration binding — are recorded in
> [`docs/architecture.md`](docs/architecture.md). This file stays the
> domain glossary (the *what* and *why*); the notes hold the *where*.

---

> **Shipped (Model B, contract step #135, 2026-08-10):** the sections below document the shipped model — the decided target IS the wire contract. Every content item (search/browse/content/home) carries required `form` + `styles` axes; `Section` declares its `form`/`styles` filter axes; providers populate them directly with the typed `form`/`styles` values (the legacy `model_b_axes` mapping was removed in contract step #319); `/api/search` + `/api/browse` filter on them (tickets #129–#135, #319). The legacy `MediaType`/`type` field is gone from models, providers, and API responses — `anime`/`cartoon`/`dorama` ship as `styles`, never conflated with `movie`/`series`.

## Catalog taxonomy (Model B — form + style)

The catalog is shaped by **two independent axes**: the **form** of the content (movie vs series) and the **style** tags on it (anime, cartoon, dorama, none). This split was decided to replace Model A's single `MediaType` enum, which conflated the two and forced ambiguous content like "дитяче аніме" to be either one.

### Term: MediaForm

`MediaForm = "movie" | "series"`. Required on every content item. The cinematic-vs-episodic split. Doesn't say anything about content genre.

- Form is *orthogonal to style*: the same `series` form can be `anime`, `cartoon`, `dorama`, or ordinary live-action.
- OVA / ONA / Special are *not* per-item forms — they are section-level groupings under `series` form (decided: they live in `Section.id`, not in `MediaForm`).

### Term: MediaStyle

`MediaStyle = "anime" | "cartoon" | "dorama"`. Optional tag(s) attached to a content item. Plain live-action content has no style tag (empty, not `"live"`).

- A single item may carry *several* styles via a `frozenset[MediaStyle]` (decided: SET, not single).
- Decided: ordinary content is represented by **empty `frozenset()`**, not by a fourth style value (decided: empty, not `"live"`).

### Term: ProviderCapabilities

`/api/providers` returns a list of `ProviderCapabilities` describing what each provider offers. Shape (decided: two independent sets, Variant A):

```text
forms: frozenset[MediaForm]      # {"movie"}, {"series"}, or both
styles: frozenset[MediaStyle]    # ∅ = no style-tagged content; or {"anime"} / {"cartoon"} / {all three}
```

- This is the **rollup**, not a precise cross-product — it answers "does Provider X have *any* anime movie at all?". Per-section precision is in `Section` (next term).
- Tradeoff accepted: cannot express "only anime movies, no plain movies". If such constraint appears in practice, an exotic provider would express it via Sections (which carry form+styles filters per-section).

---

### Term: Content (item shape in Model B)

```text
form: MediaForm                # required: "movie" | "series"
styles: frozenset[MediaStyle]  # default = empty (= ordinary live-action)
```

Examples (decided):

| Content | form | styles | Notes |
| --- | --- | --- | --- |
| Naruto (аниме-серия) | series | {anime} | |
| Bee Movie (аніме-фільм) | movie | {anime} | same style, different form |
| Bee Movie звичайний (live-action) | movie | ∅ | empty = ordinary |
| Слово Пацана | series | ∅ | |
| Simpsons | series | {cartoon} | |
| K-drama серіал | series | {dorama} | |
| Дитяче аніме (мультик) | series | {anime, cartoon} | set allows both, no pick-one |

---

## Domain-modeling ground rules for this session

- Terms are added here **only after the user confirms** the grilling decision.
- Implementation details (Pydantic fields, JSON wire shape) live in code; this file holds intent.
- An item's `styles` is **a SET**, never a single value or string — a frozen/empty set literally encodes "no style tag", avoiding a special "default" sentinel.
- `MediaForm` is **always required** (no `None`, no default) — every piece of content is either a movie or a series.
- OVA / ONA / Special live at the **section layer**, not the item layer.

---

## Section schema (decided Variant A)

```text
Section {
  id: str                 # slug, unique within provider
  title: str              # display label
  form: MediaForm | None  # exact-or-None filter
  styles: frozenset[MediaStyle] | None  # 3-case filter (see below)
}
```

**Match semantics:**

- `form`: `None` → pass; else `item.form == section.form` must hold.
- `styles`: three cases, distinguishable by the field value:
  - `None` → pass (any styles, including empty).
  - `frozenset()` (`∅`) → pass iff `item.styles == frozenset()` (i.e. **ordinary-only**: no anime, no cartoon, no dorama).
  - non-empty `frozenset({...})` → pass iff `bool(item.styles & section.styles)` (intersection: must include at least one).

**Why one styles field, not two:** real catalog sections are all single-axis (style ∈ {anime} or form ∈ {movie}) plus the occasional `∅` for "ordinary-only". Subset/all-of semantics would model "дитяче аніме мультики" but no current section needs it (YAGNI). Adding `styles_all` later is non-breaking.

---

> **Shipped (ticket #134, 2026-08-08):** `GET /api/search` accepts `form` and `style` as documented below, and `/api/browse` filters each section's results by its declared `form`/`styles` axes (undeclared axes pass everything). The `/api/search` cache key carries both axes, fulfilling the ADR-0001 obligation.

## Search filter axes (decided A)

`GET /api/search` accepts the same axes as Section, with the 2-of-3 case subset (no `ordinary-only` token):

```text
?q=...                  # full-text title query, required
&form=movie|series      # exact-or-None; absent = any
&style=anime|cartoon|dorama[,anime,...]   # intersection (comma-separated list); absent = any
```

The 3-case semantics on Section (`None | ∅ | non-empty`) collapses to 2-case on query strings:

- `style` absent = any (covers `None`).
- `style=anime,cartoon` = intersection (covers non-empty).
- `ordinary-only` (`∅` case) is **deliberately not exposed on search** — Section is the way to filter to ordinary-only; keeping `?style` simple avoids a magic `style=none` token.

`/api/browse` does NOT take additional filter params — the section itself encodes the filter.

---

## Adult content scope (decided A)

- No `is_adult` field anywhere in the domain model (no Section, Provider, or Item).
- The spec decision: "HentaiUkr is in scope, in the last implementation group, **without any hiding/disabled-by-default flag**".
- Provider identity (`HentaiUkr` name) plus section title (`Хентай`) carry the signal; PS4 OS-level parental controls (if any) are out of scope for this catalog domain.

---

## Stream contract (decided A on Q11–Q15: minimum surface)

`StreamResponse` keeps the spec §3.6 shape unchanged. The five Q11–Q15 decisions are all "do not add fields" — minimum viable surface for a LAN streaming scenario:

```python
class StreamResponse(BaseModel):
    url: str
    type: StreamType = "mp4"                       # "mp4" | "m3u8" | "hls" | "dash"
    headers: dict[str, str] = Field(default_factory=dict)
```

The deliberate non-features (each rejected with alternatives evaluated):

- **No `streams: list`** — single URL only. Provider `stream()` picks the best upstream option; multi-URL providers' alternatives are dropped, not exposed.
- **No `quality: str`** — `type` and known CDN behaviour carry enough information; explicit quality would either lie (heuristics on `*_720p.mp4` filenames are unreliable) or require probing.
- **No backend URL probe** — LAN topology (backend + PS4 share egress) means a 200 from backend ≈ a 200 from PS4; MPV surfaces playback errors directly.
- **No TTL contract** (`expires_at` etc.) — manifest URLs are short-lived but the streaming protocol (HLS segments) refreshes them transparently. No observed issue today.
- **No fallback chain** — PS4 retries by user action; backend does not rank URLs from the same provider as primary/fallback.

If any of these start biting in production, they can be added as **non-breaking optional fields** (`streams: list[str] | None = None`, `quality: str | None = None`, etc.) without touching existing clients. The current shape is the floor, not the ceiling.

---

## Translation contract (decided A on Q16–Q20: minimum surface)

Translation handling keeps the spec §3.6 shapes unchanged. The five Q16–Q20 decisions are all "do not add fields, do not merge, do not introduce a new contract" — minimum viable surface for a LAN streaming scenario:

```python
class Translation(BaseModel):
    id: str          # per-provider scoped, opaque to consumers
    label: str       # display label

TranslationLevel = Literal["content", "episode"]

class Episode(BaseModel):
    translations: list[Translation] | None = None

class ContentResponse(BaseModel):
    translations: list[Translation] = Field(min_length=1)
    translations_level: TranslationLevel = "content"
```

### Decisions

- **Q16 — `{id, label}` only.** No `language`, `kind`, `studio`. PS4 displays label; user picks by it.
- **Q17 — `translations_level` is provider-decided.** AnimeUA / Kinotron auto-detect at parse time (uniform voices → `"content"`, varied → `"episode"`). Animeon / Uakino hardcode `"episode"` because their data structure guarantees per-episode variation.
- **Q18 — `Translation.id` is per-provider scoped.** Same principle as `Section.id`: identity is `(provider, content_id, id)`. Cross-provider equality not guaranteed.
- **Q19 — Default = `translations[0]` implicit.** PS4 sends `translation=None` to mean "default", and `stream()` returns the first available candidate. List order encodes provider priority.
- **Q20 — Multiple studios stay distinct.** FunDub / AnimeUA / AniUA — three Ukrainian studios — remain three separate `Translation` entries with different ids. No merging. No `is_primary` flag.

### What `translations_level` means to PS4

- `"content"` — all episodes share the same voice set; show one global picker.
- `"episode"` — episodes may have different voice sets; show per-episode picker.

For `"episode"` content, an episode whose `translations=None` falls back to `content.translations`. This is the existing `episode_translations()` contract in `base.py`.

### Named translation picker + dub memory (spec #276)

PlaybackInfo serves ONE MediaSource per translation when the item has
more than one (cap 8, deduped by label, first player per label), each
carrying an audio `MediaStream` with `Index` + `DisplayTitle` = the
translation label — the client's named source picker becomes real
instead of mpv's unnamed demuxed tracks. The source `Id` encodes
`<item_id>::<translation_id>`; the stream route decodes it (split on
the LAST `::`) and streams THAT translation.

- **Dynamic index.** The response's source order re-ranks per request:
  the source matching the echoed `AudioStreamIndex` goes FIRST (the
  client plays `MediaSources[0]`), otherwise the REMEMBERED dub goes
  first, otherwise provider order. `Index` values are 1..N in the
  response order (the client's default selected index is 1).
- **Dub memory** (per-series, `UserStateStore.dub_memory`, persisted in
  `user-state.json`): the label of the last streamed translation is
  remembered for the SERIES (resolved from the episode wire id via the
  #214 reverse lookup), LRU-bounded at 50, newest pick wins. The next
  PlaybackInfo of that series defaults to it. **Movies are never
  remembered** (v3 decision) — films always start on the default dub.
- **Single-translation items are untouched**: one thin source, no
  picker, no memory recording (D6 path).
- **Known limitation (issue #243, triage decision (a) — accepted,
  2026-08-16).** Single-translation content whose HLS file is a
  dual-audio mux (e.g. uaflix zetvideo `ukr_eng` files — «Таємниця
  бункера» s1e9) still shows mpv's UNNAMED in-band audio tracks in
  Switchfin's picker: the CDN playlists carry no `EXT-X-MEDIA` groups
  and the TS audio PIDs carry no language/title metadata, and the
  client consults the facade's `MediaStreams` only when the media has
  ≤1 audio track. The second track lives INSIDE the TS segments, so a
  manifest-only rewrite cannot name it — fixing it would mean
  remuxing/proxying the segments through the LAN host (option (b),
  deferred to a dedicated spec) or an upstream Switchfin change
  (option (c)). Accepted: both tracks play, the viewer just cannot
  tell them apart by name.

### Dashboard surface (spec #280)

The Switchfin dashboard stops 404ing — every route it opens answers
with honest data or an honest empty, never a fake number:

- **`/Items/Counts`** — movies/series counted from the home snapshot
  (deduped by group key); episodes summed from the CACHED content
  pages only (a `peek`, never a fetch — a cold series contributes 0).
  `ItemCount` = movie+series (the headline number).
- **`/System/Info/Storage`** — real bytes for the ONE directory the
  facade writes (the poster cache → `ImageCacheFolder`: used bytes via
  `os.walk` sum, free bytes via `statvfs`) plus honest empty rows for
  every other named folder; `Libraries` is empty (the catalog is
  virtual).
- **`/Users`** — the single fixed facade user (D4).
- **`/ScheduledTasks`, `/Devices`, `/System/ActivityLog/Entries`,
  `/LiveTv/Programs/Recommended`** — the client's standard empty
  envelopes (never 404).
- **`POST /Sessions/Capabilities/Full`** — every Switchfin connect
  posts its playback capabilities; the facade answers 204 (nothing to
  store — stateless, D8).
- **`GET /Items/{id}/Download`** — the SAME bytes the stream route
  serves, named via `Content-Disposition` `<safe-title>.<container>`.
  Cyrillic titles ride in the RFC 5987 `filename*=UTF-8''…` form
  (headers are latin-1); the byte proxy is forced here even when the
  stream route would 302, so the file name can ride along. The detail
  DTO carries the matching `MediaSources[].Name` for the download
  manager.
- **`POST /System/Restart`** — operator action (LAN-only by design):
  204 first, re-exec deferred one loop tick via the injectable
  `_schedule_restart` / `_exec_restart` seams.

**Danmaku and music/playlist are deliberate non-answers (N/A)**: the
catalog is movies/series/episodes — there is no danmaku surface and no
music/playlist content, so the client's related screens stay quietly
empty rather than erroring.

### Upstream drift monitor (spec #285)

A standalone nightly probe (``scripts/drift_monitor.py``, modules in
``drift/``) that detects upstream drift within a day instead of
waiting for user-visible errors:

- **Hybrid sweep.** Every plain-HTTP provider (all except uakino — its
  health is the API's browser-session heartbeat, and probing would
  warm a second browser session) gets a nightly LISTING probe: page 1
  of its newest section parsed through the REAL adapter, so a parse
  breakage or form/style flip shows up as a changed signature. A
  rotating subset additionally gets a DEEP probe (first card's
  ``content()`` → ``stream()`` → HEAD of the stream URL), full
  coverage every 6 days — stream-level drift (animeon's lost URLs)
  is caught without hammering upstreams nightly.
- **Self-calibrating baseline** (``drift/baseline.py``): each healthy
  pass stores the provider's signature — card-count band (low/high),
  form/style distribution, required-fields expectation. Drift = zero
  items, missing fields (empty title/url), count under the calibrated
  low, or a SIGNIFICANT form/style (≥20% share) leaving the band
  (<5%). Hard floors: at least 2 cards, non-empty titles. A healthy
  pass widens the band (low shrinks, high grows) and refreshes the
  distribution — catalog growth never false-positives; a flipped
  kind-signal (kinovezha search cards all defaulting to movie) trips.
- **Verdict + issue flow** (``drift/monitor.py``, ``drift/issues.py``):
  consecutive-failure counters persist in the state file; the FIRST
  failure logs only, the SECOND consecutive failure files a GitHub
  issue (one per provider, deduped by title pattern — ``gh issue
  create/list`` with the operator's auth), repeated failures reuse the
  open issue, and a healthy pass comments + closes it. The issue
  gateway is an injectable boundary so tests never spawn `gh`.
- **Report/state**: machine-readable JSON per run
  (``~/.cache/cs-uk-api/drift-report.json``) plus a per-provider
  human summary line; baseline/counters in
  ``~/.cache/cs-uk-api/drift-state.json``. Both gitignored runtime
  state, overridable via ``CS_UK_DRIFT_REPORT`` / ``CS_UK_DRIFT_STATE``.
- **Scheduling**: ``backend/deploy/cs-uk-api-drift.{service,timer}``
  (nightly 03:10, Persistent=true). The script exits non-zero when any
  provider failed — the timer's status / journal shows it.

Concrete regressions the monitor must catch (2026-08-14): animeon
episode rows losing their URLs (deep probe), eneyida bare card URLs,
kinovezha search cards losing their kind signal (form-band verdict).

### Deliberate non-features (each rejected with alternatives evaluated)

- **No `language` / `kind` / `studio` fields** — providers come from the wild; no consistent language taxonomy upstream. Display label only.
- **No merge by language** — different dubbing studios are user-meaningful even when the language is the same (FunDub ≠ AnimeUA for fans).
- **No `default_translation_id` field** — `translations[0]` already encodes it by list convention; redundant field adds no signal.
- **No `is_primary` flag** — list order = priority; one extra marker adds nothing.
- **No global `Translation.id` namespace** — cross-provider equality is not a feature; identity is `(provider, content_id, id)`.
- **No formal stability contract** — providers build id from upstream voice names, which is stable by construction. Documenting a stability guarantee is paperwork, not behavior change.

If any of these start biting in production, each can be added as a **non-breaking optional field** (`language: str | None = None`, `default_translation_id: str | None = None`, etc.) without touching existing clients. The current shape is the floor, not the ceiling.

---

## Provider lifecycle (decided on Q34–Q40)

Provider lifecycle is a deployment-time, code-reviewed concern. The project follows the minimum-surface principle: active providers are explicit in the registry, upstream reachability is learned lazily, and retirement does not introduce a second runtime configuration system.

- **Registration mechanism:** providers are registered with hardcoded `register(...)` calls in `backend/cs_uk_api/providers/_registry.py`. The registry is the authoritative active-provider list.
- **Hot-reload:** there is no hot-reload. Provider-list changes take effect when the backend process restarts during deployment; no file watcher, re-import, or SIGHUP contract exists.
- **Health tracking:** runtime health is owned by v3 issue #53, not this lifecycle decision. Its sliding-window tracker supplies `/api/providers` status and `last_error_at`; this section does not add or redefine health fields.
- **Retirement convention:** comment out the provider's registration in `_registry.py`, as with Banderakino after its upstream became unavailable. The adapter can remain in source for historical context or later reactivation, but a retired provider is absent from the active registry.
- **Priority / ordering:** search results follow registry order. `/api/search` flattens provider results in `PROVIDERS.values()` order; there is no explicit priority field or alphabetical secondary sort.
- **Startup discovery:** providers are not pinged at startup. The backend discovers upstream reachability lazily on the first applicable request and records the outcome through the existing health tracker. Deterministic local prerequisites may still produce a startup marker.
- **Adding a provider:** create `providers/<id>.py` implementing `BaseProvider`; capture live fixtures and add `test_<id>.py`; apply boundary validation and shared HTTP helpers; register the instance in `_registry.py`; update provider triage; and run the provider live gate. The detailed checklist remains in [`docs/status.md`](docs/status.md).

---

## Cache contract (decided on Q27–Q33: status quo, made explicit)

The backend caches listing and metadata responses in per-endpoint in-memory TTL stores. The seven Q27–Q33 decisions ratify the shipped behaviour rather than change it — the reasoning lives in [`docs/adr/0003-cache-contract.md`](docs/adr/0003-cache-contract.md).

Deployment assumption that drives most of it: **one host, one uvicorn process, LAN-only, a couple of concurrent clients.**

### Per-endpoint TTLs

| Endpoint | Store | TTL | Env knob | Why |
| --- | --- | --- | --- | --- |
| `/api/search` | memory | 5m | `CS_UK_CACHE_SEARCH` | Absorbs re-search on back-navigation; short enough that a new release surfaces within one viewing session. Also bounds memory, since `q` makes the keyspace unbounded. |
| `/api/browse` | memory | 5m | `CS_UK_CACHE_SEARCH` (shared) | Same freshness class as search — both are listings that change when episodes land. One knob for both; a third env var nobody would tune is not worth it. |
| `/api/content/{id}` | memory | 30m | `CS_UK_CACHE_CONTENT` | Episode lists and translations change on release day, not by the minute. Makes the browse → content → back → content loop free. |
| `/api/content/{id}` (blocked-country 404) | memory | 30m | `CS_UK_CACHE_CONTENT` (shared) | The block follows from the item's `country` field, which is as stable as the item. Avoids re-fetching and re-parsing content that will be rejected again. |
| `/api/poster` | memory | 1h | `CS_UK_CACHE_POSTER` | A memory-pressure bound, not a freshness bound — a poster is immutable for a given URL. |
| `/api/poster` | backend disk | 7d | `CS_UK_POSTER_DISK_TTL` | Issue #54. Survives restarts; disk is cheap; content is immutable per URL. |
| `/Items/{id}/Images/*` (facade) | memory | per-poster | — | The facade serves the same backend-disk-cached bytes inline (public, token-less image routes); the WebP transcode memo bounds CPU, not freshness. |
| `/api/providers` | **not cached** | — | — | Embeds live `TRACKER.status()`. A TTL would delay exactly the health signal the endpoint exists to deliver. |
| `/api/sections` | **not cached** | — | — | A list comprehension over a static in-process registry. A cache in front of a dict lookup is pure overhead. |
| `/api/stream/{id}` | **not cached** | — | — | See "What is not cached" below. |

### Resume state (ticket #248, spec #247)

Playback positions are NOT a cache: they are persisted domain state —
the deliberate exception to the in-memory invariant below (ADR-0003
note). One versioned JSON file, `{"v": 1, "items": {item_id:
{"position_ticks", "runtime_ticks"?, "updated_at"}}}`, written
atomically (temp + rename), flushed immediately on a Stopped report and
debounced on Progress heartbeats, flushed again on shutdown.

| Aspect | Value |
| --- | --- |
| Location | next to the poster disk cache (`~/.cache/cs-uk-api/playback.json`) |
| Env knob | `CS_UK_RESUME_PATH` (explicit empty string → memory-only) |
| Corruption / version mismatch | warn + empty resume, API keeps serving |
| Restart | survives (the whole point — «Продовжити перегляд» persists) |
| Finished-marking (#249) | position ≥ 95% of a known runtime drops the item from Resume + NextUp; items with no runtime are never auto-finished |
| Cap (#249) | 50 entries LRU by `updated_at`; the row returns the ≤20 most recently updated, most recent first |
| Runtime on the wire (#250) | Resume/NextUp DTOs carry `RunTimeTicks` (alongside `PlaybackPositionTicks`) from the recorded runtime when known; reports without a runtime stay position-only |
| Wipe | `rm <path>` — clean state, documented operator story |

### User state (ticket #258, spec #257)

Favorites and played marks — tapped on Switchfin's detail screen (heart)
and card context menu (mark played/unplayed) — are persisted domain
state like the resume store, deliberately in a SEPARATE versioned file
so the two specs' version bumps never collide. One JSON file,
`{"v": 1, "favorites": [...], "played": [...]}`, written atomically
(temp + rename) synchronously on every toggle, so the UserDataResult
response always reflects durable state.

| Aspect | Value |
| --- | --- |
| Location | `user-state.json` next to the resume file |
| Env knob | `CS_UK_USER_STATE_PATH` (explicit empty string → memory-only) |
| Corruption / version mismatch | warn + empty state, API keeps serving |
| Restart | survives (hearts and checkmarks persist) |
| Cap | bounded lists with dedupe (256 each) |
| Ids stored | whatever wire id the client sends — `g2:` keys, episode wire ids, season ids — stored verbatim |
| Wire surface | `POST/DELETE /Users/{uid}/FavoriteItems/{id}` and `/PlayedItems/{id}` answer the `UserDataResult` object (client reads `IsFavorite`/`Played` from the response); card/detail/episode DTOs carry `UserData` |
| Wipe | `rm ~/.cache/cs-uk-api/user-state.json` (or `CS_UK_USER_STATE_PATH`) |

### Recommendations (spec #252)

Two personalized home rows — «Рекомендовано для тебе» (≤20) and
«Схоже на X» (≤10) — rank the home snapshot's groups by content
similarity to the viewer's taste. The scorer is a pure weighted cosine
in `cs_uk_api/recommend.py` (genres 1.0, people 0.9, styles 0.4, year
proximity 0.3 within |Δyear| ≤ 2; a form mismatch halves the total;
query matches add a fixed boost) — the pluggable seam for a future LLM
ranker. Profiles are built from providers' content pages by a
background warm with bounded concurrency (piggybacks the 30-min content
cache; in-memory, keyed by group). Taste anchors are the ≤3 most recent
watched items (from the persisted resume store) plus the ≤50 recent
search queries (persisted beside the playback state, resume-file schema
v2). Watched items are excluded; rows are omitted when there is no
signal. Placement: after «Популярні зараз» (or the form-split recent
rows when popular is absent), before the type rows — each row is just
another home-row kind (`recommended` / `similar`), so the facade serves
them through the existing view mechanism with zero client changes.

The detail screen's «Схожі» shelf (`/Items/{gk}/Similar`, spec #267
T1) uses the SAME scorer: with warm profiles it ranks the snapshot's
groups against the item's profile (deduped by group key, the item
itself excluded, capped at the client's limit) — so a genre-less item
whose content page carries signal is no longer stuck with an empty
shelf. A cold profile store falls back to the pre-#267
genre-matching shelf.

The person page's filmography (spec #272): the Items route honors
`PersonIds` (comma-separated; the client's person page sends a single
provider-scoped person key whose final path segment is the name),
matching case-insensitively against the profile store's people and
returning the matching home-snapshot groups as Movie/Series cards,
filtered by `includeItemTypes` the way the client asks (Movie|Series),
deduped by group key. An unknown person or a cold profile store is the
tolerant empty result. No new scraping — the #252 profiles already
carry people per title. Person portraits stay placeholder (accepted,
no upstream data).

**Source/dub memory is a CLIENT limitation, not a backend gap (spec
#272).** The player picks the dub inside Switchfin and never reports
the choice back (no `Sessions/Playing` field carries it), so the
backend cannot record which dub a viewer watched. Evaluated and
rejected for parity round 3 — documented so no future round re-proposes
it.

### LLM taste profile (spec #290)

An OPTIONAL enrichment of the #252 scorer, filled through the seam the
scorer was designed for. When all three knobs (`CS_UK_LLM_BASE_URL`,
`CS_UK_LLM_KEY`, `CS_UK_LLM_MODEL`) are set, a daily background task in
the app lifespan calls ONE OpenAI-compatible chat-completions endpoint
(30 s timeout, no retries) with the signals — the ≤10 most recent
history items resolved through the series-group reverse lookup
(`episode_group_key` → warm content profile, so episodes land on their
series), the recent search queries, and the catalog genre vocabulary
(the union of genres across the warm profiles). Provider titles are
untrusted DATA in the prompt, never instructions.

The validated v1 profile (`llm.py::TasteProfile`) plugs into the pure
scorer (`recommend.py`) as strictly additive:

- **genre_weights** (0.2–2.0) multiply each SHARED genre's cosine
  contribution — a boosted genre's titles re-rank above the unweighted
  order;
- **theme_tags** reuse the query-boost token mechanics (a tag matching
  the title or a genre label adds the fixed boost);
- **row_ideas** (≤2) become home rows with Ukrainian titles, filtered
  to items whose profile shares a declared genre, capped at the idea's
  max — served through fixed facade row-kind slots `llm_idea_1` /
  `llm_idea_2` (stable view ids, zero client changes). An idea with no
  matching item ships no row; the curation never lies.

**The fallback invariant: ANY failure → the pure scorer.** A missing
knob, a network error, a non-JSON answer, an out-of-band weight, or a
malformed idea rejects the WHOLE profile (never a partial install) and
leaves the previous profile — or none — active. Without a profile the
rows and scoring are byte-identical to the pre-LLM behavior. The
profile is in-memory only (regenerable; a restart re-runs the loop),
never blocks the home build (the call happens in the refresh task, not
on the home path), and `refresh_profile()` clears the home cache on a
successful install so the new rows surface immediately.

Operator surface: `POST /ScheduledTasks/Running/llm-profile`
(token-gated, in the dashboard's task idiom) refreshes on demand — 204
on success, 200 with a note when inert, never an error.

### Home composition (spec #263)

«Новинки» was retired (2026-08-14) in favour of a Netflix-style home:

- **Form-split recent rows** — «Нещодавно додані: Фільми» and
  «Нещодавно додані: Серіали» (`recent_movie` / `recent_series`).
  Each row is the providers' newest listings filtered by form,
  round-robin-deduped, and **topped up** from the form-section page-1
  items (the same data the type rows use) when under the cap — overlap
  between a recent row and its type row is accepted, Netflix-style. A
  row is omitted when its form has no data anywhere.
- **«Нові серії»** (`new_episodes`, spec #267 T3) — position 3, right
  after the two form-split rows: the series-form NEWEST listings
  (no section top-up) whose group keys sit in the viewer's playback
  history, ranked by listing position, ≤20, omitted when empty. Fed
  `watched_series` = the episode wire ids' merged groups from the
  persisted resume store (`_watched_group_keys`).
- **«Нещодавно переглянуто»** (`recently_watched`, spec #272) —
  position 4, after «Нові серії», before «Популярні зараз»: the most
  recently seen items, active AND finished (the resume shelf drops
  finished titles; this row keeps them browsable), most recent first,
  ≤20, omitted when empty. Fed by the playback store's finished
  history (`ResumeStore._finished`, spec #272) plus the active
  entries, resolved against every collected listing (a finished item
  is usually off the newest page by then).
- **Genre rails** — the top-6 genres by profile-store coverage across
  the snapshot become rows (`genre:<slug>` view kinds, Ukrainian
  labels, ≤20 items each, recency-ranked, deduped by group key);
  genres below the 3-member threshold are skipped. Pure builder
  `build_genre_rows` in `cs_uk_api/home.py`, fed by the same warm
  content profiles the recommendation rows use.
- **View ids** — a row kind's `Id` is `uuid5("cs-uk-api-view:{kind}")`,
  so every kind (including the new `recent_*` / `genre:*`) resolves
  through one formula; the reverse lookup scans the snapshot and
  re-resolves against a fresh load when the cached home is
  mid-invalidation (the profile-warm clear).

Trade-off of the retirement: the old «Новинки» view id
(`ac357d43…`) no longer resolves — a client that cached it gets the
tolerant empty library until it refreshes its view list. Accepted: the
client re-lists views on every cold launch.

### Persisted home snapshot (ticket #269, spec #267 T2)

The last successful home build (rows + the group resolution map) is
mirrored to a single versioned JSON file
(`{"v": 1, "rows": [...], "sources": {...}}`) next to the resume
file, written atomically (temp + rename) after every successful build.
On a cold start `load_home` serves the persisted snapshot **at ANY
age** — stale is accepted, dead-poster risk on a very old file is
accepted — while a background task rebuilds and overwrites it. A
corrupt / version-mismatched / unparseable file degrades to a fresh
build with a logged warning, never a crash.

| Aspect | Value |
| --- | --- |
| Location | `home-snapshot.json` next to the resume file |
| Env knob | `CS_UK_SNAPSHOT_PATH` (explicit empty string → memory-only) |
| Corruption / version mismatch | warn + fresh build, API keeps serving |
| Restart | survives (cold start answers instantly, heals in background) |
| Wipe | `rm ~/.cache/cs-uk-api/home-snapshot.json` (or `CS_UK_SNAPSHOT_PATH`) |

### Cache key format

Flat, colon-joined `{namespace}:{discriminants…}` strings, one store per namespace:

```text
search:{provider}:{q}                  # provider="all" for the aggregate
browse:{provider}:{section}:{page}
content:{content_id}                   # content_id is itself "{provider}:{external_id}"
{poster_url}                           # poster memory store: bare URL, own store
sha256(poster_url)[:32] + ext          # poster disk stores, both sides
```

Rules:

- The key carries **every request parameter that can change the response** — nothing more.
- The **provider axis is always present**: explicitly in `search:` / `browse:` keys, implicitly in `content:` keys because `content_id` is prefixed with the provider.
- `q` is **not normalized** (no case-folding, no whitespace collapsing). A normalizer would have to mirror what 19 independent scrapers do with the query; where it disagrees, a hit returns results for a different search. Duplicate entries are the cheaper failure.
- **Fulfilled (ticket #134)**: the `/api/search` key carries the `form` and `styles` axes (`search:{provider}:{q}:{form}:{sorted-styles}`), so filtered and unfiltered searches never collide.

### Invalidation

**TTL-only.** There is no flush endpoint, no event-driven invalidation, no manual purge API.

- A **process restart is the global flush**, and it is free because every store is in-memory.
- Providers are scraped websites — there are no webhooks and no push channel, so event-driven invalidation is not merely undesirable, it is unavailable.
- The poster **disk** layers and the resume state file are the only state that survives a restart; `rm -rf ~/.cache/cs-uk-api/posters` flushes the posters (opaque bytes, never need one for correctness), and `rm ~/.cache/cs-uk-api/playback.json` (or `CS_UK_RESUME_PATH`) wipes the resume shelf.

### Versioning

**No version token in cache keys.** The invariant that makes one unnecessary:

> No value carrying a domain schema is ever persisted beyond process lifetime.

A schema change is a code change is a restart is an empty cache — so a version prefix could never differ from the one that wrote the entry. The poster disk caches satisfy the invariant by storing opaque image bytes under a content-addressed key. **Persisting any domain object (offline catalog, warm-start snapshot, disk-backed `content:` layer) breaks this invariant and makes a version token mandatory** — ADR-0003 must be revisited first. The viewer-profile persistence (spec #323) is the first such value: it persists through `versioned_store.py` with a version token + atomic writes (see [`docs/architecture.md`](docs/architecture.md) §6).

The resume state file is exactly that exception: it persists a domain schema, so it carries a **mandatory version token** (`v` field, see above) and a mismatched file is ignored (warn + empty) — the remedy ADR-0003's consequences section prescribes. See the ADR note.

### Stampede protection

**None, deliberately.** No per-key single-flight lock. A stampede needs concurrent misses on an *identical* key; this deployment has a person pressing buttons on a gamepad. `/api/search` fan-out is already bounded by the 12s `asyncio.wait_for` budget and the shared `httpx` connection limits. If the backend is ever exposed beyond LAN, single-flight is the first thing to add — and it fits behind the existing `TtlCache` API without touching call sites.

### What is NOT cached

- **`/api/stream/{id}`** — upstream URLs are session-scoped or token-signed. A stale hit returns a URL that fails at the player with no way for the backend to know; a miss costs one request. This is also why the Stream contract above declines an `expires_at` field: nothing is cached to hold stale.
- **`/api/providers`** — live health data.
- **`/api/sections`** — a static registry read.
- **Error responses.** A 502 from a flapping provider is never pinned. Negative caching is permitted only for **deterministic verdicts** (the blocked-country 404, which follows from the item's own `country`) and never for **failures**.

### Known limits (accepted, not fixed here)

- **Unbounded entry count.** No LRU, no max size; expiry is lazy. The pressure point is the poster memory store (1h TTL, up to 4 MB per entry). A size bound belongs in the implementation issue, not the contract.
- **Partial failures are cached, and stay visible.** When `provider=all` and one provider errors, the degraded response is cached for the full 5m — but it carries ADR-0002's `failures` array, so a stale-partial hit is visibly partial rather than silently thin. A non-empty `failures` array does **not** skip the cache (that would turn a flapping provider into a permanent cache bypass); the 502 total-timeout path is never cached, because it raises before any `set`.
- **Values are returned by reference.** Handlers hand the same response object to every caller within the TTL. The rule: **mutate before caching, never after.**
- **`--workers N` degrades the hit rate toward 1/N, not correctness** — every entry is an equally valid snapshot of the same upstream.

---

## Section identity (decided A)

A Section's identity is the **tuple `(provider, id)`**, not `id` alone.

- `Section.id` is **scoped within a provider**. Cross-provider id collisions are expected and harmless (e.g. both `uaflix` and `animeua` have a section with `id="anime"`).
- The wire format reflects this: `/api/browse?provider=...&section=...` requires both; `/api/sections` returns a list of `{ provider, name, sections: [...] }`.
- No global id registry, no namespace prefix. Cross-provider semantic merging is handled by `/api/search`, not by Section-id equality.

---

## Failure semantics for `/api/search` (decided on Q21+)

`/api/search` aggregates many providers in parallel under a 12-second total budget. Partial failures are the **common case**, not the exception — a single slow provider must not poison the whole response, but a totally dead aggregator must still surface a clean error. The reasoning behind each decision lives in [`docs/adr/0002-failure-semantics.md`](docs/adr/0002-failure-semantics.md).

### Wire shape (decision-rich fields)

> **Shipped-vs-decided (2026-08-08 sync):** the wire shape below is **out of date**. Since issue #71, `SearchResponse` ships `groups: list[SearchGroup]` (merged cross-provider cards, each with `group_key`, canonical fields, and a `sources: list[SearchResult]` list) instead of a flat top-level `results`. `ProviderFailure` still ships as documented. The ADR-0002 *semantics* (Q21–Q26) are unchanged and still authoritative; only the shape of the results container moved.

```python
class ProviderFailure(BaseModel):
    provider: str       # provider id, e.g. "uakino"
    code: str           # "upstream_unreachable" | "timeout" | (future codes)
    message: str        # human-readable, surfaced for debugging only

class SearchResponse(BaseModel):
    query: str
    groups: list[SearchGroup]   # issue #71: merged cross-provider cards
    failures: list[ProviderFailure] = []   # omitted from JSON when empty
```

### Q21–Q26 decisions

- **Q21 — HTTP code for partial failure: 200 OK.** `failures` array carries per-provider errors alongside any successful results. Rejected: 207 Multi-Status (WebDAV idiom, no PS4 client support), 502/504 (loses partial results, defeats the budget), per-provider status envelope (restructures the existing flat results shape, breaks v3 §3.2).
- **Q22 — Per-provider timeout: enforced at the network layer.** The shared `httpx.AsyncClient` carries `httpx.Timeout(upstream_timeout_s=8)`, so each upstream request self-cancels at 8s. The outer `asyncio.wait_for(..., timeout=12)` is the overall backstop, not a per-provider knob. Per-task `asyncio.wait_for` would duplicate the policy and race the httpx one.
- **Q23 — Error envelope: `{provider, code, message}` only.** No `retryable` field (no backend retry policy today), no per-failure latency, no stack traces. `code` is derived from the exception class (`"timeout"` for `httpx.TimeoutException` / `asyncio.TimeoutError`, `"upstream_unreachable"` otherwise).
- **Q24 — Retry policy: no backend retry.** LAN topology, PS4 user can re-press. A `tenacity`-style backoff is YAGNI; if a specific provider proves transient-flaky, the fix belongs in that provider.
- **Q25 — Total budget: `asyncio.gather(..., return_exceptions=True)` plus outer `asyncio.wait_for(timeout=12)`.** `return_exceptions=True` captures escaping exceptions per-task (cancellation, unhandled errors) instead of aborting the batch. When the overall 12s fires: any in-flight providers that didn't complete get a synthetic `code="timeout"` row; if **no** provider returned any results, the route returns **502** with `ErrorResponse(error="search_timeout", ...)` instead. Total timeout with nothing usable is a real server-side problem; partial results always come back as 200.
- **Q26 — Scope: `/api/search` only.** `/api/content` and `/api/stream` aggregate exactly one provider — there is nothing to be partial about. Their existing semantics (any exception → 502 with `error="upstream_unreachable"`) stay unchanged. The 4xx codes on `/api/stream` (`not_found`, `invalid_translation`, `translation_missing`) are client-side semantics, not upstream health, and stay distinct from `502`.

### Search-specific non-features (each rejected with alternatives evaluated)

- **No 207 Multi-Status** — WebDAV idiom, no client tooling, duplicates the `provider` field already on each `SearchResult`.
- **No `retryable: bool` on `ProviderFailure`** — no backend retry today; a field that always says `false` is noise.
- **No per-item `status` field on `SearchResult`** — results are not failures; conflating them prevents future per-item health.
- **No `failures: []` emitted when empty** — Pydantic omission (`exclude_unset`-style) is the floor; an empty array is indistinguishable from "no providers failed" and adds payload bytes.
- **No per-search-result kind probe for bare-URL cards (issue #244,
  decision (a) — accepted, 2026-08-16).** kinovezha's search cards
  carry NO kind signal — bare `/N-slug.html` links with only
  title/year/original-title (unlike eneyida's season label) — so they
  classify as `movie` by default. Browse is unaffected (the section-
  kind override forces the form); the search card still opens the
  authoritative series detail (`content()` is authoritative, with the
  seasons). A per-result content probe was rejected (blows the 12 s
  search budget across 19 providers); lazy reclassification on open
  was rejected (search responses are cached at the 5 m TTL, so the
  list label would not update, and mutating group projections post-
  open touches the shared merge/resolution machinery for a cosmetic
  label). Accepted: the card may read "movie" until opened.

If any of these start biting in production, each can be added as a **non-breaking optional field** (`retryable: bool | None = None`, `failures_always: bool = False`, etc.) without touching existing clients. The current shape is the floor, not the ceiling.

### What `failures` means to PS4

- A row with `code="timeout"` means that provider's contribution did not arrive in time; the operator can see which sites are slow today without parsing free-text `message`s.
- A row with `code="upstream_unreachable"` means the provider threw (network error, parse error, redirect to disallowed host, anything else); the PS4 client can show a "skipped N providers" hint.
- A provider that returns `[]` legitimately (no title match) is **not** in `failures` — empty results and unreachable providers are distinguishable.
- `/api/providers` health (`status: ok | degraded | down`) is unchanged and still authoritative for the per-provider dashboard; `failures` is the per-response surface, not a replacement.

### Out of scope for this ADR

- **Cache contract** — TTL, key shape, invalidation. Reserved for a separate session.
- **Provider lifecycle** — registration, hot-reload, retirement. Reserved for a separate session.
- **Failure attribution on `/api/content` and `/api/stream`** — single-provider routes stay on their existing 502 semantics.
