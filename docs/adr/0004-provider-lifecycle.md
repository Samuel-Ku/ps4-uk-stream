# Provider lifecycle

The backend treats provider lifecycle as a deployment-time, code-reviewed concern. The lifecycle surface remains deliberately small: providers are explicit in the registry, requests discover upstream reachability lazily, and unavailable providers are removed from active registration rather than represented by new runtime metadata.

## Status

Accepted (2026-08-02, after grilling session Q34–Q40).

## Context

The backend aggregates 19 Ukrainian providers through `BaseProvider` instances stored in `PROVIDERS`. Today `_registry.py` imports provider classes and calls `register(...)` in a fixed sequence. `/api/search` iterates that registry, while `/api/providers` reports provider identity, capabilities, and the health status tracked by issue #53's sliding window. Provider adapters are code and are deployed with the backend; no requirement exists for operators or users to add providers at runtime.

The implementation also needs a consistent answer for upstream sites that disappear, provider-list changes, and the ordering and reachability assumptions exposed to the PS4 client. The decision must preserve the project's minimum-surface principle and avoid turning deployment-time code changes into a second configuration system.

## Decision

Keep provider lifecycle as an explicit, deployment-time code workflow:

- **Registration:** retain hardcoded `register(...)` calls in `backend/cs_uk_api/providers/_registry.py`. The registry is the authoritative active-provider list and its call order is meaningful.
- **Hot-reload:** do not support hot-reload. A provider-list change takes effect when the backend process is restarted as part of deployment.
- **Health tracking:** issue #53 and its v3 sliding-window health design remain the source of truth for runtime health. This ADR does not redefine thresholds, samples, status values, or `/api/providers` health fields; it owns only lifecycle decisions not covered by #53.
- **Retirement:** comment out the provider's registration in `_registry.py` and retain the adapter/source for historical context or possible reactivation. This is the convention already used for Banderakino.
- **Ordering / priority:** preserve registry order. `/api/search` returns the flattened provider results in `PROVIDERS.values()` order; no priority field or secondary sort is added.
- **Startup discovery:** do not ping providers at startup. Reachability is learned lazily from the first applicable request and recorded through the existing health tracker. Deterministic local prerequisites may still produce an explicit startup marker, as with missing Uakino Chromium. Uakino is the one bounded single-provider exception to "no startup probing" — a deterministic Chromium prerequisite plus a Cloudflare-bypassing browser session, warmed once per process rather than swept (2026-08-09 amendment below).
- **Adding a provider:** follow the existing workflow: create the adapter under `providers/`, add live-captured fixtures and provider tests, implement and validate the `BaseProvider` boundary, register it in `_registry.py`, update provider triage, and run the live gate. The detailed eight-step checklist remains in `docs/status.md`.

## Considered Options

- **Config-file-driven registration:** rejected. YAML/JSON would add parsing, validation, packaging, and drift without a demonstrated runtime-registration need.
- **Environment-variable-driven registration:** rejected. Environment variables are unsuitable for structured provider metadata and would make the active list less explicit.
- **File watcher or re-import hot-reload:** rejected. Provider adapters are deployed code; re-importing mutable global registrations adds state and failure modes without a requirement.
- **Restart-on-change versus no hot-reload:** restart is the operational consequence of the decision, not a new reload mechanism; no in-process reload is added.
- **ADR-owned health contract:** rejected. Issue #53 already owns sliding-window tracking and `/api/providers` status; duplicating that contract would create competing sources of truth.
- **Startup health sweep:** rejected. It delays startup, creates a thundering herd, and treats temporary upstream reachability as a deployment gate; lazy requests already feed health tracking.
- **Config flag or retired-provider registry:** rejected. A second lifecycle list would duplicate the active registry and add fields or synchronization rules; commenting out registration is sufficient.
- **Explicit priority field:** rejected. Registry order already provides deterministic ordering and preserves the current search behavior; a field would add surface without a user requirement.
- **Alphabetical or result-score ordering:** rejected. Both would change established provider/result ordering and require new tie-break and relevance rules.
- **Runtime plugin discovery:** rejected. Providers are trusted application code, not third-party runtime plugins; explicit imports keep review and deployment behavior visible.

## Consequences

- Provider availability changes require a normal code change and backend restart; there is no runtime registration or reload API.
- Registry order is a small but intentional part of the search contract. Reordering `register(...)` calls can change result order and should therefore be reviewed as behavior, not formatting.
- Retired adapters remain available in source but do not appear in `PROVIDERS` or `/api/providers`, and cannot receive search, content, browse, or stream requests.
- Runtime health remains separate from lifecycle membership: an active provider can be `ok`, `degraded`, or `down` under issue #53, while retirement removes it from active registration.
- Startup is not coupled to upstream availability. The first request may encounter an upstream error, which is recorded by the existing tracker.
- Adding a provider requires fixtures and tests plus one explicit registry edit; no frontend contract change is required when the existing API shapes are respected.

## Amendment: uakino background warm (2026-08-09)

The "do not ping providers at startup" rule above targets multi-provider
sweeps and transient probing: a startup sweep delays boot, thundering-herds
upstreams, and makes a temporary outage a deployment gate. Uakino is a
narrow, deliberate exception (issues #193/#195):

- **Deterministic prerequisite, not a probe.** Uakino's session requires
  a system Chromium binary at `UAKINO_CHROMIUM` — a deterministic local
  fact the process checks once, not an upstream reachability probe. If
  Chromium is absent the lifespan pins a `chromium_missing` startup
  marker and never launches a session.
- **Bounded, single-provider, once per process.** The lifespan warms the
  session in the background at startup — the ``warm()`` call itself is
  bounded by the session's ``WARM_TIMEOUT_S`` — and the session holds a
  one-process ``asyncio.Lock`` so warm / heartbeat / fetch never
  interleave. A heartbeat every 5 minutes keeps the session warm and
  feeds the #53 sliding window, so a cold start recovers without user
  action. Startup cost is one browser launch, not a sweep.
- **No re-sweep.** Lazy discovery still governs every other provider;
  this amendment does not re-open multi-provider startup sweeps, which
  remain rejected.

## References

- [`CONTEXT.md`](../../CONTEXT.md) — canonical provider lifecycle terms and minimum-surface principles.
- [`backend/cs_uk_api/providers/_registry.py`](../../backend/cs_uk_api/providers/_registry.py) — active registration order.
- [`backend/cs_uk_api/providers/__init__.py`](../../backend/cs_uk_api/providers/__init__.py) — `PROVIDERS` and `register(...)` implementation.
- [`backend/cs_uk_api/main.py`](../../backend/cs_uk_api/main.py) — `/api/providers`, lazy request handling, and registry-order search flattening.
- [`backend/cs_uk_api/health.py`](../../backend/cs_uk_api/health.py) — issue #53 sliding-window health tracker.
- [`docs/status.md`](../status.md) — provider count, Banderakino retirement context, and provider-addition workflow.
- GitHub issue [#53](https://github.com/Samuel-Ku/ps4-uk-stream/issues/53) — provider health sliding window and `/api/providers` status; source of truth for health tracking.
