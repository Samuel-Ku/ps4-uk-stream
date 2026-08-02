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
    cache_poster_s: int
    providers: tuple[str, ...]


def load_settings() -> Settings:
    raw = os.environ.get("CS_UK_PROVIDERS", "uakino")
    providers = tuple(p.strip() for p in raw.split(",") if p.strip())
    raw_hosts = os.environ.get("CS_UK_POSTER_ALLOWED_HOSTS", "")
    hosts = tuple(h.strip().lower() for h in raw_hosts.split(",") if h.strip())
    return Settings(
        host=os.environ.get("CS_UK_HOST", "0.0.0.0"),
        port=int(os.environ.get("CS_UK_PORT", "8000")),
        upstream_timeout_s=float(os.environ.get("CS_UK_UPSTREAM_TIMEOUT", "8")),
        search_total_timeout_s=float(os.environ.get("CS_UK_SEARCH_TOTAL", "12")),
        poster_size_cap_bytes=int(os.environ.get("CS_UK_POSTER_MAX", str(4 * 1024 * 1024))),
        poster_allowed_hosts=hosts or DEFAULT_POSTER_ALLOWED_HOSTS,
        cache_search_s=int(os.environ.get("CS_UK_CACHE_SEARCH", "300")),
        cache_content_s=int(os.environ.get("CS_UK_CACHE_CONTENT", "1800")),
        cache_poster_s=int(os.environ.get("CS_UK_CACHE_POSTER", "3600")),
        providers=providers or ("uakino",),
    )


SETTINGS = load_settings()
