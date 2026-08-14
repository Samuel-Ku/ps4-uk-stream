"""Tests for the episode-fallback extraction (ticket #142).

The gate.sh series-only fallback used to be pinned by STATIC string
matches against the script source (test_gate_script.py) — a regression
in the episode-id extraction or prefixing rule passed the grep. Ticket
#142 moves the decision into ``gate_tools.fallback_episode_cid`` so the
behaviour is pinned by EXECUTION against representative content JSON:

- seasons present / absent
- empty episode list
- an episode id that already carries the ``<provider>:`` prefix
- one that does not (must be prefixed by the caller's provider)

These tests execute the logic; they go red if the JSON path or the
prefixing rule regresses.
"""
from __future__ import annotations

from cs_uk_api.gate_tools import fallback_episode_cid

# Representative content() payloads (the ``ContentResponse`` wire shape).

CONTENT_WITH_SEASONS = {
    "title": "Серіал",
    "seasons": [
        {
            "number": 1,
            "episodes": [
                {"number": 1, "id": "simpsonsuatv:https://simpsonsua.tv/…/e1.html"},
                {"number": 2, "id": "simpsonsuatv:https://simpsonsua.tv/…/e2.html"},
            ],
        }
    ],
}

CONTENT_WITH_UNPREFIXED_EPISODE = {
    "title": "Дорама",
    "seasons": [
        {
            "number": 1,
            "episodes": [
                {"number": 1, "id": "dorama-408-…:s1e1"},
            ],
        }
    ],
}

CONTENT_WITHOUT_SEASONS = {"title": "Фільм"}

CONTENT_EMPTY_EPISODES = {"title": "Серіал", "seasons": [{"number": 1, "episodes": []}]}

CONTENT_EMPTY_SEASONS = {"title": "Серіал", "seasons": []}


def test_seasons_present_picks_first_episode() -> None:
    # Already prefixed: unchanged (simpsonsuatv full-page URL case).
    cid = fallback_episode_cid(CONTENT_WITH_SEASONS, "simpsonsuatv")
    assert cid == "simpsonsuatv:https://simpsonsua.tv/…/e1.html"


def test_unprefixed_episode_gets_provider_prefix() -> None:
    # The bare episode id must be prefixed with the caller's provider
    # (the wire id /api/stream accepts is ``<provider>:<external>``).
    cid = fallback_episode_cid(CONTENT_WITH_UNPREFIXED_EPISODE, "ufdub")
    assert cid == "ufdub:dorama-408-…:s1e1"


def test_no_seasons_returns_empty() -> None:
    assert fallback_episode_cid(CONTENT_WITHOUT_SEASONS, "kinotron") == ""


def test_empty_episode_list_returns_empty() -> None:
    assert fallback_episode_cid(CONTENT_EMPTY_EPISODES, "serialno") == ""


def test_empty_seasons_returns_empty() -> None:
    assert fallback_episode_cid(CONTENT_EMPTY_SEASONS, "serialno") == ""


def test_different_provider_still_prefixes() -> None:
    # Same content, different caller provider: the prefix must follow the
    # CALLER, not be baked into the content.
    cid = fallback_episode_cid(CONTENT_WITH_UNPREFIXED_EPISODE, "anitubeinua")
    assert cid == "anitubeinua:dorama-408-…:s1e1"
