"""Tests for the provider-probing module (spec #323, Probe T1 #327).

The three probe facts are pinned: entry-point selection (newest ->
section -> search), the canonical wire-id split, and verdict
normalization (gated is NOT a failure, ADR-0002 in one place).
"""

from __future__ import annotations

from cs_uk_api.models import Section
from cs_uk_api.probe import (
    VERDICT_ERROR,
    VERDICT_FAIL,
    VERDICT_GATED,
    VERDICT_NO_EPISODES,
    VERDICT_OK,
    VERDICT_UNAVAILABLE,
    EntryPoint,
    attributed_provider,
    is_episodic_item,
    is_probe_failure,
    probe_error_verdict,
    select_entry_points,
    split_wire_id,
)
from cs_uk_api.providers.base import BaseProvider, ProviderError


class _Stub(BaseProvider):
    """Minimal provider; entry selection only reads static attributes."""

    id = "stub"
    name = "Stub"
    types = ("series",)

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _section(sid: str) -> Section:
    return Section(id=sid, title=sid)


# ----------------------------------------------------------------------
# entry-point selection — newest -> section -> search
# ----------------------------------------------------------------------


def test_entries_newest_then_sections_then_search() -> None:
    p = _Stub()
    p.newest_section = "page"
    p.sections = (_section("anime"), _section("films"))
    assert select_entry_points(p) == (
        EntryPoint(kind="newest", section="page"),
        EntryPoint(kind="section", section="anime"),
        EntryPoint(kind="section", section="films"),
        EntryPoint(kind="search"),
    )


def test_entries_without_newest_start_at_sections() -> None:
    p = _Stub()
    p.sections = (_section("anime"),)
    assert select_entry_points(p) == (
        EntryPoint(kind="section", section="anime"),
        EntryPoint(kind="search"),
    )


def test_entries_without_sections_is_search_only() -> None:
    # The base contract guarantees search(), so a probe always has a
    # last-resort entry.
    assert select_entry_points(_Stub()) == (EntryPoint(kind="search"),)


# ----------------------------------------------------------------------
# wire-id split — THE canonical copy
# ----------------------------------------------------------------------


def test_split_wire_id_plain() -> None:
    assert split_wire_id("uakino:6268") == ("uakino", "6268")


def test_split_wire_id_keeps_colons_in_external() -> None:
    # Episode wire ids carry the season/episode grammar after the
    # provider prefix — the split must not cut them.
    assert split_wire_id("ufdub:dorama-408-123:s1e1") == ("ufdub", "dorama-408-123:s1e1")
    assert split_wire_id("uakino:6268:e1") == ("uakino", "6268:e1")


def test_split_wire_id_without_colon_yields_empty_external() -> None:
    assert split_wire_id("nocolon") == ("nocolon", "")


# ----------------------------------------------------------------------
# row-type + attribution — the sweep's home-item derivations (Probe T2)
# ----------------------------------------------------------------------


def test_is_episodic_item_form_series() -> None:
    assert is_episodic_item({"form": "series", "providers": ["uakino"]}) is True


def test_is_episodic_item_movie_is_not() -> None:
    # A movie is a dead end for the episode rail (D3); style tags never
    # change the form.
    assert is_episodic_item({"form": "movie", "providers": ["uakino"]}) is False
    assert is_episodic_item({"form": "movie", "type": "anime"}) is False


def test_attributed_provider_takes_first_seen() -> None:
    item = {"form": "series", "providers": ["uakino", "animeon"]}
    assert attributed_provider(item) == "uakino"


def test_attributed_provider_none_without_providers() -> None:
    assert attributed_provider({"form": "series"}) is None
    assert attributed_provider({"form": "series", "providers": []}) is None


# ----------------------------------------------------------------------
# verdict vocabulary — the sweep reports in the probe module's words
# ----------------------------------------------------------------------


def test_sweep_verdicts_are_the_probe_vocabulary() -> None:
    """Probe T2: the episode-rail sweep's verdict constants ARE the probe
    module's vocabulary (no private copy in the sweep)."""
    from cs_uk_api import sweep_episode_rail

    assert sweep_episode_rail.OK == VERDICT_OK
    assert sweep_episode_rail.FAIL == VERDICT_FAIL
    assert sweep_episode_rail.NO_EPISODES == VERDICT_NO_EPISODES


def test_no_episodes_verdict_is_not_a_failure() -> None:
    """A resolved-but-empty rail is a warning (⚠️), not a provider break."""
    assert is_probe_failure(VERDICT_NO_EPISODES) is False
    assert is_probe_failure(VERDICT_FAIL) is True


# ----------------------------------------------------------------------
# verdict normalization — gated is not a failure (ADR-0002)
# ----------------------------------------------------------------------


def test_gated_provider_error_is_not_a_failure() -> None:
    verdict = probe_error_verdict(ProviderError("gated", "Для підписників"))
    assert verdict == VERDICT_GATED
    assert is_probe_failure(verdict) is False


def test_other_provider_error_is_a_failure() -> None:
    verdict = probe_error_verdict(ProviderError("not_found", "nope"))
    assert verdict == VERDICT_UNAVAILABLE
    assert is_probe_failure(verdict) is True


def test_transport_crash_is_a_failure() -> None:
    verdict = probe_error_verdict(RuntimeError("connection reset"))
    assert verdict == VERDICT_ERROR
    assert is_probe_failure(verdict) is True


def test_unknown_verdict_fails_closed() -> None:
    assert is_probe_failure("weird-verdict") is True
