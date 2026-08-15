"""KlonTV provider (https://klonua.com) — Ukrainian-dubbed films and
serials. Issue #17, Group 2. The site migrated from klon.fun to
klonua.com (2026-08); klon.fun now answers 301 to klonua.com.

The site is a DLE-style CMS (same shape as UFDub / CikavaIdeya). The
player pages live on `ashdi.vip` (PlayerJS `file: '...m3u8'`), so the
two-hop content → player → media pattern is identical to CikavaIdeya
(only the `Object(...)` JSON wrapper is absent — PlayerJS uses a plain
`file: '[...]'` string here).

External-id shape: `<section>/<slug>` where section is `films` or
`series` (matching the section ids) and slug is the URL path component
after the section prefix (e.g. `films/11719-duna-chastyna-druga`).
"""
from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

from ..country import extract_country
from ..models import (
    ContentResponse,
    Episode,
    Person,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from ..wire_identity import MOVIE_SUFFIX
from .base import (
    BaseProvider,
    ProviderError,
    ProviderErrorCode,
    model_b_axes,
    split_content_suffix,
)


def _jsonld_doc(soup: BeautifulSoup) -> dict[str, Any] | None:
    """The page's schema.org JSON-LD as a dict, or None when missing
    or malformed (shared by the cast and rating parsers)."""
    script = soup.find("script", type="application/ld+json")
    if script is None:
        return None
    try:
        data = json.loads(script.string or "")
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _jsonld_rating(soup: BeautifulSoup) -> float | None:
    """Rating from the page's schema.org ``aggregateRating`` (ticket
    #222). klonua's JSON-LD carries ``{ratingValue, bestRating,
    ratingCount}`` on the 0-10 scale — the only real score the catalog
    exposes (ufdub/kinotron only show +/- vote deltas). Returns None
    when the block is missing or the value is not a number.
    """
    data = _jsonld_doc(soup)
    if data is None:
        return None
    raw = (data.get("aggregateRating") or {}).get("ratingValue")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value


def _jsonld_cast(soup: BeautifulSoup, provider: str) -> list[Person]:
    """Cast from the page's schema.org JSON-LD (ticket #221).

    klonua's content pages embed ``application/ld+json`` with an
    ``actor[]`` (and ``director[]``) of ``{@type: Person, name}`` — the
    only cast source on the site (there is no per-person page to link).
    JSON-LD has names only, so ids are positional within the role.
    Returns [] when the block is missing or malformed.
    """
    data = _jsonld_doc(soup)
    if data is None:
        return []
    people: list[Person] = []
    for role, label in (("actor", "Actor"), ("director", "Director")):
        for i, entry in enumerate(data.get(role, []) or []):
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name.strip():
                people.append(
                    Person(id=f"{provider}:{role}:{i}", name=name.strip(), role=label)
                )
    return people

# The genre row's first link is the section (Фільми/Серіали/…) —
# a section is not a genre, so those link texts must be excluded
# when parsing the Жанр row (the section slugs match the section
# ids on klonua.com).
_SECTION_SLUGS: frozenset[str] = frozenset(
    {"films", "filmy", "series", "serialy", "multfilmy", "multserialy", "anime"}
)


def _table_info_year_genres(soup: BeautifulSoup) -> tuple[int | None, list[str]]:
    """Year and genres from the page's ``table-info__item`` rows.

    The content page carries a ``table-info__item`` block per fact
    (Рік, Країна, Жанр, Тривалість, …). Рік is a link like
    ``/year/2000/``, Жанр a row of links like ``/dramy/``. The first
    Жанр link is the section (Фільми/Серіали) and is not a genre.
    Returns (None, []) when the rows are missing.
    """
    year: int | None = None
    genres: list[str] = []
    for item in soup.select("div.table-info__item"):
        label_el = item.select_one("div.table__category")
        if label_el is None:
            continue
        label = label_el.get_text(strip=True)
        if label == "Рік:":
            link = item.select_one("a.table-info__link")
            if link is not None:
                text = link.get_text(strip=True)
                if text.isdigit():
                    year = int(text)
        elif label == "Жанр:":
            for link in item.select("a.table-info__link"):
                text = link.get_text(strip=True)
                href = str(link.get("href") or "")
                slug = href.rstrip("/").rsplit("/", 1)[-1]
                if text and slug not in _SECTION_SLUGS:
                    genres.append(text)
    return year, genres


BASE_URL = "https://klonua.com"
# ashdi.vip hosts the HLS manifest for every title. The upstream
# Kotlin sets the Referer to "https://tortuga.wtf/" so that the CDN
# serves the manifest.
ASHDI_REFERER = "https://tortuga.wtf/"

# Sections exposed by KlonTV's main navigation. Per the upstream
# `mainPage = mainPageOf(...)` in KlonTVProvider.kt, but the v2
# contract only ships `films` and `series` (multfilmy/multserialy/
# anime are out of scope for now).
KLONTV_SECTIONS: tuple[Section, ...] = (
    Section(id="films", title="Фільми", form="movie"),
    Section(id="series", title="Серіали", form="series"),
)

# Path prefix -> MediaType. The site uses `/filmy/` (with a `y`)
# and `/serialy/` while our internal section ids are `films` and
# `series` (no y). We accept both forms so callers can pass either a
# site URL or an external-id when classifying.
_PATH_TYPE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("films", "filmy"), "movie"),
    (("series", "serialy"), "series"),
)

