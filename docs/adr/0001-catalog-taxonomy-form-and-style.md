# Catalog taxonomy: form + style

The catalog separates **form** (cinematic vs episodic) from **style** (anime / cartoon / dorama / none), each captured as its own field on every content item, so ambiguous content like "дитяче аніме мультфільм" can carry both tags rather than being forced into one. The model's authoritative terms live in [CONTEXT.md](../../CONTEXT.md); this ADR records *why* this shape, not *what* it is.

## Status

Accepted (2026-08-02, after grilling session).

## Context

The PS4 catalog aggregates content from 19 Ukrainian providers — films, serials, anime, cartoons, doramas. Each item lives under one `MediaType`. The v2 spec defined that type as a single `Literal["movie", "series", "anime", "cartoon", "dorama"]`. In practice this conflated two orthogonal axes (the *form* of the content with the *style* tag on it) and forced every ambiguous item to be classified into exactly one of five slots, losing information ("ангемотне" children's anime мультики, anime films styled as `movie` but sharing the anime catalogue, etc.).

A grilling session walked the resulting design tree across 9 dependent decisions (form vs style axes; single value vs set; empty-set vs sentinel; provider-capabilities shape; section filter semantics; `/api/search` filter contract; adult-content scope; Section identity).

## Decision

Adopt Model B as the canonical taxonomy. Its surface rules:

- `MediaForm = "movie" | "series"` — required on every item, no None.
- `MediaStyle = "anime" | "cartoon" | "dorama"` — optional tag(s) carried as a `frozenset[MediaStyle]`; **empty frozenset** (not `"live"`) means ordinary live-action content.
- A single item can carry multiple styles (e.g. `{anime, cartoon}` for a children's anime мультик).
- `ProviderCapabilities` exposes two independent sets (`forms`, `styles`) — the rollup of what each provider offers; precise cross-product lives in Sections.
- `Section` filters by `{form?, styles: frozenset[MediaStyle] | None}` with three-case semantics: `None` = any, `∅` = ordinary-only, non-empty = intersection.
- `/api/search` mirrors Section filter axes (`?form=&style=…`) but supports only the 2-of-3 cases (ordinary-only is reachable only through a Section, not through a query parameter).
- Section identity is the tuple `(provider, id)`; `Section.id` is per-provider scoped, cross-provider collisions are harmless.
- No `is_adult` field anywhere — adult content is signalled by provider name and section title; per the spec, no hiding/disabled-by-default mechanism.

Definitions, examples, and edge cases live in [CONTEXT.md](../../CONTEXT.md).

## Considered Options

The full chain considered three Model-A alternatives before B was chosen, and smaller alternatives at each downstream step.

- **Model A — single `MediaType` enum** (`Literal["movie", "series", "anime", "cartoon", "dorama"]`): rejected. Anime film becomes `movie`, anime serial becomes `anime` — same style, different types, no way to recover the lost axis. Forces "дитяче аніме мультики" into exactly one slot.
- **Model C — three-tier hierarchical type** (`form × style × release-format`, e.g. `series.anime.ona`): rejected for v1. Release-format (ONA / OVA / Special) is genuinely section-level, not per-item, in current provider data. Adding a third axis now would be YAGNI.
- **`"live"` as a fourth MediaStyle sentinel** (instead of empty frozenset): rejected. Conflated "default" with "tag", invited UI bugs ("why are live-action items showing a 'live' badge?"), required a special case wherever `styles` is filtered.
- **Per-item single-style (`style: MediaStyle | None`)**: rejected. Preserved the model's "pick one" failure mode one level down. The whole reason for Model B is to escape that constraint.
- **ProviderCapabilities as `(form, style)` tuples** (Variant B): rejected for now. Real catalog usage has no provider that needs the precision ("only anime movies, never ordinary movies"). Sections still encode precise filters, so the precision is not lost — only deferred.
- **Two-field Section styles (subset `all` + intersection `any`)**: rejected. Real catalog has no "all-of" section (a "дитяче аніме мультики" section that needs both anime AND cartoon). YAGNI; adding `styles_all` later is non-breaking.
- **`/api/search` separate `/api/discover` endpoint** for filter UI: rejected. Two endpoints with the same filter axes would diverge; one endpoint keeps the mental model uniform.
- **`is_adult: bool` field on Section / Provider / Item** (even display-only): rejected. Spec explicitly declined any flag mechanism; any field on the wire becomes a potential filter; provider identity + section title carry the signal.
- **Globally unique Section IDs** (namespaced `uaflix-films`, `animeua-anime-serial`): rejected. Cross-provider Section-id equality is not a useful operation; the API already keys on `(provider, id)`.

## Consequences

- **API contract change.** `/api/sections`, `/api/browse`, `/api/search`, `/api/content/{id}` JSON shapes will diverge from the v2 spec (§3.2–3.6 of `docs/superpowers/specs/2026-08-01-ps4-uk-stream-design.md`). All 19 provider adapters need updates to emit the new shape (per-item `styles` set, per-section filter axes). On the wire, `MediaType` is replaced by `form` + `styles`.
- **pplay-fork side.** `ScreenResults`, `ScreenContent`, `ScreenSections` consume the new contract — type rendering, icon logic, and section grouping need to be aware that the same item can carry `styles = frozenset({"anime", "cartoon"})`. Default to rendering a small icon for each distinct style.
- **Caching.** `/api/search` cache key gains `form` and `styles` axes. Sections are still pre-cached per provider.
- **Future work surface.** Adding a new MediaStyle (e.g. `"documentary"`) is a Literal/Enum extension plus provider updates, not a schema overhaul. Adding `styles_all` (subset semantics) to Sections is a non-breaking optional field. Adding `is_adult` later remains possible if the spec gets revised; this ADR will be the first thing to argue against it.
- **Spec reconciliation.** `docs/superpowers/specs/2026-08-01-ps4-uk-stream-design.md` still describes Model A. After this ADR is accepted, that spec section is **obsolete in spirit**; it remains the source of truth for the parts not affected by Model B (architecture, data flow, error envelopes).

## References

- [`CONTEXT.md`](../../CONTEXT.md) — glossary of `MediaForm`, `MediaStyle`, `ProviderCapabilities`, `Section`, search/browse filter contract, adult-content scope, Section identity.
- Obsolete: `docs/superpowers/specs/2026-08-01-ps4-uk-stream-design.md` §3.2 (`MediaType` enum) — to be superseded in a spec revision.
