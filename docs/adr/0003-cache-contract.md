# Cache contract: TTL-only, in-memory, no persisted schema

The backend caches listing and metadata responses in per-endpoint in-memory TTL stores keyed by a flat `{namespace}:{discriminants}` string, expires them by TTL alone, and never persists a value that carries a schema — so a code change invalidates the cache by restarting the process, and no version token is needed. The operative TTL table and key format live in [CONTEXT.md](../../CONTEXT.md); this ADR records *why* this shape, not *what* it is.

## Status

Accepted (2026-08-02, after grilling session Q27–Q33).

## Context

The v2 design spec never defined a cache contract. An implementation grew organically: `TtlCache` (a ~35-line in-memory dict guarded by a `threading.Lock`) instantiated four times in `main.py` plus once in `poster_proxy.py`, with three environment-tunable TTLs (`CS_UK_CACHE_SEARCH` / `_CONTENT` / `_POSTER`). `docs/status.md` advertises this as "TTL cache (5m search / 30m content / 1h posters)", which undercounts it — `/api/browse` and the Russian-content blocklist are also cached, and posters have a second 7-day disk layer on *both* sides of the LAN (issue #54).

Nothing about that arrangement was ever argued. It was also listed as explicitly deferred in [`docs/superpowers/specs/2026-08-02-domain-model-decisions.md`](../superpowers/specs/2026-08-02-domain-model-decisions.md) ("Out of Scope — **Cache contract**: TTL, key shape, invalidation. Reserved for next grilling session"), and ADR-0001 left a standing obligation against it ("`/api/search` cache key gains `form` and `styles` axes"). Two questions in particular had no answer on record:

- **Does a schema change poison the cache?** Model B (ADR-0001) replaces `type: MediaType` with `form` + `styles` on every cached `ContentResponse`. If cached values outlive the code that produced them, that migration serves items in a shape the client can no longer parse.
- **Is anything protecting against concurrent misses?** Nothing does. `TtlCache`'s lock guards the dict, not the fetch; N simultaneous misses on one key produce N upstream fan-outs.

A grilling session walked seven dependent decisions (Q27 key shape; Q28 per-endpoint TTLs; Q29 invalidation; Q30 stampede protection; Q31 scope; Q32 what stays un-cached; Q33 versioning). The deployment context that drives most answers: **one household, one host, one uvicorn process, LAN-only, at most a couple of concurrent clients.**

## Decision

Ratify the current shape, with the reasoning made explicit and two forward obligations recorded. Its surface rules:

- **Key format is a flat colon-joined `{namespace}:{discriminants…}` string**, where the discriminants are *every* request parameter that can change the response. The provider axis is already present everywhere it matters — explicitly in `search:` / `browse:` keys, and implicitly in `content:` keys because `content_id` is itself `{provider}:{external_id}`. No structured-tuple key, no query normalization.
- **Per-endpoint TTLs stay as configured**: search 5m, browse 5m (sharing the search knob), content 30m, blocklist 30m, poster 1h in memory / 7d on disk. Full table with per-endpoint rationale in CONTEXT.md.
- **Invalidation is TTL-only.** No flush endpoint, no event-driven invalidation. A process restart is the global flush, and it is free because the store is in-memory.
- **No stampede protection.** No per-key single-flight lock. The fan-out this would prevent requires concurrent misses on an identical key, which this deployment does not produce.
- **Scope is in-memory, single-process.** No Redis, no SQLite, no shared memory. The poster disk layer is the deliberate exception — it is cross-process and cross-restart by design.
- **Un-cached, permanently**: `/api/stream/{id}` (short-lived upstream URLs), `/api/providers` (embeds live health), `/api/sections` (a dict read). **Error responses are never cached** — the sole negative cache is the Russian-content 404, which is a deterministic property of the item, not a failure.
- **No version token in cache keys.** Instead, the invariant that makes versioning unnecessary: **no value carrying a domain schema is ever persisted beyond process lifetime.** A schema change is a code change is a restart is an empty cache. The two poster disk caches satisfy the invariant because they store opaque image bytes under a content-addressed key.

Definitions, the TTL table, and the key format live in [CONTEXT.md](../../CONTEXT.md).

## Considered Options

- **Structured `{endpoint, params, version}` tuple keys** (instead of flat strings): rejected. Requires a canonical serialization to be usable as a dict key, which is a flat string again — with an extra layer that can disagree with itself. The flat form is already unambiguous because each namespace has its own store.
- **A `{version}:` prefix on every key, bumped by hand on schema changes**: rejected. It is a provable no-op — an in-memory cache cannot outlive the process that defines the schema, so the prefix can never differ from the one that wrote the entry. Worse, it *looks* like a safety mechanism, so the first contributor who forgets to bump it gets false confidence rather than a visible failure.
- **A schema-derived key prefix** (`hash(ContentResponse.model_json_schema())`): rejected for the same reason, minus the rot. Auto-correct, self-maintaining, and still a no-op today; it buys nothing until something is persisted, at which point it becomes the right answer.
- **Normalizing `q` before keying** (case-fold, collapse whitespace): rejected. The normalizer would have to mirror what 19 independent scrapers do with the query; where it disagrees, a cache hit returns results for a *different* search — a correctness bug traded for a memory saving. Duplicate entries are the cheap failure.
- **Per-provider search caching** (cache each provider's slice, assemble the response from parts): rejected for now. It would make `provider=all` and `provider=animeua` share warmth, and would fix the "partial failure cached as complete" wart below. It also requires the aggregation step to become cache-aware and makes each response a mix of entries with different ages. Deferred to issue #83 if the wart bites; genuinely non-breaking to add.
- **Per-key `asyncio.Lock` single-flight**: rejected. Correct, and solves a problem this deployment does not have. Costs an unbounded lock dictionary (or a cleanup race on deleting locks), and couples the synchronous `TtlCache` (a `threading.Lock`) to the event loop. `/api/search` fan-out is already bounded by the 12s `asyncio.wait_for` budget and the shared `httpx` client's connection limits.
- **`asyncio.Future`-based coalescing** (first caller fetches, the rest await its result): rejected. Same verdict, plus it changes the error path — every waiter now inherits one upstream's exception instead of retrying independently.
- **Probabilistic early expiration (XFetch)**: rejected. Solves thundering-herd-on-expiry for high-QPS caches. This backend's QPS is a person pressing buttons on a gamepad.
- **Redis as the cache backend**: rejected. An extra daemon, an extra failure mode to handle on every `get`, and a serialization boundary that forces a Pydantic ↔ JSON round-trip on every hit — in exchange for sharing state between processes that do not exist.
- **SQLite as the cache backend**: rejected. Same trade as Redis, plus disk IO on the hot path, minus the daemon.
- **An authenticated `POST /api/cache/flush` admin endpoint**: rejected. A state-changing endpoint on a LAN box with no auth story, to replicate something `systemctl restart` already does — and restarting is already how the operator deploys.
- **Event-driven invalidation** (invalidate `content:{id}` when a provider publishes an episode): rejected as impossible, not merely undesirable. The providers are scraped websites; there are no webhooks, no ETags worth trusting, and no push channel.
- **Caching `/api/providers`**: rejected. The response embeds `TRACKER.status(p.id)`, live health. A TTL in front of it delays exactly the signal the endpoint exists to deliver — a provider that just went down would keep reporting `ok`.
- **Caching `/api/sections`**: rejected. The handler is a list comprehension over an in-process registry that never changes at runtime. A cache in front of a dict lookup is pure overhead.
- **Caching `/api/stream/{id}`** (even briefly): rejected. Upstream URLs are session-scoped or token-signed; a hit after expiry returns a URL that fails at the player with no way for the backend to know. A miss costs one request; a stale hit costs a broken playback the user cannot distinguish from a dead provider.
- **Caching error responses** (negative-caching 502s for a few seconds to spare a flapping provider): rejected. It pins a transient blip for the whole TTL and hides recovery from the health tracker. The distinction this ADR draws: negative caching is permitted for **deterministic verdicts** (the Russian-content 404, which follows from the item's `country` field) and never for **failures**.

## Consequences

- **`/api/search` caches partial failures, but they are now self-describing.** When `provider=all` and one provider fails, the response is cached under the same key for 5 minutes — so a user who searches during a provider blip sees the degraded result set for the rest of the TTL. ADR-0002 materially improves this: the cached `SearchResponse` now carries a `failures: list[ProviderFailure]` array, so a stale-partial hit is *visibly* partial to the client rather than silently thin. Two consequences follow, and this ADR commits to both: (a) a response with a non-empty `failures` array is still cached — the alternative, skipping the cache on any failure, converts a flapping provider into a permanent cache bypass for every query; (b) the **502 path is never cached**, because ADR-0002's total-timeout-with-no-results case raises rather than returns, and errors do not reach a `set`.
- **Caches are unbounded in entry count.** `TtlCache` has no LRU and no max size, and expiry is lazy — an entry that is never re-requested holds its memory until something asks for it again. The realistic pressure point is the poster memory cache (1h TTL, up to 4 MB per entry per `poster_size_cap_bytes`); a long browsing session over a large catalog can accumulate hundreds of megabytes. Not fixed in this ADR because the contract is not the bottleneck — a size bound or a periodic sweep belongs in issue #83.
- **Cached values are live Python objects returned by reference.** Handlers return the same `SearchResponse` / `ContentResponse` instance to every caller within the TTL. Nothing mutates a response after caching today (`content()` sets `group_key` *before* the `set`), and this ADR makes that ordering a rule rather than an accident: **mutate before caching, never after.**
- **Running under `--workers N` degrades the hit rate, not correctness.** Each worker keeps its own store; the hit rate falls toward 1/N. Every entry is an equally valid snapshot of the same upstream, so there is no cross-worker consistency requirement to violate. Documented as an accepted degradation rather than a supported configuration.
- **The Model B migration needs no cache work beyond the key.** Because no schema-bearing value is persisted, the shape change lands with the restart that deploys it. The one outstanding obligation from ADR-0001 stands: the `/api/search` key must grow the `form` and `styles` axes when those filters ship, or filtered and unfiltered searches will collide on `search:{provider}:{q}`.
- **Persisting any domain object breaks this ADR's central invariant.** An offline catalog, a warm-start snapshot, or a disk-backed `content:` layer would all put schema-bearing values on disk across code versions — at which point a version token in the key stops being a no-op and becomes mandatory. This ADR must be revisited before any such feature, not after.
- **The poster path has two backend caches plus the facade's per-poster WebP memo** (backend memory 1h → backend disk 7d, then the facade's in-memory transcode memo) and no invalidation between them. This is safe only because a poster is immutable for a given URL — providers publish new art at a new URL. If a provider ever reuses a URL for changed art, that art is wrong for up to 7 days on the console. Accepted; the alternative is conditional requests against 19 scraped sites.
- **`docs/status.md` understates the cache.** Its "5m search / 30m content / 1h posters" line omits browse, the blocklist, and both disk layers. CONTEXT.md's table is now the authoritative version.

## References

- [`CONTEXT.md`](../../CONTEXT.md) — TTL table, cache key format, invalidation strategy, what is not cached.
- [`docs/adr/0001-catalog-taxonomy-form-and-style.md`](0001-catalog-taxonomy-form-and-style.md) — the standing obligation that the `/api/search` key gains `form` and `styles` axes.
- [`docs/superpowers/specs/2026-08-02-domain-model-decisions.md`](../superpowers/specs/2026-08-02-domain-model-decisions.md) "Out of Scope" — where this decision was deferred from.
- Issue #54 (`v3: Poster disk cache — 7 days on both sides`) — the two poster disk layers, their key derivation, and their TTL.
- Issue #83 (`Cache contract: implementation`) — the follow-on work: search key axes, and the entry-count bound if it becomes necessary.
- Issue #80 / ADR-0002 (`Failure semantics`) — owns the "partial failure cached as complete" question raised above.
