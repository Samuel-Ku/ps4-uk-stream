"""Internal resolution module (spec #309 T5) — group-key machinery.

Covers ``cs_uk_api._catalog_state.resolution`` directly: ``g2:``
resolution from the sources map, the episode-tail reverse lookup
(exact + numeric fallback), search-group registration folding into the
map, the subscription-gate sweep verdicts, and the hard-unavailable
verdict. The wire-level behaviour is pinned in test_gated_filter /
test_group_content_peek / test_jellyfin_detail; these tests exercise
the internal module's own contracts.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from cs_uk_api import _catalog_state as catalog_state
from cs_uk_api._catalog_state.resolution import (
    episode_group_key,
    filter_gated_items,
    group_key_for_external,
    is_hard_unavailable,
    register_search_groups,
    resolve_group,
)
from cs_uk_api.models import (
    ContentResponse,
    SearchGroup,
    SearchResult,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError


def _item(pid: str, external: str, title: str) -> SearchResult:
    mb_form, _mb_styles = ("movie", frozenset())
    return SearchResult(
        id=f"{pid}:{external}",
        provider=pid,
        form=mb_form,
        styles=frozenset(),
        title=title,
        year=2021,
        url=f"https://{pid}.example/{external}",
    )


def _seed_sources(mapping: dict[str, dict[str, SearchResult]]) -> None:
    catalog_state.sources_cache.set(catalog_state._SOURCES_KEY, mapping)


@pytest.fixture(autouse=True)
def isolate() -> Iterator[None]:
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (
        catalog_state.sources_cache,
        catalog_state.content_cache,
        catalog_state.gated_cache,
        catalog_state.blocklist_cache,
        catalog_state.home_cache,
    ):
        cache.clear()
    catalog_state.clear_playback()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_resolve_group_cold_is_none_then_seeded() -> None:
    assert resolve_group("g2:abc") is None
    item = _item("p1", "1", "Дюна")
    _seed_sources({"g2:abc": {"p1": item}})
    per_provider = resolve_group("g2:abc")
    assert per_provider is not None
    assert per_provider["p1"].title == "Дюна"


def test_episode_group_key_passthrough_and_reverse_lookup() -> None:
    # A movie reports its g2: key directly.
    assert episode_group_key("g2:abc") == "g2:abc"
    # An episode wire id resolves through the sources map.
    item = _item("p1", "dorama-1", "Серіал")
    _seed_sources({"g2:ser": {"p1": item}})
    assert episode_group_key("p1:dorama-1:s1e1") == "g2:ser"
    # An unresolvable id answers None, never raises.
    assert episode_group_key("p1:unknown:s1e1") is None


def test_group_key_for_external_exact_and_numeric_fallback() -> None:
    _seed_sources(
        {
            "g2:ser": {
                "p1": _item("p1", "anime-series:6268-narutto-1-sezon", "Наруто"),
            }
        }
    )
    # Exact composite match.
    assert group_key_for_external("p1:anime-series:6268-narutto-1-sezon") == "g2:ser"
    # Ticket #234 numeric fallback: the episode prefix ``p1:6268`` misses
    # exactly, but the numeric segment matches inside the card id.
    assert group_key_for_external("p1:6268") == "g2:ser"
    assert group_key_for_external("p1:zzz") is None


def test_register_search_groups_folds_member_keys_into_resolution_map() -> None:
    """A searched card registers under EVERY member key (ticket #106) —
    any key the client holds resolves the full group."""
    item = _item("p1", "x", "Дюна")
    group = SearchGroup(
        group_key="g2:dune",
        title="Дюна",
        year=2021,
        poster=None,
        form="movie",
        styles=[],
        genres=[],
        sources=[item],
        member_keys=["g2:dune", "g2:dune-yearless"],
    )
    register_search_groups([group])
    for key in ("g2:dune", "g2:dune-yearless"):
        per_provider = resolve_group(key)
        assert per_provider is not None and per_provider["p1"].title == "Дюна"


class _Gated(BaseProvider):
    """can_gate provider whose content() always raises ``gated``."""

    id = "gated-stub"
    name = "GatedStub"
    types = ("movie",)
    can_gate = True

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise ProviderError(code="gated", message="promo only")

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


async def test_filter_gated_items_drops_gated_and_caches_verdict() -> None:
    PROVIDERS["gated-stub"] = _Gated()
    items = [_item("gated-stub", "1", "Промо")]
    kept = await filter_gated_items(items, http=cast(Any, None))
    assert kept == []
    # The verdict is cached — a repeat pass needs no provider call.
    assert catalog_state.gated_cache.get("content:gated-stub:1") is True


class _Free(BaseProvider):
    id = "free-stub"
    name = "FreeStub"
    types = ("movie",)
    can_gate = True

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        return ContentResponse(
            id=f"free-stub:{external_id}",
            form="movie",
            title="Вільний",
            translations=[Translation(id="uk", label="UK")],
        )

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


async def test_filter_gated_items_keeps_free_card_and_marks_known_good() -> None:
    PROVIDERS["free-stub"] = _Free()
    items = [_item("free-stub", "1", "Вільний")]
    kept = await filter_gated_items(items, http=cast(Any, None))
    assert [it.id for it in kept] == ["free-stub:1"]
    assert catalog_state.gated_cache.get("content:free-stub:1") is False


def test_is_hard_unavailable_unknown_gated_and_clean() -> None:
    # Unknown group — the cold-cache 404 stands.
    assert is_hard_unavailable("g2:unknown") is True
    item = _item("p1", "1", "Дюна")
    _seed_sources({"g2:abc": {"p1": item}})
    # Seeded + no verdicts → available.
    assert is_hard_unavailable("g2:abc") is False
    # A gated verdict flips it to hard-unavailable.
    catalog_state.gated_cache.set("content:p1:1", True)
    assert is_hard_unavailable("g2:abc") is True
