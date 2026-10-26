"""Facade member fallback: one member's refusal is not the title's verdict.

A ``g2:`` movie key is carried by several providers (that is what makes it
a *group*), so the first member failing is not yet the client's answer.
The seam walks to the group's next member when the refusal is one the
group can repair — a lane fault, or ``not_found`` — and stops when it is
not.

Pinned through ``GET /Videos/{gk}/stream``, the route the client's play
press actually crosses:

  - the first member's lane fault → the SECOND serves, and the 302 names
    whose bytes the client got, each member called with its own bare id;
  - ``not_found`` on the first member walks too (the dead-file case), and
    it moves only the playback axis (ADR-0002: a verdict is not lane
    health) — while the lane fault in the test above does reach the lane;
  - a dead DETAIL page on the first member walks as well (Q47: content
    resolution walks the group), and the play then leads with the member
    whose page answered instead of asking the dead one for bytes;
  - a ``gated`` DETAIL page ends the walk there — the same deliberate
    verdict, one stage earlier;
  - ``gated`` does NOT walk: 404 with the second member never called, so a
    deliberate unavailability costs the group no upstream calls;
  - every member refusing is still a 404, never a 5xx (D2), with every
    attempt on the playback axis;
  - the walk is capped (``MAX_MEMBER_ATTEMPTS``): a fourth member is never
    asked, so a title carried by many providers cannot spend one play
    press on all of them;
  - an episode wire id has exactly one member by construction — no walk.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from cs_uk_api._catalog_state import (
    blocklist_cache,
    content_cache,
    home_cache,
    reset_catalog_state,
)
from cs_uk_api._catalog_state import resolution as resolution_mod
from cs_uk_api.config import SETTINGS
from cs_uk_api.health import TRACKER
from cs_uk_api.jellyfin import delivery
from cs_uk_api.main import app
from cs_uk_api.models import ContentResponse, SearchResult, StreamResponse, Translation
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError

client = TestClient(app)

TOKEN = SETTINGS.jellyfin_token
_CDN = "https://cdn.example.test"
_TITLE = "Дюна"
_POSTER = f"{_CDN}/posters/dune.jpg"

#: Three members is a full walk (``MAX_MEMBER_ATTEMPTS``); the cap test
#: needs a fourth to prove the stop rather than a depleted group.
_CAPPED = 4


class _MemberStub(BaseProvider):
    """One provider of the shared group: canned content, scripted stream()."""

    types = ("movie",)
    newest_section = "page"

    def __init__(self, pid: str, external: str) -> None:
        self.id = pid
        self.name = pid.upper()
        self.external = external
        self.card = SearchResult(
            id=f"{pid}:{external}",
            provider=pid,
            form="movie",
            title=_TITLE,
            year=2021,
            poster=_POSTER,
            url=f"https://{pid}.example/{external}",
        )
        self.stream_calls: list[str] = []
        self.content_calls: list[str] = []
        #: What ``stream()`` does: a stream to serve, a ProviderError to
        #: raise, or None for "not scripted" (a not_found, so a test that
        #: forgot to script a member fails loudly instead of serving).
        self.outcome: StreamResponse | ProviderError | None = None
        #: What ``content()`` does instead of answering: the code whose
        #: ProviderError this member's DETAIL page dies with (Q47).
        self.content_failure: str | None = None

    async def search(self, query: str, http: Any) -> list[SearchResult]:
        return [self.card]

    async def browse(
        self, section: str, page: int, http: Any
    ) -> tuple[list[SearchResult], bool]:
        if section == "page":
            return [self.card], False
        return [], False

    async def content(self, external_id: str, http: Any) -> ContentResponse:
        """The metadata page — what decides playability (form == movie).

        Working by default: the fault most of this file is about is the
        VIDEO failing while the title itself resolves. A member can also be
        scripted with a DEAD page (``content_failure``), which is the whole
        point of the walk reaching into content resolution (Q47).
        """
        self.content_calls.append(external_id)
        if self.content_failure is not None:
            raise ProviderError(
                self.content_failure, f"{self.content_failure} page for {self.id}"
            )
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            form="movie",
            title=_TITLE,
            year=2021,
            description="Епічна науково-фантастична стрічка.",
            poster=_POSTER,
            translations=[Translation(id="uk", label="Дубляж")],
        )

    async def stream(
        self, content_id: str, translation: str | None, http: Any
    ) -> StreamResponse:
        self.stream_calls.append(content_id)
        if isinstance(self.outcome, ProviderError):
            raise self.outcome
        if self.outcome is None:
            raise ProviderError("not_found", f"no scripted stream for {content_id}")
        return self.outcome.model_copy(deep=True)


def _serves(member: _MemberStub) -> None:
    """Script the member to serve bytes under its own CDN path."""
    member.outcome = StreamResponse(
        url=f"{_CDN}/{member.id}-{member.external}.mp4", type="mp4", headers={}
    )


def _fails(member: _MemberStub, code: str) -> None:
    member.outcome = ProviderError(code, f"{code} for {member.id}")


def _dead_page(member: _MemberStub, code: str) -> None:
    """Script the member's DETAIL page to die with ``code`` (Q47)."""
    member.content_failure = code


