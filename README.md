# PS4 UK Stream

A Linux-side backend that serves Ukrainian-dubbed content scraped from the
providers of cloudstream-extensions-uk, played on the PS4 through
**Switchfin** — a Jellyfin client — via the backend's Jellyfin facade
(spec #100). The backend's native `/api/*` routes and the facade serve the
same catalog; a Jellyfin client pointed at the backend finds a server
without configuration.

See `docs/superpowers/specs/2026-08-05-jellyfin-adapter.md` for the facade
design and `CONTEXT.md` for the domain model.

## Network addresses (home LAN)

- Backend host `openclaw-home`: `http://192.168.2.166:8003` (LAN IP, `wlan0` — wireless, DHCP/dynamic).
- PS4 FTP (GoldHEN): `192.168.2.105:2121` (for PKG install / payload transfer).

## Quick start

0. Backend dependencies: `cd backend && pip install -r requirements.txt`.
   The uakino provider additionally needs a system Chromium binary
   (default `/usr/bin/chromium`, override with `UAKINO_CHROMIUM`): its
   Cloudflare gate only passes for in-page `fetch()` calls, so the API
   runs a headless browser session for that one provider
   (see `backend/cs_uk_api/scripts/README.md`).
1. Run the backend on a Linux host in the same LAN as the PS4:
   `cd backend && uvicorn cs_uk_api.main:app --host 0.0.0.0 --port 8003`
2. On the PS4, open Switchfin and add a server at
   `http://<host-ip>:<port>` (the backend's Jellyfin facade). Any
   username/password completes the handshake — the facade is accept-any
   and stateless (ADR-0002).

## Deploy (systemd, single LAN host)

A ready-to-edit unit ships in `backend/deploy/cs-uk-api.service`:

```bash
sudo cp backend/deploy/cs-uk-api.service /etc/systemd/system/
# edit User= / WorkingDirectory= / ExecStart= paths to this host
sudo systemctl daemon-reload
sudo systemctl enable --now cs-uk-api
journalctl -u cs-uk-api -f   # warm-up: uakino reports "warming" until its
                             # browser session is ready, then "ok"
```

The service is LAN-only by design (ADR-0003: one host, one uvicorn
process, no auth). Tunable knobs: `CS_UK_CACHE_SEARCH`,
`CS_UK_CACHE_CONTENT`, `CS_UK_CACHE_POSTER`,
`CS_UK_POSTER_DISK_TTL`, `CS_UK_RESUME_PATH`, `CS_UK_USER_STATE_PATH`,
`UAKINO_CHROMIUM`, `CS_UK_ROW_MAX_PAGES` — see `CONTEXT.md` §Cache
contract.

**Continue watching («Продовжити перегляд»)** persists playback
positions in one versioned JSON file, by default
`~/.cache/cs-uk-api/playback.json` (next to the poster disk cache).
Point it elsewhere with `CS_UK_RESUME_PATH`; an explicit empty string
disables the disk layer (memory-only). Wipe the shelf with a single
command — `rm ~/.cache/cs-uk-api/playback.json` — or remove the file at
whatever path `CS_UK_RESUME_PATH` points to (see `CONTEXT.md` §Resume
state).

**Favorites and played marks (spec #257)** — the heart and the
context-menu "mark played/unplayed" on Switchfin's screens persist in a
versioned `user-state.json` (default next to the resume file, override
with `CS_UK_USER_STATE_PATH`; wipe with `rm ~/.cache/cs-uk-api/user-state.json`).
The Remote and Live TV tabs answer graceful empties instead of errors
(see `CONTEXT.md` §User state).

**Personalized rows (spec #252)** — «Рекомендовано для тебе» (≤20)
and «Схоже на <title>» (≤10) — appear on home once there is history
(watched items and/or recent searches) and are omitted otherwise. They
are fully offline by design: content profiles are built from the
providers' own pages by a bounded background warm, similarity is a
pure weighted-cosine function, and nothing leaves the LAN — no API
keys, no external services (see `CONTEXT.md` §Recommendations).

**Deep rows (spec #305)** — home rows scroll far beyond the first 20
cards: when a client pages past a row's snapshot, the facade lazily
extends the pool from the providers' browse pages 2..N (round-robin +
group-key dedupe against the snapshot, so page 2+ shows NEW cards
with an honest `TotalRecordCount`), and the row ends cleanly when the
bounded depth is exhausted. The depth is capped by
`CS_UK_ROW_MAX_PAGES` (default 5 upstream pages per provider, ≈100
cards per row) so upstream load stays under control — the tradeoff is
that scrolling is deep but finite, not unbounded. Extended pools cache
at the browse TTL (repeated passes are instant); a failing provider
page skips that provider and a fully-failed extension degrades to the
snapshot slice, so browsing never breaks. The personalized and genre
rails stay snapshot-bounded by design (their pool IS the snapshot).

**LLM taste-profile layer (spec #290, OPTIONAL)** — an optional
enrichment of the personalized rows against any OpenAI-compatible
endpoint (OpenAI, OpenRouter, Groq, or a local llama.cpp/ollama
server). A single daily background call turns the watch history and
recent searches into a structured taste profile — per-genre weights
(re-rank «Рекомендовано для тебе»), theme tags (token boosts), and up
to two extra personalized rows with Ukrainian titles («Похмурі драми
для тебе») served through the existing facade views. Enable it with
the three knobs:

```bash
CS_UK_LLM_BASE_URL=https://api.openai.com/v1   # any OpenAI-compatible base
CS_UK_LLM_KEY=sk-…                              # never commit this
CS_UK_LLM_MODEL=gpt-4o-mini
```

All three must be set to activate; without them (or on ANY failure —
network error, non-JSON answer, invalid profile) the layer is
invisible and the pure scorer runs unchanged — a broken model can
never hurt home. Refresh on demand (token-gated, operator action):

```bash
curl -X POST http://host:8003/ScheduledTasks/Running/llm-profile \
  -H "X-Emby-Token: $TOKEN"   # 204 on success, 200 + note otherwise
```

See `CONTEXT.md` §LLM taste profile.

**Upstream drift monitor (spec #285)** — providers drift (animeon
lost card URLs, anitubeinua moved its listing page — both found by
this monitor). A detached nightly probe sweeps every plain-HTTP
provider's listing through the real adapters, deep-probes a rotating
subset (content → stream → HEAD) so each provider gets full coverage
every 6 days, verdicts each against a self-calibrating baseline (a
healthy pass updates the expected card-count band and form/style
distribution, so catalog growth never false-positives), and files a
GitHub issue on the second consecutive failure — one issue per
provider and fault, recovery comments and closes it. uakino is never
probed (its health is the API's browser-session heartbeat).

A provider that is SERVING but has moved (a smaller listing, a form
or style it dropped for good) does not keep an issue open forever: its
failures are `shape` failures, and after three consecutive ones the
new shape becomes the baseline and the issue closes as an accepted
change (not as a recovery). Failures that mean the provider serves
nothing usable (`broken`: probe error, zero items, missing fields,
under the hard floor) never converge — they close when it really
recovers. Without the convergence rule a persisted band from an older
shape failed every sweep, and since the baseline only follows a
HEALTHY pass, nothing could ever break that loop (#382/#383).

The sweep also judges the PLAYBACK axis, which no probe can reach. It reads
`/api/health`'s `playback` counters — the API's own record of what it served
and what the client said played — and applies the same rule on an issue of
its own: a provider that SERVED streams the client never played logs on the
first faulting sweep and files on the second consecutive one. The verdict is
a WINDOW between sweeps, since those counters are process-lifetime totals: a
clean window means streams were served and at least one played, so a lane
being actively watched is never accused, and a sweep with no plays at all is
neither a fault nor a recovery. Lanes the probes exclude (uakino, the torrent
engine) are judged here too, because this verdict comes from the API's own
records and costs no probe. A listing that recovers never closes a playback
issue, and vice versa. `CS_UK_DRIFT_HEALTH` (default
`http://127.0.0.1:8003/api/health`) names the surface; an unreachable API is
"not read", never a fault.

Install the timer pair beside the service unit:

```bash
sudo cp backend/deploy/cs-uk-api-drift.service backend/deploy/cs-uk-api-drift.timer /etc/systemd/system/
# edit User= / WorkingDirectory= to this host (and gh auth for issue filing)
sudo systemctl daemon-reload
sudo systemctl enable --now cs-uk-api-drift.timer
```

Run it by hand (no issue filing):

```bash
cd backend && . .venv/bin/activate
python -m cs_uk_api.scripts.drift_monitor --no-issues   # probes + report only
cat ~/.cache/cs-uk-api/drift-report.json               # machine-readable report
```

The report and the baseline/counter state live in
`~/.cache/cs-uk-api/drift-{report,state}.json` (gitignored runtime
state; override with `CS_UK_DRIFT_REPORT` / `CS_UK_DRIFT_STATE`). The
script exits non-zero when any provider failed — visible in
`journalctl -u cs-uk-api-drift`.

**Play attestation (the playback axis of `/api/health`)** — what the
server did with every playback request. A catalog full of titles that
refuse to play used to look exactly like a healthy server: the refusal
existed only as a log line. Each request is now recorded as served, as
refused (with the verdict that refused it — `gated`, `not_found`, a dead
upstream — and the surface that asked: `playbackinfo`, `stream`,
`download`, `segment`, the native route), or as `no-progress`: the client
reported stopping before the position advanced, i.e. bytes were served
and did not play (the resume store discards those reports by design,
#214, which is why this state had no owner).

A group key is carried by several providers, so a refusal there is not
yet the title's verdict: the first member's lane fault or `not_found`
walks to the group's next member (up to three, in the order the detail
page chips list them) and the log names who served. `gated`, and a
client's own bad translation pick, do not walk — another member cannot
repair the client's own answer.

The same walk reaches the title's own PAGE: a title whose first member's
detail page is dead still opens and still plays, and the play then leads
with the member whose page answered (its translation ids are the ones the
client is holding). Both stages share one refusal taxonomy and one cap.

It is separate from `/api/providers`' lane status on purpose — a lane can
read `ok` while every play request against one title is refused
(ADR-0002) — and both facts are on the wire together:

```bash
curl -s http://127.0.0.1:8003/api/health   # .playback = the axis above
```

The drift monitor reads this axis nightly and files an issue of its own when
a provider serves streams the client never plays (see §Upstream drift
monitor above): the fault a listing probe and a HEAD cannot see.

**Torrent lane (English content, spec #374):** stand the BitPlay engine up
with `backend/deploy/docker-compose.bitplay.yml`; operate it via
[docs/torrent-lane.md](docs/torrent-lane.md).

## Release gate

```bash
cd backend && . .venv/bin/activate
pytest cs_uk_api/tests -q      # fixtures only (no live I/O)
ruff check cs_uk_api           # clean
mypy cs_uk_api                 # strict on the package (tests excluded in pyproject)
```

Live smoke against real upstreams (optional, needs internet):

```bash
uvicorn cs_uk_api.main:app --host 127.0.0.1 --port 8003 &
curl -s localhost:8003/api/providers        # 19 providers; uakino "warming" then "ok"
curl -s "localhost:8003/api/search?q=Дюна"  # groups + no failures
curl -s "localhost:8003/api/stream/<id>"    # live m3u8/mp4 URL, plays in mpv
```
