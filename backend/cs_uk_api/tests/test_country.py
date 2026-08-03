from __future__ import annotations

from bs4 import BeautifulSoup

from cs_uk_api.country import (
    BLOCKED_COUNTRIES,
    extract_country,
    is_blocked_country,
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# --------------------------------------------------------------------------
# extract_country
# --------------------------------------------------------------------------


def test_extract_country_with_a_links():
    html = '<li><span>Країна:</span> <a href="/c/ua">Україна</a></li>'
    assert extract_country(_soup(html)) == "україна"


def test_extract_country_plain_text_sibling():
    html = '<li><span>Країна:</span> Велика Британія</li>'
    assert extract_country(_soup(html)) == "велика британія"


def test_extract_country_comma_separated():
    html = '<div>Країна:</div><div>США, Канада, Німеччина</div>'
    assert extract_country(_soup(html)) == "сша"


def test_extract_country_multiple_links_returns_first():
    html = (
        '<li><span>Країна:</span> '
        '<a href="/c/us">США</a>, <a href="/c/ca">Канада</a></li>'
    )
    assert extract_country(_soup(html)) == "сша"


def test_extract_country_none_when_absent():
    html = '<li><span>Рік виходу:</span> 2021</li><li><span>Жанр:</span> боєвик</li>'
    assert extract_country(_soup(html)) is None


def test_extract_country_nested_in_ul():
    html = (
        '<li class="item"><span class="title">Країна:</span>'
        '<ul><li><a href="/c/kr">Південна Корея</a></li></ul></li>'
    )
    assert extract_country(_soup(html)) == "південна корея"


def test_extract_country_span_sibling():
    html = '<li><span>Країна:</span> <span class="country">США</span></li>'
    assert extract_country(_soup(html)) == "сша"


def test_extract_country_russian_detected():
    html = '<li><span>Країна:</span> <a href="/c/ru">Росія</a></li>'
    assert extract_country(_soup(html)) == "росія"


def test_extract_country_no_trailing_colon_variants():
    html = '<li><span>Країна :</span> <a href="/c/ru">Росія</a></li>'
    assert extract_country(_soup(html)) == "росія"


# --------------------------------------------------------------------------
# is_blocked_country
# --------------------------------------------------------------------------


def test_is_blocked_country_russian():
    assert is_blocked_country("росія")
    assert is_blocked_country("російська федерація")
    assert is_blocked_country("россия")
    assert is_blocked_country("russia")


def test_is_blocked_country_non_russian():
    assert not is_blocked_country("україна")
    assert not is_blocked_country("сша")
    assert not is_blocked_country("японія")


def test_is_blocked_country_none_passes_open():
    assert not is_blocked_country(None)


def test_blocked_countries_is_nonempty():
    assert len(BLOCKED_COUNTRIES) > 0
    assert "росія" in BLOCKED_COUNTRIES
