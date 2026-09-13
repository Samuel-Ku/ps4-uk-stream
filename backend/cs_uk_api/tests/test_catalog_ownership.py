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


def _group_at(key: str, *sources: SearchResult) -> SearchGroup:
    """A search group pinned to an explicit key (the merge's own decision).

    ``_group`` derives the key from its first source; this pins it, so a
    test can hand the owner a search whose sources are ONE provider while
    the snapshot carries the same key under a DIFFERENT one — the shape
    the registration narrowing is about.
    """
    first = sources[0]
    return SearchGroup(
        group_key=key,
        title=first.title,
        year=first.year,
        form=first.form,
        sources=list(sources),
        member_keys=[key],
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


def test_a_carried_registration_never_resurrects_a_dropped_snapshot_provider() -> None:
    """The registration owns the SEARCH's providers, not the map's whole entry.

    A snapshot row supplies p1 for a key and a search returns only p2 for
    it. The registration must carry p2 alone: p1 belongs to the snapshot,
    and a replacement that drops it has to actually drop it. Recording the
    whole map entry made the search layer a storer of snapshot state — the
    dropped provider came back as a trailing chip for the rest of the TTL,
    and every later search of the key re-recorded it, so it never had to
    age out. This is audit finding 4 of the 2026-09-12 review.
    """
    snap = _item("p1", "Снапшот", year=2019)
    key = item_group_key(snap)
    _cache_home({"p1": [snap]}, {}, {})
    assert catalog_state.resolve_group(key) is not None

    catalog_state.register_search_groups([_group_at(key, _item("p2", "Пошук", year=2019))])
    # Both layers are visible while the snapshot that carried p1 is current.
    assert set(catalog_state.resolve_group(key) or {}) == {"p1", "p2"}

    # A replacement lands whose snapshot does not carry the key at all.
    _cache_home({"p9": [_item("p9", "Інший", year=2020)]}, {}, {})

    # Only the SEARCH's provider rides the registration forward.
    assert set(catalog_state.resolve_group(key) or {}) == {"p2"}


def test_a_carried_registration_serves_the_searchs_own_card() -> None:
    """Whose card for a provider BOTH layers carry? The search's, once the snapshot is gone.

    The map keeps the snapshot's card for a provider both layers carry —
    the snapshot wins while it exists. The registration is the SEARCH
    layer's state, though, so it must not carry that same card past the
    snapshot that chose it: after a replacement drops the key, the card
    that answers is the one the search returned, which is the result the
    client actually saw and clicked. Otherwise a snapshot listing outlives
    its own snapshot because a search happened to name the same provider.
    """
    snap = _item("p1", "Дюна", year=2021, n="snap")
    key = item_group_key(snap)
    _cache_home({"p1": [snap]}, {}, {})
    search_card = _item("p1", "Дюна", year=2021, n="search")
    catalog_state.register_search_groups([_group_at(key, search_card)])

    # Snapshot precedence holds while the snapshot does.
    live = catalog_state.group_resolution(key)
    assert live is not None
    assert live.source("p1") is not None
    assert live.source("p1").id == snap.id

    # A replacement lands that does not carry the key at all.
    _cache_home({"p9": [_item("p9", "Інший", year=2020)]}, {}, {})

    after = catalog_state.group_resolution(key)
    assert after is not None
    assert set(after.providers) == {"p1"}
    assert after.source("p1") is not None
    assert after.source("p1").id == search_card.id


def test_a_carried_registration_keeps_an_earlier_searchs_providers() -> None:
    """Narrowing must exclude the snapshot's providers, not an earlier search's.

    Two searches hit the same key with different providers, inside the same
    TTL (the second extends it), so BOTH promises are live and both
    providers are search-owned. The registration carries both — it drops
    only what the snapshot contributed, which is the fix's whole point and
    the half a naive "record this search's union" implementation would
    break (a later search would silently retire the earlier one's result).
    """
    film = _item("p1", "Фільм", year=2024)
    key = item_group_key(film)
    catalog_state.register_search_groups([_group_at(key, film)])
    catalog_state.register_search_groups([_group_at(key, _item("p2", "Фільм", year=2024))])

    _cache_home({"p9": [_item("p9", "Інший", year=2020)]}, {}, {})

    assert set(catalog_state.resolve_group(key) or {}) == {"p1", "p2"}


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


# ---------------------------------------------------------------------------
# The suite's seed / reset seam
# ---------------------------------------------------------------------------


def test_seed_group_sources_installs_the_map_and_nothing_else() -> None:
    """The seed writes the resolution map, not a snapshot row.

    A test that seeds group sources gets map-side resolution only: no
    card, no row kind, no index entry — the shape a bare write to the
    map's cache used to produce, now spelled once and named for what it
    is.
    """
    hit = _item("p1", "Дюна", year=2021)
    key = item_group_key(hit)
    catalog_state.seed_group_sources({key: {"p1": hit}})

    assert catalog_state.resolve_group(key) is not None
    assert catalog_state.get_group_entry(key) is None
    answer = catalog_state.group_resolution(key)
    assert answer is not None
    assert answer.origin is catalog_state.GroupOrigin.REGISTERED
    assert answer.card is None


def test_a_map_only_clear_strands_the_index() -> None:
    """Why the reset seam moves three pieces: clearing one strands the rest.

    This is the shape the suite used to create by hand (clear the map's
    cache, leave the index alone) and the shape issue #420 was made of —
    resolution answered from a map the index no longer described. Pinned
    so the seam's contract has its reason attached, and so the reset test
    below is read as "all three", not "the map".
    """
    snap_hit = _item("p2", "Снапшот", year=2019)
    snap_key = item_group_key(snap_hit)
    _cache_home({"p2": [snap_hit]}, {}, {})
    assert catalog_state.get_group_entry(snap_key) is not None

    # The map-only clear (the old idiom), performed through the seed seam:
    catalog_state.seed_group_sources({})

    assert catalog_state.resolve_group(snap_key) is None  # the map: emptied
    assert catalog_state.get_group_entry(snap_key) is not None  # the index: stranded


def test_reset_drops_the_map_the_index_and_the_registrations_together() -> None:
    """One call clears all three pieces of the owned state (ADR-0010).

    The pre-ADR-0010 suite emptied the map by hand, which left the index
    still describing snapshot rows the map no longer held — the
    divergence this ownership exists to remove. So the reset seam moves
    all three: a caller that clears one and not the others can no longer
    reproduce the half-state.
    """
    snap_hit = _item("p2", "Снапшот", year=2019)
    snap_key = item_group_key(snap_hit)
    _cache_home({"p2": [snap_hit]}, {}, {})
    searched = _group(_item("p1", "Пошуковий", year=2024))
    catalog_state.register_search_groups([searched])
    assert catalog_state.get_group_entry(snap_key) is not None
    assert catalog_state.resolve_group(searched.group_key) is not None

    catalog_state.reset_catalog_state()

    # The map is gone...
    assert catalog_state.resolve_group(searched.group_key) is None
    assert catalog_state.group_resolution(snap_key) is None
    # ...and so is the SNAPSHOT row's index entry: a map-only clear would
    # have left the index describing a catalog the map no longer had.
    assert catalog_state.get_group_entry(snap_key) is None
