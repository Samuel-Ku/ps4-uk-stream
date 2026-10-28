"""Doc-drift guard: backticked refs in living docs must resolve against code.

The 2026-09-19 sweep found five real drifts in the living docs — dead
symbols named as load-bearing fact (``CatalogStores``, ``STORES``,
``main._browse_cache``, ``DirectPlayerResponse``) and a pre-rename
module path — because nothing failed when code moved and docs did not.
This guard makes that accumulation loud: it extracts every backticked
span from the living docs, resolves each against the tracked codebase,
and fails on any span that looks like a concrete reference (identifier,
dotted chain, or file path) but matches nothing.

False-positive classes observed during the sweep are built into the
resolver, not the allowlist:

- fenced code blocks are skipped (their content is quoted, not claimed);
- placeholders (``<id>``, ``{...}``, ``$VAR``, ``...``) are not refs;
- prefix-relative paths (``tests/test_x.py`` under ``backend/``) resolve
  by trying ``backend/`` and repo-root roots, then basename match;
- dotted chains resolve when their last component is a defined Python
  name (``catalog_state.merged_search`` survives the module rename);
- full-span substring across the non-doc tracked corpus resolves shell
  knobs (``CS_UK_API_URL`` lives in ``deploy/*.sh``) and upstream URLs
  the adapters build verbatim.

The allowlist is the last resort, one reason per entry, and it is for
*documented* non-existence only: recorded removals/moves and glossary
concept terms whose wire-model name differs. Everything else failing to
resolve is drift.

One class is environment-sensitive: a bare commit SHA resolves against
the object database, so a SHALLOW clone cannot verify the commits the
docs cite (``docs/v1-deviations.md`` cites four). That is why the
release gate checks out FULL history — and why a shallow run fails with
the environment NAMED rather than excusing the spans: an unverifiable
claim is not a verified one, and allowlisting it would switch the check
off for a typo the shallow clone cannot see either.

Usage:
    python -m cs_uk_api.scripts.doc_drift_guard [--root PATH] [--verbose]

Exit code 0 = every span resolved or allowlisted; 1 = drift found
(the report names file, line and span for each).
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

#: The living docs — the drift surface. Dated records (docs/adr/*,
#: docs/status-*.md, docs/*-2026-*.md, research/, superpowers/) are
#: point-in-time by design and deliberately not scanned; their staleness
#: is history, not drift. ``docs/status.md`` is also dated: its own
#: header scopes it to "what was delivered by the implementation pass
#: that followed the plan" — a delivery log, not a maintained contract.
LIVING_DOCS: tuple[str, ...] = (
    "CONTEXT.md",
    "README.md",
    "CLAUDE.md",
    "docs/architecture.md",
    "docs/provider-triage.md",
    "docs/torrent-lane.md",
    "docs/v1-deviations.md",
    "backend/PROVIDERS.md",
    "backend/deploy/DEPLOY.md",
    "backend/cs_uk_api/scripts/README.md",
)

#: Spans that look unresolvable but are documented non-existence. One
#: reason each; a future reader must be able to judge an entry without
#: re-deriving it. Documented *removals* and *concept-vs-wire-name*
#: mappings only — never "haven't fixed it yet".
ALLOWLIST: dict[str, str] = {
    "main._browse_cache": (
        "documented move pointer (architecture.md §5): the cache moved "
        "into _stores.browse_cache; the old name is kept once as history"
    ),
    "ProviderCapabilities": (
        "glossary concept term; the wire model is ProviderInfo "
        "(CONTEXT.md maps the two deliberately)"
    ),
    "model_b_axes": (
        "documented removal (contract step #319): the legacy mapping was "
        "replaced by the typed form/styles axes"
    ),
    "is_adult": (
        "documented rejection (ADR-0001): no adult flag anywhere in the "
        "domain model — the doc line asserts its non-existence"
    ),
    "styles_all": (
        "documented deferral (ADR-0001): subset semantics rejected as "
        "YAGNI; adding later is non-breaking"
    ),
    "is_primary": (
        "documented rejection (CONTEXT.md): list order encodes dub "
        "priority; a primary flag was declined"
    ),
    "default_translation_id": (
        "documented rejection (CONTEXT.md): translations[0] encodes the "
        "default by list convention"
    ),
    "retryable": (
        "documented rejection: per-item retry flag was declined; the "
        "search budget is a total asyncio.wait_for, not per-provider "
        "retries"
    ),
    "tenacity": (
        "documented rejection: the retry dependency was declined; "
        "asyncio.wait_for carries the budget"
    ),
    "wontfix": (
        "process vocabulary (triage label), defined in "
        "docs/agents/triage-labels.md — not a code symbol"
    ),
    "wlan0": (
        "operator environment (network interface on the target host in "
        "README's setup example) — not a repo symbol"
    ),
    "semen": (
        "operator environment (the deployment host's user, quoted as a "
        "host-specific value in DEPLOY.md §2's edit table and in "
        "docs/agents/remote-hosts.md's host contract) — the units ship the "
        "old operator's paths and say so, so nothing tracked carries it"
    ),
    "/etc/netplan/50-cloud-init.yaml": (
        "operator environment (the deployment host's netplan file, quoted "
        "in README's network map as the place its static address lives) — "
        "a host path, not a repo file"
    ),
    "omarchy": (
        "operator environment (the authoring workstation's hostname in "
        "README's network map) — not a repo symbol"
    ),
    "/data/Switchfin/config.json": (
        "operator environment (the PS4 client's saved server/token file on "
        "the console, quoted in README's network map because a changed host "
        "address must be mirrored into it) — a device path, not a repo file"
    ),
    "_STYLE_BY_TYPE": (
        "documented removal (contract step #319): the legacy type→styles "
        "table was replaced by typed constructors"
    ),
    "backend/probe_rail.py": (
        "documented deletion (probe T2 #328): throwaway triage probe, "
        "consolidated into cs_uk_api/drift/probe.py"
    ),
    "probe_rail.py": (
        "documented deletion (probe T2 #328): throwaway triage probe, "
        "consolidated into cs_uk_api/drift/probe.py"
    ),
    "_VIEW_TYPE_BY_ID": (
        "documented deletion: the facade's private view-type maps were "
        "replaced by the shared row-registry vocabulary"
    ),
    "codeberg.org/CakesTwix/cloudstream-extensions-uk": (
        "upstream repository — provider-triage.md catalogs the Cloudstream "
        "extensions' code, which this repo does not track"
    ),
    "mild": (
        "upstream triage vocabulary in provider-triage.md, not a repo symbol"
    ),
    "getM3url": (
        "upstream Kotlin method in provider-triage.md (Cloudstream "
        "extension internals)"
    ),
    "Povnometrazhka": (
        "upstream Cloudstream extension name in provider-triage.md"
    ),
    "banderakino": (
        "upstream Cloudstream extension name in provider-triage.md"
    ),
    "_crypto.py": (
        "upstream Cloudstream extension file in provider-triage.md"
    ),
    "ExtractorApi": (
        "upstream Cloudstream class in provider-triage.md"
    ),
    "ExtractorLink": (
        "upstream Cloudstream class in provider-triage.md"
    ),
    "lftp": (
        "external CLI tool named in provider-triage.md's ops notes, not "
        "a repo symbol"
    ),
    "./config/settings.json": (
        "the BitPlay container's config path (container filesystem, "
        "torrent-lane.md runbook) — not a repo file"
    ),
    "mongoimport": (
        "external CLI tool in torrent-lane.md's ops notes, not a repo symbol"
    ),
    "libchromaprint1": (
        "Debian/Ubuntu library package named in DEPLOY.md's teleport "
        "prerequisites as the package that does NOT carry `fpcalc` — a "
        "distro package on the host, not a repo symbol"
    ),
    "kRows": (
        "the PS4 client's C++ symbol (separate repo) — v1-deviations.md "
        "records client-side build deviations"
    ),
    "kCols": (
        "the PS4 client's C++ symbol (separate repo) — v1-deviations.md "
        "records client-side build deviations"
    ),
    "kLayout": (
        "the PS4 client's C++ symbol (separate repo) — v1-deviations.md "
        "records client-side build deviations"
    ),
    "OnscreenKeyboard::kRows": (
        "the PS4 client's C++ symbol (separate repo) — v1-deviations.md "
        "records client-side build deviations"
    ),
    "OnscreenKeyboard::kCols": (
        "the PS4 client's C++ symbol (separate repo) — v1-deviations.md "
        "records client-side build deviations"
    ),
    "CXX": (
        "CMake variable in the PS4 client's standalone build (separate repo)"
    ),
    "div.video-box iframe[data-src]": (
        "upstream DOM selector quoted in an adapter's notes (CSS, not a "
        "repo symbol)"
    ),
    "Mao": (
        "upstream title string quoted in PROVIDERS.md's gate-word examples"
    ),
    "3f4641d": (
        "a commit in the upstream codeberg Cloudstream extensions repo, "
        "cited by provider-triage.md — not an object of this repository"
    ),
}

#: Tracked extensions whose files form the resolution corpus (everything
#: that can legitimately define or use a symbol). Docs (.md) are excluded
#: from the corpus — docs referencing docs would make every
#: cross-reference self-proving.
CORPUS_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".sh", ".toml", ".json", ".yml", ".yaml", ".cfg", ".ini",
     ".service", ".timer", ".txt"}
)

#: Suffixes that mark an *upstream web* reference (the providers' sites),
#: never one of ours — skipped, since guarding them means network probes.
WEB_SUFFIXES: frozenset[str] = frozenset({".html", ".htm", ".php", ".aspx"})

_SPAN_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"^\s*```")
_DEFINE_RE = re.compile(r"^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=", re.MULTILINE)
#: Annotated names (pydantic fields: ``    id: str``) — claims like
#: ``Section.id`` must resolve against the model's own fields.
_ANNOT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:[^=]", re.MULTILINE)
_PATH_RE = re.compile(r"[\w./-]+\.[A-Za-z]{2,5}\b")
_PLACEHOLDER_RE = re.compile(r"[<>{}$*]|\.\.\.")
_URL_RE = re.compile(r"https?://|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
#: Operational shell lines are instructions, not symbol claims.
_COMMAND_RE = re.compile(
    r"^(?:rm|cd|pip|python3?|uvicorn|git|docker|curl|cp|mv|mkdir|sudo|"
    r"systemctl|journalctl|source)\b|~\s|~/.| && "
)
#: HTTP routes (ours or the upstream facade surface) — guarding them
#: means route-table extraction, beyond a lightweight symbol guard.
_ROUTE_RE = re.compile(r"^(?:GET|POST|PUT|DELETE|PATCH|HEAD\b)?\s*/")
#: Verbatim quotes and log-line fragments (arrows, quotes) are prose,
#: not symbol claims.
_PROSE_RE = re.compile(r"[→'\"≈]")
#: Bare git commit SHAs resolve against the object database.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def is_shallow(container: Path) -> bool:
    """True when the repository at ``container`` is a shallow clone.

    A shallow clone's object database holds only the fetched tip, so a
    commit the docs cite can be real and simply absent — an environment
    fact, not drift. Stated apart from the resolution so the report can
    name it (``shallow_sha_hint``) instead of leaving a red gate looking
    like doc drift.
    """
    out = subprocess.run(
        ["git", "-C", str(container), "rev-parse", "--is-shallow-repository"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.returncode == 0 and out.stdout.strip() == "true"


def shallow_sha_hint(
    drift: list[tuple[str, int, str]], *, is_shallow_repo: bool
) -> str | None:
    """The environment note for SHA spans a shallow clone cannot verify.

    None when the repository is complete (the spans really are drift) or
    when no unresolved span is a commit SHA. The spans are NOT excused —
    the guard still fails; the note only says which failure is an
    environment fact, and what fixes it, so nobody allowlists a real
    claim to make a shallow checkout green.
    """
    if not is_shallow_repo:
        return None
    shas = sorted({span for _, _, span in drift if _SHA_RE.fullmatch(span)})
    if not shas:
        return None
    return (
        f"{len(shas)} unresolved span(s) are commit SHAs ({', '.join(shas)}) and this "
        "checkout is SHALLOW: the cited commits are absent from its object database. "
        "Fetch full history (actions/checkout with fetch-depth: 0) instead of "
        "allowlisting them — they are real claims, checkable only in a full clone."
    )


def _tracked_files(repo: Path) -> list[str]:
    """Tracked file paths (``git ls-files``), forward-slashed."""
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached"],
        capture_output=True, text=True, check=True,
    )
    return [p for p in out.stdout.split("\0") if p]


def _load_corpus(repo: Path, files: list[str]) -> tuple[str, list[str]]:
    """Concatenated bytes of tracked corpus files + the tracked .md list.

    Returns ``(corpus_text, tracked_md_paths)``. Missing files (deleted
    in the working tree but still indexed) are skipped silently — the
    guard reports on the tree as checked out.
    """
    chunks: list[str] = []
    md: list[str] = []
    for rel in files:
        path = repo / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rel.endswith(".md"):
            md.append(rel)
        elif path.suffix in CORPUS_SUFFIXES:
            chunks.append(text)
    return "\n".join(chunks), md


def _defined_names(corpus: str) -> set[str]:
    """Python names the codebase defines (``def``/``class``/assignment)."""
    names: set[str] = set()
    for line in corpus.splitlines():
        m = _DEFINE_RE.match(line.strip())
        if m:
            names.add(m.group(1))
    names.update(_ASSIGN_RE.findall(corpus))
    names.update(_ANNOT_RE.findall(corpus))
    return names


def _resolve_path(span: str, files: list[str], md: list[str]) -> bool:
    """Path-like spans: exact (with root normalization), basename, or dir."""
    if span in files or f"backend/{span}" in files:
        return True
    clean = span.rstrip("/")
    dirs = {f.rsplit("/", 1)[0] for f in files if "/" in f}
    if clean in dirs or f"backend/{clean}" in dirs:
        return True
    base = clean.rsplit("/", 1)[-1]
    corpus_all = files + md
    if "/" in clean:
        candidates = [f for f in corpus_all if f.endswith("/" + base)]
    else:
        candidates = [f for f in corpus_all if f.split("/")[-1] == base]
    return bool(candidates)


def _resolve_dotted(
    span: str, corpus: str, defined: set[str], files: list[str]
) -> bool:
    """Dotted chains / bare identifiers.

    A chain resolves when the full span or ANY dotted prefix occurs
    verbatim in the corpus (module ownership: ``httpx.Timeout`` rides on
    ``httpx``; ``ResumeStore._finished`` on ``ResumeStore``), when its
    last component is a defined Python name (survives module renames:
    ``catalog_state.merged_search``), or when any suffix of the chain
    names a tracked module file.
    """
    parts = span.split(".")
    for i in range(len(parts)):
        if _in_corpus(".".join(parts[i:]), corpus):
            return True
    if parts[-1] in defined:
        return True
    for i in range(len(parts)):
        module = "/".join(parts[i:]) + ".py"
        if module in files or f"backend/cs_uk_api/{module}" in files:
            return True
        pkg = "/".join(parts[i:]) + "/__init__.py"
        if pkg in files or f"backend/cs_uk_api/{pkg}" in files:
            return True
    # Importable module prefix (stdlib/third-party: ``asyncio.wait_for``).
    return importlib.util.find_spec(parts[0]) is not None


def _in_corpus(span: str, corpus: str) -> bool:
    """Verbatim presence in the tracked non-doc corpus."""
    return span in corpus


def _file_suffix(span: str) -> str | None:
    """The span's trailing file suffix, when it has one."""
    m = re.search(r"\.(\w{1,5})$", span)
    return f".{m.group(1)}" if m else None


