"""UFDub provider (https://ufdub.com) — Ukrainian-dubbed anime, films,
serials, doramas, cartoons, mult-serials. Issue #17, Group 1."""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

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
from .base import BaseProvider, ProviderError, model_b_axes

BASE_URL = "https://ufdub.com"
# Hosts the upstream may legally redirect to. The content page lives on
# ufdub.com and the player on video.ufdub.com; a hostile CMS response
# must not be able to pivot either hop to an attacker-controlled host.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"ufdub.com", "video.ufdub.com"})

UFDUB_SECTIONS: tuple[Section, ...] = (
    Section(id="filmy", title="Фільми", type="movie"),
    Section(id="serialy", title="Серіали", type="series"),
    Section(id="doramy", title="Дорами", type="dorama"),
    Section(id="cartoons", title="Мультфільми", type="movie"),
    Section(id="multserialy", title="Мультсеріали", type="series"),
    Section(id="anime", title="Аніме", type="anime"),
)

# Path prefix -> MediaType. Order matters: longest prefixes first so
# `/cartoon-serial/` is classified as `series` (not `movie` via `cartoon`)
# and `/serial/` is not also matched by `/serials/`. Per the upstream
# Kotlin source's `when { contains("serials") -> TvType.TvSeries ... }`
# logic.
_PATH_TYPE: tuple[tuple[str, str], ...] = (
    ("cartoon-serial", "series"),  # /cartoon-serial/ (multserialy)
    ("serial", "series"),          # /serial/, /serials/
    ("cartoon", "movie"),          # /cartoon/, /cartoons/
    ("film", "movie"),             # /film/
    ("dorama", "dorama"),          # /dorama/
    ("anime", "anime"),            # /anime/
)


def _page_number(href: str) -> int:
    """Pull the `/page/N/` integer out of a DLE pagination link."""
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


