"""KinoTron provider (https://kinotron.tv), HTML and inline-player JSON."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from ..models import ContentResponse, Episode, SearchResult, Season, Section, StreamResponse, Translation, TranslationLevel
from .base import BaseProvider, MediaTypeStr, ProviderError

BASE_URL = "https://kinotron.tv"
SECTIONS = (
    Section(id="films", title="Фільми", type="movie"),
    Section(id="serials", title="Серіали", type="series"),
    Section(id="cartoons", title="Мультфільми", type="movie"),
    Section(id="cartoon-series", title="Мультсеріали", type="cartoon"),
    Section(id="anime", title="Аніме", type="anime"),
)

# external_id is a numeric-prefixed slug (e.g. "10496-mesniki-..."). Gate
# the URL interpolation against path-traversal payloads before hitting the
# upstream HTTP client.
_SLUG_RE = re.compile(r"\d+-[a-z0-9][a-z0-9-]*")


def _external_id(href: str) -> str:
    match = re.search(r"/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href, re.I)
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
        results.append(SearchResult(
            id=f"{provider}:{external_id}", provider=provider, type=media_type,
            title=title, year=int(year_match.group()) if year_match else None,
            poster=poster, url=urljoin(BASE_URL, str(link["href"])),
        ))
    return results


class KinoTronProvider(BaseProvider):
    id = "kinotron"
    name = "KinoTron"
    types = ("movie", "series", "cartoon", "anime")
    sections = SECTIONS

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
        iframe = soup.select_one("div.video-box iframe")
        if iframe is None or not iframe.get("data-src"):
            return None
        return urljoin(BASE_URL, str(iframe["data-src"]))

    @staticmethod
    def _files(player_html: str) -> list[dict[str, object]]:
        scripts = BeautifulSoup(player_html, "lxml").select("script")
        text = next((item.get_text() for item in scripts if "file" in item.get_text()), "")
        match = re.search(r"file\s*:\s*'([^']+)'", text)
        if not match:
            return []
        try:
            raw = json.loads(match.group(1))
        except json.JSONDecodeError:
            return [{"dub": "", "season": "", "title": "", "file": match.group(1)}]
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
        response = await self._get(url, http)
        soup = BeautifulSoup(response.text, "lxml")
        section_type = next(item.type for item in self.sections if item.id == section)
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
        player_url = self._player_url(soup)
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
        description_el = soup.select_one(".full-text")
        return ContentResponse(id=f"{self.id}:{external_id}", type=kind, title=title_el.get_text(" ", strip=True),
            description=description_el.get_text(" ", strip=True) if description_el else "",
            poster=poster, translations=translations, seasons=seasons, translations_level=translations_level)

    async def stream(self, content_id: str, translation: str | None, http: httpx.AsyncClient) -> StreamResponse:
        parts = content_id.split(":")
        external_id = parts[-2] if len(parts) >= 3 else parts[-1]
        episode_match = re.fullmatch(r"s(\d+)e(\d+)", parts[-1]) if len(parts) >= 3 else None
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
        return StreamResponse(url=str(selected["file"]), type="m3u8", headers={"Referer": player_url, "User-Agent": "cs-uk-api/0.1"})


__all__ = ["KinoTronProvider"]
