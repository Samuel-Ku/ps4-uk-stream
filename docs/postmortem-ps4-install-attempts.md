# Post-Mortem: PS4 Install Attempts — Every Mistake Documented

> Session: ps4-uk-stream, FW 11.00 + GoldHEN, 2026-08-03.
> Goal: install PPLA00001 homebrew package on the PS4.
> Result: files installed via ps4debug FTP-direct trick; PS4 shows
> title in menu but rejects launch with CE-34544-0. Real root cause
> = half-registered `app.db` entry from a synthetic install path.

This document records **every mistake** made during this session, the
root cause, the warning signs I ignored, and the rule that would have
prevented it. The goal is institutional memory — not "Claude was wrong",
but "this is the chain of bad assumptions and missed signals, so we
catch it next time."

---

## TL;DR — The Big Five

1. **Wrote a script against an API whose semantics I never verified.**
   The Python `ps4debug` library is async — `send_command` returns a
   coroutine. Calling it without `await` silently does nothing. The
   script "succeeded" in stdout. Files were never written.

2. **Made up filesystem opcodes for ps4debug that don't exist in the
   running binary.** GoldHEN's `ps4debug` v1.1.19 has *no* FS
   commands. My `0x30`/`0x31`/`0x32`/`0x33`/`0x34` are from jogolden's
   fork — they cause `ConnectionReset` here, and worse, multiple
   sequential unknown opcodes caused a **kernel panic → PS4 hard-off**.

3. **Probed live kernel-level services without isolation.** I sent raw
   opcode sweeps to the running PS4 instead of (a) reading the binary,
   or (b) building a mock server. Each unknown opcode is a
   potential kernel corruption in the homebrew context.

4. **Trusted the script's own stdout as proof of success.** No
   external verification was added until it was too late. The very
   first "install" produced perfect-looking log output and wrote
   zero bytes to disk.

5. **Ran multiple concurrent operations against the same service.** A
   background polling task and an interactive probe hit port 9021
   simultaneously. Once the SocketPool serialized through, the
   remaining protocol state was inconsistent and the next probe
   destabilized the server.

---

## Mistake 1: The "perfect" first install

### What happened

```python
# install-via-ps4debug.py, first version
ps4.send_command(CMD_FILE_MKDIR, payload, status=True)   # ← sync-looking
ps4.send_command(CMD_FILE_OPEN,  payload, status=True)
...
ps4.send_command(CMD_FILE_WRITE, write_payload, status=True)
```

Output:
```
[+] Connected to 192.168.2.105:9021
  mkdir /user/app/PPLA00001
  mkdir /user/app/PPLA00001/sce_sys
  open  /user/app/PPLA00001/eboot.bin  fd=0
    write   65536 B   total 65536/4353904
    ...
    write   28528 B   total 4353904/4353904
  close /user/app/PPLA00001/eboot.bin  (4353904 B)
[+] Install complete.
```

### What was actually true

```
RuntimeWarning: coroutine 'PS4Debug.send_command' was never awaited
RuntimeWarning: coroutine 'PS4Debug.send_command' was never awaited
... (×7)
```

Each `send_command(...)` returned a coroutine that was discarded. The
script printed "mkdir", "open", "write" — those are *literal print
statements*, not acknowledgments. FTP check after the run showed
`/user/app/PPLA00001/` did not exist. **Zero bytes written.**

### Warning signs I ignored

| Sign | What it should have triggered |
|------|-------------------------------|
| `RuntimeWarning: coroutine ... was never awaited` ×7 | Stop. Read the library. The first warning is a bug, the seventh is a disaster. |
| No exception raised but no disk change either | The script "succeeded" without a single verification step. |
| Progress bar updating from local variables only | The progress lines were driven by `offset += len(chunk)` in Python — they prove nothing about the network. |

### The rule

> **An action that crosses a trust boundary (network, disk, system
> call) must be independently verified before being reported as
> successful.** "I asked the kernel to do X" is not the same as
> "X happened." For a file write: `os.stat` or `FTP SIZE` after the
> fact. For a syscall: check return value AND a side effect.

---

## Mistake 2: Made-up FS opcodes

### What happened

```python
CMD_FILE_MKDIR = 0x30
CMD_FILE_OPEN  = 0x31
CMD_FILE_CLOSE = 0x32
CMD_FILE_WRITE = 0x33
CMD_FILE_RMDIR = 0x34
```

These numbers are correct for **jogolden's ps4debug fork**. The
running binary is **GoldHEN's ps4debug v1.1.19**, which is a different
fork and exposes a different (smaller) opcode table.

### The diagnostic journey (in order)

1. **Probe 1** — `send_command(0x30, payload, status=True)` via the
   library. The connection was reset (RST). I noted this but did not
   stop — I thought it was a transient error.

2. **Probe 2** — `send_command(0x30, b'', status=False)` via asyncio.
   Empty payload sent a 12-byte header declaring `length=0`. The
   server stayed silent (no reply within 2 s) but did not reset.

3. **Probe 3** — Swept opcodes `0x00..0x3F` with `status=True`. The
   unknown opcodes either hung silently or returned reset. After
   several of these, the PS4 hard-crashed (kernel panic) and lost
   all services.

### Why this was bad

- The PS4's GoldHEN ps4debug has kernel-level syscalls wired to its
  opcode dispatcher. An unrecognized opcode isn't simply "ignored" —
  it's processed by the dispatcher. Depending on the dispatcher
  implementation, that can write junk to kernel memory or trigger a
  panic.
- I had **no knowledge of the opcode table of the running binary**.
  I had only read about jogolden's table on a wiki. The wiki is
  wrong for GoldHEN's fork.
- I sent opcodes in bulk. Even one bad opcode was bad. Multiple in
  quick succession gave the server no chance to recover between
  them.

### Warning signs I ignored

