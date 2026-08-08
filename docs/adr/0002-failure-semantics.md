# Failure semantics for `/api/search`

The search endpoint aggregates 19 upstream providers in parallel under a
12-second total budget. Partial failures are the common case, not the
exception: a single slow provider must not poison the whole response, but a
totally dead aggregator must still surface a clean error. The model's
authoritative terms live in [CONTEXT.md](../../CONTEXT.md); this ADR records
*why* this shape, not *what* it is.

## Status

Accepted (2026-08-02, after grilling session — Q21+).

## Context

`/api/search` fans out to every registered provider (or a single one when
`?provider=` is given), each running `BaseProvider.search()` against its
upstream site. The route lives in `backend/cs_uk_api/main.py` (around
`/api/search`); per-provider work is wrapped in an inner `run()` that swallows
exceptions and returns `[]`, then collected via
`asyncio.gather(...)` with an outer `asyncio.wait_for(..., timeout=12)`. The
shared `httpx.AsyncClient` carries an `httpx.Timeout(8)` so each upstream
request self-cancels after 8s.

The 19 providers span the long tail of the Ukrainian web: site outages,
Cloudflare challenges, JS-only renderers (uakino, marked down at startup
when Chromium is missing), and slow cold pages. In practice, a search over
all providers routinely sees one or two failures per call. Today the route
silently swallows them and returns `results: []` for the failed providers —
the PS4 client cannot distinguish "no matches" from "3 providers timed
out, 16 returned nothing". Health is recorded per provider (`TRACKER`) but
not surfaced to the caller.

Two ambiguities in the current code were resolved by this grilling:

- `asyncio.gather(...)` is invoked **without** `return_exceptions=True`, so
  any exception that escapes `run()` aborts the whole gather. The outer
  `asyncio.wait_for` then races with `httpx.Timeout`: a provider whose
  upstream request exceeds 8s returns `[]` via `run()`'s catch; a provider
  whose `search()` body itself hangs (post-network parse/extract) trips the
  overall `wait_for` and 500s the whole route.
- `ProviderError` carries `(code, message)` but the route doesn't
  differentiate `ProviderError` from generic `Exception` in `/api/search` —
  every failure collapses into the same swallowed `[], []` row.

`/api/content` and `/api/stream` route to a single provider. They already
follow a simple semantics: any exception → 502 with
`ErrorResponse(error="upstream_unreachable", message=...)`. The failure
envelope this ADR introduces for `/api/search` is deliberately scoped to
multi-provider aggregation; single-provider routes are unchanged.

## Decision

`/api/search` returns **200 OK with a `failures` array** whenever at least
one provider's contribution failed; it returns **502 with
`ErrorResponse(error="search_timeout", ...)`** only when the overall 12s
budget was exceeded by *all* providers (i.e. nothing usable came back in
time). The wire shape (decision-rich fields only — full schema in code):

```python
class ProviderFailure(BaseModel):
    provider: str       # provider id, e.g. "uakino"
    code: str           # machine-readable, e.g. "upstream_unreachable", "timeout"
    message: str        # human-readable, surfaced for debugging only

class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    failures: list[ProviderFailure] = []   # omitted from JSON when empty
```

Concrete rules:

- A provider that returns `[]` from `search()` with no exception is **not**
  a failure — empty results are a legitimate answer (no title match).
- A provider whose `search()` raises **anything** (caught by the inner
  `run()`) contributes a `ProviderFailure` entry with `code` derived from
  the exception class (`"timeout"` for `httpx.TimeoutException` /
  `asyncio.TimeoutError`, `"upstream_unreachable"` otherwise) and
  `message=str(exc)`.
- `asyncio.gather(..., return_exceptions=True)` so that an escaping
  exception (cancelled task, unhandled error) is captured per-task instead
  of aborting the whole batch.
- `asyncio.wait_for(..., timeout=12)` stays as the overall backstop. If it
  fires, any in-flight providers that didn't complete get a synthetic
  `ProviderFailure(provider=..., code="timeout")` row before the response
  is assembled. If the overall timeout fires AND no provider returned any
  results, the route returns **502** with
  `ErrorResponse(error="search_timeout", message=...)` instead — total
  failure is genuinely a server-side problem and deserves a real error code.
- `failures: []` is **omitted from the JSON** when no provider failed
  (Pydantic `exclude_unset` semantics on the model); when at least one
  failed, the field is present even if all providers still produced
  results. Minimum surface: only carry the field when it carries signal.

## Considered Options

