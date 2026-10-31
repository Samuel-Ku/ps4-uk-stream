"""Local catalog index: SQLite FTS5, local-first search, hints, crawler.

The index is a rebuildable cache beside the other runtime stores
(``catalog.sqlite3`` in ``~/.cache/cs-uk-api`` by default). It exists to
make the library feel bigger than any one home snapshot and to make
search work even when the upstreams are slow or down:

  - **Catalog**: the crawler walks the providers' advertised ``browse()``
    sections, so the same listing parsers the rest of the API uses feed
    the index — no second scraper dialect.
  - **Search**: FTS5 over a normalized title, the official and a loose
    Ukrainian transliteration, and the card's genre labels. Queries are
    tokenized, every token is prefix-matched, and each token may match
    either the Cyrillic or the Latin form. Exact/prefix hits rank above
    substring hits; a LIKE fallback keeps substring search working when
    FTS5 is unavailable.
  - **Local-first**: ``catalog.search`` answers from the index alone once
    it has ``SETTINGS.index_local_min`` merged groups; below that, or on
    a total live-search failure, it merges the local hits with the live
    fan-out. Live results are indexed on the way out, so the index grows
    from normal use as well as from the nightly crawl.
  - **Hints**: ``GET /api/search/hints`` is index-only — no upstream
    calls on a keystroke.

The DB is a cache: deleting it loses no viewer state and the next crawl
rebuilds it. ``CS_UK_CATALOG_DB=""`` disables the whole layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

from . import config as _config
from .filters import matches_axes, section_matches
from .http_client import get_client
from .merge import merge_results
from .models import (
    MediaForm,
    MediaStyle,
    SearchGroup,
    SearchHint,
    SearchResult,
    Section,
)
from .providers import PROVIDERS
from .providers.base import BaseProvider
from .wire_identity import item_group_key, normalize_title, project_group, title_aliases

log = logging.getLogger("cs_uk_api.catalog_index")

_FORM_VALUES = frozenset({"movie", "series"})
_STYLE_VALUES = frozenset({"anime", "cartoon", "dorama"})

#: Query tokenizer. ``normalize_title`` already lowercases and replaces
#: punctuation with spaces; this split also keeps Ukrainian/Russian
#: letters outside the basic ``а-я`` range (і, ї, є, ґ, ё, ы, э).
_TOKEN_RE = re.compile(r"[0-9a-zа-яіїєґёыэ]+")

#: Ukrainian transliteration, official style (КМУ 2010-ish): «Дюна» →
#: ``diuna``. Used for the typeahead bridge AND for ranking.
_OFFICIAL = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "h",
        "ґ": "g",
        "д": "d",
        "е": "e",
        "є": "ye",
        "ж": "zh",
        "з": "z",
        "и": "y",
        "і": "i",
        "ї": "yi",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ь": "",
        "ю": "iu",
        "я": "ia",
        "ё": "e",
        "ы": "y",
        "э": "e",
        "ъ": "",
    }
)

#: Practical/loose transliteration: «Дюна» → ``duna`` (the form people
#: actually type on a phone). Both forms are indexed and queried.
_LOOSE = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "h",
        "ґ": "g",
        "д": "d",
        "е": "e",
        "є": "e",
        "ж": "zh",
        "з": "z",
        "и": "y",
        "і": "i",
        "ї": "i",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "c",
        "ч": "ch",
        "ш": "sh",
        "щ": "sh",
        "ь": "",
        "ю": "u",
        "я": "a",
        "ё": "e",
        "ы": "y",
        "э": "e",
        "ъ": "",
    }
)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS catalog (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    title_latin TEXT NOT NULL,
    year INTEGER,
    form TEXT NOT NULL,
    styles TEXT NOT NULL,
    genres TEXT NOT NULL,
    poster TEXT,
    url TEXT NOT NULL,
    imdb_id TEXT,
    last_seen REAL NOT NULL,
    search_text TEXT NOT NULL
)
"""

