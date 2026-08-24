"""Eneyida Ukrainian-dubbed film and series provider."""

from __future__ import annotations

import json
import re
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

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
    parse_episode_tail,
    split_wire_id,
)
from .base import dle_has_next, BaseProvider, MediaTypeStr, ProviderError

BASE_URL = "https://eneyida.tv"
ENEYIDA_SECTIONS = (
    Section(id="films", title="Фільми", form="movie"),
    Section(id="series", title="Серіали", form="series"),
)
_PATH_TYPE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("serials", "series"), "series"),
    (("films",), "movie"),
)
_SLUG_RE = re.compile(r"\d+-[a-z0-9-]+")
# Upstream's deliberate-unavailable embed page: «Контент недоступний»
# (captured live 2026-08-08 — 1441 bytes, the phrase in <title> and <h1>,
# no `file:` payload). This is upstream-removed content, NOT a provider
# bug, so stream() must surface the `gated` verdict (ADR-0002: a
# deliberate-unavailable is client-side semantics, not an upstream
# failure) instead of `parse_failed`.
_CONTENT_UNAVAILABLE = "Контент недоступний"


def _content_unavailable(html: str) -> bool:
    return _CONTENT_UNAVAILABLE in html




def _external_id_from_url(href: str) -> str | None:
    m = re.search(r"/(?:films|serials)/?(\d+-[a-z0-9-]+)\.html", href)
    if not m:
        m = re.search(r"/(\d+-[a-z0-9-]+)\.html", href)
    if not m:
        return None
    section = "series" if "/serials/" in href else "films"
    return f"{section}/{m.group(1)}"


def _type_from_url(href: str) -> MediaTypeStr:
    return "series" if "/serials/" in href or "/series/" in href else "movie"


def _card_kind(card: Tag, href: str) -> str | None:
    """Film/series kind from the card itself (2026-08-14 upstream drift:
    the site serves BARE urls everywhere — ``/8550-....html`` — so the
    URL no longer carries the kind and every card was classified as a
    film). Series cards carry a season/episode label
    (``<div class="metaBottom label_quel-camrip">3 сезон 6 серія</div>``);
    films don't. Returns ``"movie"`` or ``"series"`` or ``None`` when the
    card carries no signal. ALL labels are scanned — the quality label
    (``label_quel-hd`` «FHD 1080p») comes before the season label
    (``label_quel-camrip`` «3 сезон 6 серія») in document order."""
    for el in card.select(".metaBottom, [class*=label_quel]"):
        label = el.get_text(" ", strip=True)
        if re.search(r"\bсезон\b|\bсерія\b", label, re.IGNORECASE):
            return "series"
    if "/films/" in href:
        return "movie"
    if "/series/" in href or "/serials/" in href:
        return "series"
    return None


def _section_url(section: str, page: int) -> str:
    if section not in {"films", "series"}:
        raise ProviderError("not_found", f"unknown section: {section}")
    root = f"{BASE_URL}/{section}/"
    return root if page <= 1 else f"{root}page/{page}/"


#: Section id -> MediaForm kind (the browse override).
_SECTION_KIND: dict[str, str] = {"films": "movie", "series": "series"}


def _parse_card(
    card: Tag, provider_id: str, kind: str | None = None
) -> SearchResult | None:
    a = card.select_one("a.short_title") or card.select_one("a.short_img")
    if not a or not a.get("href"):
        return None
    ext = _external_id_from_url(str(a["href"]))
    if not ext:
        return None
    title = a.get_text(" ", strip=True)
    img = card.select_one("img")
    poster_src = (img.get("data-src") or img.get("src")) if img else None
    resolved_kind = kind or _card_kind(card, str(a["href"])) or "movie"
    _kind = cast(MediaTypeStr, resolved_kind)
    mb_form: MediaForm = _kind if _kind == "movie" or _kind == "series" else "series"
    mb_styles: frozenset[MediaStyle] = (
        frozenset() if _kind == "movie" or _kind == "series" else frozenset({_kind})
    )
    _, _, slug = ext.partition("/")
    id_kind = "series" if resolved_kind == "series" else "films"
    return SearchResult(
        id=f"{provider_id}:{id_kind}/{slug}",
        provider=provider_id,
        title=title,
        poster=urljoin(BASE_URL, str(poster_src)) if poster_src else None,
        url=urljoin(BASE_URL, str(a["href"])),
        form=mb_form,
        styles=mb_styles,
    )


def _file_url(html: str) -> str | None:
    m = re.search(r"file\s*:\s*(?:\"([^\"]+)\"|'([^']+)')", html)
    url = m.group(1) or m.group(2) if m else None
    return url


