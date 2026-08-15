"""Episode-rail verification sweep (issue #136).

Pure, testable logic for the diagnostic sweep that walks the series
episode-rail path the PS4 client actually follows across every provider:

    /Shows/{g1}/Seasons
      -> pick a season id
    /Shows/{g1}/Episodes?seasonId={season}
      -> pick the first episode's wire id
    POST /Items/{ep_id}/PlaybackInfo
    GET  /Videos/{ep_id}/stream?static=true

The three "hops" — Shows/Episodes (the episode-rail), PlaybackInfo, and
stream — are each asserted to return 200; the first hop that fails is
recorded with its status + error so the fix ticket (#136's blocker) can
target each broken provider at the exact hop.

Why a dedicated sweep rather than the movie-only gate
======================================================
The g1-key PlaybackInfo 404 for a *series* is EXPECTED (D3 contract: a
series key is not playable; the client drills down through episodes).
So a movie-only PlaybackInfo gate passes even when the series
episode-rail is broken. This sweep targets the path the client walks
for series content.

The empty-200 hazard
====================
``/Shows/{g1}/Episodes`` WITHOUT a ``seasonId`` returns 200 with an
EMPTY ``Items`` list — the route defers to ``_hierarchy(None)`` which
answers an empty result (D5 tolerant answer). A naive sweep that hit
``Episodes`` with just the g1 key would therefore report a false
"✅ episode-rail" for EVERY provider while testing nothing. The sweep
must therefore go through ``Seasons`` first and only count an
episode-rail hop as real when it has a non-empty ``Items`` from a
resolved ``seasonId``.

The logic here is HTTP-agnostic: each hop is fed a ``HopResult``
(status + parsed JSON + error string) so the live runner can drive it
with real requests and the unit tests can drive it with canned
responses. No network, no providers, no server in this module.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .probe import (
    VERDICT_FAIL as FAIL,
)
from .probe import (
    VERDICT_NO_EPISODES as NO_EPISODES,
)
from .probe import (
    VERDICT_OK as OK,
)
from .probe import (
    attributed_provider,
    is_episodic_item,
)
from .versioned_store import atomic_write_text

#: The hops a series item must survive to be playable on PS4.
HOP_SHOWS = "Shows"
HOP_PLAYBACK = "PlaybackInfo"
HOP_STREAM = "stream"

#: HTTP status a hop must return to count as passed.
PASS_STATUS = 200


@dataclass(frozen=True)
class HopResult:
    """The outcome of one HTTP request, isolated from any client.

    ``status`` is the HTTP status code (or -1 for a transport error);
    ``json`` is the parsed JSON body when present; ``error`` carries a
    short human string for transport/parse failures. ``json`` is
    optional so tests can feed partial bodies without inventing fields.
    """

    status: int
    json: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == PASS_STATUS


@dataclass
class SeriesResult:
    """Per-series outcome of the four-hop walk."""

    title: str
    group_key: str
    hop: str = HOP_SHOWS  # the last hop attempted (HOP_*)
    status: str = OK  # OK | FAIL | NO_EPISODES
    shows_status: int | None = None
    episodes_status: int | None = None
    playback_status: int | None = None
    stream_status: int | None = None
    error: str | None = None
    episode_id: str | None = None
    season_id: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class ProviderResult:
    """Per-provider roll-up of the sweep (acceptance criterion)."""

    provider: str
    series_tested: int = 0
    series_ok: int = 0
    series_failed: int = 0
    series_no_episodes: int = 0
    skipped: bool = False
    skip_reason: str | None = None
    series: list[SeriesResult] = field(default_factory=list)

    def add(self, r: SeriesResult) -> None:
        self.series.append(r)
        self.series_tested += 1
        if r.status == OK:
            self.series_ok += 1
        elif r.status == FAIL:
            self.series_failed += 1
        elif r.status == NO_EPISODES:
            self.series_no_episodes += 1

    @property
    def all_ok(self) -> bool:
        return self.series_failed == 0 and self.series_no_episodes == 0 and self.series_tested > 0


def _items(hop: HopResult) -> list[dict[str, Any]]:
    """The ``Items`` array of a Jellyfin ``Result<T>`` envelope, or []."""
    if hop.json is None:
        return []
    items = hop.json.get("Items")
    return items if isinstance(items, list) else []


def pick_season_id(seasons: HopResult) -> str | None:
    """The first season id from a ``/Shows/{g1}/Seasons`` answer.

    Returns None when the hop failed or listed no seasons — the caller
    then records a NO_EPISODES (rail dead at the Seasons hop, no seasons
    to drill into).
    """
    if not seasons.ok:
        return None
    items = _items(seasons)
    if not items:
        return None
    return items[0].get("Id")


def pick_episode_id(episodes: HopResult) -> str | None:
    """The first episode wire id from a ``/Shows/{g1}/Episodes`` answer.

    Returns None when the hop failed or listed no episodes — the caller
    records NO_EPISODES (the rail resolved but exposed nothing playable).
    """
    if not episodes.ok:
        return None
    items = _items(episodes)
    if not items:
        return None
    return items[0].get("Id")


def walk_series(
    title: str,
    group_key: str,
    seasons: HopResult,
    episodes: HopResult,
    playback: HopResult,
    stream: HopResult,
) -> SeriesResult:
    """Walk one series through the four hops, recording the first failure.

    The contract: Seasons must yield at least one season, Episodes (with
    a real ``seasonId``) must yield at least one episode, PlaybackInfo
    and stream must both 200. The first hop that breaks sets ``status``
    and ``hop``; a rail that resolves but exposes zero episodes is
    NO_EPISODES (a real break — BUG-1/-2 style — not a false ✅).
    """
    res = SeriesResult(title=title, group_key=group_key)
    res.season_id = pick_season_id(seasons)

    if not seasons.ok:
        res.hop = HOP_SHOWS
        res.shows_status = seasons.status
        res.status = FAIL
        res.error = seasons.error or f"Seasons HTTP {seasons.status}"
        return res

    if res.season_id is None:
        # Rail reached the Seasons hop but exposed no seasons to drill
        # into — dead at the first hop (e.g. a provider whose series
        # listing never resolves seasons).
        res.hop = HOP_SHOWS
        res.shows_status = seasons.status
        res.status = NO_EPISODES
        res.error = "Seasons returned 0 seasons"
        return res

    if not episodes.ok:
        res.hop = HOP_SHOWS
        res.shows_status = seasons.status
        res.episodes_status = episodes.status
        res.status = FAIL
        res.error = episodes.error or f"Episodes HTTP {episodes.status}"
        return res

    res.episode_id = pick_episode_id(episodes)
    if res.episode_id is None:
        # Rail resolved a season but exposed zero episodes — the exact
        # break BUG-2 (serialno) / anitubeinua produce live.
        res.hop = HOP_SHOWS
        res.shows_status = seasons.status
        res.episodes_status = episodes.status
        res.status = NO_EPISODES
        res.error = "Episodes returned 0 episodes"
        return res

    if not playback.ok:
        res.hop = HOP_PLAYBACK
        res.shows_status = seasons.status
        res.episodes_status = episodes.status
        res.playback_status = playback.status
        res.status = FAIL
        res.error = playback.error or f"PlaybackInfo HTTP {playback.status}"
        return res

    if not stream.ok:
        res.hop = HOP_STREAM
        res.shows_status = seasons.status
        res.episodes_status = episodes.status
        res.playback_status = playback.status
        res.stream_status = stream.status
        res.status = FAIL
        res.error = stream.error or f"stream HTTP {stream.status}"
        return res

    res.hop = HOP_STREAM
    res.shows_status = seasons.status
    res.episodes_status = episodes.status
    res.playback_status = playback.status
    res.stream_status = stream.status
    res.status = OK
    return res


def render_report(providers: Sequence[ProviderResult]) -> str:
    """A per-provider Markdown table: ✅/🐛 + failing hop + error.

    Mirrors the shape of ``docs/diagnostics-2026-08-08.md`` so #136's
    blocker ticket can diff the episode-rail map against the search→
    content map already in that report.
    """
    lines: list[str] = []
    lines.append("| provider | series tested | series ✅ | 🐛 | ⚠️ no-eps | verdict | notes |")
    lines.append("| --- | ---: | ---: | ---: | ---: | --- | --- |")
    for p in providers:
        if p.skipped:
            lines.append(f"| {p.provider} | — | — | — | — | ⏭️ skip | {p.skip_reason or ''} |")
            continue
        if p.all_ok:
            verdict = "✅"
            note = ""
        elif p.series_failed:
            verdict = "🐛"
            note = "; ".join(
                f"{s.group_key}:{s.hop} {s.error}" for s in p.series if s.failed
            )
        else:
            verdict = "⚠️"
            note = "episode-rail resolves but exposes no episodes"
        lines.append(
            f"| {p.provider} | {p.series_tested} | {p.series_ok} | "
            f"{p.series_failed} | {p.series_no_episodes} | {verdict} | {note} |"
        )
    return "\n".join(lines)


def write_report(path: str, report: str) -> None:
    """Write the sweep report atomically (spec #323, Store T3 #326).

    A torn report is worse than the previous one: a reader (or a future
    drift diff over ``docs/sweep-episode-rail-<date>.md``) may open it
    mid-write. ``atomic_write_text`` (mkstemp + replace in the same
    directory) guarantees readers see the old file or the new one, never
    a half-written one; it never raises. Output bytes are identical to
    the old ``open(path, "w")`` write (report + trailing newline).
    """
    atomic_write_text(path, report + "\n")


# --------------------------------------------------------------------------
# Live driver layer
# --------------------------------------------------------------------------
#
# The hop logic above is HTTP-agnostic; this layer turns it into a real
# sweep against a running Jellyfin-facade server. ``request`` is injected
# so the driver is unit-testable without a network (a fake client returns
# canned ``HopResult``s). ``sweep_home`` walks the home snapshot; the bash
# wrapper ``scripts/sweep_episode_rail.sh`` boots the server and calls
# ``sweep_episode_rail`` via ``python -m cs_uk_api.sweep_episode_rail``.

#: Row-type and provider-attribution facts come from the probing module
#: (spec #323, Probe T2 #328): ``is_episodic_item`` (Model B
#: ``form == "series"`` is the episodic signal — a movie is a dead end
#: for this sweep, D3) and ``attributed_provider`` (first-seen provider).
#: ``sweep_home`` walks items in ``registered`` order so skipped
#: providers are recorded, not dropped.


def _series_items_by_provider(home: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Group the home snapshot's series items by their first provider.

    A merged ``HomeItem`` lists every contributing provider in
    ``providers``; the first one is the source the facade's resolution map
    will pick, so it is the right attribution to sweep under (the probe
    module's ``attributed_provider`` decides in one place). A provider
    with zero series items in the snapshot is simply absent here — the
    caller records it as skipped (acceptance criterion).
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for row in home.get("rows", []):
        for item in row.get("items", []):
            if not is_episodic_item(item):
                continue
            provider = attributed_provider(item)
            if provider is None:
                continue
            out.setdefault(provider, []).append(item)
    return out


#: A request driver is ``Callable[[str, dict[str, str], str], HopResult]``:
#: (url, headers, method) -> HopResult. The live driver POSTs to
#: PlaybackInfo (the spec'd verb) and GETs the rest; the injected test
#: fake ignores method.


def sweep_home(
    home: dict[str, Any],
    request: Callable[[str, dict[str, str], str], HopResult],
    token: str,
    base_url: str,
    per_provider: int = 3,
    registered: Sequence[str] | None = None,
) -> list[ProviderResult]:
    """Walk the episode-rail for every registered provider.

    ``request`` is ``(url, headers) -> HopResult`` (absolute urls built
    from ``base_url``). For each provider we attempt up to ``per_provider``
    series items (or all available when fewer). A provider with no series
    in the warm home snapshot is recorded as SKIPPED with a note
    (acceptance criterion: cover all 19, skip only when zero series) —
    so the report always accounts for every registered provider, not just
    the ones that happened to surface in this snapshot.

    ``registered`` is the authoritative provider id list (defaults to the
    providers found in ``home`` when not supplied, e.g. in tests).
    """
    headers = {"X-Emby-Token": token}
    by_provider = _series_items_by_provider(home)
    results: list[ProviderResult] = []

    ordered = list(registered) if registered is not None else list(by_provider.keys())

    for provider in ordered:
        items = by_provider.get(provider, [])[:per_provider]
        if not items:
            # Zero series in the warm home snapshot for this provider:
            # skip with a note rather than fabricate a series to test.
            p = ProviderResult(provider=provider, skipped=True, skip_reason="no series in home snapshot")
            results.append(p)
            continue
        p = ProviderResult(provider=provider)
        for item in items:
            # Group keys and episode ids carry ':'/'/' (e.g. "g2:uakino:k",
            # "uakino:s1e1") — percent-encode them so the URL stays valid
            # and a provider break is never misattributed to a bad request.
            raw_gk = item["group_key"]
            gk = quote(raw_gk, safe="")
            seasons = request(f"{base_url}/Shows/{gk}/Seasons", headers, "GET")
            season_id = pick_season_id(seasons)
            if season_id is not None:
                episodes = request(
                    f"{base_url}/Shows/{gk}/Episodes?seasonId={quote(season_id, safe='')}",
                    headers,
                    "GET",
                )
            else:
                # No real season resolved — hit the episodes hop with NO
                # seasonId so walk_series records the NO_EPISODES break at
                # the Seasons hop (the empty-200 hazard) rather than us
                # inventing a season id.
                episodes = request(f"{base_url}/Shows/{gk}/Episodes", headers, "GET")
            ep_id = pick_episode_id(episodes)
            if ep_id is None:
                # Rail dead before an episode id exists — PlaybackInfo and
                # stream are not reachable, so feed empty hops so walk_series
                # reports the break at the rail, not a transport crash.
                playback = HopResult(status=0, json=None)
                stream = HopResult(status=0, json=None)
            else:
                # PlaybackInfo is spec'd as POST (the @jellyfin/sdk verb);
                # the facade also answers GET, but we exercise the real path.
                playback = request(
                    f"{base_url}/Items/{quote(ep_id, safe='')}/PlaybackInfo", headers, "POST"
                )
                stream = request(
                    f"{base_url}/Videos/{quote(ep_id, safe='')}/stream?static=true", headers, "GET"
                )
            p.add(
                walk_series(
                    title=str(item.get("title", raw_gk)),
                    group_key=raw_gk,
                    seasons=seasons,
                    episodes=episodes,
                    playback=playback,
                    stream=stream,
                )
            )
        results.append(p)
    return results


def _http_client_request(base_url: str, token: str) -> Callable[[str, dict[str, str], str], HopResult]:
    """Build a ``request`` bound to a live server (uses urllib, no dep).

    ``method`` selects GET/POST; PlaybackInfo is exercised as POST (the
    spec'd verb) while the rail hops are GET.
    """
    import json
    import urllib.error
    import urllib.request

    headers = {"X-Emby-Token": token, "Accept": "application/json"}

    def _do(url: str, req_headers: dict[str, str], method: str = "GET") -> HopResult:
        req = urllib.request.Request(url, headers={**headers, **req_headers}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(body) if body else None
                except json.JSONDecodeError:
                    data = None
                return HopResult(status=resp.status, json=data)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body) if body else None
            except json.JSONDecodeError:
                data = None
            return HopResult(status=e.code, json=data, error=body[:200] or None)
        except Exception as e:  # noqa: BLE001
            return HopResult(status=-1, json=None, error=f"{type(e).__name__}: {e}")

    return _do


def _main(argv: list[str]) -> int:
    """CLI used by scripts/sweep_episode_rail.sh.

    Usage::

        python -m cs_uk_api.sweep_episode_rail http <base_url> \
            [--token TOKEN] [--per-provider N] [--out report.md]

    Boots nothing — it expects a running server (the bash wrapper boots
    it). Fetches ``/api/home`` once, warms the facade resolution map by
    hitting ``/Items`` for each series group key, then walks the
    episode-rail and writes the Markdown report.
    """
    import argparse
    import json
    import urllib.request

    parser = argparse.ArgumentParser(prog="sweep_episode_rail")
    sub = parser.add_subparsers(dest="cmd", required=True)
    http_cmd = sub.add_parser("http", help="sweep a running server")
    http_cmd.add_argument("base_url")
    http_cmd.add_argument("--token", default="jellyfin-dev-token")
    http_cmd.add_argument("--per-provider", type=int, default=3)
    http_cmd.add_argument("--out", default=None, help="write the report here")
    args = parser.parse_args(argv[1:])

    if args.cmd == "http":
        request = _http_client_request(args.base_url.rstrip("/"), args.token)
        # Warm the facade resolution map: the home snapshot is the SAME
        # store the /Shows routes read (catalog_state). Bootstrapping it
        # once here means every later hop resolves from a populated map.
        try:
            with urllib.request.urlopen(
                f"{args.base_url.rstrip('/')}/api/home", timeout=60
            ) as resp:
                home = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            print(f"FATAL: cannot fetch /api/home: {e}", file=__import__("sys").stderr)
            return 1

        from .providers import (
            PROVIDERS,
            _registry,  # noqa: F401  (runs register() bootstrap)
        )

        results = sweep_home(
            home,
            request,
            args.token,
            args.base_url.rstrip("/"),
            args.per_provider,
            registered=list(PROVIDERS.keys()),
        )
        report = render_report(results)
        print(report)
        if args.out:
            write_report(args.out, report)
        return 0
    return 2


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv))


__all__ = [
    "FAIL",
    "HOP_PLAYBACK",
    "HOP_SHOWS",
    "HOP_STREAM",
    "NO_EPISODES",
    "OK",
    "PASS_STATUS",
    "HopResult",
    "ProviderResult",
    "SeriesResult",
    "pick_episode_id",
    "pick_season_id",
    "render_report",
    "sweep_home",
    "walk_series",
]
