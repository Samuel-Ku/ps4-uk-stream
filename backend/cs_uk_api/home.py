"""Cross-provider Home aggregation (issue #70, spec #263).

Pure functions — no I/O. The route layer in ``main.py`` is responsible
for fetching per-provider listings and calling these helpers with the
pre-collected data; this module never touches ``PROVIDERS`` or HTTP.

The observable contracts:

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
    ordered list of ``HomeRow``: «Нещодавно додані: Фільми» →
    «Нещодавно додані: Серіали» (the form-split rows that REPLACE the
    retired «Новинки» rail, spec #263) → «Популярні зараз» → five
    type rows in the spec-mandated order. Rows that no provider
    contributed to are omitted.

  - ``build_genre_rows`` — the Netflix-style genre rails (spec #263):
    the top-N genres by profile-store coverage across the home
    snapshot become rows with Ukrainian labels, recency-ranked, ≤20
    each; genres below a coverage threshold are skipped.

Spec ordering invariant for the five type rows: ``movie, series,
anime, cartoon, dorama`` — the spec calls out five specific rows, in
that order. The split rows and «Популярні зараз» come first when
present.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet

from .merge import item_group_key, merge_results
from .models import HomeItem, HomeRow, SearchResult, Section
from .recommend import ItemProfile

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

#: The form-split «Нещодавно додані» rows (spec #263) that replace the
#: retired «Новинки» rail: one row per form, round-robin across the
#: providers' newest listings, topped up from the form-section page-1
#: items (the same data the type rows use) when under the cap.
_RECENT_ROWS: tuple[tuple[str, str, str], ...] = (
    ("movie", "Нещодавно додані: Фільми", "recent_movie"),
    ("series", "Нещодавно додані: Серіали", "recent_series"),
)

#: Genre rails (spec #263): the top-N genres by profile-store coverage
#: across the home snapshot; genres with fewer than ``GENRE_RAILS_MIN_ITEMS``
#: members are skipped; each row is capped at ``GENRE_RAILS_LIMIT``.
GENRE_RAILS_TOP_N = 6
GENRE_RAILS_MIN_ITEMS = 3
GENRE_RAILS_LIMIT = 20

#: «Нові серії» (spec #267 T3): the series the viewer watches whose
#: groups appear in the providers' newest listings — ranked by listing
#: position, capped at the standard row cap, omitted when the viewer
#: has no such series. Position 3 in the decided order (after the two
#: form-split recent rows, before «Популярні зараз»).
_NEW_EPISODES_ROW = ("Нові серії", "new_episodes")

#: «Нещодавно переглянуто» (spec #272): the most recently seen items —
#: active AND finished playback-history groups, most recent first,
#: capped at the standard row cap, omitted when there is no history.
#: Position 4 (after «Нові серії», before «Популярні зараз»).
_RECENTLY_WATCHED_ROW = ("Нещодавно переглянуто", "recently_watched")


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


def _item_matches_row(type_key: str, item: SearchResult) -> bool:
    """True when a listing item belongs in the row of the given kind.

    The by-type rows are populated from provider sections whose DECLARED
    axes key the row — but the items inside are what the upstream site
    actually filed there. A mis-filed card (e.g. a series whose bare URL
    an adapter classified as a film, 2026-08-14 eneyida drift) must not
    leak into the row as a junk card. Form rows check ``item.form``;
    style rows check the style set (an item without the style tag is
    still legitimate content from a style section — the row filter only
    guards against contradictory FORM on form rows).
    """
    if type_key in ("movie", "series"):
        return item.form == type_key
    return True


def build_home_rows(
    *,
    newest: Mapping[str, Sequence[SearchResult]],
    popular: Mapping[str, Sequence[SearchResult]],
    by_type: Mapping[str, Mapping[str, Sequence[SearchResult]]],
    newest_limit: int = 20,
    watched_series: AbstractSet[str] | None = None,
    history_groups: Sequence[str] | None = None,
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

    The first two rows are the form-split «Нещодавно додані» rows
    (spec #263): providers' newest listings are filtered by form and
    round-robin-deduped; a row under the cap is topped up from the
    form-section page-1 items (Netflix-style overlap accepted).

    Position 3 is «Нові серії» (spec #267 T3): when ``watched_series``
    (the group keys behind the viewer's playback history) is provided,
    the series-form newest listings that the viewer watches form a
    dedicated row ranked by listing position, omitted when empty. With
    ``watched_series=None`` the row never appears (the pure builder's
    default keeps every existing caller's output unchanged).

    Position 4 is «Нещодавно переглянуто» (spec #272): when
    ``history_groups`` (ordered group keys, most recent first — active
    AND finished, from the playback store) is provided, the matching
    groups become a row in that order, capped, omitted when empty. With
    ``history_groups=None`` the row never appears.

    Within each row, items are round-robin-deduped and capped at
    ``newest_limit`` (the spec's «Новинки» ceiling, reused for the
    split rows). The five type rows and the split rows all use the same
    cap.
    """
    rows: list[HomeRow] = []

    # «Нещодавно додані: Фільми» / «: Серіали» — the form-split rows
    # (spec #263) that REPLACE the retired «Новинки» rail. A row is
    # emitted iff at least one provider contributed a matching-form
    # item; items are round-robin-deduped and capped at ``newest_limit``.
    for form, label, row_type in _RECENT_ROWS:
        form_per_pid = {
            pid: [it for it in items if it.form == form] for pid, items in newest.items()
        }
        items_row = round_robin_dedup(form_per_pid, newest_limit)
        if len(items_row) < newest_limit:
            # Top up from the providers' form-section page-1 items (the
            # same data the type rows use) — overlap is accepted,
            # Netflix-style (spec #263). Round-robin across providers,
            # deduped by group key within the row.
            section_per_pid = by_type.get(form, {})
            filtered_per_pid = {
                pid: [it for it in items if _item_matches_row(form, it)]
                for pid, items in section_per_pid.items()
            }
            topup = round_robin_dedup(filtered_per_pid, newest_limit)
            existing = {it.group_key for it in items_row}
            for it in topup:
                if it.group_key in existing:
                    continue
                items_row.append(it)
                existing.add(it.group_key)
                if len(items_row) >= newest_limit:
                    break
        if items_row:
            rows.append(
                HomeRow(
                    title=label,
                    type=row_type,
                    items=aggregate_by_group_key(items_row),
                )
            )

    # «Нові серії» (spec #267 T3) — position 3, right after the two
    # form-split rows: the series-form NEWEST listings (no section
    # top-up — "recently added" is the point) whose group keys the
    # viewer watches, ranked by listing position, capped, omitted when
    # the viewer has no such series (or no watched set was provided).
    if watched_series is not None:
        watched_per_pid = {
            pid: [
                it
                for it in items
                if it.form == "series" and item_group_key(it) in watched_series
            ]
            for pid, items in newest.items()
        }
        watched_items = round_robin_dedup(watched_per_pid, newest_limit)
        if watched_items:
            label, row_type = _NEW_EPISODES_ROW
            rows.append(
                HomeRow(
                    title=label,
                    type=row_type,
                    items=aggregate_by_group_key(watched_items),
                )
            )

    # «Нещодавно переглянуто» (spec #272) — position 4: the history
    # groups (most recent first) resolved against EVERY collected
    # listing (newest + type sections — a finished item is likely NOT
    # in the newest page anymore), deduped, capped, omitted when empty
    # or when no history was provided.
    if history_groups is not None and history_groups:
        known: dict[str, SearchResult] = {}
        for listing in list(newest.values()) + [
            items_src for per_type in by_type.values() for items_src in per_type.values()
        ]:
            for cand in listing:
                known.setdefault(item_group_key(cand), cand)
        picked = [known[gk] for gk in history_groups if gk in known][:newest_limit]
        if picked:
            # Project SearchResult → HomeItem in the history's recency
            # order (NOT round-robin — the row IS the recency order).
            history_items = [
                HomeItem(
                    group_key=item_group_key(it),
                    title=it.title,
                    year=it.year,
                    poster=it.poster,
                    form=it.form,
                    styles=it.styles,
                    genres=list(it.genres),
                    providers=[it.provider],
                    member_keys=[item_group_key(it)],
                )
                for it in picked
            ]
            label, row_type = _RECENTLY_WATCHED_ROW
            rows.append(
                HomeRow(
                    title=label,
                    type=row_type,
                    items=aggregate_by_group_key(history_items),
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
        # Drop items whose FORM contradicts the row (upstream mis-filed
        # cards must not surface as junk in the wrong row).
        per_pid = {
            pid: [it for it in items if _item_matches_row(type_key, it)]
            for pid, items in per_pid.items()
        }
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


def _genre_slug(genre: str) -> str:
    """ASCII-stable view-id slug for a genre label (spec #263).

    ``genre:<slug>`` must be deterministic and stable for a fixed
    catalog vocabulary — the slug is the lowercased label with
    non-alphanumeric runs collapsed to ``-`` (Cyrillic letters are
    alphanumeric and survive, so «Драми» → ``драми``).
    """
    out = "".join(c if c.isalnum() else "-" for c in genre.strip().lower())
    return "-".join(part for part in out.split("-") if part)


def build_genre_rows(
    *,
    home_items: Sequence[HomeItem],
    profiles: Mapping[str, ItemProfile],
    top_n: int = GENRE_RAILS_TOP_N,
    min_items: int = GENRE_RAILS_MIN_ITEMS,
    limit: int = GENRE_RAILS_LIMIT,
) -> list[HomeRow]:
    """The Netflix-style genre rails (spec #263), from the profile store.

    Coverage is counted per home-snapshot group carrying a warm content
    profile (spec #252); the top ``top_n`` genres by coverage become
    rows, tied counts broken lexicographically for determinism. Genres
    with fewer than ``min_items`` members are skipped, and each row is
    capped at ``limit`` items.

    Within a row, members are the home groups whose profile carries the
    genre, recency-ranked: snapshot order IS the recency proxy (the
    form-split rows — fed by the providers' newest listings — come
    first), deduped by group key (a group can surface in several
    snapshot rows, Netflix-style overlap).

    Rail labels are Ukrainian — the content-page genre name, falling
    back to the listing card's original casing when the content page
    and the card disagree (``profile_from_content`` lowercases).
    """
    by_key = {it.group_key: it for it in home_items}
    coverage: dict[str, int] = {}
    label_by_genre: dict[str, str] = {}
    for it in home_items:
        prof = profiles.get(it.group_key)
        if prof is None:
            continue
        for genre in prof.genres:
            coverage[genre] = coverage.get(genre, 0) + 1
            label_by_genre.setdefault(genre, genre)
        # Prefer the listing card's original casing for the rail title.
        for raw in it.genres:
            norm = raw.strip().lower()
            if norm and norm in coverage:
                label_by_genre[norm] = raw.strip()

    top = sorted(coverage.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    rows: list[HomeRow] = []
    for genre, count in top:
        if count < min_items:
            continue
        members: list[HomeItem] = []
        seen: set[str] = set()
        for it in home_items:
            if len(members) >= limit:
                break
            prof = profiles.get(it.group_key)
            if prof is None or genre not in prof.genres:
                continue
            if it.group_key in seen:
                continue
            seen.add(it.group_key)
            members.append(by_key[it.group_key])
        rows.append(
            HomeRow(
                title=label_by_genre.get(genre, genre),
                type=f"genre:{_genre_slug(genre)}",
                items=members,
            )
        )
    return rows


__all__ = [
    "GENRE_RAILS_LIMIT",
    "GENRE_RAILS_MIN_ITEMS",
    "GENRE_RAILS_TOP_N",
    "aggregate_by_group_key",
    "build_genre_rows",
    "build_home_rows",
    "round_robin_dedup",
    "section_row_type",
]
