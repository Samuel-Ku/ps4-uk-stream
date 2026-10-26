"""Probe harness (spec #285, ticket #286).

Two probe kinds, both running through the REAL provider adapters (the
same ``browse``/``search``/``content``/``stream`` code the API uses):

  - **Listing probe** — page 1 of the provider's newest section (its
    ``newest_section``, falling back to the first declared section,
    falling back to a search for the provider's own name). Parsed by
    the adapter, so a parse breakage or form/style flip shows up as a
    changed signature.
  - **Deep probe** — the first listing card's ``content()``, then its
    ``stream()``, then a HEAD of the stream URL. Rotating: a subset per
    run so every provider gets a deep probe every ``every_n_days``
    days without hammering upstreams nightly.

uakino is excluded: its health is already tracked by the API's
browser-session heartbeat, and probing it would warm a second browser
session (spec #285, user story 9).

The probe results are pure data (``ListingProbeResult`` /
``DeepProbeResult``); verdicts and state live in ``baseline.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..models import ContentResponse
from ..providers.base import BaseProvider, ProviderError

#: uakino is never probed (spec #285, user story 9): the API's
#: browser-session heartbeat already tracks its health, and a second
#: session would only warm an extra browser.
EXCLUDED_PROVIDER_IDS = frozenset({"uakino"})


@dataclass
class ListingProbeResult:
    """Outcome of one provider's listing probe (parsed by the adapter)."""

    provider_id: str
    ok: bool
    error: str | None = None
    #: Parsed cards — the adapter's own SearchResult objects, so the
    #: signature (form/style distribution, required fields) is computed
    #: from exactly what the API would surface.
    cards: list[Any] = field(default_factory=list)
    #: Probe kind actually used ("browse" | "search") — recorded for the
    #: report so an operator can see which seam answered.
    kind: str = "browse"


@dataclass
class DeepProbeResult:
    """Outcome of one provider's deep probe (content → stream → HEAD)."""

    provider_id: str
    ok: bool
    error: str | None = None
    stream_url: str | None = None
    head_status: int | None = None


def failure_text(error: ProviderError) -> str:
    """The report line for a provider failure: ``code: detail``.

    httpx's transport exceptions stringify to ``''`` — ``ReadTimeout``,
    ``ConnectTimeout``, ``ReadError`` and ``RemoteProtocolError`` all do —
    so the adapters' canonical ``ProviderError("unreachable", str(e))``
    rendered as ``unreachable: `` and the sweep could not say whether the
    upstream had gone slow (read timeout) or stopped accepting
    connections (connect timeout). When the message is empty (including
    whitespace-only), fall back to the cause: its own text when it has
    any, else its class name. With no cause either, the detail is
    ``unknown`` — this line never ends in an ambiguous empty tail.
    """
    detail = error.message
    if not detail.strip():
        cause = error.__cause__
        if cause is not None:
            detail = str(cause).strip() or type(cause).__name__
        else:
            detail = "unknown"
    return f"{error.code}: {detail}"


def listing_section(provider: BaseProvider) -> tuple[str, str] | None:
    """The (section_id, kind) the listing probe should use.

    ``newest_section`` when the provider declares one (the freshest
    listing), else the first declared section; a provider with no
    sections at all returns None (caller falls back to search).
    """
    if provider.newest_section:
        return provider.newest_section, "browse"
    if provider.sections:
        return provider.sections[0].id, "browse"
    return None


