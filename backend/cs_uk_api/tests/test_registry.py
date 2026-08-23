"""Registry completeness guard.

Every concrete provider module under ``cs_uk_api.providers`` must be
registered after bootstrap, so the live gate can exercise it.

Regression: commit e79329b ("feat(providers): EneyidaProvider for #17")
replaced ``register(CoaninetProvider())`` with ``register(EneyidaProvider())``
instead of adding a new line, silently dropping Coaninet from the
registry while its module and tests stayed in place.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import cs_uk_api.providers._registry  # noqa: F401  (runs bootstrap() at import)
from cs_uk_api import providers
from cs_uk_api.providers import PROVIDERS, BaseProvider


def _concrete_provider_ids() -> list[str]:
    ids: list[str] = []
    pkg_dir = Path(providers.__file__).parent
    for path in sorted(pkg_dir.glob("*.py")):
        module_name = path.stem
        if module_name.startswith("_") or module_name == "base":
            continue
        module = importlib.import_module(f"{providers.__name__}.{module_name}")
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseProvider)
                and obj is not BaseProvider
                and getattr(obj, "id", None)
            ):
                ids.append(obj.id)
    return ids


def test_every_provider_module_is_registered() -> None:
    registered = {p.id for p in PROVIDERS.values()}
    assert registered == set(_concrete_provider_ids())


def test_every_registered_provider_declares_a_nonempty_allowlist() -> None:
    """ADR-0005 startup-fail-closed guarantee: every active provider
    declares ``allowed_hosts``, so ``provider_safe_get`` can enforce the
    SSRF contract centrally. An adapter that forgets the declaration
    fails this test at suite time instead of fetching undeclared hosts
    (or failing every fetch) in production. uakino rides the browser
    session for its own site — it still declares the ashdi.vip hosts
    its stream path fetches."""
    empty = [p.id for p in PROVIDERS.values() if not p.allowed_hosts]
    assert not empty, f"providers without an allowlist declaration: {empty}"
