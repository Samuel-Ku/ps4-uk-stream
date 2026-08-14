# LLM taste-profile layer (phase D) — design

## Purpose

An OPTIONAL LLM layer that enriches the content-based recommendation
scorer with a generated taste profile: per-genre weights, theme tags
and up to two personalized row ideas with Ukrainian copy. The layer is
strictly additive — without an API key, or on any LLM failure, the
existing pure scorer (spec #252) runs unchanged.

## Role boundary (decided)

- The LLM **generates a structured profile** from the viewer's signals.
- The LLM **never ranks candidates per home cycle** (cost/latency) and
  **never curates titles directly** (hallucination risk). Candidate
  selection always happens through the existing scorer against the
  home-snapshot pool.

## Interface

OpenAI-compatible chat-completions endpoint, configured by three env
knobs: `CS_UK_LLM_BASE_URL`, `CS_UK_LLM_KEY`, `CS_UK_LLM_MODEL`. No key
→ the layer is disabled. Works with OpenAI, OpenRouter, Groq, or a
local llama.cpp/ollama server — no vendor code.

## Data flow

1. Signals are collected: the most recent playback-history items
   (title, genres, year, form; bounded to the top N), the persisted
   search queries, and the catalog's genre vocabulary.
2. One prompt is sent (single request, timeout, no retries).
3. The response must be strict JSON; it is validated with a Pydantic
   model. Any invalid field rejects the WHOLE profile.
4. The validated profile is kept in memory; the scorer reads it.
5. On failure: a warning is logged and the pure scorer remains active.

## Profile schema (prototype)

```json
{
  "v": 1,
  "genre_weights": {"драма": 1.4, "комедія": 0.3},
  "theme_tags": ["похмуре", "детектив"],
  "row_ideas": [
    {"title": "Похмурі драми для тебе", "genres": ["драма"], "max": 20}
  ],
  "generated_at": "2026-08-14T18:00:00Z"
}
```

- `genre_weights`: multipliers applied to the genre term of
  `similarity()` (default 1.0; clamped to a sane band).
- `theme_tags`: token boosts, reusing the `query_boost` mechanism.
- `row_ideas`: at most 2; `genres` MUST intersect the catalog's known
  genre vocabulary — an unknown genre discards the idea; the row is
  populated by the same snapshot-pool filtering the genre rails use.
  Titles are display-only strings (bounded length).

## Cadence and persistence

- Background generation once per day plus an on-demand admin trigger.
- No persistence: the profile is regenerated on demand and after
  restarts; until the first successful run the pure scorer is active.

## Safety

- Provider titles are untrusted DATA in the prompt, never instructions
  (prompt injection surface).
- Output is validated strictly; nothing the LLM says can crash or
  corrupt the recommendation flow — worst case is a fallback.
- The request carries only catalog signals and history — no secrets
  beyond the user's own API key; the deployment is LAN-only.

## Error handling

- LLM unreachable / timeout / non-JSON / schema violation → log,
  fallback to the pure scorer, retry on the next cycle.
- A failed generation never delays or blocks the home build.

## Testing

- respx-mocked LLM endpoint (no real API in tests):
  - a valid profile applies the genre weights and theme boosts;
  - invalid JSON, unknown genres, or a network error fall back to the
    pure scorer;
  - row ideas are bounded (≤2) and genre-validated;
  - the layer is inert without the env key.
- Prior art: the respx provider tests and the recommend-module unit
  tests (spec #252).

## Out of scope

- Conversational search (needs client UI Switchfin doesn't have).
- Per-cycle LLM ranking of candidates.
- LLM-curated titles without scorer verification.
- Profile persistence across restarts (regenerable by design).
