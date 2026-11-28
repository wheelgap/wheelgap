"""--github-annotations: GitHub Actions workflow commands that mark each gap on its line in the input file.

The format is GitHub's `::error file=F,line=N,title=T::message` (and `::warning`), escaped the way
@actions/core does it (packages/core/src/command.ts): `%`, CR and LF in the message, and also `:` and `,` in
property values.

Lines are found by reading the input's text again, and checked against what the parser read. Where that can't
pin a package to one line, the annotation names the file without a line: a wrong line is worse than none.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from bisect import bisect_right
from collections.abc import Mapping
from pathlib import Path

from packaging.utils import canonicalize_name

from . import fix, gapwheels
from .check import Row, Status
from .sources import _COMMENT_RE, _NAMED_URL_RE, Package, Source, SourceError, _line_include, requirement_to_package
from .targets import Target

# GitHub shows at most 10 error and 10 warning annotations per step; the rest are dropped.
ANNOTATION_LIMIT = 10

# A location in a file: the path and the 1-based line, or None when the line isn't known for sure.
Location = tuple[Path, int | None]


def escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def workflow_command(
    level: str, message: str, file: str | None = None, line: int | None = None, title: str = ""
) -> str:
    props = [(key, value) for key, value in (("file", file), ("line", line), ("title", title)) if value]
    head = f"::{level}"
    if props:
        head += " " + ",".join(f"{key}={escape_property(str(value))}" for key, value in props)
    return f"{head}::{escape_data(message)}"


def _lines(path: Path) -> list[str] | None:
    """The file's lines as GitHub numbers them (split on LF; CRLF is fine). None if it can't be read, or has a CR
    on its own, which the parsers (reading with universal newlines) would count as a line break and GitHub may not."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    return None if any("\r" in line for line in lines) else lines


_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_HEADER_RE = re.compile(r"\s*\[")
_NAME_RE = re.compile(r"""\s*name\s*=\s*(?:"([^"\\]*)"|'([^']*)')\s*(?:#.*)?""")


def toml_lines(path: Path, table: str) -> dict[tuple[str, str | None], list[int]]:
    """Line of each `name = "..."` in the `[[table]]` blocks of a TOML lockfile, keyed by (canonical name, version).

    Only the block's own keys count (those before its next header), so `[[packages.wheels]]` names are skipped. The
    blocks found must match the parsed array one for one, name for name, or no lines are returned at all.
    """
    lines = _lines(path)
    # A multi-line string can hold text that looks like a header or a name line, so don't try.
    if lines is None or any('"""' in line or "'''" in line for line in lines):
        return {}
    try:
        entries = tomllib.loads("\n".join(lines)).get(table)
    except tomllib.TOMLDecodeError:
        return {}
    if not isinstance(entries, list):
        return {}
    header = re.compile(rf"\s*\[\[\s*{re.escape(table)}\s*\]\]\s*(?:#.*)?")
    found: list[int | None] = []  # per block: the line of its name, if seen
    names: list[str | None] = []
    in_block = False
    for number, line in enumerate(lines, 1):
        if header.fullmatch(line):
            found.append(None)
            names.append(None)
            in_block = True
        elif _HEADER_RE.match(line):
            in_block = False
        elif in_block and found[-1] is None and (m := _NAME_RE.fullmatch(line)):
            found[-1] = number
            names[-1] = m.group(1) if m.group(1) is not None else m.group(2)
    if len(found) != len(entries):
        return {}
    out: dict[tuple[str, str | None], list[int]] = {}
    for entry, name, name_line in zip(entries, names, found, strict=True):
        if not isinstance(entry, dict) or name is None or name_line is None or entry.get("name") != name:
            return {}  # the scan and the parser disagree: no line is safe
        version = entry.get("version")
        out.setdefault((canonicalize_name(name), version if isinstance(version, str) else None), []).append(name_line)
    return out


