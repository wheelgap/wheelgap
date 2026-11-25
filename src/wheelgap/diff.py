"""Compare two lockfiles, or two pinned releases with --deps: which packages got worse, and which new ones have gaps.

    wheelgap diff old/uv.lock uv.lock -p 3.13 -t linux-aarch64,musl-aarch64
    python -m wheelgap.diff old/uv.lock uv.lock -p 3.13 -t linux-aarch64 --json

Both sides are checked with `check_lockfile()` and packages are matched by canonical name. Exit 1 if
anything got worse (a new package with a gap counts), 0 otherwise, and 2 on bad input.

How statuses rank, per target:

- `wheel`, `bundled` and `n/a` are all fine and rank the same; `source` is worse than them, and `missing`
  is worse than `source`.
- `unknown` (the package couldn't be checked) isn't on that scale, because it says nothing about what pip
  would do. So a change is never called "not worse" on a guess: anything → `unknown` is worse, and
  `unknown` → `source` or `missing` is worse too. Only `unknown` → `wheel`, `bundled` or `n/a` is not.
  `unknown` on both sides counts as worse if the version changed; at the same version it's unchanged, and a
  note on stderr names those packages.
- A package installed from somewhere wheelgap doesn't check (git, a path, a URL, an editable install or a
  private index) is "skipped" on that target and treated like `unknown`: anything → skipped is worse, and
  skipped → `source` or `missing` is too. Skipped on both sides is unchanged. A new skipped package counts as
  a new gap, except a uv `virtual` project, which installs nothing. This is decided per target, so a uv.lock
  fork or marker split that is checked on one target and skipped on another is compared target by target.
- A requirements-file path with no name (`./pkg`, or `-e ./pkg` without `#egg=`) is matched by its path, not
  a package name: `ranged==1.5` → `./ranged` shows `./ranged` as a new gap (still exit 1), not as `ranged`.
- A package listed more than once (a uv.lock fork) takes its worst status on each target, with `unknown`
  beating `missing`, and ignoring `n/a` unless every entry is `n/a`.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidName, canonicalize_name

from . import __version__, fix, gapwheels, origin, suggest
from .api import Result, _check_lockfile, check_packages, explain_issues_urls, make_targets, running_python
from .check import CELL, Row, Status, applies, lock_environments_note, narrow_to_lock_environments, reachable
from .cli import (
    find_pyproject,
    gap_wheel_fills,
    libc_versions,
    load_config,
    load_uv_targets,
    project_dir,
    read_python_version,
    release_lookups,
)
from .logsafe import guarded_output
from .pypi import check_index_url
from .sources import Source, SourceError, file_kind, find_lockfile, parse_uv_lock, tool_table
from .targets import (
    DEFAULT_ANDROID_API,
    DEFAULT_GLIBC,
    DEFAULT_IOS,
    DEFAULT_MACOS,
    DEFAULT_MUSL,
    Target,
    TargetError,
    next_python_note,
)

# Higher is worse. UNKNOWN only orders entries of the same package on one target (see the module docstring).
RANK = {
    Status.WHEEL: 0,
    Status.BUNDLED: 0,
    Status.NOT_NEEDED: 0,
    Status.SOURCE: 1,
    Status.MISSING: 2,
    Status.UNKNOWN: 3,
}
GAPS = (Status.SOURCE, Status.MISSING, Status.UNKNOWN)


@dataclass
class Package:
    """One package name on one side: its version(s), its worst status per target, and the targets that
    install it from outside the index (git, a path, a private index...), which aren't checked."""

    name: str
    versions: list[str]
    statuses: dict[Target, Status]
    skips: dict[Target, str] = field(default_factory=dict)
    # Per target, the direct dependencies that bring the package in (from the entries with the winning status).
    direct: dict[Target, tuple[str, ...]] = field(default_factory=dict)

    @property
    def version(self) -> str:
        if self.versions:
            return ", ".join(self.versions)
        return f"({', '.join(dict.fromkeys(self.skips.values()))})" if self.skips else "?"

    def unchecked(self, t: Target) -> bool:
        return t in self.skips or self.statuses[t] is Status.UNKNOWN

    def cell(self, t: Target) -> str:
        return f"skipped ({self.skips[t]})" if t in self.skips else CELL[self.statuses[t]][0]

    def status(self, t: Target) -> str:
        """The status for --json: a Status value, or "skipped" (the reason is in skip_reason)."""
        return "skipped" if t in self.skips else self.statuses[t].value


