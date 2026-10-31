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

Nightly incremental refresh (all registered providers, 10 pages per section,
Mon–Sat at 04:10 — an hour after the drift monitor's 03:10 sweep) plus a
weekly full build (100 pages per section, Sunday 00:10). The two never run
on the same night: Sunday belongs to the full pass.

On `openclaw-home` this runs as **user** units for `semen`, not system
units: the four files in `backend/deploy/` (`cs-uk-api-index.{service,timer}`
and `cs-uk-api-index-full.{service,timer}`) are system-unit templates
(`User=rorschach`, a working directory with spaces,
`WantedBy=multi-user.target`), so the installed copies are adapted rather
than copied verbatim. The live files are
`~/.config/systemd/user/cs-uk-api-index.{service,timer}` and
`~/.config/systemd/user/cs-uk-api-index-full.{service,timer}`:

```ini
# ~/.config/systemd/user/cs-uk-api-index.service
[Unit]
Description=cs-uk-api - local catalog index crawler
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/home/semen/ps4-uk-stream/backend
Environment=HOME=/home/semen
Environment=PATH=/home/semen/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=UAKINO_CHROMIUM=/home/semen/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome
Environment=CS_UK_POPCORN_BASE_URL=https://fusme.link
ExecStart="/home/semen/ps4-uk-stream/backend/.venv/bin/python" -m cs_uk_api.scripts.index_catalog --all-providers --pages 10 --delay 0.3
TimeoutStartSec=5400
Nice=10
```

```ini
# ~/.config/systemd/user/cs-uk-api-index.timer
[Unit]
Description=Run the cs-uk-api catalog index crawler nightly (Mon-Sat)

[Timer]
OnCalendar=Mon..Sat *-*-* 04:10:00
Persistent=true
Unit=cs-uk-api-index.service

[Install]
WantedBy=timers.target
```

```ini
# ~/.config/systemd/user/cs-uk-api-index-full.service
[Unit]
Description=cs-uk-api - local catalog index crawler (weekly full build)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/home/semen/ps4-uk-stream/backend
Environment=HOME=/home/semen
Environment=PATH=/home/semen/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=UAKINO_CHROMIUM=/home/semen/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome
Environment=CS_UK_POPCORN_BASE_URL=https://fusme.link
ExecStart="/home/semen/ps4-uk-stream/backend/.venv/bin/python" -m cs_uk_api.scripts.index_catalog --all-providers --pages 100 --delay 0.3
TimeoutStartSec=7200
Nice=10
```

```ini
# ~/.config/systemd/user/cs-uk-api-index-full.timer
[Unit]
Description=Run the cs-uk-api catalog index full build weekly

[Timer]
OnCalendar=Sun *-*-* 00:10:00
Persistent=false
Unit=cs-uk-api-index-full.service

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now cs-uk-api-index.timer cs-uk-api-index-full.timer
systemctl --user list-timers 'cs-uk-api-index*'
journalctl --user -u cs-uk-api-index -u cs-uk-api-index-full -f
```

A user timer only fires while the user is logged out if lingering is on
(`loginctl enable-linger semen`; already enabled on this host).
`OnCalendar` is host-local time and `openclaw-home` runs UTC, so 04:10 and
00:10 are UTC there. The weekly timer is deliberately **not** persistent: a
catch-up full build at boot could start beside the persistent nightly run
and both write the same SQLite DB. A missed week waits for the next Sunday.
Force a run by hand with `systemctl --user start cs-uk-api-index.service`
or `cs-uk-api-index-full.service` (add `--no-block` to return immediately,
then follow the journal).

The CLI prints per-provider pages/seen/indexed/errors/attempts, then a
`crawl summary:` line with the run totals — providers walked, sections,
pages fetched, cards seen/indexed, errors, providers skipped by the 403
breaker, and the wall-clock duration of the walk (`duration=29m35s`). An
`http cache:` summary line follows, and the process exits non-zero when a
provider answered no page. A failed section is recorded and
skipped; one bad upstream does not abort the run. uakino's Chromium session
is closed at the end of the crawl.

A provider that answered **no page at all** is re-walked after the main
pass — two retry passes, 20 s and 40 s apart, one provider at a time — so a
short upstream or Cloudflare outage does not cost the library a whole
source for the night. A provider that answered some sections and failed
others keeps its recorded error and is not re-walked: re-reading sections
that already answered would spend upstream calls without adding cards.

### Conditional requests (ETag / Last-Modified)

The crawler re-reads the same first pages every night, so its HTTP client is
wrapped by `cs_uk_api/http_cache.py`: when an origin sent an `ETag` or a
valid `Last-Modified`, the next night's request carries `If-None-Match` /
`If-Modified-Since`, and a `304 Not Modified` is answered from the stored
copy instead of re-downloading and re-parsing the HTML. Validators and
bodies live in `http-cache.sqlite3` beside the catalog DB (so
`CS_UK_CATALOG_DB` moves both), kept for 14 days and capped at 512 MiB with
the oldest entry evicted first.

Only GET `200` responses that carry a validator are stored; redirects,
error pages and binary bodies are not. A malformed `Last-Modified` (some
DLE hosts emit `... -1100 GMT`) is dropped rather than echoed back. With
no validators nothing is stored, so deleting `http-cache.sqlite3` costs at
most one full re-download. `uakino` talks to Chromium, not this client, and
is outside the layer.

Measured against the live hosts on 2026-10-30: `hentaiukr` (ETag),
`bambooua` and `animeua` (Last-Modified) answer `304`; the rest advertise no
validator and keep sending the full page. A three-provider, one-page pass
revalidated 14 of 14 pages and skipped 2.8 MB. Several hosts send a stock
`Cache-Control: no-store` while still honouring `304`; the stored copy is
only replayed for the same URL after the origin confirms it is current, and
is never served as a cache hit by the API.

A provider that answers **403** on several consecutive nights is stood down
by `cs_uk_api/crawl_guard.py`: after three nights in which every section
failed with a 403, later runs skip it altogether — its rows stay searchable,
just stale — and one probe is allowed after seven days. A success clears the
record; another 403 at the probe re-arms the cooldown immediately, without
waiting for three more nights. Timeouts and other failures never trip it:
they break the streak and are left to the retry passes above. State lives in
`crawl-guard.json` beside the catalog DB (deleting it only costs one extra
walk). The run report prints `SKIP <provider> …` while a provider is stood
down and `BLOCK <provider> …` on the night the breaker trips.

Inspect the file directly:

```bash
sqlite3 ~/.cache/cs-uk-api/catalog.sqlite3 \
  'select provider, count(*) from catalog group by provider;'
```

Delete it to rebuild from scratch (safe — it is only a cache):

```bash
rm ~/.cache/cs-uk-api/catalog.sqlite3* ~/.cache/cs-uk-api/http-cache.sqlite3*
```

## Knobs

| Env | Default | Meaning |
| --- | --- | --- |
| `CS_UK_CATALOG_DB` | `~/.cache/cs-uk-api/catalog.sqlite3` | DB path; explicit empty string disables the index (live-only search) |
| `CS_UK_INDEX_PROVIDERS` | `uakino,ufdub,kinotron,uaflix,cikavaideya` | manual-run provider list; ignored by `--all-providers` |
| `CS_UK_INDEX_PAGES` | `10` | CLI/timer default pages per section |
| `CS_UK_INDEX_LOCAL_MIN` | `5` | local groups needed to skip the live fan-out; 1 = always local on any hit |
| `CS_UK_INDEX_DELAY` | `0.2` | seconds between crawler page fetches |
| `CS_UK_INDEX_BLOCK_AFTER_NIGHTS` | `3` | consecutive all-403 nights before a provider is skipped; 0 disables the breaker |
| `CS_UK_INDEX_BLOCK_COOLDOWN_DAYS` | `7` | days a tripped provider is left alone before one probe run |

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
