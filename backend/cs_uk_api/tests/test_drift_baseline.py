"""Drift baseline + verdict tests (spec #285, ticket #287; spec #363).

The verdict logic is pure: probe results + a signature in, a verdict
out. Cases pinned: first-run calibration never trips; a healthy pass
updates the signature (band widens for growth); zero items, missing
fields, count below the calibrated low, and a significant form/style
leaving the band all trip; the state file persists consecutive-failure
counters across store instances in the shared ``{"version": 1,
"data": ...}`` envelope (spec #363), and a legacy bare-payload file or
any corrupt content degrades to a fresh recalibration — never a crash.
"""

from __future__ import annotations

import json
import logging

import pytest

from cs_uk_api.drift.baseline import (
    BaselineStore,
    Signature,
    update_signature,
    verdict_for,
)
from cs_uk_api.drift.probe import ListingProbeResult
from cs_uk_api.models import SearchResult


def _card(i: int, *, form: str = "movie", title: str | None = None, url: str | None = None) -> SearchResult:
    # ``or`` fallbacks would mask an explicitly-empty title/url, which
    # the missing-fields verdicts depend on — use explicit None checks.
    return SearchResult(
        id=f"p1:ext-{i}",
        provider="p1",
        form=form,  # type: ignore[arg-type]
        title=title if title is not None else f"Title {i}",
        url=url if url is not None else f"https://p1.example/{i}",
    )


def _listing(cards: list[SearchResult], *, ok: bool = True, error: str | None = None) -> ListingProbeResult:
    return ListingProbeResult(provider_id="p1", ok=ok, error=error, cards=cards)


def _sig(*, low: int = 10, high: int = 10, forms: dict[str, float] | None = None, styles: dict[str, float] | None = None) -> Signature:
    return Signature(
        count_low=low,
        count_high=high,
        form_frac=forms if forms is not None else {"movie": 0.6, "series": 0.4},
        style_frac=styles if styles is not None else {},
        fields_ok=True,
    )


# ------------------------------------------------------------ first run


def test_first_run_calibrates_not_trips() -> None:
    """No signature yet → healthy, so a first pass seeds the baseline."""
    cards = [_card(i) for i in range(5)]
    verdict = verdict_for(_listing(cards), None)
    assert verdict.ok
    assert "baseline" in verdict.reason


# ------------------------------------------------------------ verdicts


def test_probe_error_trips() -> None:
    verdict = verdict_for(_listing([], ok=False, error="timeout: slow"), _sig())
    assert not verdict.ok
    assert "probe failed" in verdict.reason


def test_zero_items_trips() -> None:
    verdict = verdict_for(_listing([]), _sig())
    assert not verdict.ok
    assert "zero items" in verdict.reason


def test_missing_required_fields_trips() -> None:
    bad = [_card(1, title="")]
    verdict = verdict_for(_listing(bad), _sig())
    assert not verdict.ok
    assert "missing required fields" in verdict.reason


def test_missing_url_trips() -> None:
    bad = [_card(1, url="")]
    verdict = verdict_for(_listing(bad), _sig())
    assert not verdict.ok
    assert "missing required fields" in verdict.reason


def test_count_below_calibrated_low_trips() -> None:
    """A listing under the calibrated low (but over the hard floor)
    leaves the count band → drift."""
    cards = [_card(i) for i in range(4)]  # 4 < low 10
    verdict = verdict_for(_listing(cards), _sig(low=10))
    assert not verdict.ok
    assert "below calibrated low" in verdict.reason


def test_hard_floor_trips_even_without_signature() -> None:
    """Fewer than MIN_CARDS items trips even on the first run."""
    verdict = verdict_for(_listing([_card(1)]), None)
    assert not verdict.ok
    assert "hard floor" in verdict.reason


def test_growth_stays_healthy() -> None:
    """Growth beyond the calibrated high never trips (catalog growth)."""
    # Same 0.6/0.4 movie:series split, just 3x more cards: a bigger
    # catalog with the same shape is healthy, not drift.
    cards = [_card(i, form="movie") for i in range(18)] + [
        _card(100 + i, form="series") for i in range(12)
    ]
    verdict = verdict_for(_listing(cards), _sig(low=5, high=10))
    assert verdict.ok


def test_form_leaving_band_trips() -> None:
    """A significant form vanishing (kinovezha kind flip: series → all
    movie) leaves the distribution band → drift."""
    sig = _sig(forms={"movie": 0.5, "series": 0.5})
    cards = [_card(i, form="movie") for i in range(10)]  # series share 0.0
    verdict = verdict_for(_listing(cards), sig)
    assert not verdict.ok
    assert "left the band" in verdict.reason


