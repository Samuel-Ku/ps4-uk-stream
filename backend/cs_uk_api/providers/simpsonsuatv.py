"""SimpsonsUATv provider (https://simpsonsua.tv) — Ukrainian-dubbed
cartoon archive (Сімпсони, Футурама, Гріфіни, Південний Парк, ...).
Issue #17, Group 3.

The site is a DLE-style CMS. The home page has a `div.ep_slider` block
that lists the latest 15 episode updates (each with a `-seriya` URL),
plus a `div.movie_item` block per show card. The
`/multserialy-ukrainskoyu/` listing surfaces all show/series pages.
Search uses `GET /?s=QUERY` (matches the upstream Kotlin `?s=...`).
The show page nests season URLs; the season page nests episode URLs;
the episode page exposes a single `<iframe data-player="..." src="...">`
pointing at ashdi.vip. The player page embeds a plain `file: '...m3u8'`.

External-id shape: a slug matching `[a-z0-9][a-z0-9-]+` that names
either a show (e.g. `simpsony`), a season (e.g. `s35`, `sezon-1`),
or a specific episode (e.g. `4441-37-sezon-17-seriya`). The provider
validates the slug at both `content()` and `stream()` boundaries to
refuse path traversal before any HTTP request is made.

Season cap: a show page's `content()` surfaces only the newest
`_MAX_SHOW_SEASONS` seasons (audit #138 kept the cap — see the
constant's comment for the measured rationale). The Simpsons archive
has 37 seasons, and the CMS both serialises concurrent season fetches
and rate-limits bursts, so enumerating every season runs close to the
request budget and silently drops seasons to HTTP 429s. Older seasons
remain reachable directly via their own season slug (e.g. `content("s5")`).
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from ..http_client import provider_safe_get
from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from .base import BaseProvider, ProviderError, dle_has_next

BASE_URL = "https://simpsonsua.tv"
BASE_URL_HOST = urlparse(BASE_URL).hostname
# ashdi.vip hosts the HLS manifest for every episode. The upstream
# Kotlin sets the Referer to the site root so the CDN serves the
# manifest.
ASHDI_REFERER = BASE_URL + "/"
# Hosts the upstream may legally redirect to. Anything outside this
# set is treated as a not_found error so a hostile CMS response can't
# pivot the request to an attacker-controlled host.

# How many season subpages a show page may fetch at once. A show like
# The Simpsons has 37 seasons; sequential fetches pushed content() past
# 30s (issue #119), so season enumeration runs with a bounded concurrency.
_SEASON_FETCH_CONCURRENCY = 6

# How many seasons a show page surfaces. Audit #138 (measured live on
# simpsonsua.tv, 2026-08-08) found the cap is load-bearing, so it is
# kept. A cap-off `content()` for The Simpsons (37 seasons, fetched
# 6-wide) took 25.5s — inside the D6 >30s symptom budget but with no
# headroom — and, decisively, the 38-request sweep tripped CMS
# rate-limiting (HTTP 429 / connection drops) that silently dropped 14
# of 37 seasons. So dropping the cap is not merely slow, it is lossy:
# the same rate-limit that makes it slow also corrupts the result. The
# cap value 10 = 11 upstream requests for a show page, which completes
# in ~8-10s (single page ~0.95s, and the CMS serialises concurrent
# requests — 6 parallel fetches took 4.69s wall, only ~1.2x faster than
# sequential) and stays under the rate-limit burst threshold. We return
# the newest 10 seasons (the hot path); the price is that older seasons
# (1-27 for The Simpsons) vanish from the show's browsable rail and are
# only reachable directly via their own season slug (e.g. `content("s5")`)
# or season external id.
_MAX_SHOW_SEASONS = 10

# Browse surfaces the latest home-page updates and the paginated catalogue.
SIMPSONSUATV_SECTIONS: tuple[Section, ...] = (
    Section(id="updates", title="Останні оновлення", styles=frozenset({"cartoon"})),
    Section(id="page", title="Усі мультсеріали", styles=frozenset({"cartoon"})),
)

# External-id regex. Accepts a slug, a season (e.g. `s35`, `sezon-1`),
# or a news_id-slug episode (e.g. `4441-37-sezon-17-seriya`). The
# initial character must be alphanumeric; trailing `-.` are rejected.
_EXTERNAL_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]+")

# Season number parser. Matches `sezon-1`, `s35`, etc. Returns 0 if
# the URL is not a season page.
_SEASON_RE = re.compile(r"(?:sezon-|s)(\d+)")
# Episode number parser. Matches `N-sezon` or `N-seriya`.
_EPISODE_NUM_RE = re.compile(r"(\d+)-seriya")

# URL patterns the upstream Kotlin ignores (matches `ignoredUrlPatterns`).
# Kept in sync so `content()` doesn't recurse into navigation pages.
_IGNORED_PATTERNS: tuple[str, ...] = (
    "/multserialy-ukrainskoyu/",
    "/terms.html",
    "/subscribe.html",
    "/index.php",
    "do=login",
    "t.me/",
    "youtube.com",
    "tiktok.com",
    "x.com/",
    "franecki.net",
    "franeski.net",
    "javascript:",
)

# Subitem slugs on a show page that are not seasons and should be
# skipped (matches `specialSlugs` in the upstream Kotlin). These
# redirect to thematic playlists that don't follow the season/episode
# shape we can map to v2.
_SPECIAL_SLUGS: frozenset[str] = frozenset(
    {
        "inshe",
        "dobirky",
        "simpsony-u-kino",
        "halloween",
        "rizdvo",
        "majbutnye",
        "main-simpsons-episodes",
        "lito",
        "pereyizd",
        "podoroz",
        "shkola",
        "love",
        "lgbt",
        "patrik",
        "tracey-ullman-show",
    }
)

# Upstream currently spells this slug `riksanchez`; the live site uses
# `rick137`, so retain both aliases to avoid drifting from either source.
_TITLE_MAP: dict[str, str] = {
    "simpsony": "Сімпсони",
    "allfuturama": "Футурама",
    "family-guy": "Гріфіни",
    "pivdennyi-park": "Південний Парк",
    "riksanchez": "Рік та Морті",
    "rick137": "Рік та Морті",
    "solar-opposites": "Сонячні протилежності",
    "rozcharuvannya": "Розчарування",
    "duncanville": "Дунканвілл",
    "nevkolupnyi": "Невразливий",
    "central-park": "Центральний Парк",
    "sponge": "Губка Боб Квадратні Штани",
    "american-dad": "Американський тато",
    "clevelandshow": "Шоу Клівленда",
    "brickleberry": "Бріклбері",
    "pd-paradise": "Поліція Парадайз",
    "polus": "Полюс",
    "bojack": "Кінь BoДжек",
    "tuca-and-bertie": "Тука і Bertie",
    "big-mouth": "Великий рот",
    "gravity-falls": "Ґравіті Фолз",
    "amfibiya": "Амфібія",
    "owl-house": "Совиний Дім",
    "hotel-hazbin": "Готель Хазбін",
    "pekelniy-bos": "Пекельний бос",
    "gilda": "Гільда",
    "final-space": "Космічний рубіж",
    "adventure-time": "Час пригод",
    "star-proty-syl-zla": "Зоряна принцеса проти сил зла",
    "opivnichne-evangelie": "Опівнічне Євангеліє",
    "infinity-train": "Нескінченний поїзд",
    "my-little-pony": "My Little Pony",
    "maylo-merfi": "Закон Майла Мерфі",
    "fineas-ferb": "Фінеас і Ферб",
    "rockos-modern-life": "Сучасне рок-життя Рокко",
    "invader-zim": "Загарвник Зім",
}




def _slug_from_href(href: str) -> str | None:
    """Return the trailing path segment of a URL, with `.html` and
    trailing slashes stripped. Returns None for unrecognised inputs."""
    # Strip scheme/host so we work on absolute and relative paths.
    cleaned = href.split("?", 1)[0].rstrip("/")
    if not cleaned:
        return None
    last = cleaned.rsplit("/", 1)[-1]
    if not last:
        return None
    # Strip `.html` if present.
    last = last.removesuffix(".html")
    return last or None


def _is_valid_content_url(href: str) -> bool:
    """Mirror of `isValidContentUrl` from the upstream Kotlin: skip
    navigation/login/social URLs that don't represent playable content."""
    if not href.startswith("http") and not href.startswith("/"):
        return False
    return not any(p in href for p in _IGNORED_PATTERNS)


