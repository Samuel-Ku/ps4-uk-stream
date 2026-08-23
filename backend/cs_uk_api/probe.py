"""Provider probing contract (spec #323, Probe T1 #327).

One module names the three facts every provider probe — the drift
monitor, the episode-rail sweep, the triage scripts — must agree on:

  1. **Entry-point selection** — the chain a probe walks to reach a
     provider's content: ``newest_section`` -> declared sections ->
     search (the fallback every provider has).
  2. **Wire-id splitting** — ``provider:external`` -> ``(provider,
      external)``. The canonical copy lives in ``wire_identity``
      (spec #340); this module re-exports it so existing probe
      imports keep working.
  3. **Verdict normalization** — a ``gated`` ``ProviderError`` is a
     policy outcome, NOT a failure (ADR-0002): it must never flip a
     probe's health verdict. ``is_probe_failure`` decides in one place
     so a third implementation can't drift.

Pure functions, no I/O — tests drive them with canned providers and
exceptions. HTTP stays at the caller (sweep, drift monitor).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .providers.base import BaseProvider, ProviderError
from .wire_identity import split_wire_id as split_wire_id  # canonical home: wire_identity (#340)

#: Probe verdict vocabulary — the one vocabulary every probe (drift,
#: episode-rail sweep, triage) reports in. ``gated`` (a policy outcome)
#: and ``no_episodes`` (a resolved-but-empty rail — a warning, not a
#: break) are deliberately separate from ``fail``/``unavailable``/
#: ``error`` so callers render them distinctly and the failure predicate
#: below stays the single ADR-0002 authority.
VERDICT_OK = "ok"
VERDICT_FAIL = "fail"
VERDICT_NO_EPISODES = "no_episodes"
VERDICT_GATED = "gated"
VERDICT_UNAVAILABLE = "unavailable"
VERDICT_ERROR = "error"


@dataclass(frozen=True)
class EntryPoint:
    """One step in the entry-point chain a probe walks.

    ``kind`` is ``newest`` (``provider.newest_section``), ``section``
    (one declared ``Section``) or ``search`` (the last-resort entry;
    ``section`` is None for it). ``section`` carries the id the caller
    passes to ``browse()``.
    """

    kind: Literal["newest", "section", "search"]
    section: str | None = None


def select_entry_points(provider: BaseProvider) -> tuple[EntryPoint, ...]:
    """The entry-point chain: newest -> sections -> search.

    Every provider ends with the ``search`` entry — ``search()`` is the
    only method the base contract guarantees — so a probe always has a
    last resort. ``newest_section`` and ``sections`` are optional
    (``None`` / empty on providers that opt out of section browsing).
    """
    entries: list[EntryPoint] = []
    if provider.newest_section is not None:
        entries.append(EntryPoint(kind="newest", section=provider.newest_section))
    for section in provider.sections:
        entries.append(EntryPoint(kind="section", section=section.id))
    entries.append(EntryPoint(kind="search"))
    return tuple(entries)


def attributed_provider(item: Mapping[str, Any]) -> str | None:
    """The provider a probe attributes a home item to.

    The first-seen entry of the item's ``providers`` list — the same
    first-seen order the home rows surface and the facade's resolution
    map picks (a merged card's source order determines attribution).
    ``None`` when the item carries no providers (it is not attributed to
    anyone, so a probe cannot sweep it under a provider).
    """
    providers = item.get("providers")
    if not isinstance(providers, list) or not providers:
        return None
    first = providers[0]
    return first if isinstance(first, str) else None


def is_episodic_item(item: Mapping[str, Any]) -> bool:
    """The row-type probe fact: True when a home item is episodic
    (sweepable by the episode-rail probe).

    Model B: ``form == "series"`` is the episodic signal — a movie is a
    dead end for the episode rail (D3), and style tags (anime/cartoon/
    dorama) change the style, never the form.
    """
    return item.get("form") == "series"


def probe_error_verdict(exc: Exception) -> str:
    """Normalize a probe exception into a verdict (ADR-0002 in one place).

    ``ProviderError`` with code ``gated`` -> ``gated`` (subscription
    policy, not an upstream failure); any other ``ProviderError`` ->
    ``unavailable`` (a deterministic upstream answer); anything else ->
    ``error`` (a transport/parse crash).
    """
    if isinstance(exc, ProviderError):
        return VERDICT_GATED if exc.code == "gated" else VERDICT_UNAVAILABLE
    return VERDICT_ERROR


def is_probe_failure(verdict: str) -> bool:
    """True iff a verdict means the provider FAILED a probe.

    The single ADR-0002 authority: only ``ok``, ``gated`` (a policy
    outcome, never marks the provider down) and ``no_episodes`` (a
    resolved-but-empty rail — a warning, not a break) are non-failures;
    anything else — ``fail``, ``unavailable``, ``error``, or an unknown
    verdict — counts as a failure (fail closed).
    """
    return verdict not in (VERDICT_OK, VERDICT_GATED, VERDICT_NO_EPISODES)