def _group(*members: _MemberStub) -> str:
    """Register the members, build home once, return their shared group key.

    Home merges the members' identical cards (alias + form + year) into ONE
    group, so the row item's ``group_key`` is what the client plays — the
    key whose resolution lists all the members.
    """
    for member in members:
        PROVIDERS[member.id] = member
    r = client.get("/api/home")
    assert r.status_code == 200
    keys = {
        cast(str, item["group_key"])
        for row in cast("dict[str, Any]", r.json())["rows"]
        for item in row["items"]
        if item["title"] == _TITLE
    }
    assert len(keys) == 1, f"members did not merge into one group: {keys}"
    key = keys.pop()
    # The walk follows the resolution's own order; assert the harness is
    # what the tests assume (registration order) rather than discovering a
    # different order at the end of the walk.
    res = delivery.group_resolution(key)
    assert res is not None
    assert [s.provider for s in res.sources] == [m.id for m in members]
    return key


def _play(group_key: str) -> Any:
    return client.get(f"/Videos/{group_key}/stream", follow_redirects=False)


def playback() -> dict[str, Any]:
    """The ``/api/health`` playback axis, read as a client reads it."""
    r = client.get("/api/health")
    assert r.status_code == 200
    return cast("dict[str, Any]", r.json()["playback"])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Only the members of the group exist — no real provider is browsed.

    The B23 content retry delay is zeroed: a member with a dead DETAIL page
    now costs the walk a retry pair, and these tests are about WHICH member
    answers, never about how long the upstream took.
    """
    monkeypatch.setattr(resolution_mod, "CONTENT_RETRY_DELAY_S", 0.0)
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    for cache in (home_cache, content_cache, blocklist_cache):
        cache.clear()
    reset_catalog_state()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)
        for cache in (home_cache, content_cache, blocklist_cache):
            cache.clear()
        reset_catalog_state()


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def test_a_lane_fault_on_the_first_member_walks_to_the_next() -> None:
    p1 = _MemberStub("p1", "dune-1")
    p2 = _MemberStub("p2", "dune-2")
    _fails(p1, "unreachable")
    _serves(p2)
    group_key = _group(p1, p2)

    r = _play(group_key)

    assert r.status_code == 302
    assert r.headers["location"] == f"{_CDN}/p2-dune-2.mp4"  # the SECOND member's bytes
    assert p1.stream_calls == ["dune-1"]  # each member with ITS OWN bare id
    assert p2.stream_calls == ["dune-2"]

    axis = playback()
    assert axis["providers"]["p1"]["refused_codes"] == {"unreachable": 1}
    assert axis["providers"]["p2"]["served"] == 1
    assert [f["provider"] for f in axis["recent"]] == ["p1"]
    # The walk moves only the playback axis for the member that served; the
    # lane fault of the member that failed IS lane health (its own entry).
    assert TRACKER.last_error_at("p1") is not None
    assert TRACKER.last_error_at("p2") is None


def test_the_served_member_and_the_walk_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator's trace names who served — the axis counts, it does not
    say which member answered a group key."""
    p1 = _MemberStub("p1", "dune-1")
    p2 = _MemberStub("p2", "dune-2")
    _fails(p1, "unreachable")
    _serves(p2)
    group_key = _group(p1, p2)

    with caplog.at_level(logging.INFO, logger="cs_uk_api.jellyfin.delivery"):
        assert _play(group_key).status_code == 302

    assert any(
        "member fallback" in rec.getMessage() and "served_by=p2" in rec.getMessage()
        for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]


def test_not_found_walks_but_moves_only_the_playback_axis() -> None:
    """The dead-file case, and ADR-0002 in the same request.

    A member answering ``not_found`` is repairable by the group (another
    member may still have the file) and is NOT lane health — so the walk
    continues while the lane's window stays empty.
    """
    p1 = _MemberStub("p1", "dune-1")
    p2 = _MemberStub("p2", "dune-2")
    _fails(p1, "not_found")
    _serves(p2)
    group_key = _group(p1, p2)

    assert _play(group_key).status_code == 302

    assert playback()["providers"]["p1"]["refused_codes"] == {"not_found": 1}
    assert TRACKER.last_error_at("p1") is None


