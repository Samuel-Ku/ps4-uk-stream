"""Wire identity module (spec #309, step 1; #340 consolidation) — id grammar + projection.

Pins the single home of the wire id grammars:

  - the group-key prefix (``g2:``) and its recognizer ``is_group_key``;
  - the episode-tail grammar (``:s1e1`` / ``:e5`` / ``:eN:<blob>``);
  - the movie-suffix sentinel (``:__movie__``);
  - the ``provider:external`` composite split;

and the single projection function for merged groups (canonical fields +
member keys) that the home rows, the search groups and the group-key
resolution map all share.

Invariant: these are the SAME values the old per-module copies produced
— the migration must be wire-invisible (US11). ``merge.group_key``
builds every key from ``wire_identity.group_key``, so a version bump
edits one file (US4).

Import direction (spec #340): ``wire_identity`` is a leaf — it imports
nothing but models + stdlib, never ``merge``; the guard tests below
enforce it.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

import cs_uk_api.wire_identity
from cs_uk_api.merge import item_group_key, merge_results
from cs_uk_api.models import SearchResult
from cs_uk_api.providers.base import model_b_axes
from cs_uk_api.wire_identity import (
    GROUP_KEY_PREFIX,
    MOVIE_SUFFIX,
    group_key,
    is_group_key,
    is_movie_wire_id,
    project_group,
    provider_union,
    split_episode_tail,
    strip_movie_suffix,
)


def _make_item(
    pid: str,
    title: str,
    year: int | None = None,
    n: str = "1",
) -> SearchResult:
    mb_form, mb_styles = model_b_axes(cast(Any, "movie"))
    return SearchResult(
        id=f"{pid}:{n}",
        provider=pid,
        form=mb_form,
        styles=mb_styles,
        title=title,
        year=year,
        url=f"https://{pid}.example/{n}",
    )


# ---------------------------------------------------------------------------
# Group-key prefix
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Import-direction guard (spec #340): wire_identity is a leaf
# ---------------------------------------------------------------------------


def _wire_identity_source() -> str:
    import cs_uk_api.wire_identity

    return Path(cs_uk_api.wire_identity.__file__).read_text(encoding="utf-8")


@pytest.mark.unit
def test_wire_identity_has_no_merge_import_anywhere() -> None:
    """AST guard: ``wire_identity`` must not import ``merge`` — at module
    level, inside a function body (the old lazy cycle-break), or under
    ``TYPE_CHECKING``. The dependency edge is one-way: merge →
    wire_identity, never the reverse (spec #340)."""
    tree = ast.parse(_wire_identity_source())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "merge" or alias.name.startswith("merge."):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (
                node.level > 0
                and module.split(".")[0] == "merge"
                or module in ("cs_uk_api.merge", "merge")
                or module.startswith("cs_uk_api.merge.")
            ):
                names = ", ".join(a.name for a in node.names)
                offenders.append(f"line {node.lineno}: from {module} import {names}")
    assert not offenders, f"wire_identity imports merge: {offenders}"


@pytest.mark.unit
def test_importing_wire_identity_does_not_pull_merge_transitively() -> None:
    """Runtime guard: a fresh interpreter that imports ONLY
    ``cs_uk_api.wire_identity`` must not end up with
    ``cs_uk_api.merge`` in ``sys.modules`` — the grammar module stays a
    models+stdlib leaf (spec #340)."""
    backend_root = str(Path(cs_uk_api.wire_identity.__file__).resolve().parents[2])
    program = (
        "import sys; "
        "import cs_uk_api.wire_identity; "
        "sys.exit(1 if 'cs_uk_api.merge' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=backend_root,
        env={**os.environ, "PYTHONPATH": backend_root},
        timeout=60,
    )
    assert result.returncode == 0, (
        "importing cs_uk_api.wire_identity pulled in cs_uk_api.merge:\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.unit
def test_group_key_carries_the_shared_prefix() -> None:
    """``merge`` builds every key from the wire module's prefix — the
    version lives in ONE file (US4: a bump edits one file)."""
    key = group_key("дюна", "movie", 2021)
    assert key.startswith(GROUP_KEY_PREFIX)
    assert key == f"{GROUP_KEY_PREFIX}{key[len(GROUP_KEY_PREFIX):]}"
    assert len(key) == len(GROUP_KEY_PREFIX) + 16  # 16-hex digest
    assert is_group_key(key)
    assert key == item_group_key(_make_item("p1", "Дюна", year=2021, n="p1-1"))


@pytest.mark.unit
def test_is_group_key_distinguishes_group_keys_from_other_ids() -> None:
    """The recognizer the routes use to route: group keys yes; episode
    wire ids, movie-suffix ids, plain composites and view ids no."""
    assert is_group_key("g2:" + "0" * 16)
    assert not is_group_key("ufdub:dorama-408-x:s1e1")
    assert not is_group_key("cikavaideya:281-duelianty:__movie__")
    assert not is_group_key("p1:serial-1")
    assert not is_group_key("0123456789abcdef0123456789abcdef")


# ---------------------------------------------------------------------------
# Episode-tail grammar
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_split_episode_tail_ufdub_style() -> None:
    """``:s1e1`` tail — the composite prefix identifies the merged group."""
    assert split_episode_tail("ufdub:dorama-408-ona:s1e1") == (
        "ufdub:dorama-408-ona",
        ":s1e1",
    )


@pytest.mark.unit
def test_split_episode_tail_bare_e_style() -> None:
    """``:e5`` tail (uakino/kinotron-style)."""
    assert split_episode_tail("uakino:6268:e5") == ("uakino:6268", ":e5")


@pytest.mark.unit
def test_split_episode_tail_animeon_blob() -> None:
    """The base64 source blob AFTER the ``:eN`` tail stays part of the
    id (animeon-style) — the tail regex must not split mid-id."""
    blob = "eyJzb3VyY2UiOiJ4In0="
    assert split_episode_tail(f"animeon:918:e1:{blob}") == ("animeon:918", f":e1:{blob}")


@pytest.mark.unit
def test_split_episode_tail_rejects_non_episodes() -> None:
    """Group keys, movie-suffix ids and plain composites have no tail."""
    assert split_episode_tail("g2:" + "0" * 16) is None
    assert split_episode_tail("eneyida:films/9366-duna:__movie__") is None
    assert split_episode_tail("p1:serial-1") is None


# ---------------------------------------------------------------------------
# Movie-suffix sentinel
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_movie_suffix_strip_and_recognize() -> None:
    """The ``:__movie__`` sentinel (one definition, spec #309) round-trips:
    recognize it on a movie wire id, strip it to the bare external id,
    leave a bare id untouched."""
    movie_id = f"cikavaideya:281-duelianty{MOVIE_SUFFIX}"
    assert is_movie_wire_id(movie_id)
    assert strip_movie_suffix(movie_id) == "cikavaideya:281-duelianty"
    assert not is_movie_wire_id("p1:serial-1:s1e1")
    assert strip_movie_suffix("p1:serial-1:s1e1") == "p1:serial-1:s1e1"


# ---------------------------------------------------------------------------
# Single merge projection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_project_group_canonical_fields_and_member_keys() -> None:
    """One merged group → canonical fields (first-seen source) + member
    keys + provider union — the projection every consumer shares."""
    yearful = _make_item("p1", "Дюна", year=2021, n="p1-1")
    yearless = _make_item("p2", "Дюна", year=None, n="p2-1")
    groups = merge_results([yearful, yearless])
    assert len(groups) == 1
    proj = project_group(groups[0])

    # Canonical fields from the first-seen source.
    assert proj.key == item_group_key(yearful)  # yearful-preferred-min
    assert proj.title == "Дюна"
    assert proj.year == 2021
    assert proj.form == "movie"
    # Both member keys present, deduped, first-seen order.
    assert set(proj.member_keys) == {item_group_key(yearful), item_group_key(yearless)}
    assert proj.key in proj.member_keys
    # Provider union, first-seen order.
    assert proj.providers == ("p1", "p2")
    assert proj.sources == (yearful, yearless)


@pytest.mark.unit
def test_project_group_dedupes_member_keys() -> None:
    """Duplicate listings from one provider collapse to one member key
    (issue #89: one key per item identity)."""
    a1 = _make_item("p1", "Дюна", year=2021, n="p1-1")
    a2 = _make_item("p1", "Дюна", year=2021, n="p1-2")
    assert item_group_key(a1) == item_group_key(a2)
    groups = merge_results([a1, a2])
    proj = project_group(groups[0])
    assert proj.member_keys == (item_group_key(a1),)
    assert proj.providers == ("p1",)


@pytest.mark.unit
def test_provider_union_first_seen_wins() -> None:
    """The resolution map's shape: ``provider -> first-seen SearchResult``."""
    first = _make_item("p1", "Дюна", year=2021, n="p1-1")
    second = _make_item("p1", "Дюна", year=2021, n="p1-2")
    third = _make_item("p2", "Дюна", year=2021, n="p2-1")
    union = provider_union([first, second, third])
    assert list(union) == ["p1", "p2"]
    assert union["p1"] is first  # first-seen wins, not the later duplicate
    assert union["p2"] is third