def _page_number(href: str) -> int:
    """Pull the `/page/N/` integer out of a DLE pagination link."""
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


def _external_id_from_url(href: str) -> str | None:
    """Return "<section>/<slug>" where section is `films`/`series`
    and slug is the numeric-prefixed part of the path. Returns None
    for unrecognised URLs so the caller can skip the card."""
    # The site uses `/filmy/` (with a trailing `y`) and `/serialy/`
    # for section paths. The internal section ids are `films` (no y)
    # and `series` (no y) so we map them here.
    m = re.search(r"/(filmy|serialy)/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href)
    if not m:
        return None
    section = "series" if m.group(1) == "serialy" else "films"
    return f"{section}/{m.group(2)}"


_SLUG_RE = re.compile(r"\d+-[a-z0-9-]+")


def _strip_query_param(url: str, name: str) -> str:
    """Drop a single query parameter safely, preserving all others.

    Unlike `str.replace`, this handles the boundary cases:
    - `?multivoice&foo=bar` becomes `?foo=bar` (not `foo=bar`).
    - `?multivoice=1` becomes `` (empty query, not `=1`).
    - A bare `?multivoice` (no other params) collapses the trailing `?`.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != name
    ]
    rebuilt = "&".join(f"{k}={v}" if v else k for k, v in pairs)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, rebuilt, parts.fragment))


def _type_from_url(href: str) -> str:
    """Map the URL's path segment to a MediaType. Accepts both
    `/filmy/` and `/serialy/` (site paths) as well as our internal
    `films/<slug>` / `series/<slug>` external-id form for robustness."""
    lower = href.lower()
    for needles, t in _PATH_TYPE:
        for needle in needles:
            # External-id form (`films/...`) is matched as a path
            # segment after `klontv:` (so `:` then `films/...`). Site
            # form is `/filmy/` or `/serialy/`.
            if f"/{needle}/" in lower or f":{needle}/" in lower:
                return t
    return "series"  # safe default, matches the upstream "else TvSeries"


def _section_url(section: str, page: int) -> str:
    """Build the listing URL for a section + page.

    DLE convention: page 1 lives at the section root (`/filmy/`),
    subsequent pages use `/filmy/page/N/`. The upstream Kotlin hits
    the section root for page 1 too — we mirror that.
    """
    paths = {
        "films": "filmy",
        "series": "serialy",
    }
    if section not in paths:
        raise ProviderError(ProviderErrorCode.NOT_FOUND, f"unknown section: {section}")
    base = f"{BASE_URL}/{paths[section]}/"
    if page <= 1:
        return base
    return f"{base}page/{page}/"


def _parse_card(card: Tag, provider_id: str) -> SearchResult | None:
    """Parse one `.short-news__slide-item` listing card.

    Each card wraps an anchor with `.short-news__small-card__link` and
    a title link with `.card-link__style`. The poster `<img>` uses
    `data-src` (lazy-loaded) and the relative path `/uploads/...`.
    """
    a = card.select_one("a.card-link__style")
    if a is None or not a.get("href"):
        # Fallback for variants in older sections.
        a = card.select_one("a.short-news__small-card__link")
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    title_el = card.select_one(".card-link__text")
    if title_el is None:
        # The kotlin source also accepts `.text-module__main`; some
        # related-news cards drop the wrapper.
        title_el = card.select_one(".text-module__main")
    title = title_el.get_text(strip=True) if title_el else ""
    img = card.select_one(".card-poster__img")
    poster_src = str(img["data-src"]) if img and img.get("data-src") else None
    if poster_src is None and img is not None and img.get("src"):
        # Some pages set `src` directly (not `data-src`).
        poster_src = str(img["src"])
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    external_id = _external_id_from_url(href)
    if external_id is None:
        return None
    mb_form, mb_styles = model_b_axes(_type_from_url(href))  # type: ignore[arg-type]
    return SearchResult(
        id=f"{provider_id}:{external_id}",
        provider=provider_id,
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


def _file_url(play_html: str) -> str | None:
    """Extract the `file: '...'` value from the PlayerJS inline
    script on the ashdi.vip player page. Works for both shapes:
    `file:'https://.../index.m3u8'` (movie) and
    `file:'[{...playlist...}]'` (series)."""
    m = re.search(r"file\s*:\s*'([^']+)'", play_html)
    return m.group(1) if m else None


class KlonTVProvider(BaseProvider):
    id = "klontv"
    name = "KlonTV"
    types = ("movie", "series")
    sections = KLONTV_SECTIONS
    #: SSRF allowlist (spec #309 T8): the CMS on klonua.com and the
    #: player CDN on ashdi.vip. A hostile CMS response must not be able
    #: to pivot either hop to an attacker-controlled host.
    #: ``guarded_get`` applies this by default.
    hosts = frozenset({"klonua.com", "ashdi.vip"})

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # DLE search form posts to the site root with `do`/`subaction`/
        # `story` fields. The upstream Kotlin sends the same payload;
        # we quote the query (httpx form-encodes the rest).
        try:
            resp = await http.post(
                BASE_URL + "/",
                data={"do": "search", "subaction": "search", "story": quote(query)},
            )
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(
                ProviderErrorCode.UPSTREAM_UNREACHABLE, f"status {resp.status_code}"
            )
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select(".short-news__slide-item"):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        return results

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        url = _section_url(section, page)
        try:
            resp = await self.guarded_get(http, url)
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select(".short-news__slide-item"):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        # Pagination lives inside `<div class="navigation"><div
        # class="navigation-page"><div class="pages">…</div></div></
        # div>`. The current page is a `<span class='...disabled'>`;
        # any sibling `<a class='...' href=".../page/N/">` to a higher
        # page number means there is a next page.
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select("div.navigation div.pages a[href*='/page/']")
        )
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        kind, _, slug = external_id.partition("/")
        if not kind or not slug:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, f"invalid external_id: {external_id!r}")
        if not _SLUG_RE.fullmatch(slug):
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"bad external_id: {external_id!r}")
        path = "filmy" if kind == "films" else "serialy"
        url = f"{BASE_URL}/{path}/{slug}.html"
        # Fetch through guarded_get: the upstream 301-redirects a title
        # moved between sections (e.g. `/filmy/...` -> `/serialy/...`,
        # observed live 2026-08-09) and the guard follows same-host
        # redirects instead of surfacing a dead not_found for a card
        # whose player page is alive.
        try:
            resp = await self.guarded_get(
                http, url, headers={"Referer": BASE_URL + "/"}
            )
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        # Title selector: the upstream Kotlin uses `.seo-h1__position`
        # (a class on the `<h1>`); we accept the same.
        title_el = soup.select_one("h1.seo-h1__position")
        if title_el is None:
            # Fallback to the first h1 — same fallback the upstream
            # would get via `tryParseJson` when the JSON-LD is missing.
            title_el = soup.select_one("h1")
        if title_el is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "title missing")
        # Poster: `.cover-image` (a class on the `<img>` inside the
        # `.poster-block__poster` block). We also accept any `img`
        # with `/uploads/...` in case the class name drifts.
        poster_el = soup.select_one(".poster-block__poster img")
        if poster_el is None:
            poster_el = soup.select_one("img.cover-image")
        poster = (
            urljoin(BASE_URL, str(poster_el.get("data-src") or poster_el.get("src") or ""))
            if poster_el is not None
            else None
        ) or None
        desc_el = soup.select_one(".info-clamp__hid")
        description = desc_el.get_text(strip=True) if desc_el else ""
        country: str | None = extract_country(soup)
        # Player URL: the upstream Kotlin reads
        # `document.select(playerSelector).attr("data-src")` where
        # `playerSelector = "div.film-player iframe"`. The iframe
        # is lazy-loaded via `data-src` (no `src`).
        iframe = soup.select_one("div.film-player iframe")
        player_url: str | None = None
        if iframe is not None:
            data_src = iframe.get("data-src")
            src = iframe.get("src")
            if isinstance(data_src, str):
                player_url = data_src
            elif isinstance(src, str):
                player_url = src
        if player_url is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no player iframe on content page")
        # tvType: classify from the content URL's path; the upstream
        # Kotlin also overrides to `TvSeries` when the player URL is
        # `ashdi.vip/serial/...` (the rare case where a `/filmy/` URL
        # actually points to a serial-player page). We mirror that
        # check for safety.
        media_type = _type_from_url(url)
        if media_type != "series" and "/serial/" in player_url:
            media_type = "series"
        seasons: list[Season] | None = None
        if media_type == "series":
            seasons = await self._build_series_seasons(player_url, external_id, http, self.id)
        else:
            # Movie: single playable URL, surfaced as season 1 episode 1
            # with the `__movie__` suffix sentinel so stream() can pick
            # it up without per-episode routing.
            seasons = [Season(number=1, episodes=[Episode(
                number=1, id=f"{self.id}:{external_id}{MOVIE_SUFFIX}", title="Фільм",
            )])]
        cast = _jsonld_cast(soup, self.id)
        rating = _jsonld_rating(soup)
        year, genres = _table_info_year_genres(soup)
        mb_form, mb_styles = model_b_axes(media_type)  # type: ignore[arg-type]
        return ContentResponse(
            id=f"klontv:{external_id}",
            title=title_el.get_text(strip=True),
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
            country=country,
            form=mb_form,
            styles=mb_styles,
            people=cast,
            rating=rating,
            year=year,
            genres=genres,
        )

    async def _build_series_seasons(
        self, player_url: str, external_id: str, http: httpx.AsyncClient, provider_id: str
    ) -> list[Season] | None:
        """Fetch the player page and decode the PlayerJS playlist.

        The PlayerJS `file: '[...]'` payload is a JSON array of "dub"
        objects, each with a `folder: [...]` of seasons, each with a
        `folder: [...]` of episode dicts:
            `[{"title": " Кіно", "folder": [{"title": " Сезон 1",
              "folder": [{"title": "Серія 1", "file": "https://...m3u8",
              ...}, ...]}, ...]}]`

        Each episode becomes a `Season` of `Episode`s. We pick the
        first dub (matching the upstream Kotlin's implicit "play the
        first available dub" behaviour). Returns None on parse failure
        so the caller surfaces an empty seasons list — the live gate
        will then see no episodes and stop, instead of crashing.
        """
        try:
            resp = await self.guarded_get(
                http,
                _strip_query_param(player_url, "multivoice"),
                headers={"Referer": BASE_URL + "/"},
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        raw = _file_url(resp.text)
        if raw is None:
            return None
        try:
            dubs = cast(list[dict[str, Any]], json.loads(raw))
        except json.JSONDecodeError:
            return None
        if not dubs:
            return None
        seasons: list[Season] = []
        for dub in dubs:
            season_list = dub.get("folder") or []
            for s_idx, season in enumerate(season_list, start=1):
                episodes_raw = season.get("folder") or []
                episodes = [
                    Episode(
                        number=e_idx,
                        id=f"{provider_id}:{external_id}:s{s_idx}e{e_idx}",
                        title=str(ep.get("title", "")).strip(),
                    )
                    for e_idx, ep in enumerate(episodes_raw, start=1)
                ]
                seasons.append(Season(number=s_idx, episodes=episodes))
            # Only the first dub; matches the upstream behaviour.
            break
        return seasons or None

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # `content_id` arrives as either "<external_id>:__movie__"
        # (movie, explicit suffix from the content listing), or
        # "<external_id>:s<N>e<M>" (series episode). /api/stream
        # strips the `<provider>:` prefix before calling us, so the
        # incoming value is the external_id with the suffix attached.
        ext_id, ep_suffix = split_content_suffix(content_id)
        kind, _, slug = ext_id.partition("/")
        if not kind or not slug:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, f"invalid content_id: {content_id!r}")
        if not _SLUG_RE.fullmatch(slug):
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"bad external_id: {ext_id!r}")
        path = "filmy" if kind == "films" else "serialy"
        content_url = f"{BASE_URL}/{path}/{slug}.html"
        # Same-host redirect following as content() — a title moved
        # between sections (e.g. /filmy/ -> /serialy/) must still
        # resolve its player page instead of surfacing not_found.
        try:
            resp = await self.guarded_get(
                http, content_url, headers={"Referer": BASE_URL + "/"}
            )
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        iframe = soup.select_one("div.film-player iframe")
        if iframe is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no player iframe on content page")
        data_src = iframe.get("data-src")
        src = iframe.get("src")
        player_url = data_src if isinstance(data_src, str) else src
        if not isinstance(player_url, str):
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no player url on content page")
        # Fetch the player page (ashdi.vip/vod/<id> for movies,
        # ashdi.vip/serial/<id> for series) with the same Referer
        # header the upstream Kotlin uses. The URL came from upstream
        # HTML, so it goes through the allowlist via guarded_get (#117).
        try:
            player_resp = await self.guarded_get(
                http,
                _strip_query_param(player_url, "multivoice"),
                headers={"Referer": BASE_URL + "/"},
            )
        except httpx.HTTPError as e:
            raise ProviderError(ProviderErrorCode.UNREACHABLE, str(e)) from e
        if player_resp.status_code != 200:
            raise ProviderError(ProviderErrorCode.NOT_FOUND, f"status {player_resp.status_code}")
        raw = _file_url(player_resp.text)
        if raw is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, "no file: in player page")
        # Movies: `raw` is the m3u8 URL. Series: `raw` is a JSON
        # playlist — pick the right episode via the suffix. A bare
        # series id (no episode suffix) must NOT hand the client the
        # raw playlist JSON as a "stream" (regression class #165:
        # content() with empty seasons lets a client stream the bare
        # series id, and the JSON blob is not a playable URL).
        media_url: str | None
        if ep_suffix:
            media_url = self._select_episode_url(raw, ep_suffix)
        elif raw.startswith("["):
            media_url = None
        else:
            media_url = raw
        if media_url is None:
            raise ProviderError(ProviderErrorCode.PARSE_FAILED, f"no media url for {ep_suffix!r}")
        # ashdi.vip refuses manifest requests without the upstream
        # Referer; same Referer the upstream Kotlin uses for HLS.
        return StreamResponse(
            url=media_url,
            type="m3u8",
            headers={"Referer": ASHDI_REFERER, "User-Agent": "cs-uk-api/1.0"},
        )

    @staticmethod
    def _select_episode_url(raw: str, ep_suffix: str) -> str | None:
        """Resolve a series episode URL from the PlayerJS playlist JSON.

        Returns None when the suffix is malformed or out of range, so
        the caller surfaces an explicit `parse_failed` — there is no
        silent "first available episode" fallback (that would mask a
        missing suffix in the caller, a known regression pattern).
        """
        m = re.fullmatch(r"s(\d+)e(\d+)", ep_suffix)
        if not m:
            return None
        s_idx, e_idx = int(m.group(1)), int(m.group(2))
        try:
            dubs = cast(list[dict[str, Any]], json.loads(raw))
        except json.JSONDecodeError:
            return None
        if not dubs:
            return None
        seasons = (dubs[0].get("folder") or [])
        if not (1 <= s_idx <= len(seasons)):
            return None
        episodes_raw = seasons[s_idx - 1].get("folder") or []
        if not (1 <= e_idx <= len(episodes_raw)):
            return None
        file_url = episodes_raw[e_idx - 1].get("file")
        return str(file_url) if file_url else None


__all__ = ["KlonTVProvider"]
