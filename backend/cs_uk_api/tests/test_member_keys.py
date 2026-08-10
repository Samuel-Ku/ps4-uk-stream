"""Card↔record groupKey matching via member keys (issue #89).

Latent mismatch from #69 spec AC3: client resume/memory records are
keyed by the *played item's* group key, but a merged card is keyed by
the group's *min* member key (yearful-preferred-min). For a non-min
member (e.g. a yearless kinolub listing inside a pool with a yearful
uakino listing), the record key != card key and the «Продовжити
перегляд» row would miss the entry.

Resolution: every merged payload (HomeItem, SearchGroup) carries the
full set of member group keys. The client matches an entry against
ANY member key, not only the canonical ``group_key``.

Wire invariants under test:

  - HomeItem.member_keys is non-empty and includes the canonical
    ``group_key`` (the yearful-preferred-min).
  - SearchGroup.member_keys is non-empty and includes ``group_key``.
  - For a yearful + yearless pair, BOTH the yearful key and the
    yearless key are present in member_keys (the canonical key is
    the yearful min, but the yearless key is also reachable).
  - Member keys are deduped — duplicate listings from the same
    provider don't double-count.

Why a separate test file: the merge core's per-item group keys
collision logic is orthogonal to its grouping output. We exercise the
client-facing wire shape here without dragging in the cross-provider
search fan-out machinery.
"""
from __future__ import annotations

from typing import Any, cast

import pytest

from cs_uk_api.home import round_robin_dedup
from cs_uk_api.merge import group_key_from, item_group_key
from cs_uk_api.models import SearchResult
from cs_uk_api.providers.base import model_b_axes


def _make_item(
    pid: str,
    title: str,
    year: int | None = None,
    n: str = "1",
) -> SearchResult:
    mb_form, mb_styles = model_b_axes(cast(Any, "movie"))
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form=mb_form,
        styles=mb_styles,
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
    )


# ---------------------------------------------------------------------------
# HomeItem.member_keys
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_home_item_member_keys_includes_canonical_and_min() -> None:
    """For a single-provider single-listing, member_keys is the per-item
    group key and equals the canonical ``group_key``."""
    by_provider = {"p1": [_make_item("p1", "Дюна", year=2021, n="p1-1")]}
    out = round_robin_dedup(by_provider, limit=20)
    assert len(out) == 1
    assert out[0].member_keys == [out[0].group_key]
    assert out[0].member_keys == [item_group_key(_make_item("p1", "Дюна", year=2021))]


@pytest.mark.unit
def test_home_item_member_keys_for_yearful_yearless_pair() -> None:
    """Year-soft pair (yearful + yearless) — the canonical ``group_key``
    is the yearful-preferred-min (the yearful key, because the
    yearless key has a longer hash digest and the yearful preference
    beats lex-min on tie). Both keys are present in member_keys so the
    client can match a record keyed by either. This is the spec's
    «Тато» / «Daddy» case."""
    yearful = _make_item("p1", "Дюна", year=2021, n="p1-1")
    yearless = _make_item("p2", "Дюна", year=None, n="p2-1")
    by_provider = {"p1": [yearful], "p2": [yearless]}
    out = round_robin_dedup(by_provider, limit=20)
    assert len(out) == 1
    expected_yearful_key = item_group_key(yearful)
    expected_yearless_key = item_group_key(yearless)
    # The canonical key is the yearful-preferred-min.
    assert out[0].group_key == expected_yearful_key
    # Both member keys are present (set equality, order may vary).
    assert set(out[0].member_keys) == {expected_yearful_key, expected_yearless_key}


@pytest.mark.unit
def test_home_item_member_keys_dedup_multiple_listings_same_provider() -> None:
    """When a provider surfaces multiple listings for the same group
    (same title, different ids), the member_keys list dedupes — the
    client only needs one key per item identity."""
    a1 = _make_item("p1", "Дюна", year=2021, n="p1-1")
    a2 = _make_item("p1", "Дюна", year=2021, n="p1-2")
    by_provider = {"p1": [a1, a2]}
    out = round_robin_dedup(by_provider, limit=20)
    assert len(out) == 1
    # Per-item group key for the same title+type+year is identical
    # regardless of the upstream id (verified by item_group_key directly),
    # so the merge core collapses the two listings into one row.
    expected_key = item_group_key(a1)
    assert item_group_key(a1) == item_group_key(a2)
    # The merged row's member_keys carries the key once.
    assert out[0].member_keys == [expected_key]


@pytest.mark.unit
def test_home_item_member_keys_field_present_in_wire_shape() -> None:
    """Defensive: the field is part of the wire shape (Pydantic
    ``model_dump``). A client that parses the JSON will see the
    ``member_keys`` key on every HomeItem."""
    by_provider = {"p1": [_make_item("p1", "Дюна", year=2021, n="p1-1")]}
    out = round_robin_dedup(by_provider, limit=20)
    dumped = out[0].model_dump()
    assert "member_keys" in dumped
    assert isinstance(dumped["member_keys"], list)


# ---------------------------------------------------------------------------
# Property: item_group_key mirrors group_key_from
# ---------------------------------------------------------------------------
# This is the contract SearchGroup.member_keys relies on: the
# per-item key computed by ``item_group_key(item)`` must equal the
# ``group_key_from(item.title, item.form, item.year, item.id)`` the
# single-source content route uses. Already verified for HomeItem in
# test_home.py above; this is a one-liner guardrail for SearchGroup.


@pytest.mark.unit
def test_member_key_matches_group_key_from_for_item() -> None:
    """The per-item key the merge core computes (``item_group_key``)
    equals the key the single-source content route computes
    (``group_key_from`` for the same item's own fields). A client
    that records under the played item's key can match it against
    any member key of the merged card without translation."""
    item = _make_item("p1", "Дюна", year=2021, n="p1-1")
    assert item_group_key(item) == group_key_from(item.title, item.form, item.year, item.id)