def test_minor_form_leaving_does_not_trip() -> None:
    """A never-significant form disappearing is absorbed (no trip)."""
    sig = _sig(forms={"movie": 0.95, "series": 0.05})
    cards = [_card(i, form="movie") for i in range(10)]
    verdict = verdict_for(_listing(cards), sig)
    assert verdict.ok


def test_style_leaving_band_trips() -> None:
    """A significant style (anime) vanishing trips when it was real."""
    sig = _sig(styles={"anime": 0.8, "cartoon": 0.2})
    cards = [_card(i, form="series") for i in range(10)]  # no styles
    verdict = verdict_for(_listing(cards), sig)
    assert not verdict.ok
    assert "left the band" in verdict.reason


def test_plain_provider_with_no_style_band_skips_style_check() -> None:
    """A provider whose healthy passes were all plain live-action has an
    empty style band — nothing to drift against."""
    sig = _sig(forms={"movie": 1.0}, styles={})
    cards = [_card(i, form="movie") for i in range(10)]
    verdict = verdict_for(_listing(cards), sig)
    assert verdict.ok


# ------------------------------------------------------------ signature


def test_update_signature_first_pass_seeds() -> None:
    cards = [_card(i, form="movie") for i in range(5)]
    sig = update_signature(None, _listing(cards))
    assert sig.count_low == 5
    assert sig.count_high == 5
    assert sig.form_frac == {"movie": 1.0}


def test_update_signature_widens_band_for_growth() -> None:
    """A healthy bigger pass widens high without raising low."""
    old = _sig(low=3, high=10)
    cards = [_card(i) for i in range(25)]
    sig = update_signature(old, _listing(cards))
    assert sig.count_low == 3
    assert sig.count_high == 25
    # Distribution follows the latest healthy pass.
    assert sig.form_frac == {"movie": 1.0}


def test_update_signature_tracks_shrinking_healthy_low() -> None:
    """A healthy smaller pass lowers the calibrated low."""
    old = _sig(low=10, high=30)
    cards = [_card(i) for i in range(8)]
    sig = update_signature(old, _listing(cards))
    assert sig.count_low == 8
    assert sig.count_high == 30


# ------------------------------------------------------------ state file


def test_state_persists_counters_across_store_instances(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = BaselineStore(path)
    state = store.get("p1")
    state.consecutive_failures = 2
    store.save()

    reloaded = BaselineStore(path)
    assert reloaded.get("p1").consecutive_failures == 2


def test_state_persists_signature(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = BaselineStore(path)
    store.get("p1").signature = _sig(low=7, high=9)
    store.save()

    reloaded = BaselineStore(path)
    sig = reloaded.get("p1").signature
    assert sig is not None
    assert sig.count_low == 7
    assert sig.count_high == 9


def test_state_file_carries_versioned_envelope(tmp_path) -> None:
    """spec #363: the baseline file is the shared VersionedFileStore
    envelope — a schema change can never be silently mis-read as old
    state again."""
    path = tmp_path / "state.json"
    store = BaselineStore(path)
    state = store.get("p1")
    state.consecutive_failures = 1
    store.save()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert doc["data"]["p1"]["consecutive_failures"] == 1
    # No temp leftovers (atomic write).
    assert [f.name for f in tmp_path.iterdir() if ".tmp" in f.name] == []


def test_corrupt_state_file_starts_fresh(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = BaselineStore(path)
    assert store.get("p1").consecutive_failures == 0
    assert store.get("p1").signature is None


def test_legacy_bare_payload_degrades_to_fresh_recalibration(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """spec #363: the pre-envelope file was the bare {provider: state}
    map — no version token. It must degrade to a fresh store (warning
    logged, first healthy pass recalibrates) instead of being silently
    mis-read across a schema change."""
    path = tmp_path / "state.json"
    legacy = {
        "p1": {
            "consecutive_failures": 5,
            "signature": {"count_low": 3, "count_high": 30, "fields_ok": True},
        }
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cs_uk_api.drift.baseline"):
        store = BaselineStore(path)
    assert store.get("p1").consecutive_failures == 0
    assert store.get("p1").signature is None
    assert caplog.text.strip() != ""  # the degradation is visible


def test_save_never_raises_on_unwritable_location(tmp_path) -> None:
    """spec #363: save() must not raise — the nightly run logs and moves
    on even when the state directory cannot be written."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    store = BaselineStore(blocker / "child" / "state.json")
    store.get("p1").consecutive_failures = 1
    store.save()  # must not raise
