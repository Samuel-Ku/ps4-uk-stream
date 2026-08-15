"""CikavaIdeya provider (https://cikava-ideya.top) — Ukrainian-dubbed
films, serials, cartoons (Мультсеріали) and arthaus. Issue #17, Group 1."""
from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from ..country import extract_country
from ..extractors import RegexExtractor
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
from ..wire_identity import MOVIE_SUFFIX
from .base import BaseProvider, ProviderError, model_b_axes

BASE_URL = "https://cikava-ideya.top"
# Hosts the upstream may legally redirect to: the CMS and the ashdi
# player CDN. A hostile CMS response must not be able to pivot the
# player hop to an attacker-controlled host.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"cikava-ideya.top", "ashdi.vip"})
# ashdi.vip hosts the HLS manifest for each episode. The upstream
# Kotlin source sets the Referer to "https://tortuga.wtf/" so that the
# CDN serves the manifest.
ASHDI_REFERER = "https://tortuga.wtf/"

# Sections exposed by CikavaIdeya's main navigation. Per the upstream
# `mainPage = mainPageOf(...)` in CikavaIdeyaProvider.kt.
CIKAVA_SECTIONS: tuple[Section, ...] = (
    Section(id="filmy", title="Фільми", form="movie"),
    Section(id="serialy", title="Серіали", form="series"),
    Section(id="cartoon", title="Мультсеріали", form="series"),
    Section(id="arthaus", title="Артхаус", form="movie"),
)

# Per-card type classifier mirrors the upstream Kotlin conditional:
#   if (tags.contains("Фільми") or tags.contains("Артхаус")) Movie
#   else TvSeries
# Order matters: longest first so "Мультсеріали" beats "Серіали" and
# "Фільми" beats "Анімаційні" when both appear in the same card.
# Needles are pre-lowered to skip `.lower()` on every call.
_TAG_TYPE: tuple[tuple[str, str], ...] = (
    ("мультсеріали", "series"),
    ("фільми", "movie"),
    ("артхаус", "movie"),
    ("серіали", "series"),
    ("анімаційні", "series"),
)

# The upstream Kotlin feeds `JSONObject(script.substring("Object(", ");"))`
# for player json. The wrapper `Object(...)` is purely cosmetic.
_PLAYER_JSON_RE = re.compile(
    r"switches\s*=\s*Object\((\{.*?\})\)\s*;",
    re.DOTALL,
)

#: Upstream's deliberate-unavailable marker on a removed title: the
#: `.fmessage` box reads «Видалено на прохання правовласника. Шукайте
#: на інших сайтах.» (captured live 2026-08-08, content_fundaciya.html —
#: Player1 then holds only a "Трейлер" youtube URL, no playable
#: episodes). Upstream-removed content is NOT a provider-health signal →
#: `gated` (ADR-0002), mirroring eneyida's «Контент недоступний» (#137).
_REMOVED_MARKER = "Видалено на прохання правовласника"

#: ashdi.vip answers a dead/removed VOD with a 47-byte
#: `<center>Файл не знайдено</center>` page (captured live 2026-08-08,
#: ashdi_vod_127413.html — vsesvit's first episode). Upstream-removed →
#: `gated`, not `parse_failed`, so the health tracker stays green (#139).
_ASHDI_NOT_FOUND = "Файл не знайдено"

# Sentinel episode-id suffix for movies (whose Player1 is a single URL
# rather than a season/episode map; defined once in ``wire_identity``,
# spec #309).


def _page_number(href: str) -> int:
    """Pull the `/page/N/` integer out of a DLE pagination link."""
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


def _numeric_sort_key(label: str) -> int:
    """Sort key for labels like "1 сезон", "2 серія" — leading integer
    wins. Python's `sorted` is stable, so tied labels keep their
    insertion order."""
    m = re.match(r"\s*(\d+)", label)
    return int(m.group(1)) if m else 0


_SLUG_RE = re.compile(r"\d+-[a-z0-9-]+")


def _classify_from_tags(tags_text: str) -> str:
    """Map the .th-subtitle (or content-page Жанр) text to a MediaType."""
    lower = tags_text.lower()
    for needle, t in _TAG_TYPE:
        if needle in lower:
            return t
    return "series"  # safe default, matches the upstream "else TvSeries"


def _section_url(section: str, page: int) -> str:
    paths = {
        "filmy": "/filmy/",
        "serialy": "/serialy/",
        "cartoon": "/cartoon/",
        "arthaus": "/arthaus/",
    }
    if section not in paths:
        raise ProviderError("not_found", f"unknown section: {section}")
    base = f"{BASE_URL}{paths[section]}"
    if page <= 1:
        return base
    return f"{base}page/{page}/"


