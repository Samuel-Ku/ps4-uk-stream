# ADR-0010: Catalog snapshot ownership and the sanctioned invalidations

The catalog's derived state — the last good home snapshot (rows + the group resolution map), the group index, and the deep-row pools — gets **one owner** with a single apply step. Search registrations **survive** a snapshot replacement, bounded by the search TTL that created them rather than by the snapshot's cycle (see *Decision* for which of the two bounds is the timer). Two deliberate cache clears in the warm path are **sanctioned event-driven invalidations** and must go through that owner. This revisits ADR-0003's invalidation clause and its "no persisted schema" wording.

## Status

Accepted (2026-09-12, grilling session of the 2026-09-12 architecture review). Supersedes ADR-0003's claim that invalidation is **TTL-only**; see *Consequences* for the wording that stops being true.

## Context

ADR-0003 decided that "invalidation is TTL-only — no flush endpoint, no event-driven invalidation", on the reasoning that providers are scraped websites with no webhook to invalidate *from*. That reasoning holds for provider responses and still does. It does not describe the catalog layer, which grew later and invalidates itself **on purpose**:

- `_catalog_state/warm.py` clears the home cache when the profile warm adds profiles (so a new taste profile surfaces in the rows) and again after an LLM taste refresh (same reason, stated in the code: "the home rows are BUILT from the active profile").
- `_catalog_state/snapshot.py` clears the snapshot-anchored deep-row pools whenever a rebuild replaces the snapshot (spec #305).

None of this is a flush endpoint and none of it is triggered by an upstream event, so the ADR was not *wrong* so much as scoped to a layer that no longer contains all the invalidation.

The trigger for writing this down is issue #420. A group key returned by search answered `/Items/{id}` with `404 item_unavailable` seconds after it had resolved, and the explanation took a journal reconstruction, because:

1. The resolution map, the group index, the snapshot and the pools are four pieces of derived state that must move together, and they were written from **ten sites across three modules** — `snapshot.py` (persisted cold start, and `_cache_home`), `resolution.py` (search registration) and `warm.py` (three clears). `snapshot.py` asserted the invariant it wanted in a comment ("single mutation site so map and index cannot diverge") while `_populate_group_index` had two whole-replacement callers and search added a third writer.
2. Two of those sites replace the derived state **whole** rather than merging, so a live search registration is destroyed with no trace. The persisted cold-start path (ticket #269) replaces the index *first*, before any rebuild counts anything.
3. Nothing owned the decision of what survives a replacement, so nothing carried search registrations forward and nothing re-registered on read.

This was reproduced live on the deployment and measured: a search registered 16 keys, the next replacement logged exactly `dropped 16 search-registered key(s)`, and the subsequent `/Items/{id}` read answered 404 in 0 ms (issue #420). The invalidation frequency is activity-driven — the warm clears are why a live registration had a shelf life of seconds to minutes rather than the 30-minute snapshot TTL an operator would predict.

## Decision

- **One owner.** The catalog snapshot and everything derived from it (the resolution map, the group index, the deep-row pools) is owned by a single catalog-snapshot component with **one apply step**. Both replacement sites — the persisted cold start and a finished rebuild — go through it. This is the "one owner per piece of state" rule applied to the catalog, and it is what makes the next bullet expressible.
- **Search registrations are carried forward.** When a replacement lands, keys registered by search are merged into the new map and index rather than dropped. Their lifetime is the **search** TTL that created them, *not* the snapshot's cycle — with one precision, because only one of the two bounds is a timer: a registration is **guaranteed to survive replacements for that window**, and it is the **next replacement** that enforces the TTL, since an aged-out registration is retired by the apply step and never by a read; in the absence of any replacement, the resolution map's own cache lifetime is the outer bound. The promise a search makes to a client is that its results stay actionable for as long as the results themselves are cached; a snapshot rebuild is an internal event and must not shorten that.
- **Two invalidations are sanctioned, and they go through the owner.** The profile warm's clear and the taste-refresh clear are legitimate: the rows are derived from the active profile, so a stale profile in a fresh snapshot is a correctness bug. They are recorded here so the next reader does not "fix" them by deleting them, and they are reachable only through the owner — no module clears the snapshot behind its back.
- **The derived state is not separately written.** `resolve_group` and friends read through the owner; the index is not merged by a second module. A caller that appears to need a second write path is a signal that the owner's interface is missing an operation.
- **The read side is two surfaces, not ten accessors.** Resolution is ONE typed value — the card, the row kind, the ordered source cards, and the `origin` layer that answered — and iteration is a separate `snapshot_entries()` view of the **snapshot layer only**, so a route that counts, shelves or matches by profile cannot mix a registration into snapshot-only rows. Precedence is stated once, on the owner: the snapshot wins the card and the row kind for a key both layers hold, and `sources` is the union with the snapshot's providers first. The old split accessors (`card_for_group`, `poster_url_for_group`, `view_row_type_for_group`, `year_for_group`, `genres_for_group`, `group_sources`, `first_source`, `group_source`, `group_entries`, `home_items_in_index_order`) are retired outright rather than deprecated: they let a caller ask the index, then the map, then the map again, which is the shape the #420 diagnosis had to unpick.
- **Behaviour deliberately unchanged**: the persisted snapshot still serves at any age on a cold start with a rebuild in the background (ticket #269), and the TTLs in the table stand. This ADR changes who owns the writes and what survives them, not when a client sees fresh content. The persisted FILE keeps its shape too: what is written is the snapshot's own map, never the merged one — a registration is search-TTL state, so persisting it would resurrect an expired promise across a restart and grow the file with a session's searches.

## Consequences

- **ADR-0003's "TTL-only" is superseded for the catalog layer.** Its invalidation clause now reads: TTL-only *for provider responses and endpoint caches*; the catalog snapshot additionally has the two sanctioned invalidations above, owned centrally. ADR-0003's other decisions — key format, per-endpoint TTLs, scope, un-cached endpoints, the deterministic-404 negative cache — are untouched and remain binding.
- **ADR-0003's "no persisted schema" wording was already exceeded** by the persisted home snapshot (ticket #269, `{"v": 1, "rows": [...], "sources": {...}}`) and by viewer state (spec #323 via `versioned_store.py`). CONTEXT.md's versioning section records that exception; this ADR only makes the earlier document's scope honest rather than re-deciding it.
- **`item_unavailable` stops covering a case it never described.** A hard 404 for a group key meant "cold cache"; it also silently meant "a rebuild came through". The two remain distinguishable after this change: a cold key has never been seen, a carried-forward key is resolvable, and the ticket #224 degradation continues to cover the third case (known card, transient upstream failure).
- **The replacement becomes testable without a live host.** The carry-forward rule is a property of one apply step, so "a wipe must not drop a live registration" is a unit test rather than a race reproduced against a deployment — which is how #420 had to be confirmed.
- **The five-second-cadence invalidation stays, now visibly.** Carrying registrations forward removes the correctness damage of frequent invalidation without pretending the invalidations do not happen; if the profile warm ever clears on every build, that is now a visible, owned event rather than a silent wipe.
- **The retirement log is a replacement-time signal, not a timer.** `group index replaced: N registered key(s) retired` fires when a replacement retires registrations that have aged out, and stays silent for the whole life of a key that never meets a replacement. Read it as "a replacement retired stale registrations", not as "registrations live exactly the search TTL".
- **A new module may not be needed.** The owner can start as one apply function where the state already lives, with the extraction to a named component following only if the interface earns it. Ownership is the decision; file layout is not.
- **"Which layer answered" becomes a value, not an inference.** A caller that used to read a `None` card and guess whether the key was unknown or merely registration-only now reads `origin`. The iteration surface is deliberately narrower than the index, so a registration is invisible to counts, person filmography, the similar shelf and the genre rails by construction rather than by every call site remembering to filter `home_item is None`.

## References

- Issue [#420](https://github.com/Samuel-Ku/ps4-uk-stream/issues/420) — the confirmed live reproduction, with the measured drop and the journal timeline.
- PRs [#422](https://github.com/Samuel-Ku/ps4-uk-stream/pull/422) / [#423](https://github.com/Samuel-Ku/ps4-uk-stream/pull/423) — the instrumentation that made the drop countable, and the site correction it forced.
- [`docs/test-artifacts/openclaw-home-smoke-2026-09-12.md`](../test-artifacts/openclaw-home-smoke-2026-09-12.md) — the deployment record and finding 2.
- [ADR-0003](0003-cache-contract.md) — the cache contract this revisits.
- [`CONTEXT.md`](../../CONTEXT.md) — §Cache contract (§Invalidation, §Persisted home snapshot), and §Home composition.
