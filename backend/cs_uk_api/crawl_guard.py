"""Circuit breaker for providers that keep answering 403.

A provider that has decided to block this host will keep blocking it every
night. Re-walking it costs the whole section set in requests and earns the
same 403, so after a few consecutive blocked nights the crawler stands down
for a cooldown, then probes once:

  - three consecutive nights where EVERY section failed with a 403 trip the
    breaker (``DEFAULT_BLOCK_AFTER_NIGHTS``);
  - while tripped the provider is not walked at all — its rows stay in the
    index, stale but searchable;
  - after the cooldown (seven days by default) one normal walk happens. A
    success clears the record; another 403 trips it again for a full
    cooldown, without needing three more nights.

Only 403 counts. A timeout, DNS failure or Cloudflare challenge is treated
as "other": it breaks the streak (the failure was not a block verdict) and
never trips the breaker — that is what the retry passes in
:func:`cs_uk_api.catalog_index.crawl_index` are for.

State is a small JSON file next to the catalog DB (``crawl-guard.json``), so
the verdict survives the nightly process. Deleting it only costs one extra
walk.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

log = logging.getLogger("cs_uk_api.crawl_guard")

#: Consecutive blocked nights before the provider is skipped.
DEFAULT_BLOCK_AFTER_NIGHTS = 3
#: How long a tripped provider is left alone before the next probe.
DEFAULT_COOLDOWN_DAYS = 7

Verdict = Literal["ok", "403", "other"]

#: A provider error mentions the status either as ``status 403`` or in a
#: lower-level message; a bare number keeps this adapter-agnostic.
FORBIDDEN = re.compile(r"\b403\b")


def _today(today: date | None) -> date:
    # Local calendar day, matching the nightly schedule and the drift
    # monitor's convention; ``astimezone`` makes the clock explicit (DTZ011).
    return today if today is not None else datetime.now().astimezone().date()


@dataclass(frozen=True)
class SkipInfo:
    """Why a provider is being skipped, for the run report."""

    nights: int
    retry_after: str


@dataclass
class _Entry:
    nights: int = 0
    skip_until: str | None = None
    last_counted: str | None = None


def classify(
    *, ok: bool, sections: int, errors: Sequence[str]
) -> Verdict:
    """Turn one provider's crawl stats into a breaker verdict.

    ``ok`` means at least one page answered. A provider with no browsable
    sections is not a block (nothing to crawl). Everything that is not
    uniformly 403 is "other", so a flaky night never counts toward a block.
    """
    if ok:
        return "ok"
    if sections <= 0:
        return "other"
    if errors and all(FORBIDDEN.search(error) for error in errors):
        return "403"
    return "other"


class ProviderGuard:
    """Persistent consecutive-403 counter with a cooldown."""

    def __init__(
        self,
        path: str | Path,
        *,
        block_after: int = DEFAULT_BLOCK_AFTER_NIGHTS,
        cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    ) -> None:
        self.path = Path(path)
        # 0 disables the breaker (the operator asked for no skipping).
        self.block_after = max(0, block_after)
        self.cooldown_days = max(1, cooldown_days)
        self._entries: dict[str, _Entry] = {}
        self._load()

    def blocked(
        self, provider_id: str, *, today: date | None = None
    ) -> SkipInfo | None:
        if self.block_after == 0:
            return None
        entry = self._entries.get(provider_id)
        if entry is None or not entry.skip_until:
            return None
        if entry.skip_until <= _today(today).isoformat():
            return None
        return SkipInfo(entry.nights, entry.skip_until)

    def record(
        self, provider_id: str, verdict: Verdict, *, today: date | None = None
    ) -> SkipInfo | None:
        """Update the counter; return SkipInfo when this run tripped it."""
        if self.block_after == 0:
            return None
        day = _today(today)
        key = day.isoformat()
        entry = self._entries.setdefault(provider_id, _Entry())
        if verdict == "ok":
            self._entries.pop(provider_id, None)
            return None
        if verdict == "other":
            # Not a block verdict: the streak is broken, but an existing
            # cooldown is left in place.
            entry.nights = 0
            entry.last_counted = None
            return None
        if entry.last_counted != key:
            entry.nights += 1
            entry.last_counted = key
        if entry.nights >= self.block_after:
            until = (day + timedelta(days=self.cooldown_days)).isoformat()
            if entry.skip_until != until:
                log.warning(
                    "provider %s answered 403 on %d consecutive nights; "
                    "skipping it until %s",
                    provider_id,
                    entry.nights,
                    until,
                )
            entry.skip_until = until
            return SkipInfo(entry.nights, until)
        return None

    def partition(
        self, provider_ids: Iterable[str], *, today: date | None = None
    ) -> tuple[list[str], list[tuple[str, SkipInfo]]]:
        """Split the selection into (walk now, skipped with the reason)."""
        active: list[str] = []
        skipped: list[tuple[str, SkipInfo]] = []
        for provider_id in provider_ids:
            info = self.blocked(provider_id, today=today)
            if info is None:
                active.append(provider_id)
            else:
                skipped.append((provider_id, info))
        return active, skipped

    def save(self) -> None:
        payload = {
            "version": 1,
            "providers": {
                provider_id: {
                    "nights": entry.nights,
                    "skip_until": entry.skip_until,
                    "last_counted": entry.last_counted,
                }
                for provider_id, entry in sorted(self._entries.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            log.warning("crawl guard state unreadable (%s); starting clean", error)
            return
        providers = raw.get("providers") if isinstance(raw, dict) else None
        if not isinstance(providers, dict):
            log.warning("crawl guard state has no providers map; starting clean")
            return
        for provider_id, data in providers.items():
            if not isinstance(provider_id, str) or not isinstance(data, dict):
                continue
            nights = data.get("nights")
            skip_until = data.get("skip_until")
            last_counted = data.get("last_counted")
            self._entries[provider_id] = _Entry(
                nights=nights if isinstance(nights, int) and nights >= 0 else 0,
                skip_until=skip_until if isinstance(skip_until, str) else None,
                last_counted=last_counted if isinstance(last_counted, str) else None,
            )
