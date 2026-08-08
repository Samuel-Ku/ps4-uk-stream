"""Tests for the episode-rail sweep logic (issue #136).

The pure decision logic in ``cs_uk_api.sweep_episode_rail`` is driven
with canned ``HopResult`` responses so the four-hop walk — and above all
the empty-200 hazard — is pinned without booting a server or hitting any
provider.
"""
from __future__ import annotations

import pytest

from cs_uk_api.sweep_episode_rail import (
    FAIL,
    NO_EPISODES,
    OK,
    HopResult,
    ProviderResult,
    SeriesResult,
    pick_episode_id,
    pick_season_id,
    render_report,
    sweep_home,
    walk_series,
)


def _env(items: list[dict[str, object]], status: int = 200) -> HopResult:
    """A Jellyfin ``Result<T>`` envelope response."""
    return HopResult(status=status, json={"Items": items, "TotalRecordCount": len(items)})


def _ok(items: list[dict[str, object]]) -> HopResult:
    return _env(items)


def _err(status: int, error: str | None = None) -> HopResult:
    return HopResult(status=status, json=None, error=error)


# ---------- pickers ----------


def test_pick_season_id_takes_first() -> None:
    hop = _ok([{"Id": "g1:k:S1"}, {"Id": "g1:k:S2"}])
    assert pick_season_id(hop) == "g1:k:S1"


def test_pick_season_id_none_when_empty() -> None:
    assert pick_season_id(_ok([])) is None


def test_pick_season_id_none_on_error() -> None:
    assert pick_season_id(_err(500)) is None


def test_pick_episode_id_takes_first() -> None:
    hop = _ok([{"Id": "p1:s1e1"}, {"Id": "p1:s1e2"}])
    assert pick_episode_id(hop) == "p1:s1e1"


def test_pick_episode_id_none_when_empty() -> None:
    assert pick_episode_id(_ok([])) is None


# ---------- the empty-200 hazard (the whole point of the sweep) ----------


def test_walk_flags_no_episodes_when_rail_empty() -> None:
    """A series whose Seasons resolves but yields no seasons is NOT ✅.

    This is the trap: ``/Shows/{g1}/Episodes`` without a seasonId would
    have returned 200 with an empty list — a false pass. Going through
    Seasons first turns that into NO_EPISODES.
    """
    res = walk_series(
        "Серіал",
        "g1:k",
        seasons=_ok([]),  # Seasons hop 200 but empty
        episodes=_ok([]),
        playback=_ok([{"Id": "x"}]),
        stream=HopResult(status=200),
    )
    assert res.status == NO_EPISODES
    assert res.hop == "Shows"
    assert res.season_id is None


def test_walk_flags_no_episodes_when_episodes_empty() -> None:
    """Seasons resolve, but the episode-rail exposes zero episodes (BUG-2)."""
    res = walk_series(
        "Серіал",
        "g1:k",
        seasons=_ok([{"Id": "g1:k:S1"}]),
        episodes=_ok([]),  # rail resolves a season but no episodes
        playback=_ok([{"Id": "x"}]),
        stream=HopResult(status=200),
    )
    assert res.status == NO_EPISODES
    assert res.episode_id is None
    assert res.season_id == "g1:k:S1"


def test_walk_full_pass_through_all_hops() -> None:
    res = walk_series(
        "Серіал",
        "g1:k",
        seasons=_ok([{"Id": "g1:k:S1"}]),
        episodes=_ok([{"Id": "p1:s1e1"}]),
        playback=_ok([{"Id": "p1:s1e1"}]),
        stream=HopResult(status=200),
    )
    assert res.status == OK
    assert res.failed is False
    assert res.episode_id == "p1:s1e1"
    assert res.season_id == "g1:k:S1"
    assert res.stream_status == 200


# ---------- per-hop failure attribution ----------


def test_walk_fails_at_seasons() -> None:
    res = walk_series(
        "Серіал",
        "g1:k",
        seasons=_err(404, "item_unavailable"),
        episodes=_ok([{"Id": "p1:s1e1"}]),
        playback=_ok([{"Id": "x"}]),
        stream=HopResult(status=200),
    )
    assert res.status == FAIL
    assert res.hop == "Shows"
    assert res.shows_status == 404
    assert res.error == "item_unavailable"


def test_walk_fails_at_episodes() -> None:
    res = walk_series(
        "Серіал",
        "g1:k",
        seasons=_ok([{"Id": "g1:k:S1"}]),
        episodes=_err(500),
        playback=_ok([{"Id": "x"}]),
        stream=HopResult(status=200),
    )
    assert res.status == FAIL
    assert res.hop == "Shows"
    assert res.episodes_status == 500
    assert res.error == "Episodes HTTP 500"


