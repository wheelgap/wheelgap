"""--fix: say which gaps a wheel from wheelgap's gap wheel index fills, and how to install from it.

The gap wheel list (pypi.fetch_gap_wheels) is read in one request that names no package. Each entry is untrusted: one
that isn't exactly the expected shape is dropped whole (parse). A cell is fillable only when all of these hold
(fillable):
- its status is `source` or `missing` on a target that isn't --ignore'd;
- the package is positively PyPI's (origin.known_pypi), and pip may use its wheels (no --no-binary);
- an entry has the same canonical name and an equal version, and the target accepts one of its wheel's tags.

It never changes a status, the summary or the exit code: wheelgap still reports what PyPI has.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from urllib.parse import quote

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from . import origin, pypi
from .check import Row, Status, wheel_tags
from .sources import Source
from .targets import Target

SCHEMA = 1
# Inputs pip installs from as they are: a requirements file, a pyproject.toml, a pip report or package names. Lockfiles
# pin each file's URL, so adding an index to pip changes nothing for them; they get the lockfile advice (_lock_kinds).
PIP_KINDS = frozenset({"requirements", "pyproject", "pip-report", None})
INDEX = "https://wheelgap.github.io/wheels/simple/"
DOWNLOAD = "https://github.com/wheelgap/wheels/releases/download/gap-wheels"
ABOUT = "https://wheelgap.github.io/wheels/"
# PEP 508 names, as sources.py reads them.
_NAME = re.compile(r"[A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9]", re.IGNORECASE | re.ASCII)
# What a wheel filename may hold here: it goes into a URL and onto the terminal.
_WHEEL = re.compile(r"[A-Za-z0-9_.!+-]+\.whl", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_TOML_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)


@dataclass(frozen=True)
class GapWheel:
    key: str  # canonical name
    version: Version
    wheel: str
    sha256: str
    tags: frozenset[Tag]
    requires_python: SpecifierSet | None

    @property
    def url(self) -> str:
        return f"{DOWNLOAD}/{quote(self.wheel)}"


def _entry(e: object) -> GapWheel | None:
    """One entry of the list, or None if any field isn't exactly what's expected."""
    if not isinstance(e, dict):
        return None
    name, version, wheel, sha256 = e.get("name"), e.get("version"), e.get("wheel"), e.get("sha256")
    if not (isinstance(name, str) and _NAME.fullmatch(name)):
        return None
    if not (isinstance(sha256, str) and _SHA256.fullmatch(sha256)):
        return None
    if not (isinstance(wheel, str) and _WHEEL.fullmatch(wheel)) or not isinstance(version, str):
        return None
    try:
        parsed = Version(version)
        wheel_name, wheel_version, _, _ = parse_wheel_filename(wheel)
    except (InvalidVersion, InvalidWheelFilename):
        return None
    if wheel_name != canonicalize_name(name) or wheel_version != parsed:
        return None
    tags = wheel_tags([wheel])
    if not tags:
        return None
    requires_python = None
    if "requires_python" in e:
        spec = e["requires_python"]
        if not isinstance(spec, str):
            return None
        try:
            requires_python = SpecifierSet(spec)
        except InvalidSpecifier:
            return None
    return GapWheel(canonicalize_name(name), parsed, wheel, sha256, tags[0], requires_python)


def parse(data: object) -> list[GapWheel] | None:
    """The list's usable entries, or None if the document itself isn't the expected shape (or schema)."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("wheels"), list):
        return None
    return [w for w in map(_entry, data["wheels"]) if w is not None]


def look_up() -> list[GapWheel] | None:
    """The gap wheel list, or None if it couldn't be fetched or read."""
    try:
        data = pypi.fetch_gap_wheels()
    except Exception:  # unreachable, a timeout, an HTTP error, a body that isn't JSON...
        return None
    return parse(data)


@dataclass(frozen=True)
class Fill:
    row: Row
    target: Target
    wheel: GapWheel
    source: Source


def candidates(row: Row, targets: list[Target]) -> list[Target]:
    """The targets where a gap wheel could fill this row's gap: `source` or `missing`, and not --ignore'd."""
    return [t for t in targets if row.results[t] in (Status.SOURCE, Status.MISSING) and t not in row.ignored]


def eligible(row: Row, source: Source, simple_index: str | None, private: Collection[str]) -> bool:
    """Whether this row's package may get a gap wheel at all: positively PyPI's, pinned, and not --no-binary."""
    return (
        row.package.version is not None
        and not row.package.no_binary
        and origin.known_pypi(row.package, source, simple_index=simple_index, private=private)
    )


