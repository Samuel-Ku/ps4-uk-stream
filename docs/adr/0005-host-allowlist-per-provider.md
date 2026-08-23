# Host allowlist is a per-provider, centrally-enforced contract

The backend must never fetch an upstream-derived URL (one scraped from CMS HTML) unless its host is on the provider's declared allowlist. `safe_get` already enforces the check on the initial URL and every redirect hop, but the allowlist was re-declared as a module constant per adapter — and six adapters (animeon, animeua, bambooua, coaninet, hentaiukr, unimay) fetched upstream URLs with no allowlist at all, leaving a genuine SSRF gap (a hostile CMS page can point the backend at an arbitrary host from its LAN position).

**Decision:** the allowlist becomes a required class attribute on `BaseProvider` (`allowed_hosts: frozenset[str]`, alongside `sections` / `newest_section` / `can_gate`), and the SSRF check reads it centrally. The declaration lives with the adapter; the enforcement lives in `safe_get`. A new adapter that omits it fails closed — it cannot fetch upstream-derived URLs.

**Status:** Accepted (2026-08-16, from the architecture-review deepening pass).

**Considered Options**

- **Per-adapter module constants** (the status quo): gaps are possible by omission — six adapters had none. Rejected.
- **A central registry mapping provider → hosts** in one table: separates the host list from the adapter that knows it. Rejected — the adapter is the natural owner of its own hosts, and the declarative class attribute already matches the `sections`/`can_gate` pattern.
- **Default-allow** when no list is declared: defeats the point. Rejected.

**Consequences**

- The six gap adapters must have their CMS + player-CDN hosts enumerated as part of the migration (security-correctness core, not cleanup).
- A startup or test assertion enforces "every provider declares a non-empty allowlist", so a new adapter cannot forget it.
- Relaxing this later is a security decision, not a refactor.