#: A top folder title like «1 сезон» marks a REAL season (ticket #331);
#: anything else (dubbing-studio names) marks a translation track.
_SEASON_TITLE_RE = re.compile(r"сезон|season", re.IGNORECASE)


def _is_season_titled(entry: dict[str, Any] | None) -> bool:
    return bool(entry and _SEASON_TITLE_RE.search(str(entry.get("title", ""))))


def _dub_index(folders: list[dict[str, Any]], translation: str | None) -> int | None:
    """Index of the dub folder titled ``translation``, or None.

    The dub picker (spec #276) passes the translation id — for eneyida
    that is the studio name from the payload folder title (ticket #331).
    """
    if not translation:
        return None
    for idx, folder in enumerate(folders):
        if str(folder.get("title", "")).strip() == translation:
            return idx
    return None


#: hdvbua player URLs inside an iframe tag — recovery path for the
#: upstream's doubled-quote template bug (the regex runs against the
#: RAW tag HTML, where the URL is intact).
_PLAYER_RE = re.compile(r"https://hdvbua\.pro/(?:embed|vid)/[^\"'\s]+")
_IFRAME_TAG_RE = re.compile(r"<iframe[^>]*>", re.IGNORECASE)


def _ensure_md_token(url: str) -> str:
    """hdvbua embed endpoints now REQUIRE the ``md`` marker token
    (live 2026-08-09: ``embed/<id>/<hash>`` without it answers the
    «Контент недоступний» page, with ``?md`` it serves the real
    player). The upstream Kotlin appends the token itself; the raw
    iframe ``src`` on eneyida content pages omits it, so every embed
    fetch must carry it. ``vid/`` endpoints work either way, and URLs
    that already carry a query keep it."""
    if not url.startswith("https://hdvbua.pro/embed/"):
        return url
    if "?" in url:
        return url
    return f"{url}?md"


def _player_url(html: str, allowed_hosts: frozenset[str]) -> str | None:
    """First player URL from the content page's iframe block.

    The upstream template occasionally emits a doubled quote on the
    first iframe — ``src="data-src="https://hdvbua.pro/embed/..."``
    (live 2026-08-09, «Шуґар»). BeautifulSoup then parses ``src`` as
    the garbage value ``data-src=``, so the PARSED attribute is
    unusable and a regex over the RAW tag HTML must recover the real
    URL. For a well-formed iframe the parsed ``src`` wins (it
    HTML-decodes ``&amp;`` in query tokens — the embed token is
    REQUIRED, e.g. ``?md&akpdef141``; a regex that stops at ``?``
    would fetch the embed without the token and get a dead page).
    """
    tag_match = _IFRAME_TAG_RE.search(html)
    if tag_match is None:
        return None
    tag = tag_match.group(0)
    iframe = BeautifulSoup(tag, "lxml").select_one("iframe")
    if iframe is not None:
        src = str(iframe.get("src") or "")
        if urlsplit(src).hostname in allowed_hosts:
            return _ensure_md_token(src)
    m = _PLAYER_RE.search(tag)
    if m:
        url = m.group(0)
        if urlsplit(url).hostname in allowed_hosts and "?tr" not in url:
            return _ensure_md_token(url)
    return None


