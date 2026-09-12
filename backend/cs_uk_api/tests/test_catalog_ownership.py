"""ADR-0010 pins: the catalog snapshot has ONE apply step.

Issue #420: a group key returned by search answered ``/Items/{id}`` with
``404 item_unavailable`` seconds after it had resolved. The resolution map,
the group index, the snapshot and the deep-row pools were four pieces of
derived state written from ten sites across three modules, and two of those
sites replaced the map + index WHOLE — so a live search registration was
destroyed with no trace. Measured on the deployment: 16 keys registered, the
next replacement dropped exactly 16, the read 404-ed in 0 ms.

ADR-0010 makes "what survives a replacement" a property of one apply step,
so the guarantee is pinned here as unit tests instead of a race reproduced
against a live host.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from cs_uk_api import _catalog_state as catalog_state
from cs_uk_api._catalog_state._stores import CatalogState, install_catalog_state
from cs_uk_api._catalog_state.snapshot import _cache_home
from cs_uk_api.merge import item_group_key
from cs_uk_api.models import SearchGroup, SearchResult

_STORES_LOGGER = "cs_uk_api.catalog_state.stores"


def _item(pid: str, title: str, *, year: int | None = None, n: str = "1") -> SearchResult:
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form="movie",
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
    )


def _group(*sources: SearchResult) -> SearchGroup:
    """A merged search group over the given provider sources."""
    first = sources[0]
    keys = [item_group_key(s) for s in sources]
    return SearchGroup(
        group_key=keys[0],
        title=first.title,
        year=first.year,
        form=first.form,
        sources=list(sources),
        member_keys=keys,
    )


def _clock() -> tuple[dict[str, float], Callable[[], float]]:
    """A controllable clock (no auto-advance: the test moves time)."""
    state = {"t": 1000.0}

    def now() -> float:
        return state["t"]

    return state, now


# ---------------------------------------------------------------------------
# Carry-forward: the searched key outlives a replacement
# ---------------------------------------------------------------------------


def test_rebuild_carries_a_live_search_registration_forward() -> None:
    """A rebuild must not drop a key a client just searched (ADR-0010).

    A search registers a key the snapshot does NOT contain — most results
    are not home-row cards. Before ADR-0010 the rebuild replaced the map
    and index whole; now the registration rides the apply step into the
    new map and index.
    """
    hit = _item("p1", "Пошуковий Фільм", year=2024)
    group = _group(hit)
    key = group.group_key
    catalog_state.register_search_groups([group])

    # A rebuild lands whose snapshot carries a DIFFERENT title.
    other = _item("p2", "Інший Фільм", year=2020)
    _cache_home({"p2": [other]}, {}, {})

    # The snapshot's own row is indexed...
    assert catalog_state.get_group_entry(item_group_key(other)) is not None
    # ...and the searched key is still resolvable and still indexed, with
    # its provider union intact.
    resolved = catalog_state.resolve_group(key)
    assert resolved is not None
    assert set(resolved) == {"p1"}
    carried = catalog_state.get_group_entry(key)
    assert carried is not None
    # No home card exists for it — the promise is resolvability, not a row.
    assert carried.home_item is None


def test_carried_registration_lapses_on_the_search_ttl(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registrations expire on the SEARCH TTL, not the snapshot's cycle.

    The promise is "your results stay actionable for as long as the
    results themselves are cached" — so two rebuilds inside the TTL must
    not touch the key, and one past the TTL must retire it.
    """
    state, now = _clock()
    install_catalog_state(CatalogState(search_ttl_s=300, now=now))
    key = _group(_item("p1", "Пошуковий Фільм")).group_key
    catalog_state.register_search_groups([_group(_item("p1", "Пошуковий Фільм"))])

    for title in ("Раз", "Два"):
        _cache_home({"p2": [_item("p2", title)]}, {}, {})
    assert catalog_state.resolve_group(key) is not None

    state["t"] += 301
    with caplog.at_level(logging.INFO, logger=_STORES_LOGGER):
        _cache_home({"p2": [_item("p2", "Три")]}, {}, {})
    assert catalog_state.resolve_group(key) is None
    assert catalog_state.get_group_entry(key) is None
    assert "retired" in caplog.text


def test_a_carried_registration_is_not_reported_as_retired(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The #420 instrument stays silent while a key is still wanted.

    The negative half of the pin above: a rebuild that carries a live
    registration must log NO retirement. Before ADR-0010 this is the line
    that fired on every replacement — which is what made #420 measurable.
    """
    key = _group(_item("p1", "Пошуковий Фільм")).group_key
    catalog_state.register_search_groups([_group(_item("p1", "Пошуковий Фільм"))])

    with caplog.at_level(logging.INFO, logger=_STORES_LOGGER):
        _cache_home({"p2": [_item("p2", "Інший")]}, {}, {})

    assert catalog_state.resolve_group(key) is not None
    assert "retired" not in caplog.text


def test_re_registering_a_key_extends_its_ttl() -> None:
    """A second search extends a registration — never shortens it."""
    state, now = _clock()
    install_catalog_state(CatalogState(search_ttl_s=300, now=now))
    group = _group(_item("p1", "Пошуковий Фільм"))
    key = group.group_key
    catalog_state.register_search_groups([group])

    state["t"] += 250
    catalog_state.register_search_groups([group])  # fresh 300s from here
    state["t"] += 250  # 500s after the first, 250s after the second

    _cache_home({"p2": [_item("p2", "Інший")]}, {}, {})

    assert catalog_state.resolve_group(key) is not None


def test_a_snapshot_row_is_not_demoted_by_a_carried_registration() -> None:
    """Snapshot content wins: a carried key keeps its home card + row kind.

    The carry-forward merges, it does not overwrite — otherwise a rebuild
    could downgrade a real home card to a card-less placeholder.
    """
    item = _item("p1", "Дюна", year=2021)
    key = item_group_key(item)
    _cache_home({"p1": [item]}, {}, {})
    catalog_state.register_search_groups([_group(item)])

    _cache_home({"p1": [item]}, {}, {})

    entry = catalog_state.get_group_entry(key)
    assert entry is not None
    assert entry.home_item is not None
    assert entry.row_type is not None


# ---------------------------------------------------------------------------
# The sanctioned invalidation
# ---------------------------------------------------------------------------


def test_invalidate_clears_the_snapshot_but_keeps_live_registrations() -> None:
    """The warm path's clear is owned, and it is not a search wipe.

    ADR-0010 sanctions the invalidation (the rows are built FROM the
    profiles, so a stale snapshot is a correctness bug) but routes it
    through the owner — and the owner keeps the client's promise.
    """
    hit = _item("p1", "Пошуковий Фільм")
    key = _group(hit).group_key
    catalog_state.register_search_groups([_group(hit)])
    _cache_home({"p2": [_item("p2", "Снапшот")]}, {}, {})
    pool = catalog_state.row_deep_cache
    pool.set("row-deep:movie", [_item("p2", "Старий", n="9")])
    assert catalog_state.get_home() is not None

    catalog_state.catalog_state().invalidate(reason="test")

    assert catalog_state.get_home() is None
    assert pool.get("row-deep:movie") is None
    assert catalog_state.resolve_group(key) is not None
