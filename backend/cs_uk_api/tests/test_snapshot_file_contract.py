"""What the home-snapshot file is ALLOWED to contain (ticket #269, ADR-0010).

The file is the next cold start's snapshot, so its content is a contract
rather than an implementation detail: the snapshot's rows and the
SNAPSHOT's own resolution map, under the versioned envelope — and nothing
else. Transient state must never land there. A search registration is
search-TTL state (ADR-0010), and persisting one would:

  - resurrect an expired promise across a restart, because a cold start
    applies the file with no age check and no registration record comes
    back to retire it;
  - grow the file with a session's searches (16 keys measured per search
    on the deployment, issue #420).

These are the PRODUCER-side pins: what the apply step is allowed to hand
the store. ``test_snapshot_store.py`` owns the envelope mechanics (round
trip, atomic write, corrupt / version-mismatched degrade); this module
owns what may be inside that envelope, including the guarantee that a
restart cannot bring a transient key back.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import cs_uk_api._catalog_state as catalog_state
import cs_uk_api._catalog_state.snapshot as snapshot_mod
from cs_uk_api.merge import item_group_key
from cs_uk_api.models import SearchGroup, SearchResult
from cs_uk_api.snapshot_store import SNAPSHOT_VERSION, SnapshotStore

_FILE_NAME = "home-snapshot.json"


def _item(pid: str, title: str, *, n: str = "1", year: int = 2024) -> SearchResult:
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form="movie",
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
    )


def _group(group_key: str, *sources: SearchResult) -> SearchGroup:
    """A merged search group under an EXPLICIT key (member keys kept 1:1).

    The key is caller-chosen so a test can register a search carrying a
    provider the snapshot does not know, without inventing a second group
    identity for the same title.
    """
    return SearchGroup(
        group_key=group_key,
        title=sources[0].title,
        year=sources[0].year,
        form=sources[0].form,
        sources=list(sources),
        member_keys=[group_key],
    )


def _raw(path: Path) -> dict[str, object]:
    """The file's parsed JSON envelope."""
    return json.loads(path.read_text(encoding="utf-8"))