def test_a_dead_detail_page_still_resolves_and_plays() -> None:
    """Q47: the fault one stage earlier, repaired by the same walk.

    The first member's DETAIL page is dead, so the group's content is
    served by the second member (``resolve_group_content`` walks the group
    too) — and the play then leads with THAT member, whose translations the
    client is holding, instead of spending an upstream call on the member
    whose page could not even be read.
    """
    p1 = _MemberStub("p1", "dune-1")
    p2 = _MemberStub("p2", "dune-2")
    _dead_page(p1, "not_found")
    _serves(p2)
    group_key = _group(p1, p2)

    detail = client.get(f"/Items/{group_key}", headers={"X-Emby-Token": TOKEN})

    assert detail.status_code == 200  # the title renders, one stage repaired
    assert p1.content_calls == ["dune-1", "dune-1"]  # its own B23 retry pair
    assert p2.content_calls == ["dune-2"]  # THIS page is what answered

    r = _play(group_key)

    assert r.status_code == 302
    assert r.headers["location"] == f"{_CDN}/p2-dune-2.mp4"  # the SECOND member's bytes
    assert p1.stream_calls == []  # never asked again for bytes it never described
    assert p2.stream_calls == ["dune-2"]
    # The already-answered page is what later views read: the dead member is
    # not re-probed (its B23 pair would cost every view a retry delay).
    assert p1.content_calls == ["dune-1", "dune-1"]

    axis = playback()
    assert axis["providers"]["p2"]["served"] == 1
    assert "p1" not in axis["providers"]  # no play request ever reached it
    assert TRACKER.last_error_at("p1") is None  # dead page = item verdict


def test_a_gated_detail_page_does_not_walk() -> None:
    """The deliberate verdict, one stage earlier.

    ``gated`` says the title is not for this client — another member's page
    is not a repair for that — so the walk stops where it met the verdict:
    the second member's page is never fetched, and ``is_hard_unavailable``
    (read along the same order) still answers 404 rather than degrading to
    a card answer.
    """
    p1 = _MemberStub("p1", "dune-1")
    p2 = _MemberStub("p2", "dune-2")
    _dead_page(p1, "gated")
    _serves(p2)
    group_key = _group(p1, p2)

    r = client.get(f"/Items/{group_key}", headers={"X-Emby-Token": TOKEN})

    assert r.status_code == 404
    assert p1.content_calls == ["dune-1"]  # a verdict is not retried
    assert p2.content_calls == []
    assert p2.stream_calls == []


def test_gated_does_not_walk() -> None:
    """A deliberate unavailability is the client's own verdict.

    ``gated`` means "this title is not for you here", not "this member is
    broken": the group cannot repair it, so the second member is never
    asked — a walk would spend an upstream call to answer a question the
    client already answered.
    """
    p1 = _MemberStub("p1", "dune-1")
    p2 = _MemberStub("p2", "dune-2")
    _fails(p1, "gated")
    _serves(p2)
    group_key = _group(p1, p2)

    assert _play(group_key).status_code == 404

    assert p2.stream_calls == []
    axis = playback()
    assert axis["providers"]["p1"]["refused_codes"] == {"gated": 1}
    assert "p2" not in axis["providers"]


def test_every_member_refusing_is_still_a_404() -> None:
    p1 = _MemberStub("p1", "dune-1")
    p2 = _MemberStub("p2", "dune-2")
    _fails(p1, "unreachable")
    _fails(p2, "not_found")
    group_key = _group(p1, p2)

    r = _play(group_key)

    assert r.status_code == 404  # never a 5xx for an unplayable title (D2)
    assert p1.stream_calls == ["dune-1"]
    assert p2.stream_calls == ["dune-2"]
    axis = playback()
    assert axis["providers"]["p1"]["refused_codes"] == {"unreachable": 1}
    assert axis["providers"]["p2"]["refused_codes"] == {"not_found": 1}
    # Both attempts are lane-visible where they belong: the lane fault did
    # reach the tracker, the item verdict did not.
    assert TRACKER.last_error_at("p1") is not None
    assert TRACKER.last_error_at("p2") is None


def test_the_walk_is_capped() -> None:
    """Why the cap exists: a group carried by many providers must not spend
    one play press on all of them (#305's lesson, applied to playback)."""
    members = [_MemberStub(f"p{i}", f"dune-{i}") for i in range(1, _CAPPED + 1)]
    for member in members:
        _fails(member, "unreachable")
    group_key = _group(*members)

    assert _play(group_key).status_code == 404

    cap = delivery.MAX_MEMBER_ATTEMPTS
    assert [len(m.stream_calls) for m in members] == [1] * cap + [0] * (_CAPPED - cap)


def test_an_episode_wire_id_has_one_member() -> None:
    """Episodes are not reverse-lookupable (D2), so there is no group to
    walk: an episode's failure is final, not an invitation to ask a
    different provider for a title it may not even carry."""
    p1 = _MemberStub("p1", "dune-1")
    p2 = _MemberStub("p2", "dune-2")
    _fails(p1, "unreachable")
    _serves(p2)
    _group(p1, p2)

    r = client.get("/Videos/p1:dune-1/stream", follow_redirects=False)

    assert r.status_code == 404
    assert p1.stream_calls == ["dune-1"]
    assert p2.stream_calls == []
    assert playback()["providers"]["p1"]["refused_codes"] == {"unreachable": 1}
