"""Conditional GET (ETag / Last-Modified) for the catalog crawler.

The crawler re-reads the same first pages every night. Most origins send a
validator (``ETag`` or ``Last-Modified``) on those listing pages; replaying
the request with ``If-None-Match`` / ``If-Modified-Since`` lets the origin
answer ``304 Not Modified`` instead of the full HTML, so an unchanged page
costs one tiny round trip instead of a download and a parse.

This module is a transport, not a provider feature: every adapter already
receives one shared ``httpx.AsyncClient``, so wrapping its transport gives
the whole crawl conditional requests without touching 20 parsers. The cache
is SQLite next to the catalog index, so the validators survive between
nightly runs — an in-memory cache would forget them every night and save
nothing.

Deliberate limits:

  - GET + 200 only: redirects are left alone (``safe_get`` re-asks each hop
    anyway) and error pages are never cached;
  - only responses that carry at least one validator are stored, so a site
    that does not support the protocol is simply never cached;
  - only text/JSON/XML bodies below ``MAX_BODY_BYTES`` are stored;
  - a caller that already sent a conditional header is passed through as-is
    (the transport will not overwrite its own conditions or replay a body
    for a 304 it did not ask for).

On ``Cache-Control: no-store``: several WordPress/DLE hosts send that
header by boilerplate yet still advertise ``Last-Modified`` and answer
``304``. The entry here is never served as a cache hit — the body is only
replayed for the SAME URL after the origin just confirmed it is current,
and it never leaves this process. Skipping those hosts would make
conditional GET pointless exactly where it works, so the directive is
noted in the docstring rather than used as a veto. If an origin ever
starts answering a conditional request with a full 200, the stored copy is
simply replaced, so the worst case is one unconditional fetch.

Serving a cached body is safe by construction: a 304 means the origin just
vouched that the representation it gave us is still current. The cached
headers have the wire-level framing and content-coding stripped and the body
stored decoded, so the replay is a plain 200 the parsers read normally.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from . import config as _config
from .http_client import USER_AGENT

log = logging.getLogger("cs_uk_api.http_cache")

#: Bodies above this are not cached: listing pages are a few hundred KB, and
#: a media file must never end up in the cache DB.
MAX_BODY_BYTES = 4 * 1024 * 1024

#: Validators older than this are dropped on open. An origin that stopped
#: sending validators must not pin an entry forever.
PRUNE_AFTER_S = 14 * 24 * 3600

#: Total body budget. Distinct URLs are bounded by the crawl (a few
#: thousand pages), but 4 MiB pages would still add up; the oldest row is
#: evicted until the store fits.
MAX_CACHE_BYTES = 512 * 1024 * 1024

#: Replay headers are rebuilt from the cached ones; framing and coding are
#: dropped because the stored body is the DECODED representation.
_DROP_HEADERS = frozenset(
    {"content-encoding", "content-length", "transfer-encoding", "connection"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    headers       TEXT NOT NULL,
    body          BLOB NOT NULL,
    fetched_at    REAL NOT NULL
)
"""


@dataclass(frozen=True)
class CachedPage:
    """One stored listing page and the validators that answer for it."""

    etag: str | None
    last_modified: str | None
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class CacheSummary:
    """Counters for the run report (``index_catalog`` prints them)."""

    stored: int
    revalidated: int
    bytes_saved: int


