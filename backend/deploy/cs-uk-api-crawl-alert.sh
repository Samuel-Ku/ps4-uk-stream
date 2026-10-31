#!/usr/bin/env bash
# Alert on a failed or timed-out catalog crawl.
#
# cs-uk-api-index.service and cs-uk-api-index-full.service name this in
# OnFailure=, so systemd runs it once when a crawl exits non-zero or is
# killed at TimeoutStartSec (Result=timeout). The script always writes an
# ALERT block to the journal and then delivers the same text through every
# configured channel:
#
#   CS_UK_ALERT_URL       POST the message as the plain-text request body.
#                          Works with an ntfy topic URL, or any webhook that
#                          accepts text. Title/Priority/Tags headers are
#                          added for ntfy and ignored elsewhere.
#   CS_UK_ALERT_CMD       pipe the message to a shell command (Telegram
#                          curl, mail, a custom script).
#   CS_UK_ALERT_JOURNAL   "user" (default) or "system": which journal scope
#                          the failing crawl units live in.
#
# Both channels may be set. With neither, the alert stays journal-only and
# the script says so instead of failing silently.
#
# Usage: cs-uk-api-crawl-alert.sh [unit ...]
#   Defaults to both crawler units; units that are not failed are skipped,
#   so a stale call cannot announce a crawl that actually succeeded.

set -u

units=("$@")
if [ "${#units[@]}" -eq 0 ]; then
  units=(cs-uk-api-index.service cs-uk-api-index-full.service)
fi

scope=${CS_UK_ALERT_JOURNAL:-user}
if [ "$scope" = system ]; then
  systemctl_scope=(systemctl)
  journal_scope=(journalctl)
else
  systemctl_scope=(systemctl --user)
  journal_scope=(journalctl --user)
fi

host=$(hostname 2>/dev/null || uname -n)
now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
sections=()

for unit in "${units[@]}"; do
  # A oneshot that exited 0 is "inactive", not "failed"; only a real failure
  # or a timeout is worth waking someone for.
  active=$("${systemctl_scope[@]}" show "$unit" -p ActiveState --value 2>/dev/null || true)
  [ "$active" = failed ] || continue
  result=$("${systemctl_scope[@]}" show "$unit" -p Result --value 2>/dev/null || true)
  status=$("${systemctl_scope[@]}" show "$unit" -p ExecMainStatus --value 2>/dev/null || true)
  # A failed oneshot never reaches "active", so ActiveEnterTimestamp is
  # empty; the state change is when it entered "failed".
  since=$("${systemctl_scope[@]}" show "$unit" -p StateChangeTimestamp --value 2>/dev/null || true)
  log=$("${journal_scope[@]}" -u "$unit" -n 15 --no-pager -o cat 2>/dev/null || true)
  block=$(printf 'unit: %s\nresult: %s (exit status %s)\nsince: %s' \
    "$unit" "${result:-unknown}" "${status:-?}" "${since:-unknown}")
  if [ -n "$log" ]; then
    block="$block"$'\n'"last log:"$'\n'"$log"
  fi
  sections+=("$block")
done

if [ "${#sections[@]}" -eq 0 ]; then
  echo "alert: no failed crawl unit in the ${scope} scope; nothing to report"
  exit 0
fi

# One substitution for the whole body: appending a second $(...) would
# strip the blank line between the header and the first unit block.
message=$(
  printf '[cs-uk-api] crawl failed on %s at %s\n\n' "$host" "$now"
  printf '%s\n\n' "${sections[@]}"
)

# Always leave the verdict in the journal, even when delivery is configured:
# the journal is the one channel that cannot be misconfigured.
printf 'ALERT cs-uk-api crawl failed on %s\n%s\n' "$host" "$message"

if [ -n "${CS_UK_ALERT_URL:-}" ]; then
  if ! curl -fsS --max-time 10 -X POST \
    -H "Title: cs-uk-api crawl failed on $host" \
    -H "Priority: high" \
    -H "Tags: warning" \
    --data-binary "$message" \
    "$CS_UK_ALERT_URL"; then
    echo "alert: POST to CS_UK_ALERT_URL failed: $CS_UK_ALERT_URL" >&2
  fi
fi

if [ -n "${CS_UK_ALERT_CMD:-}" ]; then
  if ! printf '%s\n' "$message" | sh -c "$CS_UK_ALERT_CMD"; then
    echo "alert: CS_UK_ALERT_CMD exited non-zero" >&2
  fi
fi

if [ -z "${CS_UK_ALERT_URL:-}" ] && [ -z "${CS_UK_ALERT_CMD:-}" ]; then
  echo "alert: no CS_UK_ALERT_URL or CS_UK_ALERT_CMD configured; journal only"
fi

exit 0