def _persisted_map(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    """The envelope's ``data.sources`` map, typed for the assertions."""
    data = payload["data"]
    assert isinstance(data, dict)
    sources = data["sources"]
    assert isinstance(sources, dict)
    return sources


def _persisted_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    data = payload["data"]
    assert isinstance(data, dict)
    rows = data["rows"]
    assert isinstance(rows, list)
    return rows


@pytest.fixture()
def snapshot_path(tmp_path: Path) -> Iterator[Path]:
    """A real ``SnapshotStore`` over a temp path, restored afterwards.

    Leaking the temp-path store would keep serving the persisted file to
    every later cold ``load_home`` in the process (the isolation note in
    ``test_snapshot_store.py``'s cold-start test).
    """
    previous = catalog_state._snapshot_store()
    path = tmp_path / _FILE_NAME
    catalog_state.install_snapshot_store(SnapshotStore(str(path)))
    try:
        yield path
    finally:
        catalog_state.install_snapshot_store(previous)


def test_the_file_holds_the_snapshot_rows_and_map_and_nothing_else(
    snapshot_path: Path,
) -> None:
    """Rows + the SNAPSHOT's own map, under the versioned envelope.

    With no registration in play the built map IS the snapshot's map, so
    this pins the envelope's exact key set, the row round trip against the
    in-memory snapshot, the map's completeness (every persisted key
    carries at least one provider — a card-less placeholder has no business
    on disk), and that the file is a faithful copy rather than a superset.
    """
    snapshot_mod._cache_home(
        {"p1": [_item("p1", "Дюна", year=2021)]},
        {},
        {},
    )

    payload = _raw(snapshot_path)
    assert payload["version"] == SNAPSHOT_VERSION
    assert set(payload["data"]) == {"rows", "sources"}  # type: ignore[arg-type]

    home = catalog_state.get_home()
    assert home is not None
    assert sorted(it["title"] for row in _persisted_rows(payload) for it in row["items"]) == sorted(
        it.title for row in home.rows for it in row.items
    )

    persisted = _persisted_map(payload)
    live = catalog_state.catalog_state().sources()
    assert set(persisted) == set(live)
    for key, per_provider in persisted.items():
        assert per_provider, "a card-less placeholder must never be persisted"
        assert set(per_provider) == set(live[key])


def test_a_registration_cannot_smuggle_a_provider_into_the_file(
    snapshot_path: Path,
) -> None:
    """The enrichment is memory-only; the file keeps the SNAPSHOT's map.

    A search that knows a second provider enriches the LIVE map — that is
    the decided union behaviour — but the persisted map must stay the
    snapshot's own, or a provider the next snapshot has dropped would come
    back from disk after every restart.
    """
    card = _item("p1", "Дюна", year=2021)
    key = item_group_key(card)
    snapshot_mod._cache_home({"p1": [card]}, {}, {})

    catalog_state.register_search_groups([_group(key, card, _item("p9", "Дюна", n="9", year=2021))])
    assert set(catalog_state.catalog_state().sources()[key]) == {"p1", "p9"}

    # The leak needs a replacement to land while the registration is live:
    # that is the moment the merged map could reach the file.
    snapshot_mod._cache_home({"p1": [card]}, {}, {})
    assert set(_persisted_map(_raw(snapshot_path))[key]) == {"p1"}


def test_a_cold_start_from_the_file_cannot_resurrect_a_registration(
    snapshot_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promise is in-memory state; the file cannot bring one back.

    The search-only key is live after the rebuild and absent from the
    file. A restart — fresh owner with no registrations, empty caches,
    the file as the only source — must MISS it and still resolve the
    snapshot's own row, card included.
    """
    search_only = _item("p9", "Пошуковий Фільм")
    group = _group(item_group_key(search_only), search_only)
    key = group.group_key
    card = _item("p1", "Снапшот", n="2")
    card_key = item_group_key(card)

    catalog_state.register_search_groups([group])
    snapshot_mod._cache_home({"p1": [card]}, {}, {})
    assert catalog_state.resolve_group(key) is not None
    assert key not in _persisted_map(_raw(snapshot_path))

    # Restart: a fresh owner carries no registrations, the caches are
    # empty, and only the file is left to answer from.
    catalog_state.install_catalog_state(catalog_state.CatalogState())
    catalog_state.home_cache.clear()
    catalog_state.reset_catalog_state()
    catalog_state.PROVIDERS.clear()

    async def _noop_heal() -> None:
        return None

    monkeypatch.setattr(snapshot_mod, "_build_home", _noop_heal)
    home = asyncio.run(catalog_state.load_home())
    assert home is not None

    assert catalog_state.resolve_group(key) is None
    assert catalog_state.get_group_entry(key) is None
    assert catalog_state.resolve_group(card_key) is not None
    entry = catalog_state.get_group_entry(card_key)
    assert entry is not None and entry.home_item is not None


def test_a_sanctioned_invalidation_never_touches_the_file(snapshot_path: Path) -> None:
    """The file is written by a successful build only (ADR-0010).

    ``invalidate`` is an in-memory event: the warm path's sanctioned clear
    must leave the last good snapshot on disk, because that file is what
    makes the NEXT cold start instant (ticket #269) — a clear that wiped
    it would trade a background rebuild for a cold-start fan-out.
    """
    snapshot_mod._cache_home({"p1": [_item("p1", "Дюна", year=2021)]}, {}, {})
    before = snapshot_path.read_bytes()

    catalog_state.catalog_state().invalidate(reason="test")

    assert catalog_state.get_home() is None
    assert snapshot_path.read_bytes() == before
    persisted, sources = SnapshotStore(str(snapshot_path)).load()
    assert persisted is not None and sources is not None
