"""Internal resolution module (spec #309 T5) — group-key machinery.

Covers ``cs_uk_api._catalog_state.resolution`` directly: ``g2:``
resolution from the sources map, the episode-tail reverse lookup
(exact + numeric fallback), search-group registration folding into the
map, the subscription-gate sweep verdicts, the hard-unavailable
verdict, and the member walk content resolution shares with the play
seam (Q47 — a dead first member is not the title's verdict). The
wire-level behaviour is pinned in test_gated_filter /
test_group_content_peek / test_jellyfin_member_fallback /
test_jellyfin_detail; these tests exercise the internal module's own
contracts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest

from cs_uk_api import _catalog_state as catalog_state
from cs_uk_api._catalog_state import resolution
from cs_uk_api._catalog_state.resolution import (
    episode_group_key,
    filter_gated_items,
    group_key_for_external,
    is_hard_unavailable,
    register_search_groups,
    resolve_group,
)
from cs_uk_api.health import ITEM_VERDICT_CODES, STATUS_OK, TRACKER
from cs_uk_api.models import (
    ContentResponse,
    SearchGroup,
    SearchResult,
    Translation,
)
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError


def _item(pid: str, external: str, title: str) -> SearchResult:
    mb_form, _mb_styles = ("movie", frozenset())
    return SearchResult(
        id=f"{pid}:{external}",
        provider=pid,
        form=mb_form,
        styles=frozenset(),
        title=title,
        year=2021,
        url=f"https://{pid}.example/{external}",
    )


def _seed_sources(mapping: dict[str, dict[str, SearchResult]]) -> None:
    catalog_state.seed_group_sources(mapping)


@pytest.fixture(autouse=True)
def isolate() -> Iterator[None]:
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (
        catalog_state.content_cache,
        catalog_state.gated_cache,
        catalog_state.blocklist_cache,
        catalog_state.home_cache,
    ):
        cache.clear()
    catalog_state.reset_catalog_state()
    catalog_state.clear_playback()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def test_resolve_group_cold_is_none_then_seeded() -> None:
    assert resolve_group("g2:abc") is None
    item = _item("p1", "1", "Дюна")
    _seed_sources({"g2:abc": {"p1": item}})
    per_provider = resolve_group("g2:abc")
    assert per_provider is not None
    assert per_provider["p1"].title == "Дюна"


def test_episode_group_key_passthrough_and_reverse_lookup() -> None:
    # A movie reports its g2: key directly.
    assert episode_group_key("g2:abc") == "g2:abc"
    # An episode wire id resolves through the sources map.
    item = _item("p1", "dorama-1", "Серіал")
    _seed_sources({"g2:ser": {"p1": item}})
    assert episode_group_key("p1:dorama-1:s1e1") == "g2:ser"
    # An unresolvable id answers None, never raises.
    assert episode_group_key("p1:unknown:s1e1") is None


def test_group_key_for_external_exact_and_numeric_fallback() -> None:
    _seed_sources(
        {
            "g2:ser": {
                "p1": _item("p1", "anime-series:6268-narutto-1-sezon", "Наруто"),
            }
        }
    )
    # Exact composite match.
    assert group_key_for_external("p1:anime-series:6268-narutto-1-sezon") == "g2:ser"
    # Ticket #234 numeric fallback: the episode prefix ``p1:6268`` misses
    # exactly, but the numeric segment matches inside the card id.
    assert group_key_for_external("p1:6268") == "g2:ser"
    assert group_key_for_external("p1:zzz") is None


def test_register_search_groups_folds_member_keys_into_resolution_map() -> None:
    """A searched card registers under EVERY member key (ticket #106) —
    any key the client holds resolves the full group."""
    item = _item("p1", "x", "Дюна")
    group = SearchGroup(
        group_key="g2:dune",
        title="Дюна",
        year=2021,
        poster=None,
        form="movie",
        styles=[],
        genres=[],
        sources=[item],
        member_keys=["g2:dune", "g2:dune-yearless"],
    )
    register_search_groups([group])
    for key in ("g2:dune", "g2:dune-yearless"):
        per_provider = resolve_group(key)
        assert per_provider is not None and per_provider["p1"].title == "Дюна"


class _Gated(BaseProvider):
    """can_gate provider whose content() always raises ``gated``."""

    id = "gated-stub"
    name = "GatedStub"
    types = ("movie",)
    can_gate = True

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        raise ProviderError(code="gated", message="promo only")

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


async def test_filter_gated_items_drops_gated_and_caches_verdict() -> None:
    PROVIDERS["gated-stub"] = _Gated()
    items = [_item("gated-stub", "1", "Промо")]
    kept = await filter_gated_items(items, http=cast(Any, None))
    assert kept == []
    # The verdict is cached — a repeat pass needs no provider call.
    assert catalog_state.gated_cache.get("content:gated-stub:1") is True


class _Free(BaseProvider):
    id = "free-stub"
    name = "FreeStub"
    types = ("movie",)
    can_gate = True

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        return ContentResponse(
            id=f"free-stub:{external_id}",
            form="movie",
            title="Вільний",
            translations=[Translation(id="uk", label="UK")],
        )

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


async def test_filter_gated_items_keeps_free_card_and_marks_known_good() -> None:
    PROVIDERS["free-stub"] = _Free()
    items = [_item("free-stub", "1", "Вільний")]
    kept = await filter_gated_items(items, http=cast(Any, None))
    assert [it.id for it in kept] == ["free-stub:1"]
    assert catalog_state.gated_cache.get("content:free-stub:1") is False


def test_is_hard_unavailable_unknown_gated_and_clean() -> None:
    # Unknown group — the cold-cache 404 stands.
    assert is_hard_unavailable("g2:unknown") is True
    item = _item("p1", "1", "Дюна")
    _seed_sources({"g2:abc": {"p1": item}})
    # Seeded + no verdicts → available.
    assert is_hard_unavailable("g2:abc") is False
    # A gated verdict flips it to hard-unavailable.
    catalog_state.gated_cache.set("content:p1:1", True)
    assert is_hard_unavailable("g2:abc") is True


class _FlakyThenReal(BaseProvider):
    """YTS-stub whose details flake (the playtest's stripped payload):
    the first TWO content() calls raise typed not_found — a sustained
    burst strip, both B23 attempts dead — then the third returns the
    real envelope, the upstream having healed between resolves."""

    id = "flaky-stub"
    name = "FlakyStub"
    types = ("movie",)
    calls = 0

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        _FlakyThenReal.calls += 1
        if _FlakyThenReal.calls <= 2:
            raise ProviderError(code="not_found", message="carried no torrents")
        return ContentResponse(
            id=f"flaky-stub:{external_id}",
            form="movie",
            title="Dune",
            translations=[Translation(id="en", label="EN")],
        )

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


async def test_stripped_details_not_cached_and_next_resolve_heals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The playtest fix's wire contract: a provider ``not_found`` from a
    stripped details payload must NEVER enter ``content_cache`` — the
    poisoned envelope was what turned a one-off upstream flake into a
    30-minute title outage. The very next ``resolve_group_content``
    re-fetches and serves the healed answer. (ADR-0002: the verdict is
    item-class, so the lane stays healthy.)"""
    monkeypatch.setattr(resolution, "CONTENT_RETRY_DELAY_S", 0.0)
    PROVIDERS["flaky-stub"] = _FlakyThenReal()
    item = _item("flaky-stub", "tt1160419", "Dune")
    _seed_sources({"g2:dune": {"flaky-stub": item}})

    assert await resolution.resolve_group_content("g2:dune") is None
    # Nothing cached — the failure left no envelope behind.
    assert catalog_state.content_cache.get("content:flaky-stub:tt1160419") is None
    assert TRACKER.status("flaky-stub") is STATUS_OK  # item-class verdict

    healed = await resolution.resolve_group_content("g2:dune")
    assert healed is not None and healed.title == "Dune"
    # calls 1-2: the first resolve's B23 pair (both stripped); call 3: the
    # fresh upstream probe the second resolve was allowed to make.
    assert _FlakyThenReal.calls == 3
    assert catalog_state.content_cache.get("content:flaky-stub:tt1160419") is not None


# --- the member walk (Q47) --------------------------------------------
#
# A ``g2:`` key is carried by several providers, so ONE member's dead
# detail page is not the title's verdict — the same walk the play seam
# runs, applied to content resolution.


class _Member(BaseProvider):
    """One group member with a scripted detail page.

    ``failure`` is the ProviderError code its ``content()`` raises instead
    of answering; ``None`` serves a movie page named after the member, so
    a test can tell which member's page it got.
    """

    name = "Member"
    types = ("movie",)

    def __init__(self, pid: str, external: str, *, failure: str | None = None) -> None:
        self.id = pid
        self.name = pid.upper()
        self.external = external
        self.card = _item(pid, external, "Дюна")
        self.failure = failure
        self.content_calls: list[str] = []

    async def search(self, query, http):  # type: ignore[no-untyped-def]
        return []

    async def content(self, external_id, http):  # type: ignore[no-untyped-def]
        self.content_calls.append(external_id)
        if self.failure is not None:
            raise ProviderError(code=self.failure, message=f"{self.failure} for {self.id}")
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            form="movie",
            title=f"{self.id} page",
            year=2021,
            translations=[Translation(id="uk", label="Дубляж")],
        )

    async def stream(self, content_id, translation, http):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _seed_members(group_key: str, *members: _Member) -> None:
    """Register the members and seed their shared group, in this order."""
    for member in members:
        PROVIDERS[member.id] = member
    _seed_sources({group_key: {m.id: m.card for m in members}})


async def test_group_content_walks_past_a_dead_detail_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-visible point of the walk: a title whose FIRST member's
    detail page is dead still resolves from another member.

    ``not_found`` is a dead page, not the title's verdict (ADR-0002), so
    the walk asks the next member — and the page is cached under the key
    of the member that ANSWERED, so the native ``/api/content`` route and
    the next facade call share it."""
    monkeypatch.setattr(resolution, "CONTENT_RETRY_DELAY_S", 0.0)
    p1 = _Member("p1", "dune-1", failure="not_found")
    p2 = _Member("p2", "dune-2")
    _seed_members("g2:dune", p1, p2)

    content = await resolution.resolve_group_content("g2:dune")

    assert content is not None and content.id == "p2:dune-2"
    assert content.group_key == "g2:dune"
    assert p1.content_calls == ["dune-1", "dune-1"]  # its own B23 pair
    assert p2.content_calls == ["dune-2"]  # each member with its OWN bare id
    assert catalog_state.content_cache.get("content:p2:dune-2") is not None
    assert catalog_state.content_cache.get("content:p1:dune-1") is None
    # The seam that tells the play walk who answered.
    assert catalog_state.group_content_member("g2:dune") == "p2"
    assert catalog_state.peek_group_content("g2:dune") is not None
    # ADR-0002: a dead page is item-class (not lane health); the member
    # that served is the only health sample this request makes.
    assert TRACKER.last_error_at("p1") is None
    assert TRACKER.status("p2") is STATUS_OK

    # The walked-to answer is what the next resolve reads — no refetch of
    # either member (both pages are now answered for).
    p1.content_calls.clear()
    p2.content_calls.clear()
    again = await resolution.resolve_group_content("g2:dune")
    assert again is not None and again.id == "p2:dune-2"
    assert p1.content_calls == [] and p2.content_calls == []


async def test_group_content_lane_fault_also_walks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane fault is a member problem, so it walks too — and it IS lane
    health, on the member that faulted."""
    monkeypatch.setattr(resolution, "CONTENT_RETRY_DELAY_S", 0.0)
    p1 = _Member("p1", "dune-1", failure="upstream_unreachable")
    p2 = _Member("p2", "dune-2")
    _seed_members("g2:dune", p1, p2)

    assert await resolution.resolve_group_content("g2:dune") is not None
    assert TRACKER.last_error_at("p1") is not None


async def test_group_content_does_not_walk_past_a_deliberate_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``gated`` answers the CLIENT's request: another member's page is not
    a repair for it, so the walk stops and the next member is never asked
    (the same rule the play seam follows)."""
    monkeypatch.setattr(resolution, "CONTENT_RETRY_DELAY_S", 0.0)
    p1 = _Member("p1", "dune-1", failure="gated")
    p2 = _Member("p2", "dune-2")
    _seed_members("g2:dune", p1, p2)

    assert await resolution.resolve_group_content("g2:dune") is None
    assert p2.content_calls == []
    # One attempt, not a retry pair: a deliberate verdict does not change
    # a second later, and the verdict is cached for the next call.
    assert p1.content_calls == ["dune-1"]
    assert catalog_state.gated_cache.get("content:p1:dune-1") is True
    assert TRACKER.last_error_at("p1") is None


async def test_group_content_is_none_when_every_member_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolution, "CONTENT_RETRY_DELAY_S", 0.0)
    p1 = _Member("p1", "dune-1", failure="not_found")
    p2 = _Member("p2", "dune-2", failure="not_found")
    _seed_members("g2:dune", p1, p2)

    assert await resolution.resolve_group_content("g2:dune") is None
    assert p1.content_calls and p2.content_calls


async def test_group_content_walk_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream etiquette: a work carried by many providers does not spend
    one request on all of them (``MAX_MEMBER_ATTEMPTS``, the play seam's
    cap — one vocabulary, so the two walks cannot diverge)."""
    monkeypatch.setattr(resolution, "CONTENT_RETRY_DELAY_S", 0.0)
    members = [
        _Member(f"p{i}", f"dune-{i}", failure="not_found") for i in range(1, 5)
    ]
    _seed_members("g2:dune", *members)

    assert await resolution.resolve_group_content("g2:dune") is None

    cap = resolution.MAX_MEMBER_ATTEMPTS
    asked = [bool(m.content_calls) for m in members]
    assert asked == [True] * cap + [False] * (len(members) - cap)
    assert resolution.WALKABLE_VERDICT_CODES >= {"not_found", "unreachable"}
    assert "gated" not in resolution.WALKABLE_VERDICT_CODES


def test_the_pinned_track_verdicts_walk_and_the_deliberate_one_does_not() -> None:
    """A track is per-MEMBER knowledge, so the client's own track verdicts
    are repairable by the group: the member that carries the pinned dub is
    the one asked for it (Q48). The codes stay the taxonomy's item
    verdicts — walking past them changes WHO is asked, never lane health
    (ADR-0002) — while ``gated`` remains the walk's stop."""
    assert resolution.TRANSLATION_VERDICT_CODES <= resolution.WALKABLE_VERDICT_CODES
    assert resolution.TRANSLATION_VERDICT_CODES <= ITEM_VERDICT_CODES
    assert "gated" not in resolution.WALKABLE_VERDICT_CODES


async def test_group_content_concurrent_callers_share_one_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#224 under the walk: the app fires detail + seasons + episodes +
    playback in one tick, so concurrent callers share ONE leader walk —
    including its decision to move past the dead member."""
    monkeypatch.setattr(resolution, "CONTENT_RETRY_DELAY_S", 0.0)
    p1 = _Member("p1", "dune-1", failure="not_found")
    p2 = _Member("p2", "dune-2")
    _seed_members("g2:dune", p1, p2)

    async def _resolve() -> ContentResponse | None:
        return await resolution.resolve_group_content("g2:dune")

    marks = [asyncio.create_task(_resolve()) for _ in range(4)]
    answers = await asyncio.gather(*marks)

    assert all(a is not None and a.id == "p2:dune-2" for a in answers)
    assert len(p1.content_calls) == 2  # ONE walk's B23 pair, not four
    assert p2.content_calls == ["dune-2"]


def test_hard_unavailable_reads_the_walks_order() -> None:
    """A deliberate verdict held by a LATER member still answers: the first
    member's page being dead is not a licence to degrade a gated title into
    a card answer (#224's rule, walked)."""
    p1 = _Member("p1", "dune-1", failure="not_found")
    p2 = _Member("p2", "dune-2")
    _seed_members("g2:dune", p1, p2)
    assert is_hard_unavailable("g2:dune") is False

    # The first member has no page and no verdict; the second holds the
    # deliberate one the walk would stop on.
    catalog_state.gated_cache.set("content:p2:dune-2", True)
    assert is_hard_unavailable("g2:dune") is True

    # Once a member's page IS resolved, the group is available — even if a
    # member after it holds a verdict the walk never reaches.
    catalog_state.gated_cache.clear()
    catalog_state.content_cache.set(
        "content:p1:dune-1",
        ContentResponse(
            id="p1:dune-1",
            form="movie",
            title="P1",
            translations=[Translation(id="uk", label="Дубляж")],
        ),
    )
    catalog_state.gated_cache.set("content:p2:dune-2", True)
    assert is_hard_unavailable("g2:dune") is False


def test_member_attempts_lead_moves_the_serving_member_first() -> None:
    """The play seam's lead (Q47): first-seen order, with the member whose
    page answered pulled to the front and every other rule untouched."""
    p1 = _Member("p1", "dune-1")
    p2 = _Member("p2", "dune-2")
    p3 = _Member("p3", "dune-3")
    _seed_members("g2:dune", p1, p2, p3)
    sources = (p1.card, p2.card, p3.card)

    assert resolution.member_attempts(sources) == (
        ("p1", "dune-1"),
        ("p2", "dune-2"),
        ("p3", "dune-3"),
    )
    assert resolution.member_attempts(sources, lead="p2") == (
        ("p2", "dune-2"),
        ("p1", "dune-1"),
        ("p3", "dune-3"),
    )
    # A lead the group does not carry is a no-op, not an emptier walk.
    assert resolution.member_attempts(sources, lead="gone") == (
        ("p1", "dune-1"),
        ("p2", "dune-2"),
        ("p3", "dune-3"),
    )


def test_member_attempts_drops_unregistered_only_when_asked() -> None:
    """The play seam needs the provider object; content resolution does
    not (a cached page answers without it) — one rule, two callers."""
    p1 = _Member("p1", "dune-1")
    _seed_members("g2:dune", p1)
    sources = (p1.card, _item("retired", "x", "Дюна"))

    assert resolution.member_attempts(sources) == (("p1", "dune-1"),)
    assert resolution.member_attempts(sources, registered_only=False) == (
        ("p1", "dune-1"),
        ("retired", "x"),
    )