_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(
    search_text,
    content='catalog',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
)
"""

#: External-content FTS5 stays in sync through triggers. Keeping the
#: triggers in SQLite (rather than in Python) means an upsert from the
#: crawler and an upsert from a live search cannot take different paths.
_FTS_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS catalog_ai AFTER INSERT ON catalog BEGIN
        INSERT INTO catalog_fts(rowid, search_text)
        VALUES (new.rowid, new.search_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS catalog_ad AFTER DELETE ON catalog BEGIN
        INSERT INTO catalog_fts(catalog_fts, rowid, search_text)
        VALUES ('delete', old.rowid, old.search_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS catalog_au AFTER UPDATE ON catalog BEGIN
        INSERT INTO catalog_fts(catalog_fts, rowid, search_text)
        VALUES ('delete', old.rowid, old.search_text);
        INSERT INTO catalog_fts(rowid, search_text)
        VALUES (new.rowid, new.search_text);
    END
    """,
)

_UPSERT_SQL = """
INSERT INTO catalog (
    id, provider, title, title_norm, title_latin, year, form, styles,
    genres, poster, url, imdb_id, last_seen, search_text
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    provider = excluded.provider,
    title = excluded.title,
    title_norm = excluded.title_norm,
    title_latin = excluded.title_latin,
    year = excluded.year,
    form = excluded.form,
    styles = excluded.styles,
    genres = excluded.genres,
    poster = excluded.poster,
    url = excluded.url,
    imdb_id = excluded.imdb_id,
    last_seen = excluded.last_seen,
    search_text = excluded.search_text
"""


def _fold(text: str) -> str:
    """Lowercase and strip Latin diacritics (NFKD), leaving Cyrillic."""
    lowered = text.lower()
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(ch)
    )


def _transliterate(text: str) -> tuple[str, str]:
    """``(official, loose)`` Latin forms of ``text`` (lowercased)."""
    folded = _fold(text)
    return folded.translate(_OFFICIAL), folded.translate(_LOOSE)


def _normalize_for_index(raw: str) -> str:
    """The canonical normalized title used for matching and the FTS row."""
    normalized = normalize_title(raw)
    if normalized:
        return normalized
    return " ".join(_TOKEN_RE.findall(raw.lower()))


def _query_tokens(query: str) -> tuple[str, ...]:
    """Lowercase word tokens of a query, using the same normalization."""
    normalized = _normalize_for_index(query)
    return tuple(_TOKEN_RE.findall(normalized))


def _token_variants(token: str) -> tuple[str, ...]:
    """A query token plus both Latin transliterations, deduped.

    Two-letter prefixes stay in their original script: expanding «дю» to
    ``du`` would prefix-match every unrelated Latin/transliterated token
    (``dune``, ``dubliazh``, …). At three characters the expansion is
    narrow enough to bridge scripts usefully (``дюн`` → ``dun``).
    """
    if len(token) < 3:
        return (token,)
    official, loose = _transliterate(token)
    return tuple(dict.fromkeys((token, official, loose)))


def _search_text(title_text: str, official: str, loose: str, genres: Sequence[str]) -> str:
    """The single FTS column: title forms + genre labels, space-joined.

    ``title_text`` already carries every normalized alias, so searching
    «Daddy» finds a card listed as «Тато / Daddy». ``dict.fromkeys`` both
    dedupes and keeps order, so a Latin title does not store its own
    transliteration twice.
    """
    parts = [title_text, official, loose]
    parts.extend(genre.lower() for genre in genres if genre)
    return " ".join(dict.fromkeys(part for part in parts if part))


def _row_styles(raw: object) -> frozenset[MediaStyle]:
    try:
        values = json.loads(str(raw or "[]"))
    except (json.JSONDecodeError, TypeError):
        return frozenset()
    if not isinstance(values, list):
        return frozenset()
    return frozenset(
        cast(MediaStyle, value)
        for value in values
        if isinstance(value, str) and value in _STYLE_VALUES
    )


