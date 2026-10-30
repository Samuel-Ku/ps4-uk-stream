# Local catalog index (local-first search)

`cs_uk_api.catalog_index` is the local SQLite FTS5 catalog beside the
other runtime stores. It exists so the library is not limited to the home
snapshot's first page per section, and so search keeps working when an
upstream is slow or down.

The DB is a rebuildable cache: deleting it loses no viewer state, and the
next crawl recreates it.

## What gets indexed

- The crawler reuses each provider's own `browse(section, page)` adapter —
  the same parser `/api/browse` uses. No second scraper dialect.
- The nightly unit crawls **every registered provider** (`--all-providers`).
  A manual run without the flag uses `CS_UK_INDEX_PROVIDERS` (default: the
  five broadest section providers: `uakino`, `ufdub`, `kinotron`, `uaflix`,
  `cikavaideya`) or the CLI `--providers`.
- `yts` series cards need a Popcorn-API host. The shipped units set
  `CS_UK_POPCORN_BASE_URL=https://fusme.link` (live aliases and the
  self-host recipe: [torrent-lane.md](torrent-lane.md)). Without it, yts
  movies still crawl and its series section records the not-configured
  error.
- Default depth is 10 browse pages per section; the systemd timer asks for
  20. A first build can use `--pages 100`; the crawler stops early when a
  section reports `has_next = false`.
- Live search results are also upserted on the way out, so normal use
  grows the index between crawls.

The indexed card is exactly `SearchResult`: provider wire id, title, year,
form, styles, genres, poster, URL, IMDb id. No content pages are fetched
during a crawl.

## Matching

`catalog` stores the normalized title plus two Ukrainian transliterations:
official (`Дюна` → `diuna`) and loose (`Дюна` → `duna`). Every normalized
alias is indexed, so a card listed as «Тато / Daddy» matches either
language. All forms live in the single FTS5 `search_text` column with the
genre labels. A query is tokenized and every token is prefix-matched, so
the last word can be partial; each token may match the Cyrillic or either
Latin form.

- `дюна`, `дю`, `duna`, `diuna` all find «Дюна».
- `dune` finds a Latin-titled «Dune».
- `фантастика` finds cards whose genre carries that label.
- Multi-word queries require **all** tokens (`AND`); a middle-of-word
  match is still possible through the LIKE fallback, but it also requires
  every token. There is no noisy “one word matched” fallback.
- Ranking: native exact → native prefix → transliteration → all-token
  coverage → rest, then freshness, year and shorter title. A native
  spelling beats a transliteration collision («Дюна» before «Дуна!» for
  «дюна»). Results from several providers are projected through the shared
  `merge` core, so group keys match live search exactly.

FTS5 is the normal path. A Python/SQLite build without FTS5 logs a warning
and falls back to `LIKE` over the same `search_text` column.

## Search policy

`catalog.search` is local-first with a completeness guard:

1. The index is always queried first.
2. At least `CS_UK_INDEX_LOCAL_MIN` merged groups (default 5) → answer from
   the index alone, no upstream call.
3. Fewer local groups → run the normal live fan-out and merge local + live
   sources by wire id, then index the live results.
4. A **total** live failure with local hits → serve the local groups
   instead of a 502.

`/api/search?form=` and `?style=` are applied to local results before the
merge. `/api/search/hints` is index-only and never fans out, so it is safe
to call on every keystroke.

## Operations

First build:

```bash
cd backend && . .venv/bin/activate
python -m cs_uk_api.scripts.index_catalog --all-providers --pages 100
```

Nightly refresh (all registered providers, 20 pages per section, after
the drift monitor):

```bash
sudo cp backend/deploy/cs-uk-api-index.service \
        backend/deploy/cs-uk-api-index.timer /etc/systemd/system/
# edit User= / WorkingDirectory= / ExecStart= for this host
sudo systemctl daemon-reload
sudo systemctl enable --now cs-uk-api-index.timer
journalctl -u cs-uk-api-index -f
```

The CLI prints per-provider pages/seen/indexed/errors and exits non-zero
when a provider answered no page. A failed section is recorded and
skipped; one bad upstream does not abort the run. uakino's Chromium
session is closed at the end of the crawl.

Inspect the file directly:

```bash
sqlite3 ~/.cache/cs-uk-api/catalog.sqlite3 \
  'select provider, count(*) from catalog group by provider;'
```

Delete it to rebuild from scratch (safe — it is only a cache):

```bash
rm ~/.cache/cs-uk-api/catalog.sqlite3*
```

## Knobs

| Env | Default | Meaning |
| --- | --- | --- |
| `CS_UK_CATALOG_DB` | `~/.cache/cs-uk-api/catalog.sqlite3` | DB path; explicit empty string disables the index (live-only search) |
| `CS_UK_INDEX_PROVIDERS` | `uakino,ufdub,kinotron,uaflix,cikavaideya` | manual-run provider list; ignored by `--all-providers` |
| `CS_UK_INDEX_PAGES` | `10` | CLI/timer default pages per section |
| `CS_UK_INDEX_LOCAL_MIN` | `5` | local groups needed to skip the live fan-out; 1 = always local on any hit |
| `CS_UK_INDEX_DELAY` | `0.2` | seconds between crawler page fetches |

## Non-goals in this pass

- No stale-row eviction yet: a title removed upstream stays searchable
  indefinitely in this pass (each crawl only refreshes `last_seen` for
  cards it still sees); playback still refuses it. A later sweep can use
  `last_seen` to hide old rows.
- No subscription-gate sweep during the crawl: resolving every card's
  content page would be far heavier than the listing walk. A card whose
  only stream is the sponsor promo can therefore appear in local results;
  playback still refuses it with `gated`, and live search keeps the
  existing sweep.
- No alias table beyond transliteration: «Дюна»/`Duna` works, but
  «Дюна»/`Dune` still requires the live fallback.
- No episode-level rows: the index holds the merged title card, not the
  season/episode tree (that still comes from the provider content page).
