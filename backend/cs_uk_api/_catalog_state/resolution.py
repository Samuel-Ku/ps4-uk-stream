"""Group-key + content resolution machinery (spec #309 T5).

The resolution half of the catalog state: a ``g2:`` group key resolves
to its ``{provider: SearchResult}`` map, a played
episode's wire id resolves to its merged group, and a group key resolves
to ONE MEMBER's content detail — walking the group's other members when
that member refuses in a way the group can repair (the same walk the
play seam runs, Q45/Q47) — all through the same content / blocklist /
gated stores the native routes use. Also home here: the subscription-gate
sweep (``filter_gated_items``), the uakino readiness gate, and the
search-group registration that folds searched cards into the shared
resolution map (ticket #106).

Depends only on ``_stores`` (the caches/keys) and the shared service
modules — never on the snapshot / warm / search modules, so the package
imports form a DAG (resolution is imported by every other module).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence

import httpx
from fastapi import HTTPException

from .. import config as _config
from ..country import is_blocked_country
from ..health import LANE_VERDICT_CODES, TRACKER, record_verdict
from ..http_client import get_client
from ..merge import group_key_from
from ..models import (
    STATUS_DOWN,
    STATUS_WARMING,
    ContentResponse,
    ErrorResponse,
    SearchGroup,
    SearchResult,
)
from ..providers import PROVIDERS
from ..providers.base import BaseProvider, ProviderError
from ..uakino_browser import get_session
from ..wire_identity import is_group_key, split_episode_tail
from ._stores import (
    blocklist_cache,
    catalog_state,
    content_cache,
    gated_cache,
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
    entry = catalog_state().sources().get(group_key)
    if entry is None:
        # Absent means a cold cache: no snapshot row carries the key and no
        # search ever registered it. Since ADR-0010 a rebuild is no longer
        # a way to lose an actionable key — registrations ride the apply
        # step forward and lapse on the search TTL (issue #420 was the
        # version of this comment that had to say otherwise). Debug-level,
        # because an unknown key is routine and the callers already log
        # their own 404 verdict.
        log.debug("group resolution miss key=%s (cold cache)", group_key)
    return entry


def group_key_for_external(composite: str) -> str | None:
    """Reverse group lookup: ``provider:external`` -> its ``g2:`` key.

    The playback reports carry the item id the client played — for an
    episode that is the provider-scoped wire id
    (``ufdub:dorama-408-...:s1e1``), whose ``provider:external`` prefix
    identifies the merged group (ticket #214). Built from the same
    resolution map ``resolve_group`` reads.

    Ticket #234: the episode prefix is NOT always the card's composite
    id. uakino's episode wire id carries only the bare numeric news id
    (``uakino:6268:e1``) while its search card id is the full
    ``uakino:anime-series:6268-narutto-1-sezon`` — the exact match
    misses and the resume rail would drop the episode. Fall back to a
    same-provider item-id match: the numeric segment (``6268``) appears
    as the ``{section}:{item_id}-{slug}`` shape of one of the group's
    cards.
    """
    per_provider = catalog_state().sources()
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


# ------------------------------------------------------------- member walk
#
# A group key is carried by SEVERAL providers, and one member's refusal is
# not the title's verdict: the walk asks the group's next member when the
# refusal is one the group can repair. Two seams run it — the play seam
# (``jellyfin.delivery.resolve_stream``, for the bytes) and content
# resolution (``resolve_group_content``, for the title's own page) — so the
# vocabulary lives HERE, next to the group map both of them read, and
# neither stage can drift away from the other (Q45/Q47).

#: The client's own TRACK verdicts: the dub it picked is not on THIS
#: member — ``invalid_translation`` from the episode-translation gate
#: (``play_request``), ``translation_missing`` from a provider that has no
#: such voice (uakino, animeon). Both are item verdicts in the health
#: taxonomy (``ITEM_VERDICT_CODES``), and both are per-MEMBER knowledge by
#: construction: every member of a group lists its OWN dubs, so "this
#: member does not carry that track" says nothing about the title.
#: The set is a literal rather than derived from the taxonomy, because
#: only these two codes mean "wrong member, same title": a third
#: translation verdict belongs HERE as well.
TRANSLATION_VERDICT_CODES = frozenset({"invalid_translation", "translation_missing"})

#: Refusals that mean "ask the group's next member": a lane fault (the
#: member could not answer), ``not_found`` (it answered with no playable
#: file), or a translation verdict (this member does not carry the track
#: the client pinned — ``TRANSLATION_VERDICT_CODES``). The last one is a
#: walk for the same reason as the others: the pick is per-member
#: knowledge, so the member that HAS the pinned dub is asked with the very
#: id the client picked instead of the client getting a 404 for a title
#: that plays. Derived from the health taxonomy rather than copied, so a
#: new lane code becomes walkable the moment it lands in
#: ``LANE_VERDICT_CODES`` — the classification keeps its one owner and
#: the walk cannot drift away from it. Deliberately NOT walkable:
#: ``gated`` — the title is deliberately unavailable, and another member's
#: answer is not a repair for what the client asked. A codeless crash
#: (``None``) IS walkable: it is recorded as a lane fault, so the walk
#: treats it like one.
WALKABLE_VERDICT_CODES = frozenset(
    LANE_VERDICT_CODES | TRANSLATION_VERDICT_CODES | {"not_found"}
)

#: How many of a group's members one request may walk. Upstream
#: etiquette, the #305 lesson applied to both seams: a work carried by
#: eight providers must not spend eight upstream calls — each up to the
#: client timeout — to answer one request. The walk follows the group's
#: first-seen order (the order the detail page lists, so the member the
#: viewer sees first is the one tried first) and stops here.
MAX_MEMBER_ATTEMPTS = 3


def member_attempts(
    sources: Sequence[SearchResult],
    *,
    lead: str | None = None,
    registered_only: bool = True,
) -> tuple[tuple[str, str], ...]:
    """A group's members as ``(provider, bare external id)`` attempts.

    First-seen order, so the walk degrades from the member the viewer saw
    toward the rest of the union; at most one attempt per provider, never
    more than ``MAX_MEMBER_ATTEMPTS``, and a card whose bare id is empty is
    dropped — the remaining attempts (or the final refusal) still record
    this request's outcome.

    ``lead`` moves one member to the FRONT when the group carries it: the
    member whose content page answered (``group_content_member``), so the
    play seam asks the member whose detail page it just read — and whose
    translation ids the client is holding — before it spends an upstream
    call on a member it already knows is dead.

    ``registered_only`` (the play seam's rule) drops a member whose provider
    is no longer registered: ``stream()`` needs the provider object itself.
    Content resolution and the cache-only readers pass False — a member's
    page may still be CACHED, and a cached page answers without the provider
    — and the content walk skips a member it cannot fetch, asking the next.
    """
    attempts: list[tuple[str, str]] = []
    asked: set[str] = set()
    for result in sources:
        if result.provider in asked:
            continue
        if registered_only and result.provider not in PROVIDERS:
            continue
        external_id = result.id.partition(":")[2]
        if not external_id:
            continue
        asked.add(result.provider)
        attempts.append((result.provider, external_id))
        if len(attempts) == MAX_MEMBER_ATTEMPTS:
            break
    if lead is None:
        return tuple(attempts)
    leading = [a for a in attempts if a[0] == lead]
    if not leading:
        return tuple(attempts)
    return tuple(leading + [a for a in attempts if a[0] != lead])


def _member_attempts_of(
    per_provider: Mapping[str, SearchResult] | None, *, lead: str | None = None
) -> tuple[tuple[str, str], ...]:
    """``member_attempts`` over an already-resolved group map (() when None).

    The CONTENT side's view of the group: unregistered members stay in (a
    cached page of theirs still answers), which is why the walk's fetch step
    checks for a provider before calling one.
    """
    if not per_provider:
        return ()
    return member_attempts(
        tuple(per_provider.values()), lead=lead, registered_only=False
    )


def _member_cached(cache_key: str) -> tuple[str, ContentResponse | None]:
    """Cache-only verdict for one member's content key.

    ``("hit", page)`` — this member's page is already resolved;
    ``("gated", None)`` / ``("blocked", None)`` — a DELIBERATE verdict
    (subscription gate / Russian content), the one answer the walk stops
    on rather than repairing with another member; ``("miss", None)`` —
    nothing known yet, a fetch may answer. Both deliberate kinds mean "not
    for this client", so their order among themselves is immaterial.
    """
    if gated_cache.get(cache_key) is True:
        return "gated", None
    if blocklist_cache.get(cache_key) is not None:
        return "blocked", None
    cached = content_cache.get(cache_key)
    if cached is not None:
        return "hit", cached
    return "miss", None


def _cached_group_page(group_key: str) -> tuple[str, ContentResponse] | None:
    """``(member, page)`` already resolved for a group, or None.

    The cache-only twin of the walk's per-member ladder, read in the walk's
    OWN order (same attempts, same cap, same deliberate verdicts) so it can
    never disagree with what ``resolve_group_content`` answered: the first
    member holding a page wins, a member holding a deliberate verdict
    answers nothing (the walk would have stopped there), and a cold group
    answers None. Never fetches.
    """
    for provider_id, external_id in _member_attempts_of(resolve_group(group_key)):
        kind, cached = _member_cached(f"content:{provider_id}:{external_id}")
        if kind == "hit" and cached is not None:
            return provider_id, cached
        if kind != "miss":
            return None
    return None


def peek_group_content(group_key: str) -> ContentResponse | None:
    """Cache-only group content read (ticket #216).

    Returns the member page already resolved for the group — never
    fetches. The view-card Type is a cheap URL/section-guess while the
    content page is the truth, and the facade re-verifies a card against
    this peek; a cold group answers None exactly like
    ``resolve_group_content`` would on its first cache-miss, so callers
    degrade identically ("keep the card's own guess") without paying a
    fetch.

    Q47: the member that answered is read the way the WALK landed — a
    group whose first member's page is dead reads the page of the member
    that served it, not the dead one's absence.
    """
    pair = _cached_group_page(group_key)
    return None if pair is None else pair[1]


def group_content_member(group_key: str) -> str | None:
    """The member whose page IS this group's content, or None.

    The play seam's read of the walk's outcome (Q47): the member whose
    detail page answered is asked for the stream FIRST. It is the member
    whose translations (and translation IDS) the client is holding — the
    facade built the play request from that page — so asking it before a
    member whose page we could not even read is both the cheaper and the
    correcter order.
    """
    pair = _cached_group_page(group_key)
    return None if pair is None else pair[0]


#: Single-flight guard for ``resolve_group_content`` (ticket #224): a
#: per-GROUP-KEY task so concurrent facade calls for the SAME item — the
#: app fires detail + seasons + episodes + playback in one tick — share
#: ONE upstream resolution and its verdict instead of N parallel
#: re-resolves (run8 re-hit animeon 4x in ~2s during a 502 storm; run7's
#: cold walk ballooned to 59 fetches across two retry rounds). The map
#: is bounded: each entry is removed once its task finishes. Keyed by the
#: group, not by a member's cache key: the walk may end on a member other
#: than the first, and two callers asking for one group must share the
#: whole walk (Q47).
_resolve_inflight: dict[str, asyncio.Task[ContentResponse | None]] = {}


async def resolve_group_content(group_key: str) -> ContentResponse | None:
    """Resolve a ``g2:`` group key to ONE member's content detail.

    The facade's ticket #105 detail path: a ``g2:`` key maps to the same
    ``{provider: SearchResult}`` map the native ``/api/home`` populates;
    the group's members are asked in first-seen order (the order the home
    row's chip strip shows, so the member the viewer saw first is tried
    first) and the walk continues past a member the GROUP can repair —
    ``WALKABLE_VERDICT_CODES``: a lane fault, ``not_found`` — exactly the
    discipline the play seam follows, so a title whose first member's
    detail page is dead still resolves (and still plays) from another
    member. A deliberate verdict (``gated``) ends the walk: asking the
    group again is not a repair for what the client asked. The walkable
    set also carries the translation verdicts, which only the PLAY seam
    can meet: a content fetch answers about a TITLE, never about a track,
    so a page never refuses in that vocabulary. At most ``MAX_MEMBER_ATTEMPTS``
    members are asked, and one already resolved (by any member the walk
    would reach) answers without any upstream call at all. The response
    comes from the SAME ``content_cache`` / ``blocklist_cache`` stores the
    native ``/api/content`` route uses — one TTL, one cache-key shape per
    MEMBER, one ``clear()`` — so a detail view and a native content call
    never cache two different shapes of the same title.

    Returns ``None`` when the key is absent (cold cache, D2's "item
    unavailable" 404) or every walked member's ``content()`` raises (the
    facade degrades to 404; it never surfaces a 502 like a native route
    would — Jellyfin clients treat both as "skip this item").

    Single-flight (#224): concurrent callers for the same key wait for
    the first resolution and share its verdict (success OR failure) — a
    failed leader means the waiters return None without re-storming the
    upstream.
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        return None
    attempts = _member_attempts_of(per_provider)
    if not attempts:
        return None
    # Single-flight (#224): if a resolution for this group is already in
    # flight, share ITS verdict (success or failure) instead of
    # re-storming the upstream in parallel — the app fires detail +
    # seasons + episodes + playback for one item in the same tick, and
    # duplicate walks are what stretched run7's cold resolution and
    # multiplied run8's 502s. The map entry is created synchronously
    # (no await between the get and the set), so two callers in the
    # same event-loop tick can never both become leader.
    existing = _resolve_inflight.get(group_key)
    if existing is not None:
        return await existing
    task = asyncio.create_task(_resolve_group_content_once(group_key, attempts))
    _resolve_inflight[group_key] = task
    try:
        return await task
    finally:
        # Drop the guard once the burst is over (bounded memory); a
        # later, genuinely new request starts a fresh resolution.
        if _resolve_inflight.get(group_key) is task:
            _resolve_inflight.pop(group_key, None)


async def _resolve_group_content_once(
    group_key: str,
    attempts: tuple[tuple[str, str], ...],
) -> ContentResponse | None:
    """One leader resolution for ``group_key`` (single-flight, #224).

    Walks the group's members in order. Re-checks the caches under the
    guard per member: a previous burst may have landed a verdict between
    the caller's first check and the task starting. The page is cached
    under the SERVING member's key (the native route's key shape), and a
    walk that had to move on says so in the log, because the axis counts
    requests, not which member answered.

    An already-resolved page answers first, whichever member holds it —
    the walk THEN does not spend a first member's B23 retry pair (2 calls
    plus the retry delay) on every view of a title it has already
    resolved from further down the group. Only a group no member has
    answered yet is walked upstream, in first-seen order.
    """
    answered = _cached_group_page(group_key)
    if answered is not None:
        return answered[1]
    http = get_client()
    for position, (provider_id, external_id) in enumerate(attempts):
        cache_key = f"content:{provider_id}:{external_id}"
        kind, cached = _member_cached(cache_key)
        if kind == "hit":
            return cached
        if kind != "miss":
            # gated / blocklisted: the walk's own stopping rule — a
            # deliberate verdict about the CLIENT's request (ADR-0002)
            # and never a health sample, the #139 backstop.
            return None
        provider = PROVIDERS.get(provider_id)
        if provider is None:
            # On record but unaskable (no provider object left): nothing to
            # fetch, so the walk moves on like any other unreachable member.
            log.warning(
                "group content member unavailable key=%s provider=%s — next member",
                group_key,
                provider_id,
            )
            continue
        resp, code = await _fetch_member_content(
            provider_id, provider, external_id, http, group_key
        )
        if resp is None:
            if code == "gated":
                # ADR-0002: a gated verdict never moves the health
                # tracker. Cache it so every later group-key call
                # short-circuits — the load_home sweep normally
                # populates `gated_cache` first, but a cold-cache g2:
                # detail call must not record a health-down here either
                # (#139).
                gated_cache.set(cache_key, True)
            if code is not None and code not in WALKABLE_VERDICT_CODES:
                return None
            log.warning(
                "group content member refused key=%s provider=%s err=%s — next member",
                group_key,
                provider_id,
                code,
            )
            continue
        TRACKER.record(provider_id, ok=True)
        if _config.SETTINGS.block_russian and is_blocked_country(resp.country):
            blocklist_cache.set(cache_key, True)
            log.info("blocked Russian content id=%s country=%s", cache_key, resp.country)
            return None
        resp.group_key = group_key
        content_cache.set(cache_key, resp)
        if position:
            log.info(
                "group content member fallback key=%s served_by=%s after=%d refusal(s)",
                group_key,
                provider_id,
                position,
            )
        return resp
    log.warning(
        "group content failed key=%s members=%d all refused", group_key, len(attempts)
    )
    return None


async def _fetch_member_content(
    provider_id: str,
    provider: BaseProvider,
    external_id: str,
    http: httpx.AsyncClient,
    group_key: str,
) -> tuple[ContentResponse | None, str | None]:
    """One member's content page, with the B23 retry pair.

    Returns the served page, or the verdict that refused it (``None`` for
    a codeless crash, recorded as the lane fault it is) for the walker to
    judge — the same split the play seam's per-member attempt makes. A
    ``gated`` verdict is NOT retried: a deliberate answer does not change
    between two attempts a second apart, and the walker caches it.
    """
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            return await provider.content(external_id, http), None
        except ProviderError as e:
            if e.code == "gated":
                return None, e.code
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
                provider_id,
                group_key,
                last_err,
            )
            await asyncio.sleep(CONTENT_RETRY_DELAY_S)
    # The shared item-vs-lane classification (ADR-0002, #373): a
    # deterministic item verdict (``not_found`` on a dead torrent or
    # bogus id) is a client-side outcome, not lane health — five dead
    # titles must not flip the provider down. ``gated`` never gets
    # here (returned above, unretried); generic (non-ProviderError)
    # failures record as lane faults.
    code = last_err.code if isinstance(last_err, ProviderError) else None
    record_verdict(provider_id, code)
    return None, code


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
        return "ok", cached
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

    Q47: read along the WALK's own order, so the verdict a later member
    holds still answers — a group that gated from its second member, while
    the first member's page was merely dead, is a 404 and not a degraded
    card answer.
    """
    per_provider = resolve_group(group_key)
    if per_provider is None:
        return True  # unknown group — the cold-cache 404 stands
    for provider_id, external_id in _member_attempts_of(per_provider):
        kind, _ = _member_cached(f"content:{provider_id}:{external_id}")
        if kind == "hit":
            # A member's page IS resolved: the group is available, whatever
            # a member after it (never reached by the walk) holds.
            return False
        if kind != "miss":
            return True  # the deliberate verdict the walk would stop on
    return False


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
    resolution map carries. A search covers the whole
    catalog — most results are NOT in the 30-min home snapshot — so the
    facade registers each merged group's provider union under EVERY
    member key it holds, the same shape ``_build_sources_map`` stores for
    home rows. First-seen provider order is preserved; providers the map
    already knows are left untouched.

    The write itself belongs to the catalog snapshot owner (ADR-0010): it
    updates the resolution map and the group index in one step and
    records the keys with the SEARCH TTL, so a snapshot replacement
    carries them forward instead of dropping them (issue #420).
    """
    catalog_state().register_search(groups)