def _row_genres(raw: object) -> list[str]:
    try:
        values = json.loads(str(raw or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)]


def _row_to_result(row: sqlite3.Row) -> SearchResult | None:
    form_raw = str(row["form"] or "")
    if form_raw not in _FORM_VALUES:
        return None
    url = str(row["url"] or "")
    if not url:
        return None
    return SearchResult(
        id=str(row["id"]),
        provider=str(row["provider"]),
        form=cast(MediaForm, form_raw),
        title=str(row["title"]),
        year=cast(int | None, row["year"]),
        poster=cast(str | None, row["poster"]),
        url=url,
        styles=_row_styles(row["styles"]),
        genres=_row_genres(row["genres"]),
        imdb_id=cast(str | None, row["imdb_id"]),
    )


def _escape_fts(token: str) -> str:
    """Quote-safe FTS token (tokens are already regex-clean)."""
    return token.replace('"', '""')


def _fts_query(alternatives: Sequence[Sequence[str]]) -> str:
    """Build ``("a"* OR "b"*) AND ("c"*)`` from per-token alternatives.

    AND across tokens is deliberate: a two-word query must match both
    words. The LIKE fallback below still catches middle-of-word matches,
    but it also requires every token, so a local hit never becomes a
    noisy one-word match.
    """
    clauses: list[str] = []
    for variants in alternatives:
        terms = [f'"{_escape_fts(variant)}"*' for variant in variants if variant]
        if terms:
            clauses.append("(" + " OR ".join(terms) + ")")
    return " AND ".join(clauses)


def _rank_key(
    result: SearchResult,
    alternatives: Sequence[Sequence[str]],
    last_seen: float,
) -> tuple[int, float, int, int, str]:
    """Sort key: native exact → native prefix → transliteration → all-token
    coverage → rest, then freshness/year/shorter-title. Lower is better.

    Native spelling outranks a transliteration collision: «Дюна» must beat
    «Дуна!» for the query «дюна», even when the latter is fresher."""
    title_norm = _normalize_for_index(result.title) or result.title.lower()
    official, loose = _transliterate(title_norm)
    latin_tokens = set(f"{official} {loose}".split())
    hay_tokens = set(title_norm.split()) | latin_tokens
    hay_tokens.update(genre.lower() for genre in result.genres)

    query_text = " ".join(variants[0] for variants in alternatives if variants)
    query_official, query_loose = _transliterate(query_text)
    query_variants = {query_text, query_official, query_loose}

    title_tokens = set(title_norm.split())
    native_exact = title_norm == query_text
    native_prefix = any(
        any(word.startswith(variants[0]) for word in title_tokens)
        for variants in alternatives
        if variants
    )
    latin_exact = bool(latin_tokens & query_variants)
    latin_prefix = any(
        word.startswith(variant)
        for word in latin_tokens
        for variant in query_variants
    )
    coverage = all(
        any(word.startswith(variant) for word in hay_tokens for variant in variants)
        for variants in alternatives
    )
    if native_exact:
        tier = 0
    elif native_prefix:
        tier = 1
    elif latin_exact or latin_prefix:
        tier = 2
    elif coverage:
        tier = 3
    else:
        tier = 4
    return (tier, -last_seen, -(result.year or 0), len(title_norm), result.title)