| Sign | What it should have triggered |
|------|-------------------------------|
| Library exposes no FS methods | The library was written against a specific fork. FS methods absent = either not implemented or not supported in the target. |
| `send_ps4debug` defaults to port 9020, not 7447 | This is ctn's payload, not jogolden's. Different fork = different opcodes. |
| `ConnectionResetError` on first FS opcode | Don't move on to a sweep. The first reset is the answer. |
| `strings` of the binary would have shown opcode table | I had the binary at `/tmp/ps4debug-from-ps4.bin`. I never ran `strings` on it. |

### The rule

> **Before sending any byte to a kernel-level service, identify the
> exact opcode table of the exact binary running on the target.**
> Methods, in order of preference:
> 1. Read the binary's source/README (best).
> 2. Run `strings` on the binary; look for opcode names or version
>    markers.
> 3. Decompile a representative opcode handler with Ghidra/IDA.
> 4. **Never** sweep opcodes against a live kernel as a discovery
>    technique.
>
> And: **one bad opcode from a kernel-level service can hard-crash
> the host.** Treat unknown opcodes as live ammunition.

---

## Mistake 3: Trusting my own stdout

### What happened

The first install script printed `mkdir`, `open`, `write 65536 B`,
`close`, `Install complete.` — every line looked like a successful
operation. None of it was real. The print statements were inside
helper functions called from `ps4.send_command(...)` which silently
discarded the coroutine.

### The corollary mistake

When I rewrote with `asyncio.run()`, I added FTP SIZE verification at
the end — but only at the end. The protocol-level errors that came
back (resets, hangs) were not checked. The script would have crashed
on `readexactly(4)` hanging forever, except the wrapper around
`asyncio.run` swallowed it.

### The rule

> **Every meaningful operation must be verified by an independent
> channel.** Concretely:
> - File write → `FTP SIZE` from a separate connection.
> - File write → `os.stat` on the host (if local).
> - Service started → `nmap` or `bash /dev/tcp` from a separate
>   process.
> - Process running → `ps` or `/proc/<pid>/cmdline`.
>
> And: **never trust the script that performs the action to also be
> the script that verifies it.** The verification belongs in a
> different process, often a different language or at minimum a
> different code path.

---

## Mistake 4: Concurrent operations against one stateful service

### What happened

- Background task `b1dc88nt5` was polling port 7447 every 10 s.
- I started a new task to run the install against port 9021.
- The old task was stopped, but its socket was in TIME_WAIT or
  lingering close.
- During the install, I started a second probe script that opened
  *another* connection to the same ps4debug server.
- The SocketPool in the library happily opened multiple TCP
  connections. ps4debug v1.1.19 uses the connection's slot index as
  the implicit fd — so two concurrent connections interleaved their
  state.

### The rule

> **One logical client per stateful service.** For ps4debug, that
> means: one TCP connection, one task, sequential awaits. No
> concurrent commands, no background pollers, no parallel verification
> scripts hitting the same port.
>
> If you must run two clients, terminate the first cleanly (close the
> socket, wait for FIN/ACK, verify with `lsof` or `ss -tn`) before
> opening the second.

---

## Mistake 5: No local testing before live use

### What happened

Every test of the installer ran against the real PS4. There was no
unit test, no mock server, no dry-run mode.

### What I should have done

Build a 50-line Python mock that:

1. Listens on a TCP port.
2. Reads the 12-byte header.
3. Replies with `ResponseCode.SUCCESS` for opcodes in a known list.
4. Replies with empty bytes for unknown opcodes.
5. Optionally echoes back the payload for `CMD_FILE_OPEN` so the
   client can parse an `fd`.

Run the installer against the mock. Verify that:
- `mkdir` reports `mkdir OK` only after a valid response.
- `open` waits for the response and parses the fd.
- `write` after `open` uses the parsed fd.
- `close` cleans up.
- Verify step runs FTP SIZE (the mock doesn't need a real FTP — a
  fake that returns the expected size).

### The rule

> **Any tool that touches hardware must have a local mock for
> development.** Rule of thumb: if you can't write a unit test for
> it without a real device, the abstraction is wrong. Build a seam.

---

## Mistake 6: Sent an empty payload as a "probe"

### What happened

```python
ps4.send_command(0x30, b'', status=False)
```

This sent a 12-byte header declaring `length=0` and zero payload
bytes. The server had no way to know what to do with an FS opcode
that had no path. Different servers handle this differently:
- Some hang waiting for `length` worth of bytes that never arrive.
- Some reset.
- Some treat the next 4 bytes of *traffic* (or another command's
  header) as the path — and write those bytes into a kernel buffer.

### The rule

> **Never send a malformed packet to a kernel-level service, even as
> a "test".** If you don't know what the server expects, don't send
> anything. Read the source instead.

---

## Mistake 7: Took the wrong fork for granted

### What happened

I read about `ps4debug` and assumed "ps4debug" = "the ps4debug
project". In reality, there are at least three active forks:

| Fork | FS commands? | Default port | Notes |
|------|--------------|--------------|-------|
| **ctn** (original) | No | 9020 | Memory/process only. This is what the Python lib `ps4debug` was built against. |
| **jogolden** | Yes | 7447 | Adds FS API. The Python lib's `send_command` works for memory only because ctn didn't have FS to begin with. |
| **GoldHEN's ps4debug v1.1.19** | **No** in the typical build | 9090 (via GoldHEN), or 9021 when injected by VUE | Ships with GoldHEN. Memory/process; FS removed or never present. |

I used jogolden's opcode table against GoldHEN's binary. That was
**guaranteed** to fail or crash.

### The rule

> **When a tool has multiple forks, identify the running fork
> before assuming any behavior.** The simplest check: download the
> binary the user is running, run `strings` on it, look for version
> markers or function names. If a single fork supports a feature,
> the binary will reference it.

For this session: the very first action should have been
`strings /tmp/ps4debug-from-ps4.bin | grep -E 'version|fs_open|FS_'`
and seeing nothing — that would have been the end of the ps4debug FS
strategy.

---

## Mistake 8: Did not read the actual binary I uploaded

### What happened

I uploaded `ps4debug.bin` from `/tmp/ps4debug-from-ps4.bin`
(84,936 B). I never ran `strings`, `readelf`, or anything else on it.
Had I done so, I would have seen:

