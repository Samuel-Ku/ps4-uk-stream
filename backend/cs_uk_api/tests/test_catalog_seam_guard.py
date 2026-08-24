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
#: (Ticket #345 eliminated main.py's last direct imports; the interim
#: exemption list from #339 is gone.)
_ALLOWED_IMPORTERS = {"catalog.py"}

#: Any import statement mentioning the implementation package by either
#: name (word-boundary anchored, so ``catalog_state_x`` never matches).
_CATALOG_STATE_TOKEN = re.compile(r"\b_?catalog_state\b")
_IMPORT_LINE = re.compile(r"^(?:from|import)\s")
#: Dynamic-import trick: the fully-qualified name inside a string.
_QUALIFIED_STRING = re.compile("[\"']cs_uk_api\\.(?:_)?catalog_state")


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


#: The row-kind vocabularies the table owns (spec #362 hardening item
#: 4): no production module may DEFINE these names anymore — every one
#: was a private shadow of a ``row_kinds`` fact, and two of them
#: contradicted the table (popular's extendability, the retired
#: «Новинки» row).
_ROW_KIND_VOCABULARY = re.compile(
    r"^\s*"
    r"(_VIEW_TYPES|_COLLECTION_TYPE_BY_ROW|JF_TYPE_BY_ROW|_HOME_KINDS_BY_JF_TYPE|_EXTENDABLE_ROWS)"
    r"\b\s*[:=]"
)


def test_row_kind_vocabularies_are_defined_only_in_the_table() -> None:
    """Spec #362 hardening item 4: any production DEFINITION of a facade/
    snapshot row-kind vocabulary outside ``row_kinds.py`` fails — the
    table is the single source of these facts, so a re-introduced shadow
    (the drift this wave removed) cannot come back silently."""
    pkg_root = Path(cs_uk_api.__file__).resolve().parent
    violators: list[str] = []
    for path in sorted(pkg_root.rglob("*.py")):
        rel = path.relative_to(pkg_root).as_posix()
        if rel.split("/")[0] == "tests" or rel == "row_kinds.py":
            continue
        for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _ROW_KIND_VOCABULARY.match(raw):
                violators.append(f"{rel}:{lineno}: {raw.strip()}")
    assert not violators, (
        "Row-kind facts live only in cs_uk_api/row_kinds.py (spec #362);"
        " private vocabulary definitions found:\n  " + "\n  ".join(violators)
    )