def merge(results: list[Result], targets: list[Target]) -> dict[str, Package]:
    """The results keyed by canonical name, with entries for the same name combined."""
    grouped: dict[str, list[Result]] = {}
    for r in results:
        grouped.setdefault(canonicalize_name(r.name), []).append(r)
    merged = {}
    for key, rs in grouped.items():
        statuses = {}
        for t in targets:
            found = [r.statuses.get(t, Status.NOT_NEEDED) for r in rs]
            needed = [s for s in found if s is not Status.NOT_NEEDED]
            statuses[t] = max(needed, key=RANK.__getitem__) if needed else Status.NOT_NEEDED
        versions = list(dict.fromkeys(r.version for r in rs if r.version))
        direct: dict[Target, tuple[str, ...]] = {}
        for t in targets:
            # A uv fork can have multiple versions: retain the root(s) for the
            # status that won on this target rather than an unrelated fork.
            roots = dict.fromkeys(
                name for r in rs if r.statuses.get(t, Status.NOT_NEEDED) is statuses[t] for name in r.direct.get(t, ())
            )
            if roots:
                direct[t] = tuple(roots)
        merged[key] = Package(rs[0].name, versions, statuses, direct=direct)
    return merged


def add_skipped(packages: dict[str, Package], source: Source, targets: list[Target]) -> None:
    """Add the source's skipped packages on the targets that install them, as check() decides that."""
    installed = {t: reachable(source, t) for t in targets} if source.edges is not None else None
    for i, pkg in enumerate(source.packages):
        if not pkg.skip:
            continue
        on = [t for t in targets if (i in installed[t] if installed is not None else applies(pkg, t))]
        if not on:
            continue
        entry = packages.setdefault(pkg.key, Package(pkg.name, [], dict.fromkeys(targets, Status.NOT_NEEDED)))
        entry.skips.update(dict.fromkeys(on, pkg.skip))
        if pkg.version and pkg.version not in entry.versions:
            entry.versions.append(pkg.version)


def worse(old: Status, new: Status) -> bool:
    if new is Status.UNKNOWN:
        return old is not Status.UNKNOWN
    if old is Status.UNKNOWN:
        return new in GAPS
    return RANK[new] > RANK[old]


def worse_on(was: Package, now: Package, t: Target) -> bool:
    if t in now.skips:
        return t not in was.skips  # skipped on both sides: neither was checked, so nothing changed
    if t in was.skips:
        return now.statuses[t] in GAPS  # like unknown → a status
    if was.statuses[t] is Status.UNKNOWN and now.statuses[t] is Status.UNKNOWN:
        return was.version != now.version  # a new release may have lost a wheel
    return worse(was.statuses[t], now.statuses[t])


def new_gap(pkg: Package, t: Target) -> bool:
    if t in pkg.skips:
        return pkg.skips[t] != "virtual"  # a uv virtual project installs nothing; anything else isn't checked
    return pkg.statuses[t] in GAPS


@dataclass
class Report:
    """What compare() found, for the text report and --json."""

    lines: list[str] = field(default_factory=list)
    worse: list[dict] = field(default_factory=list)
    new: list[dict] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)


def _side(pkg: Package, t: Target, prefix: str = "") -> dict[str, str]:
    out = {f"{prefix}status": pkg.status(t)}
    if t in pkg.skips:
        out[f"{prefix}skip_reason"] = pkg.skips[t]
    return out


def _direct(pkg: Package, targets: list[Target], multi_py: bool) -> dict[str, list[str]]:
    """The direct dependencies that bring the package in, per changed target (empty for a direct dependency)."""
    return {t.label(multi_py): list(pkg.direct[t]) for t in targets if pkg.direct.get(t)}


