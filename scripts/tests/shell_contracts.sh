#!/usr/bin/env bash
# Exit-contract tests for the mission tooling: scripts/next-ticket.sh and
# backend/deploy/env-probe.sh.
#
# Both scripts promise exit codes the tracker docs rely on (0 found / 1 none
# / 2 usage for the selector; 0 ready / 1 blocked / 2 usage for the probe).
# Nothing else in CI executes them, so this pins the promises with stubbed
# dependencies: no network, no gh auth, no console, no docker socket.
#
# Run: scripts/tests/shell_contracts.sh
# Exits 0 when every case passes, 1 when any fails, 2 on a setup failure.
set -uo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
NEXT_TICKET="${NEXT_TICKET:-$REPO_ROOT/scripts/next-ticket.sh}"
ENV_PROBE="${ENV_PROBE:-$REPO_ROOT/backend/deploy/env-probe.sh}"

for script in "$NEXT_TICKET" "$ENV_PROBE"; do
  [ -x "$script" ] || { echo "shell-contracts: not executable: $script" >&2; exit 2; }
done

PASS=0
FAIL=0
pass() { echo "  PASS  $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }

TMP=$(mktemp -d) || { echo "shell-contracts: mktemp failed" >&2; exit 2; }
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"
mkdir -p "$BIN" || exit 2

# stub <name> <body> — writes an executable stub on PATH.
stub() {
  printf '#!/bin/sh\n%s\n' "$2" > "$BIN/$1"
  chmod +x "$BIN/$1"
}

# expect <label> <want-rc> <want-stdout> <command...>
expect() {
  local label=$1 want_rc=$2 want_out=$3
  shift 3
  local out rc
  out=$("$@" 2>/dev/null)
  rc=$?
  if [ "$rc" -eq "$want_rc" ] && [ "$out" = "$want_out" ]; then
    pass "$label"
  else
    fail "$label: rc $rc (want $want_rc), stdout '$out' (want '$want_out')"
  fi
}

# expect_rc <label> <want-rc> <command...>
expect_rc() {
  local label=$1 want_rc=$2
  shift 2
  "$@" >/dev/null 2>&1
  local rc=$?
  if [ "$rc" -eq "$want_rc" ]; then
    pass "$label (rc $rc)"
  else
    fail "$label: rc $rc, want $want_rc"
  fi
}

echo "next-ticket.sh"
stub gh 'echo 42'
expect "lowest available number" 0 42 env PATH="$BIN:$PATH" "$NEXT_TICKET"
stub gh 'echo none'
expect "empty queue" 1 none env PATH="$BIN:$PATH" "$NEXT_TICKET"
stub gh 'exit 1'
expect "gh failure" 2 "" env PATH="$BIN:$PATH" "$NEXT_TICKET"
expect_rc "bad argument" 2 "$NEXT_TICKET" --bogus
expect_rc "help" 0 "$NEXT_TICKET" --help

echo "env-probe.sh"
git init -q "$TMP/switchfin" || { echo "shell-contracts: git init failed" >&2; exit 2; }
for tool in ping timeout docker pacbrew-package create-fself pkg-tool; do
  stub "$tool" 'exit 0'
done
printf '#!/bin/sh\nprintf "List of devices attached\\nABC123\\tdevice product:foster\\n"\n' > "$BIN/adb"
chmod +x "$BIN/adb"

expect_rc "all six pass" 0 env PATH="$BIN:$PATH" SWITCHFIN_DIR="$TMP/switchfin" "$ENV_PROBE"
stub ping 'exit 1'
expect_rc "one failing probe" 1 env PATH="$BIN:$PATH" SWITCHFIN_DIR="$TMP/switchfin" "$ENV_PROBE"
expect_rc "bad argument" 2 "$ENV_PROBE" --bogus
expect_rc "help" 0 "$ENV_PROBE" --help

echo
echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
