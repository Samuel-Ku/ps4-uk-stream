"""Cross-provider merge core (issue #52, v3 spec §4).

Pure functions, no I/O. Two listing items from different providers are the
same title iff:

    normalized title (any alias) equal
    AND type equal
    AND (year equal OR at least one year unknown)

Group keys are stateless and versioned: ``group_key`` = "gN:" + sha1 of the
canonical (alias|form|year) triple, so a normalization-rule change is a
simple "gN:" bump, never a data migration. The identity axis is the Model
B ``form`` (movie|series) — the legacy ``type`` axis is gone (contract
#135); style tags do NOT participate in identity (the same film tagged
"anime" by one provider and plain by another is the same title).

Since #340 this module owns ONLY the matching/projection logic (title-
alias union-find, the year-soft rule, the audit log); every id-grammar
primitive — the ``g2:`` prefix, title normalization, per-item key
derivation — lives in ``wire_identity`` and is re-exported here for the
established import paths. The edge is strictly one-way: merge →
wire_identity.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import SearchResult
from .wire_identity import (
    effective_year as effective_year,  # noqa: PLC0414
)
from .wire_identity import (
    extract_year as extract_year,  # noqa: PLC0414
)
from .wire_identity import (
    group_key as group_key,  # noqa: PLC0414
)
from .wire_identity import (
    group_key_from as group_key_from,  # noqa: PLC0414
)
from .wire_identity import (
    item_group_key as item_group_key,  # noqa: PLC0414
)
from .wire_identity import (
    normalize_title as normalize_title,  # noqa: PLC0414
)
from .wire_identity import (
    title_aliases as title_aliases,  # noqa: PLC0414
)

log = logging.getLogger("cs_uk_api.merge")

#: Model B contract step (#135): the identity axis changed from the
#: legacy ``MediaType`` to ``form``, so keys are regenerated under a new
#: version. A style-tagged title now keys on its FORM (e.g. an anime
#: film and a plain film with the same name+year merge). The ``gN:``
#: prefix itself lives in ``wire_identity`` (spec #309) — a version
#: bump edits one file.


@dataclass(frozen=True)
class MergeGroup:
    """One merged title: a stable key plus all its source items."""

    key: str
    sources: tuple[SearchResult, ...] = field(compare=False)


def _effective_year(it: SearchResult) -> int | None:
    return effective_year(it.title, it.year)


def _years_match(a: int | None, b: int | None) -> bool:
    return a is None or b is None or a == b


def merge_results(items: Iterable[SearchResult]) -> list[MergeGroup]:
    """Union-find merge of listing items by alias+type with the year-soft rule.

    Every actual union of two previously-separate items is INFO-logged with
    both raw titles — the only after-the-fact wrong-merge detector.
    """
    items = list(items)
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        parent[ra] = rb
        log.info(
            "merge: '%s'[%s] + '%s'[%s]",
            items[a].title, items[a].id,
            items[b].title, items[b].id,
        )
        return True

    seen: dict[tuple[str, str], list[int]] = {}
    per_item_aliases = [title_aliases(it.title) for it in items]
    for i, it in enumerate(items):
        for alias in per_item_aliases[i]:
            for j in seen.get((alias, it.form), []):
                if _years_match(_effective_year(it), _effective_year(items[j])):
                    union(i, j)
            seen.setdefault((alias, it.form), []).append(i)

    buckets: dict[int, list[int]] = {}
    for i in range(len(items)):
        buckets.setdefault(find(i), []).append(i)

    groups: list[MergeGroup] = []
    for members in buckets.values():
        # Group key = min over the members' own stateless item keys:
        # order-independent, and every member can recompute its own key
        # from its listing data alone (issue #69, v3 spec §4.3). Members
        # with a known year are preferred: otherwise two different
        # year-soft groups (Дюна 2021 + yearless vs Дюна 1984 + yearless)
        # would both min to the yearless member's key (its digest hashes
        # below both year variants) and collide, silently dropping one
        # title at groupKey-dedup. Yearless singletons keep their own key.
        yearful = [m for m in members if _effective_year(items[m]) is not None]
        preferred = yearful if yearful else members
        key = min(item_group_key(items[m]) for m in preferred)
        groups.append(
            MergeGroup(
                key=key,
                sources=tuple(items[m] for m in members),
            )
        )
    return groups
