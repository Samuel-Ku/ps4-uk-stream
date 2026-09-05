# Domain Model Decisions — ps4-uk-stream

> Synthesized from the 2026-08-02 grilling session (Q1–Q20).
> Authoritative glossary lives in [`CONTEXT.md`](../../CONTEXT.md); catalog-taxonomy decision lives in [`docs/adr/0001-catalog-taxonomy-form-and-style.md`](../../adr/0001-catalog-taxonomy-form-and-style.md).
>
> The reasoning behind this document's existence: the v2 design spec ([`2026-08-01-ps4-uk-stream-design.md`](2026-08-01-ps4-uk-stream-design.md)) defined `MediaType` as a single literal enum that conflated two orthogonal axes. After implementation, a grilling session resolved all 20 dependent decisions across three domains. The implementation is already shipped (per `docs/status.md`); this spec records the resolved shape so future contributors do not have to re-decide it.

## Problem Statement

The PS4 catalog aggregator pulls content from 19 Ukrainian providers via a Linux-side FastAPI backend. The original v2 design spec defined `MediaType` as a single `Literal["movie", "series", "anime", "cartoon", "dorama"]` — a single axis that conflated form (cinematic vs episodic) with style (anime / cartoon / dorama / none). This forced ambiguous content into exactly one slot: an anime film became `movie`, an anime serial became `anime` — same style, different types, no way to recover the lost axis. "Дитяче аніме мультики" — content that is both anime and cartoon — had to pick one.

Three downstream surfaces were also ambiguous or undecided: stream URL contract, translation semantics, and adult-content scope. A grilling session walked the design tree across 20 dependent decisions and resolved each with a focus on minimum surface and non-breaking evolution.

## Solution

The post-grilling domain model resolves three independent sub-domains:

- **Catalog taxonomy — Model B** (form + style as independent axes on every content item).
- **Stream contract — minimum surface** (single URL, no quality / probe / TTL / fallback).
- **Translation contract — minimum surface** (`{id, label}` only, scoped ids, list[0]=default).

The current shape is the floor, not the ceiling. Each rejected alternative can be added later as a non-breaking optional field without touching existing clients.

## User Stories

### PS4 user flows

1. As a PS4 user browsing a provider's catalog, I want to see sections labelled with their form+style intent (e.g. "Серіали · Аніме"), so that I can decide where to look based on what I want to watch.
2. As a PS4 user with ambiguous taste (anime + cartoon), I want to find content tagged with both styles, so that I do not have to search twice.
3. As a PS4 user, I want ordinary live-action series to be discoverable as their own thing, so that they do not get lost in anime / cartoon categories.
4. As a PS4 user searching by title, I want to filter results by `?form=series&style=anime,cartoon`, so that I can narrow down without scrolling through all hits.
5. As a PS4 user opening a content, I want to see the available translations in priority order, so that the most likely-preferred one is at the top of the picker.
6. As a PS4 user opening an episode, I want translations that vary per episode to show in a per-episode picker, so that I do not pick a voice that does not exist on the episode I want.
7. As a PS4 user, I want the system to remember my voice preference across episodes when all episodes share the same set, so that I do not have to re-pick for every episode.

### Backend developer flows

8. As a backend developer adding a new provider, I want a single shape for `ContentResponse` and `Section`, so that I do not have to design a new schema per provider.
9. As a backend developer, I want the shape to be stable across provider additions, so that the Switchfin client (via the Jellyfin facade) does not need updates.
10. As a backend developer, I want `translation=None` on the stream endpoint to return a usable default, so that the PS4 client does not have to block on the user picking one when only one is available.
11. As a backend developer, I want a 12s budget on `/api/search` to return partial results on timeout, so that one slow provider does not kill the whole search.

### Switchfin consumer flows

12. As a Switchfin-side developer, I want `Section.styles` to be a frozenset with 3-case semantics, so that I can render the right filter hint without per-provider special cases.
13. As a Switchfin-side developer, I want `item.styles` to be a frozenset, so that I can render one icon per distinct style without pick-one heuristics.
14. As a Switchfin-side developer, I want `translations_level` to tell me whether to render a global picker or a per-episode picker, so that I do not have to inspect every episode to decide UI shape.
15. As a Switchfin-side developer, I want a single `StreamResponse.url` field, so that I do not have to handle multi-URL fallback chains that do not exist in the data.

### Domain maintainer flows