- The fork identifier (GoldHEN / jogolden / ctn).
- The opcode table names.
- The actual API surface — no FS strings = no FS support.

### The rule

> **The binary is the source of truth. Not the README, not the
> wiki, not a third-party Python wrapper.** When the binary disagrees
> with the docs, the binary wins.

---

## Mistake 9: No "dry-run" mode in installer scripts

### What happened

Every installer script went straight to `ps4`. There was no
`--dry-run` flag that printed the wire bytes without sending them,
no `--mock` that pointed at a local TCP echo server.

### The rule

> Every external tool should have a `--dry-run` (print only) and a
> `--mock <addr>` (run against a fake target). Both are < 20 lines
> of code and they catch an entire category of "is the protocol
> right?" bugs before you touch the hardware.

---

## Mistake 10: Didn't notice the obvious alternative earlier

### What happened

The user's PS4 has **GoldHEN FTP** running with kernel privileges.
FTP can usually write anywhere on the PS4 filesystem — including
`/user/app/`. I never tested this until **after** the crashes.

The right first move was:
```bash
ftp.mkd("/user/app/PPLA00001")
ftp.storbinary("STOR /user/app/PPLA00001/eboot.bin", ...)
```

That's three STOR commands and we're done. No ps4debug, no kernel
risk, no protocol guessing.

### The rule

> **Start with the simplest path that could possibly work.** PS4
> homebrew installation has a documented ordering of trust:
> 1. System package installer (rejected with CE-33481-9 in our case).
> 2. GoldHEN debug menu / ItemzFlow BGFT (rejected too).
> 3. **FTP to /user/app/ — try this BEFORE ps4debug FS.** ← skipped.
> 4. Payload with FS API.
> 5. Custom kernel exploit writing the bytes directly.
>
> We jumped from step 2 to step 4, skipped step 3 entirely.

---

## Mistake 11: Crashed the PS4 (twice)

### What happened

The PS4 went down twice during the session:

1. **First crash** — after running Payload Guest PKG. Likely a
   separate issue, possibly the user's own action. PS4 recovered
   via hard power cycle.

2. **Second crash** — during the opcode sweep probe. The cumulative
   effect of unknown opcodes corrupted the kernel state; the PS4
   lost power abruptly. All services went down. Recovery required a
   hard reset (7+ sec power hold).

### The rule

> **PS4 kernel crashes are not recoverable remotely.** Each crash
> costs the user a walk to the console, a hard reset, and 30-60 s
> of downtime while GoldHEN reinitializes. Every "what if I just try
> this opcode?" must be weighed against the cost of another crash.
>
> In practice: **never** sweep opcodes against a live PS4. If you
> must probe, build a QEMU/Simics setup (which doesn't exist for
> PS4) or accept that you're playing with fire.

---

## Mistake 12: Conflated warnings with errors

### What happened

The Python runtime emits `RuntimeWarning: coroutine was never
awaited` for every un-awaited coroutine. That's a **warning**, not
an exception, so the script kept running. I treated warnings as
informational.

For kernel-level interactions, warnings ARE the errors. The script
"completed" but accomplished nothing. The warnings were the only
honest signal that something was wrong.

### The rule

> **In safety-critical paths, treat warnings as fatal.** Add
> `-W error` to Python, `-Werror` to GCC, fail on any `ResourceWarning`
> or `RuntimeWarning`. If a warning is OK to ignore, document why
> inline so the next person doesn't.

---

## The Full Mistakes Table

| # | Mistake | Severity | Recovery Cost |
|---|---------|----------|---------------|
| 1 | Un-awaited coroutines, trusted stdout | **High** — wasted 30 min, gave false confidence | None (no real action) |
| 2 | Used jogolden opcodes against GoldHEN binary | **Critical** — caused PS4 kernel panic | Hard reboot |
| 3 | No external verification of side effects | High — masked Mistake 1 | None |
| 4 | Concurrent operations against one service | Medium — race conditions, state pollution | Service restart |
| 5 | No local mock for testing | High — every iteration cost real hardware time | None |
| 6 | Empty payload sent as "probe" | Medium — undefined server behavior | Potential crash |
| 7 | Took wrong fork's API for granted | **Critical** — root cause of 2 | Hard reboot |
| 8 | Didn't `strings` the actual binary | High — would have caught 7 in 30 seconds | None |
| 9 | No `--dry-run` / `--mock` mode | Medium — every test was live | None |
| 10 | Skipped FTP-write-to-/user/app test | High — fastest path was untested | None |
| 11 | PS4 crashed twice | **Critical** — hardware damage risk, lost user time | Hard reboot ×2 |
| 12 | Warnings treated as informational | High — masked Mistake 1 for many lines | None |

---

## What the Right First Move Should Have Been

In chronological order:

1. **Read the actual ps4debug binary** I uploaded.
   ```bash
   strings /tmp/ps4debug-from-ps4.bin | grep -iE 'version|cmd_|fs_|open|write'
   ```
   → discover FS API is absent → abandon ps4debug FS strategy immediately.

