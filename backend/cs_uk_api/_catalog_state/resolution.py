"""Group-key + content resolution machinery (spec #309 T5).

The resolution half of the catalog state: a ``g2:`` group key resolves
to its ``{provider: SearchResult}`` map (``sources_cache``), a played
episode's wire id resolves to its merged group, and a group key resolves
to ONE provider's content detail — all through the same content /
blocklist / gated stores the native routes use. Also home here: the
subscription-gate sweep (``filter_gated_items``), the uakino readiness
gate, and the search-group registration that folds searched cards into
the shared resolution map (ticket #106).

Depends only on ``_stores`` (the caches/keys) and the shared service
modules — never on the snapshot / warm / search modules, so the package
imports form a DAG (resolution is imported by every other module).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import httpx
from fastapi import HTTPException

from .. import config as _config
from ..country import is_blocked_country
from ..health import TRACKER
from ..http_client import get_client
from ..merge import group_key_from
from ..models import (
    STATUS_DOWN,
    STATUS_WARMING,
    ContentResponse,
    Episode,
    ErrorResponse,
    SearchGroup,
    SearchResult,
    Season,
    Translation,
)
from ..providers import PROVIDERS
from ..providers.base import ProviderError
from ..uakino_browser import get_session
from ..wire_identity import is_group_key, provider_union, split_episode_tail, split_wire_id
from ._stores import (
    _SOURCES_KEY,
    blocklist_cache,
    content_cache,
    dub_for,
    gated_cache,
    remember_dub,
    sources_cache,
)

log = logging.getLogger("cs_uk_api.catalog_state.resolution")

#: Bound on how long an explicit uakino route waits for the browser
#: session to become ready before answering 503 ``warming`` (issue #193).
#: Distinct from ``UakinoSession.WARM_TIMEOUT_S`` (the bounded ``warm()``
#: call itself). Lives in the resolution module because
#: ``await_uakino_ready`` is shared by the native routes and the facade
#: search (ticket #106).
WARM_WAIT_S: float = 15.0

#: Cap on concurrent content-page fetches during the gate sweep, and
#: the sweep's own time budget. The budget deliberately lives OUTSIDE
#: the 12s search fan-out: a sweep timeout degrades to "keep the cards"
#: (stream()/content() still refuse gated items, so the promo clip
#: never plays) rather than failing the whole home build. Issue #168:
#: the inline budget is deliberately short — a cold sweep of every
#: can_gate listing can take 20-60s (animeon's content() does several
#: upstream hops per item), so tasks that outlive it are NOT cancelled:
#: load_home returns the fast partially-swept home and a background
#: task finishes the sweep and re-caches the cleaned home seconds
#: later. The 30-min home cache absorbs the re-cache.
_GATE_CHECK_CONCURRENCY = 24
GATE_CHECK_TIMEOUT_S = 12.0

#: Delay before the one detail-scrape retry (B23): providers flake once
#: under load (animeon ``unreachable``) and succeed on a second attempt.
CONTENT_RETRY_DELAY_S = 1.0


def episode_group_key(item_id: str) -> str | None:
    """The merged group key behind a played item id (spec #252).

    Movies report their ``g2:`` key; episodes report the provider-scoped
    wire id (``ufdub:dorama-408-...:s1e1``), whose ``provider:external``
    prefix identifies the merged group (reverse lookup, #214). The
    tail grammar lives in ``wire_identity`` (spec #309).
    """
    if is_group_key(item_id):
        return item_id
    split = split_episode_tail(item_id)
    if split is None:
        return None
    composite, _tail = split
    return group_key_for_external(composite)


def _gate_cache_key(item: SearchResult) -> str:
    """The shared ``content:{provider}:{external}`` key for a card."""
    _, _, external = item.id.partition(":")
    return f"content:{item.provider}:{external}"


async def filter_gated_items(
    items: Sequence[SearchResult],
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore | None = None,
) -> list[SearchResult]:
    """Drop subscription-gated sources from a listing (can_gate providers).

    Gating is only knowable from the item's content page — the site
    never marks listings — so every card of a ``can_gate`` provider is
    resolved once. Verdicts are cached (``gated_cache``, TTL > home
    cache) and the shared ``content_cache`` is populated with the same
    shape the detail routes use, so the sweep is free on every later
    rebuild and a resolved detail page never double-fetches.

    Only KNOWN-gated items are dropped; a transient upstream error
    keeps the card (dead providers are health-tracked elsewhere, and
    ``stream()`` still refuses gated items on its own).
    """
    todo = [it for it in items if it.provider in PROVIDERS and PROVIDERS[it.provider].can_gate]
    if not todo:
        return list(items)
    # Issue #168: callers may pass a shared semaphore so parallel sweep
    # tasks (one per listing/provider) don't each open their own
    # concurrency window — N tasks × 24 requests each throttled the
    # upstreams and blew the sweep budget, leaking gated cards into home.
    if sem is None:
        sem = asyncio.Semaphore(_GATE_CHECK_CONCURRENCY)

    async def check(item: SearchResult) -> bool:
        """True iff ``item`` is KNOWN gated (cached or freshly resolved)."""
        key = _gate_cache_key(item)
        cached = gated_cache.get(key)
        if cached is not None:
            return bool(cached)
        if content_cache.get(key) is not None:
            gated_cache.set(key, False, ttl_s=_config.SETTINGS.cache_content_s)
            return False
        async with sem:
            # Re-check under the lock: another sweep may have resolved
            # the same card while we waited for the semaphore.
            cached = gated_cache.get(key)
            if cached is not None:
                return bool(cached)
            if content_cache.get(key) is not None:
                gated_cache.set(key, False, ttl_s=_config.SETTINGS.cache_content_s)
                return False
            provider = PROVIDERS[item.provider]
            _, _, external = item.id.partition(":")
            try:
                resp = await provider.content(external, http)
            except ProviderError as e:
                if e.code == "gated":
                    gated_cache.set(key, True)
                    return True
                return False
            except Exception:  # noqa: BLE001
                return False
            # Mirror the detail routes' cache shape (group_key set
            # BEFORE caching, ADR-0003) + the blocklist check, so a
            # later detail cache-hit behaves identically.
            if _config.SETTINGS.block_russian and is_blocked_country(resp.country):
                blocklist_cache.set(key, True)
                return False
            resp.group_key = group_key_from(resp.title, resp.form, resp.year, resp.id)
            content_cache.set(key, resp)
            # A known-good verdict follows the CONTENT TTL (not the long
            # gated TTL): an un-gated title is re-checked when its
            # content cache expires, so it re-enters the catalog as soon
            # as the upstream publishes the real video.
            gated_cache.set(key, False, ttl_s=_config.SETTINGS.cache_content_s)
            return False

    tasks = {asyncio.create_task(check(it)): it for it in todo}
    gated_ids: set[str] = set()
    # Issue #168: run to COMPLETION, never cancel pending checks. The
    # load_home sweep bounds the whole pass with its own budget and
    # finishes stragglers in the background — but if THIS function
    # cancelled its checks on timeout it would return a partially-
    # filtered list and gated cards would leak into the cached home.
    done, _pending = await asyncio.wait(tasks.keys())
    for t in done:
        item = tasks[t]
        try:
            if t.result():
                gated_ids.add(item.id)
        except Exception:  # noqa: BLE001, S110
            pass
    return [it for it in items if it.id not in gated_ids]


def resolve_group(group_key: str) -> dict[str, SearchResult] | None:
    """Resolution for a ``g2:`` group key (ticket #101).

    Returns the ``provider -> SearchResult`` map for the group, or
    ``None`` when the key is absent (cold cache → the caller yields a
    404 "item unavailable", which Jellyfin clients tolerate).
    """
    per_provider: dict[str, dict[str, SearchResult]] = cast(
        dict[str, dict[str, SearchResult]], sources_cache.get(_SOURCES_KEY) or {}
    )
    return per_provider.get(group_key)


def group_key_for_external(composite: str) -> str | None:
    """Reverse group lookup: ``provider:external`` -> its ``g2:`` key.

    The playback reports carry the item id the client played — for an
    episode that is the provider-scoped wire id
    (``ufdub:dorama-408-...:s1e1``), whose ``provider:external`` prefix
    identifies the merged group (ticket #214). Built from the same
    ``sources_cache`` map ``resolve_group`` reads.

    Ticket #234: the episode prefix is NOT always the card's composite
    id. uakino's episode wire id carries only the bare numeric news id
    (``uakino:6268:e1``) while its search card id is the full
    ``uakino:anime-series:6268-narutto-1-sezon`` — the exact match
    misses and the resume rail would drop the episode. Fall back to a
    same-provider item-id match: the numeric segment (``6268``) appears
    as the ``{section}:{item_id}-{slug}`` shape of one of the group's
    cards.
    """
    per_provider: dict[str, dict[str, SearchResult]] = cast(
        dict[str, dict[str, SearchResult]], sources_cache.get(_SOURCES_KEY) or {}
    )
    for group_key, providers in per_provider.items():
        for result in providers.values():
            if result.id == composite:
                return group_key
    provider_id, _, item_seg = composite.partition(":")
    if not provider_id or not item_seg.isdigit():
        return None
    for group_key, providers in per_provider.items():
        for result in providers.values():
            if result.provider != provider_id:
                continue
            # ``{section}:{item_id}-{slug}`` (uakino) or ``{item_id}``
            # (animeon) — the numeric id is a segment boundary, never
            # part of a longer number.
            for part in result.id.split(":"):
                if part == item_seg or part.startswith(f"{item_seg}-"):
                    return group_key
    return None


def peek_group_content(group_key: str) -> ContentResponse | None:
    """Cache-only group content read (ticket #216).

    Returns the first-seen provider's ContentResponse when it is already
    cached — never fetches. The view-card Type is a cheap
    URL/section-guess while the content page is the truth, and the
    facade re-verifies a card against this peek; a cold group answers
    None exactly like ``resolve_group_content`` would on its first
    cache-miss, so callers degrade identically ("keep the card's own
    guess") without paying a fetch.
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        return None
    provider_id, item = next(iter(per_provider.items()))
    _, _, external_id = item.id.partition(":")
    cache_key = f"content:{provider_id}:{external_id}"
    if gated_cache.get(cache_key) is True:
        return None
    if blocklist_cache.get(cache_key) is not None:
        return None
    cached = content_cache.get(cache_key)
    if cached is None:
        return None
    return cast(ContentResponse, cached)


#: Single-flight guard for ``resolve_group_content`` (ticket #224): a
#: per-cache-key task so concurrent facade calls for the SAME item — the
#: app fires detail + seasons + episodes + playback in one tick — share
#: ONE upstream resolution and its verdict instead of N parallel
#: re-resolves (run8 re-hit animeon 4x in ~2s during a 502 storm; run7's
#: cold walk ballooned to 59 fetches across two retry rounds). The map
#: is bounded: each entry is removed once its task finishes.
_resolve_inflight: dict[str, asyncio.Task[ContentResponse | None]] = {}


async def resolve_group_content(group_key: str) -> ContentResponse | None:
    """Resolve a ``g2:`` group key to ONE provider's content detail.

    The facade's ticket #105 detail path: a ``g2:`` key maps to the same
    ``{provider: SearchResult}`` map the native ``/api/home`` populates;
    the first-seen provider (same order the home row's chip strip shows)
    is asked for its ContentResponse. The response comes from the SAME
    ``content_cache`` / ``blocklist_cache`` stores the native
    ``/api/content`` route uses — one TTL, one cache-key shape, one
    ``clear()`` — so a detail view and a native content call never cache
    two different shapes of the same title.

    Returns ``None`` when the key is absent (cold cache, D2's "item
    unavailable" 404) or the provider's ``content()`` raises (the facade
    degrades to 404; it never surfaces a 502 like a native route would —
    Jellyfin clients treat both as "skip this item").

    Single-flight (#224): concurrent callers for the same key wait for
    the first resolution and share its verdict (success OR failure) — a
    failed leader means the waiters return None without re-storming the
    upstream.
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        return None
    # First-seen provider; the SearchResult's composite id
    # (``provider:external``) is split back to the bare external id the
    # provider's content() expects (animeon rejects composite ids).
    provider_id, item = next(iter(per_provider.items()))
    _, _, external_id = item.id.partition(":")
    cache_key = f"content:{provider_id}:{external_id}"
    # Single-flight (#224): if a resolution for this key is already
    # in flight, share ITS verdict (success or failure) instead of
    # re-storming the upstream in parallel — the app fires detail +
    # seasons + episodes + playback for one item in the same tick, and
    # duplicate walks are what stretched run7's cold resolution and
    # multiplied run8's 502s. The map entry is created synchronously
    # (no await between the get and the set), so two callers in the
    # same event-loop tick can never both become leader.
    existing = _resolve_inflight.get(cache_key)
    if existing is not None:
        return await existing
    task = asyncio.create_task(
        _resolve_group_content_once(
            cache_key, group_key, provider_id, external_id,
        )
    )
    _resolve_inflight[cache_key] = task
    try:
        return await task
    finally:
        # Drop the guard once the burst is over (bounded memory); a
        # later, genuinely new request starts a fresh resolution.
        if _resolve_inflight.get(cache_key) is task:
            _resolve_inflight.pop(cache_key, None)


async def _resolve_group_content_once(
    cache_key: str,
    group_key: str,
    provider_id: str,
    external_id: str,
) -> ContentResponse | None:
    """One leader resolution for ``cache_key`` (single-flight, #224).

    Re-checks the caches under the guard: a previous burst may have
    landed a verdict between the caller's first check and the task
    starting. Mirrors the body of the pre-#224 ``resolve_group_content``.
    """
    if gated_cache.get(cache_key) is True:
        return None
    if blocklist_cache.get(cache_key) is not None:
        return None
    cached = content_cache.get(cache_key)
    if cached is not None:
        return cast(ContentResponse, cached)
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        return None
    http = get_client()
    resp = None
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = await provider.content(external_id, http)
            break
        except ProviderError as e:
            if e.code == "gated":
                # ADR-0002: a gated verdict never moves the health
                # tracker. Cache it so every later group-key call
                # short-circuits — the load_home sweep normally
                # populates `gated_cache` first, but a cold-cache g2:
                # detail call must not record a health-down here either
                # (#139).
                gated_cache.set(cache_key, True)
                return None
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt == 1:
            # B23: heavy titles flake once under load (animeon
            # ``unreachable``) and then succeed — retry before declaring
            # the item unavailable, so the runner warm / app detail don't
            # 404 a valid item. The health tracker only sees the verdict.
            log.warning(
                "group content failed provider=%s key=%s attempt 1/2 err=%s — retrying",
                provider_id, group_key, last_err,
            )
            await asyncio.sleep(CONTENT_RETRY_DELAY_S)
    if resp is None:
        log.warning(
            "group content failed provider=%s key=%s both attempts err=%s",
            provider_id, group_key, last_err,
        )
        TRACKER.record(provider_id, ok=False)
        return None
    TRACKER.record(provider_id, ok=True)
    if _config.SETTINGS.block_russian and is_blocked_country(resp.country):
        blocklist_cache.set(cache_key, True)
        log.info("blocked Russian content id=%s country=%s", cache_key, resp.country)
        return None
    resp.group_key = group_key
    content_cache.set(cache_key, resp)
    return resp


async def cached_provider_content(
    provider_id: str, external_id: str
) -> tuple[str, ContentResponse | None]:
    """The native content-by-id cache path (spec #309 T4), verdict first.

    Mirrors the pre-T4 ``main._content_by_id`` semantics exactly, with
    the cache key and verdict stores staying HERE (the implementation):

      - blocklisted → ("blocked", None)
      - gated verdict cached → ("gated", None)
      - cached detail → ("ok", resp)
      - else one upstream ``content()`` fetch, with the block-russian
        check and the stateless group-key derivation applied before
        caching → ("ok", resp) or ("blocked", None)

    Upstream ProviderError propagates unchanged — the native route's
    ``upstream_guard`` (via ``content_provider_error``) converts a
    provider-raised ``gated`` into its client-visible 404, exactly as
    before. Health recording stays with the guard too.
    """
    cache_key = f"content:{provider_id}:{external_id}"
    if blocklist_cache.get(cache_key) is not None:
        return "blocked", None
    if gated_cache.get(cache_key) is True:
        return "gated", None
    cached = content_cache.get(cache_key)
    if cached is not None:
        return "ok", cast(ContentResponse, cached)
    resp = await PROVIDERS[provider_id].content(external_id, get_client())
    if _config.SETTINGS.block_russian and is_blocked_country(resp.country):
        blocklist_cache.set(cache_key, True)
        return "blocked", None
    resp.group_key = group_key_from(resp.title, resp.form, resp.year, resp.id)
    content_cache.set(cache_key, resp)
    return "ok", resp


def is_hard_unavailable(group_key: str) -> bool:
    """True when a group key is DELIBERATELY unavailable — gated
    (subscription), blocklisted (Russian content), or unknown — as
    opposed to transiently unresolvable (an upstream blip).

    Ticket #224: the facade detail route uses this to decide between a
    degraded card answer (a known card whose live resolution failed
    should still render) and the D2 404 — a gated/blocked verdict must
    NEVER be masked by the degradation.
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        return True  # unknown group — the cold-cache 404 stands
    provider_id, item = next(iter(per_provider.items()))
    _, _, external_id = item.id.partition(":")
    cache_key = f"content:{provider_id}:{external_id}"
    if gated_cache.get(cache_key) is True:
        return True
    return blocklist_cache.get(cache_key) is not None


def should_skip_uakino_in_fanout() -> bool:
    """True when uakino must be dropped from a ``provider=all`` fan-out.

    Dropped while down (startup marker or sliding-window) or while the
    browser session has not yet become ready (issue #193). Shared by the
    native ``/api/search`` route and the facade search (ticket #106).
    """
    if "uakino" not in PROVIDERS:
        return False
    if TRACKER.status("uakino") == STATUS_DOWN:
        return True
    return not get_session().ready_event.is_set()


async def await_uakino_ready() -> None:
    """Gate an explicit uakino route on the browser session being ready.

    A deterministic startup marker short-circuits with 502 — the session
    is dead for the process lifetime, so waiting is pointless. Otherwise
    wait for ``ready_event`` bounded by ``WARM_WAIT_S``; a cold session
    still warming answers 503 ``warming`` so the client can back off and
    retry (issue #193/#196).
    """
    marker = TRACKER.startup_marker("uakino")
    if marker is not None:
        raise HTTPException(
            502,
            detail=ErrorResponse(
                error="upstream_unreachable", message=f"uakino {marker}"
            ).model_dump(),
        )
    session = get_session()
    try:
        await asyncio.wait_for(session.ready_event.wait(), timeout=WARM_WAIT_S)
    except TimeoutError:
        raise HTTPException(
            503,
            detail=ErrorResponse(
                error=STATUS_WARMING,
                message="uakino session is still warming; retry shortly",
            ).model_dump(),
        ) from None


def register_search_groups(groups: Sequence[SearchGroup]) -> None:
    """Fold search-result groups into the shared group-key resolution map.

    Ticket #106: the Jellyfin facade's search must open in the #105
    detail surface, and ``resolve_group_content`` only knows keys the
    resolution map (``sources_cache``) carries. A search covers the whole
    catalog — most results are NOT in the 30-min home snapshot — so the
    facade registers each merged group's provider union under EVERY
    member key it holds, the same shape ``_build_sources_map`` stores for
    home rows. First-seen provider order is preserved; providers the map
    already knows are left untouched. The whole map keeps the home
    cache's TTL (ADR-0003), so a registered key expires with the next
    snapshot refresh.
    """
    if not groups:
        return
    existing: dict[str, dict[str, SearchResult]] = cast(
        dict[str, dict[str, SearchResult]], sources_cache.get(_SOURCES_KEY) or {}
    )
    changed = False
    for g in groups:
        union = provider_union(g.sources)
        keys = g.member_keys or [g.group_key]
        for key in keys:
            current = existing.get(key)
            if current is None:
                existing[key] = dict(union)
                changed = True
            else:
                merged = dict(current)
                for pid, item in union.items():
                    merged.setdefault(pid, item)
                if merged != current:
                    existing[key] = merged
                    changed = True
    if changed:
        # Re-set refreshes the TTL on the whole map (ADR-0003): a search
        # extends the snapshot's life, never shortens it.
        sources_cache.set(_SOURCES_KEY, existing)


# ---------------------------------------------------------------------------
# Viewer-state derivations (#347) — folded off the facade router.
#
# The DOMAIN half of the facade's playback routes: which translations a
# playable item offers and in what order the picker's candidates rank,
# what a dub pick is remembered as, and where a played episode sits in
# its season. The wire half — MediaSourceInfo assembly, the
# ``item::translation`` source-id codec, HTTP body parsing — stays in
# ``jellyfin/router.py``; these accessors hand it typed answers instead
# of store lookups.
# ---------------------------------------------------------------------------


#: Multi-source cap (spec #276): at most 8 translations surface as
#: picker candidates — providers with many dubs don't bloat the response.
MAX_TRANSLATION_SOURCES = 8


@dataclass(frozen=True)
class PlaybackEpisodePairing:
    """Where a played episode wire id sits in its series (#347).

    Everything the facade needs to shape the episode's DTO (and its
    next sibling's): the owning group/provider/title plus the matched
    Season/Episode domain models. ``next_episode`` is None on a season
    finale. None answers (no pairing) are unresolvable ids — cold group,
    non-episode id, unknown key.
    """

    group_key: str
    provider_id: str
    series_title: str
    season: Season
    episode: Episode
    next_episode: Episode | None


def _translation_label(
    translations: Sequence[Translation], translation_id: str
) -> str | None:
    """The label for a translation id, or None (spec #276: the picker
    renders labels; the memory stores labels)."""
    for t in translations:
        if t.id == translation_id:
            return t.label
    return None


def ordered_translation_candidates(
    translations: Sequence[Translation],
    *,
    remembered: str | None = None,
    picked_index: int | None = None,
) -> list[Translation]:
    """The picker's candidate order for one PlaybackInfo response
    (spec #276).

    Dedupe by label first (first provider wins), capped at
    ``MAX_TRANSLATION_SOURCES`` during collection; THEN rank: the source
    matching the request's echoed ``picked_index`` (1-based position in
    the deduped list — the switch path) goes first when present,
    otherwise the ``remembered`` dub label goes first so a replay of the
    series defaults to it, otherwise provider order stands. The sort is
    stable, so ties keep the original order.
    """
    deduped: list[Translation] = []
    seen_labels: set[str] = set()
    for t in translations:
        if t.label in seen_labels:
            continue
        seen_labels.add(t.label)
        deduped.append(t)
        if len(deduped) >= MAX_TRANSLATION_SOURCES:
            break

    def rank(t: Translation, idx: int) -> tuple[int, int]:
        # (order group, stable tiebreak): picked/remembered first.
        if picked_index is not None and idx == picked_index:
            return (0, idx)
        if picked_index is None and remembered is not None and t.label == remembered:
            return (0, idx)
        return (1, idx)

    ordered = sorted(enumerate(deduped, start=1), key=lambda pair: rank(pair[1], pair[0]))
    return [t for _, t in ordered]


async def playback_translations(item_id: str) -> tuple[list[Translation], str | None]:
    """(candidate translations, remembered dub label) for a playable item
    (spec #276). The translation list comes from the episode blob (no
    network) or the content page (already fetched); the remembered label
    comes from the user-state dub memory keyed by the SERIES group key.
    Movies are never remembered (v3 decision) — their group key is the
    memory key only for episodes.
    """
    remembered: str | None = None
    if is_group_key(item_id):
        # Movie: content translations; no dub memory.
        content = await resolve_group_content(item_id)
        if content is None:
            return [], None
        return list(content.translations), None

    # Episode wire id: resolve the merged group → the content page → the
    # episode's own translations (fall back to the content's).
    group_key = episode_group_key(item_id)
    if group_key is None:
        return [], None
    content = await resolve_group_content(group_key)
    if content is None:
        return [], None
    # The composite split is wire_identity's grammar; the prefix strip
    # below has no canonical helper and keeps its literal form so the
    # episode matching stays byte-identical (#347).
    provider_id, _ = split_wire_id(content.id)
    prefix = f"{provider_id}:"
    episode_tail = item_id[len(prefix) :] if item_id.startswith(prefix) else item_id
    translations = list(content.translations)
    if content.seasons:
        for season in content.seasons:
            for ep in season.episodes:
                if ep.id == episode_tail or ep.id == item_id:
                    if ep.translations:
                        translations = list(ep.translations)
                    break
    remembered = dub_for(group_key)
    return translations, remembered


async def record_dub_choice(item_id: str, translation_id: str) -> None:
    """Record the viewer's dub pick as per-series memory (spec #276).

    The series group key is resolved from the played item (episode wire
    ids via the reverse lookup; movies are skipped — v3 decision). The
    label is what PlaybackInfo reorders by, so the id is translated
    through the SAME translation list the picker rendered (the episode's
    own dubs, falling back to the content's) before storing. A
    best-effort record: resolution failures just skip the memory.
    """
    if is_group_key(item_id):
        return
    translations, _ = await playback_translations(item_id)
    label = _translation_label(translations, translation_id)
    if label is None:
        return
    group_key = episode_group_key(item_id)
    if group_key is not None:
        remember_dub(group_key, label)


def _played_episode_wire(provider_id: str, episode_id: str) -> str:
    """The provider-scoped wire id an episode DTO carries: the provider
    prefix unless the episode id already embeds it (the same rule the
    facade builds its season-rail ids with — providers are not uniform,
    D2). Kept here because the match must agree with what the client
    plays, while the state package cannot import the facade."""
    prefix = f"{provider_id}:"
    return episode_id if episode_id.startswith(prefix) else prefix + episode_id


async def playback_episode_pair(item_id: str) -> PlaybackEpisodePairing | None:
    """Locate a played episode wire id in its series (#347).

    The ``provider:external`` prefix identifies the merged group
    (reverse lookup, #214), whose season hierarchy holds the episode;
    the next sibling is the same season's following entry. Returns None
    for a non-episode id or an unresolvable group (cold cache / gated
    item) — the caller degrades exactly as before.
    """
    group_key = episode_group_key(item_id)
    if group_key is None:
        return None
    content = await resolve_group_content(group_key)
    if content is None or not content.seasons:
        return None
    provider_id, _ = split_wire_id(content.id)
    for season in content.seasons:
        episodes = season.episodes
        for idx, ep in enumerate(episodes):
            if _played_episode_wire(provider_id, ep.id) != item_id:
                continue
            nxt = episodes[idx + 1] if idx + 1 < len(episodes) else None
            return PlaybackEpisodePairing(
                group_key=group_key,
                provider_id=provider_id,
                series_title=content.title,
                season=season,
                episode=ep,
                next_episode=nxt,
            )
    return None