class CatalogIndex:
    """One SQLite catalog file. Thread-safe; the connection opens in
    ``__init__`` so a broken path fails at construction, not mid-search."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            os.path.expanduser(path), check_same_thread=False, timeout=5.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        self._conn = connection
        self._fts = self._ensure_schema()

    @property
    def path(self) -> str:
        return self._path

    @property
    def fts_enabled(self) -> bool:
        return self._fts

    @property
    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("catalog index is closed")
        return self._conn

    def _ensure_schema(self) -> bool:
        with self._lock:
            connection = self._connection
            connection.execute(_CREATE_TABLE)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS catalog_provider_idx ON catalog(provider)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS catalog_seen_idx ON catalog(last_seen)"
            )
            fts = True
            try:
                connection.execute(_CREATE_FTS)
                for trigger in _FTS_TRIGGERS:
                    connection.execute(trigger)
            except sqlite3.OperationalError as error:
                # A Python/SQLite build without FTS5 still gets a working
                # index through the LIKE fallback; the catalog table is
                # the source of truth either way.
                log.warning("SQLite FTS5 unavailable; catalog search falls back to LIKE: %s", error)
                fts = False
            connection.commit()
            return fts

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM catalog").fetchone()
        return int(row["n"]) if row is not None else 0

    def upsert(self, results: Iterable[SearchResult], *, now: float | None = None) -> int:
        """Insert/refresh one batch. Returns the number of distinct ids."""
        stamp = time.time() if now is None else now
        rows: dict[str, tuple[object, ...]] = {}
        for result in results:
            if not result.id or not result.title:
                continue
            aliases = title_aliases(result.title)
            title_norm = aliases[0] if aliases else _normalize_for_index(result.title)
            if not title_norm:
                continue
            # Index EVERY alias, not just the primary one: DLE titles often
            # read «Тато / Daddy», and a viewer may type either language.
            title_text = " ".join(aliases) if aliases else title_norm
            official, loose = _transliterate(title_text)
            title_latin = official if official == loose else f"{official} {loose}"
            rows[result.id] = (
                result.id,
                result.provider,
                result.title,
                title_norm,
                title_latin,
                result.year,
                result.form,
                json.dumps(sorted(result.styles), ensure_ascii=False),
                json.dumps(list(result.genres), ensure_ascii=False),
                result.poster,
                result.url,
                result.imdb_id,
                stamp,
                _search_text(title_text, official, loose, result.genres),
            )
        if not rows:
            return 0
        with self._lock:
            self._connection.executemany(_UPSERT_SQL, list(rows.values()))
            self._connection.commit()
        return len(rows)

    def _fetch_fts(self, query: str, limit: int) -> list[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT c.*, bm25(catalog_fts) AS rank
            FROM catalog_fts
            JOIN catalog AS c ON c.rowid = catalog_fts.rowid
            WHERE catalog_fts MATCH ?
            ORDER BY bm25(catalog_fts)
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()

    def _fetch_like(self, alternatives: Sequence[Sequence[str]], limit: int) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[object] = []
        for variants in alternatives:
            terms = [f"%{variant}%" for variant in variants if variant]
            if not terms:
                continue
            clauses.append("(" + " OR ".join("c.search_text LIKE ?" for _ in terms) + ")")
            params.extend(terms)
        if not clauses:
            return []
        params.append(limit)
        return self._connection.execute(
            "SELECT c.* FROM catalog AS c WHERE "
            + " AND ".join(clauses)
            + " ORDER BY c.last_seen DESC LIMIT ?",
            params,
        ).fetchall()

    def _query_rows(
        self, alternatives: Sequence[Sequence[str]], limit: int
    ) -> list[sqlite3.Row]:
        if self._fts:
            try:
                rows = self._fetch_fts(_fts_query(alternatives), limit)
                if rows:
                    return rows
            except sqlite3.OperationalError as error:
                log.warning("catalog FTS query failed; falling back to LIKE: %s", error)
        # FTS5 prefix search does not match the middle of a word. LIKE does,
        # and it is the only path on a build without FTS5.
        return self._fetch_like(alternatives, limit)

    def search(self, query: str, *, limit: int = 40) -> list[SearchResult]:
        """Ranked local matches, best first. No I/O beyond SQLite."""
        tokens = _query_tokens(query)
        if not tokens or limit <= 0:
            return []
        limit = min(limit, 200)
        alternatives = tuple(_token_variants(token) for token in tokens)
        with self._lock:
            rows = self._query_rows(alternatives, limit * 4)
            ranked: list[tuple[SearchResult, float]] = []
            for row in rows:
                result = _row_to_result(row)
                if result is not None:
                    ranked.append((result, float(row["last_seen"] or 0.0)))
        ranked.sort(key=lambda pair: _rank_key(pair[0], alternatives, pair[1]))
        return [result for result, _ in ranked[:limit]]

    def hints(
        self,
        query: str,
        *,
        limit: int = 10,
        allowed: Collection[str] | None = None,
    ) -> list[SearchHint]:
        """Distinct typeahead rows, one per title + form + year.

        Raw group keys are not enough here: two yearless cards of the same
        title can key differently (their digest has no year to hash), and a
        typeahead then shows the same «Дюна» four times. Known-year rows win;
        yearless duplicates collapse once a year is known, while different
        years remain distinct works (a 1984 and a 2021 series both show).
        """
        if limit <= 0:
            return []
        seen_groups: set[str] = set()
        known_years: set[tuple[str, MediaForm]] = set()
        candidates: list[SearchHint] = []
        # Wider than the final limit: fresher yearless rows can crowd the
        # head, and a known-year row must still be in the pool to win the
        # duplicate collapse below.
        for result in self.search(query, limit=limit * 10):
            if allowed is not None and result.provider not in allowed:
                continue
            key = item_group_key(result)
            if key in seen_groups:
                continue
            seen_groups.add(key)
            title_key = (
                _normalize_for_index(result.title) or result.title.lower(),
                result.form,
            )
            if result.year is not None:
                known_years.add(title_key)
            candidates.append(
                SearchHint(
                    id=result.id,
                    group_key=key,
                    title=result.title,
                    year=result.year,
                    form=result.form,
                    poster=result.poster,
                    provider=result.provider,
                )
            )
        out: list[SearchHint] = []
        seen_rows: set[tuple[str, MediaForm, int | None]] = set()
        for hint in candidates:
            title_key = (
                _normalize_for_index(hint.title) or hint.title.lower(),
                hint.form,
            )
            if hint.year is None and title_key in known_years:
                continue
            row_key = (title_key[0], hint.form, hint.year)
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            out.append(hint)
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
# Process-wide index + the narrow seam catalog.py searches through
# ---------------------------------------------------------------------------

_index: CatalogIndex | None = None
_broken_path: str | None = None


def get_index() -> CatalogIndex | None:
    """The configured index, or None when disabled/unopenable."""
    global _index, _broken_path
    path = _config.SETTINGS.catalog_db_path
    if not path:
        return None
    if _index is not None and _index.path == path:
        return _index
    if _broken_path == path:
        return None
    if _index is not None:
        _index.close()
        _index = None
    try:
        _index = CatalogIndex(path)
    except (OSError, sqlite3.Error) as error:
        log.warning("catalog index unavailable at %s: %s", path, error)
        _broken_path = path
        return None
    _broken_path = None
    return _index


def reset_index() -> None:
    """Close and forget the process-wide index (tests / config reload)."""
    global _index, _broken_path
    if _index is not None:
        _index.close()
    _index = None
    _broken_path = None


def groups_from_sources(sources: Sequence[SearchResult]) -> list[SearchGroup]:
    """Project ranked sources through the shared merge core."""
    return [
        SearchGroup(
            group_key=projection.key,
            title=projection.title,
            year=projection.year,
            poster=projection.poster,
            form=projection.form,
            styles=projection.styles,
            genres=list(projection.genres),
            sources=list(projection.sources),
            member_keys=list(projection.member_keys),
        )
        for projection in (project_group(group) for group in merge_results(sources))
    ]


def search_groups(
    query: str,
    *,
    limit: int = 60,
    allowed: Collection[str] | None = None,
    form: MediaForm | None = None,
    style_filter: frozenset[MediaStyle] | None = None,
) -> list[SearchGroup] | None:
    """Local matches as merged groups, or None when the index is disabled.

    ``[]`` is the honest "enabled but nothing matched" answer; callers use
    the difference to decide between local-first and live-only paths.
    """
    index = get_index()
    if index is None:
        return None
    try:
        results = index.search(query, limit=limit * 3)
    except sqlite3.Error as error:
        # A corrupt/locked cache must degrade to the live fan-out, never
        # fail the search request.
        log.warning("catalog index search failed; using live search: %s", error)
        return None
    if allowed is not None:
        results = [result for result in results if result.provider in allowed]
    if form is not None or style_filter is not None:
        results = [
            result for result in results if matches_axes(result, form, style_filter)
        ]
    if not results:
        return []
    return groups_from_sources(results)[:limit]


def remember_groups(groups: Sequence[SearchGroup]) -> int:
    """Index the sources of live/hybrid search groups. Best-effort."""
    index = get_index()
    if index is None:
        return 0
    sources = [source for group in groups for source in group.sources]
    try:
        return index.upsert(sources)
    except Exception as error:  # noqa: BLE001 - indexing must never break a search
        log.warning("catalog index upsert failed: %s", error)
        return 0


def hints(
    query: str,
    *,
    limit: int = 10,
    allowed: Collection[str] | None = None,
) -> list[SearchHint]:
    """Index-only typeahead hints (no upstream fan-out)."""
    index = get_index()
    if index is None:
        return []
    try:
        return index.hints(query, limit=limit, allowed=allowed)
    except Exception as error:  # noqa: BLE001 - typeahead must never 500
        log.warning("catalog index hints failed: %s", error)
        return []


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCrawlStats:
    """What one provider contributed to a crawl run."""

    provider: str
    sections: int
    pages: int
    seen: int
    indexed: int
    errors: tuple[str, ...]
    #: Crawl passes that walked this provider. 1 = answered first time;
    #: >1 = the run re-asked it after it had answered no page at all.
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.pages > 0


@dataclass(frozen=True)
class CrawlStats:
    """One crawl run's per-provider facts and totals."""

    providers: tuple[ProviderCrawlStats, ...]
    total_seen: int
    total_indexed: int

    @property
    def failed_providers(self) -> tuple[str, ...]:
        return tuple(provider.provider for provider in self.providers if not provider.ok)


