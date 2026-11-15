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
import tomllib
from bisect import bisect_right
from pathlib import Path

from packaging.utils import canonicalize_name

from .check import Row, Status
from .sources import _COMMENT_RE, _INCLUDE_RE, _NAMED_URL_RE, Package, Source, SourceError, requirement_to_package
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


def requirement_lines(path: Path, seen: set[Path] | None = None) -> list[tuple[Package, Path, int | None]] | None:
    """Each requirement of a requirements file and the files it includes with -r, as the parser reads them: the
    package, the file and the line its name is on (None if that isn't clear). None if a file can't be read the
    same way."""
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen:
        return []
    seen.add(path)
    lines = _lines(path)
    if lines is None or any(_OTHER_BREAKS.search(line) for line in lines):
        return None
    out: list[tuple[Package, Path, int | None]] = []
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
        if m := _INCLUDE_RE.match(line):
            included = requirement_lines(path.parent / m.group(1).strip(), seen)
            if included is None:
                return None
            out.extend(included)
            continue
        if line.startswith(("-", ".", "/")):
            continue  # options, constraints, editables and paths: never a package with a gap
        tokens = line.split()
        line = " ".join(
            t for k, t in enumerate(tokens) if not t.startswith("--hash") and (k == 0 or tokens[k - 1] != "--hash")
        )
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
            found = first + 1 if first is not None and name and canonicalize_name(name.group()) == package.key else None
            out.append((package, path, found))
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
            # there are two such lines, it could be either, so neither is used.
            matches = [(f, n) for p, f, n in found.get(package.key, []) if p.markers == package.markers]
            return matches[0] if len(matches) == 1 else (source.path, None)
        numbers = found.get((package.key, package.version), [])
        # pdm.lock and Pipfile.lock keep the first entry of a name and version (sources._add_once); the other
        # lockfiles keep each entry, so two entries of one name and version could be either.
        if len(numbers) == 1 or (numbers and source.kind in ("pdm", "pipfile")):
            return source.path, numbers[0]
        return source.path, None

    @staticmethod
    def _read(source: Source, path: Path) -> dict:
        if source.kind == "requirements":
            scanned = requirement_lines(path)
            # The scan must find the same requirements as the parser, in the same order, or no line is trusted.
            if scanned is None or [(p.key, p.markers) for p, _, _ in scanned] != [
                (p.key, p.markers) for p in source.packages if not p.skip
            ]:
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


def relative_path(path: Path) -> str | None:
    """The path GitHub expects: relative to the repository (GITHUB_WORKSPACE, else the current directory), with
    forward slashes. None for a file outside it, which GitHub couldn't show in the diff."""
    base = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
    try:
        return path.resolve().relative_to(Path(base).resolve()).as_posix()
    except (OSError, ValueError):
        return None


GAP_WORDS = {
    Status.MISSING: ("can't install", "can't install on"),
    Status.UNKNOWN: ("couldn't be checked", "couldn't be checked on"),
    Status.SOURCE: ("builds from source", "builds from source on"),
}


def annotation(
    row: Row, targets: list[Target], level: str, locator: Locator, advisory: frozenset[Target] = frozenset()
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
    message = f"{name} {'; '.join(parts)}."
    if pkg.error:
        message += f" Lookup error: {pkg.error}."
    if level == "warning":
        required = [s for t, s in row.results.items() if t not in row.ignored and t not in advisory]
        if Status.SOURCE in required:
            message += " Source builds are allowed (fail-on missing), so this doesn't fail the run."
        else:
            message += " Gaps on advisory targets don't fail the run."
    location = locator.locate(pkg)
    file = line = None
    if location is not None:
        file = relative_path(location[0])
        line = location[1] if file is not None else None
        if file is None:
            message += f" (in {location[0]})"
    return workflow_command(level, message, file, line, f"{name}: {GAP_WORDS[worst][0]}")


def github_annotations(
    rows: list[Row],
    targets: list[Target],
    sources: list[Source],
    failing: list[Row],
    advisory: frozenset[Target] = frozenset(),
) -> list[str]:
    """One ::error per failing package and one ::warning per package whose gaps don't fail the run (source builds
    that --fail-on missing allows, or gaps only on --advisory targets), at most ANNOTATION_LIMIT of each: past that,
    the last one says how many more there are."""
    failing_ids = {id(r) for r in failing}
    locator = Locator(sources)
    out = []
    for level in ("error", "warning"):
        chosen = [r for r in rows if not r.ok and (id(r) in failing_ids) == (level == "error")]
        shown = chosen if len(chosen) <= ANNOTATION_LIMIT else chosen[: ANNOTATION_LIMIT - 1]
        out += [annotation(r, targets, level, locator, advisory) for r in shown]
        if more := len(chosen) - len(shown):
            what = f"{more} more packages with gaps"  # never 1: a single extra would fit in the limit
            out.append(workflow_command(level, f"{what}; the log lists them all.", title=f"wheelgap: {what}"))
    return out
