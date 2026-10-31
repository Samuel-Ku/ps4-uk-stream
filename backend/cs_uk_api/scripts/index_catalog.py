"""Build/refresh the local catalog index from provider browse sections.

The crawler walks each selected provider's advertised sections through the
same ``browse()`` adapters ``/api/browse`` uses, page by page, and upserts
every card into the SQLite FTS5 index. It is deliberately a detached
process (like ``drift_monitor``): it imports the adapters and the index,
never the FastAPI app, so a long crawl cannot slow the API.

Usage:
    python -m cs_uk_api.scripts.index_catalog --all-providers
    python -m cs_uk_api.scripts.index_catalog --providers uakino,ufdub --pages 50
    python -m cs_uk_api.scripts.index_catalog --pages 100 --delay 0.5

``--all-providers`` walks every provider in the registry (the nightly unit
uses it); without it, the list comes from ``--providers`` /
``CS_UK_INDEX_PROVIDERS``. Pages/delay/DB default to ``CS_UK_INDEX_PAGES`` /
``CS_UK_INDEX_DELAY`` / ``CS_UK_CATALOG_DB``. Exit code 0 = every provider
that was walked answered at least one page (a provider deliberately skipped
by the 403 breaker counts as fine); 1 = at least one walked provider failed;
2 = the index is disabled or no providers were selected.

A provider that answers 403 on several consecutive nights is skipped for a
cooldown and then probed once (``cs_uk_api.crawl_guard``; state in
``crawl-guard.json`` beside the catalog DB).

Scheduling: ``backend/deploy/cs-uk-api-index.{service,timer}`` runs the
crawler Mon-Sat with ``--all-providers`` (10 pages per section); the
weekly full build is ``cs-uk-api-index-full.{service,timer}`` (100 pages,
Sunday). Install them beside the API unit.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

import cs_uk_api.providers._registry  # noqa: F401  (populate PROVIDERS)

from .. import config as _config
from ..catalog_index import CatalogIndex, crawl_index
from ..crawl_guard import ProviderGuard, SkipInfo, classify
from ..http_cache import ConditionalCache, build_conditional_client
from ..http_client import close_client
from ..providers import PROVIDERS
from ..uakino_browser import get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("cs_uk_api.scripts.index_catalog")


def _format_duration(seconds: float) -> str:
    """Human duration for the run summary (``29m35s``, ``1h04m12s``)."""
    total = round(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


async def main() -> int:
    settings = _config.SETTINGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--providers",
        default=",".join(settings.index_providers),
        help="comma-separated provider ids (default: CS_UK_INDEX_PROVIDERS)",
    )
    parser.add_argument(
        "--all-providers",
        action="store_true",
        help="crawl every registered provider (overrides --providers)",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=settings.index_pages,
        help="browse pages per section (default: CS_UK_INDEX_PAGES / 10)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=settings.index_crawl_delay_s,
        help="pause between page fetches in seconds (default: CS_UK_INDEX_DELAY / 0.2)",
    )
    parser.add_argument(
        "--db",
        default=settings.catalog_db_path or "",
        help="catalog SQLite path (default: CS_UK_CATALOG_DB)",
    )
    args = parser.parse_args()

    db_path = args.db or settings.catalog_db_path
    if not db_path:
        print(
            "catalog index disabled (set CS_UK_CATALOG_DB to a path)",
            file=sys.stderr,
        )
        return 2
    provider_ids = (
        tuple(PROVIDERS)
        if args.all_providers
        else tuple(p.strip() for p in args.providers.split(",") if p.strip())
    )
    if not provider_ids:
        print("no providers selected", file=sys.stderr)
        return 2

    # A provider that has answered 403 on several consecutive nights is
    # stood down for the cooldown; one probe run is allowed afterwards.
    guard = ProviderGuard(
        os.path.join(os.path.dirname(os.path.abspath(db_path)), "crawl-guard.json"),
        block_after=settings.index_block_after_nights,
        cooldown_days=settings.index_block_cooldown_days,
    )
    active_ids, skipped = guard.partition(provider_ids)
    for provider_id, info in skipped:
        print(
            f"SKIP {provider_id:<14} blocked: 403 on {info.nights} nights, "
            f"retry after {info.retry_after}"
        )
    if not active_ids:
        print("crawl: every selected provider is blocked; nothing to walk")
        return 0

    index = CatalogIndex(db_path)
    # Validators live next to the catalog DB so a nightly run can revalidate
    # pages the previous night stored; an in-memory cache would save nothing.
    cache = ConditionalCache(
        os.path.join(os.path.dirname(os.path.abspath(db_path)), "http-cache.sqlite3")
    )
    client = build_conditional_client(cache)
    tripped: list[tuple[str, SkipInfo]] = []
    try:
        started = time.monotonic()
        result = await crawl_index(
            active_ids,
            pages=args.pages,
            index=index,
            http=client,
            delay_s=max(0.0, args.delay),
        )
        elapsed = time.monotonic() - started
        total = index.count()
        for provider in result.providers:
            signal = guard.record(
                provider.provider,
                classify(
                    ok=provider.ok,
                    sections=provider.sections,
                    errors=provider.errors,
                ),
            )
            if signal is not None:
                tripped.append((provider.provider, signal))
        guard.save()
    finally:
        await client.aclose()
        cache.close()
        index.close()
        # The uakino adapter lazily launches Chromium; close it so a
        # finished crawl never leaves a browser process behind.
        await get_session().close()
        await close_client()

    for provider_id, info in tripped:
        print(
            f"BLOCK {provider_id:<13} tripped after {info.nights} consecutive "
            f"403 nights; skipping until {info.retry_after}"
        )
    for provider in result.providers:
        status = "OK  " if provider.ok else "FAIL"
        print(
            f"{status} {provider.provider:<14} sections={provider.sections:<2} "
            f"pages={provider.pages:<4} seen={provider.seen:<5} "
            f"indexed={provider.indexed:<5} errors={len(provider.errors)} "
            f"attempts={provider.attempts}"
        )
        for error in provider.errors[:5]:
            print(f"     {error}")
    print(
        f"crawl summary: providers={len(result.providers)} "
        f"sections={sum(provider.sections for provider in result.providers)} "
        f"pages={sum(provider.pages for provider in result.providers)} "
        f"seen={result.total_seen} indexed={result.total_indexed} "
        f"errors={sum(len(provider.errors) for provider in result.providers)} "
        f"skipped={len(skipped)} duration={_format_duration(elapsed)}"
    )
    print(
        f"catalog: {result.total_indexed} rows written, "
        f"{result.total_seen} cards seen, {total} rows held, db={db_path}"
    )
    summary = cache.summary()
    print(
        f"http cache: {summary.stored} bodies stored, "
        f"{summary.revalidated} revalidated (304), "
        f"{summary.bytes_saved} bytes not re-downloaded"
    )
    if result.failed_providers:
        print(
            f"FAILED providers: {', '.join(result.failed_providers)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
