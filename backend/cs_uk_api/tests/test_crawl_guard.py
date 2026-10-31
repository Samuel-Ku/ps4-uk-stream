"""Tests for the crawler's 403 circuit breaker (``cs_uk_api.crawl_guard``)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from cs_uk_api.crawl_guard import ProviderGuard, SkipInfo, classify

DAY = date(2026, 10, 1)


def test_three_consecutive_403_nights_trip_the_breaker(tmp_path: Path) -> None:
    guard = ProviderGuard(tmp_path / "crawl-guard.json")

    assert guard.record("kinotron", "403", today=DAY) is None
    assert guard.blocked("kinotron", today=DAY) is None
    assert guard.record("kinotron", "403", today=DAY + timedelta(days=1)) is None
    assert guard.blocked("kinotron", today=DAY + timedelta(days=1)) is None

    info = guard.record("kinotron", "403", today=DAY + timedelta(days=2))

    assert info == SkipInfo(3, (DAY + timedelta(days=9)).isoformat())
    assert guard.blocked("kinotron", today=DAY + timedelta(days=8)) is not None


def test_a_second_run_on_the_same_day_counts_once(tmp_path: Path) -> None:
    guard = ProviderGuard(tmp_path / "crawl-guard.json")
    guard.record("x", "403", today=DAY)
    guard.record("x", "403", today=DAY)
    guard.record("x", "403", today=DAY + timedelta(days=1))

    assert guard.blocked("x", today=DAY + timedelta(days=1)) is None
    assert guard.record("x", "403", today=DAY + timedelta(days=1)) is None


def test_a_successful_night_resets_the_streak(tmp_path: Path) -> None:
    guard = ProviderGuard(tmp_path / "crawl-guard.json")
    guard.record("x", "403", today=DAY)
    guard.record("x", "403", today=DAY + timedelta(days=1))
    guard.record("x", "ok", today=DAY + timedelta(days=2))
    assert guard.record("x", "403", today=DAY + timedelta(days=3)) is None
    assert guard.record("x", "403", today=DAY + timedelta(days=4)) is None

    assert guard.blocked("x", today=DAY + timedelta(days=5)) is None


def test_a_non_403_failure_breaks_the_streak(tmp_path: Path) -> None:
    """A timeout night is not a block verdict; the retry passes handle it."""
    guard = ProviderGuard(tmp_path / "crawl-guard.json")
    guard.record("x", "403", today=DAY)
    guard.record("x", "other", today=DAY + timedelta(days=1))
    guard.record("x", "403", today=DAY + timedelta(days=2))

    assert guard.blocked("x", today=DAY + timedelta(days=3)) is None


def test_the_breaker_half_opens_after_the_cooldown(tmp_path: Path) -> None:
    guard = ProviderGuard(tmp_path / "crawl-guard.json")
    for offset in range(3):
        guard.record("x", "403", today=DAY + timedelta(days=offset))
    info = guard.blocked("x", today=DAY)
    assert info is not None
    until = date.fromisoformat(info.retry_after)

    assert guard.blocked("x", today=until - timedelta(days=1)) is not None
    assert guard.blocked("x", today=until) is None


def test_a_probe_that_is_still_blocked_re_trips_immediately(tmp_path: Path) -> None:
    guard = ProviderGuard(tmp_path / "crawl-guard.json")
    for offset in range(3):
        guard.record("x", "403", today=DAY + timedelta(days=offset))
    probe_day = DAY + timedelta(days=9)

    info = guard.record("x", "403", today=probe_day)

    assert info is not None
    assert info.nights == 4
    assert info.retry_after == (probe_day + timedelta(days=7)).isoformat()


def test_the_verdict_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "crawl-guard.json"
    first = ProviderGuard(path)
    for offset in range(3):
        first.record("x", "403", today=DAY + timedelta(days=offset))
    first.save()

    second = ProviderGuard(path)

    assert second.blocked("x", today=DAY + timedelta(days=4)) is not None


def test_a_corrupt_state_file_starts_clean(tmp_path: Path) -> None:
    path = tmp_path / "crawl-guard.json"
    path.write_text("{not json", encoding="utf-8")

    guard = ProviderGuard(path)

    assert guard.blocked("x", today=DAY) is None
    assert guard.record("x", "403", today=DAY) is None


def test_zero_disables_the_breaker(tmp_path: Path) -> None:
    guard = ProviderGuard(tmp_path / "crawl-guard.json", block_after=0)
    for offset in range(10):
        assert guard.record("x", "403", today=DAY + timedelta(days=offset)) is None
    assert guard.blocked("x", today=DAY + timedelta(days=20)) is None


def test_classify_only_counts_uniform_403s() -> None:
    assert classify(ok=True, sections=5, errors=()) == "ok"
    assert classify(ok=False, sections=0, errors=("no browsable sections",)) == "other"
    assert (
        classify(
            ok=False,
            sections=4,
            errors=("films p1: status 403", "serials p1: 403 Forbidden"),
        )
        == "403"
    )
    assert (
        classify(
            ok=False,
            sections=4,
            errors=("films p1: status 403", "serials p1: timed out"),
        )
        == "other"
    )


def test_a_tripped_provider_is_left_out_of_the_walk(tmp_path: Path) -> None:
    guard = ProviderGuard(tmp_path / "crawl-guard.json")
    for offset in range(3):
        guard.record("blocked", "403", today=DAY + timedelta(days=offset))

    active, skipped = guard.partition(
        ["healthy", "blocked", "dead"], today=DAY + timedelta(days=4)
    )

    assert active == ["healthy", "dead"]
    assert [provider for provider, _ in skipped] == ["blocked"]