def fillable(
    rows: list[Row],
    targets: list[Target],
    source_of: dict[int, Source],
    wheels: list[GapWheel],
    private: Collection[str],
    simple_index: str | None = None,
) -> list[Fill]:
    """The cells a gap wheel fills, in row and target order; the first fitting entry of the list for each."""
    by_key: dict[str, list[GapWheel]] = {}
    for w in wheels:
        by_key.setdefault(w.key, []).append(w)
    fills = []
    for r in rows:
        source = source_of[id(r.package)]
        if not by_key.get(r.package.key) or not eligible(r, source, simple_index, private):
            continue
        try:
            version = Version(r.package.version or "")
        except InvalidVersion:
            continue
        for t in candidates(r, targets):
            python = Version(f"{t.major}.{t.minor}")
            for w in by_key[r.package.key]:
                if w.version != version or not t.accepts(w.tags):
                    continue
                if w.requires_python is not None and not w.requires_python.contains(python, prereleases=True):
                    continue
                fills.append(Fill(r, t, w, source))
                break
    return fills


def to_json(fills: list[Fill], multi_py: bool) -> list[dict]:
    """--json's gap_wheels: one entry per filled cell."""
    return [
        {
            "name": f.row.package.name,
            "version": f.row.package.version,
            "target": f.target.label(multi_py),
            "wheel": f.wheel.wheel,
            "url": f.wheel.url,
            "sha256": f.wheel.sha256,
        }
        for f in fills
    ]


def _by_package(fills: list[Fill]) -> dict[tuple[str, str], list[Fill]]:
    """Fills grouped by (name as the input writes it, version), in order."""
    grouped: dict[tuple[str, str], list[Fill]] = {}
    for f in fills:
        grouped.setdefault((f.row.package.name, f.row.package.version or ""), []).append(f)
    return grouped


def _toml_key(name: str) -> str:
    return name if _TOML_BARE_KEY.fullmatch(name) else json.dumps(name)


def _plain(text: str) -> str:
    return text


def _code(text: str) -> str:
    return f"`{text}`"  # only names, "names" and TOML table headers: no backticks in them


def _uv_direct_note(name: str, fills: list[Fill], code: Callable[[str], str] = _plain) -> str | None:
    """For a uv.lock package, what to do besides its [tool.uv.sources] line: uv applies sources only to direct
    dependencies, so a transitive one would still come from PyPI. None when the lock shows it's a direct one (a
    dependency path of length 2, from the project or one of its groups, on some filled target)."""
    paths = [f.row.via.get(f.target) for f in fills]
    if any(p is not None and len(p) == 2 for p in paths):
        return None
    add = (
        f"uv only applies {code('[tool.uv.sources]')} to direct dependencies, so {{}}also list {code(json.dumps(name))}"
    )
    if paths and all(p is not None and len(p) > 2 for p in paths):
        firsts = list(dict.fromkeys(p[1] for p in paths if p is not None))
        through = ", ".join(map(code, firsts))
        # Next to the package that brings it in: the same dependencies, dependency group or extra, so a dev-only
        # package doesn't become a runtime dependency.
        return (
            f"{code(name)} comes in through {through}; "
            + add.format("")
            + f" where {through} {'is' if len(firsts) == 1 else 'are'}"
            + " (the same dependencies, dependency group or extra)"
        )
    return f"wheelgap can't tell whether {code(name)} is a direct dependency; " + add.format("to be safe ") + " as one"


def _parts(
    fills: list[Fill], multi_py: bool, code: Callable[[str], str] = _plain
) -> tuple[list[str], list[str], list[str], bool]:
    """(one line per package, the uv config lines, the uv notes, whether the pip way applies)."""
    listed = [
        f"{name} {version}: {', '.join(dict.fromkeys(f.target.label(multi_py) for f in fs))}"
        for (name, version), fs in _by_package(fills).items()
    ]
    uv_names: dict[str, list[Fill]] = {}  # by canonical name: one sources line per package
    pip = False
    for f in fills:
        if f.source.kind == "uv":
            uv_names.setdefault(f.row.package.key, []).append(f)
        elif f.source.kind in PIP_KINDS:
            pip = True
    config: list[str] = []
    notes: list[str] = []
    if uv_names:
        config = ["[[tool.uv.index]]", 'name = "wheelgap"', f'url = "{INDEX}"', "explicit = true", ""]
        config.append("[tool.uv.sources]")
        for fs in uv_names.values():
            name = fs[0].row.package.name
            config.append(f'{_toml_key(name)} = {{ index = "wheelgap" }}')
            if note := _uv_direct_note(name, fs, code):
                notes.append(note)
    return listed, config, notes, pip


