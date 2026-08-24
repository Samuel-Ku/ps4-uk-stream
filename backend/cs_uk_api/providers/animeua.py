"""AnimeUA provider (https://animeua.club) — Ukrainian-dubbed anime,
anime films, OVA and ONA. HTML cards plus ashdi.vip player pages whose
inline ``file: '[...]'`` script holds either a JSON array of dubs
(series) or a direct m3u8 URL (films)."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..http_client import provider_safe_get
from ..models import (
    ContentResponse,
    Episode,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
    TranslationLevel,
)
from ..wire_identity import episode_wire_id, parse_episode_tail
from .base import BaseProvider, MediaTypeStr, ProviderError

BASE_URL = "https://animeua.club"
# The ashdi.vip CDN serves the HLS manifest only with this Referer; the
# upstream Kotlin passes the same constant to M3u8Helper.generateM3u8.
ASHDI_REFERER = "https://tortuga.wtf/"

# Sections mirror the upstream `mainPage = mainPageOf(...)`. The "page"
# section is the site root ("Нове аніме").
ANIMEUA_SECTIONS: tuple[Section, ...] = (
    Section(id="page", title="Нове аніме", styles=frozenset({"anime"})),
    Section(id="film", title="Повнометражки", form="movie"),
    Section(id="anime", title="Аніме серіали", styles=frozenset({"anime"})),
    Section(id="ona", title="ONA", styles=frozenset({"anime"})),
    Section(id="ova", title="OVA", styles=frozenset({"anime"})),
)

# Same as the upstream Kotlin `fileRegex`. The capture group is either a
# JSON array of dubs (series) or a direct m3u8 URL (films).
_FILE_RE = re.compile(r"file\s*:\s*'([^']+)'")

# external_id is a numeric-prefixed slug (e.g. "7952-dandadan"). Gate
# content()/stream()/episode_translations() against values that could
# escape the URL path before interpolation.
_SLUG_RE = re.compile(r"\d+-[a-z0-9][a-z0-9-]*")

# season title -> episode title -> [(dub name, file url)] in first
# appearance order. One episode usually appears once per dub.
_DubsMap = dict[str, dict[str, list[tuple[str, str]]]]


def _external_id(href: str) -> str:
    match = re.search(r"/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href, re.IGNORECASE)
    if not match:
        raise ProviderError("parse_failed", f"unrecognized url: {href}")
    return match.group(1)


def _page_number(href: str) -> int:
    match = re.search(r"/page/(\d+)/?", href)
    return int(match.group(1)) if match else 0


def _section_url(section: str, page: int) -> str:
    prefixes = {
        "page": "",
        "film": "/film",
        "anime": "/anime",
        "ona": "/ona",
        "ova": "/ova",
    }
    if section not in prefixes:
        raise ProviderError("not_found", f"unknown section: {section}")
    if page <= 1:
        return f"{BASE_URL}{prefixes[section]}/"
    return f"{BASE_URL}{prefixes[section]}/page/{page}/"


def _parse_cards(html: str, provider: str, media_type: MediaTypeStr) -> list[SearchResult]:
    soup = BeautifulSoup(html, "lxml")
    results: list[SearchResult] = []
    seen: set[str] = set()
    for card in soup.select("a.poster"):
        href = card.get("href")
        if not href:
            continue
        try:
            external_id = _external_id(str(href))
        except ProviderError:
            continue
        if external_id in seen:
            continue
        seen.add(external_id)
        title_el = card.select_one("h3.poster__title")
        title = title_el.get_text(" ", strip=True) if title_el else card.get_text(" ", strip=True)
        image = card.select_one(".img-fit-cover img")
        poster = urljoin(BASE_URL, str(image.get("data-src"))) if image and image.get("data-src") else None
        # animeua is an anime-only site: every item carries the anime
        # style; the form comes from the section/fixture type (film vs
        # series — search results are all "anime" = series).
        results.append(SearchResult(
            id=f"{provider}:{external_id}", provider=provider,
            title=title, poster=poster, url=urljoin(BASE_URL, str(href)),
            form=("movie" if media_type == "movie" else "series"),
            styles=frozenset({"anime"}),
        ))
    return results


def _file_value(player_html: str) -> str | None:
    """Return the ``file: '...'`` value from the player page's inline
    scripts — the upstream Kotlin applies `fileRegex` to the concatenated
    script HTML; we scan each script in turn."""
    for script in BeautifulSoup(player_html, "lxml").select("script"):
        match = _FILE_RE.search(script.get_text())
        if match:
            return match.group(1)
    return None


def _parse_dubs(raw: str | None) -> list[dict[str, Any]] | None:
    if not raw or not raw.startswith("["):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return data


def _group_episodes(dubs: list[dict[str, Any]]) -> _DubsMap:
    grouped: _DubsMap = {}
    for dub in dubs:
        dub_name = str(dub.get("title", "")).strip()
        if not dub_name:
            continue
        for season in dub.get("folder") or []:
            if not isinstance(season, dict):
                continue
            season_title = str(season.get("title", "")).strip()
            if not season_title:
                continue
            for episode in season.get("folder") or []:
                if not isinstance(episode, dict):
                    continue
                episode_title = str(episode.get("title", "")).strip()
                file_url = str(episode.get("file") or "").strip()
                if not episode_title or not file_url:
                    continue
                files = grouped.setdefault(season_title, {}).setdefault(episode_title, [])
                if not any(name == dub_name for name, _ in files):
                    files.append((dub_name, file_url))
    return grouped


def _episode_files(grouped: _DubsMap, ep_suffix: str) -> list[tuple[str, str]] | None:
    parsed = parse_episode_tail(ep_suffix)
    if parsed is None:
        return None
    s_idx, e_idx = parsed
    if not (1 <= s_idx <= len(grouped)):
        return None
    episodes = list(grouped.values())[s_idx - 1]
    if not (1 <= e_idx <= len(episodes)):
        return None
    return list(episodes.values())[e_idx - 1]


def _resolve_episode(grouped: _DubsMap, ep_suffix: str, translation: str | None) -> str | None:
    """Pick the episode's file, preferring the requested dub. Returns
    None when the suffix is malformed or out of range."""
    files = _episode_files(grouped, ep_suffix)
    if files is None:
        return None
    for name, url in files:
        if name == translation:
            return url
    return files[0][1]


def _build_seasons(grouped: _DubsMap, external_id: str, provider_id: str) -> list[Season]:
    seasons: list[Season] = []
    for s_idx, (_, episodes) in enumerate(grouped.items(), 1):
        seasons.append(Season(
            number=s_idx,
            episodes=[
                Episode(
                    number=e_idx,
                    id=episode_wire_id(provider_id, external_id, s_idx, e_idx),
                    title=episode_title,
                    translations=[Translation(id=name, label=name) for name, _ in files],
                )
                for e_idx, (episode_title, files) in enumerate(episodes.items(), 1)
            ],
        ))
    return seasons


def _dub_names(grouped: _DubsMap) -> list[str]:
    return list(dict.fromkeys(
        name
        for episodes in grouped.values()
        for files in episodes.values()
        for name, _ in files
    ))


class AnimeUAProvider(BaseProvider):
    id = "animeua"
    name = "AnimeUA"
    types = ("anime", "movie")
    sections = ANIMEUA_SECTIONS
    # v3 (issue #70): "Нове аніме" contributes to «Новинки».
    newest_section = "page"
    #: The animeua.club CMS plus the ashdi.vip player pages its
    #: iframe srcs point at (ADR-0005).
    allowed_hosts = frozenset({"animeua.club", "ashdi.vip"})

    @staticmethod
    def _player_url(soup: BeautifulSoup) -> str | None:
        """First non-empty `.video-responsive > iframe` data-src."""
        for iframe in soup.select(".video-responsive > iframe"):
            src = str(iframe.get("data-src") or "").strip()
            if src:
                return src
        return None

    @staticmethod
    def _type_from_player(tags: list[str], player_url: str | None) -> MediaTypeStr:
        # Mirrors the upstream Kotlin `tvType` when-block: an
        # `ashdi.vip/serial/...` player means an anime serial;
        # "Повнометражка" among the genre tags means a film; OVA/ONA
        # and everything else stay anime. We keep animeua typed as
        # `anime` (not `series`) because every entry on this site is
        # animation — surfacing them as `series` would inflate the
        # serial catalogue with anime titles.
        if player_url and "/serial/" in player_url:
            return "anime"
        if any("Повнометражка" in tag for tag in tags):
            return "movie"
        return "anime"

    async def _get(self, url: str, http: httpx.AsyncClient) -> httpx.Response:
        try:
            response = await provider_safe_get(
                http, self, url, headers={"Referer": f"{BASE_URL}/"}
            )
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        return response

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        try:
            # httpx's form encoder percent-encodes the value, so a raw
            # multi-word query is correct. Pre-replacing spaces with `+`
            # here would double-encode the `+` and turn the needle into
            # the literal string `"foo+bar"` server-side.
            response = await http.post(BASE_URL, data={
                "do": "search", "subaction": "search", "story": query,
            })
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code != 200:
            raise ProviderError("upstream_unreachable", f"status {response.status_code}")
        return _parse_cards(response.text, self.id, "anime")

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        response = await self._get(_section_url(section, page), http)
        # Contract #135: sections carry Model B axes, not the legacy
        # ``type`` — the card classifier needs the legacy type string,
        # derived from the axes (style wins, else form).
        axes = next(item for item in self.sections if item.id == section)
        section_type = (
            min(axes.styles) if axes.styles else (axes.form or "series")
        )
        results = _parse_cards(response.text, self.id, section_type)
        soup = BeautifulSoup(response.text, "lxml")
        has_next = any(
            _page_number(str(a.get("href", ""))) > page
            for a in soup.select("div#pagination a[href*='/page/']")
        )
        return results, has_next

    async def content(self, external_id: str, http: httpx.AsyncClient) -> ContentResponse:
        if not _SLUG_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
        response = await self._get(f"{BASE_URL}/{external_id}.html", http)
        soup = BeautifulSoup(response.text, "lxml")
        title_el = soup.select_one(".page__subcol-main h1")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        image = soup.select_one("div.page__subcol-side .img-fit-cover img")
        poster = urljoin(BASE_URL, str(image.get("data-src"))) if image and image.get("data-src") else None
        desc_el = soup.select_one(".full-text")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""
        tags = [a.get_text(" ", strip=True) for a in soup.select(".pmovie__genres a")]
        year_el = soup.select_one(".pmovie__year")
        year: int | None = None
        if year_el is not None:
            year_match = re.search(r"\b(?:19|20)\d{2}\b", year_el.get_text(" ", strip=True))
            year = int(year_match.group()) if year_match else None
        player_url = self._player_url(soup)
        kind = self._type_from_player(tags, player_url)
        translations = [Translation(id="uk", label="Українська")]
        translations_level: TranslationLevel = "content"
        seasons: list[Season] | None = None
        if kind == "anime" and player_url:
            player = await self._get(player_url, http)
            dubs = _parse_dubs(_file_value(player.text))
            if dubs:
                grouped = _group_episodes(dubs)
                seasons = _build_seasons(grouped, external_id, self.id)
                names = _dub_names(grouped)
                if names:
                    translations = [Translation(id=name, label=name) for name in names]
                translations_level = "episode"
        # kind="movie" (Повнометражка) means an anime film — form=movie
        # with the anime style, not the default plain movie; kind="anime"
        # means an anime series (form=series). The axes always carry the
        # anime style — every entry on this site is animation.
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            title=title_el.get_text(" ", strip=True),
            year=year,
            description=description,
            poster=poster,
            translations=translations,
            form=("movie" if kind == "movie" else "series"),
            styles=frozenset({"anime"}),
            seasons=seasons,
            translations_level=translations_level,
            # Ticket #213: the ``.pmovie__genres`` tag list (already
            # parsed above for kind detection) IS the genre metadata —
            # surface it on the detail so the Jellyfin detail page can
            # render the genre row.
            genres=tags,
        )

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        if ":" in content_id:
            external_id, _, ep_suffix = content_id.rpartition(":")
        else:
            external_id, ep_suffix = content_id, ""
        if not _SLUG_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
        response = await self._get(f"{BASE_URL}/{external_id}.html", http)
        player_url = self._player_url(BeautifulSoup(response.text, "lxml"))
        if not player_url:
            raise ProviderError("parse_failed", "no player iframe found")
        player = await self._get(player_url, http)
        raw = _file_value(player.text)
        dubs = _parse_dubs(raw)
        if dubs is not None:
            url = _resolve_episode(_group_episodes(dubs), ep_suffix, translation)
        else:
            # Film player: the `file:` value is the direct m3u8 URL.
            url = raw if raw and not ep_suffix else None
        if url is None:
            raise ProviderError("parse_failed", f"no stream URL for {ep_suffix!r}")
        return StreamResponse(
            url=url,
            type="m3u8",
            headers=self.stream_headers(ASHDI_REFERER),
        )

    async def episode_translations(
        self, content_id: str, http: httpx.AsyncClient
    ) -> list[str] | None:
        """Return the dub ids available for a specific episode, or None
        when the episode cannot be resolved (caller falls back to
        content-level translations)."""
        if ":" not in content_id:
            return None
        external_id, _, ep_suffix = content_id.rpartition(":")
        if not _SLUG_RE.fullmatch(external_id):
            return None
        response = await self._get(f"{BASE_URL}/{external_id}.html", http)
        player_url = self._player_url(BeautifulSoup(response.text, "lxml"))
        if not player_url:
            return None
        player = await self._get(player_url, http)
        dubs = _parse_dubs(_file_value(player.text))
        if dubs is None:
            return None
        files = _episode_files(_group_episodes(dubs), ep_suffix)
        if files is None:
            return None
        return [name for name, _ in files]


__all__ = ["AnimeUAProvider"]
