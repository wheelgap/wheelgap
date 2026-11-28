"""--fix: for each gap cell (a package on a target, `source` or `missing`, not --ignore'd), the way out, in order of
preference:

1. an upgrade to an official release: the one --suggest names for that target (suggest.suggest: the lowest newer
   release with a live, compatible wheel whose Requires-Python admits the target's Python, no pre-release unless the
   pin is one, and that keeps a wheel on every target the package has one on now);
2. an unofficial gap wheel of the pinned version from wheelgap's gap wheel index (gapwheels.fillable), the fallback;
3. none, with the reason, and the package's issue tracker where wheelgap has it; with --build-info, for a `source`
   cell on a Linux target, also the distro packages its sdist's build requirements prove the build needs
   (build.system_packages).

The releases come from the same lookups as --suggest (cli.release_lookups), so only names origin.known_pypi accepts
are looked up. Like the rest of --fix, nothing here changes a status, the summary or the exit code.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from . import gapwheels
from .build import RUST, BuildInfo, SystemPackages, system_packages
from .check import ABI3_ON_FREETHREADED, Row, Status, marker_value, rejection
from .cutoff import describe, is_duration, parse_cutoff
from .sources import Package, Source
from .suggest import Suggestion
from .targets import Target

UPGRADE, GAP_WHEEL, NONE = "upgrade", "gap_wheel", "none"
# Why a cell has no fix (`reason` in --json).
NO_NEWER = "no_newer_release"  # looked up: no newer release has a wheel for the target (and no gap wheel fits)
LOSES = "loses_wheel"  # every newer release with a wheel for the target drops one the package has elsewhere
NOT_LOOKED_UP = "not_looked_up"  # the releases weren't looked up (see the why line), and no gap wheel fits
NO_BINARY = "no_binary"  # --no-binary: pip uses no wheel for it, so neither an upgrade nor a gap wheel helps
# uv.lock only, instead of LOSES: what the newer releases drop is only another Python's wheel, and uv may lock a
# release per Python (a fork), so `uv lock --upgrade-package NAME` may still fix it. wheelgap can't tell: try it.
UV_MAY_FORK = "uv_may_fork"
# --deps only (#821): a dependency of another package. --deps already took the newest release every requirement on it
# allows, so a newer one with the wheel is outside one of them: the package it comes in through must change instead.
HELD_BACK = "held_back"
# The reasons that mean no fix is known; NOT_LOOKED_UP and UV_MAY_FORK don't, so the summary counts them apart.
NO_FIX = (NO_NEWER, LOSES, NO_BINARY, HELD_BACK)

RESOLVE_NOTE = "wheelgap doesn't resolve dependencies: the rest of them must allow these releases."
# Inputs whose ranged requirement (Package.spec) is the user's own constraint, written in that input.
SPEC_KINDS = frozenset({"requirements", "pyproject", "script", "conda-env", None})


@dataclass(frozen=True)
class Cell:
    row: Row
    target: Target
    source: Source
    kind: str  # UPGRADE, GAP_WHEEL or NONE
    version: str | None = None  # UPGRADE: the release to move to (HELD_BACK: the newer release with the wheel)
    fill: gapwheels.Fill | None = None  # an unofficial gap wheel of the pinned version (for UPGRADE, the fallback)
    reason: str | None = None  # NONE: one of the reasons above
    abi3_only: bool = False  # NONE, on a free-threaded target: the release has abi3 wheels for its platform only
    # LOSES and UV_MAY_FORK: the nearest newer release with a wheel for the target, and the targets it has none for
    drops: tuple[str, tuple[Target, ...]] | None = None
    # LOSES: the project's own requirement that leaves that release out, where it would otherwise be UV_MAY_FORK
    blocked: Constraint | None = None
    # UPGRADE: when the release was uploaded after uv's exclude-newer for the package (#822), the cutoff and the time
    # it must be moved past (the upload of the release's last live file); uv can't pick the release before then.
    cutoff: tuple[datetime, datetime] | None = None
    # HELD_BACK: the packages in the --deps tree whose requirement on it leaves `version` out, with that requirement
    held: tuple[tuple[str, str], ...] = ()
    # NONE with a NO_FIX reason, --build-info: what to install to build it from source on the target's distro
    build: SystemPackages | None = None


@dataclass(frozen=True)
class Constraint:
    """The user's own requirement for an upgrade cell's package, as wheelgap read it, and whether the release fits."""

    file: str
    requirements: tuple[str, ...]  # NAME + specifier; when `allows` is False, only the ones that exclude the release
    allows: bool
    via: str = "requirement"  # or "constraint": a requirements file's -c constraints, which pip applies too


def gap_targets(row: Row, targets: list[Target]) -> list[Target]:
    return [t for t in targets if row.results[t] in (Status.SOURCE, Status.MISSING) and t not in row.ignored]


def abi3_only(row: Row, target: Target) -> bool:
    """Whether a free-threaded target's gap is down to abi3 wheels, by --explain's reason (check.rejection)."""
    pkg = row.package
    return target.freethreaded and any(
        f.endswith(".whl") and f not in pkg.yanked and rejection(f, target) == ABI3_ON_FREETHREADED
        for f in pkg.files or []
    )


def plan(
    gaps: list[Row],
    targets: list[Target],
    source_of: dict[int, Source],
    suggestions: dict[int, Suggestion],
    fills: list[gapwheels.Fill],
    not_looked_up: dict[int, str],
) -> list[Cell]:
    """One Cell per gap cell, in row and target order. `suggestions` and `not_looked_up` (why a package's releases
    weren't looked up) are by id() of the package; `fills` are gapwheels.fillable's."""
    filled = {(id(f.row), f.target): f for f in fills}
    cells = []
    for r in gaps:
        source = source_of[id(r.package)]
        found = suggestions.get(id(r.package))
        for t in gap_targets(r, targets):
            fill = filled.get((id(r), t))
            newer = None if found is None else found.found.get(t) or found.beyond.get(t, (None,))[0]
            if newer is not None and (blockers := held_by(r, t, source, newer)):
                # --deps: another package in the tree requires a range that leaves the upgrade out (#821).
                if fill is not None:
                    cells.append(Cell(r, t, source, GAP_WHEEL, fill=fill))
                else:
                    cells.append(Cell(r, t, source, NONE, version=newer, reason=HELD_BACK, held=blockers))
                continue
            if found is not None and t in found.found:
                cells.append(Cell(r, t, source, UPGRADE, version=found.found[t], fill=fill))
                continue
            if found is not None and t in found.beyond and found.cutoff is not None:  # only past exclude-newer
                version, uploaded = found.beyond[t]
                cells.append(Cell(r, t, source, UPGRADE, version=version, fill=fill, cutoff=(found.cutoff, uploaded)))
                continue
            if fill is not None:
                cells.append(Cell(r, t, source, GAP_WHEEL, fill=fill))
                continue
            if r.package.no_binary:
                reason = NO_BINARY
            elif id(r.package) in not_looked_up or found is None:
                reason = NOT_LOOKED_UP
            elif t in found.loses:
                drops = found.drops.get(t)
                # The release leaves the lost wheels' older Pythons out by Requires-Python: uv may lock the old release
                # there and the new one here (a fork per Python). pip can't, so only for a uv.lock.
                forks = source.kind == "uv" and t in found.python_drops
                blocked = None
                if forks and drops is not None:
                    # Not when the project's own requirement (as wheelgap reads it) leaves that release out: then
                    # uv lock --upgrade-package can't reach it, fork or not.
                    own = constraint(Cell(r, t, source, UPGRADE, version=drops[0]))
                    forks, blocked = own is None or own.allows, own if own is not None and not own.allows else None
                reason = UV_MAY_FORK if forks else LOSES
                cells.append(Cell(r, t, source, NONE, reason=reason, drops=drops, blocked=blocked))
                continue
            else:
                reason = NO_NEWER
            cells.append(Cell(r, t, source, NONE, reason=reason, abi3_only=abi3_only(r, t)))
    return cells


def _buildable(c: Cell) -> bool:
    """A cell with no fix (a NO_FIX reason) whose target builds the release from source."""
    return c.kind == NONE and c.reason in NO_FIX and c.row.results[c.target] == Status.SOURCE


def buildable_rows(cells: list[Cell]) -> list[Row]:
    """The rows with a cell with_build can answer for: --fix --build-info reads only these rows' sdists, not those of
    packages an upgrade or a gap wheel already fixes."""
    return list({id(c.row): c.row for c in cells if _buildable(c)}.values())


def with_build(cells: list[Cell], build_info: dict[int, BuildInfo]) -> list[Cell]:
    """The cells, with `build` set on each one with no fix (a NO_FIX reason) whose target builds the release from
    source, where `build_info` (--build-info's answers, by id() of the package) proves what the build needs."""
    out = []
    for c in cells:
        info = build_info.get(id(c.row.package))
        if info is not None and _buildable(c):
            c = replace(c, build=system_packages(info, c.target))
        out.append(c)
    return out


def held_by(row: Row, target: Target, source: Source, version: str) -> tuple[tuple[str, str], ...]:
    """--deps: the other packages in the resolved tree whose requirement on the row's package leaves `version` out,
    with that requirement (#821). Empty without --deps, and when none does: then what holds it back is a -c constraint,
    the cutoff or the user's own range, which the upgrade line names."""
    if source.deps_imposed is None:
        return ()
    index = next((i for i, p in enumerate(source.packages) if p is row.package), None)
    out = []
    for parent, spec in source.deps_imposed.get(target, {}).get(index, []) if index is not None else []:
        try:
            if spec and not SpecifierSet(spec).contains(Version(version), prereleases=True):
                out.append((parent, f"{row.package.name}{spec}"))
        except (InvalidSpecifier, InvalidVersion):
            continue
    return tuple(dict.fromkeys(out))


def transitive(row: Row, target: Target, source: Source) -> bool:
    """--deps: whether the row's package is reached through another package on the target (not a direct requirement)."""
    return source.deps_paths is not None and len(row.via.get(target, ())) > 1


def upload_cutoff(source: Source, pkg: Package, now: datetime | None = None) -> datetime | None:
    """uv's exclude-newer for a package, which `uv lock --upgrade-package` applies too (#822): from a uv.lock's
    [options] (exclude-newer-package, else exclude-newer-span, a duration counted back from now as uv counts it each
    time it locks, else exclude-newer), or as a pyproject.toml or script sets it (Package.exclude_newer); None for
    other inputs. ValueError, naming the setting, when one uv recorded in a uv.lock can't be read. A duration counts
    back from `now` (default: the current time): pass one time for the whole run."""
    return _cutoff(source, pkg, now)[0]


def cutoff_span(source: Source, pkg: Package) -> str | None:
    """The duration a package's cutoff counts back from now, as written (P850D, "850 days"); None for a fixed time."""
    try:
        return _cutoff(source, pkg)[1]
    except ValueError:
        return None


def _cutoff(source: Source, pkg: Package, now: datetime | None = None) -> tuple[datetime | None, str | None]:
    if source.kind != "uv":
        setting = cutoff_setting(source, pkg.key)
        span = next(
            (
                d[len(setting) + 4 : d.index('" is ')]
                for d in source.exclude_newer_durations
                if d.startswith(f'{setting} = "')
            ),
            None,
        )
        return pkg.exclude_newer, span
    options = source.uv_options
    if options is None:
        return None, None
    recorded = f"that uv recorded in {source.label}"
    if not isinstance(options, dict):
        raise ValueError(f"the [options] {recorded}")
    per = options.get("exclude-newer-package", {})
    if not isinstance(per, dict):
        raise ValueError(f"the exclude-newer-package table {recorded}")
    named: dict[str, tuple[str, object]] = {canonicalize_name(n): (n, v) for n, v in per.items()}
    try:
        if pkg.key in named:
            value = named[pkg.key][1]
            if value is False:
                return None, None
            # uv records a duration as { timestamp = "0001-01-01T00:00:00Z", span = "P850D" }: the span counts.
            if isinstance(value, dict):
                value = value["span"] if "span" in value else value.get("timestamp")
            return parse_cutoff(value, now), value if isinstance(value, str) and is_duration(value) else None
        if (span := options.get("exclude-newer-span")) is not None:
            return parse_cutoff(span, now), span if isinstance(span, str) else None
        if (value := options.get("exclude-newer")) is not None:
            return parse_cutoff(value, now), value if isinstance(value, str) and is_duration(value) else None
    except ValueError:
        what = f"exclude-newer-package for {named[pkg.key][0]}" if pkg.key in named else "exclude-newer"
        raise ValueError(f"the {what} {recorded}") from None
    return None, None


def cutoff_setting(source: Source, key: str) -> str:
    """The uv setting that gives a package its cutoff: exclude-newer-package NAME, or exclude-newer."""
    options = source.uv_options if source.kind == "uv" else None
    per = options.get("exclude-newer-package", {}) if isinstance(options, dict) else source.exclude_newer_package
    named: dict[str, str] = {canonicalize_name(n): n for n in per} if isinstance(per, dict) else {}
    return f"exclude-newer-package {named[key]}" if key in named else "exclude-newer"


def cutoff_text(cell: Cell, code: Callable[[str], str]) -> str:
    """What an upgrade past exclude-newer needs, for its line (empty for one within it). For a duration, which uv
    counts back from the time it locks, the release's upload time only: the cutoff itself moves every run."""
    if cell.cutoff is None:
        return ""
    setting = cutoff_setting(cell.source, cell.row.package.key)
    past = describe(cell.cutoff[1])
    if (span := cutoff_span(cell.source, cell.row.package)) is not None:
        return f"needs uv's {code(setting)} span ({span}) shortened, or set to a time past {past}"
    return f"needs uv's {code(setting)} moved past {past}"


def _uv_specifiers(source: Source, key: str, target: Target) -> list[str] | None:
    """The specifiers a uv.lock's own projects (workspace members: editable or virtual) write for `key` in their
    dependencies and dependency groups, plus the lock's [manifest] constraints, that apply on `target` by their
    markers (#823); None when there are none to go by, when a marker can't be decided there, or when an override
    replaces them (uv then ignores them). An `extra == ...` marker counts as holding: uv locks every extra."""
    manifest = source.uv_manifest if isinstance(source.uv_manifest, dict) else {}
    if any(isinstance(o, dict) and _key(o) == key for o in manifest.get("overrides", [])):
        return None
    entries: list[dict] = []
    for p in source.uv_packages or []:
        src, meta = p.get("source"), p.get("metadata")
        if not (isinstance(src, dict) and ("editable" in src or "virtual" in src) and isinstance(meta, dict)):
            continue
        entries += [d for d in meta.get("requires-dist", []) if isinstance(d, dict)]
        groups = meta.get("requires-dev", {})
        for group in groups.values() if isinstance(groups, dict) else []:
            entries += [d for d in group if isinstance(d, dict)] if isinstance(group, list) else []
    entries += [c for c in manifest.get("constraints", []) if isinstance(c, dict)]
    found = []
    for e in (e for e in entries if _key(e) == key):
        marker = e.get("marker")
        if marker is not None and not isinstance(marker, str):
            return None
        # Each extra the marker names counts as selected (uv locks them all); any other unknown leaves it undecided.
        extras = frozenset(_EXTRA_RE.findall(marker)) if marker else None
        holds = marker_value(marker, target, extras=extras or None)
        if holds is None:
            return None
        if holds:
            found.append(e)
    if not found or any(not isinstance(e.get("specifier", ""), str) for e in found):
        return None
    return [e.get("specifier", "") for e in found]


_EXTRA_RE = re.compile(r"""\bextra\s*==\s*['"]([^'"]+)['"]""")


PRERELEASE_TEXT = "a pre-release"


def prerelease(version: str | None) -> bool:
    """Whether an upgrade's release is a pre-release (suggest names one only for a pre-release pin: one final release
    for every target comes first, then one pre-release for every target, then a split)."""
    try:
        return version is not None and Version(version).is_prerelease
    except InvalidVersion:
        return False


def _key(entry: dict) -> str | None:
    name = entry.get("name")
    return canonicalize_name(name) if isinstance(name, str) else None


def constraint(cell: Cell) -> Constraint | None:
    """The user's own constraint on an upgrade cell's package, only where wheelgap already reads one: a uv.lock's
    project requirements, or a ranged requirement in a requirements file, pyproject.toml, script or the command line.
    None otherwise (a transitive dependency, a Poetry or PDM lock, ...): then nothing is said."""
    pkg, source = cell.row.package, cell.source
    if cell.version is None:
        return None
    if source.kind == "uv" and source.uv_script is None:
        specs = _uv_specifiers(source, pkg.key, cell.target)
        file = source.uv_pyproject.name if source.uv_pyproject is not None else "pyproject.toml"
    elif source.kind == "requirements" and (pinned := source.constraint_specs.get(pkg.key)):
        # pip applies the file's -c constraints too: one there holds the package back whatever its line says.
        specs, file = sorted(pinned), source.label
        reqs_shown = [f"{pkg.name}{s}" for s in specs]
        return _judged(cell, specs, reqs_shown, file, "constraint")
    elif source.deps_paths is not None and (pkg.spec or pkg.written_spec) and source.kind in SPEC_KINDS:
        # --deps: a direct requirement's range, as its input writes it (a dependency's is never the user's own).
        if transitive(cell.row, cell.target, source):
            return None
        specs, file = [pkg.written_spec or pkg.spec or ""], source.label
    elif pkg.spec and source.kind in SPEC_KINDS:
        written = pkg.written_spec
        try:
            same = written is not None and SpecifierSet(written) == SpecifierSet(pkg.spec)
        except InvalidSpecifier:
            same = False
        specs, file = [written if same and written else pkg.spec], source.label
    else:
        return None
    if not specs:
        return None
    specs = [s for s in specs if s]  # a bare name bounds nothing
    return _judged(cell, specs, [f"{pkg.name}{s}" for s in specs], file, "requirement")


def _judged(cell: Cell, specs: list[str], shown: list[str], file: str, via: str) -> Constraint | None:
    """Whether cell's release is within each of specs (shown as `shown`); None when one can't be read."""
    try:
        version = Version(cell.version or "")
        sets = [SpecifierSet(s) for s in specs]
    except (InvalidVersion, InvalidSpecifier):
        return None
    out = [text for text, ss in zip(shown, sets, strict=True) if not ss.contains(version, prereleases=True)]
    if out:
        return Constraint(file, tuple(dict.fromkeys(out)), allows=False, via=via)
    return Constraint(file, tuple(dict.fromkeys(shown)), allows=True, via=via)


def counts(cells: list[Cell]) -> dict[str, int]:
    """--json's fix_summary: gaps is the sum of the others. `none` counts only the gaps with no known fix; a gap whose
    releases weren't looked up (not_looked_up) or that uv may fork (uv_may_fork) is counted apart."""
    return {
        "gaps": len(cells),
        UPGRADE: sum(c.kind == UPGRADE for c in cells),
        GAP_WHEEL: sum(c.kind == GAP_WHEEL for c in cells),
        NONE: sum(c.kind == NONE and c.reason in NO_FIX for c in cells),
        NOT_LOOKED_UP: sum(c.kind == NONE and c.reason == NOT_LOOKED_UP for c in cells),
        UV_MAY_FORK: sum(c.kind == NONE and c.reason == UV_MAY_FORK for c in cells),
    }


def summary_line(cells: list[Cell]) -> str:
    """Only upgrades count as fixed: a gap wheel is unofficial, so it's counted on its own, and so are the gaps that
    weren't checked for an upgrade, which aren't known to have no fix."""
    n = counts(cells)
    packages = len({id(c.row) for c in cells})
    gaps = f"{n['gaps']} gap{'s' * (n['gaps'] != 1)} ({packages} package{'s' * (packages != 1)} × "
    gaps += f"{'its' if packages == 1 else 'their'} targets)"
    line = (
        f"--fix: {gaps}: {n[UPGRADE]} can be fixed by upgrading to an official release; "
        f"{n[GAP_WHEEL]} only by an unofficial gap wheel; "
        f"{n[NONE]} {'has' if n[NONE] == 1 else 'have'} no fix yet"
    )
    if unchecked := n[NOT_LOOKED_UP]:
        line += f"; {unchecked} {'was' if unchecked == 1 else 'were'}n't checked for an upgrade"
    if forks := n[UV_MAY_FORK]:
        line += f"; {forks} may be fixed by an upgrade uv locks per Python"
    return line + "."


def _plain(text: str) -> str:
    return text


def _code(text: str) -> str:
    return f"`{text}`"  # names, versions, specifiers, file labels and commands made of them: no backticks in them


def _by_package(cells: list[Cell]) -> dict[tuple[str, str, str | None], list[Cell]]:
    """Cells grouped by (name as the input writes it, pinned version, upgrade version), in order."""
    grouped: dict[tuple[str, str, str | None], list[Cell]] = {}
    for c in cells:
        grouped.setdefault((c.row.package.name, c.row.package.version or "?", c.version), []).append(c)
    return grouped


def _labels(cells: list[Cell], multi_py: bool) -> str:
    return ", ".join(dict.fromkeys(c.target.label(multi_py) for c in cells))


def _pins(cells: list[Cell]) -> list[str]:
    """NAME==VERSION per upgrade, each once, in order."""
    return list(dict.fromkeys(f"{c.row.package.name}=={c.version}" for c in cells))


def _hashed(cells: list[Cell]) -> bool:
    return any(c.row.package.hashes for c in cells)


_UV_EXPORT_HEADER = "autogenerated by uv via the following command:"
_PDM_HEADER = "@generated by PDM"
_PLAIN_PATH = re.compile(r"[A-Za-z0-9._/+@:~-]+")
_UV_LOCK_OPTIONS = ("--script", "--project", "--directory")  # export options that pick which lock `uv lock` updates


def _export_header(source: Source) -> tuple[str, str] | None:
    """("uv", its `uv export` command) or ("pdm", "") when a requirements file comes from a lock export (#824).

    `uv export` writes "This file is autogenerated by uv via the following command:" with the command on the next
    comment line; `pdm export` writes "This file is @generated by PDM." with no command. Those files are regenerated
    from a lock, not edited or pip-compiled: pip-compile has no .in file to work from, so its command fails.
    `poetry export` writes no header, so it can't be told apart. Only the first lines are read.
    """
    path = source.path
    if path is None:
        return None
    lines: list[str] = []
    try:
        with path.open(encoding="utf-8-sig") as f:  # a BOM (Windows editors) mustn't hide line 1
            for _ in range(10):
                line = f.readline(1000)
                if not line:
                    break
                lines.append(line.strip())
    except (OSError, UnicodeError):
        return None
    comments = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
    for i, text in enumerate(comments):
        if text.endswith(_UV_EXPORT_HEADER) and i + 1 < len(comments) and comments[i + 1].startswith("uv export"):
            return "uv", comments[i + 1].replace("`", "")  # shown inside a Markdown code span
        if text.startswith("This file is " + _PDM_HEADER):
            return "pdm", ""
    return None


def _uv_lock_options(command: str) -> str | None:
    """The --script / --project / --directory options of a `uv export` command, for the matching `uv lock`.

    None when one of their values isn't a plain path (quotes, spaces, `$(...)`): the header is someone else's text,
    and wheelgap mustn't hand it back inside a command of its own to copy and run."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    kept: list[str] = []
    for i, word in enumerate(words):
        name, eq, value = word.partition("=")
        if name not in _UV_LOCK_OPTIONS:
            continue
        if not eq:
            value = words[i + 1] if i + 1 < len(words) else ""
        if not _PLAIN_PATH.fullmatch(value):
            return None
        kept += [word] if eq else [word, value]
    return "".join(f" {w}" for w in kept)


def how(source: Source, cells: list[Cell], code: Callable[[str], str] = _plain) -> list[str]:
    """What to run or change to move `source`'s packages to the releases of these upgrade cells, one line each; first,
    for releases uploaded after uv's exclude-newer, what to move it past (#822)."""
    first = _cutoff_lines(cells, code)
    edits = [c for c in cells if not _no_edit(c)]
    relax = _relax_lines([c for c in cells if _no_edit(c)], code)
    if not (first or relax or edits):  # a --deps dependency nothing seen holds back: pin it in the input
        edits = cells
    return first + relax + (_how(source, edits, code) if edits else [])


def _no_edit(c: Cell) -> bool:
    """Whether the input has nothing to change for this cell beyond what the cutoff and relax lines say: a --deps
    dependency, which has no line of its own, or a pyproject.toml's or script's requirement that already allows the
    release, held back only by the cutoff."""
    if transitive(c.row, c.target, c.source):
        return True
    return (
        c.cutoff is not None
        and c.source.kind in ("pyproject", "script")
        and ((own := constraint(c)) is None or own.allows)
    )


def _relax_lines(cells: list[Cell], code: Callable[[str], str]) -> list[str]:
    """For cells with no line of their own to change, the user's own requirement or -c constraint to relax."""
    out: dict[str, None] = {}
    for c in cells:
        if (own := constraint(c)) is not None and not own.allows and own.requirements:
            reqs = ", ".join(map(code, own.requirements))
            where = f"the -c constraints of {code(own.file)}" if own.via == "constraint" else code(own.file)
            out[f"Relax {reqs} in {where} so it allows {code(f'{c.row.package.name}=={c.version}')}."] = None
    return list(out)


def _cutoff_lines(cells: list[Cell], code: Callable[[str], str]) -> list[str]:
    """One line for the releases uploaded after uv's exclude-newer: each setting to change, and what to (#822)."""
    changes: dict[str, list[datetime]] = {}
    pins: list[str] = []
    for c in cells:
        if c.cutoff is None:
            continue
        pins += [f"{c.row.package.name}=={c.version}"]
        setting = cutoff_setting(c.source, c.row.package.key)
        if (span := cutoff_span(c.source, c.row.package)) is not None:
            # A duration's cutoff moves with every run: name the duration, never the time it gives now.
            change = f"shorten uv's {code(setting)} span ({span}), or set it to a time past {{past}}"
        else:
            change = f"move uv's {code(setting)} ({describe(c.cutoff[0])}) past {{past}}"
        changes.setdefault(change, []).append(c.cutoff[1])
    if not changes:
        return []
    pins = list(dict.fromkeys(pins))
    one = len(pins) == 1
    said = ", and ".join(change.format(past=describe(max(times))) for change, times in changes.items())
    return [
        f"First {said}: {', '.join(map(code, pins))} {'was' if one else 'were'} uploaded after "
        f"{'its' if one else 'their'} cutoff, so uv can't pick {'it' if one else 'them'} before that."
    ]


def _how(source: Source, cells: list[Cell], code: Callable[[str], str] = _plain) -> list[str]:
    pins = _pins(cells)
    options = " ".join(f"--upgrade-package {p}" for p in pins)
    label = code(source.label)
    kind = source.kind
    if kind == "uv":
        # Bare names: uv applies NAME==VERSION to every Python the lock covers, so a release whose Requires-Python
        # leaves out an older one in requires-python makes the lock unsatisfiable; a bare name upgrades each fork.
        bare = " ".join(f"--upgrade-package {n}" for n in dict.fromkeys(c.row.package.name for c in cells))
        check = (
            f": uv moves each to the newest release your constraints allow, so check that it's at least "
            f"{', '.join(map(code, pins))}, then run wheelgap again."
        )
        if source.uv_script is not None:
            return [
                f"For {code(Path(source.uv_script).name)}'s lock, run "
                f"{code(f'uv lock --script {source.uv_script} {bare}')}{check}"
            ]
        folder = str(source.uv_pyproject.parent) if source.uv_pyproject is not None else "."
        where = "the project folder" if folder == "." else code(folder)
        return [f"In {where}, run {code(f'uv lock {bare}')}, then {code('uv sync')}{check}"]
    if kind in ("poetry", "pdm"):
        tool = "poetry" if kind == "poetry" else "pdm"
        update = f"{tool} update {' '.join(dict.fromkeys(c.row.package.name for c in cells))}"
        return [
            f"Run {code(update)} for {label}: it stays within {code('pyproject.toml')}'s constraints and picks the "
            f"newest release they allow, so check that it's at least {', '.join(map(code, pins))} "
            "(widen the constraint if not), then run wheelgap again."
        ]
    if kind == "requirements" and _hashed(cells):
        exported = _export_header(source)
        if exported is not None:
            tool, command = exported
            names = " ".join(dict.fromkeys(c.row.package.name for c in cells))
            check = (
                f"{tool} moves each to the newest release your constraints allow, so check that it's at least "
                f"{', '.join(map(code, pins))} (widen the constraint if not)"
            )
            if tool == "uv":
                bare = " ".join(f"--upgrade-package {n}" for n in dict.fromkeys(c.row.package.name for c in cells))
                lock_options = _uv_lock_options(command)
                run_lock = (
                    code(f"uv lock{lock_options} {bare}")
                    if lock_options is not None
                    else f"{code(f'uv lock {bare}')} with the export's {code('--script')}, {code('--project')} or "
                    f"{code('--directory')} option"
                )
                return [
                    f"{label} pins hashes and was made by {code(command)}, so upgrade its lock instead: run "
                    f"{run_lock}; {check}; then run the {code('uv export')} command at the top of {label} again."
                ]
            if tool == "pdm":
                return [
                    f"{label} pins hashes and was made by {code('pdm export')}, so upgrade its lock instead: run "
                    f"{code(f'pdm update {names}')}; {check}; then run {code('pdm export')} again with the options "
                    "you made it with."
                ]
        return [
            f"{label} pins hashes, so instead of editing the pins to {', '.join(map(code, pins))}, regenerate it: "
            f"{code(f'pip-compile {options}')}, or {code(f'uv pip compile {options}')} with the options you made it "
            "with."
        ]
    if kind == "requirements":
        # The file whose line pins it: a -r include's, not the top file's.
        by_file: dict[str, list[Cell]] = {}
        for c in cells:
            origin = c.row.package.origin
            by_file.setdefault(origin if origin and origin != "--deps" else source.label, []).append(c)
        return [
            f"In {code(f)}, change the {_what(cs)} to {', '.join(map(code, _pins(cs)))}." for f, cs in by_file.items()
        ]
    if kind == "conda-env":
        return [f"In {label}'s {code('pip:')} list, change the pins to {', '.join(map(code, pins))}."]
    if kind == "script":
        return [f"In {label}'s {code('# /// script')} block, change the requirements to {', '.join(map(code, pins))}."]
    if kind is None:  # package names on the command line
        return [f"Use {', '.join(map(code, pins))} instead."]
    if kind == "pyproject":
        return [f"In {label}, change the requirements so they allow {', '.join(map(code, pins))}."]
    # pylock.toml, Pipfile.lock, pixi.lock, a pip report: made by a tool wheelgap doesn't drive.
    return [f"Upgrade to {', '.join(map(code, pins))} with the tool that made {label}, then lock again."]


def _what(cells: list[Cell]) -> str:
    """ "pins", or "requirements" when a line is a range or a bare name (the index picked the version)."""
    # A -c constraint pins it, so the line itself may be a bare name.
    ranged = any(
        c.row.package.spec
        or c.row.package.written_spec
        or c.row.package.version_from_index
        or c.row.package.key in c.source.constraint_specs
        or c.source.deps_paths is not None
        and not _deps_pinned(c.source, c.row.package.key)
        for c in cells
    )
    return "requirements" if ranged else "pins"


def _deps_pinned(source: Source, key: str) -> bool:
    """--deps: whether the input's line for the package pins one release (`==` or `===`, no wildcard); a bare name
    or a range isn't a pin, though --deps resolved it to one."""
    lines = []
    for text in source.specs:
        try:
            req = Requirement(text)
        except InvalidRequirement:
            return False
        if canonicalize_name(req.name) == key:
            lines.append(req)
    return bool(lines) and all(
        len(req.specifier) == 1
        and next(iter(req.specifier)).operator in ("==", "===")
        and "*" not in str(req.specifier)
        for req in lines
    )


def constraint_text(c: Constraint, code: Callable[[str], str] = _plain) -> str:
    if not c.requirements:  # only bare names: nothing to relax
        return f"your {code(c.file)} puts no version bound on it"
    reqs = ", ".join(map(code, c.requirements))
    if c.via == "constraint":  # the requirements file's -c constraints
        where = f"the -c constraints of {code(c.file)}"
        return f"within {where}: {reqs}" if c.allows else f"needs {reqs} in {where} relaxed"
    if c.allows:
        return f"within your {code(c.file)} constraint {reqs}"
    return f"needs {code(c.file)}'s {reqs} relaxed"


def _why_not_looked_up(cells: list[Cell], not_looked_up: dict[int, str]) -> list[tuple[str, list[str]]]:
    """(why, the names) for packages whose releases weren't looked up, in order; reasons come from
    cli.release_lookups."""
    by_why: dict[str, list[str]] = {}
    for c in cells:
        if c.kind == NONE and c.reason == NOT_LOOKED_UP or c.kind == GAP_WHEEL and id(c.row.package) in not_looked_up:
            why = not_looked_up.get(id(c.row.package), "the index gave no answer")
            names = by_why.setdefault(why, [])
            if c.row.package.name not in names:
                names.append(c.row.package.name)
    return list(by_why.items())


REASON_TEXT = {
    NO_NEWER: "no newer release has a wheel",
    LOSES: "every newer release with a wheel drops one it has elsewhere",
    UV_MAY_FORK: "every newer release with a wheel drops one it has on another Python",
    NO_BINARY: "--no-binary, so pip uses no wheel for it",
    HELD_BACK: "another package's requirement on it rules out the newer release with a wheel",
}


def _reason(reason: str, cs: list[Cell], multi_py: bool) -> str:
    text = f"{REASON_TEXT[reason]} for {_labels(cs, multi_py)}"
    drops = list(dict.fromkeys(c.drops for c in cs if c.drops))
    if reason == HELD_BACK:  # the newer release with the wheel, what rules it out, and what brings that in
        newer = ", ".join(dict.fromkeys(c.version for c in cs if c.version))
        held = list(dict.fromkeys(h for c in cs for h in c.held))
        text += f" (its {newer} has the wheel, but " + ", ".join(f"{p} requires {req}" for p, req in held)
        return text + f": upgrade or relax {', '.join(dict.fromkeys(p for p, _ in held))} first)"
    if reason in (LOSES, UV_MAY_FORK) and drops:  # name what the nearest one drops
        lost = "; ".join(
            f"{version} has none for {', '.join(dict.fromkeys(t.label(multi_py) for t in ts))}" for version, ts in drops
        )
        text += f" ({lost})"
    for c in dict.fromkeys(c.blocked for c in cs if c.blocked):  # what would have let uv fork it
        if c is not None and drops:
            text += f" ({c.file}'s {', '.join(c.requirements)} leaves out {drops[0][0]})"
    return text


def _none_line(name: str, pinned: str, cs: list[Cell], multi_py: bool, ask: str | None, code: Callable) -> str:
    reasons = "; ".join(
        _reason(reason, [c for c in cs if c.reason == reason], multi_py)
        for reason in dict.fromkeys(c.reason for c in cs if c.reason)
    )
    line = f"{code(f'{name} {pinned}')}: {reasons}"
    if abi3 := [c for c in cs if c.abi3_only]:
        tags = " or ".join(dict.fromkeys(f"cp{c.target.major}{c.target.minor}t" for c in abi3))
        line += (
            f"; only abi3 wheels for {_labels(abi3, multi_py)}, which free-threaded Python can't use: "
            f"ask {code(name)} for a {tags} wheel"
        )
        return line + (f" at {ask}" if ask else "")
    if forked := [c for c in cs if c.reason == UV_MAY_FORK]:
        source = forked[0].source
        if source.uv_script is not None:  # the script's own lock: uv lock without --script relocks a project
            command = f"uv lock --script {source.uv_script} --upgrade-package {name}"
            where = ""
        else:
            command = f"uv lock --upgrade-package {name}"
            folder = str(source.uv_pyproject.parent) if source.uv_pyproject is not None else "."
            where = " in the project folder" if folder == "." else f" in {code(folder)}"
        line += f"; uv may fork {code(name)} per Python: try {code(command)}{where} and run wheelgap again"
    # Asking upstream helps only where no release has the wheel, not for a wheel dropped elsewhere or --no-binary.
    return line + (f"; ask for a wheel: {ask}" if ask and any(c.reason == NO_NEWER for c in cs) else "")


# What build_packages leaves out, in the text, Markdown and MCP output alike: [build-system] requires name the build
# tools, never the libraries a build links against.
BUILD_TOOLS_ONLY = (
    "these are build tools only: libraries the package links against (e.g. libxml2 and libxslt for lxml) aren't named"
)


def build_text(packages: SystemPackages, labels: str, code: Callable[[str], str] = _plain) -> str:
    """'to build it from source on musl-armv7l, it needs at least a C/C++ compiler: `apk add --no-cache build-base`
    on Alpine (and python3-dev with the distro's own Python); these are build tools only: ...'."""
    text = (
        f"to build it from source on {labels}, it needs at least {' and '.join(packages.needs)}: "
        f"{code(packages.command)} on {packages.distro}"
    )
    extra = []
    if packages.distro_python:
        extra.append(f"and {' '.join(map(code, packages.distro_python))} with the distro's own Python")
    if RUST in packages.needs:
        extra.append("or rustup from https://rustup.rs where the distro's Rust is older than the package needs")
    return text + (f" ({'; '.join(extra)})" if extra else "") + f"; {BUILD_TOOLS_ONLY}"


def _build_lines(cs: list[Cell], multi_py: bool, code: Callable[[str], str]) -> list[str]:
    """One line per set of distro packages among these cells (one package's), for the targets it's for."""
    by: dict[SystemPackages, list[Cell]] = {}
    for c in cs:
        if c.build is not None:
            by.setdefault(c.build, []).append(c)
    return [build_text(packages, _labels(each, multi_py), code) for packages, each in by.items()]


def _lines(
    cells: list[Cell],
    multi_py: bool,
    not_looked_up: dict[int, str],
    issues_urls: dict[int, str | None],
    code: Callable[[str], str],
    item: str,
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """(the upgrade lines, the how lines, the no-fix lines, uv's maybe-fork lines, the not-looked-up lines)."""
    upgrades = [c for c in cells if c.kind == UPGRADE]
    up = []
    for (name, pinned, version), cs in _by_package(upgrades).items():
        # Per target: a requirement's marker (#823), a cutoff or a --deps path can differ between them.
        by_notes: dict[tuple[str, ...], list[Cell]] = {}
        for c in cs:
            by_notes.setdefault(_notes(c, code), []).append(c)
        for notes, group in by_notes.items():
            line = f"{item}{code(f'{name} {pinned} -> {version}')}"
            if prerelease(version):  # only for a pre-release pin, when no one final release fits every target (#825)
                line += f" ({PRERELEASE_TEXT})"
            line += f": {_labels(group, multi_py)}"
            up.append(line + (f" ({'; '.join(notes)})" if notes else ""))
    versions: dict[int, set[str | None]] = {}
    for c in upgrades:
        versions.setdefault(id(c.row), set()).add(c.version)
    # A row whose gap targets got different releases: no one release has wheels for all of them.
    split = list(dict.fromkeys(c.row.package.name for c in upgrades if len(versions[id(c.row)]) > 1))
    ways: list[str] = []
    # A split package's pins aren't in the how line (one file can't pin two releases of it), except for a uv.lock's
    # bare --upgrade-package NAME.
    for cs in _by_source([c for c in upgrades if c.row.package.name not in split or c.source.kind == "uv"]).values():
        ways += how(cs[0].source, cs, code)
    for name in split:
        each = [c for c in upgrades if c.row.package.name == name]
        per = "; ".join(
            f"{code(p)} for {_labels([c for c in each if f'{name}=={c.version}' == p], multi_py)}" for p in _pins(each)
        )
        ways.append(
            f"No one release of {code(name)} has wheels for all of these targets ({per}): pick the one for the "
            "targets you need."
        )
    if upgrades:
        ways.append(RESOLVE_NOTE)
    # Not looked up: the why lines below say so, as they're not known to have no fix; nor are uv's maybe-forks.
    none, forks = (
        [
            line
            for (name, pinned, _), cs in _by_package([c for c in cells if c.kind == NONE and c.reason in which]).items()
            for line in [
                item + _none_line(name, pinned, cs, multi_py, issues_urls.get(id(cs[0].row.package)), code),
                *(f"{' ' * len(item)}  {b}" for b in _build_lines(cs, multi_py, code)),
            ]
        ]
        for which in (NO_FIX, (UV_MAY_FORK,))
    )
    unlooked = [
        f"Newer releases of {', '.join(map(code, names))} weren't looked up: {why}."
        for why, names in _why_not_looked_up(cells, not_looked_up)
    ]
    return up, ways, none, forks, unlooked


def _notes(c: Cell, code: Callable[[str], str]) -> tuple[str, ...]:
    """What an upgrade line says about one cell: the user's own requirement, the cutoff, and for a --deps dependency
    what brings it in (it has no line of its own to change)."""
    notes = [constraint_text(own, code)] if (own := constraint(c)) is not None else []
    if c.cutoff is not None:
        notes.append(cutoff_text(c, code))
    if transitive(c.row, c.target, c.source) and (through := c.row.direct_via.get(c.target)):
        notes.append(f"a dependency of {', '.join(map(code, through))}")
    return tuple(notes)


def _by_source(cells: list[Cell]) -> dict[int, list[Cell]]:
    out: dict[int, list[Cell]] = {}
    for c in cells:
        out.setdefault(id(c.source), []).append(c)
    return out


def any_upgrade(cells: list[Cell]) -> bool:
    return any(c.kind == UPGRADE for c in cells)


def alternatives(cells: list[Cell]) -> set[tuple[int, Target]]:
    """(id(row), target) of the gap wheels that are only the fallback: an upgrade fixes that cell too."""
    return {(id(c.row), c.target) for c in cells if c.kind == UPGRADE and c.fill is not None}


UPGRADES_HEAD = "Upgrade: a newer official release has the wheel:"
MAYBE_HEAD = "Maybe, with uv:"


def text_lines(
    cells: list[Cell], multi_py: bool, not_looked_up: dict[int, str], issues_urls: dict[int, str | None]
) -> list[str]:
    """--fix's summary, upgrades and gaps with no fix, before the gap wheel section (empty when there's no gap)."""
    if not cells:
        return []
    up, ways, none, forks, unlooked = _lines(cells, multi_py, not_looked_up, issues_urls, _plain, "  ")
    out = ["", summary_line(cells)]
    if up:
        out += ["", UPGRADES_HEAD, *up, *ways]
    if forks:
        out += ["", MAYBE_HEAD, *forks]
    if none:
        out += ["", "No fix yet:", *none]
    if unlooked:
        out += ["", *unlooked]
    return out


def markdown_lines(
    cells: list[Cell], multi_py: bool, not_looked_up: dict[int, str], issues_urls: dict[int, str | None]
) -> list[str]:
    """--fix's summary, upgrades and gaps with no fix in the Markdown report (empty when there's no gap)."""
    if not cells:
        return []
    up, ways, none, forks, unlooked = _lines(cells, multi_py, not_looked_up, issues_urls, _code, "- ")
    out = ["#### Fixes", "", summary_line(cells), ""]
    if up:
        out += [UPGRADES_HEAD, "", *up, ""]
        for way in ways:  # each its own paragraph: Markdown would join consecutive lines into one
            out += [way, ""]
    if forks:
        out += [MAYBE_HEAD, "", *forks, ""]
    if none:
        out += ["No fix yet:", "", *none, ""]
    if unlooked:
        out += [*unlooked, ""]
    return out


def _gap_wheel_json(fill: gapwheels.Fill) -> dict[str, object]:
    return {"wheel": fill.wheel.wheel, "url": fill.wheel.url, "sha256": fill.wheel.sha256, "unofficial": True}


def to_json(cell: Cell) -> dict[str, object]:
    """--json --fix: one gap cell's `fix`."""
    if cell.kind == UPGRADE:
        c = constraint(cell)
        return {
            "kind": UPGRADE,
            "version": cell.version,
            "prerelease": prerelease(cell.version),
            "exclude_newer": None
            if cell.cutoff is None
            else {
                "setting": cutoff_setting(cell.source, cell.row.package.key),
                # A duration's cutoff moves every run: its span instead, as written.
                "cutoff": None if (span := cutoff_span(cell.source, cell.row.package)) else describe(cell.cutoff[0]),
                "span": span,
                "move_past": describe(cell.cutoff[1]),
            },
            "how": " ".join(how(cell.source, [cell])),
            "constraint": None
            if c is None
            else {"file": c.file, "requirements": list(c.requirements), "allows": c.allows, "via": c.via},
            "gap_wheel": None if cell.fill is None else _gap_wheel_json(cell.fill),
        }
    if cell.kind == GAP_WHEEL and cell.fill is not None:
        return {"kind": GAP_WHEEL, **_gap_wheel_json(cell.fill)}
    return {
        "kind": NONE,
        "reason": cell.reason,
        "abi3_only": cell.abi3_only,
        "build_packages": None if cell.build is None else build_json(cell.build),
    }


def build_json(packages: SystemPackages) -> dict[str, object]:
    """--json --fix --build-info's (and the MCP's) `build_packages`."""
    return {
        "distro": packages.distro,
        "command": packages.command,
        "packages": list(packages.packages),
        "needs": list(packages.needs),
        "distro_python": list(packages.distro_python),
    }