class ConditionalCache:
    """SQLite store of validator + body, keyed by the request URL."""

    def __init__(
        self, path: str | Path, *, max_bytes: int = MAX_CACHE_BYTES
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db: sqlite3.Connection | None = sqlite3.connect(
            self.path, check_same_thread=False
        )
        self._db.execute(_SCHEMA)
        self._db.commit()
        self._prune()
        self.stored = 0
        self.revalidated = 0
        self.bytes_saved = 0

    def get(self, url: str) -> CachedPage | None:
        db = self._require_db()
        row = db.execute(
            "SELECT etag, last_modified, headers, body FROM pages WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            return None
        etag, last_modified, raw_headers, body = row
        try:
            headers = tuple((str(k), str(v)) for k, v in json.loads(raw_headers))
        except (TypeError, ValueError):
            # A corrupted row is a missing row; drop it rather than fail
            # the crawl over the cache.
            db.execute("DELETE FROM pages WHERE url = ?", (url,))
            db.commit()
            return None
        return CachedPage(
            etag=etag, last_modified=last_modified, headers=headers, body=body
        )

    def put(
        self,
        url: str,
        *,
        body: bytes,
        headers: list[tuple[str, str]],
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        db = self._require_db()
        db.execute(
            """
            INSERT INTO pages (url, etag, last_modified, headers, body, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                headers = excluded.headers,
                body = excluded.body,
                fetched_at = excluded.fetched_at
            """,
            (
                url,
                etag,
                last_modified,
                json.dumps(headers),
                body,
                time.time(),
            ),
        )
        db.commit()
        self._enforce_size_cap()
        self.stored += 1

    def touch(
        self, url: str, *, etag: str | None, last_modified: str | None
    ) -> CachedPage | None:
        """Record a 304: refresh validators and the freshness timestamp."""
        db = self._require_db()
        db.execute(
            "UPDATE pages SET etag = ?, last_modified = ?, fetched_at = ? "
            "WHERE url = ?",
            (etag, last_modified, time.time(), url),
        )
        db.commit()
        page = self.get(url)
        if page is not None:
            self.revalidated += 1
            self.bytes_saved += len(page.body)
        return page

    def summary(self) -> CacheSummary:
        return CacheSummary(
            stored=self.stored,
            revalidated=self.revalidated,
            bytes_saved=self.bytes_saved,
        )

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _prune(self) -> None:
        db = self._require_db()
        db.execute(
            "DELETE FROM pages WHERE fetched_at < ?", (time.time() - PRUNE_AFTER_S,)
        )
        db.commit()

    def _enforce_size_cap(self) -> None:
        db = self._require_db()
        total = int(
            db.execute("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM pages").fetchone()[0]
        )
        while total > self.max_bytes:
            row = db.execute(
                "SELECT url, LENGTH(body) FROM pages "
                "ORDER BY fetched_at ASC, rowid ASC LIMIT 1"
            ).fetchone()
            if row is None:
                break
            db.execute("DELETE FROM pages WHERE url = ?", (row[0],))
            total -= int(row[1])
        db.commit()

    def _require_db(self) -> sqlite3.Connection:
        if self._db is None:
            raise sqlite3.ProgrammingError("cache is closed")
        return self._db


def _cacheable_content_type(value: str | None) -> bool:
    if not value:
        return True
    lowered = value.lower()
    return (
        lowered.startswith("text/")
        or "json" in lowered
        or "xml" in lowered
    )


#: IMF-fixdate, the only format a modern server is required to emit.
#: ``parsedate_to_datetime`` alone is too lenient — it happily accepts
#: ``Wed, 21 Oct 2026 20:12:15 -1100 GMT``, which is not a valid date.
_IMF_FIXDATE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"\d{4} \d{2}:\d{2}:\d{2} GMT$"
)


def _valid_http_date(value: str | None) -> str | None:
    """Only store a ``Last-Modified`` we can send back verbatim.

    Some DLE hosts emit dates like ``Wed, 21 Oct 2026 20:12:15 -1100 GMT``;
    replaying that as ``If-Modified-Since`` risks a 400 on a stricter server.
    """
    if not value or not _IMF_FIXDATE.match(value):
        return None
    try:
        parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return value


def _stored_headers(response: httpx.Response) -> list[tuple[str, str]]:
    return [
        (key, value)
        for key, value in response.headers.multi_items()
        if key.lower() not in _DROP_HEADERS
    ]


def _replay(request: httpx.Request, page: CachedPage) -> httpx.Response:
    return httpx.Response(
        200, headers=list(page.headers), content=page.body, request=request
    )


class CachingTransport(httpx.AsyncBaseTransport):
    """Async transport that answers 304s from :class:`ConditionalCache`."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, cache: ConditionalCache
    ) -> None:
        self._inner = inner
        self.cache = cache

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        caller_conditional = bool(
            request.headers.get("if-none-match")
            or request.headers.get("if-modified-since")
        )
        cached: CachedPage | None = None
        if request.method == "GET" and not caller_conditional:
            cached = self.cache.get(url)
            if cached is not None:
                if cached.etag:
                    request.headers["If-None-Match"] = cached.etag
                if cached.last_modified:
                    request.headers["If-Modified-Since"] = cached.last_modified
        response = await self._inner.handle_async_request(request)
        if cached is not None and response.status_code == 304:
            await response.aclose()
            page = self.cache.touch(
                url,
                etag=response.headers.get("etag") or cached.etag,
                last_modified=(
                    _valid_http_date(response.headers.get("last-modified"))
                    or cached.last_modified
                ),
            )
            log.debug("304 %s", url)
            return _replay(request, page if page is not None else cached)
        if request.method == "GET" and response.status_code == 200:
            await self._store(url, response)
        return response

    async def _store(self, url: str, response: httpx.Response) -> None:
        etag = response.headers.get("etag")
        last_modified = _valid_http_date(response.headers.get("last-modified"))
        if not (etag or last_modified):
            return
        if not _cacheable_content_type(response.headers.get("content-type")):
            return
        # ``aread`` applies the content decoder, so the stored body is the
        # representation the parsers see, not the gzip envelope.
        await response.aread()
        body = response.content
        if len(body) > MAX_BODY_BYTES:
            return
        self.cache.put(
            url,
            body=body,
            headers=_stored_headers(response),
            etag=etag,
            last_modified=last_modified,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_conditional_client(cache: ConditionalCache) -> httpx.AsyncClient:
    """The crawler's client: same policy as the API client, plus the cache."""
    return httpx.AsyncClient(
        transport=CachingTransport(httpx.AsyncHTTPTransport(), cache),
        timeout=httpx.Timeout(_config.SETTINGS.upstream_timeout_s),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=False,
    )
