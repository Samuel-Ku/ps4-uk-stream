"""UASerialsPro provider (https://uaserials.com) — Ukrainian-dubbed
films, serials, cartoons, anime and exclusives. Issue #17, Group 3.

The site is a DLE-style CMS. The player config is stored inside
``<player-control data-tag1='{"ciphertext":...,"salt":...,"iv":...}'>``
on every content page. The blob is ``crypto-js``-style AES-256-CBC with
PBKDF2-HMAC-SHA512 key derivation (999 iterations). The decryption
key is derived from a hard-coded upstream password and the hex-decoded
``salt``.

The decrypted JSON is a list of tabs::

    [{"tabName":"Плеєр","url":"https://tortuga.tw/vod/..."},
     {"tabName":"Трейлер","url":"https://tortuga.tw/vod/..."}]

We pick the "Плеєр" tab URL (or the first tab if missing) and follow it
to a Tortuga-hosted player page. That page embeds a ``file:`` field
that is one of:

  * a plain https m3u8 URL (movies — direct),
  * a Tortuga XOR-encoded string (movies — encoded), or
  * a JSON array of TortugaSeason / TortugaEpisode records (series).

Tortuga decoding is the same algorithm the upstream Kotlin uses: decode
base64 (after stripping trailing ``=`` and re-padding), take the first
byte as a salt, XOR each subsequent byte with
``(salt + 7 * i + 13) % 256`` for ``i = 0, 1, ...``.

External-id shape: ``<numeric>-<slug>`` — no section prefix.
URL form: ``https://uaserials.com/<external_id>.html``.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from ..country import extract_country
from ..http_client import provider_safe_get
from ..models import (
    ContentResponse,
    Episode,
    MediaForm,
    MediaStyle,
    SearchResult,
    Season,
    Section,
    StreamResponse,
    Translation,
)
from ..wire_identity import (
    MOVIE_SUFFIX,
    episode_wire_id,
    is_movie_wire_id,
    parse_episode_tail,
    strip_movie_suffix,
)
from ._crypto_uaserialspro import decrypt_player_data
from ._tortuga import decode as _tortuga_decode
from .base import BaseProvider, MediaTypeStr, ProviderError, parse_actor_list

BASE_URL = "https://uaserials.com"
# Hosts the upstream may legally redirect to: the DLE CMS and the
# tortuga player. A hostile CMS response must not be able to pivot
# either hop to an attacker-controlled host.
# tortuga.tw serves the HLS manifest with this Referer (mirrors the
# upstream Kotlin source).
TORTUGA_REFERER = "https://tortuga.tw/"
# User-Agent for the content-page fetch (matches the upstream).
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0"

# Sections exposed by UASerialsPro's main navigation. Per the upstream
# `mainPage = mainPageOf(...)` in UASerialsProProvider.kt.
UASERIALSPRO_SECTIONS: tuple[Section, ...] = (
    Section(id="films", title="Фільми", form="movie"),
    Section(id="series", title="Серіали", form="series"),
    Section(id="fcartoon", title="Мультфільми", form="movie"),
    Section(id="cartoons", title="Мультсеріали", form="series"),
    Section(id="anime", title="Аніме", styles=frozenset({"anime"})),
    Section(id="exclusive", title="Ексклюзив", form="movie"),
)

# Whitelisted slug shape: `<numeric>-<kebab>`. Used at the provider
# boundary so callers cannot smuggle path traversal through the API.
_EXTERNAL_ID_RE = re.compile(r"\d+-[a-z0-9-]+")

# Upstream Kotlin regex for the `file: '...'` value on the Tortuga
# player page. Matches single- or double-quoted strings.
_FILE_RE = re.compile(r"""file\s*:\s*["']([^"']+?)["']""")


#: The dubbing-studio label inside the ``{...}`` prefix of a series
#: episode file value (ticket #332): ``{HDrezka Studio}url(subtitle:...)``.
_FILE_PREFIX_RE = re.compile(r"^\{(.*?)\}")


def _file_prefix_label(file_value: str) -> str | None:
    m = _FILE_PREFIX_RE.match(file_value)
    return m.group(1).strip() if m else None


def _payload_dub_labels(player_html: str) -> list[str]:
    """Every dubbing label found in a decoded player payload (ticket #332)."""
    m = _FILE_RE.search(player_html)
    if not m:
        return []
    encoded = m.group(1)
    if encoded.startswith("http"):
        decoded = encoded
    else:
        decoded = _tortuga_decode(encoded)
    if not decoded or not decoded.startswith("["):
        return []
    try:
        seasons_raw = cast(list[dict[str, Any]], json.loads(decoded))
    except json.JSONDecodeError:
        return []
    labels: list[str] = []
    for season in seasons_raw:
        for ep in season.get("folder") or []:
            label = _file_prefix_label(str(ep.get("file", "")))
            if label and label not in labels:
                labels.append(label)
    return labels


def _page_number(href: str) -> int:
    """Pull the `/page/N/` integer out of a DLE pagination link."""
    m = re.search(r"/page/(\d+)/?", href)
    return int(m.group(1)) if m else 0


def _classify_from_genres(genre_text: str) -> MediaTypeStr:
    """Map the Жанр row text to a MediaType.

    Mirrors the upstream Kotlin `when` block:
        Серіал → series, Мультсеріал → series,
        Фільм → movie, Мультфільм → movie,
        Аніме → anime, else → series.
    Order matters: longest-prefix first so "Мультсеріал" wins over
    "Серіал" and "Мультфільм" wins over "Фільм".
    """
    lowered = genre_text.lower()
    for needle, mapped in (
        ("мультсеріал", "series"),
        ("мультфільм", "movie"),
        ("серіал", "series"),
        ("аніме", "anime"),
        ("фільм", "movie"),
    ):
        if needle in lowered:
            return cast(MediaTypeStr, mapped)
    return "series"


def _type_for_section(section_id: str) -> MediaTypeStr:
    """Default MediaType per section. Used by `browse` since the
    listings carry no per-card genre tag."""
    table: dict[str, MediaTypeStr] = {
        "films": "movie",
        "series": "series",
        "fcartoon": "movie",
        "cartoons": "series",
        "anime": "anime",
        "exclusive": "movie",
    }
    return table.get(section_id, "series")


def _section_url(section: str, page: int) -> str:
    if section not in {s.id for s in UASERIALSPRO_SECTIONS}:
        raise ProviderError("not_found", f"unknown section: {section}")
    base = f"{BASE_URL}/{section}/"
    # Upstream Kotlin uses the section root for page 1 and `/page/N/`
    # otherwise — same DLE convention as CikavaIdeya / KlonTV.
    if page <= 1:
        return base
    return f"{base}page/{page}/"


def _select_player_url(tabs: list[dict[str, Any]]) -> str | None:
    """Pick the "Плеєр" tab URL from the decrypted tabs list. Falls
    back to the first tab when "Плеєр" is missing (matches upstream)."""
    if not tabs:
        return None
    for tab in tabs:
        if tab.get("tabName") == "Плеєр" and tab.get("url"):
            return str(tab["url"])
    first_url = tabs[0].get("url")
    return str(first_url) if first_url else None


def _title_text(el: Tag | None) -> str:
    """Faithful element text with collapsed whitespace.

    ``get_text(strip=True)`` concatenates the text nodes WITHOUT
    separators, so a title whose word is split by an inline element
    loses the inter-word space — the search page wraps the queried
    word in ``<mark>`` inside the card title (e.g.
    «Таємниця <mark>бункер</mark>а») and the words come out glued
    («Таємницябункера»), splitting one show into two merge groups
    (issue #246). ``get_text()`` keeps the source's own whitespace;
    collapse runs to a single space.
    """
    if el is None:
        return ""
    return " ".join(el.get_text().split())


def _parse_card(card: Tag, provider_id: str, media_type: MediaTypeStr) -> SearchResult | None:
    """Parse one `.short-item` listing card.

    The card exposes an anchor (`.short-item.width-16 .short-img`), a
    title (`.th-title.truncate`), an English title
    (`.th-title-oname.truncate`), and a lazy-loaded poster
    (`.img-fit img[data-src]`).
    """
    a = card.select_one("a.short-img")
    if a is None or not a.get("href"):
        return None
    href = str(a["href"])
    # Derive external_id from the URL — `<numeric>-<slug>` (no kind).
    m = re.search(r"/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href)
    if not m:
        return None
    title_el = card.select_one("div.th-title.truncate")
    title = _title_text(title_el)
    img = card.select_one("img")
    poster_src: str | None = None
    if img is not None:
        ds = img.get("data-src")
        if isinstance(ds, str) and ds:
            poster_src = ds
        else:
            src = img.get("src")
            if isinstance(src, str) and src:
                poster_src = src
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    mb_form: MediaForm = media_type if media_type == "movie" or media_type == "series" else "series"
    mb_styles: frozenset[MediaStyle] = (
        frozenset() if media_type == "movie" or media_type == "series" else frozenset({media_type})
    )
    return SearchResult(
        id=f"{provider_id}:{m.group(1)}",
        provider=provider_id,
        title=title,
        poster=poster,
        form=mb_form,
        styles=mb_styles,
        url=urljoin(BASE_URL, href),
    )


def _parse_search_card(a: Tag, provider_id: str) -> SearchResult | None:
    """Parse one `<a class="uas-card">` search result.

    The whole card is one anchor carrying the href, so we pull title
    and original title from inner spans.
    """
    href = a.get("href")
    if not isinstance(href, str) or not href:
        return None
    m = re.search(r"/(\d+-[a-z0-9-]+?)(?:\.html)?/?$", href)
    if not m:
        return None
    title_el = a.select_one(".uas-card__title")
    title = _title_text(title_el)
    img_el = a.select_one(".uas-card__img")
    poster_src: str | None = None
    if img_el is not None:
        ds = img_el.get("data-src")
        if isinstance(ds, str) and ds:
            poster_src = ds
        else:
            src = img_el.get("src")
            if isinstance(src, str) and src:
                poster_src = src
    poster = urljoin(BASE_URL, poster_src) if poster_src else None
    return SearchResult(
        id=f"{provider_id}:{m.group(1)}",
        provider=provider_id,
        title=title,
        year=None,
        poster=poster,
        url=urljoin(BASE_URL, href),
        form="series",
        styles=frozenset(),
    )


class UASerialsProProvider(BaseProvider):
    id = "uaserialspro"
    name = "UASerialsPro"
    types = ("movie", "series", "anime")
    sections = UASERIALSPRO_SECTIONS
    #: Site + the Tortuga player gateway it embeds (ADR-0005).
    allowed_hosts = frozenset({"uaserials.com", "tortuga.tw"})

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        # The site uses `/search/<query>/` for search. We quote the
        # query so non-ASCII Cyrillic and reserved characters survive.
        url = f"{BASE_URL}/search/{quote(query)}/"
        try:
            resp = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("upstream_unreachable", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        results: list[SearchResult] = []
        for a in soup.select("a.uas-card"):
            parsed = _parse_search_card(a, self.id)
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
        media_type = _type_for_section(section)
        results: list[SearchResult] = []
        for card in soup.select(".short-item"):
            parsed = _parse_card(card, self.id, media_type)
            if parsed is not None:
                results.append(parsed)
        # Pagination: `<div class="navigation">` with
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
        if not _EXTERNAL_ID_RE.fullmatch(external_id):
            raise ProviderError("not_found", f"bad external_id: {external_id!r}")
        url = f"{BASE_URL}/{external_id}.html"
        try:
            resp = await http.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        title_el = soup.select_one(".short-title")
        if title_el is None:
            raise ProviderError("parse_failed", "title missing")
        # Drop the inner `<span class="oname_ua">` so the title is
        # just the visible primary Ukrainian name.
        title = title_el.get_text(" ", strip=True)
        # Some titles use "/" to separate alt names ("A / B"); keep the
        # first segment so the API surfaces a single canonical name.
        title = title.split("/")[0].strip()
        # Year from `<a href="/year/YYYY/">YYYY</a>`.
        year: int | None = None
        for a in soup.select(".short-list a[href*='/year/']"):
            text = a.get_text(strip=True)
            if text.isdigit() and len(text) == 4:
                year = int(text)
                break
        # Poster from `div.fimg img-wide img`.
        poster_el = soup.select_one("div.fimg img")
        poster = (
            urljoin(BASE_URL, str(poster_el.get("src") or ""))
            if poster_el is not None and poster_el.get("src")
            else None
        )
        # Description from `.full-text`.
        desc_el = soup.select_one(".full-text")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""
        # Genre text — used for content-page type classification.
        genre_text = ""
        for li in soup.select(".short-list li"):
            span = li.find("span")
            if span is not None and "Жанр" in span.get_text():
                genre_text = li.get_text(" ", strip=True)
                break
        media_type = _classify_from_genres(genre_text)
        # Translation label (visible in `.short-list li:contains(Переклад)`).
        translation = ""
        for li in soup.select(".short-list li"):
            span = li.find("span")
            if span is not None and "Переклад" in span.get_text():
                inner = li.select_one("span[data-popup]")
                if inner is not None:
                    translation = inner.get_text(strip=True).replace("|", "/")
                break
        translations = [Translation(id="uk", label=translation or "Українська")]
        country: str | None = extract_country(soup)
        # AES-decrypt the player data-tag1 to get the player URL.
        data_tag1_el = soup.select_one("div.fplayer player-control")
        if data_tag1_el is None or not data_tag1_el.get("data-tag1"):
            raise ProviderError("parse_failed", "no data-tag1 on content page")
        tabs = decrypt_player_data(str(data_tag1_el["data-tag1"]))
        player_url = _select_player_url(tabs)
        if player_url is None:
            raise ProviderError("parse_failed", "no player url in data-tag1")
        # The player URL came from decrypted upstream HTML, so it goes
        # through the redirect allowlist (#126).
        try:
            player_resp = await provider_safe_get(
                http,
                self,
                player_url,
                headers={"User-Agent": USER_AGENT, "Referer": BASE_URL + "/"},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if player_resp.status_code != 200:
            raise ProviderError("not_found", f"status {player_resp.status_code}")
        # Decode the `file:` field — Tortuga-encoded for movies, or a
        # JSON playlist for series.
        seasons = self._build_seasons_from_player(player_resp.text, external_id, self.id)
        # Ticket #332: the player payload's episode files carry the
        # dubbing studio in a ``{label}`` prefix — the page's single
        # «Переклад» field is only the primary dub. Surface every
        # prefix label as a translation for the dub picker.
        payload_labels = _payload_dub_labels(player_resp.text)
        if payload_labels:
            translations = [Translation(id=t, label=t) for t in payload_labels]
        # Ticket #221: the page's ``Актори:`` li lists the cast with
        # one ``/person/<id>-<slug>/`` anchor per person.
        cast = parse_actor_list(
            soup, "Актори", self.id, re.compile(r"/person/([^/]+)/?$")
        )
        mb_form: MediaForm = media_type if media_type == "movie" or media_type == "series" else "series"
        mb_styles: frozenset[MediaStyle] = (
            frozenset() if media_type == "movie" or media_type == "series" else frozenset({media_type})
        )
        return ContentResponse(
            id=f"uaserialspro:{external_id}",
            title=title,
            year=year,
            description=description,
            poster=poster,
            translations=translations,
            seasons=seasons,
            country=country,
            form=mb_form,
            styles=mb_styles,
            people=cast,
        )

    @staticmethod
    def _build_seasons_from_player(
        player_html: str, external_id: str, provider_id: str
    ) -> list[Season] | None:
        """Decode the Tortuga player `file:` field into our `Season[]`.

        For movies, the field is a Tortuga-encoded m3u8 URL — we surface
        a single season/episode pair so the API client can stream it.
        For series, the field decodes to a JSON list of TortugaSeason
        records, each carrying a `folder` of TortugaEpisode records.
        Each episode's `file` is `{label}url(subtitle:...)`; the url
        portion is the m3u8 manifest.
        """
        m = _FILE_RE.search(player_html)
        if not m:
            return None
        encoded = m.group(1)
        if encoded.startswith("http"):
            decoded = encoded
        else:
            decoded = _tortuga_decode(encoded)
        if not decoded:
            return None
        if decoded.startswith("["):
            try:
                seasons_raw = cast(list[dict[str, Any]], json.loads(decoded))
            except json.JSONDecodeError:
                return None
            seasons: list[Season] = []
            for s_idx, season in enumerate(seasons_raw, start=1):
                episodes_raw = season.get("folder") or []
                episodes: list[Episode] = []
                for e_idx, ep in enumerate(episodes_raw, start=1):
                    label = _file_prefix_label(str(ep.get("file", "")))
                    episodes.append(
                        Episode(
                            number=e_idx,
                            id=episode_wire_id(provider_id, external_id, s_idx, e_idx),
                            title=str(ep.get("title", "")).strip(),
                            translations=[Translation(id=label, label=label)] if label else None,
                        )
                    )
                if episodes:
                    seasons.append(Season(number=s_idx, episodes=episodes))
            return seasons or None
        # Movie: single m3u8 URL — surface as one episode so the client
        # can hand it to /api/stream.
        return [
            Season(
                number=1,
                episodes=[
                    Episode(
                        number=1,
                        id=f"{provider_id}:{external_id}{MOVIE_SUFFIX}",
                        title="Фільм",
                    )
                ],
            )
        ]

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        # `content_id` arrives as either "<external_id>:__movie__"
        # (movie — explicit suffix from the content listing), or
        # "<external_id>:s<N>e<M>" (series episode), or just the bare
        # "<external_id>" (movie — straight from a search result).
        if is_movie_wire_id(content_id):
            ext_id = strip_movie_suffix(content_id)
            ep_suffix = ""
        elif ":" in content_id:
            ext_id, _, ep_suffix = content_id.rpartition(":")
        else:
            ext_id, ep_suffix = content_id, ""
        if not _EXTERNAL_ID_RE.fullmatch(ext_id):
            raise ProviderError("not_found", f"bad external_id: {ext_id!r}")
        url = f"{BASE_URL}/{ext_id}.html"
        try:
            resp = await http.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if resp.status_code != 200:
            raise ProviderError("not_found", f"status {resp.status_code}")
        soup = BeautifulSoup(resp.text, "lxml")
        data_tag1_el = soup.select_one("div.fplayer player-control")
        if data_tag1_el is None or not data_tag1_el.get("data-tag1"):
            raise ProviderError("parse_failed", "no data-tag1 on content page")
        tabs = decrypt_player_data(str(data_tag1_el["data-tag1"]))
        player_url = _select_player_url(tabs)
        if player_url is None:
            raise ProviderError("parse_failed", "no player url in data-tag1")
        # The player URL came from decrypted upstream HTML, so it goes
        # through the redirect allowlist (#126).
        try:
            player_resp = await provider_safe_get(
                http,
                self,
                player_url,
                headers={"User-Agent": USER_AGENT, "Referer": BASE_URL + "/"},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if player_resp.status_code != 200:
            raise ProviderError("not_found", f"status {player_resp.status_code}")
        m = _FILE_RE.search(player_resp.text)
        if not m:
            raise ProviderError("parse_failed", "no file: in player page")
        encoded = m.group(1)
        if encoded.startswith("http"):
            decoded = encoded
        else:
            decoded = _tortuga_decode(encoded)
        if not decoded:
            raise ProviderError("parse_failed", "tortuga decode empty")
        # Movies: the decoded value is the m3u8 URL. Series: the decoded
        # value is a JSON playlist — pick the right episode by suffix.
        if ep_suffix:
            media_url = self._select_episode_url(decoded, ep_suffix, translation)
            if media_url is None:
                raise ProviderError("parse_failed", f"no media url for {ep_suffix!r}")
        else:
            media_url = decoded
        return StreamResponse(
            url=media_url,
            type="m3u8",
            headers={"Referer": TORTUGA_REFERER, "User-Agent": USER_AGENT},
        )

    @staticmethod
    def _select_episode_url(
        decoded: str, ep_suffix: str, translation: str | None = None
    ) -> str | None:
        """Resolve a series episode m3u8 URL from the decoded playlist.

        The dub picker's translation id (spec #276, ticket #332) selects
        the episode entry whose ``{label}`` prefix matches; an unmatched
        translation falls back to the default track."""
        parsed = parse_episode_tail(ep_suffix)
        if parsed is None:
            return None
        s_idx, e_idx = parsed
        try:
            seasons_raw = cast(list[dict[str, Any]], json.loads(decoded))
        except json.JSONDecodeError:
            return None
        if not (1 <= s_idx <= len(seasons_raw)):
            return None
        episodes_raw = seasons_raw[s_idx - 1].get("folder") or []
        if not (1 <= e_idx <= len(episodes_raw)):
            return None
        if translation:
            candidates = [
                ep for ep in episodes_raw
                if _file_prefix_label(str(ep.get("file", ""))) == translation
            ]
            if candidates:
                ep = candidates[e_idx - 1] if e_idx <= len(candidates) else candidates[0]
            else:
                ep = episodes_raw[e_idx - 1]
        else:
            ep = episodes_raw[e_idx - 1]
        file_str = str(ep.get("file", ""))
        if not file_str:
            return None
        # The series `file:` shape is `{label}<url>(subtitle:...)`.
        # Strip the `{label}` prefix and any trailing `(subtitle:...)`.
        url = file_str
        if "}" in url:
            url = url.split("}", 1)[1]
        sub_match = re.search(r"\(subtitle:.*?\)\s*$", url)
        if sub_match:
            url = url[: sub_match.start()]
        return url.strip() or None


__all__ = ["UASerialsProProvider"]