def test_walk_fails_at_playbackinfo() -> None:
    res = walk_series(
        "Серіал",
        "g1:k",
        seasons=_ok([{"Id": "g1:k:S1"}]),
        episodes=_ok([{"Id": "p1:s1e1"}]),
        playback=_err(404, "item_unavailable"),
        stream=HopResult(status=200),
    )
    assert res.status == FAIL
    assert res.hop == "PlaybackInfo"
    assert res.playback_status == 404


def test_walk_fails_at_stream() -> None:
    res = walk_series(
        "Серіал",
        "g1:k",
        seasons=_ok([{"Id": "g1:k:S1"}]),
        episodes=_ok([{"Id": "p1:s1e1"}]),
        playback=_ok([{"Id": "p1:s1e1"}]),
        stream=_err(404, "item_unavailable"),
    )
    assert res.status == FAIL
    assert res.hop == "stream"
    assert res.stream_status == 404


# ---------- provider roll-up + report ----------


def _series(status: str, hop: str = "stream", error: str | None = None) -> SeriesResult:
    return SeriesResult(title="t", group_key="g1:k", status=status, hop=hop, error=error)


def test_provider_rollup_counts() -> None:
    p = ProviderResult(provider="p1")
    p.add(_series(OK))
    p.add(_series(FAIL, "PlaybackInfo", "item_unavailable"))
    p.add(_series(NO_EPISODES, "Shows", "0 episodes"))
    assert p.series_tested == 3
    assert p.series_ok == 1
    assert p.series_failed == 1
    assert p.series_no_episodes == 1
    assert p.all_ok is False


def test_provider_skipped_renders_skip_row() -> None:
    p = ProviderResult(provider="coaninet", skipped=True, skip_reason="no series in home")
    report = render_report([p])
    assert "coaninet" in report
    assert "skip" in report
    assert "no series in home" in report


def test_render_report_marks_bug_with_hop() -> None:
    ok_p = ProviderResult(provider="uakino")
    ok_p.add(_series(OK))
    bug_p = ProviderResult(provider="kinotron")
    bug_p.add(_series(FAIL, "stream", "item_unavailable"))
    report = render_report([ok_p, bug_p])
    assert "✅" in report
    assert "🐛" in report
    assert "kinotron" in report
    assert "stream" in report


def test_render_report_no_episodes_is_warning_not_bug() -> None:
    p = ProviderResult(provider="serialno")
    p.add(_series(NO_EPISODES, "Shows", "0 episodes"))
    report = render_report([p])
    assert "⚠️" in report
    # The bug glyph appears only in the header column label; the verdict
    # cell for a no-episodes provider must be the warning, not a bug.
    data_row = [ln for ln in report.splitlines() if ln.startswith("| serialno")][0]
    assert "🐛" not in data_row


# ---------- live driver layer (sweep_home with a fake client) ----------


class _FakeClient:
    """A ``(url, headers) -> HopResult`` client with canned answers.

    Records the urls it was asked for so the driver's four-hop URL shape
    (Seasons -> Episodes?seasonId= -> PlaybackInfo -> stream) is pinned.
    """

    def __init__(self, answers: dict[str, HopResult]) -> None:
        self._answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], method: str = "GET") -> HopResult:
        from urllib.parse import unquote

        self.calls.append((url, method))
        # The driver percent-encodes ids; match on the decoded url so
        # canned (unquoted) answers still resolve.
        decoded = unquote(url)
        for prefix, hop in self._answers.items():
            if decoded.startswith(prefix):
                return hop
        return HopResult(status=404, error="unmapped url")


_HOME = {
    "rows": [
        {
            "title": "Серіали",
            "type": "series",
            "items": [
                {
                    "group_key": "g1:uakino:serial",
                    "title": "Серіал A",
                    "type": "series",
                    "providers": ["uakino"],
                },
                {
                    "group_key": "g1:serialno:broken",
                    "title": "Серіал B",
                    "type": "series",
                    "providers": ["serialno"],
                },
            ],
        }
    ]
}