class EneyidaProvider(BaseProvider):
    id = "eneyida"
    name = "Eneyida"
    types = ("movie", "series")
    sections = ENEYIDA_SECTIONS
    #: Site + the hdvbua.pro player embed whose URL comes from CMS
    #: HTML (ADR-0005).
    allowed_hosts = frozenset({"eneyida.tv", "hdvbua.pro"})
    #: ``content()`` gates upstream-removed titles (hdvbua embed =
    #: «Контент недоступний», issue #137) so the ADR-0002 catalog sweep
    #: (``filter_gated_items``) drops dead cards from home/search instead
    #: of surfacing titles that fail only at play time (#158).
    can_gate = True

    async def search(self, query: str, http: httpx.AsyncClient) -> list[SearchResult]:
        try:
            r = await http.post(
                f"{BASE_URL}/index.php?do=search",
                data={"do": "search", "subaction": "search", "story": query},
            )
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if r.status_code != 200:
            raise ProviderError("upstream_unreachable", f"status {r.status_code}")
        return [
            x
            for c in BeautifulSoup(r.text, "lxml").select("article.short")
            if (x := _parse_card(c, self.id))
        ]

    async def browse(
        self, section: str, page: int, http: httpx.AsyncClient
    ) -> tuple[list[SearchResult], bool]:
        url = _section_url(section, page)
        try:
            r = await http.get(url)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if r.status_code != 200:
            raise ProviderError("not_found", f"status {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")
        # Section-kind override: the listing URL carries the section, but
        # the CARDS use bare URLs (upstream drift) — the section is the
        # authoritative kind here, same as kinovezha's browse.
        kind = _SECTION_KIND.get(section)
        results = [
            x
            for c in soup.select("article.short")
            if (x := _parse_card(c, self.id, kind=kind))
        ]
        has_next = dle_has_next(r.text, page)
        return results, has_next

    async def content(self, external_id: str, http: httpx.AsyncClient) -> ContentResponse:
        kind, _, slug = external_id.partition("/")
        if not kind or not slug:
            raise ProviderError("parse_failed", "invalid external_id")
        if not _SLUG_RE.fullmatch(slug):
            raise ProviderError("not_found", "bad external_id")
        try:
            r = await provider_safe_get(http, self, f"{BASE_URL}/{kind}/{slug}.html")
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if r.status_code != 200:
            raise ProviderError("not_found", f"status {r.status_code}")
        soup = BeautifulSoup(r.text, "lxml")
        h1 = soup.select_one("h1")
        if not h1:
            raise ProviderError("parse_failed", "title missing")
        country: str | None = extract_country(soup)
        img = soup.select_one(".full img") or soup.select_one("img[src*='/uploads/']")
        player = _player_url(r.text, self.allowed_hosts)
        if not player:
            raise ProviderError("parse_failed", "player missing")
        typ: MediaTypeStr = "series" if kind == "series" else "movie"
        seasons, dub_translations = await self._seasons(player, external_id, http)
        if seasons:
            # Issue #165: a /films/ page can carry a series-structured
            # player payload (a folder array with seasons/episodes —
            # observed live on «Друга світова війна з Томом Генксом»).
            # Classify it as a series so the client gets playable
            # episode rails instead of one unplayable movie blob.
            typ = "series"
        elif typ == "movie":
            # Gate upstream-removed movies at content() time (#158):
            # _seasons() already probes the embed and raises gated for
            # a «Контент недоступний» page, so the catalog sweep can
            # drop the dead card.
            seasons = [
                Season(
                    number=1,
                    episodes=[Episode(number=1, id=f"{self.id}:{external_id}{MOVIE_SUFFIX}", title="Фільм")],
                )
            ]
        mb_form: MediaForm = typ if typ == "movie" or typ == "series" else "series"
        mb_styles: frozenset[MediaStyle] = (
            frozenset() if typ == "movie" or typ == "series" else frozenset({typ})
        )
        return ContentResponse(
            id=f"{self.id}:{external_id}",
            title=h1.get_text(strip=True),
            poster=urljoin(BASE_URL, str(img.get("src"))) if img else None,
            translations=dub_translations or [Translation(id="uk", label="Українська")],
            seasons=seasons,
            country=country,
            form=mb_form,
            styles=mb_styles,
        )

    async def _seasons(
        self, player: str, ext: str, http: httpx.AsyncClient
    ) -> tuple[list[Season] | None, list[Translation]]:
        # ADR-0005: `player` comes from CMS HTML (untrusted) — the
        # fetch goes through the provider's declared allowlist and
        # fails closed on a disallowed host.
        try:
            r = await provider_safe_get(http, self, player)
        except httpx.HTTPError:
            return None, []
        if r.status_code == 200 and _content_unavailable(r.text):
            raise ProviderError("gated", "upstream content removed")
        raw = _file_url(r.text) if r.status_code == 200 else None
        try:
            data = cast(list[dict[str, Any]], json.loads(raw or "[]"))
        except (json.JSONDecodeError, TypeError):
            return None, []
        if not data or not isinstance(data[0], dict):
            return None, []
        first = data[0]
        if _is_season_titled(first):
            # Season-top payload: every top entry is one season
            # (season -> dubs -> episodes).
            season_entries = data
        else:
            # Wrapper payload: data[0]["folder"] holds either the
            # seasons (titled «N сезон») or the dubbing tracks.
            season_entries = first.get("folder", []) or []
        if not season_entries:
            return None, []
        # Ticket #331: when the entries are NOT titled like seasons,
        # they are dubbing tracks of ONE season (live: «Дім Дракона»
        # carries four folders titled by studio, each with the same S1
        # episodes in that voiceover). Collapse to one season whose
        # translations are the studio names — the facade must not show
        # phantom seasons for dubs.
        if not any(_is_season_titled(e) for e in season_entries if isinstance(e, dict)):
            dubs = [e for e in season_entries if isinstance(e, dict)]
            first_dub_folder = dubs[0].get("folder", []) if dubs else []
            episodes: list[Episode] = [
                Episode(
                    number=j,
                    id=episode_wire_id(self.id, ext, 1, j),
                    title=str(e.get("title", "")).strip(),
                )
                for j, e in enumerate(first_dub_folder, 1)
                if isinstance(e, dict)
            ]
            if not episodes:
                return None, []
            dub_titles = [str(d.get("title", "")).strip() for d in dubs if str(d.get("title", "")).strip()]
            translations = [Translation(id=t, label=t) for t in dub_titles]
            return [Season(number=1, episodes=episodes)], translations

        seasons: list[Season] = []
        season_dub_titles: list[str] = []
        for i, s in enumerate(season_entries, 1):
            if not isinstance(s, dict):
                continue
            dubs = [d for d in s.get("folder", []) if isinstance(d, dict)]
            if not dubs:
                continue
            # 3-level (season -> dubs -> episodes): episodes live under
            # the first dub; 2-level (season -> episodes): the folder
            # items ARE the episodes.
            first_dub = dubs[0]
            eps = (
                first_dub.get("folder", [])
                if isinstance(first_dub, dict) and "folder" in first_dub
                else dubs
            )
            episode_dubs = [str(d.get("title", "")).strip() for d in dubs if str(d.get("title", "")).strip()]
            if i == 1 and episode_dubs:
                season_dub_titles = episode_dubs
            episodes = [
                Episode(
                    number=j,
                    id=episode_wire_id(self.id, ext, i, j),
                    title=str(e.get("title", "")).strip(),
                    translations=[Translation(id=t, label=t) for t in episode_dubs] or None,
                )
                for j, e in enumerate(eps, 1)
                if isinstance(e, dict)
            ]
            if episodes:
                seasons.append(Season(number=i, episodes=episodes))
        if not seasons:
            return None, []
        return seasons, [Translation(id=t, label=t) for t in season_dub_titles]

    async def stream(
        self, content_id: str, translation: str | None, http: httpx.AsyncClient
    ) -> StreamResponse:
        ext, tail = split_wire_id(content_id)
        suffix = tail.removeprefix(":")
        kind, _, slug = ext.partition("/")
        if not kind or not slug:
            raise ProviderError("parse_failed", "invalid content_id")
        if not _SLUG_RE.fullmatch(slug):
            raise ProviderError("not_found", "bad external_id")
        try:
            r = await provider_safe_get(http, self, f"{BASE_URL}/{kind}/{slug}.html")
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        player = _player_url(r.text, self.allowed_hosts)
        if not player:
            raise ProviderError("parse_failed", "player missing")
        try:
            p = await provider_safe_get(http, self, player)
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)) from e
        if p.status_code == 200 and _content_unavailable(p.text):
            raise ProviderError("gated", "upstream content removed")
        raw = _file_url(p.text) if p.status_code == 200 else None
        if not raw:
            raise ProviderError("parse_failed", "media missing")
        if suffix and suffix != MOVIE_SUFFIX[1:]:
            parsed = parse_episode_tail(suffix)
            if parsed is None:
                raise ProviderError("parse_failed", "bad episode")
            s_idx, e_idx = parsed[0] - 1, parsed[1] - 1
            try:
                payload = cast(list[dict[str, Any]], json.loads(raw))
                first = payload[0]
                if _is_season_titled(first):
                    # Season-top payload: season index at the top level;
                    # the translation picks the dub folder INSIDE the
                    # season (fallback: first dub) (ticket #331).
                    dubs = payload[s_idx].get("folder", [])
                    dub_idx = _dub_index(dubs, translation) or 0
                    raw = payload[s_idx]["folder"][dub_idx]["folder"][e_idx]["file"]
                else:
                    # Wrapper payload: data[0]["folder"] holds the dub
                    # tracks (multi-dub) or the seasons. A translation
                    # matching a dub title wins; otherwise the legacy
                    # suffix dub indexing stays (ticket #331).
                    entries = payload[0].get("folder", [])
                    picked_dub = _dub_index(entries, translation)
                    raw = entries[picked_dub if picked_dub is not None else s_idx]["folder"][e_idx]["file"]
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
                raise ProviderError("parse_failed", "episode missing") from e
        else:
            # Issue #165: a movie whose player payload is a
            # series-structured folder array (a /films/ page that is
            # really a series) must resolve the first playable file,
            # not return the raw JSON blob as the stream URL.
            try:
                raw = json.loads(raw)[0]["folder"][0]["folder"][0]["file"]
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                pass  # plain media URL
        return StreamResponse(url=str(raw), type="m3u8", headers={"Referer": "https://eneyida.tv/"})


__all__ = ["EneyidaProvider"]
