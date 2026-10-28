#!/usr/bin/env bash
# Lowest-numbered available ticket for the mission loop — read-only.
#
# "Available" is defined in docs/agents/triage-labels.md: open, no open
# native blocker, no blocking `status:*` label, `ready-for-agent`, and
# unassigned. This is the snapshot query from that doc, scripted so a
# mission step calls it instead of pasting jq each time.
#
# Prints the ticket number on stdout — or `none` — and nothing else, so
# the caller can branch on the value or on the exit status.
#
# Exit: 0 a number was printed, 1 none available, 2 usage or gh failure.
#
# Usage:
#   ./next-ticket.sh
set -uo pipefail

usage() {
  cat <<'EOF'
usage: next-ticket.sh

Prints the lowest-numbered available ticket for the mission loop, or
`none`. Read-only.

Exit: 0 found, 1 none available, 2 usage or gh failure.

Availability is defined in docs/agents/triage-labels.md.
EOF
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

if ! command -v gh >/dev/null 2>&1; then
  echo "next-ticket: gh not on PATH (install the GitHub CLI)" >&2
  exit 2
fi

if ! number=$(gh issue list --state open --limit 200 \
  --json number,labels,assignees,blockedBy \
  --jq '[.[]
        | select((.labels | map(.name) | index("ready-for-agent")) != null)
        | select((.blockedBy.nodes | map(select(.state == "OPEN"))) | length == 0)
        | select(([.labels[].name | select(startswith("status:"))]) | length == 0)
        | select((.assignees | length) == 0)
        | .number]
       | sort | first // "none"'); then
  echo "next-ticket: gh issue list failed (auth or network)" >&2
  exit 2
fi

echo "$number"
if [ "$number" = "none" ]; then
  exit 1
fi
exit 0