def _with_direct(pkg: Package, targets: list[Target]) -> str:
    """The name with the same `(via parent +N)` hint as the main command's table, spelled as the lockfile has it."""
    roots: dict[str, str] = {}
    for t in targets:
        for name in pkg.direct.get(t, ()):
            roots.setdefault(canonicalize_name(name), name)
    if not roots:
        return pkg.name
    first = next(iter(roots.values()))
    extra = f" +{len(roots) - 1}" if len(roots) > 1 else ""
    return f"{pkg.name} (via {first}{extra})"


def compare(before: dict[str, Package], after: dict[str, Package], targets: list[Target]) -> Report:
    """Packages that got worse and new packages with gaps, with the names that couldn't be checked at the
    same version on either side."""
    multi_py = len({t.python for t in targets}) > 1
    report = Report()
    for key, pkg in after.items():
        if key not in before:
            gaps = [t for t in targets if new_gap(pkg, t)]
            if gaps:
                report.lines.append(f"{_with_direct(pkg, gaps)} {pkg.version} (new)")
                report.lines += [f"  {t.label(multi_py)}: {pkg.cell(t)}" for t in gaps]
                added: dict = {
                    "name": pkg.name,
                    "versions": pkg.versions,
                    "targets": {t.label(multi_py): _side(pkg, t) for t in gaps},
                }
                if direct := _direct(pkg, gaps, multi_py):
                    added["direct"] = direct
                report.new.append(added)
            continue
        was = before[key]
        changed = [t for t in targets if worse_on(was, pkg, t)]
        if any(
            was.statuses[t] is Status.UNKNOWN and pkg.statuses[t] is Status.UNKNOWN and t not in changed
            for t in targets
        ):
            report.unchecked.append(pkg.name)
        if changed:
            version = pkg.version if pkg.version == was.version else f"{was.version} → {pkg.version}"
            report.lines.append(f"{_with_direct(pkg, changed)} {version}")
            report.lines += [f"  {t.label(multi_py)}: {was.cell(t)} → {pkg.cell(t)}" for t in changed]
            worsened: dict = {
                "name": pkg.name,
                "old_versions": was.versions,
                "new_versions": pkg.versions,
                "targets": {t.label(multi_py): _side(was, t, "old_") | _side(pkg, t, "new_") for t in changed},
            }
            if direct := _direct(pkg, changed, multi_py):
                worsened["direct"] = direct
            report.worse.append(worsened)
    return report


def json_report(report: Report, targets: list[Target], fixes: Fixes | None = None) -> str:
    """--json: one object. Its keys are a public interface, like the main command's --json. Versions are
    lists: empty when the input gives no version (an unpinned or unknown package, or a requirements line
    installing from git, a path or a URL); several for a uv.lock fork. With --fix, `fix_summary` counts the
    listed gap cells as the main command's does (the `fix` entries are in `worse` and `new`)."""
    multi_py = len({t.python for t in targets}) > 1
    out: dict[str, object] = {
        "version": __version__,
        "targets": [{"label": t.label(multi_py), "python": t.python, "platform": t.platform} for t in targets],
        "worse": report.worse,
        "new": report.new,
        "unchecked": report.unchecked,
        "summary": {"worse": len(report.worse), "new": len(report.new)},
    }
    if fixes is not None:
        out["fix_summary"] = fix.counts(fixes.cells)
    return json.dumps(out, indent=2)