def classify_and_resolve(
    span: str,
    *,
    repo: Path,
    corpus: str,
    defined: set[str],
    files: list[str],
    md: list[str],
) -> bool:
    """True when the span is fine (resolved, allowlisted, or not a ref)."""
    if span in ALLOWLIST:
        return True
    if _PLACEHOLDER_RE.search(span):
        return True
    if _URL_RE.search(span):
        return True  # LAN/loopback URLs in setup docs, upstream API bases
    if _COMMAND_RE.search(span):
        return True  # operational shell lines, not symbol claims
    if _PROSE_RE.search(span):
        return True  # verbatim quotes / log fragments
    if _SHA_RE.fullmatch(span):
        return (
            subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", span],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    opt = re.sub(r"\s*\[[^\]]*\]\s*$", "", span)
    if opt != span and opt:  # ``refresh_uakino.py [--close]`` usage form
        return classify_and_resolve(
            opt, repo=repo, corpus=corpus, defined=defined, files=files, md=md
        )
    if "::" in span:  # ``llm.py::TasteProfile`` — every part must resolve
        return all(
            classify_and_resolve(
                part, repo=repo, corpus=corpus, defined=defined,
                files=files, md=md,
            )
            for part in span.split("::")
        )
    suffix = _file_suffix(span)
    if suffix is not None:
        if suffix in WEB_SUFFIXES:
            return True  # upstream-site page patterns (coaninet, uakino…)
        if suffix in CORPUS_SUFFIXES or suffix == ".md":
            if _resolve_path(span, files, md):
                return True
            # Runtime artifacts (``user-state.json``) are produced by the
            # stores, not tracked — the code naming them is the claim.
            return _in_corpus(span, corpus)
    if "/" in span:
        if span.startswith("/") or _ROUTE_RE.match(span):
            return True  # HTTP route (ours or upstream), not a file path
        if _resolve_path(span, files, md):
            return True
        # ``providers/base.MOVIE_SUFFIX`` is a module/attr chain, not a
        # file path — retry it through the dotted resolver.
        return _resolve_dotted(
            span.replace("/", "."), corpus, defined, files
        )
    if "." in span or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", span):
        # ``merge.group_key(alias, form, year)``: the call syntax is not
        # the claim — the callable's name is.
        target = re.sub(r"\([^)]*\)$", "", span)
        if target != span and not target:
            return True
        if _in_corpus(target, corpus):
            return True
        return _resolve_dotted(target, corpus, defined, files)
    return True  # prose, routes with query strings, everything else