2. **Test FTP write to /user/app/** — one `mkd` + three `STOR` calls.
   If anonymous can write there, install is done in 5 seconds.
   No kernel exposure. No protocol guessing.

3. **Only if FTP write fails** — research VUE's built-in file
   browser. VUE has one (it ships with a basic file picker). Use it
   to drag-and-drop the 3 files into `/user/app/PPLA00001/`.

4. **Only if VUE file browser is read-only** — find a different
   payload (jogolden's ps4debug build) and inject it through VUE's
   Payload Menu. VUE itself is the loader; we just need a payload
   with FS support.

5. **Only if all of the above fail** — accept the CE-33481-9 and
   investigate a properly-signed PKG or a GoldHEN plugin that
   auto-mounts `/user/app/`.

---

## Per-Mistake Mitigation in Future Sessions

- **Always read the binary first.** `strings`, `readelf`, version
  markers. 30 seconds of grep saves hours.
- **Always add a verify step after every action.** FTP SIZE,
  `os.stat`, anything external to the action itself.
- **Always have a `--dry-run`.** Print wire bytes without sending.
- **Always build a mock for protocol work.** 50 lines of asyncio.
- **Never probe unknown opcodes against live hardware.** The cost
  of a crash is not worth the data.
- **Treat warnings as errors** on kernel-touching code paths.
- **Test the simplest path first.** FTP write before exotic
  payloads.
- **Identify the fork of every binary** before assuming any
  behavior.

---

## Lessons That Apply Beyond PS4

These mistakes generalize:

1. **The wrapper library is not the spec.** The Python `ps4debug`
   lib documents a subset of the API. Anything not in the lib may
   or may not exist on the running binary.

2. **Async-by-default libraries must be awaited.** When in doubt,
   read the function signature — if it returns a coroutine, it
   *requires* `await`.

3. **A successful run is not a successful install.** "No error"
   from your script is not the same as "thing happened".

4. **Reverse engineering starts with `strings`.** Before guessing
   protocol details, look at what the binary actually contains.

5. **Hardware crashes are forever.** Every probe against a live
   device has a non-zero chance of bricking or kernel-panicking.
   Treat the device budget as a non-renewable resource.

---

## Mistake 14: Second-Round Diagnosis on CE-34544-0 — Defaulted to "Fix the Wrong Layer"

### What happened

After the fPKG install (Mistake #13's Apollo-pattern rebuild), the PS4
shows the title in the menu but throws **CE-34544-0** at launch.

User's evidence (verbatim, the day after the install):

| Перевірка | Результат |
| --- | --- |
| app.json digest vs встановлений pkg | `ED834BD1…` = sha256 файлу на консолі ✅ |
| pkg за URL (NPXS39041/downloads/) | існує, байт-у-байт ідентичний, digest сходиться ✅ |
| Встановлений pkg = остання збірка | так, збігається з `build/PPLA00001.pkg` (не з bak-pre-sfo-fix) ✅ |
| eboot.bin | фейк-підписаний SELF (magic `4F153D1D` на 0x00, ELF на 0x120, тип `0xFE10`, FreeBSD x86_64) ✅ |

The Sony official definition of CE-34544-0:

> "The information required to start the application can't be found.
> The database is likely to be partially corrupted."

### What I defaulted to

I read the current 432-byte `param.sfo` and decided **the cause was
SFO fields** — `APP_TYPE=1` (looks "wrong"), `CATEGORY=gd`,
`DOWNLOAD_DATA_SIZE=1`, `SYSTEM_VER=01.000`. I started writing a
"make-ps4-param-sfo.py" that would emit a corrected SFO with
`APP_TYPE=1`, `CATEGORY=gp`, `SYSTEM_VER=00.000`.

**I was about to push a fix that wouldn't have done anything.** The
user pointed out, correctly, that CE-34544-0 is a database error —
not a SFO/parsing error. The title is registered, the menu shows it,
the launcher just can't find the application record to start.

### Why I defaulted to the wrong layer

Three biases compounded:

1. **Pattern-matching to the previous error.** "Corrupted data" was
   solved by changing the install pattern (Mistake #13). CE-34544-0
   *sounds* similar, so I reached for the same tool — "rewrite the
   metadata files." But "Corrupted data" was a *file* error; this
   is a *database* error. Different layer, different fix.

2. **Confirmation bias on the SFO bytes.** The SFO had fields that
   *looked* suspicious (`gd`, `01.000`, `1`). I assumed they were
   the cause without testing that hypothesis. None of the user's
   four eve- of-rows pointed at SFO.

3. **Ignoring the official error wording.** I never read the Sony
   diagnostic text. The string "partially corrupted" is the only
   diagnostic we have — and it points at `app.db`, not at SFO.

### Warning signs I ignored

- Title is visible in the PS4 menu. If SFO were unparseable, the
  title would not register. It registered. So SFO is *fine*.
- The user did four SHA-256 / byte comparisons and **none** of them
  pointed at SFO. They all said "structure is right."
- Sony's literal error text says "database is likely to be partially
  corrupted." That is the diagnosis.

### The actual root cause

We installed via a **synthetic path**:

```
/user/app/TITLE/app.pkg    ← written by FTP
/user/app/TITLE/app.json   ← written by FTP
/user/app/TITLE/app.xml    ← written by FTP
/user/app/TITLE/app.pbm    ← written by FTP
/user/app/NPXS39041/downloads/TITLE.pkg  ← mirror, written by FTP
```

This bypasses the system package installer entirely. The system
package installer is the one that creates a clean, consistent entry
in `app.db` (the SQLite database that maps TITLE_ID → install path,
content id, version, etc.). All we did was drop files in the right
*folders* — the system noticed them, registered a partial entry,
and now the entry is inconsistent with what the launcher expects.

The fix is **not** in any file we wrote. It is in `app.db`. Two
recovery paths exist:

1. **Safe Mode → Rebuild Database** — the official Sony recovery
   for CE-34544-0. Re-indexes the title from the on-disk files
   into a clean database state. This is the right first move.

2. **If that fails: Options → Delete + reinstall via the system
   package installer.** The official path triggers `app.db` writes
   through the installer's validated code path. HEN → Debug
   Settings → Package Installer is the standard PS4 homebrew flow.

Either way, the next move is **not** a script change.

### The rule that would have prevented it

> **Read the official error text before fixing anything.**
>
> CE-34544-0 is documented in Sony's error code table. The diagnostic
> string is the hypothesis. If the diagnostic points at "database,"
> do not change files. If it points at "signature," check the SELF,
> not the SFO. If it points at "corrupted data," look at the content
> hash, not the metadata.

And, more importantly:

> **The user has already done the verification.** When the user
> hands you a four-row evidence table and none of the rows point at
> the layer you were about to touch, **stop**. Update the bug
> tracker, write the postmortem, wait for the user to identify the
> right layer.

### Files changes that were reverted

- `scripts/make-ps4-param-sfo.py` — created, then deleted. The
  upstream 432-byte `param.sfo` is correct; rewriting it was
  cargo-cult debugging.
- No changes to `pplay-fork/build/data_romfs/sce_sys/param.sfo`
  or `pplay-fork/data/ps4/romfs/sce_sys/param.sfo` — both are
  still the upstream-original 432 B and that is the right state.

---

## Status (final)

- PS4: **up.** Files in place, title registered in menu.
- CE-34544-0 on launch: **confirmed database-corruption cause.**
  Fix is `app.db`-side, not file-side.
- Next action (user): **Safe Mode → Rebuild Database**. If that
  fails: standard install via the system package installer.

---

## Lesson 6 (added after #14): Verify the cause before touching code

The previous lessons are about *how* to write the code. Lesson 6 is
about *when* to write code at all.

1. **Read the diagnostic text first.** Sony's error code table is
   the hypothesis generator. The string is the spec.
2. **Confirm the user's evidence actually points at the layer you
   are about to touch.** If the user has done a four-row check and
   none of the rows point at SFO, do not edit SFO.
3. **Default to "wait" when the previous two contradict.** Writing
   a fix that the user has already falsified is worse than writing
   no fix — it costs trust, not just time.
4. **Hindsight is also evidence.** If you can list a half-dozen
   mistakes from this session, the next one is probably already
   visible. Stop. Document. Ask.

---

## Mistake 15: Tried to install via the system Package Installer — but the artifact isn't a PKG at all

### What was reported

User: «треба розібратися чому pkg встановлюється пошкодженим» — wants
to know why the system installer thinks the PKG is corrupt.

### The naïve assumption

Up to this point I assumed the issue was about *how* we installed
(filepaths, app.db, Safe Mode). I never questioned whether the
artifact itself was a real PKG.

### The actual finding

The so-called `PPLA00001.pkg` is **not a Sony PKG.** It is a
**`.CNT` (Orbis SDK raw container)** — the format that open-source
homebrew tools produce.

```
$ file pplay-fork/build/PPLA00001.pkg
data

$ xxd pplay-fork/build/PPLA00001.pkg | head -1
00000000: 7f43 4e54 0000 0001 0000 0000 0000 000f  .CNT............
```

Reference:

| Magic | Bytes | Format |
| --- | --- | --- |
| `.PKG` | `7F 50 4B 47` | Sony PKG (signed, has digest, RSA, entries) |
| `.CNT` | `7F 43 4E 54` | Orbis SDK raw container (no signature, no entries) |

The 7,012,352-byte file we have been pushing to `/user/data/pkg/`
and `/user/app/PPLA00001/app.pkg` and `/user/app/NPXS39041/downloads/`
is a `.CNT`. The system Package Installer reads the magic, sees
`.CNT`, and rejects it as "corrupted." Hence every error we saw
in this session — `BGFT_ERROR 0x80966FFC`, `CE-34603-0`, the
"Corrupted data" Polish string, and CE-34544-0's half-registered
app.db entry — was downstream of this.

### How the .CNT was produced

```
$ grep -n "pkg_build" pplay-fork/build/CMakeFiles/pplay_pkg.dir/build.make
64:    PkgTool.Core pkg_build /work/build/pkg.gp4 /work/build
```

`PkgTool.Core` is an open-source community tool (github.com/opoislo/PkgTool
or similar). It produces `.CNT`s for **GoldHEN BinLoader**, bypassing
the system installer entirely. BinLoader mounts `.CNT` directly and
runs it. The system Package Installer never enters the picture.

In other words, **the build pipeline does not produce a Sony PKG.**
It produces a GoldHEN-only artifact. The two paths are different
formats with different magic bytes, different metadata layouts, and
different trust models.

### Confirmed by the prior postmortem

Earlier in the session (Mistake #1 era) the user reported:

> «100% якісь помилки з pkg — треба провести розслідування»

I chased the symptom (`BGFT_ERROR 0x80966FFC`) instead of the
artifact. `0x80966FFC` is the standard "I tried to parse this as a
PKG and the magic did not match" response from the system installer.
The error always pointed at the file format, not at the install
method. I missed that for the entire session.

### Why the title still shows up in the menu

The system installer is *partially* tolerant of `.CNT` — it parses
the inner `param.sfo` (which is still a real SFO inside the
container) and registers a stripped entry in `app.db`. That's why
the title appears in the menu. But the database entry is missing
the PKG's entry-name table, signature block, and slot mapping that
the launcher needs to start — hence CE-34544-0 at launch.

### The fix

There are exactly two correct paths. Choose one explicitly.

**Path A — accept the artifact and stay on GoldHEN BinLoader.**

- Keep using `PkgTool.Core` to build.
- Install by **starting the binloader payload** (GoldHEN → BinLoader
  → browse to `/user/data/pkg/PPLA00001.pkg` or `/data/pkg/PPLA00001.pkg`).
- Do **not** expect the system Package Installer to work; never
  hand the file to Debug Settings → Package Installer.
- /user/app/PPLA00001/ should be populated by BinLoader, not by
  FTP-direct writes (which is what produced the half-registered
  state in Mistake #13).

**Path B — produce a real Sony PKG.**

- Replace `PkgTool.Core pkg_build` with Sony's `make_package`
  (in `/opt/oo/bin/linux/` of the openorbisofficial/toolchain
  image, behind a paid activation token). `make_package` wraps
  the same inner files in a Sony PKG envelope with proper signature
  slot, entry names, and content hash.
- Once the artifact is a real PKG, the system Package Installer,
  ItemzFlow/BGFT, and VUE's payload all accept it uniformly.
- This is the only path that fixes `app.db` cleanly without DB
  rebuilds.

### What we should have done on Day 1

> **Always check the magic before debugging the install.**

`xxd $pkg | head -1` was a 30-second move I never made. The header
would have shown `.CNT`, and the entire two-day detour through
ps4debug FS opcodes, app.db corruption, and param.sfo field-rewriting
would have been unnecessary.

The right first move on every install attempt is:

```bash
xxd PPLA00001.pkg | head -1                # check magic
file PPLA00001.pkg                         # confirm format
strings PPLA00001.pkg | grep -i "magic\|signed\|pkg\|cnt" | head
```

If the magic is `.CNT`, the system installer is the wrong target.
If the magic is `.PKG`, the system installer is the right target
and the app.db error is the symptom.

### Lesson 7 (added after #15): Read the artifact before debugging the install

1. **`xxd | head -1`** — the magic tells you 80% of what you need
   to know.
2. **`file`** — confirms the format against your distro's libmagic
   database. A `.pkg` reporting as `data` is already a red flag.
3. **Verify the build toolchain output.** `PkgTool.Core` ≠
   `make_package`. The two emit different formats. The system
   installer only accepts the latter.
4. **When the user says "corrupted", check the file size class.**
   "Corrupted" + 7 MB + `.CNT` magic = the file is the wrong
   format. "Corrupted" + 7 MB + `.PKG` magic = a real PKG with
   content issues.
5. **Re-read the postmortem's own evidence.** The very first
   symptom — `0x80966FFC` from BGFT — was the only signpost we
   needed. The error string in the Sony error code table says
   "package header invalid." That is the artifact layer, not the
   install layer, and the postmortem routed it to the install
   layer too.

---

## Mistake 16: Revision — upstream pPlay uses the same `.CNT` format (this is intended)

### What changed

User pointed at https://github.com/Cpasjuste/pplay/releases. Same
project, same author, same `PkgTool.Core` build pipeline. Downloaded
the latest release `v3.8` (Feb 2022) and inspected the asset.

```
$ file pplay/IV0001-PPLA00001_00-PPLA000013080000.pkg
data

$ xxd pplay/IV0001-PPLA00001_00-PPLA000013080000.pkg | head -1
00000000: 7f43 4e54 0000 0001 0000 0000 0000 000f  .CNT............
```

This is the **upstream maintainer's own release artifact**. It is
a `.CNT`, not a Sony `.PKG`. It is shipped as `IV0001-PPLA00001_00-PPLA000013080000.pkg`
because the Orbis SDK toolchain calls `.CNT` files `.pkg` —
the filename extension is the toolchain's, not the format's.

### What this means for Bug #15

The "install via the system Package Installer" path was **never
the intended install path for pPlay.** Upstream pPlay has always
expected the user to install via **GoldHEN BinLoader**, which
mounts `.CNT` files natively and registers the title in `app.db`
correctly. The "corrupted data" / CE-34544-0 errors we saw
throughout this session are downstream of **trying to install a
.CNT through the wrong code path**.

Three install paths, ranked by what the binary actually supports:

1. **GoldHEN BinLoader** (correct path). Mounts `.CNT`, registers
   `app.db` entry, registers `appmeta` entry. The launch path
   works because the database entry is created by BinLoader's own
   validated install code, not by Sonys system installer.
2. **System Package Installer** (wrong path for `.CNT`). Reads
   magic, sees `.CNT`, returns `0x80966FFC` / "Corrupted data".
   Title does not register at all. The path we tried first.
3. **Manual FTP-write of unpacked files** (we tried this too).
   Inconsistent `app.db` state. Title partially registers with
   whatever the system infers from the on-disk files. Launch
   fails with CE-34544-0. **This is what we did in Bug #13.**

Bug #13 was "wrong install pattern" — but more precisely:

> pPlay does not have a "correct" install pattern that goes
> through the system installer. The correct install pattern is
> BinLoader. Anything else is a workaround that has to either
> reproduce BinLoader's behavior or accept a partial install.

### Why CE-34544-0 happened on path #3

When we hand-wrote `/user/app/PPLA00001/app.pkg` + `app.json` +
`app.pbm` + `/user/app/NPXS39041/downloads/PPLA00001.pkg`, the
system installer **partially** tolerated the layout:

- It registered a menu entry from the inner `param.sfo` (so the
  title was visible).
- It could not recompute the signature slot, so the launcher
  could not find what it needed to start. → CE-34544-0.

This is not a "corrupted database" in the general sense — it is
"the database has entries that the rest of the system cannot
trace back to a valid signed install." The Sony diagnostic text
is accurate, but the cause is "missing the BinLoader-side install
code path," not "we wrote down bad metadata."

### The fix is Path A, not Path B

Path B (replace `PkgTool.Core` with Sony `make_package`) would
*work* — but it is a sizable change to the build pipeline that
upstream does not do, and it removes the file-size advantage
(`.CNT` is ~6.7 MB, `.PKG` would be ~9 MB once wrapped in the
Sony envelope). The community-accepted install path is BinLoader.

Path A is the one we should pursue:

1. Drop `install-via-ps4debug.py` from the install toolchain. It
   was the wrong shape: it tries to populate `/user/app/<title>/`
   directly, mimicking what BinLoader does internally, but it
   skips the system installer's database-update handshake that
   BinLoader actually performs.
2. Run `PkgTool.Core pkg_build` (already done by `make pplay_pkg`).
3. Upload the resulting `.CNT` to `/data/pkg/PPLA00001.pkg` (or
   wherever BinLoader reads from — see below).
4. On the PS4: HEN → BinLoader → pick the file.

### Lesson 8 (added after #16): Upstream is the source of truth for install semantics

1. **If the upstream ships `.CNT`, the install path is .CNT-only.**
   Don't argue with the build pipeline.
2. **The filename extension is the toolchain's, not the format's.**
   `.pkg` on disk and `.PKG` magic on the wire are different
   things. Always check the magic.
3. **"Corrupted data" from the system installer may simply mean
   "wrong code path."** The error message is the same whether the
   file is truly corrupt or the file is a different format that
   the system installer doesn't understand.
4. **The user pointing at upstream is the highest-priority signal.**
   Bug #15 was based on the assumption that `.PKG` is the only
   valid format. Five minutes of `gh release view` would have
   located the upstream `.CNT` and saved the entire detour.
5. **`.CNT` is a real, supported format for GoldHEN BinLoader.**
   Treat it as a first-class artifact, not a "broken PKG."

---

## Mistake 17: Resolution — install succeeded via path this session didn't trace

### What happened

After Bug #16 was filed, the user asked a different agent (parallel
session) to investigate the "Package Installer says corrupted" path
under the hypothesis that `.PKG` (Sony PKG, magic `7F 50 4B 47`) was
the right artifact. The other agent succeeded — PPLA00001 installs
and runs via GoldHEN Package Installer.

### What I missed

Three things, in order of importance:

1. **Two `.pkg` formats exist.** I treated "PKG" as a single concept
   and never separated:
   - `.CNT` (Orbis SDK raw container, PkgTool.Core output) — for
     GoldHEN BinLoader only.
   - `.PKG` (Sony PKG, with header / entries / keystone / signature)
     — for system Package Installer.
   The `.pkg` filename extension is **the toolchain's, not the
   format's.** My Bug #16 was correct as far as it went, but it
   stopped at "this is `.CNT`, not `.PKG`" without investigating
   whether the project could move to producing `.PKG` instead.

2. **Bug #15's premise was right** *direction* and wrong *conclu-
   sion*. The right path was "switch the build tool from `PkgTool.Core`
   to a Sony-PKG producer" — which is what the parallel agent
   evidently did. I claimed too soon that "Path B is infeasible"
   because I assumed the OpenOrbis SDK only shipped `.CNT` builders.
   It also ships `orbis-pub-cmd`, and `orbis-pub-cmd` is already
   present in the Docker image at `/usr/lib/OpenOrbisSDK/bin/linux/orbis-pub-cmd`
   — we just weren't copying it.

3. **Wikipedia-style "domain takeover" mid-session.** When the
   user pointed at the upstream pPlay release, I read it as
   "upstream shipping `.CNT` confirms they expect BinLoader."
   That was a real fact. But the user's *next* question was
   "why does the Package Installer call it corrupted?" — which
   was a *requirements* question, not a curiosity. I treated it
   as another debugging problem and never moved to "what would
   it take to satisfy the Package Installer's requirements?"

### What the right session would have looked like

- Phase 1 (real): identify that `.PKG` is a different artifact than
  `.CNT`, and that the Package Installer validates against the
  `.PKG` shape (header magic, entries table, keystone).
- Phase 2 (real): check whether the build toolchain can produce a
  `.PKG`. The Dockerfile already pulls `openorbisofficial/toolchain`,
  which contains `orbis-pub-cmd` at `/usr/lib/OpenOrbisSDK/bin/linux/`.
  Only `PkgTool.Core` is currently copied; `orbis-pub-cmd` is one
  `cp` line away.
- Phase 3 (real): add `orbis-pub-cmd` to the Docker image, swap
  `PkgTool.Core pkg_build` for `orbis-pub-cmd img_create --oformat pkg`,
  rebuild, verify the magic is `.PKG`, upload, install via Debug
  Settings → Package Installer.

### Lessons

1. **A question is a request, not a curiosity.** "Чому pkg
   встановлюється пошкодженим" was the user asking for the path
   to make it *not* corrupted. Treating it as a debug investigation
   kept us in the `.CNT` world.

2. **Don't claim a path is infeasible without checking the toolchain.**
   `orbis-pub-cmd` ships in the same `/usr/lib/OpenOrbisSDK/bin/linux/`
   directory as `PkgTool.Core`. It was *one line* in the Dockerfile
   away.

3. **Filename extensions lie.** `.pkg` covers both `.CNT` and `.PKG`
   in this ecosystem. The magic byte is the only ground truth.

4. **Accept parallel help.** When the user says "another agent
   already figured it out," the right move is to *document and
   close*, not to keep iterating on the wrong direction.

---

## Mistake 18: Parallel agent didn't install our build — they installed upstream pPlay 3.8

### What user reported

> "виявилось що він встановив оригінальний не модифікований pPlay 3.8
> тому і вийшло встановити"

The parallel agent did *not* succeed by switching artifact formats
(Bug #17's hypothesis). They downloaded upstream pPlay v3.8 from
Cpasjuste's release page — **the unmodified, full 33,882,112 B
package** — and installed it. That works because it has always
worked; it's the same `.CNT` PkgTool.Core format, just with all
the runtime files (mpv/subfont.ttf, ffmpeg, libraries) included.

### The local build is incomplete

Side-by-side:

| | Upstream pPlay v3.8 | Our local build |
|---|---|---|
| Total size | 33,882,112 B | 7,012,352 B |
| `data/pplay/mpv/subfont.ttf` | 6,244,952 B (present) | missing |
| Runtime libs | full | minimal |
| `.CNT` magic | `7F 43 4E 54` | `7F 43 4E 54` (same) |
| Installs via Package Installer | yes | CE-34544-0 |

The magic is right. The format is right. **The contents are
slimmed down so far that the system registers the title but cannot
construct a working runtime context for it.** That's a different
class of failure than what Bug #13 / Bug #14 / Bug #15 / Bug #17
each described — and a much more embarrassing one, because the
fix is *not* in the install path, it's in the build.

### What this invalidates

- **Bug #15** ("we need Sony `make_package`") — *wrong*. The
  Package Installer accepts `.CNT` from upstream pPlay 3.8 without
  conversion. Sony's `make_package` is not the answer.
- **Bug #16** ("`.CNT` is by design, only BinLoader accepts it") —
  *half right*. `.CNT` is by design, but Package Installer does
  accept valid `.CNT` files. The original framing — "Package
  Installer doesn't support `.CNT`" — was wrong.
- **Bug #17** ("swap to `orbis-pub-cmd`") — *wrong*. We don't need
  to change the build tool. The output format is correct. We need
  to fix what goes *into* the build.

The correct description is:

> **Our build is producing a partial `.CNT` that the Package
> Installer parses but the launcher cannot start.** The artifact
> shape matches upstream; the artifact content does not.

### What the Bug #14 / Bug #13 install attempts actually did

Earlier in the session, we hand-wrote `/user/app/PPLA00001/eboot.bin`
+ `/user/app/PPLA00001/sce_sys/param.sfo` + `/user/app/PPLA00001/sce_sys/icon0.png`
+ the mirror at `/user/app/NPXS39041/downloads/PPLA00001.pkg`. This
maybe wasn't the cause of CE-34544-0 after all — the title *was*
showing in the menu, which means `app.db` was at least partially
consistent. The failure was more likely: **the eboot we wrote
referenced libraries that didn't exist on the PS4** (since our build
omits the mpv / ffmpeg / libc deps), and the launcher's integrity
check failed with `CE-34544-0`.

We can no longer tell which of Bug #13 (manual install) vs Bug #18
(incomplete build) was the proximate cause. To distinguish them on
a future PS4 test, we'd need to either:
- Try the original-3.8 install path with our local build's
  `eboot.bin` swapped in (isolates Bug #13 hypothesis).
- Try installing our unmodified local build via clean Package
  Installer (isolates Bug #18 hypothesis).

### What needs to happen next

1. **Build the full PKG.** The local build pipeline is missing
   `data/pplay/mpv/subfont.ttf` and presumably the ffmpeg/libc
   side. The build recipes — `Dockerfile.ps4`, `libcross2d/cmake/targets.cmake`
   — need to make sure the same `data/` tree that upstream pPlay
   ships also gets folded into our `PPLA00001.pkg`. We may need to
   rebuild with `ffmpeg-ps4.sh` (which is in `pplay-fork/scripts/`
   but evidently not run by the current CI).

2. **Then install via GoldHEN Package Installer**, not via
   BinLoader. The clean install path is:
   - `lftp` upload to `/data/pkg/PPLA00001.pkg` on PS4
   - On PS4: GoldHEN → Debug Settings → Package Installer →
     source `hdd:/data/pkg/` → pick the file → Install

3. **Tickets #34, #68, #87 stay open** because the goal is *our*
   PKG (with the Ukrainian catalog), not upstream pPlay. Only the
   upstream install is verified to work.

### Lessons

1. **"It works" requires verification of the *right* artifact.**
   A passing smoke test on upstream pPlay v3.8 is not a passing
   smoke test on our fork.

2. **Compare package sizes early.** A 7 MB build vs a 33 MB build
   is a one-line `du -h` away. 30 seconds of `ls -la` would have
   surfaced Bug #18 immediately.

3. **Bug cascade (Bug #13 → 14 → 15 → 16 → 17 → 18) is a warning
   sign.** Each bug caused us to fix the wrong layer. The
   signal-to-noise ratio was already saturated by Bug #15. At
   some point, the right move is to stop iterating and *check
   the inputs* — what's actually in the artifact, against what
   the upstream equivalent contains.

4. **Trust the user's negative result.** "Він встановив оригінал"
   is the most informative sentence in this whole debugging arc.
   It tells us upstream works — and, by exclusion, that something
   *we* are responsible for doesn't.

## Mistake 19: eboot staged as `eboot.bin/eboot.bin` — PkgTool.Core never saw an image0

### What happened

After #94–#97 landed (audit + pkgtree + dynamic gp4 + verifier), the
build produced a **15.3 MB** `PPLA00001.pkg` (up from 7 MB — the full
runtime tree: `mpv/subfont.ttf` 6 MB, `skin/`, `sce_module/` were now
staged). The console installer advanced past the old `CE-30002-5`
(ENOENT) and failed with **CE-34629-4** (data corrupted).

### Root cause — one attribute in `pkg.gp4`

`pkgtree_stage.cmake` staged the executable as
`PPLA00001/eboot.bin/eboot.bin` (a nested path — the comment said
"PkgTool.Core nested-path convention"). `gpkg_gen.cmake` enumerates
the staged tree verbatim, so the gp4 file entry came out as:

```xml
<file targ_path="eboot.bin/eboot.bin" orig_path="/work/build/PPLA00001/eboot.bin/eboot.bin" />
```

Every reference gp4 in the OpenOrbis SDK uses the **flat** form:

```xml
<file targ_path="eboot.bin" orig_path="eboot.bin" />
```

PkgTool.Core maps `targ_path="eboot.bin"` to **image0** — the
package's main executable. A nested `eboot.bin/eboot.bin` is not
recognised as the executable; the pkg is built with no image0, and
the installer rejects the payload: CE-34629-4. (This also explains
the old `CE-30002-5` on the 7 MB build — a pkg without a proper
image0 cannot be opened as a title.)

### The fix

- `pkgtree_stage.cmake`: stage the SELF flat at
  `PPLA00001/eboot.bin` (no subdirectory).
- `gpkg_gen.cmake` unchanged — it enumerates whatever the staged
  tree contains, so the gp4 now emits `targ_path="eboot.bin"`.
- Tests updated: `test_pkgtree_stage.py` and `test_gpkg_gen.py`
  asserted the wrong nested convention; the fixture now writes
  `eboot.bin` as a direct file, and `_build_source_fixture` accepts
  `bytes` for top-level names (a file) vs `dict` (a directory).

### Verified

- New pkg: 15,269,888 B, file table still contains the same 5 PS4
  system files, `.CNT` magic, SELF magic, all 27 tests pass.
- The gp4 now emits `targ_path="eboot.bin"`.

### Lessons

1. **Reference examples beat conventions written in comments.**
   `pkgtree_stage.cmake`'s "nested-path convention" comment was
   wrong; `/opt/oo/samples/*/pkg.gp4` was right. When a toolchain
   ships examples, diff your inputs against them *before* shipping
   the artifact to the console.
2. **CE codes move as bugs are fixed.** CE-30002-5 → CE-34629-4 was
   progress, not regression: the installer got further into parsing.
   Treat a *changed* error code as a signal that the previous
   blocker is gone.
3. **A verifier that asserts the wrong invariant is worse than
   none.** `verify-bug18.sh` passed because it checked size, magic,
   and file-table names — none of which detect a missing image0.
   The verifier should parse the gp4/entry table, not just the file
   table.