def add_comment_suggestions(report: Report, rows: list[Row], sources: list[Source], targets: list[Target]) -> None:
    """Add the check's release suggestions for worsened wheels, using the actual head lockfile's provenance."""
    if not report.worse or origin.other_index() is not None:
        return
    private = origin.private_names(sources)
    worse: dict[str, dict] = {canonicalize_name(entry["name"]): entry for entry in report.worse}
    multi_py = len({t.python for t in targets}) > 1
    wanted: dict[str, list[tuple[str, list[Target], list[Target]]]] = {}
    for row in rows:
        pkg = row.package
        entry = worse.get(pkg.key)
        if entry is None or not pkg.version or pkg.no_binary:
            continue
        changed = entry["targets"]
        gaps = [
            t
            for t, status in row.results.items()
            if status in (Status.SOURCE, Status.MISSING)
            and changed.get(t.label(multi_py), {}).get("new_status") == status.value
        ]
        if not gaps or not origin.known_pypi(pkg, sources[-1], private=private):
            continue
        try:
            key = canonicalize_name(pkg.name, validate=True)
        except InvalidName:
            continue
        keep = [t for t, status in row.results.items() if status is Status.WHEEL]
        wanted.setdefault(key, []).append((pkg.version, gaps, keep))

    found, _ = suggest.look_up_all({name: requests for name, requests in wanted.items()})
    hints = {}
    for name, answers in found.items():
        entry = worse[name]
        lines = []
        for (version, gaps, _), answer in zip(wanted[name], answers, strict=True):
            actionable = [t for t in gaps if t in answer.found or t in answer.last]
            lines.extend(suggest.suggest_lines(entry["name"], version, actionable, answer, multi_py))
        if lines:
            hints[name] = list(dict.fromkeys(lines))
    for name, lines in hints.items():
        worse[name]["suggestions"] = lines


@dataclass
class Fixes:
    """--fix: the way out of each gap cell the report lists, as `wheelgap NEW --fix` plans it, and what the text
    report needs to show it."""

    cells: list[fix.Cell]
    fills: list[gapwheels.Fill]
    not_looked_up: dict[int, str]
    issues_urls: dict[int, str | None]


def add_fixes(report: Report, rows: list[Row], sources: list[Source], targets: list[Target]) -> Fixes:
    """Add `fix` (fix.to_json, as `wheelgap NEW --fix --json` has it) to each target entry of `worse` and `new` that
    is a `source` or `missing` gap, and return the cells. The lookups and the plan are --fix's own (cli.release_lookups,
    cli.gap_wheel_fills, fix.plan) on the NEW lockfile's rows, for the packages the report lists only, with names
    judged by the NEW input alone, as `wheelgap NEW --fix` judges them."""
    multi_py = len({t.python for t in targets}) > 1
    entries = {canonicalize_name(e["name"]): e for e in report.worse + report.new}
    listed = [r for r in rows if not r.ok and canonicalize_name(r.package.name) in entries]
    if not listed:
        return Fixes([], [], {}, {})
    # The rows are the NEW input's, so only it decides which names are PyPI's, as with `wheelgap NEW --fix`: the OLD
    # side is an earlier copy of the same file, not another input of the run, and a REF:PATH copy (always pypi_only
    # False) would otherwise keep every name it shares with NEW in, though NEW places it on PyPI.
    new_side = sources[-1:]
    fills = gap_wheel_fills(listed, targets, new_side, None, "--index-url", False)
    upgrades, not_looked_up = release_lookups(listed, targets, new_side, None, False, flag="--fix")
    source_of = {id(p): s for s in sources for original in s.packages for p in original.variants}
    cells = []
    for cell in fix.plan(listed, targets, source_of, upgrades, fills, not_looked_up):
        side = entries[canonicalize_name(cell.row.package.name)]["targets"].get(cell.target.label(multi_py))
        # Only the cells the report lists, from the entry whose status it shows (a uv.lock fork has several).
        if side is None or "fix" in side:
            continue
        if side.get("new_status", side.get("status")) != cell.row.results[cell.target].value:
            continue
        side["fix"] = fix.to_json(cell)
        cells.append(cell)
    shown = {(id(c.row), c.target) for c in cells}
    fills = [f for f in fills if (id(f.row), f.target) in shown]
    return Fixes(cells, fills, not_looked_up, explain_issues_urls([c.row for c in cells], new_side))