async def probe_listing(
    provider: BaseProvider, http: httpx.AsyncClient
) -> ListingProbeResult:
    """Page-1 listing through the adapter's own parse path.

    browse(newest section, page 1) when the provider has sections —
    every plain-HTTP provider does — else search(provider.name). A
    raise (HTTP error, parse failure) becomes ok=False with the error
    text; an adapter bug surfaces the same way a drift would.
    """
    result = ListingProbeResult(provider_id=provider.id, ok=False)
    try:
        sec = listing_section(provider)
        if sec is not None:
            section_id, kind = sec
            cards, _has_more = await provider.browse(section_id, 1, http)
            result.kind = kind
        else:
            result.kind = "search"
            cards = await provider.search(provider.name, http)
        result.cards = list(cards)
        result.ok = True
    except ProviderError as e:
        result.error = failure_text(e)
    except Exception as e:  # noqa: BLE001 — a probe never crashes the sweep
        result.error = f"{type(e).__name__}: {e}"
    return result


async def probe_deep(
    provider: BaseProvider, http: httpx.AsyncClient, first_card: Any
) -> DeepProbeResult:
    """Deep probe of one card: content() → stream() → HEAD stream URL.

    ``first_card`` is the adapter's SearchResult from the listing probe
    (its ``id`` carries the provider-scoped external id). The stream
    step uses the first playable episode id from ``content()`` — the
    API's own contract (``:s1e1`` for series, ``:__movie__`` for
    films) — not the bare search-result id, which is not playable for
    series and for some movie adapters. A gated verdict (ProviderError
    ``gated``) is NOT a failure — it is the provider's deliberate "for
    subscribers" state, same semantics as the API (ADR-0002). Any
    other raise or a HEAD that is not 2xx/3xx marks the provider failed.
    """
    result = DeepProbeResult(provider_id=provider.id, ok=False)
    try:
        external_id = _external_id(first_card)
        content = await provider.content(external_id, http)
        playable_id = _playable_id(provider, content, external_id)
        stream = await provider.stream(playable_id, None, http)
        head = await http.head(stream.url)
        result.stream_url = stream.url
        result.head_status = head.status_code
        result.ok = 200 <= head.status_code < 400
        if not result.ok:
            result.error = f"HEAD {stream.url} → {head.status_code}"
    except ProviderError as e:
        if e.code == "gated":
            result.ok = True  # gated is a deliberate state, not drift
            result.error = "gated"
        else:
            result.error = failure_text(e)
    except Exception as e:  # noqa: BLE001 — a probe never crashes the sweep
        result.error = f"{type(e).__name__}: {e}"
    return result


def _external_id(card: Any) -> str:
    """The provider-scoped external id from a SearchResult id.

    SearchResult ids are ``{provider}:{external}``; the external part is
    what content()/stream() consume.
    """
    _, _, external = str(card.id).partition(":")
    return external or str(card.id)


def _playable_id(
    provider: BaseProvider, content: ContentResponse, external_id: str
) -> str:
    """The id a real client would pass to ``stream()``.

    Content pages emit episode rows whose ids already carry the
    playable tail: ``:s1e1`` for series, ``:__movie__`` for films, and
    a full episode URL for simpsonsuatv. Prefer the first episode's
    wire id (strip the provider prefix) and fall back to the bare
    external id only when the page exposes no episodes at all.
    """
    for season in content.seasons or []:
        if season.episodes:
            return str(season.episodes[0].id).removeprefix(f"{provider.id}:")
    return external_id


def rotate_deep_providers(
    provider_ids: list[str], day_offset: int, every_n_days: int
) -> set[str]:
    """The rotating deep-probe subset for one run.

    Deterministic round-robin by ``day_offset`` (e.g. the day-of-year):
    each provider is deep-probed once every ``every_n_days`` days, and
    the subset size is ``ceil(len / every_n_days)`` so full coverage
    completes exactly every ``every_n_days`` runs. Excluded providers
    are never in the rotation.
    """
    ids = [pid for pid in provider_ids if pid not in EXCLUDED_PROVIDER_IDS]
    if not ids:
        return set()
    step = max(1, (len(ids) + every_n_days - 1) // every_n_days)
    start = (day_offset * step) % len(ids)
    picked: list[str] = []
    i = start
    while len(picked) < step:
        picked.append(ids[i])
        i = (i + 1) % len(ids)
    return set(picked)