16. As a domain maintainer, I want every decision documented in `CONTEXT.md` and (where non-obvious) in an ADR, so that future contributors can read WHY before re-deciding.
17. As a domain maintainer, I want to add new `MediaStyle` values via Literal extension, not schema redesign, so that adding "documentary" does not break existing data.
18. As a domain maintainer, I want cross-provider Section-id collisions to be harmless, so that I do not have to namespace ids.

## Implementation Decisions

### Catalog taxonomy (Q1–Q10, ADR-0001)

- **Model B adopted**: independent `form` and `styles` axes on every content item.
- `MediaForm = "movie" | "series"` is **required** on every item, no `None`, no default.
- `MediaStyle = "anime" | "cartoon" | "dorama"` is **optional**, carried as `frozenset[MediaStyle]`.
- **Empty frozenset** (not a sentinel value) encodes ordinary live-action content.
- A single item may carry **multiple styles** via the frozenset (e.g. `{anime, cartoon}` for a children's anime мультик).
- `ProviderCapabilities` exposes two independent sets (`forms`, `styles`) — the rollup of what each provider offers. Precise cross-product lives in `Section`.
- `Section` filters by `{form?, styles: frozenset[MediaStyle] | None}` with **3-case semantics**: `None` = any (including empty), `∅` = ordinary-only, non-empty = intersection.
- `/api/search` mirrors Section filter axes (`?form=&style=…`) but exposes **only 2-of-3 cases** (ordinary-only is reachable only through a Section, not through a query parameter — deliberately no `?style=none` magic token).
- Section identity is the **tuple `(provider, id)`**; cross-provider Section-id equality is harmless.
- **No `is_adult` field** anywhere. Provider identity plus section title carry the signal; PS4 OS-level parental controls (if any) are out of scope for this catalog domain.

### Stream contract (Q11–Q15, minimum surface)

- `StreamResponse` keeps the spec §3.6 shape: `{url, type, headers}`.
- **Single URL only** — provider `stream()` picks the best upstream option; multi-URL alternatives are dropped, not exposed.
- **No `quality: str` field** — `type` and known CDN behaviour carry enough information; heuristic quality labels on filenames (`*_720p.mp4`) are unreliable.
- **No backend URL probe** — LAN topology (backend + PS4 share egress) means a 200 from backend ≈ a 200 from PS4; mpv surfaces playback errors directly.
- **No `expires_at` / TTL contract** — manifest URLs are short-lived but HLS segments refresh them transparently. No observed issue today.
- **No fallback chain** — PS4 retries by user action; backend does not rank URLs from the same provider as primary / fallback.
- **No fallback chain (amended after #373)** — the response still carries ONE
  URL; the PS4 still retries by user action, no ranked URL list is exposed.
  What the #373 live acceptance added is an INTERNAL bounded retry: when the
  policy pick's engine session is dead-on-arrival (EngineRejected — metadata
  never arrived in the add window), the provider advances to the next
  policy-ordered candidate, capped at ``_MAX_SESSION_ATTEMPTS = 3``, before
  reporting item-level ``not_found``. Lane-level failures (engine
  unreachable) never advance. The StreamResponse wire contract is unchanged.

### Translation contract (Q16–Q20, minimum surface)

- `Translation = {id, label}` — **no `language`, `kind`, `studio` fields**. PS4 displays label; user picks by it.
- `translations_level = "content" | "episode"` is **provider-decided**. AnimeUA / Kinotron auto-detect at parse time (uniform voices → `"content"`, varied → `"episode"`). Animeon / Uakino hardcode `"episode"` because their data structure guarantees per-episode variation.
- `Translation.id` is **per-provider scoped**. Same principle as `Section.id`: identity is `(provider, content_id, id)`. Cross-provider equality not guaranteed.
- **Default = `translations[0]` implicit**. PS4 sends `translation=None` to mean "default"; `stream()` returns the first available candidate. List order encodes provider priority.
- **Multiple studios stay distinct.** FunDub / AnimeUA / AniUA — three Ukrainian studios — remain three separate `Translation` entries with different ids. No merging. No `is_primary` flag.

### Wire-shape summary

The trimmed-to-decision canonical content / stream shapes (full schema in the backend models module):

```python
class Translation(BaseModel):
    id: str          # per-provider scoped, opaque to consumers
    label: str       # display label

TranslationLevel = Literal["content", "episode"]

class Episode(BaseModel):
    translations: list[Translation] | None = None

class ContentResponse(BaseModel):
    id: str
    form: MediaForm                # "movie" | "series" — required
    styles: frozenset[MediaStyle]  # ∅ = ordinary live-action
    title: str
    year: int | None = None
    description: str = ""
    poster: str | None = None
    translations: list[Translation] = Field(min_length=1)
    seasons: list[Season] | None = None
    translations_level: TranslationLevel = "content"

class StreamResponse(BaseModel):
    url: str
    type: StreamType = "mp4"
    headers: dict[str, str] = Field(default_factory=dict)
```

(Snippet trimmed to decision-rich fields; it encodes the resolved shape more precisely than prose. The full schema lives in the backend Pydantic models.)

## Testing Decisions

The implementation has these test seams, all already in place:

- **Provider-level fixture tests** — one test module per provider, 9–24 tests each. Live-captured HTML via `curl -sS https://...`, no invented fixtures. Validate the new `form` / `styles` emission against the captured upstream structure.
- **Backend integration tests** — 362 tests passing across the suite, including Section filter semantics, search filter axes, and translation-level auto-detection.
- **Jellyfin facade tests** — the facade's DTO mapping consumes the new shape (`form`, `styles` frozenset, `translations_level`) from the shared models.

What makes a good test for this domain model:

- Test **wire shape** (Pydantic validation), not provider internals.
- Test **Section filter semantics** (3-case on `styles`, 2-of-3 on search) at the model level — no live upstream needed.
- Test **translations_level auto-detection** by parsing captured fixtures that exhibit uniform vs varying voices.
- Test **default translation** behaviour by sending `translation=None` to a stub provider and asserting the first candidate is returned.
- Test **frozenset semantics** — `{anime, cartoon}` round-trips through JSON as an array, deserialises back into a frozenset; `∅` round-trips as `[]`.

## Out of Scope

Explicitly NOT part of these decisions (deferred to future sessions or rejected permanently):

- **Failure semantics** — `/api/search` 12s budget, partial failures, error envelope. Reserved for next grilling session.
- **Cache contract** — TTL, key shape, invalidation. Reserved for next grilling session.
- **Provider lifecycle** — registration, hot-reload, retirement. Reserved for next grilling session.
- **New `MediaStyle` values** (e.g. `"documentary"`). Adding a style is a Literal extension plus provider updates, not a schema overhaul — but is YAGNI today.
- **`styles_all` field on Section** (subset / all-of semantics). Reserved for when a real "дитяче аніме мультики" section appears in upstream data.
- **`is_adult` field** — explicitly rejected per ADR-0001.
- **Enriched `Translation`** (`language`, `kind`, `studio` fields) — explicitly rejected per Q16.
- **Multi-URL streams, quality labels, backend probes, TTL contract, fallback chains** — explicitly rejected per Q11–Q15.
- **Global `Translation.id` namespace** — explicitly rejected per Q18.
- **`default_translation_id` field, `is_primary` flag** — explicitly rejected per Q19–Q20.

## Further Notes

- **`CONTEXT.md` is the single source of truth** for domain terms. This spec is a synthesis; the glossary holds the canonical definitions and edge-case behaviour.
- **ADR-0001** documents the catalog-taxonomy decision specifically (the only ADR-worthy decision — the others are simpler status-quo confirmations).
- **The current shape is the floor, not the ceiling.** Each rejected alternative can be added as a non-breaking optional field (`streams: list[str] | None = None`, `quality: str | None = None`, `language: str | None = None`, `default_translation_id: str | None = None`, etc.) without touching existing clients.
- **No issue tracker publishing.** Project issue tracker is not configured in this repository; this document is filed in `docs/superpowers/specs/` for future reference and is the deliverable, not a tracker ticket. If a tracker is wired up later, this content can be sliced into per-decision tickets (one ticket per "rejected alternative that may become a future feature").
- **Spec reconciliation.** [`2026-08-01-ps4-uk-stream-design.md`](2026-08-01-ps4-uk-stream-design.md) §3.2 (`MediaType` enum) is **obsolete in spirit** post-ADR-0001; it remains the source of truth for architecture, data flow, and error envelope sections not affected by Model B.
- **Implementation status** — per `docs/status.md`, all 19 v2 providers landed. The client is Switchfin via the Jellyfin facade (spec #100); on-console verification is driven by `scripts/switchfin_test.py` (`docs/switchfin-test-report.md`).