def test_sweep_home_walks_four_hops_per_series() -> None:
    client = _FakeClient(
        {
            "http://h/Shows/g1:uakino:serial/Seasons": _ok([{"Id": "g1:uakino:serial:S1"}]),
            "http://h/Shows/g1:uakino:serial/Episodes": _ok([{"Id": "uakino:s1e1"}]),
            "http://h/Items/uakino:s1e1/PlaybackInfo": _ok([{"Id": "uakino:s1e1"}]),
            "http://h/Videos/uakino:s1e1/stream": HopResult(status=200),
            "http://h/Shows/g1:serialno:broken/Seasons": _ok([]),  # empty -> NO_EPISODES
        }
    )
    results = sweep_home(_HOME, client, "tok", "http://h", per_provider=1)
    assert {p.provider for p in results} == {"uakino", "serialno"}

    uakino = next(p for p in results if p.provider == "uakino")
    assert uakino.series_tested == 1
    assert uakino.all_ok

    serialno = next(p for p in results if p.provider == "serialno")
    assert serialno.series_no_episodes == 1
    # The episodes hop must have been hit WITHOUT a seasonId for the
    # broken provider (the empty-200 hazard path), so it is flagged
    # rather than reporting a false pass.
    from urllib.parse import unquote
    assert any("Episodes" in unquote(url) and "seasonId" not in unquote(url) for url, _ in client.calls)


def test_sweep_home_episodes_url_carries_season_id() -> None:
    client = _FakeClient(
        {
            "http://h/Shows/g1:uakino:serial/Seasons": _ok([{"Id": "g1:uakino:serial:S1"}]),
            "http://h/Shows/g1:uakino:serial/Episodes": _ok([{"Id": "uakino:s1e1"}]),
            "http://h/Items/uakino:s1e1/PlaybackInfo": _ok([{"Id": "uakino:s1e1"}]),
            "http://h/Videos/uakino:s1e1/stream": HopResult(status=200),
        }
    )
    sweep_home(_HOME, client, "tok", "http://h", per_provider=1)
    from urllib.parse import unquote
    season_calls = [unquote(url) for url, _ in client.calls if "Episodes?seasonId=" in unquote(url)]
    assert season_calls and "g1:uakino:serial:S1" in season_calls[0]

def test_sweep_home_posts_to_playbackinfo() -> None:
    client = _FakeClient(
        {
            "http://h/Shows/g1:uakino:serial/Seasons": _ok([{"Id": "g1:uakino:serial:S1"}]),
            "http://h/Shows/g1:uakino:serial/Episodes": _ok([{"Id": "uakino:s1e1"}]),
            "http://h/Items/uakino:s1e1/PlaybackInfo": _ok([{"Id": "uakino:s1e1"}]),
            "http://h/Videos/uakino:s1e1/stream": HopResult(status=200),
        }
    )
    sweep_home(_HOME, client, "tok", "http://h", per_provider=1)
    playback_calls = [(u, m) for u, m in client.calls if "PlaybackInfo" in u]
    assert playback_calls and playback_calls[0][1] == "POST"
    # Rail hops stay GET.
    assert all(m == "GET" for u, m in client.calls if "PlaybackInfo" not in u and "stream" not in u)


def test_sweep_home_limits_per_provider() -> None:
    client = _FakeClient(
        {
            "http://h/Shows/g1:uakino:serial/Seasons": _ok([{"Id": "g1:uakino:serial:S1"}]),
            "http://h/Shows/g1:uakino:serial/Episodes": _ok([{"Id": "uakino:s1e1"}]),
            "http://h/Items/uakino:s1e1/PlaybackInfo": _ok([{"Id": "uakino:s1e1"}]),
            "http://h/Videos/uakino:s1e1/stream": HopResult(status=200),
        }
    )
    # Only one series in the fixture; per_provider=1 must not loop forever
    # or ask for more than exists.
    results = sweep_home(_HOME, client, "tok", "http://h", per_provider=5)
    assert next(p for p in results if p.provider == "uakino").series_tested == 1


def test_series_items_by_provider_groups_series_only() -> None:
    from cs_uk_api.sweep_episode_rail import _series_items_by_provider

    home = {
        "rows": [
            {
                "title": "Фільми",
                "type": "movie",
                "items": [
                    {"group_key": "g1:m", "title": "Ф", "type": "movie", "providers": ["uakino"]}
                ],
            },
            {
                "title": "Серіали",
                "type": "series",
                "items": [
                    {"group_key": "g1:s", "title": "С", "type": "series", "providers": ["uakino"]}
                ],
            },
        ]
    }
    grouped = _series_items_by_provider(home)
    assert list(grouped.keys()) == ["uakino"]
    assert grouped["uakino"][0]["group_key"] == "g1:s"


def test_sweep_home_skips_provider_with_no_series() -> None:
    client = _FakeClient({})
    # "missing" provider is registered but has no series in the home.
    results = sweep_home(
        _HOME, client, "tok", "http://h", per_provider=3, registered=["uakino", "missing"]
    )
    skipped = [p for p in results if p.skipped]
    assert any(p.provider == "missing" and "no series" in (p.skip_reason or "") for p in skipped)
    # The present provider still gets walked.
    assert any(p.provider == "uakino" and not p.skipped for p in results)