def collect_drift(
    repo: Path, docs: tuple[str, ...] = LIVING_DOCS
) -> list[tuple[str, int, str]]:
    """Unresolved spans across the living docs as ``(file, line, span)``."""
    files = _tracked_files(repo)
    corpus, md = _load_corpus(repo, files)
    defined = _defined_names(corpus)
    drift: list[tuple[str, int, str]] = []
    for doc in docs:
        path = repo / doc
        if not path.exists():
            drift.append((doc, 0, "<living doc missing from the tree>"))
            continue
        in_fence = False
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for m in _SPAN_RE.finditer(line):
                span = m.group(1).strip()
                if not span:
                    continue
                if not classify_and_resolve(
                    span, repo=repo, corpus=corpus, defined=defined,
                    files=files, md=md,
                ):
                    drift.append((doc, lineno, span))
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="repo root (default: three levels above this script)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    drift = collect_drift(args.root)
    if not drift:
        print(
            f"doc-drift guard: all backticked refs in {len(LIVING_DOCS)} "
            "living docs resolve (or are allowlisted with a reason)"
        )
        return 0
    print(f"doc-drift guard: {len(drift)} unresolved span(s):")
    for doc, lineno, span in drift:
        where = f"{doc}:{lineno}" if lineno else doc
        print(f"  {where}: `{span}`")
    hint = shallow_sha_hint(drift, is_shallow_repo=is_shallow(args.root))
    if hint is not None:
        print(f"\n{hint}")
    if args.verbose:
        print("\nallowlisted (not failures):")
        for name, reason in sorted(ALLOWLIST.items()):
            print(f"  {name}: {reason}")
    print(
        "\nIf a span is a documented removal/move or a concept-vs-wire-name "
        "mapping, add it to ALLOWLIST with that reason. Otherwise fix the "
        "doc or the code — do not allowlist to make the failure go away."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
