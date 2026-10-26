"""Drift monitor orchestration + issue-flow tests (spec #285, #288).

The monitor's verdict/counter/issue boundary is pinned with a recorder
gateway and stub providers: first failure logs only (no issue), the
second consecutive failure files (deduped — repeated failures reuse the
open issue), a healthy pass resets the counter and closes a previously
open issue, and the report + exit status reflect every provider.

The playback axis (``drift/attestation.py``) is pinned the same way at the
after the probe-axis tests: the same two-consecutive-sweep rule, its own
issue, and no interference in either direction (a recovered listing does not
close a playback issue, and a play fault does not fail a probed listing).
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest

from cs_uk_api.drift.attestation import (
    UNKNOWN_PROVIDER_KEY,
    PlayCounters,
    PlaySnapshot,
)
from cs_uk_api.drift.baseline import BaselineStore
from cs_uk_api.drift.issues import DriftFault
from cs_uk_api.drift.monitor import MonitorReport, run_once
from cs_uk_api.models import SearchResult, StreamResponse
from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers.base import BaseProvider, ProviderError


class _RecorderGateway:
    """Per-provider issue-tracker recorder. Stateful per (provider, fault):
    ``find_open`` returns None until that provider's issue ON THAT AXIS is
    filed, then its number — so dedupe and recovery are exercised per
    provider and per fault, exactly as the real tracker behaves (its titles
    differ per fault, so one axis's issue is never the other's). ``close``
    retires the issue by NUMBER from the OPEN set, again like the real
    tracker (whose ``find_open`` filters ``--state open``): a settled issue is
    not re-closed on every later healthy sweep.

    An issue of one axis is never another axis's issue, which is what lets a
    test assert that closing one leaves the other open.
    """

    #: One issue number per fault, so the fake can tell them apart.
    NUMBERS: ClassVar[dict[DriftFault, str]] = {"sweep": "42", "playback": "43"}

    def __init__(self, open_issue: str | None = None) -> None:
        # ``open_issue``: when given, EVERY provider and fault answers it as a
        # pre-existing open issue (for the reuse-path tests).
        self._pre_existing = open_issue
        self._filed: dict[tuple[str, DriftFault], str] = {}
        self.opened: list[tuple[str, DriftFault]] = []
        self.commented: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.find_calls: list[tuple[str, DriftFault]] = []
        #: The bodies, so a closing statement can be pinned as what it
        #: claims to be (recovery vs an accepted shape change).
        self.comment_bodies: list[str] = []
        self.close_comments: list[str] = []
        #: The filing bodies, so a test can pin the evidence an issue carries.
        self.opened_bodies: list[str] = []

    def find_open(
        self, provider_id: str, fault: DriftFault = "sweep"
    ) -> str | None:
        self.find_calls.append((provider_id, fault))
        if self._pre_existing is not None:
            return self._pre_existing
        return self._filed.get((provider_id, fault))

    def open(
        self, provider_id: str, body: str, fault: DriftFault = "sweep"
    ) -> str:
        self.opened.append((provider_id, fault))
        self.opened_bodies.append(body)
        self._filed[(provider_id, fault)] = self.NUMBERS[fault]
        return self.NUMBERS[fault]

    def comment(self, provider_id: str, issue_number: str, body: str) -> None:
        self.commented.append((provider_id, issue_number))
        self.comment_bodies.append(body)

    def close(self, provider_id: str, issue_number: str, comment: str) -> None:
        self.closed.append((provider_id, issue_number))
        self.close_comments.append(comment)
        for key, number in list(self._filed.items()):
            if number == issue_number:
                del self._filed[key]


class _FakeHttp:
    """Minimal httpx-like client: HEAD answers 200 so deep probes pass, and
    GET answers the API's health surface.

    The surface answers an EMPTY playback section unless a test hands it
    counters, which is "the API is up and nobody played" — so no test needs a
    network for the playback axis, and a test that does not care about it
    still gets the honest "nothing to say" rather than an unreadable-surface
    warning.
    """

    def __init__(self, playback: dict[str, dict[str, int]] | None = None) -> None:
        self.playback = playback if playback is not None else {}
        self.get_urls: list[str] = []

    async def head(self, url: str) -> Any:
        class _R:
            status_code = 200

        return _R()

    async def get(self, url: str) -> Any:
        self.get_urls.append(url)
        payload = {"playback": {"providers": self.playback, "recent": []}}

        class _R:
            status_code = 200

            def json(self) -> Any:
                return payload

        return _R()


def _card(pid: str, i: int, *, form: str = "movie") -> SearchResult:
    return SearchResult(
        id=f"{pid}:ext-{i}",
        provider=pid,
        form=form,  # type: ignore[arg-type]
        title=f"Title {i}",
        url=f"https://{pid}.example/{i}",
    )


class _FailProvider(BaseProvider):
    """Always fails: zero cards (hard floor) — deterministic drift."""

    id = "p1"
    name = "P1"
    types = ("movie", "series")
    newest_section = "page"

    async def search(self, query: str, http: object) -> list[SearchResult]:
        return []

    async def browse(self, section: str, page: int, http: object) -> tuple[list[SearchResult], bool]:
        return [], False

    async def content(self, external_id: str, http: object) -> object:
        raise ProviderError("not_found", "no content")

    async def stream(self, content_id: str, translation: str | None, http: object) -> StreamResponse:
        raise ProviderError("not_found", "no stream")


class _OkProvider(BaseProvider):
    """Always healthy: a stable 5-card listing (>= hard floor 2)."""

    id = "p2"
    name = "P2"
    types = ("movie", "series")
    newest_section = "page"

    async def search(self, query: str, http: object) -> list[SearchResult]:
        return [_card("p2", i) for i in range(5)]

    async def browse(self, section: str, page: int, http: object) -> tuple[list[SearchResult], bool]:
        return [_card("p2", i) for i in range(5)], False

    async def content(self, external_id: str, http: object) -> object:
        from cs_uk_api.models import ContentResponse, Translation

        return ContentResponse(
            id=f"p2:{external_id}",
            form="movie",  # type: ignore[arg-type]
            title="T",
            translations=[Translation(id="uk", label="Дубляж")],
        )

    async def stream(self, content_id: str, translation: str | None, http: object) -> StreamResponse:
        return StreamResponse(url="https://cdn.example.test/v.mp4", type="mp4", headers={})


class _OkP1Provider(_OkProvider):
    """The healthy provider under p1's id — for the recovery test, which
    must swap the FAILING p1 for a healthy p1 (same provider id)."""

    id = "p1"
    name = "P1"

    async def search(self, query: str, http: object) -> list[SearchResult]:
        return [_card("p1", i) for i in range(5)]

    async def browse(self, section: str, page: int, http: object) -> tuple[list[SearchResult], bool]:
        return [_card("p1", i) for i in range(5)], False


@pytest.fixture(autouse=True)
def _isolate_providers() -> None:
    saved = dict(PROVIDERS)
    PROVIDERS.clear()
    try:
        yield
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(saved)


def _seed(*providers: BaseProvider) -> None:
    for p in providers:
        PROVIDERS[p.id] = p


# ------------------------------------------------------------ run / report


@pytest.mark.asyncio
async def test_run_reports_ok_and_failed_providers(tmp_path) -> None:
    _seed(_FailProvider(), _OkProvider())
    store = BaselineStore(tmp_path / "state.json")
    report = await run_once(
        day_offset=0,
        store=store,
        gateway=_RecorderGateway(),
        report_path=tmp_path / "report.json",
        skip_issues=True,
        http=_FakeHttp(),
    )

    assert report.failed == ["p1"]
    assert "p2" not in report.failed
    assert report.providers["p1"]["consecutive_failures"] == 1
    assert report.providers["p2"]["consecutive_failures"] == 0
    assert report.providers["p2"]["ok"] is True
    # Report file written.
    assert (tmp_path / "report.json").exists()
    # Excluded providers never appear.
    assert "uakino" not in report.providers


class _TransportFailProvider(_OkProvider):
    """The adapters' canonical transport failure: ``ProviderError(
    "unreachable", str(error))`` raised ``from`` an httpx exception.

    httpx's transport exceptions stringify to ``''`` (ConnectTimeout,
    ReadTimeout, ReadError, RemoteProtocolError), so this arrives at the
    probe with an EMPTY message — the shape that left three vhosts on one
    blackholing origin reading as an undecidable ``unreachable: ``.

    ``message=`` overrides the adapter's own string, for the edge where
    the adapter passed ``""`` while the cause still had text to show.
    """

    id = "p3"
    name = "P3"

    def __init__(self, cause: httpx.HTTPError, *, message: str | None = None) -> None:
        self._cause = cause
        self._message = str(cause) if message is None else message

    async def browse(self, section: str, page: int, http: object) -> tuple[list[SearchResult], bool]:
        error = ProviderError("unreachable", self._message)
        raise error from self._cause


class _MessageFailProvider(_OkProvider):
    """A verdict the adapter DID write a message for is kept verbatim,
    even when the raise carries a transport cause of its own."""

    id = "p4"
    name = "P4"

    async def browse(self, section: str, page: int, http: object) -> tuple[list[SearchResult], bool]:
        cause = httpx.ReadTimeout("")
        raise ProviderError("upstream_unreachable", "status 503") from cause


@pytest.mark.parametrize(
    "cause_type",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.ReadError,
        httpx.RemoteProtocolError,
    ],
    ids=["ConnectTimeout", "ReadTimeout", "ReadError", "RemoteProtocolError"],
)
@pytest.mark.asyncio
async def test_a_transport_failure_names_its_cause(
    tmp_path, cause_type: type[httpx.HTTPError]
) -> None:
    """An empty verdict message falls back to the cause's class name.

    Every httpx transport exception the adapters can wrap has an empty
    ``str``; the class name is the only signal that separates a connect
    blackhole from a read timeout, both in the sweep output and in the
    filed issue. Regression for kinotron / cikavaideya / simpsonsuatv
    (three vhosts on one origin, 91.240.20.12) reporting ``unreachable: ``
    with nothing after the colon.
    """
    cause = cause_type("")
    assert str(cause) == ""  # the premise: only the class name distinguishes it
    _seed(_TransportFailProvider(cause))
    report = await run_once(
        day_offset=0,
        store=BaselineStore(tmp_path / "state.json"),
        gateway=_RecorderGateway(),
        report_path=tmp_path / "report.json",
        skip_issues=True,
        http=_FakeHttp(),
    )

    assert report.failed == ["p3"]  # still a failure — only the text changed
    expected = f"unreachable: {cause_type.__name__}"
    assert report.providers["p3"]["listing"]["error"] == expected
    assert report.providers["p3"]["reason"] == f"probe failed: {expected}"


@pytest.mark.asyncio
async def test_a_cause_with_text_is_preserved_in_the_report(tmp_path) -> None:
    """A blank adapter message must not reduce a cause that says why:
    the cause's own text reaches the report (the class is only the
    fallback when that text is empty)."""
    _seed(_TransportFailProvider(httpx.ConnectError("connection refused"), message=""))
    report = await run_once(
        day_offset=0,
        store=BaselineStore(tmp_path / "state.json"),
        gateway=_RecorderGateway(),
        report_path=tmp_path / "report.json",
        skip_issues=True,
        http=_FakeHttp(),
    )

    assert report.providers["p3"]["listing"]["error"] == "unreachable: connection refused"


@pytest.mark.asyncio
async def test_a_transport_cause_reaches_the_filed_issue_body(tmp_path) -> None:
    """The cause class is the evidence an operator reads in the filed
    issue, not only a report detail: the second consecutive failure files
    the sweep issue, and its body carries
    ``error=unreachable: ReadTimeout`` rather than the blank tail this
    formatter replaced."""
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")
    _seed(_TransportFailProvider(httpx.ReadTimeout("")))

    first = await run_once(
        day_offset=0,
        store=store,
        gateway=gateway,
        report_path=None,
        http=_FakeHttp(),
    )
    assert first.failed == ["p3"]
    assert gateway.opened == []  # the first failure logs only

    second = await run_once(
        day_offset=1,
        store=store,
        gateway=gateway,
        report_path=None,
        http=_FakeHttp(),
    )
    assert second.failed == ["p3"]
    assert gateway.opened == [("p3", "sweep")]
    body = gateway.opened_bodies[0]
    assert "error=unreachable: ReadTimeout)." in body
    assert "error=unreachable: )." not in body  # the shape this formatter replaced


@pytest.mark.asyncio
async def test_an_adapter_message_is_never_replaced(tmp_path) -> None:
    """The fallback only fires on an empty message; a real one is kept
    even when the raise carries a transport cause of its own."""
    _seed(_MessageFailProvider())
    report = await run_once(
        day_offset=0,
        store=BaselineStore(tmp_path / "state.json"),
        gateway=_RecorderGateway(),
        report_path=tmp_path / "report.json",
        skip_issues=True,
        http=_FakeHttp(),
    )

    assert report.providers["p4"]["listing"]["error"] == "upstream_unreachable: status 503"


@pytest.mark.asyncio
async def test_run_writes_report_file(tmp_path) -> None:
    _seed(_OkProvider())
    report_path = tmp_path / "report.json"
    await run_once(
        day_offset=0,
        store=BaselineStore(tmp_path / "state.json"),
        gateway=_RecorderGateway(),
        report_path=report_path,
        skip_issues=True,
        http=_FakeHttp(),
    )
    import json

    # The report is parseable JSON written atomically — no half-written
    # file or temp leftover can survive a run.
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    assert "p2" in raw["providers"]
    assert "run_at" in raw
    # Both axes are in the artefact, and the playback one says whether it was
    # read at all — an empty section is never the operator's only signal.
    assert "playback" in raw
    assert "attestation_error" in raw
    leftovers = [f.name for f in tmp_path.iterdir() if ".tmp" in f.name]
    assert leftovers == []


# ------------------------------------------------------------ issue flow


@pytest.mark.asyncio
async def test_first_failure_logs_only_no_issue(tmp_path) -> None:
    """Ticket #288 AC: the FIRST failure files nothing."""
    _seed(_FailProvider())
    gateway = _RecorderGateway()
    await run_once(
        day_offset=0,
        store=BaselineStore(tmp_path / "state.json"),
        gateway=gateway,
        report_path=None,
        http=_FakeHttp(),
    )

    assert gateway.opened == []
    assert gateway.closed == []


