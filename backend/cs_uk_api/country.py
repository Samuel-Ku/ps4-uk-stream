from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag

BLOCKED_COUNTRIES: frozenset[str] = frozenset(
    {
        "росія",
        "російська федерація",
        "россия",
        "российская федерация",
        "russia",
        "russian federation",
        "rf",
        "рос. федерація",
        "рос.федерація",
    }
)

_COUNTRY_LABEL = "країна"


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _first_country(text: str) -> str | None:
    """From a possibly comma-separated list, return the first non-empty entry."""
    if not text:
        return None
    parts = [p.strip() for p in text.split(",")]
    first = parts[0] if parts else ""
    return first or None


def _text_of(element: PageElement) -> str:
    if isinstance(element, Tag):
        return element.get_text(strip=True)
    return str(element).strip()


def _collect_value_after(label: Tag | NavigableString) -> str | None:
    """Extract country text from the siblings of *label*.

    Walks the parent's children starting *after* the element that
    contains the label text, collecting text from following ``<a>``
    links or plain-text nodes.
    """
    parent = label.parent
    if parent is None:
        return None
    seen_label = False
    texts: list[str] = []
    for child in parent.children:
        if child is label:
            seen_label = True
            continue
        if not seen_label:
            continue
        text = _text_of(child)
        if text and text != ":":
            texts.append(text)
    if texts:
        return ", ".join(texts)
    return None


def extract_country(soup: BeautifulSoup) -> str | None:
    """Extract the first country-of-origin value from a content page.

    Handles the common Ukrainian provider layout where a metadata row
    carries the label ``Країна:`` and the value is either:

    * a list of ``<a>`` links (kinovezha, uaserialspro, serialno,
      eneyida, kinotron, bambooua, doramyworld), or
    * plain text following the label (cikavaideya, klontv, uaflix).

    Returns the **first** country as a normalized (lower-cased,
    collapsed whitespace) string, or ``None`` when the page has no
    country field at all (fail-open signal for the caller).
    """
    for element in soup.find_all(["li", "div", "span"]):
        if not isinstance(element, Tag):
            continue
        label = element.find(string=True)
        if label is None:
            continue
        if _COUNTRY_LABEL in label.strip().lower():
            links = element.select("a")
            value: str | None
            if links:
                value = links[0].get_text(strip=True)
            else:
                sibling = element.find_next_sibling("a")
                if sibling is not None:
                    value = sibling.get_text(strip=True)
                else:
                    value = _collect_value_after(element)
            if value:
                first = _first_country(value)
                if first:
                    return _normalize(first)
    for text_node in soup.find_all(string=True):
        if _COUNTRY_LABEL in text_node.lower():
            parent = text_node.parent
            if parent is None:
                continue
            links = parent.select("a") or parent.find_next_siblings("a")
            if links:
                v = links[0].get_text(strip=True)
                if v:
                    first = _first_country(v)
                    if first:
                        return _normalize(first)
            value = _collect_value_after(text_node)
            if value:
                first = _first_country(value)
                if first:
                    return _normalize(first)
    return None


def is_blocked_country(country: str | None) -> bool:
    """Return True if *country* matches a blocked entry exactly.

    ``None`` (unknown) is **never** blocked — this preserves the
    fail-open contract.
    """
    if country is None:
        return False
    return country in BLOCKED_COUNTRIES
