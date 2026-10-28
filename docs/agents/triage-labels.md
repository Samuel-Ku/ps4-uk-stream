# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

## Families beyond the five roles

The roles answer *who acts*. The families below answer *where the work lands*, *how urgent it is* and *why it is stuck*; a ticket carries the labels from each family that applies.

| Family | Labels | Meaning |
| ------ | ------ | ------- |
| Blocking | `status:blocked`, `status:needs-hardware`, `status:needs-toolchain` | A real dependency or absent environment — not a priority |
| Scope | `scope:backend`, `scope:ps4-build`, `scope:pplay-fork`, `scope:testing` | Which part of the tree the work lands in |
| Area | `area:api`, `area:extractor`, `area:provider`, `area:ui` | The subsystem inside a scope |
| Priority | `priority:high`, `priority:medium`, `priority:low` | Critical path, important, nice-to-have — never makes a blocked ticket startable |
| Wayfinding | `wayfinder:map`, `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, `wayfinder:task` | `/wayfinder` map and child types; operations in `issue-tracker.md` |

GitHub's default labels (`bug`, `enhancement`, `question`, …) are used as-is for the issue's type.

`status:needs-hardware` means the user's PS4 (FW 11.00 + GoldHEN); `status:needs-toolchain` means OpenOrbis or the full pPlay build dependencies. Both state what **this machine** cannot do yet, so they go stale in both directions: re-probe them (the ticket's `Blocked by` section names its probe) before trusting either the label or its absence.

## Availability (the mission loop)

The mission takes the **lowest-numbered available ticket**, closes it, then takes the next lowest available number. A ticket is **available** only when all three hold:

1. **Open, with no open blocker** — every issue in GitHub's native dependency list (`blockedBy`) is closed, and every issue named in the body's `Blocked by` line is closed where native dependencies are unavailable.
2. **No unsatisfied blocking label** — no `status:blocked`, and neither environment label while its probe still fails here.
3. **Actionable by the agent** — `ready-for-agent`, and not `ready-for-human`, `needs-info` or `needs-triage`.

The rule is a filter, not a licence to start the lowest number: a `ready-for-agent` ticket with an open blocker is an inconsistent record. Fix the record first — the labels and the `Blocked by` text must agree, each naming the concrete probe — and do not start it until they do.

One snapshot of the open set; prints the lowest available number, or `none`:

```bash
gh issue list --state open --limit 200 \
  --json number,labels,assignees,blockedBy \
  --jq '[.[]
        | select((.labels | map(.name) | index("ready-for-agent")) != null)
        | select((.blockedBy.nodes | map(select(.state == "OPEN"))) | length == 0)
        | select(([.labels[].name | select(startswith("status:"))]) | length == 0)
        | select((.assignees | length) == 0)
        | .number]
       | sort | first // "none"'
```

An assigned ticket is already claimed; the queue is unassigned tickets only.