#: Extra passes for providers that answered NOTHING (every section failed).
#: A whole-provider failure is usually a transient upstream or Cloudflare
#: outage, so the re-ask runs after the rest of the crawl: by the time the
#: provider is walked again the other providers have bought a few minutes.
#: A provider that lost only one section mid-crawl keeps its recorded error
#: and is NOT re-walked — re-reading sections that already answered would
#: spend upstream calls without adding cards.
PROVIDER_RETRY_PASSES = 2
#: Pause before a retry pass, multiplied by the pass number (20 s, 40 s).
#: Off in tests; on in the nightly unit so a short wobble has time to settle.
PROVIDER_RETRY_DELAY_S = 20.0


async def _crawl_provider(
    provider_id: str,
    *,
    page_limit: int,
    index: CatalogIndex,
    client: httpx.AsyncClient,
    stamp: float,
    delay_s: float,
    attempts: int = 1,
) -> ProviderCrawlStats:
    """Walk ONE provider's sections into ``index``; never raises upstream.

    A failed section is recorded and skipped, so one bad page never aborts
    the provider or the run. ``attempts`` only labels the walk in the run's
    stats; the retry loop in :func:`crawl_index` decides when to call again.
    """
    provider: BaseProvider | None = PROVIDERS.get(provider_id)
    if provider is None:
        return ProviderCrawlStats(
            provider_id, 0, 0, 0, 0, ("unknown provider",), attempts
        )
    sections: tuple[Section, ...] = provider.sections
    if not sections:
        return ProviderCrawlStats(
            provider_id, 0, 0, 0, 0, ("no browsable sections",), attempts
        )
    fetched_pages = 0
    seen = 0
    indexed = 0
    errors: list[str] = []
    for section in sections:
        for page in range(1, page_limit + 1):
            try:
                items, has_next = await provider.browse(section.id, page, client)
            except Exception as error:  # noqa: BLE001 - one bad page must not abort
                errors.append(f"{section.id} p{page}: {error}")
                break
            fetched_pages += 1
            if not items:
                break
            items = [item for item in items if section_matches(item, section)]
            seen += len(items)
            if items:
                try:
                    indexed += index.upsert(items, now=stamp)
                except sqlite3.Error as error:
                    errors.append(f"{section.id} p{page} index: {error}")
                    break
            if not has_next:
                break
            if delay_s > 0:
                await asyncio.sleep(delay_s)
    return ProviderCrawlStats(
        provider=provider_id,
        sections=len(sections),
        pages=fetched_pages,
        seen=seen,
        indexed=indexed,
        errors=tuple(errors),
        attempts=attempts,
    )


