# openclaw-home post-merge smoke — the refactor wave on a live stack

**Date:** 2026-09-12 · **Host:** `openclaw-home` (LAN deployment)
**Scope:** deployment proof for PRs #413–#417 — the torrent-lane fallback,
the facade item-vs-lane health fix, the native-content carve-out, and the
smoke script itself. **Status:** every flow **PROVEN LIVE**.

This is the post-merge counterpart to `accept-373-2026-09-05.md`: that note
proved the ticket's floors on the workstation, this one proves the merged
master still serves a real client's flows on the operator's host.

## Environment (verified 2026-09-12)

- Host runs the stack as **user units** (no root): `cs-uk-api.service`
  (`~/.config/systemd/user/`), repo at `/home/semen/ps4-uk-stream`,
  uvicorn on **:8003** (`WorkingDirectory=…/backend`, no `CS_UK_JF_TOKEN`
  in the unit — the built-in default `jellyfin-dev-token` is live).
- Engine is the **compose container** (`bitplay`, `docker compose`), up
  10 h, answering on **:3347** — not the native scratch recipe the #373
  note describes.
- Untracked deploy dirs (`backend/deploy/config/`,
  `backend/deploy/torrent-data/`) were left in place; the repo was
  advanced with `git merge --ff-only` only.
- Both runs below are **read-only against shared state**: no cache purge,
  no service config change. The play flows add one torrent to the engine
  (idempotent by infohash).

## Run 1 — deployment proof at `b0d270c` (PR #416)

Host advanced `2ca7acc → b0d270c`, `cs-uk-api` restarted, startup warm
allowed to finish (`catalog_warm.status=done`, `content_warmed=31/32`,
`failed=0`).

**Health after restart:** 18/21 providers `ok`; `kinotron`,
`cikavaideya`, `simpsonsuatv` `degraded` (upstream `unreachable` / `301`
— the known broken-site class, unchanged by the refactor); `yts: ok`,
`yts:engine: ok`; `all_down: false`. `cikavaideya` self-recovered later as
the sliding window aged its failures out.

| Flow | Request | Result |
|---|---|---|
| handshake | `POST /Users/AuthenticateByName` | **200** in 0.03s, token issued, username echoed |
| views | `GET /UserViews` | **200** in 0.09s, **17 views** |
| browse | `GET /api/browse?provider=uakino&section=filmy` | **200** in 0.22s, **40 cards**, `has_next=true` |
| browse p2 | same, `&page=2` | **200** in 0.21s, 40 cards |
| browse repeat | same, page 1 | **200** in **0.00s** (cache hit) |
| detail | `GET /api/content/uakino:documentaries:36108-…` | **200** in 0.00s, full envelope |
| play (2nd title) | `POST /Items/g3:tt1935156/PlaybackInfo` | **200** in 1.72s, container `mp4` |

### The capstone — `g3:tt1160419` ("Dune: Part One")

| Step | Result |
|---|---|
| `GET /Items/g3:tt1160419` (detail) | 200 in 0.21s |
| `POST /Items/g3:tt1160419/PlaybackInfo` | **200 in 2.62s**, 1 MediaSource, `Path=/Videos/g3:tt1160419/stream` |
| `GET /Videos/…/stream` | **302** → `location: http://127.0.0.1:3347/api/v1/torrent/1018e90e7b2151782170f366048431452c157b3c/stream/0` |
| bounded read (`Range: bytes=0-65535`) | **206**, exactly **65536 bytes in 0.038s**, body `0000 0020 6674 7970 6973 6f6d …` — a valid `ftypisom` MP4 |
| seek (`Range: bytes=1048576-2097151`) | **206**, exactly **1048576 bytes in 0.04s**, `Content-Range: bytes 1048576-2097151/3075917270` |

**Journal:** no tracebacks and no route errors. Warnings are only the known
broken sites plus the documented unconfigured series lane
(`home type skipped provider=yts section=series err=unreachable: popcorn
series host not configured`). The engine leg logs clean —
`POST /api/v1/torrent/add` 200 and `GET /api/v1/torrent/{hash}` 200 per
play.

## Run 2 — the delivered gate at `37e3357` (PR #417)

`backend/deploy/smoke.sh` run **from its repo path on the host**, no
arguments:

```
=== 9 passed, 0 failed, 1 skipped ===   EXIT=0   65s
```

