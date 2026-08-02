"""Docs guard: the PROVIDERS.md registry table must match the code.

Docs-drift campaign (map #44): PROVIDERS.md is the canonical registry
view; a table row that silently diverges from a provider module (site
host, sections, or even the row's markdown shape) re-creates the drift
the campaign exists to prevent. This test enforces the sync so future
provider work keeps the doc truthful by construction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cs_uk_api.providers import PROVIDERS
from cs_uk_api.providers._registry import bootstrap

PROVIDERS_MD = Path(__file__).parents[2] / "PROVIDERS.md"

# A well-formed registry row: `| <id> | <site> | <sections> | <format> |`
# with spaces around the id column. Rows that don't match this shape
# are invisible to readers and to this guard.
_ROW_RE = re.compile(r"^\| (\w+) +\| ([^|]+?) \| ([^|]+?) \| ([^|]+) +\|$")
_SKIP_HOST_CHECK = {"coaninet", "uakino", "unimay"}  # no module-level BASE_URL


def _table_rows() -> dict[str, tuple[str, str, str]]:
    lines = PROVIDERS_MD.read_text().splitlines()
    # Only the ## Registry table is a code mirror; later tables (Live
    # gate) have different shapes.
    start = next(i for i, l in enumerate(lines) if l.startswith("## Registry"))
    end = next(
        (i for i, l in enumerate(lines) if l.startswith("## ") and i > start),
        len(lines),
    )
    rows: dict[str, tuple[str, str, str]] = {}
    for lineno in range(start, min(end, len(lines))):
        line = lines[lineno]
        if not line.startswith("|") or line.startswith("|---"):
            continue
        if re.match(r"^\| (id|provider)\s*\|", line):
            continue  # table header
        m = _ROW_RE.match(line)
        if m is None:
            raise AssertionError(
                f"PROVIDERS.md:{lineno + 1} malformed registry row: {line!r}"
            )
        pid, site, sections, stream_format = m.groups()
        rows[pid] = (site.strip(), sections.strip(), stream_format.strip())
    return rows


@pytest.fixture(scope="module", autouse=True)
def _boot() -> None:
    bootstrap()


def test_registry_table_is_complete_and_well_formed() -> None:
    rows = _table_rows()
    ids = sorted(PROVIDERS)
    assert set(rows) == set(ids), (
        "PROVIDERS.md rows and registered providers diverge: "
        f"doc-only={sorted(set(rows) - set(ids))} "
        f"code-only={sorted(set(ids) - set(rows))}"
    )
    assert len(rows) == len(ids)


def test_registry_table_sites_match_provider_hosts() -> None:
    for pid, (site, _, _) in _table_rows().items():
        if pid in _SKIP_HOST_CHECK:
            continue
        mod = __import__(f"cs_uk_api.providers.{pid}", fromlist=["BASE_URL"])
        host = mod.BASE_URL.split("//", 1)[-1].split("/", 1)[0]
        assert site.split(" ")[0].lstrip("(") == host, (
            f"PROVIDERS.md site '{site}' != {pid}.BASE_URL host '{host}'"
        )


def test_registry_table_sections_match_provider_sections() -> None:
    for pid, (_, sections, _) in _table_rows().items():
        code_sections = {s.id for s in PROVIDERS[pid].sections}
        table_sections = {s.strip() for s in sections.split(",")}
        assert table_sections == code_sections, (
            f"PROVIDERS.md sections for {pid} = {sorted(table_sections)}, "
            f"code = {sorted(code_sections)}"
        )