async def crawl_index(
    provider_ids: Sequence[str],
    *,
    pages: int,
    index: CatalogIndex,
    http: httpx.AsyncClient | None = None,
    delay_s: float = 0.0,
    retry_passes: int = PROVIDER_RETRY_PASSES,
    retry_delay_s: float = PROVIDER_RETRY_DELAY_S,
) -> CrawlStats:
    """Walk each provider's sections, page by page, into ``index``.

    Reuses the providers' own ``browse()`` adapters, so the crawler sees
    exactly what ``/api/browse`` sees (including each section's Model B
    filter). A failed section is recorded and skipped; one bad upstream
    never aborts the whole run. Sequential by design — the crawl is a
    background chore, not a latency path.

    A provider that answered no page at all is retried AFTER the main
    walk, one provider at a time, for up to ``retry_passes`` passes with a
    growing ``retry_delay_s`` pause before each. The retry's stats replace
    the failed pass wholesale (including its error list, so the report
    shows the verdict that stands); the earlier errors stay in the log.
    """
    client = http if http is not None else get_client()
    page_limit = max(1, pages)
    # One timestamp for the whole run: a row's freshness must not depend
    # on which page it happened to land on.
    stamp = time.time()
    stats = [
        await _crawl_provider(
            provider_id,
            page_limit=page_limit,
            index=index,
            client=client,
            stamp=stamp,
            delay_s=delay_s,
        )
        for provider_id in provider_ids
    ]
    for pass_no in range(1, retry_passes + 1):
        # Only a provider with sections can be re-asked: "unknown provider"
        # and "no browsable sections" are permanent verdicts, not outages.
        retry_ids = [
            stat.provider for stat in stats if not stat.ok and stat.sections > 0
        ]
        if not retry_ids:
            break
        pause = max(0.0, retry_delay_s) * pass_no
        log.warning(
            "providers answered nothing; retry pass %d/%d in %.0fs: %s",
            pass_no,
            retry_passes,
            pause,
            ", ".join(retry_ids),
        )
        if pause > 0:
            await asyncio.sleep(pause)
        retried = {
            provider_id: await _crawl_provider(
                provider_id,
                page_limit=page_limit,
                index=index,
                client=client,
                stamp=stamp,
                delay_s=delay_s,
                attempts=pass_no + 1,
            )
            for provider_id in retry_ids
        }
        stats = [retried.get(stat.provider, stat) for stat in stats]
    return CrawlStats(
        providers=tuple(stats),
        total_seen=sum(provider.seen for provider in stats),
        total_indexed=sum(provider.indexed for provider in stats),
    )