def pipfile_lines(path: Path) -> dict[tuple[str, str | None], list[int]]:
    """Line of each package key in a Pipfile.lock, keyed by (canonical name, version), in the order the parser reads
    the sections (default, develop, then the others), so the first is the entry it kept."""
    lines = _lines(path)
    if lines is None:
        return {}
    text = "\n".join(lines)
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    starts = [m.start() for m in re.finditer("\n", text)]
    keys: list[tuple[str, str, int]] = []  # (section, name, line) for each key of each top-level object
    # A small JSON walk that records where each key two levels down starts. Each frame on the stack is
    # [kind, key it is the value of, expecting a key, last key read].
    stack: list[list] = []
    pos, n = 0, len(text)
    try:
        while pos < n:
            c = text[pos]
            if c == '"':
                m = _STRING_RE.match(text, pos)
                if m is None:
                    return {}
                value, end = json.loads(m.group()), m.end()
                top = stack[-1] if stack else None
                if top is not None and top[0] == "{" and top[2]:
                    top[2], top[3] = False, value
                    if len(stack) == 2 and stack[0][0] == "{":
                        keys.append((stack[1][1], value, bisect_right(starts, pos) + 1))
                pos = end
                continue
            if c in "{[":
                parent = stack[-1] if stack else None
                stack.append([c, parent[3] if parent is not None and parent[0] == "{" else None, c == "{", None])
            elif c in "}]":
                stack.pop()
            elif c == "," and stack and stack[-1][0] == "{":
                stack[-1][2] = True
            pos += 1
    except (ValueError, IndexError):
        return {}
    sections = [s for s in ("default", "develop") if isinstance(data.get(s), dict)]
    sections += [s for s in data if s not in ("_meta", "default", "develop") and isinstance(data[s], dict)]
    rank = {section: i for i, section in enumerate(sections)}
    keys = [k for k in keys if k[0] in rank]
    walked = {(section, name) for section, name, _ in keys}
    if len(walked) != len(keys) or walked != {(s, name) for s in rank for name in data[s]}:
        return {}  # a duplicate key, or the walk disagrees with json.loads: no line is safe
    out: dict[tuple[str, str | None], list[int]] = {}
    for section, name, number in sorted(keys, key=lambda k: (rank[k[0]], k[2])):
        entry = data[section][name]
        version = entry.get("version") if isinstance(entry, dict) else None
        key = (canonicalize_name(name), version.removeprefix("==") if isinstance(version, str) else None)
        out.setdefault(key, []).append(number)
    return out


# Characters str.splitlines() splits on besides LF (which the parser uses, after reading with universal newlines).
_OTHER_BREAKS = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


_NAME_START_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def requirement_lines(
    path: Path, seen: set[Path] | None = None, constraint_paths: tuple[Path, ...] = ()
) -> list[tuple[Package, Path, int | None]] | None:
    """Each requirement reached through -r/-c, as the parser reads it: the package, the file and the line its name
    is on (None if that isn't clear). A constraints file's own lines aren't requirements. None if a file can't be
    read the same way or an include cycle is hit. A file is read through the folder it was named in, as the parser
    reads it; on a second visit of the same real file its own lines are skipped while its includes are followed
    again from the new folder. `constraint_paths` are the -c files given beside the file (`pip install -r FILE
    -c CONSTRAINTS`): the parser reads them first, each on an include chain of its own, and so does the scan."""
    state: dict[bool, set[Path]] = {False: set() if seen is None else seen, True: set()}
    out: list[tuple[Package, Path, int | None]] = []
    for constraint_path in constraint_paths:
        included = _requirement_lines(constraint_path, state, constraint=True)
        if included is None:
            return None
        out.extend(included)
    main_items = _requirement_lines(path, state)
    if main_items is None:
        return None
    out.extend(main_items)
    return out


