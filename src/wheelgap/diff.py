"""Compare two lockfiles: which packages got worse on a target, and which new packages have gaps.

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
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import canonicalize_name

from . import __version__
from .api import Result, _check_lockfile, make_targets
from .check import CELL, Status, applies, reachable
from .logsafe import guarded_output
from .sources import Source
from .targets import DEFAULT_GLIBC, DEFAULT_MACOS, DEFAULT_MUSL, Target, next_python_note, resolve_distro

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
        merged[key] = Package(rs[0].name, versions, statuses)
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


def compare(before: dict[str, Package], after: dict[str, Package], targets: list[Target]) -> Report:
    """Packages that got worse and new packages with gaps, with the names that couldn't be checked at the
    same version on either side."""
    multi_py = len({t.python for t in targets}) > 1
    report = Report()
    for key, pkg in after.items():
        if key not in before:
            gaps = [t for t in targets if new_gap(pkg, t)]
            if gaps:
                report.lines.append(f"{pkg.name} {pkg.version} (new)")
                report.lines += [f"  {t.label(multi_py)}: {pkg.cell(t)}" for t in gaps]
                report.new.append(
                    {
                        "name": pkg.name,
                        "versions": pkg.versions,
                        "targets": {t.label(multi_py): _side(pkg, t) for t in gaps},
                    }
                )
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
            report.lines.append(f"{pkg.name} {version}")
            report.lines += [f"  {t.label(multi_py)}: {was.cell(t)} → {pkg.cell(t)}" for t in changed]
            report.worse.append(
                {
                    "name": pkg.name,
                    "old_versions": was.versions,
                    "new_versions": pkg.versions,
                    "targets": {t.label(multi_py): _side(was, t, "old_") | _side(pkg, t, "new_") for t in changed},
                }
            )
    return report


def json_report(report: Report, targets: list[Target]) -> str:
    """--json: one object. Its keys are a public interface, like the main command's --json. Versions are
    lists: empty when the input gives no version (an unpinned or unknown package, or a requirements line
    installing from git, a path or a URL); several for a uv.lock fork."""
    multi_py = len({t.python for t in targets}) > 1
    return json.dumps(
        {
            "version": __version__,
            "targets": [{"label": t.label(multi_py), "python": t.python, "platform": t.platform} for t in targets],
            "worse": report.worse,
            "new": report.new,
            "unchecked": report.unchecked,
            "summary": {"worse": len(report.worse), "new": len(report.new)},
        },
        indent=2,
    )


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

    The pyproject.toml next to it at that ref comes along when there is one: an older poetry.lock takes its
    direct dependencies from there, so leaving it behind would change the answer for that side.
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
    if out.name != "pyproject.toml":
        folder = posixpath.dirname(posix)
        project = _git_blob(ref, f"{folder}/pyproject.toml" if folder else "pyproject.toml")
        if project.returncode == 0:  # none at that ref is normal: nothing to bring along
            (side / "pyproject.toml").write_bytes(project.stdout)
    return str(out)


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


def _main(argv: list[str] | None, prog: str) -> int:
    p = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    p.add_argument(
        "old",
        metavar="OLD",
        help="the lockfile before the change (or a directory, or git's REF:PATH, e.g. origin/main:uv.lock)",
    )
    p.add_argument("new", metavar="NEW", help="the lockfile after the change (or a directory, or REF:PATH)")
    p.add_argument(
        "-p",
        "--python",
        action="append",
        default=[],
        metavar="VER",
        help="target Python, e.g. 3.13, 3.14t, or next / next-t for the newest CPython wheelgap knows "
        "(repeatable; default: the running Python)",
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
    p.add_argument("--json", action="store_true", help="print one JSON object instead of the text report")
    args = p.parse_args(argv)
    try:
        # Match cli.py precedence: explicit --glibc / --musl wins over --distro.
        glibc, musl = DEFAULT_GLIBC, DEFAULT_MUSL
        if args.distro is not None:
            key, version = resolve_distro(args.distro)
            if key == "glibc":
                glibc = version
            else:
                musl = version
        if args.glibc is not None:
            glibc = args.glibc
        if args.musl is not None:
            musl = args.musl
        targets = make_targets(
            args.python or None, args.target or None, glibc=glibc, musl=musl, macos=args.macos or DEFAULT_MACOS
        )
        if note := next_python_note(args.python):
            print(f"wheelgap: note: {note}", file=sys.stderr)
        sides = []
        with tempfile.TemporaryDirectory(prefix="wheelgap-diff-") as tmp:
            for path in (args.old, args.new):
                ref = git_ref_input(path)
                if ref is not None:
                    path = read_git_ref(*ref, Path(tmp))
                results, source = _check_lockfile(path, targets, online=False, offline=False)
                packages = merge(results, targets)
                add_skipped(packages, source, targets)
                sides.append(packages)
    except Exception as e:  # unknown targets, unreadable or malformed lockfiles, a REF:PATH git can't read
        print(f"wheelgap: error: {type(e).__name__}: {e}", file=sys.stderr)
        if args.json:  # as the main command does, so scripts needn't parse stderr
            print(json.dumps({"error": {"message": str(e), "type": type(e).__name__}}))
        return 2

    report = compare(sides[0], sides[1], targets)
    if args.json:
        print(json_report(report, targets))
    else:
        for line in report.lines:
            print(line)
        print(summary(len(report.worse), len(report.new)))
    if report.unchecked:
        print(f"wheelgap: note: couldn't check {', '.join(report.unchecked)} in either lockfile", file=sys.stderr)
    return 1 if report.worse or report.new else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
