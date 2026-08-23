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

The wave's spec also calls out two seams that are **not yet in this tree**
(the wave's earlier interface tickets T1–T4 are closed but unmerged on
`master`): a typed catalog interface module and a dedicated wire-identity
module. Where a section says "future direction", that is the spec's
intended shape, not something shipped here.

---

## 1. Catalog seam — `cs_uk_api/catalog_state`

`catalog_state` is the single owner of the catalog's shared state and the
accessors over it. Both surfaces — the native `/api/*` routes (`main.py`)
and the Jellyfin facade (`jellyfin/router.py`) — read the same snapshot,
the same resolution map, and the same caches; there is one cache-key
shape, one TTL, one `clear()` per store.

Public accessors (the seams callers use, `__all__` + facade imports):

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

**Future direction (spec T2–T4):** the facade currently imports ~9 typed
accessors directly from `catalog_state`; the spec's target is a small
typed catalog interface module that narrows that surface further, with
the back-compat aliases retired last.

## 2. Wire identity — `cs_uk_api/merge` + `providers/base.MOVIE_SUFFIX`

Group identity is stateless and versioned:

- `merge.group_key(alias, form, year)` — `g2:` + sha1 of the canonical
  (alias|form|year) triple. `_KEY_VERSION = "g2"` in `merge.py` is the
  version bump point; a normalization-rule change is a `g2:` → `g3:` bump,
  never a migration.
- `merge.group_key_from(title, form, year, item_id)` /
  `merge.item_group_key(item)` — per-item stateless keys.
- `merge.merge_results(items)` — the **single merge projection** (union
  find, year-soft rule) that produces `MergeGroup`s; every caller
  (search, home rows, sources map) feeds through it instead of
  re-implementing merge rules.

Movie wire ids end in the canonical sentinel `MOVIE_SUFFIX = ":__movie__"`
(`providers/base.py`, contract step #319): the sentinel was previously
defined in 8 provider files; now it is imported from `base` everywhere.

Episode wire ids carry `:s{season}e{episode}` tails; the resume rail's
reverse lookup (`group_key_for_external`) understands both the episode
wire id and the uakino/animeon fallback shapes (ticket #234).

**Future direction (spec T1):** the spec moves the group-key prefix, the
episode-tail grammar and the movie suffix into one wire-identity module so
a version bump edits one file. In this tree the grammar lives in `merge.py`
plus `base.MOVIE_SUFFIX`.

## 3. Provider vocabulary — `cs_uk_api/providers/base`

The provider base is the typed vocabulary every adapter speaks:

- `MediaForm = "movie" | "series"` and `MediaStyle = "anime" | "cartoon" |
  "dorama"` (`models.py`) — the Model B axes (ADR-0001). `SearchResult`
  and `ContentResponse` carry `form` + `styles`; `Section` declares its
  filter axes.
- `MediaTypeStr = Literal["movie", "series", "anime", "cartoon", "dorama"]`
  (`base.py`) — the internal classification value. Classification helpers
  (`_type_from_url`, `_classify_*`, `_PATH_TYPE`/`_TAG_TYPE` tables) are
  typed to return it; no `# type: ignore[arg-type]` remains (contract
  #319).
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
  `catalog_state/_stores.py` (`ResumeStore` via `record_playback` /
  `clear_playback`; `UserStateStore` for favorites / played marks / dub
  memory, spec #247/#257/#276).
- The content taste profiles always lived in the catalog stores
  (`catalog_state._stores._profiles`, installed wholesale via the
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
- **Store constructors:** stores are constructed with a settings argument
  (re-instantiable, the snapshot-store pattern):
  - `catalog_state.CatalogStores(settings)` — the six cache stores
    (home/search/content/blocklist/gated/sources), TTLs from the snapshot;
    module singleton `STORES` is the one production binding.
  - `poster_proxy._cache` and `main._browse_cache` — TTLs from the
    snapshot at construction.
  - Tests re-instantiate stores from custom settings instead of import
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
snapshot / drift-baseline stores
named in the spec belong to the unmerged round-1 interface work and are
not in this tree yet (their versioned adapters land with it).

## 7. Provider probing — `cs_uk_api/probe`

One module names the three probe facts every provider probe (drift
monitor, episode-rail sweep, triage scripts) must agree on (spec #323,
Probe T1 #327):

- **Entry-point selection** — `select_entry_points(provider)`: the chain
  `newest_section` → declared sections → `search` (the fallback every
  provider has).
- **Wire-id splitting** — `split_wire_id(composite)`: `provider:external`
  → `(provider, external)`, splitting on the FIRST colon so episode wire
  ids (`uakino:6268:e1`) survive intact. The canonical copy: the 8th
  in-tree implementation of the same split lives here now.
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
#323, Row T1 #329): every home-row routing key maps to its title, form
filter, sources selector, wire mappings and extendability flag. The
home builder, the facade view maps and the deep-rows extension read
THIS table instead of their private vocabularies; adding a row kind
touches the table only.

- **The table** — `ROW_KINDS: dict[str, RowKind]`, insertion order IS
  the home order («Новинки» → «Популярні зараз» → movie → series →
  anime → cartoon → dorama, v3 spec §3.1). `RowKind` is a frozen
  dataclass: `kind`, `title`, `filter` (`form`/`any` — the item
  admission policy), `form` (the Model B axis a form row admits),
  `sources` (`newest`/`popular`/`type` — the sources selector),
  `jf_type` + `collection_type` (the wire mappings), `extendable`
  (deep-rows #305).
- **Derived facts flow from the table** — `view_id` (deterministic
  uuid5 of `cs-uk-api-view:{kind}`), `VIEW_TYPE_BY_ID` (the reversible
  parentId index), `KINDS_BY_JF_TYPE` (the `includeItemTypes` reverse
  index) and `TYPE_KINDS` (the five type rows the home builder iterates)
  are all derived — a new kind gets its wire identity automatically.
- **Retired private vocabularies** — `home._TYPE_ORDER` (kind → title)
  and `home._item_matches_row` (kind → form filter) became table reads
  (`build_home_rows` titles and `item_matches_row`); the facade's
  `_VIEW_ID_BY_TYPE` / `_COLLECTION_TYPE_BY_ROW` / `_JF_TYPE_BY_ROW`
  and the `_HOME_KINDS_BY_JF_TYPE` loop are gone — the item-DTO Type
  lookups now read the table by the item's `form` (both forms are
  table kinds).
- **Consistency test** — `tests/test_row_kinds.py` pins the AC: every
  home kind has an entry; every entry maps on the wire (unique
  reversible view id, valid CollectionType/JF Type, reverse index
  covers everything exactly once); the form-filter invariant; the
  sources/extendability split; and parity with the pre-registry wire
  values (movie is the only Movie/movies row).

**Honest status:** the deep-rows consumer (#305/#306/#307) is not in
this tree — the `extendable` flag is declared and pinned by the
consistency test (personalized rows snapshot-bounded, type rows
extendable) and will be read when that wave lands.

## Verification status (2026-08-15)

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
