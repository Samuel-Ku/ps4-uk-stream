"""Arch T12 (spec #309): the configuration seam — store constructors.

The operator story: stores are constructed with a settings argument
(re-instantiable, the snapshot-store pattern), so settings changes
don't require import tricks. These tests pin the constructors and the
ONE production binding — every store is built from the ``SETTINGS``
snapshot at import, and re-instantiating a store from different
settings yields an independent store with the new snapshot's TTLs.
"""

from __future__ import annotations

from dataclasses import replace

from cs_uk_api import catalog_state, poster_proxy
from cs_uk_api.catalog_state import STORES, CatalogStores
from cs_uk_api.config import SETTINGS
from cs_uk_api.main import _browse_cache
from cs_uk_api.profile_store import Profile, ProfileStore, profile_store

# ----------------------------------------------------------------------
# catalog state stores — constructed from a settings snapshot
# ----------------------------------------------------------------------


def test_catalog_stores_are_constructed_from_the_settings_snapshot() -> None:
    """Each cache store takes its TTL from the settings argument."""
    s = replace(
        SETTINGS,
        cache_home_s=7,
        cache_search_s=11,
        cache_content_s=13,
        cache_gated_s=17,
    )
    stores = CatalogStores(s)
    assert stores.home_cache._default_ttl_s == 7
    assert stores.search_cache._default_ttl_s == 11
    assert stores.content_cache._default_ttl_s == 13
    assert stores.blocklist_cache._default_ttl_s == 13  # shares content TTL
    assert stores.gated_cache._default_ttl_s == 17
    assert stores.sources_cache._default_ttl_s == 7  # shares home TTL


def test_catalog_stores_re_instantiation_is_independent() -> None:
    """A fresh store from new settings shares no state with an old one."""
    a = CatalogStores(replace(SETTINGS, cache_home_s=1))
    b = CatalogStores(replace(SETTINGS, cache_home_s=2))
    a.home_cache.set("k", "v")
    assert b.home_cache.get("k") is None
    assert a.home_cache._default_ttl_s == 1
    assert b.home_cache._default_ttl_s == 2


def test_production_stores_are_bound_once_from_settings() -> None:
    """The module-level stores are the ONE binding from the SETTINGS
    snapshot — the bare cache names are aliases of the same singleton."""
    assert STORES.home_cache._default_ttl_s == SETTINGS.cache_home_s
    assert STORES.search_cache._default_ttl_s == SETTINGS.cache_search_s
    assert STORES.content_cache._default_ttl_s == SETTINGS.cache_content_s
    assert STORES.blocklist_cache._default_ttl_s == SETTINGS.cache_content_s
    assert STORES.gated_cache._default_ttl_s == SETTINGS.cache_gated_s
    assert STORES.sources_cache._default_ttl_s == SETTINGS.cache_home_s
    assert catalog_state.home_cache is STORES.home_cache
    assert catalog_state.search_cache is STORES.search_cache
    assert catalog_state.content_cache is STORES.content_cache
    assert catalog_state.blocklist_cache is STORES.blocklist_cache
    assert catalog_state.gated_cache is STORES.gated_cache
    assert catalog_state.sources_cache is STORES.sources_cache


# ----------------------------------------------------------------------
# profile store — constructed with settings
# ----------------------------------------------------------------------


def test_profile_store_accepts_settings_at_construction() -> None:
    """ProfileStore is re-instantiable: a fresh store from settings is
    independent of the module singleton (same install/get/warm seam)."""
    store = ProfileStore(SETTINGS)
    assert store.get() == Profile()
    assert store.warm() == Profile()
    installed = Profile(played={"p1:s1e1": 5})
    assert store.install(installed) is installed
    assert store.get() is installed
    # The production singleton is a separate store from the same snapshot.
    assert profile_store.get() == Profile()


# ----------------------------------------------------------------------
# poster + browse caches — built from the settings snapshot
# ----------------------------------------------------------------------


def test_poster_and_browse_caches_built_from_settings_snapshot() -> None:
    """The poster and browse cache stores take their TTL from the
    SETTINGS snapshot at construction (structural invariant)."""
    assert poster_proxy._cache._default_ttl_s == SETTINGS.cache_poster_s
    assert _browse_cache._default_ttl_s == SETTINGS.cache_search_s