def _requirement_lines(
    path: Path,
    seen: dict[bool, set[Path]],
    constraint: bool = False,
    stack: list[Path] | None = None,
) -> list[tuple[Package, Path, int | None]] | None:
    """`seen` is the parser's: the resolved paths already read as requirements (False) and as constraints (True),
    since a file can be read once in each role. `stack` tracks the active chain of named paths so include cycles
    return None rather than recursing forever."""
    path = Path(os.path.abspath(path))
    if stack is None:
        stack = []
    if path in stack:
        return None
    resolved = path.resolve()
    already_seen = resolved in seen[constraint]
    seen[constraint].add(resolved)
    lines = _lines(path)
    if lines is None or any(_OTHER_BREAKS.search(line) for line in lines):
        return None
    stack.append(path)
    out: list[tuple[Package, Path, int | None]] = []
    try:
        number = 0
        while number < len(lines):
            group = [number]  # the physical lines of this logical line
            raw = lines[number]
            while raw.endswith("\\") and number + 1 < len(lines):  # the parser joins "\\\n" continuations
                number += 1
                group.append(number)
                raw = raw[:-1] + " " + lines[number]
            number += 1
            line = _COMMENT_RE.split(raw, 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-"):
                try:  # the -r/-c the parser follows, wherever it sits on the line (`--pre -r base.txt`)
                    include = _line_include(shlex.split(line), path.name)
                except (ValueError, SourceError):
                    return None  # the parser refused this file, so there's nothing to map
                if include:
                    is_constraint, target = include
                    if is_constraint and ("://" in target or "${" in target):
                        continue  # the parser leaves these constraints unread
                    # -r means requirements even inside a constraints file; track each file in both roles.
                    included = _requirement_lines(path.parent / target, seen, is_constraint, stack)
                    if included is None:
                        return None
                    out.extend(included)
                continue  # other options and editables: never a package with a gap
            if constraint or already_seen or line.startswith(("-", ".", "/")):
                continue  # options, constraints, already-read lines, editables and paths: skip
            # The requirement ends before the first word that starts with "-", as the parser ends it (--hash, -f URL).
            words = line.split(" ")
            line = " ".join(
                words[: next((k for k, word in enumerate(words) if word.startswith("-")), len(words))]
            ).strip()
            if "://" in line and not _NAMED_URL_RE.match(line):
                continue
            try:
                package = requirement_to_package(line, path.name)
            except SourceError:
                continue
            if not package.skip:
                # The first physical line with anything on it, if it starts with the package's name.
                texts = [lines[i][:-1] if i != group[-1] else lines[i] for i in group]
                first = next((i for i, text in zip(group, texts, strict=True) if text.strip()), None)
                name = _NAME_START_RE.match(lines[first].strip()) if first is not None else None
                found = (
                    first + 1 if first is not None and name and canonicalize_name(name.group()) == package.key else None
                )
                out.append((package, path, found))
    finally:
        stack.pop()
    return out


class Locator:
    """Where each package of the checked sources is written, read lazily and once per source."""

    def __init__(self, sources: list[Source]):
        self.sources = {id(p): s for s in sources for original in s.packages for p in original.variants}
        self.found: dict[int, dict] = {}

    def locate(self, package: Package) -> Location | None:
        source = self.sources.get(id(package))
        if source is None or source.path is None:
            return None
        if id(source) not in self.found:
            self.found[id(source)] = self._read(source, source.path)
        found = self.found[id(source)]
        if source.kind == "requirements":
            # The package came from a line with its name and markers (constraints change only the version). If
            # there are two such lines, it could be either, so neither is used. Under --deps the resolved
            # package no longer carries the requirement's markers, so only the name decides.
            matches = [
                (f, n)
                for p, f, n in found.get(package.key, [])
                if source.deps_paths is not None or p.markers == package.markers
            ]
            return matches[0] if len(matches) == 1 else (source.path, None)
        numbers = found.get((package.key, package.version), [])
        # pdm.lock and Pipfile.lock keep the first entry of a name and version (sources._add_once); the other
        # lockfiles keep each entry, so two entries of one name and version could be either.
        if len(numbers) == 1 or (numbers and source.kind in ("pdm", "pipfile")):
            return source.path, numbers[0]
        return source.path, None

    def deps_resolved(self, package: Package) -> bool:
        """Whether the package's source is one --deps resolved, so its file's own requirements are its roots."""
        source = self.sources.get(id(package))
        return source is not None and source.deps_paths is not None

    def locate_via(self, package: Package, name: str) -> Location | None:
        """The line of the requirement `name` that pulls `package` into the tree --deps resolved: the file's own
        requirement with that name, when there's exactly one. None otherwise."""
        source = self.sources.get(id(package))
        if source is None or source.path is None or source.deps_paths is None or source.kind != "requirements":
            return None
        if id(source) not in self.found:
            self.found[id(source)] = self._read(source, source.path)
        matches = self.found[id(source)].get(canonicalize_name(name), [])
        return (matches[0][1], matches[0][2]) if len(matches) == 1 else None

    @staticmethod
    def _read(source: Source, path: Path) -> dict:
        if source.kind == "requirements":
            scanned = requirement_lines(path, constraint_paths=source.constraint_paths)
            if scanned is None:
                return {}
            # The scan must find the same requirements as the parser, in the same order, or no line is trusted.
            # With --deps, source.packages is the resolved tree; the lines still answer for the file's own
            # requirements, which source.specs holds exactly as the parser read them.
            if source.deps_paths is not None:
                try:
                    expected = [
                        (p.key, p.markers)
                        for spec in source.specs
                        if not (p := requirement_to_package(spec, path.name)).skip
                    ]
                except SourceError:
                    return {}  # a spec the scan and the parser can't agree on: no line is safe
            else:
                expected = [(p.key, p.markers) for p in source.packages if not p.skip]
            if [(p.key, p.markers) for p, _, _ in scanned] != expected:
                return {}
            by_name: dict[str, list[tuple[Package, Path, int | None]]] = {}
            for package, file, number in scanned:
                by_name.setdefault(package.key, []).append((package, file, number))
            return by_name
        if source.kind == "pipfile":
            return pipfile_lines(path)
        if source.kind == "pylock":
            return toml_lines(path, "packages")
        if source.kind in ("uv", "poetry", "pdm"):
            return toml_lines(path, "package")
        return {}  # pyproject.toml: the file, but no line


def relative_path(path: Path, root_env: str = "GITHUB_WORKSPACE") -> str | None:
    """The path GitHub expects: relative to the repository (the `root_env` variable, GITHUB_WORKSPACE by default,
    else the current directory), with forward slashes. None for a file outside it, which GitHub couldn't show in the
    diff. Links are resolved: git keeps a link as a link, so only the file it points to has lines to show."""
    base = os.environ.get(root_env) or os.getcwd()
    try:
        return path.resolve().relative_to(Path(base).resolve()).as_posix()
    except (OSError, ValueError):
        return None


GAP_WORDS = {
    Status.MISSING: ("can't install", "can't install on"),
    Status.UNKNOWN: ("couldn't be checked", "couldn't be checked on"),
    Status.SOURCE: ("builds from source", "builds from source on"),
}


def upgrade_sentence(cells: list[fix.Cell], multi_py: bool) -> str:
    """--fix: what a package's annotation says first when a newer official release has the wheel for some gaps."""
    by_version: dict[str, list[str]] = {}
    for c in cells:
        pre = " (a pre-release)" if fix.prerelease(c.version) else ""
        by_version.setdefault(f"{c.row.package.name} {c.version}{pre}", []).append(c.target.label(multi_py))
    has = "; ".join(f"{v} has the wheel for {', '.join(dict.fromkeys(labels))}" for v, labels in by_version.items())
    return f" Upgrade: {has} (an official release); the --fix section of the log says how."


def gap_wheel_sentence(fills: list[gapwheels.Fill], multi_py: bool, fallback: bool = False) -> str:
    """--fix: what a package's annotation says when a gap wheel fills some of its gaps. `fills`: all of that
    package's, which come from one input. The pip command for an input pip installs from as it is; a lockfile or a
    script is pointed to the log's gap wheel section, as in gapwheels.explain_lines."""
    on = ", ".join(dict.fromkeys(f.target.label(multi_py) for f in fills))
    said = f" A gap wheel on wheelgap's gap wheel index fills {on} (unofficial, built by wheelgap from the PyPI sdist)"
    said = f" If you can't upgrade:{said[:2].lower()}{said[2:]}" if fallback else said
    if command := gapwheels.pip_install(fills[0]):
        return f"{said}. Install it with: {command}"
    return f"{said}; see the gap wheel section of the log for how to use it."


def annotation(
    row: Row,
    targets: list[Target],
    level: str,
    locator: Locator,
    advisory: frozenset[Target] = frozenset(),
    fills: list[gapwheels.Fill] | None = None,
    *,
    installed_by: str | None = None,
    installed_through: Mapping[Path, str] | None = None,
    upgrades: list[fix.Cell] | None = None,
) -> str:
    multi_py = len({t.python for t in targets}) > 1
    by_status: dict[Status, list[str]] = {}
    for t in targets:
        if t not in row.ignored and (status := row.results[t]) in GAP_WORDS:
            by_status.setdefault(status, []).append(t.label(multi_py) + (" (advisory)" if t in advisory else ""))
    worst = next(s for s in GAP_WORDS if s in by_status)
    pkg = row.package
    name = f"{pkg.name} {pkg.version}" if pkg.version else pkg.name
    parts = [f"{GAP_WORDS[s][1]} {', '.join(by_status[s])}" for s in GAP_WORDS if s in by_status]
    # --deps: a package the resolved tree pulls in has no line of its own; the direct requirement that pulls it
    # in does, when exactly one leads to it on every gap target. Name it in the message either way.
    direct: dict[str, str] = {}
    gap_targets = [t for t in targets if t not in row.ignored and row.results[t] in GAP_WORDS]
    deps_resolved = locator.deps_resolved(pkg)
    # Under --deps, a package is one of the file's own requirements on a target when its path there is just its
    # name; otherwise the tree pulls it in. Mixed, or unknown (no path), means no line can be trusted.
    rooted = [len(row.via.get(t, ())) == 1 for t in gap_targets]
    if deps_resolved:
        for t in gap_targets:
            for n in row.direct_via.get(t, ()):
                direct.setdefault(canonicalize_name(n), n)
    names = list(direct.values())
    location = locator.locate(pkg)
    if deps_resolved and location is not None and not (gap_targets and all(rooted)):
        # Not the file's own requirement on every gap target, so its own line (if it has one, say with a marker
        # that excludes these targets) isn't where it comes from. Exactly one requirement pulling it in on every
        # gap target gives that requirement's line; more than one, a mix, or an uncertain line stays file-only —
        # a wrong line is worse than none.
        transitive = not any(rooted) and all(row.direct_via.get(t) for t in gap_targets)
        via = locator.locate_via(pkg, names[0]) if transitive and len(names) == 1 else None
        location = via if via is not None else (location[0], None)
    message = f"{name} {'; '.join(parts)}"
    if names:
        message += f" (via {', '.join(names)}{'' if not any(rooted) else ' on some targets'})"
    if installed_by is not None:
        # installed_through: each file the RUN line reads through an include (resolved), and the file on the line
        # that leads to it. The file on the line itself isn't there, so its own packages say no more.
        top = installed_through.get(location[0].resolve()) if installed_through and location is not None else None
        through = f", through {top}" if top is not None else ""
        message += f" (installed by {installed_by}{through})"
    message += "."
    if pkg.error:
        message += f" Lookup error: {pkg.error}."
    if level == "warning":
        required = [s for t, s in row.results.items() if t not in row.ignored and t not in advisory]
        if Status.SOURCE in required:
            message += " Source builds are allowed (fail-on missing), so this doesn't fail the run."
        else:
            message += " Gaps on advisory targets don't fail the run."
    file = line = None
    if location is not None:
        file = relative_path(location[0])
        line = location[1] if file is not None else None
        if file is None:
            message += f" (in {location[0]})"
    if upgrades:  # --fix: the official release first, as in the log's --fix section
        message += upgrade_sentence(upgrades, multi_py)
    if fills:  # --fix: a gap wheel never changes the level, the title or the line
        # Last, after "(in FILE)", so the pip command ends the annotation and copies cleanly (#609).
        message += gap_wheel_sentence(fills, multi_py, fallback=bool(upgrades))
    return workflow_command(level, message, file, line, f"{name}: {GAP_WORDS[worst][0]}")


def github_annotations(
    rows: list[Row],
    targets: list[Target],
    sources: list[Source],
    failing: list[Row],
    advisory: frozenset[Target] = frozenset(),
    fills: list[gapwheels.Fill] | None = None,
    *,
    installed_by: Mapping[int, str] | None = None,
    installed_through: Mapping[int, Mapping[Path, str]] | None = None,
    fix_cells: list[fix.Cell] | None = None,
) -> list[str]:
    """One ::error per failing package and one ::warning per package whose gaps don't fail the run (source builds
    that --fail-on missing allows, or gaps only on --advisory targets), at most ANNOTATION_LIMIT of each: past that,
    the last one says how many more there are. `fills`: --fix's, so a package a gap wheel fills says so (None or
    empty: every annotation is as without --fix)."""
    filled: dict[int, list[gapwheels.Fill]] = {}
    for f in fills or []:
        filled.setdefault(id(f.row), []).append(f)
    upgraded: dict[int, list[fix.Cell]] = {}
    for c in fix_cells or []:
        if c.kind == fix.UPGRADE:
            upgraded.setdefault(id(c.row), []).append(c)
    failing_ids = {id(r) for r in failing}
    locator = Locator(sources)
    out = []
    for level in ("error", "warning"):
        chosen = [r for r in rows if not r.ok and (id(r) in failing_ids) == (level == "error")]
        shown = chosen if len(chosen) <= ANNOTATION_LIMIT else chosen[: ANNOTATION_LIMIT - 1]
        out += [
            annotation(
                r,
                targets,
                level,
                locator,
                advisory,
                filled.get(id(r)),
                installed_by=installed_by.get(id(r.package)) if installed_by is not None else None,
                installed_through=installed_through.get(id(r.package)) if installed_through is not None else None,
                upgrades=upgraded.get(id(r)),
            )
            for r in shown
        ]
        if more := len(chosen) - len(shown):
            what = f"{more} more packages with gaps"  # never 1: a single extra would fit in the limit
            out.append(workflow_command(level, f"{what}; the log lists them all.", title=f"wheelgap: {what}"))
    return out
