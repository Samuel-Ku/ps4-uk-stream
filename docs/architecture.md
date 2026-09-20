# Architecture notes — deepening wave (spec #309)

These notes record the seams the deepening wave (spec #309) introduced or
deepened, for future reviews. The wave's invariant: **zero wire-visible
behaviour change** — the Switchfin client and the backend suite stay green.

Two wave tickets are the anchor for this document:

- #319 (Arch T10) — provider contract step: the legacy `model_b_axes`
  mapping and the duplicated `MOVIE_SUFFIX` sentinel were removed; every
  consumer now speaks the typed vocabulary (`MediaForm` / `MediaStyle` /
  `MediaTypeStr`) directly.
- #320 (Arch T11) — profile store seam: one `install/get/warm` module for
  the facade's viewer state. (Since removed — #338, see §4.)
- #321 (Arch T12) — configuration seam: stores are constructed with a
  settings argument (re-instantiable); one `config.SETTINGS` binding and
  one test patch point.
- #329 (Row T1, spec #323) — row-kind registry: one declarative table
  (`cs_uk_api/row_kinds`) replaces the three private row-kind
  vocabularies (home title table, facade wire maps, deep-rows).

The wave's spec also called out two seams beyond its own tickets: a
typed catalog interface module and a dedicated wire-identity module.
Both have since landed on `master` — `cs_uk_api/catalog.py` (spec #309
step 2 / ticket #311, §1) and `cs_uk_api/wire_identity.py` (spec #340,
since extended by the playable-id grammar #374 and the `g3:` IMDb
identity #395, §2) — and this document describes them as shipped. The
few genuine "future direction" notes left are named as such.

---

## 1. Catalog seam — `cs_uk_api/_catalog_state`

`_catalog_state` is the single owner of the catalog's shared state and the
accessors over it. Both surfaces — the native `/api/*` routes (`main.py`)
and the Jellyfin facade (`jellyfin/router.py`) — read the same snapshot,
the same resolution map, and the same caches; there is one cache-key
shape, one TTL, one `clear()` per store.

Public accessors (the seams callers use, `_catalog_state.__all__`):

| Accessor | Purpose |
| --- | --- |
| `load_home()` | Build/return the merged home snapshot (30-min cache) |
| `get_home()` | Cached snapshot without triggering a build |
| `merged_search()` | Multi-provider search fan-out + merge + gating (native and facade share it) |
| `resolve_group(group_key)` | `g2:` key → `{provider: SearchResult}` map |
| `resolve_group_content(group_key)` | `g2:` key → one provider's `ContentResponse` (single-flight) |
| `peek_group_content(group_key)` | Cache-only read, never fetches |
| `register_search_groups(groups)` | Fold search results into the resolution map |
| `group_key_for_external(composite)` | `provider:external` → `g2:` key (resume rail) |
| `is_hard_unavailable(group_key)` | Gated/blocked/unknown verdict for the detail route |
| `filter_gated_items(...)` | Subscription-gate sweep (drops promo-clip-only cards) |

Cache-key formats (`search:{provider}:{q}:{form}:{styles}`, `content:…`,
`home:v1`, `home:sources:v1`) are private to the implementation — callers
never construct or re-derive them (spec: "cache keys and dict shapes stop
crossing the seam").

**Typed interface (landed, spec T2–T4 = ticket #311):** both surfaces
now import the small typed interface module `cs_uk_api/catalog.py` —
the native routes (`main.py`: `from . import catalog`) and the Jellyfin
facade (`jellyfin/router.py`, a 12-member import list) alike. The
module delegates to `_catalog_state` while keeping cache keys, dict
shapes and first-seen ordering off the seam; `_catalog_state.__all__`
above remains the implementation's full surface.

## 2. Wire identity — `cs_uk_api/wire_identity`

One module owns every id grammar the codebase used to re-derive by hand
(spec #340 moved it out of `merge.py` + `base.MOVIE_SUFFIX`; both
re-export the primitives for the established import paths, the edge
strictly one-way: merge → wire_identity):

- `group_key(alias, form, year)` — `g2:` + sha1 of the canonical
  (alias|form|year) triple. `GROUP_KEY_PREFIX = "g2:"` in
  `wire_identity.py` is the version bump point; a normalization-rule
  change is a `g2:` → `g3:` bump, never a migration.
- `group_key_from(title, form, year, item_id)` / `item_group_key(item)`
  — per-item stateless keys.
- **Two live key forms:** `g2:` digest keys and `g3:` IMDb keys —
  `group_key_from_imdb` emits `g3:<tt-number>` (spec #395), and when
  any merged member asserts a validated IMDb id, that `g3:` tt-key IS
  the canonical group key (one tt = one work, form-independent,
  cross-language).
- `merge.merge_results(items)` — the **single merge projection** (alias
  union-find, year-soft rule, the g3 IMDb tier) that produces
  `MergeGroup`s; every caller (search, home rows, sources map) feeds
  through it instead of re-implementing merge rules. `merge.py` still
  owns the matching/projection logic — only the id-grammar primitives
  re-export through it.

Movie wire ids end in the canonical sentinel `MOVIE_SUFFIX =
":__movie__"` (defined in `wire_identity.py`, re-exported by
`providers/base.py`): the sentinel was previously defined in 8 provider
files, then imported from `base`, and now lives beside the rest of the
grammar.

Episode wire ids carry `:s{season}e{episode}` tails; the episode-tail
grammar and the playable-id composition (`parse_playable_id`, spec
#374) are owned here too. The resume rail's reverse lookup
(`group_key_for_external`) understands both the episode wire id and the
uakino/animeon fallback shapes (ticket #234).

## 3. Provider vocabulary — `cs_uk_api/providers/base`

The provider base is the typed vocabulary every adapter speaks:

- `MediaForm = "movie" | "series"` and `MediaStyle = "anime" | "cartoon" |
  "dorama"` (`models.py`) — the Model B axes (ADR-0001). `SearchResult`
  and `ContentResponse` carry `form` + `styles`; `Section` declares its
  filter axes.
- `MediaTypeStr = Literal["movie", "series", "anime", "cartoon", "dorama"]`
  (`base.py`) — the internal classification value. The classification
  helpers typed to return it (`_type_from_url`, `_classify_from_tags`,
  the `_PATH_TYPE`/`_TAG_TYPE` tables) are per-adapter, not centralized
  in `base.py`: each HTML provider keeps its own (e.g. `bambooua.py`,
  `cikavaideya.py`, `doramyworld.py`). Three `# type: ignore[arg-type]`
  sites remain in the tree (`extractors/regex.py`,
  `providers/coaninet.py`, `providers/anitubeinua.py` — the latter two
  on `translations_level=` payloads); contract #319's zero-ignore goal
  was not fully reached.
- `ProviderError(code, message)` — the typed error vocabulary; `code` is
  a string value preserved on the wire (`"gated"`, `"not_found"`,
  `"parse_failed"`, `"upstream_unreachable"`, …), so a typo can't
  silently change consumer behaviour.

The contract step (#319) removed the legacy `model_b_axes` mapping and the
`_STYLE_BY_TYPE` table from `base.py`; literal call sites now inline the
typed values into constructors (`form="series", styles=frozenset({"anime"})`).

## 4. Profile store — REMOVED (`cs_uk_api/profile_store`, ticket #338)

The #320 (Arch T11) viewer-profile seam was **removed** (ticket #338).
Grep-verified: no production module ever imported it; its only consumer
was a test seeding fixture. Its two halves were already superseded:

- The viewer state it pretended to own (played/resume memory) is owned
  by the disk-backed resume and user-state stores in
  `_catalog_state/_stores.py` (`ResumeStore` via `record_playback` /
  `clear_playback`; `UserStateStore` for favorites / played marks / dub
  memory, spec #247/#257/#276).
- The content taste profiles always lived in the catalog stores
  (`_catalog_state._stores._profiles`, installed wholesale via the
  `install_profiles()` / read via `get_profiles()` accessors, spec
  #252).

There is deliberately no replacement seam: tests seed those stores
directly through the helpers above (conftest resets them before every
test), and `Settings.profile_file` remains declared with no consumer
(removing the knob is config-surface work outside #338's zone).

## 5. Configuration binding — `cs_uk_api/config`

The operator seam (T12):

- **One binding:** every module reads settings through the config module
  reference (`from . import config as _config; _config.SETTINGS.x`) —
  no module imports the value into its own binding. The single test patch
  point is `cs_uk_api.config.SETTINGS`.
- **Store construction:** the persisted stores are constructed with a
  settings-derived path and exposed as module singletons in
  `_catalog_state/_stores.py` (`_resume_store`, `_user_state_store`,
  `_snapshot_store_ref`), replaceable through the
  `install_resume_store()` / `install_snapshot_store()` seams (the
  snapshot-store pattern):
  - The TTL caches (home/search/browse/content/blocklist/row-deep/
    deep-page/gated/sources) are module-level `TtlCache` singletons in
    `_catalog_state/_stores.py`, their TTLs bound to
    `_config.SETTINGS.cache_*_s` at import.
  - The catalog snapshot's owned derived state (ADR-0010) is a module
    singleton `_CATALOG = CatalogState()` replaced through
    `install_catalog_state()`; `now` is the injectable clock.
  - `poster_proxy._cache` — TTL from the snapshot at construction.
    (The old `main._browse_cache` moved into `_stores.browse_cache`.)
  - Tests reset singletons directly (conftest's `_SHARED_STORES` plus
    `install_catalog_state(CatalogState())`) instead of import
    tricks; no positional `Settings(...)` reconstruction remains in tests
    (`dataclasses.replace` everywhere).

---

## 6. Versioned persistence — `cs_uk_api/versioned_store`

One deep module owns every persisted file (spec #323, Store T1 #324), so
the next store is a thin adapter (~20 lines), not a 4th copy-paste of a
byte-parallel implementation:

- **Wire envelope** `{"version": <int>, "data": <adapter payload>}` — the
  version token answers ADR-0003's obligation: once a domain value is
  persisted across process lifetime, a version token is mandatory.
- **`VersionedFileStore.load()`** — corrupt-safe ladder: missing /
  OSError / corrupt JSON / bad envelope / version mismatch / shape-
  invalid all degrade to `None` with a log line; never raises.
- **`VersionedFileStore.save()`** — atomic (`mkstemp` in the same
  directory + `os.replace`); never raises. The write body itself lives
  in the public **`atomic_write_text(path, text)`** primitive, shared by
  the envelope AND plain files.
- **`DebouncedSave`** — optional coalescing wrapper (`request` /
  `flush` / `close`) for adapters with high-frequency writes.

Adapters: the episode-rail sweep's
Markdown report (`sweep_episode_rail.write_report`, Store T3 #326) —
the previously non-atomic `open(path, "w")` write now goes through
`atomic_write_text`. (The profile-store adapter named here when the
section was written was removed with its module — §4.) The user-state /
snapshot / drift-baseline stores named in the spec landed with the
round-1 interface work: `user_state.py`, `snapshot_store.py`,
`resume_store.py` and `drift/baseline.py` all persist through
`VersionedFileStore`.

## 7. Provider probing — `cs_uk_api/probe`

One module names the three probe facts every provider probe (drift
monitor, episode-rail sweep, triage scripts) must agree on (spec #323,
Probe T1 #327):

- **Entry-point selection** — `select_entry_points(provider)`: the chain
  `newest_section` → declared sections → `search` (the fallback every
  provider has).
- **Wire-id splitting** — `split_wire_id(composite)`: `provider:external`
  → `(provider, external)`, splitting on the FIRST colon so episode wire
  ids (`uakino:6268:e1`) survive intact. The canonical copy moved to
  `wire_identity.py` (spec #340); `probe` re-exports it.
- **Verdict normalization** — `probe_error_verdict(exc)` /
  `is_probe_failure(verdict)`: `gated` is a policy outcome, NOT a
  failure (ADR-0002 decided in one place); `unavailable`/`error` are
  failures; unknown verdicts fail closed.

Consumers: the episode-rail sweep (`sweep_episode_rail.py`) — its row-
type and provider-attribution derivations (`is_episodic_item`,
`attributed_provider`) and its verdict vocabulary (OK / FAIL /
NO_EPISODES are the probe module's constants) replaced the private
copies (Probe T2 #328). The throwaway triage probe
(`backend/probe_rail.py`, committed by accident per #328) was deleted
after its logic was consolidated here.

## 8. Row registry — `cs_uk_api/row_kinds`

One declarative table is the single source of row-kind facts (spec
#323, Row T1 #329; completed by #362): every home-row routing key maps
to its title, form filter, sources selector, wire mappings and
extendability flag. The home builder, every facade wire mapping
(``jellyfin/router.py`` + ``jellyfin/dto.py``) and the deep-rows gate
(``_catalog_state/snapshot.py``) read THIS table instead of their
private vocabularies; adding a row kind touches the table only.

- **The table** — `ROW_KINDS: dict[str, RowKind]`, insertion order IS
  the canonical home-emission order (spec #362 D1): the form-split
  «Нещодавно додані» rows (recent_movie, recent_series) → «Нові серії»
  → «Нещодавно переглянуто» → «Популярні зараз» → movie → series →
  anime → cartoon → dorama → the LLM idea slots (llm_idea_1,
  llm_idea_2). `build_home_rows` emits each kind's segment at its table
  position, omitting empty/unsignalled rows (the recent rows, «Нові
  серії» and «Нещодавно переглянуто» are conditional — omission, not
  reordering). The retired «Новинки» (`newest`, retired 2026-08-14 per
  spec #263) has NO table entry; a cached client holding its view id
  gets the tolerant empty envelope until it re-lists views. The
  personalized «Рекомендовано для тебе» / «Схоже на X» rows join by the
  same recipe in a follow-up, and the `genre:<slug>` rails stay outside
  the table by design (parameterized kinds are not enumerable).
  `RowKind` is a frozen dataclass: `kind`, `title`, `filter`
  (`form`/`any` — the item admission policy), `form` (the Model B axis
  a form row admits), `sources`
  (`newest`/`popular`/`type`/`history`/`idea` — the sources selector),
  `jf_type` + `collection_type` (the wire mappings), `extendable`.
- **Derived facts flow from the table** — `view_id` (deterministic
  uuid5 of `cs-uk-api-view:{kind}`), `VIEW_TYPE_BY_ID` (the reversible
  parentId index the facade's reverse lookup reads),
  `KINDS_BY_JF_TYPE` (the `includeItemTypes` reverse index
  `_parse_include_types` reads) and `TYPE_KINDS` (the five type rows
  the home builder iterates) are all derived — a new kind gets its wire
  identity automatically.
- **Retired private vocabularies** — all row-kind facts now derive from
  the table (grep-verified absence): `home._RECENT_ROWS` /
  `_NEW_EPISODES_ROW` / `_RECENTLY_WATCHED_ROW` remain only as builder
  plumbing whose (kind, title) pairs the consistency suite pins against
  the table; the facade's private view-type tuple + uuid5 index +
  reverse map, its CollectionType dict and item-Type dicts
  (router `_VIEW_TYPES` / `_VIEW_ID_BY_TYPE` / `_VIEW_TYPE_BY_ID` /
  `_COLLECTION_TYPE_BY_ROW`, dto `JF_TYPE_BY_ROW`, and the
  `_HOME_KINDS_BY_JF_TYPE` loop) were deleted — search cards/hints,
  item DTOs, row views and the `includeItemTypes` translation all read
  the table (#362 batch B); the snapshot module's `_EXTENDABLE_ROWS`
  frozenset became a table-gated predicate (#362 batch C); the warm
  insert-scan tuple dropped the retired «Новинки» zombie (#362 batch D).
  A seam-guard source scan fails on any production definition of these
  vocabularies outside `row_kinds.py`.
- **Consistency test** — `tests/test_row_kinds.py` pins the AC: the
  exact 12-kind set in canonical order; every entry maps on the wire
  (unique reversible view id, reverse Type index covers everything
  exactly once); the form-filter invariant (form-filtered kinds =
  movie, series, recent_movie, recent_series, new_episodes); the five-
  way sources split (`newest`: recent rows + «Нові серії», `popular`,
  `history`, `idea`, `type`); the extendability split (type rows +
  recent_movie/recent_series/popular page past the snapshot — popular
  adopted the shipped deep-row behaviour; the rest stay
  snapshot-bounded); and cross-module facts so divergence cannot recur:
  `LLM_IDEA_ROW_TYPES ⊆ ROW_KINDS`, the home builder tuples' (kind,
  title) pairs equal their table entries, and the warm insert-scan
  tuple names only table kinds.

**Status:** deep-rows (#305/#306/#307) has landed — `extend_row_pool`
lazily pages an extendable row's pool past the snapshot when the client
scrolls (gated by the table's `extendable` flag via the facade Items
route), so `extendable` is consumed in production, not merely pinned.
Future flag changes are single-line edits to the table entry, consumed
everywhere.

## Verification status (frozen 2026-08-15)

Frozen as of the deepening wave's delivery — the numbers below were true
on their date and are deliberately NOT maintained (current gate numbers
live in the PR history and the ADRs). The section stays as the wave's
acceptance record.

- **Backend suite (fixture-only, no live I/O):** `pytest` **1095 passed**;
  `ruff check cs_uk_api` clean; `mypy cs_uk_api` strict-clean (59 files).
- **Provider vocabulary / contract (#319):** `model_b_axes` and the
  duplicated sentinel are gone (grep-verified: zero references).
- **Config seam (#321):** single-binding patches only (grep-verified: no
  double `SETTINGS` patches, no positional reconstruction).
- **Persistence (spec #323, Store T1 #324 + T3 #326):** `versioned_store.py`
  ladder / atomicity / debounce / `atomic_write_text` covered by
  `tests/test_versioned_store.py`; the
  sweep report write (`write_report`) in `tests/test_sweep_episode_rail.py`.
  (The profile-store adapter tests were removed with the module — §4.)
- **Probing (spec #323, Probe T1 #327 + T2 #328):** the three probe
  facts (plus row-type/attribution) covered by `tests/test_probe.py`;
  the episode-rail sweep consumes the module's verdict vocabulary and
  facts; the throwaway `probe_rail.py` deleted.
- **Row registry (spec #323, Row T1 #329):** the table-consistency suite
  `tests/test_row_kinds.py` pins every home kind's entry and wire
  mapping; the facade and home builder consume `ROW_KINDS` (grep-verified:
  no private kind→title / kind→wire dicts remain outside the table).
- **Switchfin sweep (spec: "one on-device sweep pass, zero visible
  change"):** the fixture suite is the local safety net; the on-device
  sweep (`scripts/switchfin_test.py` + a PS4 running Switchfin, per
  `docs/test-artifacts/switchfin/device-driving.md`) requires hardware and
  is pending — same as the repo's standing convention for on-console runs.
