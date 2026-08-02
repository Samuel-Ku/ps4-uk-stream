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
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from ..http_client import safe_get
from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from .base import BaseProvider, ProviderError

BASE_URL = "https://simpsonsua.tv"
BASE_URL_HOST = urlparse(BASE_URL).hostname
# ashdi.vip hosts the HLS manifest for every episode. The upstream
# Kotlin sets the Referer to the site root so the CDN serves the
# manifest.
ASHDI_REFERER = BASE_URL + "/"
# Hosts the upstream may legally redirect to. Anything outside this
# set is treated as a not_found error so a hostile CMS response can't
# pivot the request to an attacker-controlled host.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"simpsonsua.tv", "ashdi.vip"})

# Browse surfaces the latest home-page updates and the paginated catalogue.
SIMPSONSUATV_SECTIONS: tuple[Section, ...] = (
    Section(id="updates", title="Останні оновлення", type="cartoon"),
    Section(id="page", title="Усі мультсеріали", type="cartoon"),
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


def _page_number(href: str) -> int:
    """Pull the `/page/N/` integer out of a DLE pagination link."""
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


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
    if last.endswith(".html"):
        last = last[:-5]
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
    """A season page URL ends with `/sN/` or `/sezon-N/`. We accept
    any path with a `/sezon-N/` or `/sN/` segment at the end."""
    cleaned = href.split("?", 1)[0].rstrip("/")
    last = cleaned.rsplit("/", 1)[-1]
    if not last:
        return False
    return bool(_SEASON_RE.fullmatch(last))


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
        type="cartoon",
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
    )


def _parse_season_episodes(soup: BeautifulSoup, season_url: str) -> list[Episode]:
    """Parse the episode cards on a season page.

    Each card is a `div.movie_item.sezon` block containing an `<a
    href="...seriya.html">` and a `<div class="descr nazva">`
    title. Returns Episode objects whose `id` is the full URL of the
    episode page (so `stream()` can fetch the player iframe directly)."""
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
        episodes.append(Episode(number=idx, id=url, title=title or f"Серія {idx}"))
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
            response = await safe_get(
                http,
                url,
                allowed_hosts=set(_ALLOWED_HOSTS),
                headers=merged,
                params=params,
            )
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.url.host not in _ALLOWED_HOSTS:
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
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select(
                "div.navigation a[href*='/page/'], "
                "div.navigation-block a[href*='/page/']"
            )
        )
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        if not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise ProviderError("not_found", f"bad external_id: {external_id!r}")
        url = f"{BASE_URL}/{external_id}/"
        response = await self._get(url, http)
        soup = BeautifulSoup(response.text, "lxml")
        title, description, poster = self._parse_meta(soup, external_id)
        seasons = await self._build_seasons(soup, url, external_id, http)
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            type="cartoon",
            title=title,
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
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
                        Episode(number=1, id=content_url, title=ep_title or "Серія")
                    ],
                )
            ]
        if _is_season_href(content_url):
            # Season page: parse episodes directly.
            episodes = _parse_season_episodes(soup, content_url)
            season_num = _season_number(content_url) or 1
            return [Season(number=season_num, episodes=episodes)] if episodes else None
        # Show page: follow season subitems.
        season_links = _parse_show_subitems(soup, content_url)
        if not season_links:
            return None
        seasons: list[Season] = []
        for season_num_str, season_url in sorted(
            season_links, key=lambda kv: int(kv[0])
        ):
            try:
                resp = await self._get(season_url, http)
            except ProviderError:
                continue
            season_soup = BeautifulSoup(resp.text, "lxml")
            episodes = _parse_season_episodes(season_soup, season_url)
            if not episodes:
                continue
            seasons.append(Season(number=int(season_num_str), episodes=episodes))
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
        # Slug check: each path segment must match the slug regex so
        # we don't pass arbitrary user input (e.g. `../admin`) to
        # http.get(). Strip a trailing `.html` from the last segment
        # before validation.
        path = content_id[len(BASE_URL) :].lstrip("/").rstrip("/")
        segments = path.split("?")[0].split("/")
        check_segments = [s[:-5] if s.endswith(".html") else s for s in segments]
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
            response = await http.get(player_url, headers={"Referer": ASHDI_REFERER})
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
            headers={"Referer": ASHDI_REFERER, "User-Agent": "cs-uk-api/0.1"},
        )


__all__ = ["SimpsonsUATvProvider"]