def fix_text(fixes: Fixes, targets: list[Target]) -> list[str]:
    """--fix's section of the text report, as the main command prints it for these cells."""
    multi_py = len({t.python for t in targets}) > 1
    return [
        *fix.text_lines(fixes.cells, multi_py, fixes.not_looked_up, fixes.issues_urls),
        *gapwheels.text_lines(fixes.fills, multi_py, fix.alternatives(fixes.cells), fix.any_upgrade(fixes.cells)),
    ]


def summary(n_worse: int, n_new: int) -> str:
    if not n_worse and not n_new:
        return "Nothing got worse."
    parts = []
    if n_worse:
        parts.append(f"{n_worse} package{'s' * (n_worse != 1)} got worse")
    if n_new:
        parts.append(f"{n_new} new package{'s' * (n_new != 1)} {'has' if n_new == 1 else 'have'} gaps")
    return ", ".join(parts) + "."


class GitInputError(Exception):
    """A REF:PATH input git can't read, or no git to read it with."""


# A Windows drive path (C:\\x, C:/x) has a colon but isn't a ref.
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def git_ref_input(arg: str) -> tuple[str, str] | None:
    """(REF, PATH) when `arg` isn't an existing path and looks like REF:PATH (origin/main:uv.lock), else None."""
    if os.path.exists(arg) or _DRIVE.match(arg):
        return None
    ref, sep, path = arg.partition(":")
    if not sep or not ref or not path or path.endswith(("/", "\\")):
        return None
    return ref, path


def _pinned_spec(text: str) -> tuple[str, str]:
    """A single exact requirement for --deps, not a path, range, URL or unpinned name."""
    try:
        req = Requirement(text)
    except InvalidRequirement:
        raise SourceError(f"--deps needs a pinned package spec such as name==1.2, not {text!r}") from None
    specs = list(req.specifier)
    if req.url is not None or req.marker is not None or len(specs) != 1:
        raise SourceError(f"--deps needs an exact PyPI package version (name==1.2), not {text!r}")
    (spec,) = specs
    if spec.operator != "==" or "*" in spec.version:
        raise SourceError(f"--deps needs an exact PyPI package version (name==1.2), not {text!r}")
    name = req.name + (f"[{','.join(sorted(req.extras))}]" if req.extras else "")
    return name, spec.version


def _git_blob(ref: str, path: str) -> subprocess.CompletedProcess[bytes]:
    # `git cat-file blob`: plumbing, so no textconv, filters or pager, and a tree (a folder) is refused rather
    # than listed. An argument list, never a shell, and --end-of-options keeps a ref that starts with `-` from
    # being read as an option.
    try:
        return subprocess.run(
            ["git", "cat-file", "blob", "--end-of-options", f"{ref}:{path}"], capture_output=True, check=False
        )
    except FileNotFoundError:
        raise GitInputError(f"{ref}:{path} looks like a git ref, but git isn't on PATH") from None


def read_git_ref(ref: str, path: str, into: Path) -> str:
    """Write the file at REF:PATH to a file named like PATH in `into` (so its format is detected) and return it.

    The pyproject.toml and uv.toml next to it at that ref come along when there are some: an older poetry.lock
    takes its direct dependencies from pyproject.toml, and a uv.lock its no-build and no-binary settings from either,
    so leaving them behind would change the answer for that side.
    """
    done = _git_blob(ref, path)
    if done.returncode != 0:
        message = done.stderr.decode(errors="replace").strip() or f"git exited {done.returncode}"
        raise GitInputError(f"can't read {ref}:{path} from git: {message}")
    side = into / str(len(list(into.iterdir())))
    side.mkdir()
    posix = path.replace("\\", "/")
    out = side / posixpath.basename(posix)
    out.write_bytes(done.stdout)
    folder = posixpath.dirname(posix)
    for config in ("pyproject.toml", "uv.toml"):
        if out.name == config:
            continue
        found = _git_blob(ref, f"{folder}/{config}" if folder else config)
        if found.returncode == 0:  # none at that ref is normal: nothing to bring along
            (side / config).write_bytes(found.stdout)
    return str(out)


