"""Import guard for the config single-binding seam (architecture §5).

Every production module reads settings through the config module
reference (``from . import config as _config`` → ``_config.SETTINGS.x``);
§5 forbids importing the value into a module's own binding, because a
module-level ``SETTINGS`` binding is a snapshot: it keeps answering with
whatever the process imported at startup, so a test that patches the
sanctioned point (``cs_uk_api.config.SETTINGS``) silently stops
reaching that module — the exact failure the 2026-09-20 audit found in
``llm.py``, which this guard's scan then showed was not the only one.

The single test patch point is ``cs_uk_api.config.SETTINGS`` (§5).
``llm.py`` was converted to module-reference reads in the pass that
landed this guard; its tests patch ``_config`` like every other module's.
"""

from __future__ import annotations

import re
from pathlib import Path

import cs_uk_api

#: Value-import shapes the seam forbids: a from-import of a ``config``
#: module (relative at any depth, absolute, or bare) that binds the
#: ``SETTINGS`` name — including aliased and multi-name imports.
_CONFIG_FROM_IMPORT = re.compile(
    r"^\s*from\s+(?:\.+\s*)?(?:cs_uk_api\.)?config\s+import\b"
)
_SETTINGS_NAME = re.compile(r"\bSETTINGS\b")

#: ``config.py`` is the binding's owner (§5): it DEFINES the single
#: ``SETTINGS = load_settings()`` binding everything else reads through.
_OWNER = "config.py"

#: Pre-existing value imports, recorded rather than fixed: converting
#: them was out of scope for the pass that landed this guard, and each
#: carries its own patch-point migration (``dashboard``'s test patches
#: the module binding). They are the ONLY sanctioned exceptions — the
#: gate below fails on any new one, and the staleness test keeps these
#: entries honest until they are converted.
_ALLOWED_VALUE_IMPORTS: dict[str, str] = {
    "jellyfin/handshake.py": "pre-existing (3 usages); conversion is a separate pass",
    "jellyfin/identity.py": "pre-existing (1 usage); conversion is a separate pass",
    "jellyfin/dashboard.py": (
        "pre-existing (1 usage); its test patches the module binding"
        " (test_jellyfin_dashboard.py) and must migrate with it"
    ),
}


def _violations_in(rel_path: str, text: str) -> list[str]:
    """Violating value-import lines for one production source file."""
    if rel_path == _OWNER or rel_path in _ALLOWED_VALUE_IMPORTS:
        return []
    found: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if _CONFIG_FROM_IMPORT.match(raw) and _SETTINGS_NAME.search(raw):
            found.append(f"{rel_path}:{lineno}: {raw.strip()}")
    return found


def _production_sources() -> list[tuple[str, str]]:
    """(package-relative path, text) for every production module."""
    pkg_root = Path(cs_uk_api.__file__).resolve().parent
    out: list[tuple[str, str]] = []
    for path in sorted(pkg_root.rglob("*.py")):
        rel = path.relative_to(pkg_root).as_posix()
        if rel.split("/")[0] == "tests":
            continue
        out.append((rel, path.read_text(encoding="utf-8")))
    return out


def test_no_production_module_imports_the_settings_value() -> None:
    """§5: reads go through ``_config.SETTINGS``, never a local binding.

    The owner (``config.py``) must still define the single binding, and
    no production module outside it may value-import ``SETTINGS`` except
    the recorded exceptions in ``_ALLOWED_VALUE_IMPORTS``.
    """
    owner_text = dict(_production_sources())[_OWNER]
    assert re.search(r"^SETTINGS\s*[:=]", owner_text, re.MULTILINE), (
        "config.py no longer defines the single SETTINGS binding (§5);"
        " this guard and every ``_config.SETTINGS`` reader lost their seam"
    )

    violators: list[str] = []
    for rel, text in _production_sources():
        violators.extend(_violations_in(rel, text))
    assert not violators, (
        "Production modules must read settings through the config module"
        " reference (``_config.SETTINGS``; §5) — a value import snapshots"
        " the object and silently dodges the single patch point. Importers"
        " found:\n  " + "\n  ".join(violators)
    )


def test_the_value_import_allowlist_stays_current() -> None:
    """A converted module must not keep its exemption.

    If an allowlisted file no longer value-imports SETTINGS, its entry
    is dead weight that would silently re-shield a FUTURE re-introduced
    import — remove the entry in the same commit as the conversion.
    """
    sources = dict(_production_sources())
    stale: list[str] = []
    for rel in _ALLOWED_VALUE_IMPORTS:
        text = sources.get(rel)
        if text is None:
            stale.append(f"{rel}: allowlisted but the file is gone")
            continue
        if not any(
            _CONFIG_FROM_IMPORT.match(line) and _SETTINGS_NAME.search(line)
            for line in text.splitlines()
        ):
            stale.append(f"{rel}: converted — remove its allowlist entry")
    assert not stale, (
        "Stale seam-guard allowlist entries:\n  " + "\n  ".join(stale)
    )


def test_the_detector_catches_the_value_import_shapes() -> None:
    """Pin the import shapes the guard forbids and the ones it allows.

    Forbidden: relative at any depth, absolute, aliased, multi-name.
    Allowed: the module reference (``from . import config as _config``),
    a plain module import, and from-imports of other modules.
    """
    forbidden = [
        "from .config import SETTINGS",
        "from ..config import SETTINGS",
        "from ...config import SETTINGS",
        "from cs_uk_api.config import SETTINGS",
        "from .config import SETTINGS as _S",
        "from .config import Something, SETTINGS",
    ]
    allowed = [
        "from . import config as _config",
        "from .. import config as _config",
        "import cs_uk_api.config",
        "from .config import Something",
        "from pydantic_settings import BaseSettings",
        "x = _config.SETTINGS.llm_key",
    ]
    for line in forbidden:
        assert _CONFIG_FROM_IMPORT.match(line) and _SETTINGS_NAME.search(line), (
            f"detector missed a forbidden shape: {line!r}"
        )
    for line in allowed:
        assert not (
            _CONFIG_FROM_IMPORT.match(line) and _SETTINGS_NAME.search(line)
        ), f"detector flagged an allowed shape: {line!r}"