@pytest.mark.asyncio
async def test_second_consecutive_failure_files_issue(tmp_path) -> None:
    """The SECOND consecutive failure files (opens) the issue."""
    _seed(_FailProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    await run_once(day_offset=0, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == []

    await run_once(day_offset=1, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == [("p1", "sweep")]


@pytest.mark.asyncio
async def test_repeated_failures_reuse_open_issue(tmp_path) -> None:
    """Repeated failures do NOT duplicate — the open issue is reused.
    The stateful gateway returns None until the issue is actually
    filed, then the filed number, so a third failure finds it open."""
    _seed(_FailProvider())
    gateway = _RecorderGateway()  # stateful: no pre-existing issue
    store = BaselineStore(tmp_path / "state.json")

    # Two failures to reach the threshold, then a third failure while an
    # issue is already open.
    await run_once(day_offset=0, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    await run_once(day_offset=1, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == [("p1", "sweep")]

    await run_once(day_offset=2, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == [("p1", "sweep")]  # no second open
    # find_open was consulted on the filing run AND the reuse run (the
    # first failing run is below the threshold and never looks).
    assert gateway.find_calls.count(("p1", "sweep")) >= 2


@pytest.mark.asyncio
async def test_recovery_resets_counter_and_closes_issue(tmp_path) -> None:
    """A healthy pass after failures resets the counter and closes the
    previously open issue (ticket #288 AC: recovery comments + closes)."""
    gateway = _RecorderGateway()  # stateful: issue opens on 2nd failure
    store = BaselineStore(tmp_path / "state.json")

    # Fail twice with p1 only, then swap in a healthy p1.
    _seed(_FailProvider())
    await run_once(day_offset=0, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    await run_once(day_offset=1, store=store, gateway=gateway, report_path=None, http=_FakeHttp())
    assert gateway.opened == [("p1", "sweep")]

    # Now p1 is healthy — recovery path finds the open issue and closes.
    _seed(_OkP1Provider())
    await run_once(day_offset=2, store=store, gateway=gateway, report_path=None, http=_FakeHttp())

    assert gateway.closed == [("p1", "42")]
    assert gateway.commented == [("p1", "42")]
    state = store.get("p1")
    assert state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_skip_issues_never_touches_gateway(tmp_path) -> None:
    """--no-issues runs probes and verdicts without tracker calls."""
    _seed(_FailProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    await run_once(day_offset=0, store=store, gateway=gateway, report_path=None, skip_issues=True, http=_FakeHttp())
    await run_once(day_offset=1, store=store, gateway=gateway, report_path=None, skip_issues=True, http=_FakeHttp())

    assert gateway.opened == []
    assert gateway.find_calls == []
    # Counters still tracked (report + next real run use them).
    assert store.get("p1").consecutive_failures == 2


@pytest.mark.asyncio
async def test_failed_deep_probe_fails_provider(tmp_path) -> None:
    """A deep-probe failure fails the provider even with a healthy
    listing (animeon's lost stream URLs — spec #285's regression case)."""

    class _DeepFail(_OkProvider):
        async def stream(self, content_id: str, translation: str | None, http: object) -> StreamResponse:
            raise ProviderError("not_found", "no stream")

    _seed(_DeepFail())
    # day_offset must include p1 (renamed to p1 id is already p2 — use a
    # deep-every large enough to guarantee the provider is deep-probed).
    report = await run_once(
        day_offset=0,
        deep_every_n_days=1,  # every provider every run
        store=BaselineStore(tmp_path / "state.json"),
        gateway=_RecorderGateway(),
        report_path=None,
        skip_issues=True,
        http=_FakeHttp(),
    )

    assert "p2" in report.failed
    assert "deep probe failed" in report.providers["p2"]["reason"]


# ------------------------------------------------- convergence (moved shape)


class _ListingProvider(_OkProvider):
    """A healthy provider whose listing size is set per seed — the
    harness for a provider that MOVED (its catalog really shrank)."""

    id = "p1"
    name = "P1"

    def __init__(self, count: int) -> None:
        self.count = count

    async def search(self, query: str, http: object) -> list[SearchResult]:
        return [_card("p1", i) for i in range(self.count)]

    async def browse(
        self, section: str, page: int, http: object
    ) -> tuple[list[SearchResult], bool]:
        return [_card("p1", i) for i in range(self.count)], False


async def _sweep(store: BaselineStore, gateway: Any, *, day: int) -> MonitorReport:
    """One nightly run against the fakes."""
    return await run_once(
        day_offset=day,
        store=store,
        gateway=gateway,
        report_path=None,
        http=_FakeHttp(),
    )


@pytest.mark.asyncio
async def test_a_moved_shape_converges_and_closes_its_issue(tmp_path) -> None:
    """#382/#383: the provider serves fine, only the calibrated band
    disagrees — and the band follows a provider on HEALTHY passes only,
    so the stale band failed every sweep forever while the issue stayed
    open. Three consecutive shape observations adopt the new shape:
    filed once (the #288 rule), then closed as an accepted change."""
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    # Sweep 1: 5 cards calibrate the baseline (count_low = 5).
    _seed(_ListingProvider(5))
    first = await _sweep(store, gateway, day=0)
    assert first.providers["p1"]["ok"] is True
    assert first.providers["p1"]["reason"] == "baseline (first calibration)"

    # The provider shrinks to 3 cards: healthy, over the hard floor,
    # under the calibrated low — a SHAPE failure on every sweep from
    # here on, exactly the state the issues were left in.
    _seed(_ListingProvider(3))
    second = await _sweep(store, gateway, day=1)
    assert second.failed == ["p1"]
    assert gateway.opened == []  # first failure logs only (ticket #288)

    third = await _sweep(store, gateway, day=2)
    assert third.failed == ["p1"]
    assert gateway.opened == [("p1", "sweep")]  # second consecutive failure files

    fourth = await _sweep(store, gateway, day=3)
    assert fourth.failed == []  # CONVERGED — no longer a failure
    assert fourth.providers["p1"]["ok"] is True
    assert fourth.providers["p1"]["recalibrated"] is True
    assert "recalibrated after 3 shape sweeps" in fourth.providers["p1"]["reason"]
    assert gateway.closed == [("p1", "42")]
    # An accepted change is stated as one — never as a recovery.
    assert "accepted change" in gateway.comment_bodies[-1]
    assert "Recalibrated" in gateway.close_comments[-1]
    assert "Recovered" not in gateway.close_comments[-1]

    state = store.get("p1")
    assert state.signature is not None
    assert state.signature.count_low == 3  # the baseline followed the provider
    assert (state.consecutive_failures, state.shape_streak) == (0, 0)

    # And the next sweep is plainly healthy: nothing filed, nothing closed.
    fifth = await _sweep(store, gateway, day=4)
    assert fifth.failed == []
    assert fifth.providers["p1"]["recalibrated"] is False
    assert gateway.opened == [("p1", "sweep")]
    assert gateway.closed == [("p1", "42")]


@pytest.mark.asyncio
async def test_a_broken_provider_never_recalibrates(tmp_path) -> None:
    """Convergence is for shapes, not for silence: a provider serving
    nothing keeps failing and keeps its issue open however many sweeps
    pass — a count of zero is not a shape to adopt."""
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")
    _seed(_FailProvider())

    reports = [await _sweep(store, gateway, day=day) for day in range(5)]

    assert [r.failed for r in reports] == [["p1"]] * 5
    assert all(r.providers["p1"]["recalibrated"] is False for r in reports)
    assert gateway.opened == [("p1", "sweep")]  # filed once, then reused
    assert gateway.closed == []
    assert store.get("p1").consecutive_failures == 5


@pytest.mark.asyncio
async def test_a_broken_sweep_resets_the_convergence_clock(tmp_path) -> None:
    """A provider alternating between a moved shape and silence must not
    recalibrate: only CONSECUTIVE shape failures count, so a band that
    would hide the next failure is never adopted on the strength of a
    provider that has not settled."""
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    _seed(_ListingProvider(5))
    await _sweep(store, gateway, day=0)

    _seed(_ListingProvider(3))
    await _sweep(store, gateway, day=1)  # shape 1
    await _sweep(store, gateway, day=2)  # shape 2 → issue filed
    _seed(_FailProvider())
    await _sweep(store, gateway, day=3)  # BROKEN resets the clock
    _seed(_ListingProvider(3))
    await _sweep(store, gateway, day=4)  # shape 1
    unrecalibrated = await _sweep(store, gateway, day=5)  # shape 2 again

    assert unrecalibrated.providers["p1"]["recalibrated"] is False
    assert unrecalibrated.failed == ["p1"]
    assert gateway.closed == []  # the issue stays open

    settled = await _sweep(store, gateway, day=6)  # shape 3, consecutive this time
    assert settled.providers["p1"]["recalibrated"] is True
    assert gateway.closed == [("p1", "42")]


# ------------------------------------------------------- playback axis
#
# The axis the probe cannot see: the provider's listing (and its deep probe)
# answer, so every probe verdict is green, while the client reports the bytes
# never played. It runs on the SAME rule — first fault logs only, second
# consecutive one files — and carries its own issue, so the two axes never
# close each other's.


@pytest.mark.asyncio
async def test_the_sweep_reads_the_playback_axis_from_the_health_url(tmp_path) -> None:
    """The wiring itself: with a URL configured, a sweep READS the surface —
    the nightly unit needs no injected snapshot, and the reads are the window
    (each sweep re-reads the counters it will compare against next time)."""
    _seed(_OkProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")
    http = _FakeHttp(playback={"p2": {"served": 5, "no_progress": 5}})
    url = "http://api.test/api/health"

    for day in range(3):
        http = _FakeHttp(
            playback={"p2": {"served": 5 + day * 6, "no_progress": 5 + day * 6}}
        )
        report = await run_once(
            day_offset=day,
            store=store,
            gateway=gateway,
            report_path=None,
            http=http,
            health_url=url,
        )
        assert http.get_urls == [url]

    assert report.playback["p2"]["verdict"] == "no-progress"
    assert report.attestation_error is None
    assert gateway.opened == [("p2", "playback")]


@pytest.mark.asyncio
async def test_no_health_url_is_reported_and_judges_nothing(tmp_path) -> None:
    """An axis nobody read must not look like an axis that found nothing: the
    report names it, and no lane is accused or acquitted."""
    _seed(_OkProvider())
    store = BaselineStore(tmp_path / "state.json")

    report = await run_once(
        day_offset=0,
        store=store,
        gateway=_RecorderGateway(),
        report_path=None,
        http=_FakeHttp(),
        health_url=None,
    )

    assert report.playback == {}
    assert report.attestation_error == "no health URL configured"
    assert report.failed == []


@pytest.mark.asyncio
async def test_an_unreachable_surface_is_no_evidence_not_a_fault(tmp_path) -> None:
    """The API being down is not the provider failing: the sweep reports the
    unread surface and judges nothing."""

    class _DeadHttp(_FakeHttp):
        async def get(self, url: str) -> Any:
            raise httpx.ConnectError("connection refused")

    _seed(_OkProvider())
    report = await run_once(
        day_offset=0,
        store=BaselineStore(tmp_path / "state.json"),
        gateway=_RecorderGateway(),
        report_path=None,
        http=_DeadHttp(),
        health_url="http://api.test/api/health",
    )

    assert report.playback == {}
    assert report.attestation_error is not None
    assert "ConnectError" in report.attestation_error
    assert report.failed == []
    assert report.providers["p2"]["ok"] is True


def _play(served: int, no_progress: int, *, provider: str = "p2") -> PlaySnapshot:
    """A health-surface snapshot with one lane's counters."""
    return PlaySnapshot(
        providers={provider: PlayCounters(served=served, no_progress=no_progress)}
    )


async def _play_sweep(
    store: BaselineStore, gateway: Any, *, day: int, play: PlaySnapshot
) -> MonitorReport:
    """One nightly run judging an injected playback snapshot."""
    return await run_once(
        day_offset=day,
        store=store,
        gateway=gateway,
        report_path=None,
        http=_FakeHttp(),
        play=play,
    )


@pytest.mark.asyncio
async def test_the_playback_axis_files_on_the_second_consecutive_fault(tmp_path) -> None:
    """Served but never played: the first faulting sweep logs only, the
    second consecutive one files — and the probe verdict stays HEALTHY,
    because this fault is not the listing's."""
    _seed(_OkProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    # The first sweep only establishes the reference window.
    calibration = await _play_sweep(store, gateway, day=0, play=_play(5, 5))
    assert calibration.playback["p2"]["verdict"] == "no-evidence"

    first = await _play_sweep(store, gateway, day=1, play=_play(11, 11))
    assert gateway.opened == []
    assert first.providers["p2"]["ok"] is True  # the probe axis is unbothered
    assert first.playback["p2"]["verdict"] == "no-progress"
    assert first.playback["p2"]["consecutive_failures"] == 1
    assert first.failed == ["p2"]  # a fault fails the sweep, issue or no issue

    second = await _play_sweep(store, gateway, day=2, play=_play(18, 18))
    assert gateway.opened == [("p2", "playback")]
    assert second.providers["p2"]["ok"] is True
    assert store.get("p2").play_failures == 2
    # The body carries the window's evidence, not a bare claim.
    assert "7 reports for 7 served" in gateway.opened_bodies[0]
    assert "consecutive sweeps" in gateway.opened_bodies[0]


@pytest.mark.asyncio
async def test_plays_happening_again_close_the_playback_issue(tmp_path) -> None:
    """A window whose streams DID play resets the counter and closes."""
    _seed(_OkProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    for day, counters in enumerate([(5, 5), (11, 11), (18, 18)]):
        await _play_sweep(store, gateway, day=day, play=_play(*counters))
    assert gateway.opened == [("p2", "playback")]

    recovered = await _play_sweep(store, gateway, day=3, play=_play(25, 18))

    assert recovered.playback["p2"]["verdict"] == "clean"
    assert recovered.failed == []
    assert store.get("p2").play_failures == 0
    assert gateway.closed == [("p2", "43")]
    assert "plays this provider's streams again" in gateway.close_comments[-1]


@pytest.mark.asyncio
async def test_a_sweep_with_no_traffic_leaves_the_streak_alone(tmp_path) -> None:
    """A night nobody played tells the axis nothing: it is neither the second
    consecutive fault nor a recovery, so neither an issue nor a close. The
    streak counts OBSERVING sweeps, which is what makes a low-traffic lane
    able to reach a verdict at all."""
    _seed(_OkProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    await _play_sweep(store, gateway, day=0, play=_play(5, 5))
    await _play_sweep(store, gateway, day=1, play=_play(11, 11))  # fault 1

    # Same counters as last sweep: nobody played at all.
    quiet = await _play_sweep(store, gateway, day=2, play=_play(11, 11))
    assert quiet.playback["p2"]["verdict"] == "no-evidence"
    assert quiet.failed == []
    assert store.get("p2").play_failures == 1  # neither bumped nor reset
    assert gateway.opened == []

    # The next night WITH traffic that still never plays is the second fault.
    await _play_sweep(store, gateway, day=3, play=_play(18, 18))
    assert gateway.opened == [("p2", "playback")]


@pytest.mark.asyncio
async def test_a_close_is_not_repeated_every_later_quiet_sweep(tmp_path) -> None:
    """An open issue is closed once: the tracker retires it, and the monitor
    must not keep commenting on a settled lane (the probe axis's rule)."""
    _seed(_OkProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    for day, counters in enumerate([(5, 5), (11, 11), (18, 18), (25, 18)]):
        await _play_sweep(store, gateway, day=day, play=_play(*counters))
    assert gateway.closed == [("p2", "43")]

    await _play_sweep(store, gateway, day=4, play=_play(32, 18))
    assert gateway.closed == [("p2", "43")]


@pytest.mark.asyncio
async def test_the_playback_axis_judges_lanes_the_probes_never_cover(tmp_path) -> None:
    """The axis is not probing, so the lanes the probes exclude are judged
    here — uakino (``EXCLUDED_PROVIDER_IDS``) and the torrent engine. Their
    exclusion is about not warming a second browser session; these verdicts
    come from the API's own playback records, and the plays of a torrent
    lane failing is exactly the fault no listing probe can reach."""
    _seed(_OkProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")
    engine = "yts:engine"

    # A first observation cannot accuse: the reference counters are stored.
    first = await _play_sweep(store, gateway, day=0, play=_play(4, 4, provider=engine))
    assert first.playback[engine]["verdict"] == "no-evidence"
    assert engine not in first.providers  # never probed, judged all the same

    await _play_sweep(store, gateway, day=1, play=_play(9, 9, provider=engine))
    assert gateway.opened == []  # first fault logs only
    final = await _play_sweep(store, gateway, day=2, play=_play(15, 15, provider=engine))

    assert gateway.opened == [(engine, "playback")]
    assert final.failed == [engine]
    assert engine not in final.providers


@pytest.mark.asyncio
async def test_the_unknown_bucket_is_never_judged(tmp_path) -> None:
    """Ids that name no lane (malformed ids, cold group keys) share one
    bucket: nothing to file against, so no verdict and no issue."""
    _seed(_OkProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")


    def _snapshot(unknown: tuple[int, int], p2: tuple[int, int]) -> PlaySnapshot:
        return PlaySnapshot(
            providers={
                UNKNOWN_PROVIDER_KEY: PlayCounters(*unknown),
                "p2": PlayCounters(*p2),
            }
        )

    first = await _play_sweep(store, gateway, day=0, play=_snapshot((9, 9), (1, 0)))
    assert UNKNOWN_PROVIDER_KEY not in first.playback

    # The unknown bucket's window WOULD fault (9 more served, all at zero);
    # the lane beside it has genuinely played, so neither fails the sweep.
    second = await _play_sweep(store, gateway, day=1, play=_snapshot((18, 18), (3, 0)))

    assert UNKNOWN_PROVIDER_KEY not in second.playback
    assert second.playback["p2"]["verdict"] == "clean"
    assert second.failed == []


@pytest.mark.asyncio
async def test_no_issues_skips_playback_tracker_calls(tmp_path) -> None:
    """--no-issues judges the axis without touching the tracker, exactly as
    it does for the probe axis."""
    _seed(_OkProvider())
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    for day in range(3):
        await run_once(
            day_offset=day,
            store=store,
            gateway=gateway,
            report_path=None,
            skip_issues=True,
            http=_FakeHttp(),
            play=_play(5 + day * 6, 5 + day * 6),
        )

    assert gateway.opened == []
    assert gateway.find_calls == []
    assert store.get("p2").play_failures == 2  # the report still carries it


@pytest.mark.asyncio
async def test_a_tracker_outage_cannot_crash_the_sweep_on_either_axis(tmp_path) -> None:
    """The monitor's contract: a tracker outage is logged, never fatal — the
    report and the state still land. The lookup is the call that can fail."""

    class _DeadGateway(_RecorderGateway):
        def find_open(
            self, provider_id: str, fault: DriftFault = "sweep"
        ) -> str | None:
            raise RuntimeError("gh: authentication failed")

    _seed(_FailProvider())
    store = BaselineStore(tmp_path / "state.json")
    gateway = _DeadGateway()

    # Two sweep-axis failures (the second would file) plus two play faults.
    for day in (0, 1):
        report = await run_once(
            day_offset=day,
            store=store,
            gateway=gateway,
            report_path=None,
            http=_FakeHttp(),
            play=_play(5 + day * 6, 5 + day * 6, provider="p1"),
        )

    assert report.providers["p1"]["ok"] is False
    assert gateway.opened == []
    assert store.get("p1").play_failures >= 1


@pytest.mark.asyncio
async def test_each_axis_closes_only_its_own_issue(tmp_path) -> None:
    """The invariant the second fault kind exists for: a listing that
    recovers must NOT close a playback issue (the streams still do not
    play), and playback recovering must not close a listing issue."""
    gateway = _RecorderGateway()
    store = BaselineStore(tmp_path / "state.json")

    # p1's listing is broken AND its streams never play.
    _seed(_FailProvider())
    await _play_sweep(store, gateway, day=0, play=_play(5, 5, provider="p1"))
    await _play_sweep(store, gateway, day=1, play=_play(11, 11, provider="p1"))
    await _play_sweep(store, gateway, day=2, play=_play(18, 18, provider="p1"))
    assert gateway.opened == [("p1", "sweep"), ("p1", "playback")]

    # The listing recovers while the streams still never play.
    _seed(_OkP1Provider())
    listing_back = await _play_sweep(store, gateway, day=3, play=_play(24, 24, provider="p1"))

    assert listing_back.providers["p1"]["ok"] is True
    assert gateway.closed == [("p1", "42")]  # the SWEEP issue only
    assert gateway.find_open("p1", "playback") == "43"  # still open
    assert listing_back.failed == ["p1"]  # the playback fault still fails it

    # Now the streams play again: the playback issue closes too.
    await _play_sweep(store, gateway, day=4, play=_play(31, 24, provider="p1"))
    assert gateway.closed == [("p1", "42"), ("p1", "43")]
