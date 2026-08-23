"""LLM taste-profile layer (spec #290, ticket #292) — parser + client.

An OPTIONAL enrichment of the pure recommender: a structured taste
profile (per-genre weights, theme tags, up to two personalized row
ideas with Ukrainian copy) generated from the viewer's watch history
and search queries by ONE OpenAI-compatible chat-completions call. The
interface is OpenAI-compatible, so OpenAI, OpenRouter, Groq or a local
llama.cpp/ollama server all work. The profile VALUE TYPES live in
``models.py`` (the pure scorer reads them without this module's httpx/
config chain); this module owns the fetching/parsing/validation LOGIC.

The layer is strictly additive and defensive:

  - Without all three config knobs (base URL, key, model) the module is
    inert: ``active_profile()`` returns None and the client is never
    constructed.
  - The response is validated by a strict parser — ANY invalid field
    rejects the WHOLE profile (never a partial install), so a weird
    model answer leaves home unchanged (spec user story 8).
  - The client sends one request with a 30 s timeout and NO retries; a
    non-JSON answer, a network error, or a bad status all yield None.
  - Provider titles are untrusted DATA in the prompt (quoted), never
    instructions.

The profile lives in memory only (regenerable by design, spec #290 §No
persistence): until the first successful refresh the pure scorer runs
unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx

from .config import SETTINGS
from .models import RowIdea, TasteProfile

log = logging.getLogger(__name__)

#: One call, 30 s timeout, no retries (spec #290 §Client).
LLM_TIMEOUT_S = 30.0

#: v1 schema bounds.
PROFILE_VERSION = 1
WEIGHT_MIN = 0.2
WEIGHT_MAX = 2.0
MAX_ROW_IDEAS = 2


# The profile VALUE TYPES (RowIdea / TasteProfile) live in models.py —
# the pure scorer consumes them without importing this module's httpx/
# config chain. Re-exported here so existing call sites
# (``from cs_uk_api.llm import TasteProfile``) keep working.


#: The module-level active profile — installed by the refresh function
#: (catalog_state wiring, ticket #294), read by the scorer path. A
#: module-level mutable is the same pattern as the provider registry;
#: tests replace it directly.
_active: TasteProfile | None = None


def active_profile() -> TasteProfile | None:
    """The currently installed taste profile, or None (layer inert)."""
    return _active


def set_active_profile(profile: TasteProfile | None) -> None:
    """Install/replace/clear the active profile (wiring seam)."""
    global _active
    _active = profile


def llm_enabled() -> bool:
    """True when all three knobs are configured (the layer activates)."""
    return bool(SETTINGS.llm_base_url and SETTINGS.llm_key and SETTINGS.llm_model)


# ---------------------------------------------------------------- parser


class ProfileError(ValueError):
    """A rejected profile — the WHOLE profile is invalid (never partial)."""


def parse_profile(raw: Any) -> TasteProfile:
    """Strictly validate a parsed JSON object into a TasteProfile.

    Rejects: a non-dict, a wrong version, a genre weight outside the
    calibrated band (0.2–2.0), a non-string theme tag, more than two row
    ideas, an idea with non-string title/genres or a non-positive max.
    ANY invalid field rejects the whole profile (spec §Profile schema).
    """
    if not isinstance(raw, dict):
        raise ProfileError("profile must be a JSON object")
    if raw.get("v") != PROFILE_VERSION:
        raise ProfileError(f"unsupported version: {raw.get('v')!r}")

    weights_raw = raw.get("genre_weights", {})
    if not isinstance(weights_raw, dict):
        raise ProfileError("genre_weights must be an object")
    weights: dict[str, float] = {}
    for genre, w in weights_raw.items():
        if not isinstance(genre, str) or not genre.strip():
            raise ProfileError(f"invalid genre key: {genre!r}")
        if not isinstance(w, (int, float)) or isinstance(w, bool):
            raise ProfileError(f"invalid weight for {genre!r}: {w!r}")
        if not (WEIGHT_MIN <= w <= WEIGHT_MAX):
            raise ProfileError(
                f"weight for {genre!r} out of band [{WEIGHT_MIN}, {WEIGHT_MAX}]"
            )
        weights[genre.strip()] = float(w)

    tags_raw = raw.get("theme_tags", [])
    if not isinstance(tags_raw, list):
        raise ProfileError("theme_tags must be a list")
    tags = tuple(t.strip() for t in tags_raw if isinstance(t, str) and t.strip())

    ideas_raw = raw.get("row_ideas", [])
    if not isinstance(ideas_raw, list):
        raise ProfileError("row_ideas must be a list")
    if len(ideas_raw) > MAX_ROW_IDEAS:
        ideas_raw = ideas_raw[:MAX_ROW_IDEAS]  # truncate, never reject count
    ideas: list[RowIdea] = []
    for idea in ideas_raw:
        if not isinstance(idea, dict):
            raise ProfileError("row idea must be an object")
        title = idea.get("title")
        genres = idea.get("genres")
        max_ = idea.get("max")
        if not isinstance(title, str) or not title.strip():
            raise ProfileError(f"invalid row idea title: {title!r}")
        if not isinstance(genres, list) or not all(
            isinstance(g, str) and g.strip() for g in genres
        ):
            raise ProfileError(f"invalid row idea genres: {genres!r}")
        if not isinstance(max_, int) or max_ <= 0:
            raise ProfileError(f"invalid row idea max: {max_!r}")
        ideas.append(
            RowIdea(
                title=title.strip(),
                genres=tuple(g.strip().lower() for g in genres),
                max=max_,
            )
        )
    return TasteProfile(genre_weights=weights, theme_tags=tags, row_ideas=tuple(ideas))


def _extract_json(text: str) -> Any:
    """The JSON object from an answer that may wrap it in fenced text."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # ```json ... ``` fences (a common model habit).
        first = stripped.find("\n")
        last = stripped.rfind("```")
        stripped = stripped[first + 1 : last] if first != -1 and last != -1 else stripped
        stripped = stripped.strip().strip("`").strip()
    return json.loads(stripped)