def _season_number(url: str) -> int:
    """Mirror of `parseSeasonNumber` from the upstream Kotlin: pull the
    integer out of `sezon-N` or `sN`. Returns 0 if the URL is not a
    season page."""
    m = _SEASON_RE.search(url)
    return int(m.group(1)) if m else 0


def _episode_number(url: str, fallback: int) -> int:
    """Mirror of `parseEpisodeNumber` from the upstream Kotlin: pull
    the integer out of `N-seriya`. Returns `fallback` for non-episode
    URLs."""
    m = _EPISODE_NUM_RE.search(url)
    return int(m.group(1)) if m else fallback


def _is_season_href(href: str) -> bool:
    """A season page URL ends in `/sezon-N/` or `/sN/`. The season token
    may be slug-prefixed (`/futurama-sezon-11/`), but it must be the
    final token: news-id episode slugs like
    `4467-...-1-sezon-2-seriya` embed a `sezon-N` token followed by
    `-seriya`, so they are not seasons."""
    last = _slug_from_href(href)
    if not last:
        return False
    m = _SEASON_RE.search(last)
    return m is not None and m.end() == len(last)


def _clean_title(text: str) -> str:
    """Strip trailing marketing text like `дивитися онлайн` or
    `українською ...` from content titles. Mirror of `cleanTitle` in
    the upstream Kotlin."""
    if not text:
        return text
    cleaned = re.sub(r"дивитися онлайн.*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"українською.*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _title_for_slug(slug: str) -> str:
    """Look up a canonical Ukrainian title for a known show slug, or
    return a humanised version of the slug."""
    if slug in _TITLE_MAP:
        return _TITLE_MAP[slug]
    return slug.replace("-", " ").capitalize()


def _parse_card(card: Tag, provider_id: str) -> SearchResult | None:
    """Parse one listing card."""
    a = card if card.name == "a" else card.select_one("a")
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    if not _is_valid_content_url(href):
        return None
    slug = _slug_from_href(href)
    if slug is None:
        return None
    # The site's poster path is a leading-relative URL on the
    # `/photos/...` root; urljoin handles absolute and relative
    # inputs the same way.
    img = card.select_one("img")
    poster_src: str | None = None
    if img is not None:
        for attr in ("data-src", "data-lazy-src", "src"):
            val = img.get(attr)
            if isinstance(val, str) and val:
                poster_src = val
                break
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    # Title: prefer an HTML comment that the upstream Kotlin reads via
    # `getTitleFromComment`; fall back to `alt`/`title` on the image;
    # fall back to the slug/titleMap lookup.
    title_raw = img.get("alt") if img is not None else None
    if isinstance(title_raw, str) and title_raw:
        title = title_raw
    else:
        # The show page's `figure a img alt` is empty; the multserialy
        # page uses the titleMap. Both upstream and ours prefer the
        # map so the result is stable across listings.
        title = _title_for_slug(slug)
    return SearchResult(
        id=f"{provider_id}:{slug}",
        provider=provider_id,
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form="series",
        styles=frozenset({"cartoon"}),
    )


def _parse_season_episodes(
    soup: BeautifulSoup, season_url: str, provider_id: str
) -> list[Episode]:
    """Parse the episode cards on a season page.

    Each card is a `div.movie_item.sezon` block containing an `<a
    href="...seriya.html">` and a `<div class="descr nazva">`
    title. Returns Episode objects whose `id` is the full URL of the
    episode page prefixed with the provider id (so the /api/stream
    router can split on the first ':' and hand `stream()` the bare URL
    to fetch the player iframe directly)."""
    episodes: list[Episode] = []
    for idx, card in enumerate(soup.select("div.movie_item"), start=1):
        a = card.select_one("a")
        if a is None or not a.get("href"):
            continue
        href = str(a["href"])
        if "-seriya" not in href:
            continue
        url = urljoin(season_url, href)
        title_el = card.select_one(".descr.nazva")
        title = _clean_title(
            title_el.get_text(strip=True) if title_el else f"Серія {idx}"
        )
        episodes.append(
            Episode(
                number=idx, id=f"{provider_id}:{url}", title=title or f"Серія {idx}"
            )
        )
    return episodes


def _parse_show_subitems(soup: BeautifulSoup, show_url: str) -> list[tuple[str, str]]:
    """Pull (label, href) pairs from the show page's movie_item cards.

    Only season links are kept (matching the upstream Kotlin's
    `parseSeasonNumber(it.href) > 0` filter). Special-section slugs
    (inshe, dobirky, halloween, ...) are dropped because they don't
    expose a season/episode shape mappable to v2."""
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for card in soup.select("div.movie_item"):
        a = card.select_one("a")
        if a is None or not a.get("href"):
            continue
        href = str(a["href"])
        if not _is_valid_content_url(href):
            continue
        if not _is_season_href(href):
            continue
        url = urljoin(show_url, href)
        if url in seen:
            continue
        seen.add(url)
        slug = _slug_from_href(href) or ""
        if slug in _SPECIAL_SLUGS:
            continue
        season_num = _season_number(href)
        if season_num <= 0:
            continue
        items.append((str(season_num), url))
    return items


class SimpsonsUATvProvider(BaseProvider):
    id = "simpsonsuatv"
    name = "SimpsonsUA"
    types = ("cartoon", "series")
    sections = SIMPSONSUATV_SECTIONS
    #: Site + the ashdi.vip player pages it embeds (ADR-0005).
    allowed_hosts = frozenset({"simpsonsua.tv", "ashdi.vip"})
    # v3 (issue #70): "Останні оновлення" contributes to «Новинки».
    # (The second section, ``"page"``, is the full catalogue and does
    # NOT opt into «Новинки» — only the ``updates`` slot does.)
    newest_section = "updates"

    async def _get(
        self,
        url: str,
        http: httpx.AsyncClient,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Shared GET helper. `headers` is concatenated with the
        default `Referer` so callers can override per-request."""
        merged: dict[str, str] = {"Referer": BASE_URL + "/"}
        if headers:
            merged.update(headers)
        try:
            response = await provider_safe_get(
                http,
                self,
                url,
                headers=merged,
                params=params,
            )
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.url.host not in self.allowed_hosts:
            raise ProviderError("not_found", "unexpected upstream host")
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        return response

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # The upstream Kotlin hits the site root with `?s=QUERY` (GET).
        # httpx url-encodes the query via `params`, so Cyrillic and
        # reserved characters round-trip without double-encoding.
        response = await self._get(BASE_URL + "/", http, params={"s": query})
        soup = BeautifulSoup(response.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select("div.movie_item"):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        return results

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        if section == "updates":
            response = await self._get(BASE_URL + "/", http)
            soup = BeautifulSoup(response.text, "lxml")
            cards = soup.select("div.ep_slider div.movie_item")
            if not cards:
                cards = soup.select("div.su-updates-grid a.su-card")
            update_results = [
                parsed
                for card in cards[:15]
                if (parsed := _parse_card(card, self.id)) is not None
            ]
            return update_results, False
        if section != "page":
            raise ProviderError("not_found", f"unknown section: {section}")
        if page <= 1:
            url = f"{BASE_URL}/multserialy-ukrainskoyu/"
        else:
            url = f"{BASE_URL}/multserialy-ukrainskoyu/page/{page}/"
        response = await self._get(url, http)
        soup = BeautifulSoup(response.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select("div.movie_item"):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        # DLE pagination: a `<a href=".../page/N/">` link to a higher
        # page than `page` means there is a next page. The wrapper
        # class on this site is `navigation-block` (not the usual
        # `navigation`) so we match both.
        has_next = dle_has_next(response.text, page)
        return results, has_next

    async def _open_content_page(
        self, url: str, external_id: str, http: httpx.AsyncClient
    ) -> httpx.Response:
        """Fetch one content page, trying the ``.html`` variant when the
        directory spelling 404s.

        Update-slider episodes carry a bare episode slug
        (``4467-...-seriya``) that only resolves under the season's
        directory (``/prezydent-kertis-sezon-1/…``). ``BASE/<slug>.html``
        answers with a 301 to the canonical page, so the fallback repairs
        the exact case; a genuinely missing page re-raises the original
        ``not_found``."""
        try:
            return await self._get(url, http)
        except ProviderError as first:
            if "status 404" not in str(first):
                raise
            try:
                return await self._get(f"{BASE_URL}/{external_id}.html", http)
            except ProviderError:
                raise first

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        if not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise ProviderError("not_found", f"bad external_id: {external_id!r}")
        url = f"{BASE_URL}/{external_id}/"
        response = await self._open_content_page(url, external_id, http)
        soup = BeautifulSoup(response.text, "lxml")
        title, description, poster = self._parse_meta(soup, external_id)
        seasons = await self._build_seasons(soup, str(response.url), external_id, http)
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            title=title,
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
            form="series",
            styles=frozenset({"cartoon"}),
        )

    def _parse_meta(
        self, soup: BeautifulSoup, external_id: str
    ) -> tuple[str, str, str | None]:
        """Extract title, description, and poster from a content page.

        Mirrors the upstream Kotlin `.poster h2, .cat-nazva h1, h1`
        selector chain and the `.sez-opys, .fullstory, div.story`
        description chain."""
        title_el = soup.select_one(".poster h2, .cat-nazva h1, h1")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        title = _clean_title(title_el.get_text(strip=True))
        if not title:
            title = _title_for_slug(external_id)
        desc_el = soup.select_one(".sez-opys, .fullstory, div.story")
        description = desc_el.get_text(strip=True) if desc_el else ""
        # Poster: first meaningful `<img>` inside `.movie_item, div.story,
        # .poster`. We pick the first one whose `src` is non-empty and
        # not a `spacer.gif`.
        poster: str | None = None
        for img in soup.select(".movie_item img, div.story img, .poster img"):
            for attr in ("data-src", "data-lazy-src", "src"):
                val = img.get(attr)
                if isinstance(val, str) and val and "spacer" not in val:
                    poster = urljoin(BASE_URL, val)
                    break
            if poster:
                break
        return title, description, poster

    async def _build_seasons(
        self,
        soup: BeautifulSoup,
        content_url: str,
        external_id: str,
        http: httpx.AsyncClient,
    ) -> list[Season] | None:
        """Build the seasons/episodes list for a content page.

        Three shapes:
        1. The content page is a show: movie_item cards link to
           season subpages; we fetch each one to enumerate episodes.
        2. The content page is a season (`s35`, `sezon-N`): movie_item
           cards link directly to episodes; we parse them inline.
        3. The content page is a single episode (`-seriya`): no
           children, so we synthesise a single Season with a single
           Episode pointing at this same page.

        Returns None if no seasons can be resolved so the caller
        surfaces an empty seasons list rather than raising.
        """
        # Detect which shape we're in.
        if "seriya" in content_url:
            # Already an episode page — return a single-season
            # placeholder so the response model is valid.
            title_el = soup.select_one(".poster h2, h1")
            ep_title = (
                _clean_title(title_el.get_text(strip=True))
                if title_el is not None
                else "Серія"
            )
            return [
                Season(
                    number=1,
                    episodes=[
                        Episode(
                            number=1,
                            id=f"{self.id}:{content_url}",
                            title=ep_title or "Серія",
                        )
                    ],
                )
            ]
        if _is_season_href(content_url):
            # Season page: parse episodes directly.
            episodes = _parse_season_episodes(soup, content_url, self.id)
            season_num = _season_number(content_url) or 1
            return [Season(number=season_num, episodes=episodes)] if episodes else None
        # Show page: follow season subitems. Fetch the season pages
        # concurrently (bounded by a semaphore) so a 37-season archive
        # resolves in a few seconds instead of 30+ sequential hops
        # (issue #119). A failed season is skipped, matching the old
        # sequential behaviour.
        season_links = _parse_show_subitems(soup, content_url)
        if not season_links:
            return None
        # Newest first, bounded by _MAX_SHOW_SEASONS so long archives
        # (The Simpsons has 37 seasons) resolve within the request
        # budget instead of timing out.
        ordered = sorted(season_links, key=lambda kv: int(kv[0]))[-_MAX_SHOW_SEASONS:]
        semaphore = asyncio.Semaphore(_SEASON_FETCH_CONCURRENCY)

        async def fetch_season(
            season_num_str: str, season_url: str
        ) -> Season | None:
            async with semaphore:
                try:
                    resp = await self._get(season_url, http)
                except ProviderError:
                    return None
            season_soup = BeautifulSoup(resp.text, "lxml")
            episodes = _parse_season_episodes(season_soup, season_url, self.id)
            if not episodes:
                return None
            return Season(number=int(season_num_str), episodes=episodes)

        fetched = await asyncio.gather(
            *(fetch_season(num, url) for num, url in ordered)
        )
        seasons = [s for s in fetched if s is not None]
        return seasons or None

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # `content_id` is the full URL of the episode page (set by
        # `content()` so callers can pass the Episode.id directly).
        # The /api/stream router strips the `<provider>:` prefix before
        # calling us, so we get the bare URL.
        if not content_id.startswith(f"{BASE_URL}/"):
            raise ProviderError("not_found", f"bad content_id: {content_id!r}")
        # Host check + per-segment slug check so we don't pass arbitrary
        # user input (e.g. `../admin`) to http.get(). The host is
        # parsed structurally (`simpsonsua.tv` carries a dot and no
        # segment regex would accept it); each path segment must match
        # the slug regex, with a trailing `.html` stripped first.
        parsed = urlparse(content_id)
        if parsed.netloc not in (BASE_URL_HOST, f"www.{BASE_URL_HOST}"):
            raise ProviderError("not_found", f"bad content_id: {content_id!r}")
        segments = [s for s in parsed.path.split("/") if s]
        check_segments = [s.removesuffix(".html") for s in segments]
        if not check_segments or not all(
            _EXTERNAL_ID_RE.fullmatch(s) for s in check_segments
        ):
            raise ProviderError("not_found", f"bad content_id: {content_id!r}")
        response = await self._get(content_id, http)
        soup = BeautifulSoup(response.text, "lxml")
        iframes = soup.find_all("iframe")
        if not iframes:
            raise ProviderError("parse_failed", "no iframe on content page")
        ordered_iframes = sorted(
            iframes,
            key=lambda iframe: "ashdi.vip" not in str(iframe.get("src") or ""),
        )
        last_error: ProviderError | None = None
        for iframe in ordered_iframes:
            src = iframe.get("src")
            if not isinstance(src, str) or not src:
                continue
            player_url = src if src.startswith("http") else f"https:{src}"
            try:
                return await self._fetch_m3u8(player_url, http)
            except ProviderError as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise ProviderError("parse_failed", "no iframe src on content page")

    async def _fetch_m3u8(
        self, player_url: str, http: httpx.AsyncClient
    ) -> StreamResponse:
        """Fetch the player page (ashdi.vip) and pull the
        `file: '...m3u8'` URL out of the inline PlayerJS script."""
        try:
            response = await provider_safe_get(
                http, self, player_url, headers={"Referer": ASHDI_REFERER}
            )
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        m = re.search(r"file\s*:\s*['\"]([^'\"]+)['\"]", response.text)
        if not m:
            raise ProviderError("parse_failed", "no file: in player page")
        m3u8 = m.group(1)
        # The Tortuga player XOR-encodes the file value. The shared
        # `decode` helper from `._tortuga` returns a plain m3u8 URL
        # when the input decodes to one; otherwise it returns the
        # input unchanged.
        if ".m3u8" not in m3u8:
            from . import _tortuga  # local import to avoid cycle on load

            decoded = _tortuga.decode(m3u8)
            if ".m3u8" in decoded:
                m3u8 = decoded
        if ".m3u8" not in m3u8:
            raise ProviderError("parse_failed", "no m3u8 in player file value")
        return StreamResponse(
            url=m3u8,
            type="m3u8",
            headers=self.stream_headers(ASHDI_REFERER),
        )


__all__ = ["SimpsonsUATvProvider"]
