from __future__ import annotations

import os
from dataclasses import dataclass

#: Hosts the poster proxy may fetch from: provider domains (subdomain
#: matching on dot boundaries) plus the image CDNs seen in provider
#: fixtures. Override via CS_UK_POSTER_ALLOWED_HOSTS (comma-separated).
DEFAULT_POSTER_ALLOWED_HOSTS: tuple[str, ...] = (
    "animeon.club",
    "animeua.club",
    "anitube.in.ua",
    "bambooua.com",
    "cikava-ideya.top",
    "coani.net",
    "doramy.world",
    "eneyida.tv",
    "hentaiukr.com",
    "kinotron.tv",
    "kinovezha.tv",
    "klon.fun",
    "serialno.tv",
    "simpsonsua.tv",
    "uafix.net",
    "uakino.best",
    "uakino.club",
    "uaserials.com",
    "ufdub.com",
    "unimay.media",
    "hurtom.com",
    "mooncdn.net",
    "youtube.com",
    "srvd2204.com",
)


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    upstream_timeout_s: float
    search_total_timeout_s: float
    poster_size_cap_bytes: int
    poster_allowed_hosts: tuple[str, ...]
    cache_search_s: int
    cache_content_s: int
    cache_home_s: int
    cache_poster_s: int
    #: TTL of a subscription-gate "gated" verdict (True). Deliberately
    #: LONGER than the home cache: the catalog sweep re-runs on every
    #: home rebuild, and a cached verdict keeps the rebuild free.
    #: Trade-off: a title that un-gates upstream stays hidden up to this
    #: TTL (default 24h), bounded staleness in the ADR-0003 style. Known-
    #: good (False) verdicts use ``cache_content_s`` instead, so an un-
    #: gated title re-enters the catalog when its content cache expires.
    cache_gated_s: int
    poster_cache_dir: str | None
    poster_disk_ttl_s: int
    providers: tuple[str, ...]
    block_russian: bool
    home_row_limit: int
    #: Startup catalog warm (tickets #204/#210): build the home snapshot
    #: and warm each view's first-card detail chain in the background so
    #: a real client's first requests never hit a cold 17-21s scrape.
    #: Default ON; tests disable it (``CS_UK_CATALOG_WARM=0``) so no
    #: TestClient lifespan triggers real provider scrapes.
    catalog_warm_enabled: bool = True
    # v3 (Jellyfin facade, spec D4/D10): fixed opaque token; the
    # ``load_settings`` env default mirrors this so explicit
    # ``Settings(...)`` constructions (tests) stay valid.
    jellyfin_token: str = "jellyfin-dev-token"


def load_settings() -> Settings:
    raw = os.environ.get("CS_UK_PROVIDERS", "uakino")
    providers = tuple(p.strip() for p in raw.split(",") if p.strip())
    raw_hosts = os.environ.get("CS_UK_POSTER_ALLOWED_HOSTS", "")
    hosts = tuple(h.strip().lower() for h in raw_hosts.split(",") if h.strip())
    warm = os.environ.get("CS_UK_CATALOG_WARM", "1").strip().lower() not in ("0", "false", "no", "off")
    return Settings(
        host=os.environ.get("CS_UK_HOST", "0.0.0.0"),
        port=int(os.environ.get("CS_UK_PORT", "8000")),
        upstream_timeout_s=float(os.environ.get("CS_UK_UPSTREAM_TIMEOUT", "8")),
        search_total_timeout_s=float(os.environ.get("CS_UK_SEARCH_TOTAL", "12")),
        # Poster cap: 8 MiB. The 4 MiB default filtered real listing
        # postere (observed: 4.6 MiB webp from bambooua → card 404),
        # because upstream art is unbounded; 8 MiB covers the wild while
        # still bounding memory pressure (ADR-0003 poster store).
        poster_size_cap_bytes=int(os.environ.get("CS_UK_POSTER_MAX", str(8 * 1024 * 1024))),
        poster_allowed_hosts=hosts or DEFAULT_POSTER_ALLOWED_HOSTS,
        cache_search_s=int(os.environ.get("CS_UK_CACHE_SEARCH", "300")),
        cache_content_s=int(os.environ.get("CS_UK_CACHE_CONTENT", "1800")),
        # v3 (issue #70): home page is a curated snapshot of newest
        # listings + per-type buckets; 30 minutes matches the spec's
        # documented staleness behaviour for the merged view.
        cache_home_s=int(os.environ.get("CS_UK_CACHE_HOME", "1800")),
        cache_poster_s=int(os.environ.get("CS_UK_CACHE_POSTER", "3600")),
        cache_gated_s=int(os.environ.get("CS_UK_CACHE_GATED", "86400")),
        # v3 (issue #54): posters persist on disk for 7 days. Empty string
        # disables the disk layer (memory-only, pre-v3 behaviour).
        poster_cache_dir=os.environ.get(
            "CS_UK_POSTER_CACHE_DIR", os.path.expanduser("~/.cache/cs-uk-api/posters")
        ) or None,
        poster_disk_ttl_s=int(os.environ.get("CS_UK_POSTER_DISK_TTL", str(7 * 24 * 3600))),
        providers=providers or ("uakino",),
        block_russian=os.environ.get("CS_UK_BLOCK_RUSSIAN", "1") == "1",
        # v3 (issue #70): per-row cap for «Новинки» + type rows.
        home_row_limit=int(os.environ.get("CS_UK_HOME_ROW_LIMIT", "20")),
        catalog_warm_enabled=warm,
        # v3 (Jellyfin facade, spec D4/D10): the fixed opaque Jellyfin
        # token. Accept-any-credentials login (the LAN API stays open),
        # but subsequent facade requests must present this token via
        # ``X-Emby-Token`` or ``Authorization: MediaBrowser Token="…"``.
        # Default: a stable dev value; override in production.
        jellyfin_token=os.environ.get("CS_UK_JF_TOKEN", "jellyfin-dev-token"),
    )


SETTINGS = load_settings()
