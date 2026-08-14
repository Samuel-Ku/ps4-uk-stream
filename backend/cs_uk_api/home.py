"""Cross-provider Home aggregation (issue #70).

Pure functions — no I/O. The route layer in ``main.py`` is responsible
for fetching per-provider listings and calling these helpers with the
pre-collected data; this module never touches ``PROVIDERS`` or HTTP.

The three observable contracts:

  - ``round_robin_dedup`` — interleave provider listings one item at a
    time (P1[0], P2[0], P1[1], P2[1], ...) and dedup via the shared
    ``merge_results`` core so the same title from two providers becomes
    ONE HomeItem carrying both providers in its ``providers`` list. The
    canonical ``group_key`` matches the one ``/api/search`` would
    compute for the same items (issue #71, yearful-preferred-min) — so
    a client can round-trip a card between Home and Search without
    translation.

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

from .merge import item_group_key, merge_results
from .models import HomeItem, HomeRow, SearchResult, Section

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


#: Cap on raw items collected during the round-robin walk before we
#: hand off to ``merge_results`` for dedup. The dedup is post-hoc (after
#: collection), so the cursor walk can't use group-count as an early-
#: exit signal — but a high-duplicate input (one title across many
#: providers, or a single provider re-emitting a listing many times)
#: must still terminate.
#:
#: Trade-off: ``limit * N`` lets the merge produce AT MOST
#: ``limit * N`` raw items and thus at most ``limit * N`` groups. After
#: merging, output is sliced to ``limit``. Under heavy duplicate
#: collapse (many raw items fold into one group), the output may fall
#: below ``limit`` — that is correct behaviour (one group is one group),
#: not a truncation bug. Under the realistic home row counts (well
#: under ``limit * N`` unique items per row), the bound is loose enough
#: that it never bites in practice.
_WALK_BUDGET_MULTIPLIER = 4


def round_robin_dedup(
    by_provider: Mapping[str, Sequence[SearchResult]],
    limit: int,
) -> list[HomeItem]:
    """Interleave provider listings round-robin; dedup via ``merge_results``.

    Algorithm: a per-provider cursor walks each provider's listings one
    item at a time, collecting raw items in first-seen order. The
    collection is bounded by ``limit * _WALK_BUDGET_MULTIPLIER`` (or by
    all cursors exhausting, whichever comes first). The collected
    stream is then passed to ``merge_results`` (issue #52 / #71) — the
    same merge core /api/search uses — which folds year-soft duplicates
    (e.g. "Дюна 2021" + "Дюна" with no year) into one ``MergeGroup``
    whose ``key`` is the yearful-preferred-min ``item_group_key``.

    Output order matches first-seen in the walk (the merge core
    preserves bucket order = first-seen order of each bucket's first
    member). Output is capped at ``limit`` items.

    Why not per-item ``item_group_key`` dedup (the old approach)?
    That key includes the raw year field in its digest, so a year-soft
    pair (yearful + yearless members) had two different per-item keys
    and produced TWO HomeItems with TWO different ``group_key`` fields
    — breaking the round-trip with /api/search and /api/content/{key}
    (HIGH issue #71 code review, "H1: cross-route groupKey
    divergence"). Delegating to ``merge_results`` makes the two routes
    share one identity.
    """
    walk_budget = max(limit * _WALK_BUDGET_MULTIPLIER, 0)
    collected: list[SearchResult] = []
    cursors: dict[str, int] = {pid: 0 for pid in by_provider}

    while len(collected) < walk_budget:
        progress = False
        for pid in by_provider:
            cursor = cursors[pid]
            listings = by_provider[pid]
            if cursor >= len(listings):
                continue
            collected.append(listings[cursor])
            cursors[pid] = cursor + 1
            progress = True
            if len(collected) >= walk_budget:
                break
        if not progress:
            break

    # Shared merge core — same identity /api/search surfaces (issue #71).
    groups = merge_results(collected)

    # Project MergeGroup → HomeItem, preserving the merge core's
    # first-seen order. Cap at ``limit``.
    items: list[HomeItem] = []
    for mg in groups:
        if len(items) >= limit:
            break
        sample = mg.sources[0]
        # Provider union, first-seen order (the merge core preserves
        # sources in bucket-order = first-seen order).
        providers = list(dict.fromkeys(s.provider for s in mg.sources))
        # Issue #89: every per-item group key that contributed to this
        # merged row. Deduped, first-seen-preserved order. The canonical
        # ``mg.key`` is the yearful-preferred-min of these — the client
        # matches a resume entry against ANY member key, not only
        # ``group_key``. Dedup keeps the payload bounded when one
        # provider surfaces multiple listings for the same group
        # (same title+type+year, different upstream ids).
        member_keys = list(dict.fromkeys(item_group_key(s) for s in mg.sources))
        items.append(
            HomeItem(
                group_key=mg.key,
                title=sample.title,
                year=sample.year,
                poster=sample.poster,
                # Model B (contract #135): first-seen-wins, like the
                # other canonical fields.
                form=sample.form,
                styles=sample.styles,
                genres=list(sample.genres),
                providers=providers,
                member_keys=member_keys,
            )
        )
    return items


def aggregate_by_group_key(items: Sequence[HomeItem]) -> list[HomeItem]:
    """Fold same-key items into one row carrying the union of providers.

    A safety net for callers that produced HomeItems by some path other
    than ``round_robin_dedup`` (which already folds same-key hits into
    one HomeItem with the union ``providers`` list). With the canonical
    ``round_robin_dedup`` upstream, this is a no-op pass — kept as a
    named seam so the row-level dedup story stays obvious.

    Order is first-seen-preserved. The first HomeItem's ``title``,
    ``year``, ``form``, and ``poster`` fields win (round-robin gives
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


def section_row_type(section: Section) -> str | None:
    """The home-row kind a section contributes to, from its Model B axes.

    Contract step #135: sections no longer carry the legacy ``type``
    axis — their home-row kind is derived from ``form`` + ``styles``:
    a non-empty style set wins (anime/cartoon/dorama row), else the
    form (movie/series row). A section with neither axis declared
    (``form=None``, ``styles=None`` — pass-any filter) contributes to
    no kind row. Deterministic for multi-style sets (lexicographic min).
    """
    if section.styles:
        return min(section.styles)
    if section.form is not None:
        return section.form
    return None


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
    "section_row_type",
]
