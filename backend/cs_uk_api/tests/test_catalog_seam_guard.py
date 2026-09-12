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


#: The group-resolution accessors retired by the read-side collapse
#: (2026-09-12 review, ADR-0010): ``group_resolution`` and
#: ``snapshot_entries`` replaced them, and a caller that reintroduces
#: one re-opens the three-hop read — ask the index, then the map, then
#: the map again — that the collapse exists to close.
#:
#: Word-boundary anchored, so a longer name never matches, and ``_`` is
#: a word character: ``_card_for_group`` (the FACADE's own helper, which
#: stays) does not match ``card_for_group`` (the retired seam accessor).
_RETIRED_GROUP_ACCESSORS = re.compile(
    r"\b("
    r"card_for_group|poster_url_for_group|view_row_type_for_group"
    r"|year_for_group|genres_for_group|group_sources|first_source"
    r"|group_source|group_entries|home_items_in_index_order"
    r")\b"
)


def test_retired_group_accessors_never_return() -> None:
    """The read side answers through ONE typed lookup plus a separate
    snapshot-only iteration surface (ADR-0010). No production module may
    call a retired accessor, and none may import one back onto the seam —
    a rename with a deprecation shim would leave the old shape alive.

    Comment lines are skipped so the seam module can document the
    retirement in prose (which is where the names belong now).
    """
    pkg_root = Path(cs_uk_api.__file__).resolve().parent
    violators: list[str] = []
    for path in sorted(pkg_root.rglob("*.py")):
        rel = path.relative_to(pkg_root).as_posix()
        if rel.split("/")[0] == "tests":
            continue
        for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if raw.lstrip().startswith("#"):
                continue
            if _RETIRED_GROUP_ACCESSORS.search(raw):
                violators.append(f"{rel}:{lineno}: {raw.strip()}")
    assert not violators, (
        "Group resolution answers through catalog.group_resolution() and"
        " catalog.snapshot_entries() (ADR-0010); retired accessor"
        " references found:\n  " + "\n  ".join(violators)
    )


#: The resolution map's private storage: the TtlCache and the cache key
#: the catalog owner reads it through (ADR-0010). Both are the owner's
#: business — a caller reaching for either is writing the owned state
#: behind the owner's back, which is how the map and the group index
#: came to disagree (issue #420).
_OWNER_PRIVATE_STORE = re.compile(r"\b(sources_cache|_SOURCES_KEY)\b")

#: The modules allowed to name the store or its key: the owner that
#: holds it, and this guard, which cannot forbid a name without
#: spelling it out.
_OWNER_PRIVATE_ALLOWED = {
    "_catalog_state/_stores.py",
    "tests/test_catalog_seam_guard.py",
}


def test_the_resolution_map_storage_stays_the_owners_business() -> None:
    """The map and its cache key have exactly ONE user: the catalog owner.

    Every other caller — production or test — goes through it
    (``group_resolution`` / ``snapshot_entries`` / ``register_search`` /
    the seed and reset seams). The suite used to clear ``sources_cache``
    by hand and seed it under its own key, which left the group index
    describing a map something else had just emptied; naming the store
    again re-opens that. Comment lines are skipped, so the owner's own
    documentation can still talk about the cache it owns.
    """
    pkg_root = Path(cs_uk_api.__file__).resolve().parent
    violators: list[str] = []
    for path in sorted(pkg_root.rglob("*.py")):
        rel = path.relative_to(pkg_root).as_posix()
        if rel in _OWNER_PRIVATE_ALLOWED:
            continue
        for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if raw.lstrip().startswith("#"):
                continue
            if _OWNER_PRIVATE_STORE.search(raw):
                violators.append(f"{rel}:{lineno}: {raw.strip()}")
    assert not violators, (
        "The resolution map and its cache key belong to the catalog owner"
        " (ADR-0010); use catalog.group_resolution / catalog.snapshot_entries"
        " or the owner's seed/reset seams instead. Direct references found:\n  "
        + "\n  ".join(violators)
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