def _lock_kinds(fills: list[Fill]) -> list[str]:
    """The input kinds (other than uv.lock and pip's) of the fills, as their file labels, in order."""
    return list(
        dict.fromkeys(f.source.label for f in fills if f.source.kind != "uv" and f.source.kind not in PIP_KINDS)
    )


def _hashes(fills: list[Fill]) -> list[tuple[str, str]]:
    """(name==version, sha256) per pip-way wheel, for requirements files that pin hashes."""
    return list(
        dict.fromkeys(
            (f"{f.row.package.name}=={f.row.package.version}", f.wheel.sha256)
            for f in fills
            if f.source.kind in PIP_KINDS
        )
    )


PIP_CAVEAT = (
    "pip treats every index alike, so do this only if these packages come from PyPI: not if a private index "
    "(set in pip.conf, say) has packages of the same names."
)
LOCK_CAVEAT = (
    "An index that isn't limited to these packages has the same risk as with pip: don't add it if a private index "
    "has packages of the same names."
)
UV_MERGE = "If pyproject.toml already has a [tool.uv.sources] table, add these lines to it instead of a second one."


def text_lines(fills: list[Fill], multi_py: bool) -> list[str]:
    """--fix's section of the text output (empty when nothing is fillable)."""
    if not fills:
        return []
    listed, config, notes, pip = _parts(fills, multi_py)
    out = ["", "wheelgap's gap wheel index has wheels PyPI lacks for:"]
    out += [f"  {line}" for line in listed]
    if config:
        out += ["", "To install them with uv, add this to pyproject.toml, then run uv lock:", "", *config, ""]
        out += [f"{note}." for note in notes]
        out.append(UV_MERGE)
    if pip:
        out += [
            "",
            "To install them with pip, add this line to your requirements file:",
            f"  --extra-index-url {INDEX}",
            "or pass it to pip install:",
            f"  pip install --extra-index-url {INDEX} ...",
            "If the requirements pin hashes, also add each wheel's hash to its line:",
            *(f"  {req} --hash=sha256:{sha}" for req, sha in _hashes(fills)),
            PIP_CAVEAT,
        ]
    if locks := _lock_kinds(fills):
        out += [
            "",
            f"{', '.join(locks)} pins each file, so add {INDEX} as an index (for these packages only, if the tool "
            "allows it) where that lockfile is made, then lock again.",
            LOCK_CAVEAT,
        ]
    out += ["", f"How these wheels are built and checked: {ABOUT}"]
    return out


def markdown_lines(fills: list[Fill], multi_py: bool) -> list[str]:
    """--fix's section of the Markdown report (empty when nothing is fillable)."""
    if not fills:
        return []
    listed, config, notes, pip = _parts(fills, multi_py, _code)
    out = ["#### Gap wheels", "", "wheelgap's gap wheel index has wheels PyPI lacks for:", ""]
    out += [f"- `{line}`" for line in listed]  # valid names and versions, and target labels: no backticks
    out.append("")
    if config:
        out += ["To install them with uv, add this to `pyproject.toml`, then run `uv lock`:", "", "```toml", *config]
        out += ["```", ""]
        out += [f"- {note}." for note in notes]
        if notes:
            out.append("")
        out += [
            UV_MERGE.replace("pyproject.toml", "`pyproject.toml`").replace("[tool.uv.sources]", "`[tool.uv.sources]`"),
            "",
        ]
    if pip:
        out += [
            "To install them with pip, add this line to your requirements file, or pass it to `pip install`:",
            "",
            "```",
            f"--extra-index-url {INDEX}",
            "```",
            "",
            "If the requirements pin hashes, also add each wheel's hash to its line:",
            "",
            "```",
            *(f"{req} --hash=sha256:{sha}" for req, sha in _hashes(fills)),
            "```",
            "",
            PIP_CAVEAT,
            "",
        ]
    if locks := _lock_kinds(fills):
        out += [
            f"{', '.join(f'`{k}`' for k in locks)} pins each file, so add `{INDEX}` as an index (for these packages "
            "only, if the tool allows it) where that lockfile is made, then lock again.",
            "",
            LOCK_CAVEAT,
            "",
        ]
    out += [f"How these wheels are built and checked: {ABOUT}", ""]
    return out