- handshake → 17 views → browse 40 cards at `uakino/filmy`
- search `Dune` (lane-scoped) → 3 candidates
- `SKIP g3:tt35969257: PlaybackInfo -> 404 item_unavailable` (see finding 1)
- `PASS playable group g3:tt15239678` ("Dune: Part Two") →
  **302** to `http://127.0.0.1:3347/api/v1/torrent/c2f364ba7f59b22a1845b16fd8a1fc1783568f8b/stream/0`
  → **206** with 65536 bytes → seek **206** + `Content-Range` honoured
- service reported `active` throughout — the change is docs + script, so
  no restart was needed.

### Negative and edge cases (the gate's exit contract)

| Case | Expected | Observed |
|---|---|---|
| unreachable backend (`CS_UK_API_URL` → closed port) | 1 | **1** (handshake FAIL; authed flows skipped, reported) |
| wrong `CS_UK_JF_TOKEN` | 1 | **1** (`GET /UserViews -> 401`) |
| correct `CS_UK_JF_TOKEN` | 0 | **0** |
| `--skip-lane` | 0 | **0** |
| unknown flag | 2 | **2** |

## Findings

1. **A YTS-listed title can carry no torrents, and the client waits ~60s
   to find out.** `g3:tt35969257` resolved at detail (200) but
   `PlaybackInfo` answered `404 item_unavailable` after **60.12s** — the
   lane's internal engine-add timeout, re-probed rather than served from
   the poisoned cache (the #413 fix working). Operator-visible jank, not a
   broken deployment: the smoke gate treats it as a *skip with reason* and
   fails only when no candidate plays. Worth attacking separately —
   `g3:tt15239678` ("Dune: Part Two"), a far more popular title, played in
   3.73s.
2. **An unreproduced transient 404, already defended against in the
   gate.** During the first gate run, two consecutive candidates answered
   `detail -> 404` microseconds after a sibling group resolved in 0.01s —
   a search-only group missing from the resolution map (the #415 class).
   It did **not** reproduce: a re-run of the identical sequence returned
   200, and a deliberate 70s wait proved registrations survive well past a
   60s stall (so it is not TTL expiry). `smoke.sh` now re-issues the
   search before each candidate (~0s, cached) so a background index
   rebuild landing mid-run cannot fail the verdict spuriously. **Filed as
   issue [#420](https://github.com/Samuel-Ku/ps4-uk-stream/issues/420)** —
   not a fix, and not a claimed mechanism: the issue traces the code path
   (a snapshot rebuild replacing the resolution map and index wholesale,
   with no re-registration on the read path) and lists the instrumentation
   that would confirm it. That trace was done *after* this run and is not
   part of the evidence above.
3. **Never time an unbounded read against the engine.** The engine serves
   from its own local copy, so a follow-the-redirect read without a Range
   pulled the whole ~3.07 GB body (the file size `Content-Range` reports) —
   useless as a latency signal. The bounded 64 KiB range request is the
   authoritative measurement, and is what the gate and this table use.

## Re-run recipe

```bash
ssh openclaw-home
cd ~/ps4-uk-stream && git fetch origin && git merge --ff-only origin/master
backend/deploy/smoke.sh; echo $?          # 0 = whole stack healthy
backend/deploy/smoke.sh --skip-lane       # host with no engine
```

Point it at another host with `CS_UK_API_URL` / `CS_UK_ENGINE_URL`; the
script header lists the rest of the knobs. For the ticket-level floors
(engine F1–F4, facade B1–B5, subtitles) use `accept_373.sh` — it purges
the engine cache, which is why the deployment gate above does not.

## Round 2 — #420 confirmed, fixed, and re-proven live (same day)

Finding 2 above is no longer an open question. The transient was
**confirmed, measured, and fixed**; this section records the fix proof and
supersedes that finding's status (the run-1 evidence above is left as it
was observed).

- **Confirmed** by instrumenting the index replacement (PRs
  [#422](https://github.com/Samuel-Ku/ps4-uk-stream/pull/422) /
  [#423](https://github.com/Samuel-Ku/ps4-uk-stream/pull/423)): a search
  registered 16 keys, the next replacement logged exactly
  `dropped 16 search-registered key(s)`, and the following `/Items/{id}`
  read answered 404 in 0 ms. The reason "it did not reproduce" was the
  race needing a wipe *inside* the window.
- **Decided** in ADR-0010 (PR
  [#424](https://github.com/Samuel-Ku/ps4-uk-stream/pull/424)) and
  **implemented** in PR
  [#425](https://github.com/Samuel-Ku/ps4-uk-stream/pull/425): one owned
  catalog state and one apply step for the resolution map + group index;
  search registrations are carried forward and expire on the *search* TTL
  (5 m), not the snapshot cycle. Issue #420 is closed.

### Proof on `68e2eb6` (openclaw-home, service restarted onto the fix)

| step | observation |
|---|---|
| search `q=дюна` | 61 groups, **56 of them absent from the home snapshot** (search-only), 20:59:09 |
| sanctioned invalidation after the search | `catalog snapshot invalidated reason=profile warm added profiles`, 20:59:12 |
| replacement read (`/api/home` on the emptied cache) | 20:59:14 — **both** replacement sites are the apply step, so a cold-cache home read *is* a replacement |
| the reads that 404-ed in run 1 | 4/4 search-only keys → **200** (`/Items/g2:c3200c673b15493a`, `g2:9275bc434ff43d7e`, `g2:551531834212e8fa`, `g2:efa0cde3d98f347d`) |
| the #420 instrument | **0** `retired` lines since the search — nothing was dropped |

The instrument's *silence* while a key is still wanted is the fix's
observable; it now fires only for a registration that genuinely ages out at
the search TTL. Unit-level A/B on identical state (`resolve_group` for the
same key): old whole-replace → `None` (the 404), apply step → the provider
union.

## Re-run 2026-10-26 — dead-swarm failover at `e6c223d`

**Commit:** `e6c223d` (`fix(deploy): fail over when a smoke candidate's
stream stalls`) · **Status:** failover **PROVEN LIVE**, exit `0`.

The host was already on `f44a7a2` (the translation-walk deploy) when the
default `Dune` smoke run exposed the gap this re-run closes: the first
candidate, `g3:tt35969257`, returned a `MediaSource` but its swarm sent no
bytes, and the stream check ran only **after** the candidate loop. The run
ended `7 passed, 1 failed` at `bounded read failed (000 0; want 206/200
with 65536 bytes)`, even though `g3:tt15239678` ("Dune: Part Two") played
end to end in a diagnostic pass.

`e6c223d` moves the redirect/bytes/seek checks into `candidate_play`, so a
stalled candidate is a `SKIP` with its reason and the loop advances. The
host required no service restart: the commit changes only `smoke.sh` and
`DEPLOY.md`. The API had been restarted onto `f44a7a2` earlier.

The engine advertises its **LAN** URL (`http://192.168.2.223:3347`); the
run set `CS_UK_ENGINE_URL` to that address because the script's default
`127.0.0.1:3347` does not match the redirect this host hands LAN clients.
The host script's SHA-256 matched the tested local copy
(`6a9bec5d6f5bd9d34e1aa6693c8e7bf34afc7c34e3b9aa11169460444664400b`),
and `bash -n backend/deploy/smoke.sh` passed before the run.

```bash
ssh openclaw-home
cd ~/ps4-uk-stream
git fetch origin && git merge --ff-only origin/master
CS_UK_ENGINE_URL=http://192.168.2.223:3347 backend/deploy/smoke.sh; echo $?
```

| Flow | Observation |
|---|---|
| handshake | `200`, token issued (`ServerId=8e825b6f9a93a6ec`) |
| views | `200`, **17 views** |
| browse | `GET /api/browse?provider=uakino&section=filmy` → `200`, **40 cards** |
| search `Dune` | 3 candidates: `g3:tt35969257`, `g3:tt15239678`, `g3:tt0287839` |
| candidate 1 — `g3:tt35969257` | detail `200`, but the engine bounded read timed out with `000 0`; reported `SKIP ... bounded read failed (000 0; want 206/200 with 65536 bytes)` |
| candidate 2 — `g3:tt15239678` ("Dune: Part Two") | detail `200`; PlaybackInfo `200`; stream `302` to `http://192.168.2.223:3347/api/v1/torrent/c2f364ba7f59b22a1845b16fd8a1fc1783568f8b/stream/0`; first 64 KiB `206 65536`; seek `206 1048576` with `Content-Range` honoured |

The default `Dune` run finished `9 passed, 0 failed, 1 skipped`, exit `0`.
The skipped candidate is the same metadata-resolves-but-no-bytes case that
previously failed the whole run; the next candidate served 64 KiB and
honoured the seek. The per-candidate timeout remains the script's normal
`CS_UK_TIMEOUT` (60s by default).

CI for the same commit also passed:
[run 40398084720](https://github.com/Samuel-Ku/ps4-uk-stream/actions/runs/40398084720)
(`gates (py3.11)` and `gates (py3.13)`, both green).

Use `--skip-lane` on a host without the engine. For a different host,
set `CS_UK_API_URL` and `CS_UK_ENGINE_URL` to the addresses the facade
actually advertises; the redirect check compares the stream location
against `CS_UK_ENGINE_URL`.

## Re-run 2026-10-27 — the gate verifies the redirect status at `ac66df9`

**Commit:** `ac66df9` (`fix(deploy): require the smoke stream redirect to
be a 302`) · **Status:** stricter gate **PROVEN LIVE**, exit `0`.

`ac66df9` makes `candidate_play` read `%{http_code}` in the same curl call
as `%{redirect_url}` and skip a candidate whose stream redirect is not
exactly `302` — previously any 3xx pointing at the engine passed while the
gate's own PASS line claimed "302 to the engine". It closes the 10-27
smoke-test series: `75273fe` (failover regression) → `2fd0742` (the fake
mirrors the 302) → `cf6045d` (all-stall failure) → `acf69f2` (#415
detail-404 skip) → `ac66df9`.

The host checkout was fast-forwarded `da82b40 → ac66df9` (22 files, the
teleport work included); the **service was not restarted** for this run
(`ActiveEnterTimestamp` 2026-10-26 20:32:31 UTC), so the stricter script
was exercised against the live process — which is what the gate exists to
check.

The engine advertises its **LAN** URL (`http://192.168.2.223:3347`), so
the run set `CS_UK_ENGINE_URL` to that address, as the 10-26 re-run
recorded. A first run with the script's default `127.0.0.1:3347` skipped
all three candidates at the redirect-target case (`stream redirected
somewhere unexpected: http://192.168.2.223:3347/...`) — the known
environment requirement, not the new check: the status check passed there
too (the facade answered `302`), and the target comparison did the
skipping exactly as before. The host script's SHA-256 matched the tested
workstation copy
(`d50341864d97583f6afa2e48f5b371a0f17ede74b3dc778e1c9a6182e6dfc981`).

```bash
ssh openclaw-home
cd ~/ps4-uk-stream
git fetch origin && git merge --ff-only origin/master
CS_UK_ENGINE_URL=http://192.168.2.223:3347 backend/deploy/smoke.sh; echo $?
```

| Flow | Observation |
|---|---|
| handshake | `200`, token issued (`ServerId=8e825b6f9a93a6ec`) |
| views | `200`, **17 views** |
| browse | `GET /api/browse?provider=uakino&section=filmy` → `200`, **39 cards** |
| search `Dune` | 3 candidates: `g3:tt35969257`, `g3:tt15239678`, `g3:tt0287839` |
| candidate 1 — `g3:tt35969257` | skipped: `bounded read failed (000 0; want 206/200 with 65536 bytes)` (the failover case, unchanged) |
| candidate 2 — `g3:tt15239678` ("Dune: Part Two") | detail `200`; PlaybackInfo `200`; stream **`302`** to `http://192.168.2.223:3347/api/v1/torrent/c2f364ba7f59b22a1845b16fd8a1fc1783568f8b/stream/0`, the status verified by the new check; first 64 KiB `206 65536`; seek `206 1048576` with `Content-Range` honoured |

The default `Dune` run finished `9 passed, 0 failed, 1 skipped`, exit `0`.
The check adds no new failure mode for a correct facade; its only
302-shaped difference — a `stream -> <code>` skip on a different 3xx — is
pinned offline by `test_non_302_stream_redirect_is_skipped` (and the
other three behavioural cases landed in the same series), with the
live run here proving the passing half.

CI for the same commit also passed:
[run 40493199199](https://github.com/Samuel-Ku/ps4-uk-stream/actions/runs/40493199199)
(`gates (py3.11)` and `gates (py3.13)`, both green).

## Re-run 2026-10-27 — the five-flow gate proves teleport readiness at `29499b3`

**Commit:** `29499b3` (`feat(deploy): add the teleport readiness flow to the
smoke gate`) · **Status:** fifth flow **PROVEN LIVE**, exit `0`.

`29499b3` closes the gap that let a host pass every flow and still answer
a silent `503` at the first real alignment: a form POST with no audio
proves `/api/teleport/align` is mounted and enabled (`400`), a route
switched off by configuration would answer its own `503` and is a skip,
and a stale process that does not serve the route fails loudly. It then
resolves `ffmpeg` and `fpcalc` in the **running unit's own `PATH`** — via
systemctl's `MainPID` and `/proc/<pid>/environ`, the same `shutil.which()`
the aligner sees — so the `~/.local/bin` drop-in gap can no longer pass.

The commit is not on `origin` yet (push pending), so the host checkout
stays at `f9a74f7` and the standard fast-forward recipe could not run. The
committed script was delivered over stdin to `/tmp/smoke-29499b3.sh`; its
SHA-256 matched the workstation copy exactly
(`37ecc46fbc211ad4355701fd0f48fc59d5d56b2a445906762f8431c05106581a`) and
`bash -n` passed before the run. The service was **not restarted** for this
run — the commit changes the gate, its tests and `DEPLOY.md` — it had been
running since 2026-10-27 12:00:47 UTC (`MainPID` 3445439). CI has not seen
`29499b3` yet; this run is its first live evidence.

The engine advertises its **LAN** URL, so the run set `CS_UK_ENGINE_URL` to
`http://192.168.2.223:3347`, as the 10-26 and earlier 10-27 re-runs
recorded. The service's own `PATH` (`/proc/<pid>/environ`) begins with
`/home/semen/.local/bin`, where `fpcalc` resolves to the extracted-binary
symlink — while the ssh shell used for this run had no `fpcalc` in `PATH`
at all, which is exactly the gap the new flow exists to catch: the check
answered through the service-`PATH` probe, not the `CS_UK_TELEPORT_PATH`
override and not the shell fallback.

```bash
ssh openclaw-home
cd ~/ps4-uk-stream
CS_UK_ENGINE_URL=http://192.168.2.223:3347 /tmp/smoke-29499b3.sh; echo $?
# once the push lands, the standard path resumes:
#   git fetch origin && git merge --ff-only origin/master
#   CS_UK_ENGINE_URL=http://192.168.2.223:3347 backend/deploy/smoke.sh; echo $?
```

| Flow | Observation |
|---|---|
| handshake | `200`, token issued (`ServerId=8e825b6f9a93a6ec`) |
| views | `200`, **17 views** |
| browse | `GET /api/browse?provider=uakino&section=filmy` → `200`, **39 cards** |
| search `Dune` | 3 candidates: `g3:tt35969257`, `g3:tt15239678`, `g3:tt0287839` |
| candidate 1 — `g3:tt35969257` | skipped: `bounded read failed (000 0; want 206/200 with 65536 bytes)` (the failover case, unchanged) |
| candidate 2 — `g3:tt15239678` ("Dune: Part Two") | detail `200`; PlaybackInfo `200`; stream **`302`** to `http://192.168.2.223:3347/api/v1/torrent/c2f364ba7f59b22a1845b16fd8a1fc1783568f8b/stream/0`; first 64 KiB `206 65536`; seek `206 1048576` with `Content-Range` honoured |
| teleport route | `POST /api/teleport/align` with no audio → **`400`** `audio is required` (mounted and enabled) |
| teleport tools | **`PASS`** `ffmpeg + fpcalc resolvable in the cs-uk-api service's own PATH` |

The run finished `11 passed, 0 failed, 1 skipped`, exit `0`, in **132 s**
(2026-10-27 13:15:14 → 13:17:26 UTC). The skip is the same
metadata-resolves-but-no-bytes candidate the 10-26 re-run characterised;
the fifth flow's failure modes — tools missing from the probed `PATH`, a
stale process that does not serve the route — are pinned offline by the
new tests (`test_smoke_script.py`), with the live run here proving the
passing half, as the section above did for its status check.

## Follow-through 2026-10-27 — the host stands on the gate at `e0f48e5`

The five-flow section above ran before the push landed, from a delivered
copy with the host still at `f9a74f7`. This closes that loop; it is a
follow-through rather than another re-run because the re-run itself
revealed nothing new — the point is that the *standing* host now passes
the gate.

- **Pushed:** `3ebe517..e0f48e5`; CI
  [run 40511378322](https://github.com/Samuel-Ku/ps4-uk-stream/actions/runs/40511378322)
  finished green on both interpreters (`gates (py3.11)` 2m36s,
  `gates (py3.13)` 2m29s), so `29499b3` and this record are CI-covered —
  superseding the section above's "CI has not seen `29499b3` yet".
- **Fast-forwarded:** `f9a74f7 → e0f48e5` (7 files, no runtime dependency
  change, so no venv re-sync and no restart); tracked files stayed clean,
  and the host script's SHA-256 matched the committed copy again
  (`37ecc46f`). The service was untouched: `MainPID` 3445439, running
  since 2026-10-27 12:00:47 UTC.
- **Standard-path re-run:**
  `CS_UK_ENGINE_URL=http://192.168.2.223:3347 backend/deploy/smoke.sh`
  → the same five flows, `11 passed, 0 failed, 1 skipped`, exit `0`,
  **126 s** (13:29:37 → 13:31:43 UTC): the same `g3:tt35969257` skip,
  "Dune: Part Two" end to end, and teleport `400` plus tools resolved in
  the service's own `PATH`.
- **Retired:** `/tmp/smoke-29499b3.sh` removed (verified gone). The
  command block in the section above is historical; the fast-forward
  recipe is the live one again.
