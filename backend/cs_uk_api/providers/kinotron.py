"""KinoTron provider (https://kinotron.tv), HTML and inline-player JSON."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..country import extract_country
from ..http_client import safe_get
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
from ..wire_identity import MOVIE_SUFFIX
from .base import BaseProvider, MediaTypeStr, ProviderError, model_b_axes, parse_actor_list

BASE_URL = "https://kinotron.tv"
# Hosts the upstream may legally redirect to: the DLE CMS and the ashdi
# player. A hostile CMS response must not be able to pivot either hop
# to an attacker-controlled host.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"kinotron.tv", "ashdi.vip"})
SECTIONS = (
    Section(id="films", title="Фільми", form="movie"),
    Section(id="serials", title="Серіали", form="series"),
    Section(id="cartoons", title="Мультфільми", form="movie"),
    Section(id="cartoon-series", title="Мультсеріали", styles=frozenset({"cartoon"})),
    Section(id="anime", title="Аніме", styles=frozenset({"anime"})),
)

# external_id is a numeric-prefixed slug (e.g. "10496-mesniki-..."). Gate
# the URL interpolation against path-traversal payloads before hitting the
# upstream HTTP client.
_SLUG_RE = re.compile(r"\d+-[a-z0-9][a-z0-9-]*")

# Sentinel episode-id suffix for movies (whose player iframe is a single
# URL rather than a season/episode map; defined once in
# ``wire_identity``, spec #309).


def _external_id(href: str) -> str:
    match = re.search(r"/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href, re.IGNORECASE)
    if not match:
        raise ProviderError("parse_failed", f"unrecognized url: {href}")
    return match.group(1)


def _page_number(href: str) -> int:
    match = re.search(r"/page/(\d+)/?", href)
    return int(match.group(1)) if match else 0


def _parse_cards(html: str, provider: str, media_type: MediaTypeStr) -> list[SearchResult]:
    soup = BeautifulSoup(html, "lxml")
    results: list[SearchResult] = []
    seen: set[str] = set()
    for card in soup.select(".th-item"):
        link = card.select_one(".th-in")
        if link is None or not link.get("href"):
            continue
        try:
            external_id = _external_id(str(link["href"]))
        except ProviderError:
            continue
        if external_id in seen:
            continue
        seen.add(external_id)
        title_el = card.select_one(".th-title")
        image = card.select_one(".img-fit img")
        title = title_el.get_text(" ", strip=True) if title_el else link.get_text(" ", strip=True)
        year_match = re.search(r"\b(?:19|20)\d{2}\b", title)
        poster = urljoin(BASE_URL, str(image.get("data-src"))) if image and image.get("data-src") else None
        mb_form, mb_styles = model_b_axes(media_type)
        results.append(SearchResult(
            id=f"{provider}:{external_id}", provider=provider,
            title=title, year=int(year_match.group()) if year_match else None,
            poster=poster, url=urljoin(BASE_URL, str(link["href"])),
            form=mb_form, styles=mb_styles,
        ))
    return results


class KinoTronProvider(BaseProvider):
    id = "kinotron"
    name = "KinoTron"
    types = ("movie", "series", "cartoon", "anime")
    sections = SECTIONS
    #: ``content()`` gates trailer-only (youtube-only) pages (#163) so
    #: the catalog sweep drops dead cards from home/search.
    can_gate = True

    @staticmethod
    def _type_from_subtitle(subtitle: str) -> MediaTypeStr:
        text = BeautifulSoup(subtitle, "lxml").get_text(" ", strip=True)
        if "Мультсеріал" in text:
            return "cartoon"
        if "Аніме" in text:
            return "anime"
        if "Серіал" in text:
            return "series"
        return "movie"

    @staticmethod
    def _player_url(soup: BeautifulSoup) -> str | None:
        """First REAL (non-youtube) player iframe in the video box.

        Trailer-only titles (issue #163) carry only a youtube embed in
        ``div.video-box`` — upstream has no playable player — so a
        youtube-only box yields None and ``content()`` gates the card.
        """
        for iframe in soup.select("div.video-box iframe"):
            src = str(iframe.get("data-src") or "")
            if not src:
                continue
            host = urlsplit(urljoin(BASE_URL, src)).hostname or ""
            if "youtube.com" in host or host == "youtu.be":
                continue
            return urljoin(BASE_URL, src)
        return None

    @staticmethod
    def _files(player_html: str) -> list[dict[str, object]]:
        scripts = BeautifulSoup(player_html, "lxml").select("script")
        text = next((item.get_text() for item in scripts if "file" in item.get_text()), "")
        # The player serves the payload in single *or* double quotes
        # (`file:'[{...}]'` from ashdi serials vs `file:"https://..."`
        # from zetvideo vod movies) — match either (live-gate: a movie
        # was wrongly gated because only single quotes matched).
        match = re.search(r"file\s*:\s*(?:\"([^\"]+)\"|'([^']+)')", text)
        if not match:
            return []
        payload = match.group(1) or match.group(2)
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError:
            return [{"dub": "", "season": "", "title": "", "file": payload}]
        files: list[dict[str, object]] = []
        for dub in raw if isinstance(raw, list) else []:
            for season in dub.get("folder", []) if isinstance(dub, dict) else []:
                for episode in season.get("folder", []) if isinstance(season, dict) else []:
                    if isinstance(episode, dict) and episode.get("file"):
                        files.append({"dub": dub.get("title", ""), "season": season.get("title", ""), **episode})
        return files

    async def _get(self, url: str, http: httpx.AsyncClient) -> httpx.Response:
        try:
            response = await http.get(url, headers={"Referer": f"{BASE_URL}/"})
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        return response

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        try:
            response = await http.post(f"{BASE_URL}/index.php?do=search", data={
                "do": "search", "subaction": "search", "story": quote(query),
            })
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code != 200:
            raise ProviderError("upstream_unreachable", f"status {response.status_code}")
        return _parse_cards(response.text, self.id, "series")

    async def browse(self, section: str, page: int, http: httpx.AsyncClient) -> tuple[list[SearchResult], bool]:
        if not self.has_section(section):
            raise ProviderError("not_found", f"unknown section: {section}")
        url = f"{BASE_URL}/{section}/page/{page}/"
        # The upstream now 301-redirects the first page of most sections
        # (`/serials/page/1/` -> `/serials/`), so fetch through the
        # SSRF-safe `safe_get` helper (same host allowlist as the ashdi
        # player) which follows allowed same-host redirects. Pages > 1
        # still return 200 directly and are unaffected.
        try:
            response = await safe_get(http, url, allowed_hosts=set(_ALLOWED_HOSTS))
        except httpx.HTTPError as error:
            raise ProviderError("unreachable", str(error)) from error
        if response.status_code != 200:
            raise ProviderError("not_found", f"status {response.status_code}")
        soup = BeautifulSoup(response.text, "lxml")
        # Contract #135: sections carry Model B axes, not the legacy
        # ``type`` — the card classifier needs the legacy type string,
        # derived from the axes (style wins, else form).
        axes = next(item for item in self.sections if item.id == section)
        section_type = (
            min(axes.styles) if axes.styles else (axes.form or "series")
        )
        results = _parse_cards(response.text, self.id, section_type)
        has_next = any(_page_number(str(a.get("href", ""))) > page for a in soup.select(".navigation a[href*='/page/']"))
        return results, has_next

    async def content(self, external_id: str, http: httpx.AsyncClient) -> ContentResponse:
        if not _SLUG_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
        response = await self._get(f"{BASE_URL}/{external_id}.html", http)
        soup = BeautifulSoup(response.text, "lxml")
        title_el = soup.select_one(".full h1")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        image = soup.select_one(".img-box img")
        poster = urljoin(BASE_URL, str(image.get("data-src"))) if image and image.get("data-src") else None
        kind = self._type_from_subtitle(str(soup.select_one("div.fsubtitle") or ""))
        country: str | None = extract_country(soup)
        player_url = self._player_url(soup)
        # Issue #163: a youtube-only video box is a trailer-only title —
        # upstream has no playable player. Gate so the ADR-0002 sweep
        # drops the dead card instead of failing only at play time.
        if player_url is None:
            raise ProviderError("gated", "trailer only — no playable player")
        seasons = None
        translations = [Translation(id="uk", label="Українська")]
        translations_level: TranslationLevel = "content"
        if kind in {"series", "cartoon", "anime"} and player_url:
            player = await self._get(player_url, http)
            files = self._files(player.text)
            grouped: dict[str, dict[str, list[str]]] = {}
            for item in files:
                season = str(item.get("season", "")).strip() or "Сезон 1"
                episode = str(item.get("title", "")).strip()
                dub = str(item.get("dub", "")).strip() or "Українська"
                grouped.setdefault(season, {}).setdefault(episode, []).append(dub)
            all_dubs = list(dict.fromkeys(str(item.get("dub", "")).strip() for item in files))
            translations = [
                Translation(id=dub, label=dub) for dub in all_dubs if dub
            ] or [Translation(id="uk", label="Українська")]
            seasons = [
                Season(
                    number=season_number,
                    episodes=[
                        Episode(
                            number=episode_number,
                            id=f"{self.id}:{external_id}:s{season_number}e{episode_number}",
                            title=episode_title,
                            translations=[Translation(id=dub, label=dub) for dub in dubs],
                        )
                        for episode_number, (episode_title, dubs) in enumerate(episodes.items(), 1)
                    ],
                )
                for season_number, episodes in enumerate(grouped.values(), 1)
            ]
            translations_level = "episode"
        else:
            # Issue #167: a movie whose player page exposes no playable
            # files (upstream migrated several titles to a dead
            # zetvideo.net/vod/<id> page — nginx 404 body, observed live
            # 2026-08-09) must be gated at content() time so the
            # catalog sweep drops the dead card instead of failing only
            # at play time.
            player = await self._get(player_url, http)
            if not self._files(player.text):
                raise ProviderError("gated", "no playable files on player page")
        description_el = soup.select_one(".full-text")
        # Ticket #221: the page's ``В ролях:`` li lists the cast with
        # one ``/xfsearch/actors/<name>/`` anchor per person.
        cast = parse_actor_list(
            soup, "В ролях", self.id, re.compile(r"/actors/([^/]+)/?$")
        )
        mb_form, mb_styles = model_b_axes(kind)
        return ContentResponse(id=f"{self.id}:{external_id}", title=title_el.get_text(" ", strip=True),
            description=description_el.get_text(" ", strip=True) if description_el else "",
            poster=poster, translations=translations, seasons=seasons, translations_level=translations_level, country=country,
            form=mb_form, styles=mb_styles, people=cast)

    async def stream(self, content_id: str, translation: str | None, http: httpx.AsyncClient) -> StreamResponse:
        # `content_id` arrives from /api/stream with the `<provider>:`
        # prefix already stripped: "<external_id>" (movie),
        # "<external_id>:__movie__" (movie from the content listing), or
        # "<external_id>:s<N>e<M>" (series episode).
        if MOVIE_SUFFIX in content_id:
            external_id = content_id.split(MOVIE_SUFFIX, 1)[0]
            episode_match = None
        elif ":" in content_id:
            external_id, _, ep_suffix = content_id.rpartition(":")
            episode_match = re.fullmatch(r"s(\d+)e(\d+)", ep_suffix)
        else:
            external_id = content_id
            episode_match = None
        if not _SLUG_RE.fullmatch(external_id):
            raise ProviderError("not_found", "bad external_id")
        content = await self._get(f"{BASE_URL}/{external_id}.html", http)
        player_url = self._player_url(BeautifulSoup(content.text, "lxml"))
        if not player_url:
            raise ProviderError("parse_failed", "no player iframe found")
        player = await self._get(player_url, http)
        files = self._files(player.text)
        if not files:
            raise ProviderError("parse_failed", "no stream URL found")
        selected = files[0]
        if episode_match:
            season_number, episode_number = map(int, episode_match.groups())
            season_names = list(dict.fromkeys(str(item.get("season", "")).strip() for item in files))
            if season_number < 1 or season_number > len(season_names):
                raise ProviderError("not_found", "season not found")
            season_files = [item for item in files if str(item.get("season", "")).strip() == season_names[season_number - 1]]
            episode_titles = list(dict.fromkeys(str(item.get("title", "")).strip() for item in season_files))
            if episode_number < 1 or episode_number > len(episode_titles):
                raise ProviderError("not_found", "episode not found")
            matches = [item for item in season_files if str(item.get("title", "")).strip() == episode_titles[episode_number - 1]]
            selected = next((item for item in matches if str(item.get("dub", "")).strip() == translation), matches[0])
        return StreamResponse(url=str(selected["file"]), type="m3u8", headers={"Referer": player_url, "User-Agent": "cs-uk-api/1.0"})


__all__ = ["KinoTronProvider"]
