# CLAUDE.md

## Agent skills

### Remote hosts

`openclaw-home` is a deployment mirror, not an authoring workspace. Checkout path, git identity and verification contract: `docs/agents/remote-hosts.md`.

### PS4 console

GoldHEN access (FTP/KLog), filesystem map, custom-PKG install and rollback, and the client's timeout/config coupling: `docs/agents/ps4-console.md`.

### Issue tracker

Issues live in GitHub Issues (via the `gh` CLI). External PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Roles are the canonical five (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`); the blocking, scope, area and priority families and the mission-loop availability rule are in `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
