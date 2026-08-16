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
    #: Path of the persisted playback resume state (spec #247 / ticket
    #: #248). Default: next to the poster disk cache (``playback.json``
    #: in the parent of ``CS_UK_POSTER_CACHE_DIR``). Override via
    #: ``CS_UK_RESUME_PATH``; an explicit empty string disables the disk
    #: layer (memory-only — the test-suite default via conftest).
    #: Defaulted so explicit ``Settings(...)`` constructions (tests) stay
    #: valid without naming the new knob.
    resume_path: str | None = None
    #: Path of the persisted user-state file — favorites + played marks
    #: (spec #257 / ticket #258). Default: ``user-state.json`` next to
    #: the resume file. Override via ``CS_UK_USER_STATE_PATH``; an
    #: explicit empty string disables the disk layer (memory-only — the
    #: test-suite default via conftest).
    user_state_path: str | None = None
    #: Path of the persisted home snapshot (spec #267 / ticket #269):
    #: the last successful home build, served on a cold start at any
    #: age (instant first open after a restart). Default:
    #: ``home-snapshot.json`` next to the resume file. Override via
    #: ``CS_UK_SNAPSHOT_PATH``; an explicit empty string disables the
    #: disk layer (memory-only — the test-suite default via conftest).
    snapshot_path: str | None = None
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
    #: LLM taste-profile layer (spec #290): an OPTIONAL OpenAI-
    #: compatible enrichment of the recommender. All three knobs must be
    #: set for the layer to activate; any missing (or an empty key)
    #: leaves the pure scorer untouched. Defaults keep the layer inert.
    llm_base_url: str | None = None
    llm_key: str | None = None
    llm_model: str | None = None
    #: Deep rows (spec #305): max upstream browse pages fetched per row
    #: BEYOND the snapshot page when the client scrolls a home row
    #: (default 5 ≈ 100 cards per row). Override via
    #: ``CS_UK_ROW_MAX_PAGES``; deeper requests return the exhausted
    #: tail. The initial row stays at the snapshot cap.
    row_max_pages: int = 5
    # Round-2 persistence (spec #323, Store T1): when set, the viewer
    # profile store persists the resume memory to this file via the
    # VersionedFileStore (version token + atomic writes) and restores it
    # on a cold start. Default unset = in-memory only — round-1
    # behaviour, zero change unless an operator opts in.
    profile_file: str | None = None


def _load_resume_path() -> str | None:
    """Resolve the resume state file path (ticket #248).

    ``CS_UK_RESUME_PATH`` unset → next to the poster disk cache;
    explicit empty string → memory-only (no disk layer); otherwise the
    given path.
    """
    raw = os.environ.get("CS_UK_RESUME_PATH")
    if raw is None:
        base = os.environ.get("CS_UK_POSTER_CACHE_DIR", "") or os.path.expanduser("~/.cache/cs-uk-api/posters")
        return os.path.join(os.path.dirname(base), "playback.json")
    if raw == "":
        return None
    return raw


def _load_user_state_path() -> str | None:
    """Resolve the user-state file path (ticket #258, spec #257).

    ``CS_UK_USER_STATE_PATH`` unset → ``user-state.json`` next to the
    resume file (which itself defaults next to the poster disk cache);
    explicit empty string → memory-only (no disk layer); otherwise the
    given path.
    """
    raw = os.environ.get("CS_UK_USER_STATE_PATH")
    if raw is None:
        resume = _load_resume_path()
        if resume is not None:
            return os.path.join(os.path.dirname(resume), "user-state.json")
        base = os.environ.get("CS_UK_POSTER_CACHE_DIR", "") or os.path.expanduser("~/.cache/cs-uk-api/posters")
        return os.path.join(os.path.dirname(base), "user-state.json")
    if raw == "":
        return None
    return raw


def _load_snapshot_path() -> str | None:
    """Resolve the home snapshot file path (ticket #269, spec #267).

    ``CS_UK_SNAPSHOT_PATH`` unset → ``home-snapshot.json`` next to the
    resume file (which itself defaults next to the poster disk cache);
    explicit empty string → memory-only (no disk layer); otherwise the
    given path.
    """
    raw = os.environ.get("CS_UK_SNAPSHOT_PATH")
    if raw is None:
        resume = _load_resume_path()
        if resume is not None:
            return os.path.join(os.path.dirname(resume), "home-snapshot.json")
        base = os.environ.get("CS_UK_POSTER_CACHE_DIR", "") or os.path.expanduser("~/.cache/cs-uk-api/posters")
        return os.path.join(os.path.dirname(base), "home-snapshot.json")
    if raw == "":
        return None
    return raw


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
        resume_path=_load_resume_path(),
        user_state_path=_load_user_state_path(),
        snapshot_path=_load_snapshot_path(),
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
        # LLM taste-profile layer (spec #290): all three knobs must be
        # set for the layer to activate — an empty key (explicitly set
        # to "" in env) disables it like an absent one.
        llm_base_url=os.environ.get("CS_UK_LLM_BASE_URL") or None,
        llm_key=os.environ.get("CS_UK_LLM_KEY") or None,
        llm_model=os.environ.get("CS_UK_LLM_MODEL") or None,
        row_max_pages=int(os.environ.get("CS_UK_ROW_MAX_PAGES", "5")),
        # Round-2 (spec #323): opt-in versioned resume persistence.
        profile_file=os.environ.get("CS_UK_PROFILE_FILE") or None,
    )


#: The single configuration binding (Arch T12, spec #309): every module
#: reads settings through this module reference (``config.SETTINGS``) — no
#: module imports the value into its own binding — and every store is
#: constructed from this snapshot. Tests patch exactly ONE binding:
#: ``cs_uk_api.config.SETTINGS``.
SETTINGS = load_settings()