def _config_inputs(new_input: str, *, deps: bool) -> list[str]:
    """Working-tree path(s) to search for pyproject.toml and .python-version for the NEW input."""
    if deps:
        return []
    if (ref := git_ref_input(new_input)) is not None:
        working = Path(ref[1])
        if working.is_file() or working.is_dir():
            return [str(working)]
        if working.parent.is_dir():
            return [str(working.parent)]
        return []
    return [new_input]


def main(argv: list[str] | None = None, prog: str = "python -m wheelgap.diff") -> int:
    for stream in (sys.stdout, sys.stderr):
        # As in cli.main: write UTF-8 to a non-UTF-8 pipe (Windows cp1252) instead of crashing on ✓ and →.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if encoding != "utf8" and not stream.isatty() and not os.environ.get("PYTHONIOENCODING"):
                reconfigure(encoding="utf-8", errors="replace")
            else:
                reconfigure(errors="replace")
    with guarded_output():  # as in cli.main: lockfile text must never run as a workflow command in a CI log
        return _main(argv, prog)


def lock_default_targets(paths: list[Path], targets: list[Target]) -> tuple[list[Target], str | None]:
    """The default targets without the platforms both sides' uv.lock `[tool.uv] environments` leave out (a platform
    only one side leaves out stays, and is refused), with the note to print. A side that isn't a uv.lock leaves out
    nothing, so then nothing goes; a file that can't be read here is left for the check to report."""
    sources = []
    for path in paths:
        try:
            lock = find_lockfile(path) if path.is_dir() else path
            if file_kind(lock) != "uv" or not lock.is_file():
                return targets, None
            sources.append(parse_uv_lock(lock))
        except (OSError, ValueError):
            return targets, None
    narrowed, left_out = narrow_to_lock_environments(sources, targets, every=True)
    return narrowed, lock_environments_note(sources, left_out, narrowed, "default targets") if left_out else None


# The main command's way to a simple index, which diff doesn't have.
_SIMPLE_ADVICE = "To use a simple index, pass --index-url instead."


def _check_index_url() -> None:
    """pypi.check_index_url's error, with advice diff can follow: it has no --index-url."""
    try:
        check_index_url()
    except SourceError as e:
        raise SourceError(
            str(e).replace(
                _SIMPLE_ADVICE,
                "wheelgap diff can't use a simple index (it has no --index-url): check each lockfile with "
                "wheelgap --index-url instead.",
            )
        ) from None


def _refuse_index_url(table: dict, where: str) -> None:
    """diff looks packages up on PyPI (or a WHEELGAP_INDEX_URL mirror) only, so the simple index the main command
    would use for `index-url` in [tool.wheelgap] would be ignored without a word: refuse it instead."""
    if table.get("index-url") is not None:  # set at all, as the main command reads it (an empty value is its error)
        raise SourceError(
            f"{where} sets index-url in [tool.wheelgap], which wheelgap diff doesn't read yet, so it would check "
            "PyPI instead of that index; check each lockfile with wheelgap instead, which uses it"
        )


def _input_index_url(path: str, label: str) -> None:
    """Refuse `index-url` in the pyproject.toml the main command would read for this input (for a REF:PATH, the
    copy from that ref). Only that key is read: diff takes its other settings from the NEW input's project."""
    pyproject = find_pyproject([path])
    if pyproject is None:
        return
    try:
        # Named as typed: a REF:PATH's copy sits in a temporary folder that's gone after the run.
        data = tomllib.loads(pyproject.read_text(encoding="utf-8-sig"))
        table = tool_table(data, f"the pyproject.toml beside {label}").get("wheelgap", {})
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SourceError(
            f"the pyproject.toml beside {label} can't be read, so its index-url isn't known: {e}"
        ) from None
    if not isinstance(table, dict):  # as load_config() says it for the NEW side's file
        raise SourceError(f"the pyproject.toml beside {label}: [tool.wheelgap] must be a table")
    _refuse_index_url(table, f"the pyproject.toml beside {label}")


