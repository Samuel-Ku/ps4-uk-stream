#!/usr/bin/env bash
# Environment readiness for the PS4 build/toolchain lane — read-only.
#
# The hardware/toolchain tickets name six probes in their `Blocked by`
# sections (#450, #452). This runs them in one command so "is that lane
# startable yet?" is not six hand-typed checks:
#
#   console       ping the console (FW 11.00 + GoldHEN)
#   goldhen-ftp   TCP connect to GoldHEN's ftp port (file transfer)
#   adb           adb is installed and lists a device in `device` state
#   pacbrew       pacbrew-package, create-fself and pkg-tool on PATH
#   docker        the daemon answers `docker info`
#   switchfin     backend/tools/switchfin's submodules are initialized
#
# Exit status (a wrapper, systemd or a ticket can trust it):
#   0  every probe passes — the environment can run the blocked lane
#   1  at least one probe fails — the FAIL lines name which
#   2  usage error
#
# Usage:
#   ./env-probe.sh
#
# Env:
#   PS4_HOST         console address     (default 192.168.2.105)
#   PS4_FTP_PORT     GoldHEN ftp port     (default 2121)
#   PS4_PING_WAIT    ping wait seconds    (default 2)
#   SWITCHFIN_DIR    checkout to inspect (default: <repo>/backend/tools/switchfin)
set -uo pipefail
# No `set -e`: every probe must run and report, even after one fails.

HOST="${PS4_HOST:-192.168.2.105}"
FTP_PORT="${PS4_FTP_PORT:-2121}"
PING_WAIT="${PS4_PING_WAIT:-2}"
REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
SWITCHFIN="${SWITCHFIN_DIR:-$REPO_ROOT/backend/tools/switchfin}"

PASS=0
FAIL=0

ok()  { echo "  PASS  $(printf '%-13s' "$1") $2"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL  $(printf '%-13s' "$1") $2"; FAIL=$((FAIL + 1)); }

usage() {
  cat <<'EOF'
usage: env-probe.sh

Read-only readiness probe for the PS4 build/toolchain lane: console,
goldhen-ftp, adb, pacbrew, docker and switchfin.

Exit: 0 all pass, 1 any fails, 2 usage error.

Env: PS4_HOST (192.168.2.105), PS4_FTP_PORT (2121), PS4_PING_WAIT (2),
     SWITCHFIN_DIR (<repo>/backend/tools/switchfin)
EOF
}

probe_console() {
  if ! command -v ping >/dev/null 2>&1; then
    bad console "ping not on PATH (iputils-ping)"
    return
  fi
  if ping -c1 -W "$PING_WAIT" "$HOST" >/dev/null 2>&1; then
    ok console "$HOST answers ping"
  else
    bad console "$HOST does not answer ping (console off, or not on this network)"
  fi
}

probe_goldhen_ftp() {
  if ! command -v timeout >/dev/null 2>&1; then
    bad goldhen-ftp "timeout not on PATH (coreutils)"
    return
  fi
  if timeout "$PING_WAIT" bash -c "exec 3<>/dev/tcp/${HOST}/${FTP_PORT}" 2>/dev/null; then
    ok goldhen-ftp "TCP $HOST:$FTP_PORT accepts a connection"
  else
    bad goldhen-ftp "TCP $HOST:$FTP_PORT refuses or times out (GoldHEN ftp not listening)"
  fi
}

probe_adb() {
  if ! command -v adb >/dev/null 2>&1; then
    bad adb "adb not on PATH (android-tools-adb)"
    return
  fi
  local listed devices
  if ! listed=$(adb devices -l 2>/dev/null); then
    bad adb "adb devices failed (server start refused)"
    return
  fi
  devices=$(printf '%s\n' "$listed" | awk 'NR > 1 && $2 == "device" {print $1}')
  if [ -n "$devices" ]; then
    ok adb "device(s) in state device: $(printf '%s' "$devices" | tr '\n' ' ')"
  else
    bad adb "no device in state device (the PS4 is not an adb device; this only proves adb sees hardware)"
  fi
}

probe_pacbrew() {
  local tool missing=()
  for tool in pacbrew-package create-fself pkg-tool; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    ok pacbrew "pacbrew-package, create-fself, pkg-tool on PATH"
  else
    bad pacbrew "missing from PATH: ${missing[*]}"
  fi
}

probe_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    bad docker "docker not on PATH"
    return
  fi
  local err
  if err=$(docker info 2>&1 >/dev/null); then
    ok docker "daemon answers docker info"
  else
    bad docker "daemon unreachable: $(printf '%s' "$err" | head -1)"
  fi
}

probe_switchfin() {
  local status uninit
  if ! status=$(git -C "$SWITCHFIN" submodule status 2>/dev/null); then
    bad switchfin "no git checkout at $SWITCHFIN (initialize the submodule)"
    return
  fi
  uninit=$(printf '%s\n' "$status" | awk '$1 ~ /^-/ {print $2}')
  if [ -n "$uninit" ]; then
    bad switchfin "uninitialized: $(printf '%s' "$uninit" | tr '\n' ' ')"
  else
    ok switchfin "all submodules initialized"
  fi
}

case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
  "") ;;
  *)
    echo "usage: $0" >&2
    exit 2
    ;;
esac

echo "env-probe: PS4 build/toolchain readiness (read-only)"
probe_console
probe_goldhen_ftp
probe_adb
probe_pacbrew
probe_docker
probe_switchfin

echo
if [ "$FAIL" -eq 0 ]; then
  echo "=== $PASS passed, 0 failed — READY ==="
  exit 0
fi
echo "=== $PASS passed, $FAIL failed — BLOCKED ==="
exit 1
