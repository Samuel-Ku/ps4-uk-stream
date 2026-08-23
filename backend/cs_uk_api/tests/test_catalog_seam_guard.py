"""Import guard for the private catalog-state package (ticket #339).

The catalog's shared-state implementation lives in the PRIVATE package
``cs_uk_api._catalog_state`` (renamed from the public ``catalog_state``,
ticket #339). Every production caller must go through the typed seam in
``cs_uk_api/catalog.py`` — reaching into the implementation package from
a route, the facade or any other module re-opens the seam this wave
closed.

This test scans the production sources (everything under ``cs_uk_api``
except the tests and the private package itself) for imports of the
implementation package — under EITHER name (``_catalog_state`` or the
retired public ``catalog_state``), absolute or relative, statement or
dynamic-import string — and fails listing every violator.
"""

from __future__ import annotations

import re
from pathlib import Path

import cs_uk_api

#: The ONLY production module allowed to import the private package.
_ALLOWED_IMPORTERS = {"catalog.py"}

#: Named, commented exemption list for ``main.py`` (INTERIM — every
#: entry is eliminated by ticket #345, which migrates main.py's feature
#: paths onto the ``catalog`` seam and shrinks this set to empty).
#: Today main.py still reaches through for:
#:   - ``filter_gated_items`` — the browse gate sweep,
#:   - ``_GATE_CHECK_TIMEOUT_S`` — the browse gate-sweep budget,
#:   - ``await_uakino_ready`` — the content/stream readiness gates,
#:   - the five cache objects feeding main's dead back-compat aliases
#:     (search/content/blocklist/home/home-sources) slated for deletion
#:     in #345.
_MAIN_INTERIM_EXEMPT_SYMBOLS = frozenset(
    {
        "_GATE_CHECK_TIMEOUT_S",
        "await_uakino_ready",
        "blocklist_cache",
        "content_cache",
        "filter_gated_items",
        "home_cache",
        "search_cache",
        "sources_cache",
    }
)

#: Any import statement mentioning the implementation package by either
#: name (word-boundary anchored, so ``catalog_state_x`` never matches).
_CATALOG_STATE_TOKEN = re.compile(r"\b_?catalog_state\b")
_IMPORT_LINE = re.compile(r"^(?:from|import)\s")
_FROM_SPLIT = re.compile(r"^from\s+(\S+)\s+import\s+(.+)$")
#: Dynamic-import trick: the fully-qualified name inside a string.
_QUALIFIED_STRING = re.compile("[\"']cs_uk_api\\.(?:_)?catalog_state")


def _module_is_private(module_path: str) -> bool:
    """True when a ``from <module> import ...`` targets the package."""
    name = module_path.lstrip(".")
    if name == "cs_uk_api" or name.startswith("cs_uk_api."):
        name = name[len("cs_uk_api") :].lstrip(".")
    return name.split(".")[0] in {"catalog_state", "_catalog_state"}


def _violations_in(rel_path: str, text: str) -> list[str]:
    """Violating import lines for one production source file."""
    if rel_path in _ALLOWED_IMPORTERS:
        return []
    found: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        is_import = bool(_IMPORT_LINE.match(line))
        if not is_import and not _QUALIFIED_STRING.search(line):
            continue
        if not _CATALOG_STATE_TOKEN.search(line):
            continue
        if rel_path == "main.py" and is_import:
            from_m = _FROM_SPLIT.match(line)
            if from_m is not None and _module_is_private(from_m.group(1)):
                symbols = {
                    part.split(" as ")[0].strip()
                    for part in from_m.group(2).split(",")
                }
                # The named interim exemption (#345 removes every entry).
                if symbols <= _MAIN_INTERIM_EXEMPT_SYMBOLS:
                    continue
        found.append(f"{rel_path}:{lineno}: {line}")
    return found


def test_only_the_seam_imports_the_private_catalog_package() -> None:
    pkg_root = Path(cs_uk_api.__file__).resolve().parent
    violators: list[str] = []
    for path in sorted(pkg_root.rglob("*.py")):
        rel = path.relative_to(pkg_root).as_posix()
        top = rel.split("/")[0]
        if top in {"tests", "_catalog_state", "catalog_state"}:
            continue
        violators.extend(_violations_in(rel, path.read_text(encoding="utf-8")))
    assert not violators, (
        "Production modules must import catalog state through the"
        " cs_uk_api.catalog seam (ticket #339); direct importers of the"
        " private package:\n  " + "\n  ".join(violators)
    )
