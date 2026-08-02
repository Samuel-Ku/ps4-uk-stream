"""Cross-provider Home aggregation (issue #70).

Pure functions — no I/O. The route layer in ``main.py`` is responsible
for fetching per-provider listings and calling these helpers with the
pre-collected data; this module never touches ``PROVIDERS`` or HTTP.

The three observable contracts:

  - ``round_robin_dedup`` — interleave provider listings one item at a
    time (P1[0], P2[0], P1[1], P2[1], ...) and dedup by groupKey so the
    same title from two providers becomes ONE HomeItem carrying both
    providers in its ``providers`` list.

  - ``aggregate_by_group_key`` — the second-pass pass that takes the
    round-robin output and folds multiple occurrences of the same
    groupKey into one row (carrying the union of providers). Currently
    a no-op when ``round_robin_dedup`` already returns unique groupKeys
    within a row — kept as a named seam so the row-level dedup story
    stays obvious.

  - ``build_home_rows`` — the orchestrator. Takes three pre-collected
    mappings (``newest``, ``popular``, ``by_type``) and produces the
    ordered list of ``HomeRow``: «Новинки» → «Популярні зараз» → five
    type rows in the spec-mandated order. Rows that no provider
    contributed to are omitted.

Spec ordering invariant for the five type rows: ``movie, series,
anime, cartoon, dorama`` — the spec calls out five specific rows, in
that order. «Новинки» and «Популярні зараз» come first when present.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .merge import item_group_key
from .models import HomeItem, HomeRow, SearchResult

#: Five-row type-row order, per the issue #70 spec. Anything else in
#: ``by_type`` is ignored (defensive — the route layer only buckets
#: sections into these five).
_TYPE_ORDER: tuple[tuple[str, str], ...] = (
    ("movie", "Фільми"),
    ("series", "Серіали"),
    ("anime", "Аніме"),
    ("cartoon", "Мультфільми"),
    ("dorama", "Дорами"),
)


def round_robin_dedup(
    by_provider: Mapping[str, Sequence[SearchResult]],
    limit: int,
) -> list[HomeItem]:
    """Interleave provider listings round-robin; dedup by groupKey.

    Algorithm: a per-provider cursor walks each provider's listings one
    item at a time. Per round, every still-alive provider contributes
    its NEXT item. A first-seen item opens a new HomeItem keyed on its
    groupKey; a repeat hit appends the provider to that item's
    ``providers`` list (still consuming the cursor slot, so the
    round-robin pace stays governed by total listings per provider,
    not by unique keys).

    Output is capped at ``limit`` items; iteration order across
    providers matches the iteration order of ``by_provider`` (Python
    3.7+ ``dict`` preserves insertion order).
    """
    seen_order: list[str] = []
    providers_by_key: dict[str, list[str]] = {}
    sample_by_key: dict[str, SearchResult] = {}
    cursors: dict[str, int] = {pid: 0 for pid in by_provider}

    while len(seen_order) < limit:
        progress = False
        for pid in by_provider:
            cursor = cursors[pid]
            listings = by_provider[pid]
            if cursor >= len(listings):
                continue
            # Each provider contributes AT MOST one round per outer
            # iteration, even if its cursor advances past a duplicate.
            raw = listings[cursor]
            cursors[pid] = cursor + 1
            gk = item_group_key(raw)
            if gk not in providers_by_key:
                providers_by_key[gk] = [pid]
                sample_by_key[gk] = raw
                seen_order.append(gk)
            elif pid not in providers_by_key[gk]:
                # Same provider surfacing the same groupKey from two
                # listings (rare but possible — e.g. a "newest" section
                # re-emitting a "popular" hit) must not duplicate the
                # pid in the resulting ``providers`` union.
                providers_by_key[gk].append(pid)
            progress = True
            if len(seen_order) >= limit:
                return _materialize(seen_order, providers_by_key, sample_by_key)
        if not progress:
            break

    return _materialize(seen_order, providers_by_key, sample_by_key)


def _materialize(
    seen_order: list[str],
    providers_by_key: dict[str, list[str]],
    sample_by_key: dict[str, SearchResult],
) -> list[HomeItem]:
    """Convert the dedup-state maps into the HomeItem list.

    Order matches first-seen (which is round-robin position: the first
    provider to surface a groupKey anchors the row's title/year/poster
    fields). Title/year/poster from subsequent providers are dropped —
    the spec doesn't preserve per-provider field-level attribution
    beyond the providers list itself.
    """
    return [
        HomeItem(
            group_key=gk,
            title=sample_by_key[gk].title,
            year=sample_by_key[gk].year,
            type=sample_by_key[gk].type,
            poster=sample_by_key[gk].poster,
            providers=providers_by_key[gk],
        )
        for gk in seen_order
    ]


def aggregate_by_group_key(items: Sequence[HomeItem]) -> list[HomeItem]:
    """Fold same-key items into one row carrying the union of providers.

    A safety net for callers that produced HomeItems by some path other
    than ``round_robin_dedup`` (which already folds same-key hits into
    one HomeItem with the union ``providers`` list). With the canonical
    ``round_robin_dedup`` upstream, this is a no-op pass — kept as a
    named seam so the row-level dedup story stays obvious.

    Order is first-seen-preserved. The first HomeItem's ``title``,
    ``year``, ``type``, and ``poster`` fields win (round-robin gives
    them all the same value modulo data noise, so the choice is
    arbitrary).
    """
    by_key: dict[str, HomeItem] = {}
    order: list[str] = []
    for it in items:
        existing = by_key.get(it.group_key)
        if existing is None:
            by_key[it.group_key] = it.model_copy(deep=True)
            order.append(it.group_key)
        else:
            for pid in it.providers:
                if pid not in existing.providers:
                    existing.providers.append(pid)
    return [by_key[k] for k in order]


def build_home_rows(
    *,
    newest: Mapping[str, Sequence[SearchResult]],
    popular: Mapping[str, Sequence[SearchResult]],
    by_type: Mapping[str, Mapping[str, Sequence[SearchResult]]],
    newest_limit: int = 20,
) -> list[HomeRow]:
    """Assemble the HomeResponse rows from pre-collected per-provider data.

    ``newest`` and ``popular`` are ``provider -> listings`` mappings; the
    key set is the set of providers that contributed to that row. Empty
    mapping or empty listings → the row is omitted (spec: «Популярні
    зараз» present only when animeon provides it).

    ``by_type`` is a nested mapping ``type -> provider -> listings``.
    Type keys outside the spec's five are dropped silently — defensive
    against future ``Section.type`` additions that don't map to a home
    row.

    Within each row, items are round-robin-deduped and capped at
    ``newest_limit`` (the spec's «Новинки» ceiling). The five type rows
    and «Новинки» all use the same cap.
    """
    rows: list[HomeRow] = []

    # «Новинки» — emitted iff at least one provider contributed items.
    if any(newest.values()):
        deduped = round_robin_dedup(newest, newest_limit)
        rows.append(
            HomeRow(
                title="Новинки",
                type="newest",
                items=aggregate_by_group_key(deduped),
            )
        )

    # «Популярні зараз» — emitted iff animeon (or whatever provider
    # holds the "popular" role) returned at least one item. Empty list
    # from the popular provider is treated identically to no popular
    # data: the row is omitted (AC: present only when animeon
    # provides it).
    if any(popular.values()):
        deduped = round_robin_dedup(popular, newest_limit)
        rows.append(
            HomeRow(
                title="Популярні зараз",
                type="popular",
                items=aggregate_by_group_key(deduped),
            )
        )

    # Five type rows, in the spec's mandated order. A type with no
    # contributing providers is dropped from the response (it would
    # be an empty row, which would be worse than absence for the
    # client).
    for type_key, label in _TYPE_ORDER:
        per_pid = by_type.get(type_key, {})
        if not any(per_pid.values()):
            continue
        deduped = round_robin_dedup(per_pid, newest_limit)
        if not deduped:
            continue
        rows.append(
            HomeRow(
                title=label,
                type=type_key,
                items=aggregate_by_group_key(deduped),
            )
        )

    return rows


__all__ = [
    "aggregate_by_group_key",
    "build_home_rows",
    "round_robin_dedup",
]