- **207 Multi-Status** (WebDAV-style per-resource status): rejected. The
  results list already carries `provider` per item; a separate status array
  duplicates that signal. 207 has no client tooling, no PS4 client idiom,
  and only obscures the simple "some providers failed" message.
- **502/504 with empty body on partial failure**: rejected. The whole point
  of the 12s budget is to surface partial results; throwing them away
  defeats the design.
- **200 OK with per-provider status array** (i.e. a `providers: [{id,
  status, results}]` envelope that mirrors `gather`'s shape): rejected.
  Restructures the existing flat `results: list[SearchResult]` payload,
  breaks the v3 spec §3.2 contract (grouped cards), and forces every PS4
  client to demux before rendering. The `failures` field is additive — old
  clients ignoring it see exactly today's shape.
- **Per-provider HTTP code per item** (each `SearchResult` carrying a
  `status: "ok" | "timeout"` field): rejected. Results are not failures;
  conflating them prevents the future evolution where providers report
  per-item health. Two arrays, one for items and one for failures, keep
  each side clean.
- **Per-provider `retryable: bool` field** on `ProviderFailure`: rejected.
  No backend retry exists today; LAN, PS4 user re-presses. A field that
  always says `false` (or always says `true`) adds noise; the question of
  whether to retry is a client policy decision, not a wire fact.
- **Per-provider `asyncio.wait_for` wrapping each `run(p)`** (a per-task
  `timeout=upstream_timeout_s`): rejected. The shared `httpx.AsyncClient`
  already enforces 8s per request via `httpx.Timeout`; layering a second
  asyncio timeout per task duplicates the policy and creates a race (which
  one wins?). The existing network-layer timeout is the right place.
- **Per-provider timeout shorter than overall budget** (e.g. 4s per
  provider, 12s total): rejected. With 19 providers running in parallel,
  per-task timeout would force a stop on a legitimate slow site; the
  shared budget is the right knob. The httpx 8s limit plus the 12s overall
  budget already gives the right shape.
- **Backend retry on transient failure** (`tenacity` exponential backoff):
  rejected. LAN topology, fast user re-press, no observed transient that
  one retry would catch but a re-press would not. YAGNI; if a provider
  proves transient-flaky, the fix belongs in that provider, not in the
  aggregator.
- **Apply failure semantics to `/api/content` and `/api/stream` too**:
  rejected. Those routes aggregate exactly one provider; there is nothing
  to be partial about. Their existing semantics (any exception → 502 with
  `ErrorResponse(error="upstream_unreachable", ...)`) are the right shape
  and stay unchanged. Code `404` paths (`not_found`, `invalid_translation`,
  `translation_missing`) on `/api/stream` are client-side semantics, not
  upstream health, and stay distinct from `502`.
- **Always emit `failures: []` field even when empty**: rejected. An empty
  field is indistinguishable from "no providers failed"; emitting it adds
  payload bytes and a "did the client forget to check?" footgun. Pydantic's
  `exclude_unset`-style omission is the floor, not the ceiling.

## Consequences

- **Wire change, additive.** `/api/search` response gains an optional
  `failures` field. Existing PS4 clients that don't know about it keep
  working unchanged (today's shape is a strict subset).
- **Per-provider attribution.** The PS4 client can now show a "skipped N
  providers" hint and the operator can log per-provider failure codes
  without parsing free-text messages.
- **Timeout path becomes well-defined.** The pre-ADR behaviour was "12s
  budget exceeded → 500 with `internal: 'TimeoutError(...)'` via the global
  exception handler". Post-ADR: 12s exceeded with partial results → 200
  with `failures[].code="timeout"`; 12s exceeded with no results → 502
  with `error="search_timeout"`. No more 500 for a slow provider.
- **Health tracker still authoritative.** `TRACKER.record()` continues to
  mark provider health per call; the new `failures` array is the
  per-response surface, not a replacement for the `/api/providers` health
  data.
- **Scope boundary.** `/api/content`, `/api/stream`, `/api/browse` remain
  on their existing single-provider semantics. No `failures` field on
  those routes. If multi-provider aggregation ever lands on `/api/content`
  (it does not), this ADR is the reference, not a precedent.
- **Future evolution.** Adding `retryable: bool`, `latency_ms`, or per-item
  status is a non-breaking optional field on `ProviderFailure` /
  `SearchResult`. The current shape is the floor, not the ceiling.
- **Test seams.** New tests pin the four cases: (a) all providers succeed
  → `failures` omitted; (b) some succeed, some fail → `failures` present,
  results populated; (c) all fail (different codes) → 200 with empty
  results, populated `failures`; (d) overall budget exceeded → either 200
  with synthetic `timeout` rows or 502 `search_timeout` depending on
  whether any provider returned anything.

## Amendment: subscription-gate verdict `gated` (2026-08-08)

### Context

BambooUA serves subscription-gated titles ("Для підписників") a sponsor
promo clip (`/uploads/be_sponsors.mp4`) instead of the real video — the
real m3u8 is never present on the page for non-subscribers. The provider
previously resolved such titles to the promo clip, so the PS4 player
played an advertisement instead of the content. Gating is only visible
on the content page (the `const playlist` block), never on listing
cards, so detection lives in `content()`/`stream()`, not in the scrapers
that produce listings.

### Decision

A gated item raises `ProviderError("gated", ...)` from `content()` (when
EVERY playable file in the playlist is the placeholder) and `stream()`
(when the resolved file is the placeholder). The verdict is **client-side
semantics, not an upstream-health signal** — a refusal, not a failure:

- `/api/content` and `/api/stream` answer **404** with
  `ErrorResponse(error="gated", ...)`, joining `not_found` /
  `invalid_translation` / `translation_missing` as a deliberate
  unavailability — never `502 upstream_unreachable`. The Jellyfin facade
  degrades the same item to its standing 404 ("item unavailable").
- The `gated` verdict does **not** move the health tracker: the route
  handlers and the facade's `_resolve_stream` translate it to 404 BEFORE
  `TRACKER.record(ok=False)` would run. A gated title is the site's
  business model, not a broken provider.
- Catalog filtering: `can_gate` providers' cards are resolved once per
  cache window (`catalog_state.filter_gated_items`, run from
  `load_home()`, `/api/search`, `/api/browse`) and known-gated cards are
  dropped BEFORE merging rows. A merged title keeps its working sources;
  a title available only through a gated source disappears from the
  catalog (per the product decision: hide, don't play the promo).
- Verdict caching: `gated_cache` stores `True` (gated) with the long
  `cache_gated_s` TTL (24h) — an un-gated title stays hidden up to that
  bound; `False` (known-good) uses `cache_content_s`, so a title that
  un-gates re-enters the catalog when its content cache expires.
- A sweep timeout degrades to keeping the cards (no partial drop based
  on a guessed verdict); `stream()` still refuses gated items on every
  path, so the promo clip never plays even then.

### Consequences

- The sponsor promo clip is never handed to a player: `stream()` refuses
  it on every path (native route, Jellyfin facade, stale bookmarks).
- `gated` is a new client-visible 404 code; no existing client needs a
  change (404 was already a tolerated "skip this item").
- A merged title survives with its remaining sources; only gated-only
  titles vanish from the catalog — the product decision "hide from
  search/browse".
- The first cold `/api/home` build after deploy is slower (the sweep
  resolves BambooUA cards once, ~1 content fetch per card, parallel,
  cached afterwards) — accepted trade-off for instant hiding.

## References

- [`CONTEXT.md`](../../CONTEXT.md) — glossary of the failure-envelope
  shape, the per-route scope, and the deliberate non-features.
- [`docs/superpowers/specs/2026-08-02-domain-model-decisions.md`](../../superpowers/specs/2026-08-02-domain-model-decisions.md)
  — the post-grilling spec that listed this work as Out of Scope.
- `backend/cs_uk_api/main.py` (`/api/search` route, `run()` inner
  closure, `asyncio.wait_for` overall budget).
- `backend/cs_uk_api/providers/base.py` — `BaseProvider.search()`,
  `ProviderError(code, message)`, `BaseProvider.can_gate`.
- `backend/cs_uk_api/providers/bambooua.py` — the `gated` verdict
  source (sponsor-placeholder detection in `content()`/`stream()`).
- `backend/cs_uk_api/catalog_state.py` — `gated_cache` +
  `filter_gated_items` (the catalog sweep in `load_home()`).
- `backend/cs_uk_api/jellyfin/router.py` — `_resolve_stream` (gated →
  404 without health impact).
- `backend/cs_uk_api/config.py` — `SETTINGS.upstream_timeout_s=8`,
  `SETTINGS.search_total_timeout_s=12`, `SETTINGS.cache_gated_s`.
- `backend/cs_uk_api/health.py` — `TRACKER` (provider health, unchanged
  by this ADR).
- Obsolete: `docs/superpowers/specs/2026-08-01-ps4-uk-stream-design.md`
  §3.5 (search response shape) — to be updated in a spec revision.