def _external_id_from_url(href: str) -> str:
    """Return "kind-slug" where kind is film/serial/etc. (possibly with
    a hyphen, e.g. `cartoon-serial`) and slug is the numeric-prefixed
    part of the path (e.g. "48-fokus-pokus-hocus-pocus")."""
    m = re.search(r"/([a-z][a-z-]*?)/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href)
    if not m:
        raise ProviderError("parse_failed", f"unrecognized url: {href}")
    return f"{m.group(1)}-{m.group(2)}"


_SLUG_RE = re.compile(r"\d+-[a-z0-9-]+")

# One row of the player page's `var a = [['Title','codec',url], ...]`
# array. Titles may contain spaces/hyphens but no quotes; codec is a
# short label (mp4/720p/HD/source/web).
_EPISODE_ROW_RE = re.compile(r"\[\s*'([^']*)'\s*,\s*'[^']*'\s*,\s*'([^']*)'\s*\]")

# Sentinel episode-id suffix used by other providers for movies; kept
# here so stream() treats a bare id and an explicit movie suffix alike.
MOVIE_SUFFIX = ":__movie__"


def _type_from_url(href: str) -> str:
    """Map the URL's path segment to a MediaType."""
    lower = href.lower()
    for needle, t in _PATH_TYPE:
        if f"/{needle}" in lower:
            return t
    return "series"  # safe default


def _section_url(section: str, page: int) -> str:
    paths = {
        "filmy": "/film/",
        "serialy": "/serial/",
        "doramy": "/dorama/",
        "cartoons": "/cartoon/",
        "multserialy": "/cartoon-serial/",
        "anime": "/anime/",
    }
    if section not in paths:
        raise ProviderError("not_found", f"unknown section: {section}")
    base = f"{BASE_URL}{paths[section]}"
    # Page 1 is the index; subsequent pages use `/page/N/`.
    if page <= 1:
        return base
    return f"{base}page/{page}/"


def _parse_card(card: Tag | BeautifulSoup, provider_id: str) -> SearchResult | None:
    """Parse one listing card (.short-t anchor)."""
    a = card.select_one("a.short-t")
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    title = a.get_text(" ", strip=True)
    # Poster lives in a sibling ``.img-box`` div, not inside ``.short-text``;
    # walk up to the card container (``div.short``) to find it.
    container = card.parent
    img = container.select_one(".img-box img") if container is not None else None
    poster = urljoin(BASE_URL, str(img["src"])) if img and img.get("src") else None
    try:
        external_id = _external_id_from_url(href)
    except ProviderError:
        return None
    mb_form, mb_styles = model_b_axes(_type_from_url(href))  # type: ignore[arg-type]
    return SearchResult(
        id=f"{provider_id}:{external_id}",
        provider=provider_id,
        type=_type_from_url(href),  # type: ignore[arg-type]
        title=title,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


class UFDubProvider(BaseProvider):
    id = "ufdub"
    name = "UFDub"
    types = ("movie", "series", "anime", "dorama")
    sections = UFDUB_SECTIONS

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        url = f"{BASE_URL}/index.php?do=search"
        try:
            # DLE (UFDub's CMS) accepts a POST with the same fields the
            # upstream Kotlin uses. `quote()` handles non-ASCII Cyrillic
            # and reserved characters; httpx then url-form-encodes the
            # rest.
            resp = await http.post(
                url,
                data={
                    "do": "search",
                    "subaction": "search",
                    "story": quote(query),
                },
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select(".short-t"):
            parsed = _parse_card(card.parent or card, self.id)
            if parsed is not None:
                results.append(parsed)
        return results

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        url = _section_url(section, page)
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        # The upstream Kotlin removes `.section` (ad/featured blocks) before
        # mapping `.short` cards. Each card is `<div class="short clearfix">`
        # containing a `<div class="short-text">` with the title link. We
        # select only the inner wrapper to avoid double-counting.
        results: list[SearchResult] = []
        for card in soup.select(".short-text"):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        # has_next: DLE pagination is `<span class="navigation">` with
        # `<a href="/section/page/N/">` siblings. Any link to a higher page
        # than `page` means there is a next page.
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select("span.navigation a[href*='/page/']")
        )
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        kind, _, slug = external_id.partition("-")
        if not kind or not slug:
            raise ProviderError("parse_failed", f"invalid external_id: {external_id!r}")
        if not _SLUG_RE.fullmatch(slug):
            raise ProviderError("not_found", f"bad external_id: {external_id!r}")
        url = f"{BASE_URL}/{kind}/{external_id[len(kind) + 1:]}.html"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        title_el = soup.select_one("h1.top-title")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        poster_el = soup.select_one("div.f-poster img")
        poster = (
            urljoin(BASE_URL, str(poster_el["src"]))
            if poster_el and poster_el.get("src")
            else None
        )
        desc_el = soup.select_one("div.full-text p")
        description = desc_el.get_text(strip=True) if desc_el else ""
        # Player URL is in an <input value="..."> or an inline JS var.
        player_url = self._extract_player_url(soup)
        media_type = _type_from_url(url)
        seasons: list[Season] | None = None
        if media_type == "series" or media_type == "anime":
            seasons = await self._parse_seasons(player_url, external_id, http)
        mb_form, mb_styles = model_b_axes(media_type)  # type: ignore[arg-type]
        return ContentResponse(
            id=f"ufdub:{external_id}",
            type=media_type,  # type: ignore[arg-type]
            title=title_el.get_text(strip=True),
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
            form=mb_form,
            styles=mb_styles,
        )

    @staticmethod
    def _extract_player_url(soup: BeautifulSoup) -> str | None:
        # Upstream: `input[value*=https://video.ufdub.com]` OR
        # `var input_player="...";` in an inline script.
        for inp in soup.select("input"):
            v = inp.get("value")
            if isinstance(v, str) and "video.ufdub.com" in v:
                return v
        for script in soup.select("script"):
            text = script.get_text()
            m = re.search(r'input_player\s*=\s*["\']([^"\']+)["\']', text)
            if m:
                return m.group(1)
        return None

    async def _parse_seasons(
        self, player_url: str | None, external_id: str, http: httpx.AsyncClient
    ) -> list[Season] | None:
        """Fetch the player page and surface its `var a` array as a
        single season of episodes. UFDub gives every content page its
        own player (one season per page), so episode ids encode only
        the position within that page: `<external>:s1e<N>`."""
        if player_url is None:
            return None
        try:
            resp = await safe_get(
                http,
                player_url,
                allowed_hosts=set(_ALLOWED_HOSTS),
                headers={"Referer": f"{BASE_URL}/"},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        episodes = self._extract_episodes(resp.text)
        if not episodes:
            return None
        return [Season(number=1, episodes=[
            Episode(number=i, id=f"{external_id}:s1e{i}", title=title)
            for i, (title, _url) in enumerate(episodes, start=1)
        ])]

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # UFDub's player is a second-level page on `video.ufdub.com`. The
        # content page references it via `input_player=...`, and the real
        # media URL lives in that page's `var a = [['Серія 1','mp4', url]]`
        # array. Follow both hops (HTML + regex only, spec ground rule #4).
        # `content_id` is either the bare external id (`film-48-...` for
        # movies) or `<external>:s1e<N>` for a series episode.
        if MOVIE_SUFFIX in content_id:
            ext_id = content_id.split(MOVIE_SUFFIX, 1)[0]
            ep_suffix = ""
        elif ":" in content_id:
            ext_id, _, ep_suffix = content_id.rpartition(":")
        else:
            ext_id, ep_suffix = content_id, ""
        kind, _, slug = ext_id.partition("-")
        if not kind or not slug:
            raise ProviderError("parse_failed", f"invalid content_id: {content_id!r}")
        if not _SLUG_RE.fullmatch(slug):
            raise ProviderError("not_found", f"bad external_id: {content_id!r}")
        content_url = f"{BASE_URL}/{kind}/{ext_id[len(kind) + 1:]}.html"
        try:
            resp = await safe_get(
                http,
                content_url,
                allowed_hosts=set(_ALLOWED_HOSTS),
                headers={"Referer": f"{BASE_URL}/"},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        player_url = self._extract_player_url(soup)
        if player_url is None:
            raise ProviderError(
                "parse_failed", "no player iframe found on content page"
            )
        try:
            player_resp = await safe_get(
                http,
                player_url,
                allowed_hosts=set(_ALLOWED_HOSTS),
                headers={"Referer": f"{BASE_URL}/"},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if player_resp.status_code != 200:
            raise ProviderError("not_found", f"status {player_resp.status_code}")
        episodes = self._extract_episodes(player_resp.text)
        if not episodes:
            raise ProviderError(
                "parse_failed", "no media URL found in player page"
            )
        if ep_suffix:
            m = re.fullmatch(r"s(\d+)e(\d+)", ep_suffix)
            if not m:
                raise ProviderError(
                    "not_found", f"bad episode suffix: {ep_suffix!r}"
                )
            season, episode = int(m.group(1)), int(m.group(2))
            if season != 1 or episode < 1 or episode > len(episodes):
                raise ProviderError(
                    "not_found", f"episode out of range: {ep_suffix!r}"
                )
            _title, media_url = episodes[episode - 1]
        else:
            _title, media_url = episodes[0]
        return StreamResponse(
            url=media_url,
            type="mp4",
            headers={"Referer": f"{BASE_URL}/", "User-Agent": "cs-uk-api/0.1"},
        )

    @staticmethod
    def _extract_episodes(player_html: str) -> list[tuple[str, str]]:
        """Parse the player page's `var a = [['Title','codec',url], ...]`
        array into (title, url) pairs, in order."""
        scripts = BeautifulSoup(player_html, "lxml").select("script")
        text = next(
            (item.get_text() for item in scripts if re.search(r"\ba\s*=\s*\[", item.get_text())),
            "",
        )
        return [(m.group(1), m.group(2)) for m in _EPISODE_ROW_RE.finditer(text)]

    @staticmethod
    def _extract_media_url(player_html: str) -> str | None:
        episodes = UFDubProvider._extract_episodes(player_html)
        return episodes[0][1] if episodes else None


__all__ = ["UFDubProvider"]