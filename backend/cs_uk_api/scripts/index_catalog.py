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
``CS_UK_INDEX_DELAY`` / ``CS_UK_CATALOG_DB``. Exit code 0 = every selected
provider answered at least one page; 1 = at least one provider failed;
2 = the index is disabled or no providers were selected.

Scheduling: ``backend/deploy/cs-uk-api-index.{service,timer}`` runs the
crawler nightly with ``--all-providers`` (20 pages per section); install
it beside the API unit.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import cs_uk_api.providers._registry  # noqa: F401  (populate PROVIDERS)

from .. import config as _config
from ..catalog_index import CatalogIndex, crawl_index
from ..http_client import close_client
from ..providers import PROVIDERS
from ..uakino_browser import get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("cs_uk_api.scripts.index_catalog")


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

    index = CatalogIndex(db_path)
    try:
        result = await crawl_index(
            provider_ids,
            pages=args.pages,
            index=index,
            delay_s=max(0.0, args.delay),
        )
        total = index.count()
    finally:
        index.close()
        # The uakino adapter lazily launches Chromium; close it so a
        # finished crawl never leaves a browser process behind.
        await get_session().close()
        await close_client()

    for provider in result.providers:
        status = "OK  " if provider.ok else "FAIL"
        print(
            f"{status} {provider.provider:<14} sections={provider.sections:<2} "
            f"pages={provider.pages:<4} seen={provider.seen:<5} "
            f"indexed={provider.indexed:<5} errors={len(provider.errors)}"
        )
        for error in provider.errors[:5]:
            print(f"     {error}")
    print(
        f"catalog: {result.total_indexed} rows written, "
        f"{result.total_seen} cards seen, {total} rows held, db={db_path}"
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