def _parse_card(card: Tag, provider_id: str) -> SearchResult | None:
    """Parse one `.th-item` listing card."""
    a = card.select_one("a.th-in")
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    title_el = card.select_one(".th-title")
    title = title_el.get_text(strip=True) if title_el else ""
    img = card.select_one(".img-fit img")
    poster_src = str(img["src"]) if img and img.get("src") else None
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    # The .th-subtitle block carries year + one or more category tags
    # (e.g. "2021 • Серіали / Анімаційні").
    sub = card.select_one(".th-subtitle")
    subtitle_text = sub.get_text(" ", strip=True) if sub else ""
    year_m = re.search(r"\b(19|20)\d{2}\b", subtitle_text)
    year = int(year_m.group(0)) if year_m else None
    # CikavaIdeya URLs have no kind prefix — derive external_id directly
    # from the path: "226-jak-vlashtovanij-vsesvit".
    m = re.search(r"/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href)
    if not m:
        return None
    mb_form, mb_styles = model_b_axes(_classify_from_tags(subtitle_text))  # type: ignore[arg-type]
    return SearchResult(
        id=f"{provider_id}:{m.group(1)}",
        provider=provider_id,
        title=title,
        year=year,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form=mb_form,
        styles=mb_styles,
    )


def _parse_player_json(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Pull `switches = Object({...});` out of the inline scripts.

    The upstream Kotlin extracts the substring between `Object(` and
    the final `);`, then feeds it to `JSONObject()`. We do the same
    but accept either a movie (Player1 is a single URL string) or a
    series (Player1 is `{season: {episode: url}}`).
    """
    for script in soup.select("script"):
        m = _PLAYER_JSON_RE.search(script.get_text())
        if m:
            try:
                return cast(dict[str, Any], json.loads(m.group(1)))
            except json.JSONDecodeError:
                return None
    return None


def _is_playable(player1: Any) -> bool:
    """A Player1 value is playable when it is a single URL string
    (movie) or a dict holding at least one real season map
    (dict-of-episodes). A trailer-only map (`{"Трейлер": youtube}`) or an
    empty `Object({})` carries no playable episode → not playable → the
    title is `gated`."""
    if isinstance(player1, str):
        return True
    if not isinstance(player1, dict):
        return False
    return any(isinstance(v, dict) for v in player1.values())


def _removed_marker(soup: BeautifulSoup) -> bool:
    """True iff the content page carries the upstream's removed-title
    notice in its `.fmessage` box («Видалено на прохання
    правовласника», captured live 2026-08-08, content_fundaciya.html).
    Scoped to that box rather than the raw page so a user comment
    quoting the phrase on an otherwise-playable title cannot
    false-positive into `gated`."""
    box = soup.select_one(".fmessage")
    return box is not None and _REMOVED_MARKER in box.get_text()


def _real_season_keys(player1: dict[str, Any]) -> list[str]:
    """Ordered season keys of a series Player1 — dict-valued entries
    only. Trailer-only keys (e.g. "Трейлер" → a youtube URL) are not
    real seasons; skipping them keeps season numbering and episode
    resolution (`_build_seasons` vs `_select_player_url`) in agreement
    for a mixed map."""
    return [
        k
        for k in sorted(player1.keys(), key=_numeric_sort_key)
        if isinstance(player1[k], dict)
    ]


def _load_player1(soup: BeautifulSoup) -> str | dict[str, Any]:
    """Extract Player1 from a content page, gating unplayable titles.

    Raises ``ProviderError("gated", ...)`` (ADR-0002) when the page
    carries the upstream's removed-title marker in its `.fmessage` box,
    or when Player1 is absent / empty (`Object({})`) / trailer-only —
    all deliberate upstream unavailability, not provider-health signals
    (#139). Shared by `content()` and `stream()` so both judge the same
    page the same way."""
    if _removed_marker(soup):
        raise ProviderError("gated", "upstream content removed")
    player_json = _parse_player_json(soup)
    player1: str | dict[str, Any] | None = player_json.get("Player1") if player_json else None
    if player1 is None or not _is_playable(player1):
        raise ProviderError("gated", "no playable player on content page")
    return player1


async def _probe_ashdi_gate(player_url: str, http: httpx.AsyncClient) -> None:
    """Best-effort content()-time dead-VOD probe (#185).

    Fetch the representative ashdi.vip player page and raise
    ``ProviderError("gated", ...)`` when it answers with a 200 body
    carrying the «Файл не знайдено» marker (captured live 2026-08-08,
    ashdi_vod_127413.html) — upstream-removed content, not a
    provider-health signal. Mirrors eneyida's content()-time gating
    check (#139). The URL came from upstream HTML, so the fetch goes
    through the redirect allowlist stream() uses (#126).

    Transient failures are tolerated: a flaky ashdi must not drop a live
    card during the catalog sweep (``filter_gated_items`` only drops
    KNOWN-gated items), so ``stream()`` keeps the marker check as the
    play-time backstop."""
    try:
        ashdi_resp = await safe_get(
            http,
            player_url,
            allowed_hosts=set(_ALLOWED_HOSTS),
            headers={"Referer": ASHDI_REFERER},
        )
    except (httpx.HTTPError, ProviderError):
        return
    if ashdi_resp.status_code == 200 and _ASHDI_NOT_FOUND in ashdi_resp.text:
        raise ProviderError("gated", "upstream content removed")


class CikavaIdeyaProvider(BaseProvider):
    id = "cikavaideya"
    name = "Цікава Ідея"
    types = ("movie", "series")
    sections = CIKAVA_SECTIONS
    #: Gated verdicts (removed / trailer-only / no-player titles, dead
    #: ashdi embeds) must flow through `filter_gated_items` so the
    #: ADR-0002 catalog sweep drops those cards from home during
    #: `load_home`; `resolve_group_content` additionally backstops a            # Cold-cache g2: detail call so a gated verdict never records a
    #: health-down (#139).
    can_gate = True

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # DLE (CikavaIdeya's CMS) accepts a POST with the same fields
        # the upstream Kotlin uses. `quote()` handles non-ASCII Cyrillic
        # and reserved characters; httpx then url-form-encodes the rest.
        try:
            resp = await http.post(
                BASE_URL,
                data={"do": "search", "subaction": "search", "story": quote(query)},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for card in soup.select(".th-item"):
            parsed = _parse_card(card, self.id)
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
        results: list[SearchResult] = []
        for card in soup.select(".th-item"):
            parsed = _parse_card(card, self.id)
            if parsed is not None:
                results.append(parsed)
        # has_next: DLE pagination is `<div class="navigation">` with
        # `<a href="/section/page/N/">` siblings. Any link to a higher
        # page than `page` means there is a next page.
        has_next = any(
            _page_number(str(a.get("href") or "")) > page
            for a in soup.select("div.navigation a[href*='/page/']")
        )
        return results, has_next

    async def content(
        self, external_id: str, http: httpx.AsyncClient
    ) -> ContentResponse:
        if not _SLUG_RE.fullmatch(external_id):
            raise ProviderError("not_found", f"bad external_id: {external_id!r}")
        url = f"{BASE_URL}/{external_id}.html"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        title_el = soup.select_one(".full h1")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        img = soup.select_one(".img-fit img")
        poster_src = str(img["src"]) if img and img.get("src") else None
        poster = urljoin(BASE_URL, poster_src) if poster_src else None
        desc_el = soup.select_one(".fdesc")
        description = desc_el.get_text(strip=True) if desc_el else ""
        # The Жанр row is `fullInfo[2]` per the upstream Kotlin; it
        # contains "Фільми", "Серіали", "Артхаус", or "Мультсеріали"
        # (sometimes more than one tag, separated by " / ").
        flist = soup.select(".flist li")
        tags_text = flist[2].get_text(" ", strip=True) if len(flist) >= 3 else ""
        country: str | None = extract_country(soup)
        # Removed / trailer-only / no-player titles raise `gated` here
        # (ADR-0002), before any season is built (#139).
        player1 = _load_player1(soup)
        # Issue #185: a title whose Player1 is playable can still be a
        # dead VOD — ashdi.vip answers its player page with «Файл не
        # знайдено» (captured live 2026-08-08, ashdi_vod_127413.html).
        # Probe the representative player URL (a movie's single URL, or
        # a series' first real season's first episode — the same
        # resolution `_select_player_url` uses) and gate the dead VOD at
        # content() time, mirroring eneyida (#139), so the ADR-0002
        # catalog sweep drops the card instead of surfacing a title that
        # only 404s at play time. stream() keeps the marker check as a
        # backstop.
        player_url = (
            player1
            if isinstance(player1, str)
            else self._select_player_url(player1, "s1e1")
        )
        if player_url is not None:
            await _probe_ashdi_gate(player_url, http)
        seasons = self._build_seasons(player1, external_id, self.id)
        mb_form, mb_styles = model_b_axes(_classify_from_tags(tags_text))  # type: ignore[arg-type]
        return ContentResponse(
            id=f"cikavaideya:{external_id}",
            title=title_el.get_text(strip=True),
            description=description,
            poster=poster,
            translations=[Translation(id="uk", label="Українська")],
            seasons=seasons,
            country=country,
            form=mb_form,
            styles=mb_styles,
        )

    @staticmethod
    def _build_seasons(player1: str | dict[str, Any], external_id: str, provider_id: str) -> list[Season]:
        """Convert the upstream `Player1` value into our `Season[]`.

        Movies surface as season 1, episode 1 with id suffix `__movie__`
        so `stream()` can resolve the single URL. Series surface in
        order; each episode's id encodes its (season, episode) position
        so the same resolver can pick the right ashdi URL.
        """
        if isinstance(player1, str):
            return [Season(number=1, episodes=[Episode(
                number=1, id=f"{provider_id}:{external_id}{MOVIE_SUFFIX}", title="Фільм",
            )])]
        seasons: list[Season] = []
        for s_idx, season_key in enumerate(_real_season_keys(player1), start=1):
            episodes_raw = player1[season_key]
            ep_keys = sorted(episodes_raw.keys(), key=_numeric_sort_key)
            episodes = [
                Episode(
                    number=e_idx,
                    id=f"{provider_id}:{external_id}:s{s_idx}e{e_idx}",
                    title=k.strip(),
                )
                for e_idx, k in enumerate(ep_keys, start=1)
            ]
            seasons.append(Season(number=s_idx, episodes=episodes))
        return seasons

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # `content_id` arrives as either "<external_id>" (movie, bare
        # id straight from a search result), "<external_id>:__movie__"
        # (movie, explicit suffix from the content listing), or
        # "<external_id>:s<N>e<M>" (series episode). `/api/stream`
        # strips the `<provider>:` prefix before calling us.
        if MOVIE_SUFFIX in content_id:
            ext_id = content_id.split(MOVIE_SUFFIX, 1)[0]
            ep_suffix = ""
        elif ":" in content_id:
            ext_id, _, ep_suffix = content_id.rpartition(":")
        else:
            ext_id, ep_suffix = content_id, ""
        if not _SLUG_RE.fullmatch(ext_id):
            raise ProviderError("not_found", f"bad external_id: {ext_id!r}")
        content_url = f"{BASE_URL}/{ext_id}.html"
        try:
            resp = await http.get(content_url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        player1 = _load_player1(BeautifulSoup(resp.text, "lxml"))
        player_url = self._select_player_url(player1, ep_suffix)
        if player_url is None:
            raise ProviderError("parse_failed", f"no player url for {ep_suffix!r}")
        # The player URL lives on ashdi.vip; the upstream Kotlin calls
        # M3u8Helper.generateM3u8 which hits the page and pulls the
        # `file: "https://.../index.m3u8"` URL out of an inline script.
        # The URL came from upstream HTML, so it goes through the
        # redirect allowlist (#126).
        try:
            ashdi_resp = await safe_get(
                http,
                player_url,
                allowed_hosts=set(_ALLOWED_HOSTS),
                headers={"Referer": ASHDI_REFERER},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if ashdi_resp.status_code != 200:
            raise ProviderError("not_found", f"status {ashdi_resp.status_code}")
        if _ASHDI_NOT_FOUND in ashdi_resp.text:
            # ashdi.vip's dead-VOD page (captured live 2026-08-08,
            # ashdi_vod_127413.html) — upstream-removed content, not a
            # provider-health signal → `gated` (ADR-0002), mirroring
            # eneyida's «Контент недоступний» (#139).
            raise ProviderError("gated", "upstream content removed")
        extracted = RegexExtractor().extract(ashdi_resp.text)
        if extracted is None or not extracted.url:
            raise ProviderError("parse_failed", "no m3u8 in ashdi page")
        return StreamResponse(
            url=extracted.url,
            type=extracted.type,
            headers={"Referer": ASHDI_REFERER, "User-Agent": "cs-uk-api/1.0"},
        )

    @staticmethod
    def _select_player_url(player1: str | dict[str, Any], ep_suffix: str) -> str | None:
        """Resolve the ashdi URL for either a movie or a series episode.

        Returns None when the suffix is malformed or out of range so the
        caller can surface an explicit `parse_failed`. There is no
        silent "first available episode" fallback — that would mask a
        missing suffix in the caller.
        """
        if isinstance(player1, str):
            return player1 if not ep_suffix else None
        if not ep_suffix:
            return None
        m = re.fullmatch(r"s(\d+)e(\d+)", ep_suffix)
        if not m:
            return None
        s_idx, e_idx = int(m.group(1)), int(m.group(2))
        # Same real-season ordering `_build_seasons` numbers, so a
        # `:s<N>e<M>` id produced by content() resolves here to the same
        # episode a trailer-only key can never shadow.
        seasons = _real_season_keys(player1)
        if not (1 <= s_idx <= len(seasons)):
            return None
        episodes_raw = player1[seasons[s_idx - 1]]
        ep_keys = sorted(episodes_raw.keys(), key=_numeric_sort_key)
        if not (1 <= e_idx <= len(ep_keys)):
            return None
        return str(episodes_raw[ep_keys[e_idx - 1]])


__all__ = ["CikavaIdeyaProvider"]