# ---------------------------------------------------------------- client


class _LlmClient(Protocol):
    async def chat(
        self, messages: list[dict[str, str]], *, timeout: float = LLM_TIMEOUT_S
    ) -> str: ...


class HttpxLlmClient:
    """OpenAI-compatible chat-completions client (one call, no retries)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._http = http

    async def chat(
        self, messages: list[dict[str, str]], *, timeout: float = LLM_TIMEOUT_S
    ) -> str:
        """One chat-completions call; returns the assistant's text.

        Raises on network errors and non-2xx statuses — the caller
        (refresh) catches and turns any raise into a None profile.
        """
        http = self._http if self._http is not None else httpx.AsyncClient(timeout=timeout)
        own = self._http is None
        try:
            resp = await http.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": messages,
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return str(content)
        finally:
            if own:
                await http.aclose()


def _system_prompt(genres: list[str]) -> str:
    """The JSON-only system prompt (spec §Client: single request)."""
    vocab = ", ".join(genres) if genres else "(none provided)"
    return (
        "You build a viewer taste profile from watch history and search "
        "queries for a Ukrainian streaming catalog. Respond with JSON "
        "ONLY, no prose, no markdown fences. The JSON object must be:\n"
        '{"v": 1, "genre_weights": {"<genre label>": <0.2-2.0>}, '
        '"theme_tags": ["..."], "row_ideas": '
        '[{"title": "<Ukrainian row title>", "genres": ["<genre label>"], '
        '"max": <positive int>}]}\n'
        "Rules: genre labels must come ONLY from the provided vocabulary; "
        "genre_weights map favorite genres to >=1 and avoided genres to "
        "<1; row_ideas have at most 2 entries and their genres must "
        "intersect the vocabulary; row titles are short Ukrainian "
        'phrases like "Похмурі драми для тебе".\n'
        f"Catalog genre vocabulary: {vocab}"
    )


async def fetch_profile(
    *,
    history: list[dict[str, Any]],
    queries: list[str],
    genres: list[str],
    client: _LlmClient | None = None,
) -> TasteProfile | None:
    """One refresh call: collect signals → LLM → validated profile.

    Returns the validated profile on success, None on ANY failure
    (missing knobs, network error, non-JSON answer, invalid profile) —
    the layer is invisible until it works (spec user stories 7-8).
    """
    if not llm_enabled():
        return None
    client = client or HttpxLlmClient(
        base_url=SETTINGS.llm_base_url or "",
        api_key=SETTINGS.llm_key or "",
        model=SETTINGS.llm_model or "",
    )
    # Provider titles are untrusted DATA — quoted as a JSON array, never
    # embedded raw as instructions.
    user = json.dumps(
        {
            "watch_history": history[:10],
            "recent_queries": queries[:10],
        },
        ensure_ascii=False,
    )
    try:
        text = await client.chat(
            [
                {"role": "system", "content": _system_prompt(genres)},
                {"role": "user", "content": user},
            ]
        )
        return parse_profile(_extract_json(text))
    except Exception as e:  # noqa: BLE001 — the layer degrades to inert
        log.warning("llm profile refresh failed: %s", e)
        return None
