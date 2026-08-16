import os
import sys
from pathlib import Path

# Make the package importable when running pytest from backend/
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Disable the startup catalog warm (#204/#210) in tests: it would run
# real provider scrapes inside any TestClient lifespan, slowing the
# suite and hitting upstream sites.
os.environ.setdefault("CS_UK_CATALOG_WARM", "0")

# Keep the resume store memory-only in tests (ticket #248): the default
# suite must not write/read a real state file under the operator's home
# dir. Persistence is exercised by dedicated ResumeStore tests that
# construct stores over tmp paths. An explicit empty string disables the
# disk layer (see config._load_resume_path).
os.environ.setdefault("CS_UK_RESUME_PATH", "")

# Same for the user-state store (favorites/played, ticket #258): tests
# keep it memory-only; persistence is exercised by dedicated
# UserStateStore tests over tmp paths.
os.environ.setdefault("CS_UK_USER_STATE_PATH", "")

# Same for the home snapshot store (ticket #269): the default suite
# must not write/read a real snapshot file; persistence is exercised by
# dedicated SnapshotStore tests and the cold-start wire test over tmp
# paths.
os.environ.setdefault("CS_UK_SNAPSHOT_PATH", "")

import pytest  # noqa: E402

from cs_uk_api import llm  # noqa: E402
from cs_uk_api.catalog_state import _stores  # noqa: E402
from cs_uk_api.health import TRACKER  # noqa: E402
from cs_uk_api.main import _browse_cache  # noqa: E402
from cs_uk_api.poster_proxy import _cache as _poster_cache  # noqa: E402
from cs_uk_api.providers import (  # noqa: E402
    PROVIDERS,
    _registry,
)
from cs_uk_api.watchdog import WATCHDOG  # noqa: E402

#: Every module-level mutable store the suite shares (ticket #330).
#: Order-independent hygiene: each is cleared before every test so no
#: test can poison another through cached verdicts, health windows,
#: profiles, playback/user/snapshot state, or watchdog counters.
_SHARED_STORES: tuple[object, ...] = (
    _poster_cache,
    _browse_cache,
    _stores.home_cache,
    _stores.search_cache,
    _stores.content_cache,
    _stores.blocklist_cache,
    _stores.row_deep_cache,
    _stores.deep_page_cache,
    _stores.gated_cache,
    _stores.sources_cache,
)


@pytest.fixture(autouse=True)
def _reset_global_state() -> None:
    """Reset all process-wide state before each test (ticket #330).

    pytest-randomly shuffles test order; without this reset, a test that
    records a gated/blocklisted verdict, a health sample, a profile, a
    favorite, a playback position, or a watchdog tick silently breaks a
    later test using the same keys (observed: 3-12 failures varying by
    seed, green under ``-p no:randomly``).
    """
    TRACKER.reset()
    WATCHDOG.reset()
    for store in _SHARED_STORES:
        store.clear()  # type: ignore[attr-defined]
    # Tests mutate PROVIDERS directly (clear/add stubs) with save/restore
    # attempts that are NOT order-safe: a test that clears without
    # restoring (e.g. test_gated_filter) makes every later snapshot an
    # empty one, permanently draining the registry for the rest of the
    # suite. Re-run the authoritative bootstrap instead of snapshotting.
    PROVIDERS.clear()
    _registry.bootstrap()
    _stores.install_profiles({})
    _stores.clear_playback()
    _stores.clear_user_state()
    _stores.clear_snapshot_store()
    llm.set_active_profile(None)
    yield