def _main(argv: list[str] | None, prog: str) -> int:
    p = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    p.add_argument(
        "old",
        metavar="OLD",
        help="the lockfile before the change (or a directory, or git's REF:PATH, e.g. origin/main:uv.lock; "
        "with --deps, a pinned name==1.2)",
    )
    p.add_argument("new", metavar="NEW", help="the lockfile after the change (in the same forms)")
    p.add_argument(
        "-p",
        "--python",
        action="append",
        default=[],
        metavar="VER",
        help="target Python, e.g. 3.13, 3.14t, or next / next-t for the newest CPython wheelgap knows "
        "(repeatable; default: [tool.wheelgap] python, else .python-version, else the running Python)",
    )
    p.add_argument(
        "-t", "--target", action="append", default=[], metavar="PLATFORM", help="target platform (repeatable)"
    )
    p.add_argument("--glibc", metavar="X.Y", help=f"glibc version for linux-* targets (default: {DEFAULT_GLIBC})")
    p.add_argument("--musl", metavar="X.Y", help=f"musl version for musl-* targets (default: {DEFAULT_MUSL})")
    p.add_argument(
        "--distro",
        metavar="NAME",
        help="default glibc/musl version from a distro, e.g. ubuntu-22.04 (does not select -t)",
    )
    p.add_argument("--macos", metavar="X.Y", help=f"minimum macOS for macos-* targets (default: {DEFAULT_MACOS})")
    p.add_argument("--ios", metavar="X.Y", help=f"minimum iOS for ios-* targets (default: {DEFAULT_IOS})")
    p.add_argument(
        "--android-api",
        metavar="N",
        help=f"minimum Android API level for android-* targets (default: {DEFAULT_ANDROID_API})",
    )
    p.add_argument("--json", action="store_true", help="print one JSON object instead of the text report")
    p.add_argument(
        "--deps",
        action="store_true",
        help="resolve two pinned PyPI package specs and compare their full dependency trees",
    )
    p.add_argument(
        "--fix",
        action="store_true",
        help="also say how to fix each listed gap, as wheelgap NEW --fix does: an upgrade first, else an unofficial "
        "gap wheel (looks the gap packages up on PyPI and reads wheelgap's gap wheel list)",
    )
    args = p.parse_args(argv)
    if args.fix and args.deps:
        print("wheelgap: error: --fix works on two lockfiles, not with --deps", file=sys.stderr)
        if args.json:
            print(
                json.dumps(
                    {"error": {"message": "--fix works on two lockfiles, not with --deps", "type": "UsageError"}}
                )
            )
        return 2
    try:
        # As in the main command, before any lookup: a /simple URL there would make every package `unknown`.
        _check_index_url()
        config_inputs = _config_inputs(args.new, deps=args.deps)
        pyproject = find_pyproject(config_inputs)
        config = load_config(pyproject) if pyproject else {}
        if pyproject:
            _refuse_index_url(config, str(pyproject))
        glibc, musl = libc_versions(args, config)
        pythons = args.python or config.get("python") or read_python_version(project_dir(config_inputs)) or None
        platforms = args.target or config.get("targets", [])
        if not platforms and pyproject:
            platforms = load_uv_targets(pyproject, pythons or [running_python()])
            if platforms:
                note = (
                    f"wheelgap: note: targets from {pyproject} [tool.uv] required-environments: {', '.join(platforms)}"
                )
                if any(p.startswith("musl-") for p in platforms):
                    note += "; Linux markers select both glibc and musl targets"
                print(note, file=sys.stderr)
        targets = make_targets(
            pythons,
            platforms or None,
            glibc=glibc,
            musl=musl,
            macos=args.macos or config.get("macos", DEFAULT_MACOS),
            ios=args.ios or config.get("ios", DEFAULT_IOS),
            android_api=args.android_api or config.get("android-api", DEFAULT_ANDROID_API),
        )
        if next_note := next_python_note(pythons or []):
            print(f"wheelgap: note: {next_note}", file=sys.stderr)
        sides = []
        sources = []
        rows: list[Row] = []
        if args.deps:
            if (index_variable := origin.index_env()) is not None:
                raise SourceError(
                    f"--deps resolves from PyPI, but {index_variable} may point to another index; "
                    "unset it or compare lockfiles instead"
                )
            pins = []
            for spec in (args.old, args.new):  # both checked before either is looked up
                if Path(spec).exists():
                    raise SourceError(
                        "--deps takes two pinned package specs, not local files; omit --deps for lockfiles"
                    )
                pins.append(_pinned_spec(spec))
            for pin in pins:
                # A resolver refusal raises DepsError (exit 2); a package it couldn't read is `unknown`, which
                # compares as it does for lockfiles.
                results = check_packages([pin], targets, deps=True)
                # A release PyPI doesn't have isn't an error to the resolver (it's `unknown`), but here it would
                # report the other side's whole tree, or the package itself, as a change.
                root = canonicalize_name(Requirement(pin[0]).name)
                if not any(canonicalize_name(r.name) == root and r.version for r in results):
                    raise SourceError(f"--deps couldn't find {pin[0]}=={pin[1]} on PyPI")
                sides.append(merge(results, targets))
                # rows stays empty: comment suggestions are only for lockfiles.
                sources.append(Source("api", pypi_only=True))
        else:
            with tempfile.TemporaryDirectory(prefix="wheelgap-diff-") as tmp:
                read = []
                for path in (args.old, args.new):
                    ref = git_ref_input(path)
                    if ref is None and not os.path.exists(path):
                        # As the main command says it: the argument as typed, not Python's FileNotFoundError.
                        raise SourceError(f"{path}: no such file")
                    read.append((read_git_ref(*ref, Path(tmp)) if ref is not None else path, ref))
                for (path, _), label in zip(read, (args.old, args.new), strict=True):
                    _input_index_url(path, label)  # before any lookup, for either side
                if not platforms:
                    targets, narrowed = lock_default_targets([Path(path) for path, _ in read], targets)
                    if narrowed is not None:
                        print(f"wheelgap: note: {narrowed}", file=sys.stderr)
                for path, ref in read:
                    results, source, rows = _check_lockfile(path, targets, online=False, offline=False)
                    if ref is not None:
                        # An omitted parent or pdm.toml may name an index, so don't assume PyPI provenance.
                        source.pypi_only = False
                    packages = merge(results, targets)
                    add_skipped(packages, source, targets)
                    sides.append(packages)
                    sources.append(source)
    except Exception as e:  # unknown targets, unreadable or malformed lockfiles, a REF:PATH git can't read
        # The main command prints these two without the exception's name, and their text is written for people.
        prefix = "" if isinstance(e, (SourceError, TargetError)) else f"{type(e).__name__}: "
        print(f"wheelgap: error: {prefix}{e}", file=sys.stderr)
        if args.json:  # as the main command does, so scripts needn't parse stderr
            print(json.dumps({"error": {"message": str(e), "type": type(e).__name__}}))
        return 2

    report = compare(sides[0], sides[1], targets)
    fixes = None
    if args.fix:
        try:
            fixes = add_fixes(report, rows, sources, targets)
        except Exception as e:  # like the suggestions below, --fix never changes the comparison or the exit code
            print(f"wheelgap: note: --fix couldn't work out the fixes ({type(e).__name__})", file=sys.stderr)
            for entry in report.worse + report.new:
                for side in entry["targets"].values():
                    side.pop("fix", None)
    if args.json:
        # The comment's release hints say what the Fix column already does, so they're only for a run without --fix.
        if os.environ.get("_WHEELGAP_ACTION_SUGGEST") == "1" and fixes is None:
            try:
                add_comment_suggestions(report, rows, sources, targets)
            except Exception:
                pass  # An optional lookup must not change the comparison's report or exit code.
        print(json_report(report, targets, fixes))
    else:
        for line in report.lines:
            print(line)
        print(summary(len(report.worse), len(report.new)))
        for line in fix_text(fixes, targets) if fixes is not None else []:
            print(line)
    if report.unchecked:
        print(f"wheelgap: note: couldn't check {', '.join(report.unchecked)} in either lockfile", file=sys.stderr)
    return 1 if report.worse or report.new else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
