"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import sysconfig
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import packaging
from packaging.markers import InvalidMarker, Marker
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

from . import __version__, cache, condaforge, gapwheels, origin, piwheels
from . import notes as tracker
from . import suggest as release_suggest
from .api import explain_issues_urls, running_python
from .badge import write_badge
from .build import BuildInfo, describe, lookup_all, requires_for, shown
from .check import (
    CELL,
    EXPLAIN_LIMIT,
    HINTED,
    Row,
    Status,
    check,
    dropped_targets,
    explain,
    explain_lines,
    marker_holds,
    reachable,
)
from .codequality import write_codequality
from .constraints import write_constraints
from .deps import Resolver, pinned_complete, resolve_deps
from .dockerfile import DockerfileError, dockerfile_requirements, read_dockerfile
from .github import github_annotations
from .install_log import failed_builds, log_source, read_log
from .junit import write_junit
from .logsafe import guarded_output, write_command
from .offline import require_offline, undo_offline
from .pyodide import (
    PyodideLock,
    PyodideLockError,
    apply_bundled,
    bundled_version_differs,
    check_targets,
    load_pyodide_lock,
)
from .pypi import (
    apply_locked_python_ranges,
    check_index_url,
    fill_all,
    keeps_locked_files,
    previous_release,
    recheck_lock,
    split_credentials,
)
from .sarif import write_sarif
from .sources import (
    Package,
    Source,
    SourceError,
    UvSelection,
    check_report_targets,
    load_inputs,
    parse_dist,
    pyproject_group_names,
    pythons_outside_lock,
    restore_uv_python_files,
    select_pyproject_extras,
    select_pyproject_groups,
    select_uv_dependencies,
    skipped_list,
    uv_selected_roots,
)
from .suggest_targets import render_text as render_target_suggestions
from .suggest_targets import suggest as suggest_targets
from .suggest_targets import to_json as target_suggestions_json
from .targets import (
    ALIASES,
    DEFAULT_ANDROID_API,
    DEFAULT_GLIBC,
    DEFAULT_IOS,
    DEFAULT_MACOS,
    DEFAULT_MUSL,
    DEFAULT_PLATFORMS,
    DISTRO_ALIASES,
    DISTROS,
    EXTRA_ARCHES,
    MANYLINUX_FLOOR,
    MANYLINUX_X86_ARCHES,
    NATIVE_PLATFORMS,
    PLATFORMS,
    PYODIDE_PLATFORMS,
    Target,
    TargetError,
    build_targets,
    next_python_note,
    parse_api_level,
    parse_ios_version,
    parse_version_pair,
    resolve_distro,
    resolve_platform,
    resolve_platforms,
)

EXAMPLES = """\
examples:
  wheelgap                                 check the lockfile in the current directory
  wheelgap uv.lock -t musl-aarch64         will my Alpine ARM image build from source?
  wheelgap -p 3.14t -t all                 is my stack ready for free-threaded 3.14?
  wheelgap -p 3.14 -p next                 what blocks the move to the newest Python?
  wheelgap psycopg2 uvloop -t win-amd64    check packages without a lockfile
  wheelgap requirements.txt --json         machine-readable output for CI
  wheelgap -t alpine-arm64 --explain       show which wheels were rejected, and why
"""


def musl_docker_names(item: str) -> str:
    """With an Alpine image, `-t linux/arm64` means musl on arm64: Docker names say only the architecture.

    wheelgap's own names (linux-aarch64, musl-aarch64) are left alone, since they name the libc too.
    """
    out = []
    for name in item.split(","):
        if "/" in name:
            resolved = resolve_platform(name)
            name = resolved.replace("linux-", "musl-", 1) if resolved.startswith("linux-") else resolved
        out.append(name)
    return ",".join(out)


def version_string() -> str:
    """The --version line, including packaging and the running Python, for bug reports."""
    gil_disabled = sysconfig.get_config_var("Py_GIL_DISABLED")
    suffix = "t" if gil_disabled else ""
    return (
        f"wheelgap {__version__} (packaging {packaging.__version__}, "
        f"{platform.python_implementation()} {platform.python_version()}{suffix})"
    )


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wheelgap",
        description="Find the dependencies that have no prebuilt wheel for your target platform, "
        "so they'd be compiled from source (slow) or fail to install.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "inputs",
        nargs="*",
        metavar="INPUT",
        help=(
            "uv.lock, pylock.toml, poetry.lock, Pipfile.lock, pixi.lock, requirements.txt, report.json, "
            "environment.yml (pip packages), pyproject.toml (direct dependencies), a script.py's # /// script block "
            "(PEP 723), a directory, or package specs (default: .)"
        ),
    )
    p.add_argument(
        "-p",
        "--python",
        action="append",
        default=[],
        metavar="VER",
        help="target Python, e.g. 3.13, 3.14t, or next / next-t for the newest CPython wheelgap knows "
        f"(repeatable; default: .python-version, else {running_python()})",
    )
    p.add_argument(
        "-t",
        "--target",
        action="append",
        default=[],
        metavar="PLATFORM",
        help=(
            "target platform (repeatable; 'all' for the 8 common ones, 'everything' adds armv7l, ppc64le, "
            "s390x, riscv64 and i686 Linux and win32, 'mobile' is iOS and Android; "
            f"default: {','.join(DEFAULT_PLATFORMS)})"
        ),
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
    p.add_argument(
        "--dockerfile",
        metavar="PATH",
        help="take -p/-t (and --glibc/--musl) from the final stage's image, e.g. FROM python:3.13-alpine",
    )
    p.add_argument(
        "--dockerfile-inputs",
        action="store_true",
        help="with --dockerfile, check the requirements files its final stage installs with RUN pip install -r FILE "
        "(a strict subset of Dockerfiles: anything else that may install packages is an error naming its line)",
    )
    p.add_argument(
        "--dockerfile-context",
        metavar="DIR",
        help="with --dockerfile-inputs, the folder the build context starts from (default: the Dockerfile's folder)",
    )
    p.add_argument(
        "--build-arg",
        action="append",
        metavar="NAME=VALUE",
        help="with --dockerfile, a value for a global ARG used in FROM, as docker build --build-arg sets it "
        "(repeatable)",
    )
    p.add_argument(
        "--pyodide-lock",
        metavar="PATH_OR_HTTPS_URL",
        help="a Pyodide release's pyodide-lock.json: packages Pyodide ships itself count as available on -t pyodide",
    )
    p.add_argument(
        "--extra",
        action="append",
        metavar="NAME",
        help="include a uv or Poetry project extra, or a pyproject.toml optional-dependencies group (repeatable)",
    )
    p.add_argument(
        "--all-extras", action="store_true", help="include every uv or Poetry project extra (or pyproject.toml extra)"
    )
    p.add_argument(
        "--group",
        action="append",
        metavar="NAME",
        help=(
            "include a dependency group (uv.lock, Poetry 2.x poetry.lock, pdm.lock, or pyproject.toml "
            "[dependency-groups]; repeatable)"
        ),
    )
    p.add_argument(
        "--no-dev", action="store_true", help="exclude the dev group (uv.lock, Poetry 2.x poetry.lock, pdm.lock)"
    )
    p.add_argument(
        "--only-group", action="append", metavar="NAME", help="check only the named dependency groups (repeatable)"
    )
    p.add_argument(
        "--environment",
        metavar="NAME",
        help="the pixi.lock environment to check (default: default)",
    )
    p.add_argument(
        "--online", action="store_true", help="re-check lockfile packages against PyPI instead of trusting the lockfile"
    )
    p.add_argument("--offline", action="store_true", help="never contact an index; fail if an input needs one")
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="don't read or save the 24-hour cache of pinned package lookups",
    )
    p.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete the cache of pinned package lookups first; with no INPUT, do only that",
    )
    p.add_argument(
        "--from-log",
        metavar="FILE",
        help="read the packages a failed pip, uv, Poetry or PDM install couldn't build from its log FILE (- for stdin) "
        "and check them with --explain --suggest; needs -p and -t (or --dockerfile) for where the install ran",
    )
    p.add_argument("--all", action="store_true", help="show every package, not just the ones with gaps")
    p.add_argument("--explain", action="store_true", help="for each gap, list the wheels that were rejected and why")
    p.add_argument(
        "--build-info",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with --explain, read the sdist of each package that builds from source to say what the build needs "
        "(downloads them from PyPI; also build-info = true; --no-build-info overrides build-info = true)",
    )
    p.add_argument(
        "--conda-forge",
        action="store_true",
        help="with --explain, also say when conda-forge has a package of the same name with builds for a gap's "
        "platform (sends the names of PyPI packages with gaps to anaconda.org)",
    )
    p.add_argument(
        "--piwheels",
        action="store_true",
        help="with --explain, also say when piwheels has a wheel of the same version for a linux-armv7l (Raspberry "
        "Pi) gap (sends the names of PyPI packages with gaps to piwheels.org)",
    )
    p.add_argument(
        "--deps",
        action="store_true",
        help="for package names, requirements files, a conda environment.yml, pyproject.toml or a script's "
        "# /// script block, also check what they install, resolved per target from PyPI's metadata (looks each "
        "dependency up on pypi.org; a lockfile is exact)",
    )
    p.add_argument(
        "--suggest",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with --explain, also name the nearest newer release with a wheel for each gap's target "
        "(looks the PyPI packages with gaps up on pypi.org; on by default with --deps, --no-suggest turns it off)",
    )
    p.add_argument(
        "--write-constraints",
        metavar="FILE",
        help="with --suggest, also write a pip/uv constraints file to FILE: one pin per package, to the release "
        "--suggest names for its gaps, when one release covers them all without losing a wheel elsewhere",
    )
    p.add_argument(
        "--notes",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with --explain, also show the coverage tracker's notes for packages with gaps, which sends the names of "
        "those from PyPI to wheelgap.github.io (never with --offline, --index-url or a WHEELGAP_INDEX_URL mirror; "
        "--no-notes overrides notes = true)",
    )
    p.add_argument(
        "--fix",
        action="store_true",
        help="also say which gaps a wheel on wheelgap's gap wheel index fills, and how to install from it (reads "
        "the index's list from wheelgap.github.io, which sends no package names, and keeps it in the 24-hour cache; "
        "not with --offline, and off with --index-url or a WHEELGAP_INDEX_URL mirror)",
    )
    p.add_argument(
        "--print-gap-wheel-snippet",
        action="store_true",
        help="with --fix, print only the configuration that installs the gap wheels, for one uv.lock, requirements "
        "file or package names (other inputs are refused); it never writes to your project",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="print only the gap summary and affected package names (--no-quiet overrides quiet = true)",
    )
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.add_argument(
        "--markdown",
        action="store_true",
        help="print a Markdown report (e.g. for $GITHUB_STEP_SUMMARY) instead of a table",
    )
    p.add_argument(
        "--junit",
        metavar="PATH",
        help="also write a JUnit XML report to PATH, alongside the usual output",
    )
    p.add_argument(
        "--gitlab-code-quality",
        metavar="PATH",
        help="also write a GitLab Code Quality JSON report to PATH for merge request findings",
    )
    p.add_argument(
        "--sarif",
        metavar="PATH",
        help="also write a SARIF 2.1.0 report to PATH for code scanning (github/codeql-action/upload-sarif)",
    )
    p.add_argument(
        "--badge",
        metavar="PATH",
        help="also write a shields.io endpoint JSON file to PATH with how many targets have wheels for everything",
    )
    p.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="NAME[:TARGET]",
        help="ignore known package gaps (repeatable, comma-separated), on every target or only on TARGET "
        "(e.g. psycopg2:musl-x86_64); list ignored names in the report",
    )
    p.add_argument(
        "--advisory",
        action="append",
        default=[],
        metavar="TARGET",
        help="report gaps on TARGET (anything -t takes; repeatable, comma-separated) without letting them fail "
        "the run; TARGET must be a checked target, and a group like all means the checked ones among its members",
    )
    p.add_argument("--exit-zero", action="store_true", help="exit 0 even when gaps are found")
    p.add_argument(
        "--github-annotations",
        action="store_true",
        help="also mark each gap on its lockfile line in GitHub Actions (to stderr with --json or --markdown)",
    )
    p.add_argument(
        "--fail-on",
        choices=FAIL_ON,
        help="exit 1 on any gap ('source', the default) or only when a package can't install ('missing')",
    )
    p.add_argument(
        "--dist",
        metavar="DIR",
        help="check the wheels and sdist in DIR as the release you're about to upload: per target, whether a "
        "wheel installs or the sdist builds, plus a warning for each target the last PyPI release had a wheel "
        "for that this release drops (needs the PyPI JSON API, so not with --offline or --index-url)",
    )
    p.add_argument(
        "--index-url",
        metavar="URL",
        help="look packages up in this simple index (PEP 691 JSON) instead of PyPI's JSON API "
        "(lockfiles that list their files are still read offline unless --online); "
        "credentials can go in the URL (https://user:token@host/simple)",
    )
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("--list-targets", action="store_true", help="list platforms, aliases and compatibility limits")
    p.add_argument(
        "--suggest-targets",
        nargs="?",
        const=".",
        metavar="DIR",
        help="suggest targets and Pythons from DIR's GitHub Actions, GitLab CI and Dockerfiles (default: .), "
        "with a [tool.wheelgap] snippet; checks nothing",
    )
    p.add_argument("-V", "--version", action="version", version=version_string())
    return p


FAIL_ON = ("source", "missing")
LIST_KEYS = ("python", "targets", "ignore", "extras", "groups", "advisory")
STR_KEYS = ("glibc", "musl", "macos", "ios", "android-api", "fail-on", "distro", "pyodide-lock", "index-url")
BOOL_KEYS = ("quiet", "offline", "notes", "build-info")


def project_dir(inputs: list[str]) -> Path:
    """The folder of the first lockfile or directory given, else the current directory."""
    for item in inputs:
        p = Path(item)
        if p.is_dir() or p.is_file():
            return p if p.is_dir() else p.parent
    return Path(".")


def find_pyproject(inputs: list[str]) -> Path | None:
    """pyproject.toml in the project folder (see project_dir)."""
    candidate = project_dir(inputs) / "pyproject.toml"
    return candidate if candidate.is_file() else None


PYTHON_VERSION_FILE = ".python-version"
# uv and pyenv write "3.13", "3.13.1" or "3.14t"; anything else (system, pypy@3.10) is skipped.
PYTHON_VERSION_LINE = re.compile(r"3\.(\d+)(?:\.\d+)?(t?)", re.ASCII)


def read_python_version(folder: Path) -> list[str]:
    """Versions from the .python-version file in folder, e.g. ['3.13'] for a line '3.13.1'."""
    path = folder / PYTHON_VERSION_FILE
    if not path.is_file():
        return []
    versions = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: Windows editors add a BOM
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        m = PYTHON_VERSION_LINE.fullmatch(entry)
        if m:
            versions.append(f"3.{m.group(1)}{m.group(2)}")
        else:
            print(f"wheelgap: note: ignoring {entry!r} in {path}", file=sys.stderr)
    return versions


def load_config(path: Path) -> dict:
    """Defaults from the [tool.wheelgap] table; command-line flags still win."""
    try:
        text = path.read_text(encoding="utf-8-sig")  # -sig: Windows editors add a BOM
    except UnicodeDecodeError:
        raise SourceError(f"{path}: isn't UTF-8, so its [tool.wheelgap] table can't be read") from None
    table = tomllib.loads(text).get("tool", {}).get("wheelgap", {})
    if not isinstance(table, dict):
        raise SourceError(f"{path}: [tool.wheelgap] must be a table")
    config = {}
    for key, value in table.items():
        if key in LIST_KEYS:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise SourceError(f"{path}: [tool.wheelgap] {key} must be a list of strings")
        elif key in STR_KEYS:
            if key == "android-api" and isinstance(value, int) and not isinstance(value, bool):
                value = str(value)  # an API level is naturally written as a number
            if not isinstance(value, str):
                example = {
                    "android-api": 'an integer or a string like "24"',
                    "ios": 'a string like "13.0"',
                    "musl": 'a string like "1.2"',
                    "macos": 'a string like "14.0"',
                    "fail-on": 'a string like "missing"',
                    "distro": 'a string like "ubuntu-22.04"',
                    "pyodide-lock": "a path or URL string",
                }.get(key, 'a string like "2.31"')
                raise SourceError(f"{path}: [tool.wheelgap] {key} must be {example}")
            if key == "fail-on" and value not in FAIL_ON:
                raise SourceError(f"{path}: [tool.wheelgap] fail-on must be one of: {', '.join(FAIL_ON)}")
        elif key in BOOL_KEYS:
            if not isinstance(value, bool):
                raise SourceError(f"{path}: [tool.wheelgap] {key} must be a boolean")
        else:
            raise SourceError(f"{path}: unknown key in [tool.wheelgap]: {key}")
        config[key] = value
    return config


def load_uv_targets(path: Path, pythons: list[str]) -> list[str]:
    """Native platforms needed by any required-environments marker for any selected Python."""
    # -sig: load_config() has already read this file, so only a BOM is left to allow for
    table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("tool", {}).get("uv", {})
    if not isinstance(table, dict):
        raise SourceError(f"{path}: [tool.uv] must be a table")
    markers = table.get("required-environments", [])
    if isinstance(markers, str):  # uv accepts a single marker as well as a list
        markers = [markers]
    if not isinstance(markers, list) or not all(isinstance(m, str) for m in markers):
        raise SourceError(f"{path}: [tool.uv] required-environments must be a string or a list of strings")
    if not markers:
        return []
    # marker_holds conservatively accepts unparseable package markers. A project setting
    # should instead report a typo, rather than silently select every native platform.
    for marker in markers:
        try:
            Marker(marker)
        except InvalidMarker as e:
            raise SourceError(f"{path}: [tool.uv] required-environments has an invalid marker {marker!r}: {e}") from e
    targets = build_targets(pythons, NATIVE_PLATFORMS)
    platforms = list(dict.fromkeys(t.platform for t in targets if any(marker_holds(m, t) for m in markers)))
    if not platforms:
        # e.g. a mobile-only project, or `wheelgap requests` run in a project folder: don't fail the run
        print(
            f"wheelgap: note: {path} [tool.uv] required-environments matches none of the native targets "
            f"for Python {', '.join(dict.fromkeys(t.python for t in targets))}, so the default targets are checked; "
            "select targets with -t",
            file=sys.stderr,
        )
    return platforms


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def green(self, t: str) -> str:
        return self(t, "32")

    def yellow(self, t: str) -> str:
        return self(t, "33")

    def red(self, t: str) -> str:
        return self(t, "31")

    def dim(self, t: str) -> str:
        return self(t, "2")

    def bold(self, t: str) -> str:
        return self(t, "1")


def use_color(mode: str) -> bool:
    if mode != "auto":
        return mode == "always"
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def ignored_gaps(row: Row) -> list[Target]:
    """Targets where this package has a gap that `--ignore NAME:TARGET` accepts."""
    return [t for t in row.ignored if row.results[t] in (Status.SOURCE, Status.MISSING, Status.UNKNOWN)]


def cell(row: Row, target: Target) -> tuple[str, str]:
    """A table cell's text and color; a gap on a target `--ignore NAME:TARGET` ignores says so."""
    text, color = CELL[row.results[target]]
    if target in ignored_gaps(row):
        return f"{text} (ignored)", "dim"
    return text, color


def apart_from_ignored(rows: list[Row]) -> str:
    """', apart from 2 ignored gaps' for a clean run that only passes because of --ignore NAME:TARGET."""
    n = sum(len(ignored_gaps(r)) for r in rows)
    return f", apart from {n} ignored gap{'s' * (n != 1)}" if n else ""


def _input_key(row: Row, index: int) -> tuple[int, int]:
    """Identity of the input requirement a row came from (ranges resolved per Python share one)."""
    return (0, id(row.input_package)) if row.input_package is not None else (1, index)


def _group_rows(rows: list[Row]) -> list[list[Row]]:
    """Group resolved rows by their input requirement, preserving first-seen order."""
    groups: dict[tuple[int, int], list[Row]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(_input_key(row, i), []).append(row)
    return list(groups.values())


def hidden_line(hidden: list[Row], shown: list[Row] | None = None) -> str:
    """'3 packages with wheels for every target hidden', not counting those with only ignored gaps as such.

    A hidden release of a range whose other release is shown (split per Python) isn't another package: it's
    counted as an other release of a package with gaps, so the line doesn't contradict the gap summary.
    """
    shown_inputs = {id(r.input_package) for r in shown or [] if r.input_package is not None}
    rest = [r for r in hidden if r.input_package is None or id(r.input_package) not in shown_inputs]
    others = len(hidden) - len(rest)
    of = len({id(r.input_package) for r in hidden if r.input_package is not None} & shown_inputs)
    groups = _group_rows(rest)
    only_ignored = sum(1 for group in groups if any(ignored_gaps(r) for r in group))
    wheels = len(groups) - only_ignored
    parts = []
    if wheels:
        parts.append(f"{wheels} package{'s' * (wheels != 1)} with wheels for every target")
    if only_ignored:
        noun = "" if wheels else f" package{'s' * (only_ignored != 1)}"
        parts.append(f"{only_ignored}{noun} with only ignored gaps")
    if others:
        parts.append(
            f"{others} other release{'s' * (others != 1)} of {'a package' if of == 1 else 'packages'} with gaps"
        )
    return " and ".join(parts) + " hidden"


GAP_STATUSES = (Status.SOURCE, Status.MISSING, Status.UNKNOWN)


def heading(target: Target, multi_py: bool, advisory: frozenset[Target]) -> str:
    """A target's column heading: its label, marked when --advisory makes its gaps not fail the run."""
    label = target.label(multi_py)
    return f"{label} (advisory)" if target in advisory else label


def required_statuses(row: Row, advisory: frozenset[Target]) -> list[Status]:
    """The results that can fail the run: every target's except ignored (NAME:TARGET) and advisory ones."""
    return [s for t, s in row.results.items() if t not in row.ignored and t not in advisory]


def advisory_line(rows: list[Row], targets: list[Target], advisory: frozenset[Target]) -> str:
    """'4 gaps in 2 packages on advisory targets win-arm64, 3.14t linux-x86_64 don't fail the run.', or ''.

    It counts cells (a package with gaps on two advisory targets has two), so it names the packages too: the gap
    summary above it counts packages.
    """
    multi_py = len({t.python for t in targets}) > 1
    cells = [
        (i, t)
        for i, r in enumerate(rows)
        for t in targets
        if t in advisory and t not in r.ignored and r.results[t] in GAP_STATUSES
    ]
    if not cells:
        return ""
    labels = [t.label(multi_py) for t in targets if any(t is cell_target for _, cell_target in cells)]
    n = len(cells)
    packages = len({_input_key(rows[i], i) for i, _ in cells})
    return (
        f"{n} gap{'s' * (n != 1)} in {packages} package{'s' * (packages != 1)} on advisory "
        f"target{'s' * (len(labels) != 1)} {', '.join(labels)} {'do' if n != 1 else 'does'}n't fail the run."
    )


# -t names that stand for several platforms (see resolve_platforms): as --advisory values they mean the checked ones.
PLATFORM_GROUPS = ("all", "everything", "mobile")


def resolve_advisory(values: list[str], how: str, docker_musl: bool, checked: list[str]) -> tuple[list[str], list[str]]:
    """The checked platforms --advisory (or advisory = [...]) names, and the entries that name none of them.

    A single target (a name, an alias, a Docker name) must be checked itself. A group such as all stands for the
    checked targets among its members, so `-t linux-x86_64,win-arm64 --advisory all` marks both; it names none
    only when no member is checked.
    """
    platforms: list[str] = []
    unchecked: list[str] = []
    for value in values:
        for entry in (part.strip() for part in value.split(",")):
            if not entry:
                continue
            try:
                members = resolve_platforms([musl_docker_names(entry) if docker_musl else entry])
            except TargetError as e:
                raise TargetError(f"{how}: {e}") from None
            hits = [p for p in members if p in checked]
            if hits and (entry.lower() in PLATFORM_GROUPS or len(hits) == len(members)):
                platforms += hits
            else:
                unchecked.append(entry)
    return list(dict.fromkeys(platforms)), unchecked


def gap_name(row: Row) -> str:
    """The table's package cell: the name, plus `(via direct-dep +N)` for a transitive gap.
    JSON and quiet output keep the bare name."""
    if row.ok:
        return row.package.name
    direct: dict[str, str] = {}
    for target, status in row.results.items():
        if target not in row.ignored and status in (Status.SOURCE, Status.MISSING, Status.UNKNOWN):
            for name in row.direct_via.get(target, ()):
                direct.setdefault(canonicalize_name(name), name)
    if not direct:
        return row.package.name
    first = next(iter(direct.values()))
    extra = f" +{len(direct) - 1}" if len(direct) > 1 else ""
    return f"{row.package.name} (via {first}{extra})"


def render_table(
    rows: list[Row], targets: list[Target], style: Style, advisory: frozenset[Target] = frozenset()
) -> list[str]:
    multi_py = len({t.python for t in targets}) > 1
    headers = ["package", "version"] + [heading(t, multi_py, advisory) for t in targets]
    body = []
    for r in rows:
        cells = [gap_name(r), r.package.version or "?"]
        cells += [cell(r, t)[0] for t in targets]
        body.append(cells)
    widths = [max(len(h), *(len(c[i]) for c in body)) for i, h in enumerate(headers)]

    def line(cells: list[str], colors: list[str] | None = None) -> str:
        parts = []
        for i, c in enumerate(cells):
            padded = c.ljust(widths[i])
            if colors and colors[i]:
                padded = getattr(style, colors[i])(padded)
            parts.append(padded)
        return "  ".join(parts).rstrip()

    out = [style.bold(line(headers))]
    for r, cells in zip(rows, body, strict=True):
        colors = ["", "dim"] + [cell(r, t)[1] for t in targets]
        out.append(line(cells, colors))
    return out


def gap_targets(row: Row, targets: list[Target]) -> list[Target]:
    return [t for t in targets if row.results[t] in (Status.SOURCE, Status.MISSING) and t not in row.ignored]


def traced_targets(row: Row, targets: list[Target]) -> list[Target]:
    """The gap targets plus those where the package couldn't be looked up (unknown): what --json --explain's `via`
    and `direct` trace to the user's own requirements (#453)."""
    return [t for t in targets if row.results[t] in HINTED and t not in row.ignored]


RowNotes = dict[int, list[tuple[tracker.Note, list[Target]]]]
"""The tracker's notes that apply to a row's gaps, keyed by id(row), each with the gap targets it covers."""
RowBuilds = dict[int, list[tuple[Target, tracker.Build]]]
"""The tracker's build lab results for a row's `source` cells, keyed by id(row): one per target at most, for exactly
the checked release."""
Suggestions = dict[int, release_suggest.Suggestion]
"""--suggest: by id() of a gap row's package that was looked up, the release it names for each gap target that has
one (see release_suggest.suggest)."""


def notes_eligible(
    row: Row, targets: list[Target], source_of: dict[int, Source], private: frozenset[str] = frozenset()
) -> bool:
    """Whether --notes would look this row's package up: it has a gap on a platform the tracker scans, and it's
    positively a PyPI package (tracker.may_send). `source_of` maps id() of each package to the input it was read
    from; `private` holds the names some input of the run places elsewhere (origin.private_names)."""
    source = source_of.get(id(row.package))
    return (
        source is not None
        and any(t.platform in tracker.PLATFORMS for t in gap_targets(row, targets))
        and tracker.may_send(row.package, source, private)
    )


def tracker_notes(
    rows: list[Row],
    targets: list[Target],
    source_of: dict[int, Source],
    read_cache: bool,
    write_cache: bool,
    private: frozenset[str] = frozenset(),
) -> tuple[RowNotes, RowBuilds, str | None]:
    """--explain --notes: the coverage tracker's notes and build lab results for each row with a gap, and why the
    lookups were cut short, if they were (see tracker.lookup).

    Only names positively from PyPI are looked up (notes_eligible). A note is kept for the gap targets on its
    platforms (and its Pythons, if it lists any: Note.covers), and only when it's about the checked release
    (Note.applies_to): the tracker checked its latest release, which may not be yours. A build lab result is kept for
    a `source` cell only when it's for exactly that target and exactly the checked version (Build.covers).
    """
    eligible = [r for r in rows if notes_eligible(r, targets, source_of, private)]
    found, built, cut = tracker.lookup([r.package.key for r in eligible], read_cache, write_cache)
    out: RowNotes = {}
    lab: RowBuilds = {}
    for r in eligible:
        if r.package.key in found:
            gaps = gap_targets(r, targets)
            matched = [(note, [t for t in gaps if note.covers(t)]) for note in found[r.package.key]]
            if kept := [(note, ts) for note, ts in matched if ts and note.applies_to(r.package.version)]:
                out[id(r)] = kept
        results = built.get(r.package.key, [])
        if hits := [(t, b) for t in source_targets(r, targets) for b in results if b.covers(t, r.package.version)]:
            lab[id(r)] = hits
    return out, lab, cut


def source_targets(row: Row, targets: list[Target]) -> list[Target]:
    """The targets where --explain explains a `source` cell of this row (a gap that isn't ignored)."""
    return [t for t in gap_targets(row, targets) if row.results[t] == Status.SOURCE]


def render_explain(
    rows: list[Row],
    targets: list[Target],
    style: Style,
    conda_forge: dict[int, condaforge.Hint] | None = None,
    notes: RowNotes | None = None,
    build_info: dict[int, BuildInfo] | None = None,
    suggestions: Suggestions | None = None,
    builds: RowBuilds | None = None,
    issues_urls: dict[int, str | None] | None = None,
    published_wheels: list[gapwheels.Fill] | None = None,
    pi_wheels: dict[int, piwheels.Hint] | None = None,
) -> list[str]:
    """`conda_forge`: --conda-forge hints, `piwheels`: --piwheels hints, `build_info`: --build-info answers and
    `suggestions`: --suggest's, by id() of the row's package. `notes` and `builds` (the tracker's build lab results):
    --notes, by id() of the row. `published_wheels`: --fix's fills (from the gap wheel list, which may come from the
    24-hour cache), shown once per release under its first filled target."""
    multi_py = len({t.python for t in targets}) > 1
    filled: dict[int, list[gapwheels.Fill]] = {}
    for fill in published_wheels or []:
        filled.setdefault(id(fill.row), []).append(fill)
    out = []
    for r in rows:
        files = r.package.files or []
        info = (build_info or {}).get(id(r.package))
        sources = source_targets(r, targets)
        needs = describe(info, sources) if info and sources else None
        ask = (issues_urls or {}).get(id(r.package))  # once per release, under its first gap
        gap_wheel = filled.get(id(r))  # once per release, under its first filled target
        for t in gap_targets(r, targets):
            cell, color = CELL[r.results[t]]
            head = f"{r.package.name} {r.package.version or '?'} on {t.label(multi_py)}:"
            out.append(f"{style.bold(head)} {getattr(style, color)(cell)}")
            path = r.via.get(t, [])
            if len(path) >= 2:  # a lone name says nothing about why the package is there
                out.append(f"  via: {' → '.join(path)}")
            if needs and r.results[t] == Status.SOURCE:  # once per release, under its first source cell
                out.append(f"  build needs: {needs}")
                needs = None
            if r.package.install_policy:
                out.append(f"  install policy: {r.package.install_policy}")
            lines = explain_lines(
                files,
                t,
                yanked=r.package.yanked,
                requires_python_rejected=r.package.requires_python_rejected,
                limit=EXPLAIN_LIMIT,
                dim=style.dim,
                align=True,
                no_binary=r.package.no_binary,
                only_binary=r.package.only_binary,
            )
            out += [f"  {line}" for line in lines]
            if ask:
                out.append(f"  ask for a wheel: {ask}")
                ask = None
            if gap_wheel and any(f.target == t for f in gap_wheel):
                out += [f"  {line}" for line in gapwheels.explain_lines(gap_wheel, multi_py)]
                gap_wheel = None
            out.append("")
        if conda_forge and (h := conda_forge.get(id(r.package))):
            out += [condaforge.hint_text(r.package, h, multi_py), ""]
        if pi_wheels and (pw := pi_wheels.get(id(r.package))):
            out += [piwheels.hint_text(r.package, pw, multi_py), ""]
        for note, ts in (notes or {}).get(id(r), []):
            # Printed as is: a note is one line of plain text (tracker.parse). It can't hold a GitHub Actions workflow
            # command either: tracker.parse drops a note with "##[" or a word starting with "::", and the text never
            # starts a line, since it follows "  - " (the runner only reads "::" at the start of a line).
            platforms = ", ".join(dict.fromkeys(t.platform for t in ts))
            head = f"{r.package.name} {r.package.version or '?'}: note from the wheelgap tracker for {platforms}"
            out.append(f"{style.bold(head)} {style.dim(f'({note.kind}, checked {note.updated})')}")
            out.append(f"  - {note.text}")
            out += [f"    {link}" for link in note.links]
            out.append("")
        if lab := (builds or {}).get(id(r), []):
            # Every part is from a fixed vocabulary or a strict pattern (tracker.Build), so it's printed as is.
            for t, b in lab:
                head = f"{r.package.name} {r.package.version or '?'} on {t.label(multi_py)}:"
                where = style.dim(f"({b.date}, {b.runner}, Python {b.python})")
                out.append(f"{style.bold(head)} build lab {where}: {b.verdict()}")
            out.append("")
        if suggestions is not None and (found := suggestions.get(id(r.package))) is not None:
            gaps = gap_targets(r, targets)
            out += [*release_suggest.suggest_lines(r.package.name, r.package.version or "?", gaps, found, multi_py), ""]
    return out


def bundled_line(rows: list[Row], lock: PyodideLock) -> str:
    def one(r: Row) -> str:
        have, pkg = lock.packages[r.package.key], r.package
        if not bundled_version_differs(r.package, lock):
            return f"{pkg.name} {have}"
        return f"{pkg.name} {have} " + (
            f"(outside {pkg.spec})" if pkg.version_from_index else f"(you pinned {pkg.version})"
        )

    noun = "package comes" if len(rows) == 1 else "packages come"
    return f"{len(rows)} {noun} from Pyodide {lock.pyodide_version}'s own builds: " + ", ".join(map(one, rows))


_OK = (Status.WHEEL, Status.NOT_NEEDED, Status.BUNDLED)


@dataclass(frozen=True)
class Blocking:
    """What blocks the move to the newer of two checked Pythons (see blocking_newer_python)."""

    older: str
    newer: str
    names: list[str]  # packages with a gap on the newer Python only
    unchecked: int  # other packages that couldn't be checked on the newer Python, so may block it too


def blocking_newer_python(rows: list[Row], targets: list[Target]) -> Blocking | None:
    """With exactly two Pythons checked, one newer: the packages that block the upgrade.

    It's decided per package name, over every row (input or forked version) of it: a package blocks the upgrade
    when, on some platform, the newer Python has a gap (source or missing) and every result for the older Python is
    a wheel, n/a or bundled. Gaps the older Python has too and ignored targets don't count. A package that is unknown
    on the newer Python isn't listed, but is counted in `unchecked`, since it may block the upgrade too.
    """
    by_python: dict[str, dict[str, Target]] = {}
    for t in targets:
        by_python.setdefault(t.python, {})[t.platform] = t
    if len(by_python) != 2:
        return None
    older, newer = sorted(by_python.values(), key=lambda ts: next((t.major, t.minor) for t in ts.values()))
    old, new = next(iter(older.values())), next(iter(newer.values()))
    if (old.major, old.minor) == (new.major, new.minor):  # 3.14 and 3.14t: neither is newer
        return None
    by_name: dict[str, list[Row]] = {}
    for r in rows:
        by_name.setdefault(r.package.key, []).append(r)
    names, unchecked = [], 0
    for group in by_name.values():

        def results(t: Target, group: list[Row] = group) -> list[Status]:
            return [r.results[t] for r in group if t not in r.ignored]

        if any(
            any(s in (Status.SOURCE, Status.MISSING) for s in results(t)) and all(s in _OK for s in results(older[p]))
            for p, t in newer.items()
        ):
            names.append(group[0].package.name)
        elif any(Status.UNKNOWN in results(t) for t in newer.values()):
            unchecked += 1
    return Blocking(old.python, new.python, names, unchecked)


def blocking_line(blocking: Blocking) -> str:
    """'blocking 3.15: numpy, pydantic-core' (see blocking_newer_python)."""
    n = blocking.unchecked
    unchecked = f"{n} package{'s' * (n != 1)} couldn't be checked" if n else ""
    if blocking.names:
        return f"blocking {blocking.newer}: {', '.join(blocking.names)}" + (f" ({unchecked})" if n else "")
    if n:
        return f"blocking {blocking.newer}: none known ({unchecked})"
    return f"blocking {blocking.newer}: none (every gap on {blocking.newer} is on {blocking.older} too)"


def report_json(
    rows: list[Row],
    targets: list[Target],
    sources: list[str],
    skipped: list,
    ignored: list[tuple[str, list[str] | None]],
    explained: bool,
    pyodide_lock: PyodideLock | None = None,
    advisory: frozenset[Target] = frozenset(),
    conda_forge: dict[int, condaforge.Hint] | None = None,
    notes: RowNotes | None = None,
    build_info: dict[int, BuildInfo] | None = None,
    suggestions: Suggestions | None = None,
    builds: RowBuilds | None = None,
    dropped: dict[str, str] | None = None,
    issues_urls: dict[int, str | None] | None = None,
    gap_wheels: list[gapwheels.Fill] | None = None,
    pi_wheels: dict[int, piwheels.Hint] | None = None,
) -> str:
    multi_py = len({t.python for t in targets}) > 1
    gaps = [r for r in rows if not r.ok]
    blocking = blocking_newer_python(rows, targets)

    def package(r: Row) -> dict:
        out: dict[str, object] = {
            "name": r.package.name,
            "version": r.package.version,
            "error": r.package.error,
            "results": {t.label(multi_py): r.results[t].value for t in targets},
        }
        if pyodide_lock and Status.BUNDLED in r.results.values():
            out["bundled"] = pyodide_lock.packages[r.package.key]
            out["bundled_version_differs"] = bundled_version_differs(r.package, pyodide_lock)
        if r.package.pip_refuses:
            out["pip_refuses"] = r.package.pip_refuses
        if explained:
            out["rejected"] = {
                t.label(multi_py): [
                    {"file": f, "reason": why}
                    for f, why in explain(
                        r.package.files or [],
                        t,
                        r.package.yanked,
                        r.package.requires_python_rejected,
                        no_binary=r.package.no_binary,
                        only_binary=r.package.only_binary,
                    )
                ]
                for t in gap_targets(r, targets)
            }
            # The gap targets and those where the lookup failed (unknown), minus any without a path (inputs with no
            # dependency graph).
            out["via"] = {t.label(multi_py): r.via[t] for t in traced_targets(r, targets) if t in r.via}
            # The direct dependencies that lead to each of those targets, as the table's `(via ...)` names them; only
            # where the input says (uv.lock, Poetry 1.x, --deps), so `{}` for a direct dependency or an input without
            # a graph.
            out["direct"] = {
                t.label(multi_py): list(dict.fromkeys(r.direct_via[t]))
                for t in traced_targets(r, targets)
                if r.direct_via.get(t)
            }
            if gap_wheels is not None:
                out["gap_wheel"] = {
                    f.target.label(multi_py): gapwheels.explain_json(f) for f in gap_wheels if f.row is r
                }
            if issues_urls is not None:
                out["issues_url"] = issues_urls.get(id(r.package))
            if conda_forge and (h := conda_forge.get(id(r.package))):
                out["conda_forge"] = {
                    "name": h.name,
                    "version": h.version,
                    "builds": {t.label(multi_py): subdirs for t, subdirs in h.builds.items()},
                }
            if pi_wheels and (pw := pi_wheels.get(id(r.package))):
                out["piwheels"] = {
                    "name": pw.name,
                    "version": pw.version,
                    "wheels": {t.label(multi_py): ws for t, ws in pw.wheels.items()},
                    "apt": pw.apt,
                }
            # The tracker's notes for this package's gaps, each with the gap targets it covers.
            out["notes"] = [
                {**note.as_json(), "targets": [t.label(multi_py) for t in ts]}
                for note, ts in (notes or {}).get(id(r), [])
            ]
            # The tracker's build lab results for this package's source cells, by target label.
            out["build_lab"] = {
                t.label(multi_py): {**b.as_json(), "verdict": b.verdict()} for t, b in (builds or {}).get(id(r), [])
            }
            if build_info is not None:  # --build-info: what the sdist requires, on the targets that build it
                info, sources = build_info.get(id(r.package)), source_targets(r, targets)
                out["build_requires"] = [shown(q) for q in requires_for(info, sources)] if info and sources else None
            if suggestions is not None and (found := suggestions.get(id(r.package))) is not None:
                # --suggest: by gap target, the release it names; a target with no newer wheel is left out.
                out["suggest"] = {
                    t.label(multi_py): found.found[t] for t in gap_targets(r, targets) if t in found.found
                }
                # By gap target no newer release has a wheel for, the newest older release that has one.
                out["last_with_wheel"] = {
                    t.label(multi_py): found.last[t] for t in gap_targets(r, targets) if t in found.last
                }
        return out

    report: dict[str, object] = {
        "version": __version__,
        "sources": sources,
        "targets": [{"label": t.label(multi_py), "python": t.python, "platform": t.platform} for t in targets],
        "packages": [package(r) for r in rows],
        "skipped": [{"name": p.name, "reason": p.skip} for p in skipped],
        "ignored": [name for name, platforms in ignored if platforms is None],
        "ignored_targets": [
            {"name": name, "platforms": platforms} for name, platforms in ignored if platforms is not None
        ],
        "advisory": [t.label(multi_py) for t in targets if t in advisory],
        # --dist: targets the last release had a wheel for that this one drops, {label: last version}. Absent
        # when --dist didn't run or the comparison couldn't run (a first release has {} instead).
        **({"dropped": dropped} if dropped is not None else {}),
        "summary": {
            "packages": len(_group_rows(rows)),
            "with_gaps": len(_group_rows(gaps)),
            "per_target": target_counts(gaps, targets),
        },
    }
    if blocking is not None:
        report["blocking_newer_python"] = blocking.names
    if gap_wheels is not None:  # --fix: one entry per gap cell a wheel on the gap wheel index fills
        report["gap_wheels"] = gapwheels.to_json(gap_wheels, multi_py)
    return json.dumps(report, indent=2)


def libc_versions(args: argparse.Namespace, config: dict) -> tuple[str, str]:
    """CLI libc > CLI distro > Dockerfile/config libc > config distro > built-ins."""
    defaults = {}
    if "distro" in config:
        key, version = resolve_distro(config["distro"])  # validate it even when --distro wins
        if args.distro is None:
            defaults[key] = version
    defaults.update(config)
    if args.distro is not None:
        key, version = resolve_distro(args.distro)
        defaults[key] = version
    return args.glibc or defaults.get("glibc", DEFAULT_GLIBC), args.musl or defaults.get("musl", DEFAULT_MUSL)


def print_target_list(args: argparse.Namespace, config: dict) -> None:
    aliases: dict[str, list[str]] = {name: [] for name in PLATFORMS}
    for alias, name in ALIASES.items():
        aliases[name].append(alias)

    glibc_version, musl_version = libc_versions(args, config)
    versions = {
        "manylinux": ("glibc", glibc_version),
        "musllinux": ("musl", musl_version),
        "macos": ("macOS", args.macos or config.get("macos", DEFAULT_MACOS)),
    }
    limits = {}
    for family, (label, version) in versions.items():
        major, minor = parse_version_pair(version, label)
        limits[family] = f", {label} <= {major}.{minor}"
    major, minor = parse_ios_version(args.ios or config.get("ios", DEFAULT_IOS))
    limits["ios"] = f", iOS <= {major}.{minor}"
    pythons = ", ".join(f"{a}.{b}" for a, b in PYODIDE_PLATFORMS)
    limits["pyodide"] = f", Python {pythons} (not in -t all)"
    limits["ios"] += " (wheels only; -t mobile, not in -t all)"
    api = parse_api_level(args.android_api or config.get("android-api", DEFAULT_ANDROID_API))
    limits["android"] = f", API level <= {api} (wheels only; -t mobile, not in -t all)"

    glibc = parse_version_pair(versions["manylinux"][1], "glibc")
    if glibc[0] == MANYLINUX_FLOOR[0]:  # pip's 2.17 floor outside x86 (no manylinux before manylinux2014)
        floor = ".".join(map(str, MANYLINUX_FLOOR))
        below = f", no manylinux (glibc {glibc[0]}.{glibc[1]} is below the {floor} floor)"
        floored = f", glibc {floor} to {glibc[0]}.{glibc[1]}" if glibc >= MANYLINUX_FLOOR else below
    else:
        floored = limits["manylinux"]

    def accepted(family: str, arch: str) -> str:
        limit = floored if family == "manylinux" and arch not in MANYLINUX_X86_ARCHES else limits.get(family, "")
        return f"{family} {arch}{limit}{' (not in -t all)' * (arch in EXTRA_ARCHES)}"

    rows = [(name, aliases[name], accepted(family, arch)) for name, (family, arch) in PLATFORMS.items()]
    if args.json:
        print(
            json.dumps(
                [{"name": name, "aliases": names, "accepts": accepts} for name, names, accepts in rows], indent=2
            )
        )
        return

    table = [("name", "aliases", "accepts")]
    table.extend((name, ", ".join(names) or "-", accepts) for name, names, accepts in rows)
    name_width = max(len(name) for name, _, _ in table)
    alias_width = max(len(names) for _, names, _ in table)
    for name, names, accepts in table:
        print(f"{name:<{name_width}}  {names:<{alias_width}}  {accepts}")
    print("Docker platform names like linux/arm64 also work.")
    print("\nDistros (--distro supplies a libc version, not a target):")
    for name, (libc, version) in DISTROS.items():
        names = ", ".join(alias for alias, canonical in DISTRO_ALIASES.items() if canonical == name)
        print(f"  {name:<18} {libc} {version}" + (f" (also {names})" if names else ""))


def _worst_in(statuses: Iterable[Status]) -> Status:
    seen = set(statuses)
    for s in (Status.MISSING, Status.UNKNOWN, Status.SOURCE):
        if s in seen:
            return s
    return Status.WHEEL


def worst_status(row: Row) -> Status:
    """The status that best describes a gap package: can't install, then unknown, then from source."""
    return _worst_in(row.counted)


def gap_summary(gaps: list[Row], total: int, style: Style) -> str:
    # Each package is counted once, under its worst status across its rows, so the parts always add up to n_gaps.
    groups = _group_rows(gaps)
    worst = [_worst_in(s for r in group for s in r.counted) for group in groups]
    n_gaps = len(groups)
    source, missing, unknown = (worst.count(s) for s in (Status.SOURCE, Status.MISSING, Status.UNKNOWN))
    parts = []
    if source:
        parts.append(style.yellow(f"{source} will build from source"))
    if missing:
        parts.append(style.red(f"{missing} can't install at all"))
    if unknown:
        parts.append(style.dim(f"{unknown} unknown"))
    verb = "has" if n_gaps == 1 else "have"
    noun = "package" if total == 1 else "packages"
    return f"{style.bold(f'{n_gaps} of {total} {noun}')} {verb} gaps: " + ", ".join(parts) + "."


def target_counts(gaps: list[Row], targets: list[Target]) -> dict[str, dict[str, int]]:
    """Gap counts for every target (zeros included), keyed by target label. This is --json's summary.per_target.

    Text and --markdown show a subset of it (shown_target_counts), so the three formats can't disagree.
    """
    multi_py = len({target.python for target in targets}) > 1
    out = {}
    for target in targets:
        statuses = [row.results[target] for row in gaps if target not in row.ignored]
        out[target.label(multi_py)] = {
            s.value: statuses.count(s) for s in (Status.SOURCE, Status.MISSING, Status.UNKNOWN)
        }
    return out


def shown_target_counts(gaps: list[Row], targets: list[Target]) -> dict[str, dict[str, int]]:
    """The targets the text and Markdown summaries list: those with gaps, and none for a single target."""
    if len(targets) < 2:
        return {}
    return {label: counts for label, counts in target_counts(gaps, targets).items() if any(counts.values())}


def target_counts_text(counts: dict[str, int], style: Style) -> str:
    parts = []
    if source := counts["source"]:
        parts.append(style.yellow(f"{source} from source"))
    if missing := counts["missing"]:
        parts.append(style.red(f"{missing} missing"))
    if unknown := counts["unknown"]:
        parts.append(style.dim(f"{unknown} unknown"))
    return ", ".join(parts)


def shown_target_lines(
    gaps: list[Row], targets: list[Target], style: Style, advisory: frozenset[Target]
) -> list[tuple[str, str]]:
    """shown_target_counts as (heading, counts text) pairs, with advisory targets marked as in the table."""
    multi_py = len({t.python for t in targets}) > 1
    headings = {t.label(multi_py): heading(t, multi_py, advisory) for t in targets}
    per_target = shown_target_counts(gaps, targets)
    return [(headings[label], target_counts_text(counts, style)) for label, counts in per_target.items()]


def target_lines(
    gaps: list[Row], targets: list[Target], style: Style, advisory: frozenset[Target] = frozenset()
) -> list[str]:
    lines = shown_target_lines(gaps, targets, style, advisory)
    width = max((len(head) for head, _ in lines), default=0)
    return [f"{head:<{width}}  {text}" for head, text in lines]


_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~&])")


def _md(text: str) -> str:
    """Escape text so Markdown shows it literally (paths like ./__internal__/ stay as written)."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def _code(text: str) -> str:
    """A Markdown code span showing text exactly (paths and URLs stay readable and unlinked)."""
    if not text:
        return '`""`'  # `` isn't a code span, and would pair up with the backticks after it
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"  # only used outside tables, so | needs no escaping


def render_markdown(
    rows: list[Row],
    targets: list[Target],
    source_labels: list[str],
    skipped: list[Package],
    ignored: list[tuple[str, list[str] | None]],
    args: argparse.Namespace,
    pyodide_lock: PyodideLock | None,
    source_allowed: bool = False,
    advisory: frozenset[Target] = frozenset(),
    conda_forge: dict[int, condaforge.Hint] | None = None,
    notes: RowNotes | None = None,
    build_info: dict[int, BuildInfo] | None = None,
    builds: RowBuilds | None = None,
    *,
    suggestions: Suggestions | None = None,
    dropped: dict[str, str] | None = None,
    issues_urls: dict[int, str | None] | None = None,
    pi_wheels: dict[int, piwheels.Hint] | None = None,
    fix_hint: bool = False,
) -> list[str]:
    """The same report as the text output, as GitHub-flavoured Markdown (for a job summary or a PR comment)."""
    plain = Style(False)
    gaps = [r for r in rows if not r.ok]
    total_pkgs = len(_group_rows(rows))
    gap_pkgs = len(_group_rows(gaps))
    n_t = len(targets)
    if gaps:
        noun = "package" if total_pkgs == 1 else "packages"
        out = [f"### wheelgap: {gap_pkgs} of {total_pkgs} {noun} {'has' if gap_pkgs == 1 else 'have'} gaps", ""]
    elif not rows:  # "every package" reads like a success when the run checked nothing
        out = ["### wheelgap: no packages to check", ""]
    else:
        out = [
            f"### wheelgap: every package has a wheel for {'all ' if n_t > 1 else ''}{n_t} target{'s' * (n_t > 1)}"
            + apart_from_ignored(rows),
            "",
        ]
    pys = ", ".join(dict.fromkeys(t.python for t in targets))
    n_pkgs = f"{total_pkgs} package{'s' * (total_pkgs != 1)}"
    out += [f"wheelgap {__version__} · {' + '.join(map(_code, source_labels))} · {n_pkgs} · Python {pys}", ""]

    shown = rows if args.all else gaps
    if shown:
        multi_py = len({t.python for t in targets}) > 1
        headers = ["package", "version"] + [heading(t, multi_py, advisory) for t in targets]
        out.append("| " + " | ".join(_md(h) for h in headers) + " |")
        out.append("| " + " | ".join("---" for _ in headers) + " |")
        for r in shown:
            cells = [gap_name(r), r.package.version or "?"] + [cell(r, t)[0] for t in targets]
            out.append("| " + " | ".join(_md(c) for c in cells) + " |")
        out.append("")
        hidden = [] if args.all else [r for r in rows if r.ok]
        if hidden:
            out += [f"{hidden_line(hidden, shown)} (`--all` to show).", ""]

    if gaps:
        per_target = shown_target_lines(gaps, targets, plain, advisory)
        if per_target:
            out += [f"- {_md(head)}: {text}" for head, text in per_target]
            out.append("")
        out += [gap_summary(gaps, total_pkgs, plain), ""]
        if blocking := blocking_newer_python(rows, targets):
            out += [_md(blocking_line(blocking)), ""]
        if line := advisory_line(rows, targets, advisory):
            out += [_md(line), ""]
        if source_allowed:
            out += ["Source builds are allowed (fail-on missing), so this run passes.", ""]
    bundled = [r for r in rows if Status.BUNDLED in r.results.values()]
    if bundled and pyodide_lock:
        out += [_md(bundled_line(bundled, pyodide_lock)), ""]
    if skipped:
        names = skipped_list(skipped, _code)
        out += [f"Skipped {len(skipped)} non-index package{'s' * (len(skipped) != 1)}: {names}", ""]
    if ignored:
        out += [f"Ignored: {ignored_list(ignored, _code)}", ""]
    for r in rows:
        if r.package.error:
            out += [f"Could not check {_md(r.package.name)}: {_code(r.package.error)}", ""]
    if dropped:
        out += [
            f"- {_code(label)}: {_code(version)} had a wheel, this release has none"
            for label, version in dropped.items()
        ]
        out.append("")
    if args.explain and gaps:
        # Fenced, so wheel filenames, reasons and the tracker's notes need no escaping: inside a code block, Markdown
        # and HTML are shown as text. The fence is longer than any run of backticks in it, so nothing can close it.
        lines = render_explain(
            gaps,
            targets,
            plain,
            conda_forge,
            notes,
            build_info,
            builds=builds,
            issues_urls=issues_urls,  # inside the fence below, so shown exactly, with no escaping
            pi_wheels=pi_wheels,
        )
        fence = "`" * max([3, *(len(run) + 1 for line in lines for run in re.findall(r"`+", line))])
        out += ["<details><summary>Why (rejected wheels)</summary>", "", fence, *lines, fence, "", "</details>", ""]
        if suggestions:
            multi_py = len({t.python for t in targets}) > 1
            sug_lines = []
            for r in gaps:
                if (found := suggestions.get(id(r.package))) is not None:
                    g = gap_targets(r, targets)
                    sug_lines += [
                        f"- {_md(ln)}"
                        for ln in release_suggest.suggest_lines(
                            r.package.name, r.package.version or "?", g, found, multi_py
                        )
                    ]
            if sug_lines:
                out += [*sug_lines, ""]
    if fix_hint:
        out += [FIX_HINT, ""]
    return out


def ignored_list(ignored: list[tuple[str, list[str] | None]], name: Callable[[str], str] = str) -> str:
    """psycopg2, numpy (musl-x86_64): ignored packages, with the platforms of those ignored on some targets only."""
    return ", ".join(name(n) + (f" ({', '.join(platforms)})" if platforms else "") for n, platforms in ignored)


def split_ignore(item: str, how: str, docker_musl: bool) -> tuple[str, list[str] | None]:
    """An --ignore NAME or NAME:TARGET: the name, and the platforms TARGET names (None: every target).

    TARGET takes what -t does, resolved the same way: with an Alpine --dockerfile, linux/arm64 means musl.
    """
    name, colon, target = (part.strip() for part in item.partition(":"))
    if not colon:
        return name, None
    if not name or not target:
        raise TargetError(f"{how} {item!r}: expected NAME or NAME:TARGET")
    try:
        return name, resolve_platforms([musl_docker_names(target) if docker_musl else target])
    except TargetError as e:
        raise TargetError(f"{how} {item!r}: {e}") from None


def split_ignore_value(value: str, how: str, docker_musl: bool) -> list[tuple[str, str, list[str] | None]]:
    """The comma-separated entries of one --ignore value, as (entry, name, platforms).

    The value is split on commas first, so `psycopg2:linux,musl` would ignore a package named musl everywhere. A
    plain entry after a NAME:TARGET one that names a target is far more likely a second target, so it's an error.
    """
    out: list[tuple[str, str, list[str] | None]] = []
    after_target = None
    for entry in (part.strip() for part in value.split(",")):
        if not entry:
            continue
        name, platforms = split_ignore(entry, how, docker_musl)
        if platforms is not None:
            after_target = name
        elif after_target is not None:
            try:
                resolve_platforms([musl_docker_names(entry) if docker_musl else entry])
            except TargetError:
                pass  # a package name
            else:
                raise TargetError(
                    f"{how} {value!r}: {entry!r} is a target, but each NAME:TARGET takes one target; "
                    f"write {after_target}:{entry} (a package named {entry} goes in its own {how} value)"
                )
        out.append((entry, name, platforms))
    return out


def input_error_json(e: Exception) -> int:
    """With --json, also put an input error on stdout so scripts don't have to parse stderr."""
    print(json.dumps({"error": {"message": str(e), "type": type(e).__name__}}))
    return 2


def selected_packages(source: Source, targets: list[Target]) -> list[Package]:
    if source.uv_selection is None:
        return source.packages
    selected = set().union(*(reachable(source, target) for target in targets))
    return [package for index, package in enumerate(source.packages) if index in selected]


def refuse_pyodide_url(spec: str, how: str = "--offline") -> None:
    """--offline: a pyodide-lock.json URL would be downloaded, so ask for a local copy instead."""
    if "://" in spec:
        raise PyodideLockError(f"{how}: {spec} would be downloaded; save it and pass its path, or {undo_offline(how)}")


def unpinned_requirement_names(source: Source) -> list[str]:
    """Index requirements that aren't an exact pin, once each. Skipped and non-index lines are omitted, and so are
    packages with an error (an unusable constraint), since nothing was checked for them."""
    names: dict[str, str] = {}
    for package in source.packages:
        if package.skip or package.error:
            continue
        if package.version is None or package.spec:
            names.setdefault(package.key, package.name)
    return list(names.values())


def note_unpinned_requirements(sources: list[Source]) -> None:
    for source in sources:
        if not source.requirements:
            continue
        names = unpinned_requirement_names(source)
        if not names:
            continue
        noun = "package" if len(names) == 1 else "packages"
        listed = ", ".join(names[:2]) + (", …" if len(names) > 2 else "")
        print(
            f"wheelgap: note: {source.label} has {len(names)} unpinned {noun} ({listed}), "
            "so the newest matching release was checked; pin them (uv pip compile, pip-compile) "
            "to check what you install",
            file=sys.stderr,
        )


def _names(names: list[str]) -> str:
    return ", ".join(names[:3]) + (", …" if len(names) > 3 else "")


def conda_forge_hints(
    gaps: list[Row], targets: list[Target], sources: list[Source], index_url: str | None, quiet: bool, cached: dict
) -> dict[int, condaforge.Hint]:
    """--conda-forge: look the gap packages up on anaconda.org and return their hints, by id() of the package. Only
    names that are positively PyPI's are sent, and none that another input of the run places elsewhere
    (origin.private_names); notes on stderr say which packages weren't looked up, and why."""
    other = origin.other_index(index_url)
    elsewhere = origin.private_names(sources, simple_index=index_url)
    if other is not None and not quiet:
        print(
            "wheelgap: note: --conda-forge looks nothing up when packages come from another index "
            f"({other}), so no package names leave it",
            file=sys.stderr,
        )
    source_of = {id(p): s for s in sources for original in s.packages for p in original.variants}
    asked: list[Row] = []
    kept_in: dict[str, str] = {}  # by canonical name, so two spellings of one package count once
    for r in gaps:
        if r.package.version and condaforge.wanted(gap_targets(r, targets)):
            if origin.known_pypi(r.package, source_of[id(r.package)], simple_index=index_url, private=elsewhere):
                asked.append(r)
            else:
                kept_in.setdefault(r.package.key, r.package.name)
    if kept_in and other is None and not quiet:
        private = list(kept_in.values())
        print(
            f"wheelgap: note: --conda-forge didn't look up {len(private)} package{'s' * (len(private) != 1)} "
            f"whose index isn't known to be PyPI ({_names(private)}), so their names stay private",
            file=sys.stderr,
        )
    lookups = condaforge.look_up_all([(r.package.key, r.package.version or "") for r in asked], **cached)
    if lookups.unchecked and not quiet:
        n = len(lookups.unchecked)
        which = _names([f"{name} ({why})" for name, why in lookups.unchecked.items()])
        print(
            f"wheelgap: note: --conda-forge couldn't check {n} package{'s' * (n != 1)}: {which}; "
            "no hint doesn't mean conda-forge has no build",
            file=sys.stderr,
        )
    hints = {}
    for r in asked:
        if h := condaforge.hint(r.package, gap_targets(r, targets), lookups.found):
            hints[id(r.package)] = h
    return hints


def piwheels_hints(
    gaps: list[Row], targets: list[Target], sources: list[Source], index_url: str | None, quiet: bool, cached: dict
) -> dict[int, piwheels.Hint]:
    """--piwheels: look the gap packages up on piwheels.org and return their hints, by id() of the package. Only
    names that are positively PyPI's are sent, and none that another input of the run places elsewhere
    (origin.private_names); notes on stderr say which packages weren't looked up, and why."""
    other = origin.other_index(index_url)
    elsewhere = origin.private_names(sources, simple_index=index_url)
    if other is not None and not quiet:
        print(
            "wheelgap: note: --piwheels looks nothing up when packages come from another index "
            f"({other}), so no package names leave it",
            file=sys.stderr,
        )
    source_of = {id(p): s for s in sources for original in s.packages for p in original.variants}
    asked: list[Row] = []
    kept_in: dict[str, str] = {}  # by canonical name, so two spellings of one package count once
    for r in gaps:
        if r.package.version and piwheels.wanted(gap_targets(r, targets)):
            if origin.known_pypi(r.package, source_of[id(r.package)], simple_index=index_url, private=elsewhere):
                asked.append(r)
            else:
                kept_in.setdefault(r.package.key, r.package.name)
    if kept_in and other is None and not quiet:
        private = list(kept_in.values())
        print(
            f"wheelgap: note: --piwheels didn't look up {len(private)} package{'s' * (len(private) != 1)} "
            f"whose index isn't known to be PyPI ({_names(private)}), so their names stay private",
            file=sys.stderr,
        )
    lookups = piwheels.look_up_all([(r.package.key, r.package.version or "") for r in asked], **cached)
    if lookups.unchecked and not quiet:
        n = len(lookups.unchecked)
        which = _names([f"{name} ({why})" for name, why in lookups.unchecked.items()])
        print(
            f"wheelgap: note: --piwheels couldn't check {n} package{'s' * (n != 1)}: {which}; "
            "no hint doesn't mean piwheels has no wheel",
            file=sys.stderr,
        )
    hints = {}
    for r in asked:
        if h := piwheels.hint(r.package, gap_targets(r, targets), lookups.found):
            hints[id(r.package)] = h
    return hints


def version_suggestions(
    gaps: list[Row], targets: list[Target], sources: list[Source], index_url: str | None, quiet: bool
) -> Suggestions:
    """--suggest: look the pinned gap packages' releases up on PyPI, one request per name, and return what to move to,
    by id() of the package. Only names that are positively PyPI's are looked up, by the rule --conda-forge follows
    (origin.known_pypi), and nothing in a run that uses another index; notes on stderr say which weren't, and why."""
    other = origin.other_index(index_url)
    if other is not None:
        if not quiet:
            print(
                f"wheelgap: note: --suggest looks nothing up when packages come from another index ({other})",
                file=sys.stderr,
            )
        return {}
    elsewhere = origin.private_names(sources, simple_index=index_url)
    source_of = {id(p): s for s in sources for original in s.packages for p in original.variants}
    asked: dict[str, list[Row]] = {}  # by canonical name: one lookup per package, however many rows it has
    kept_in: dict[str, str] = {}
    for r in gaps:
        # Under --no-binary, pip uses no release's wheels, so a newer one with wheels wouldn't close the gap.
        if r.package.version and not r.package.no_binary and gap_targets(r, targets):
            if origin.known_pypi(r.package, source_of[id(r.package)], simple_index=index_url, private=elsewhere):
                asked.setdefault(r.package.key, []).append(r)
            else:
                kept_in.setdefault(r.package.key, r.package.name)
    if kept_in and not quiet:
        private = list(kept_in.values())
        print(
            f"wheelgap: note: --suggest didn't look up {len(private)} package{'s' * (len(private) != 1)} "
            f"whose index isn't known to be PyPI ({_names(private)}), so their names stay private",
            file=sys.stderr,
        )
    # A suggestion must keep a wheel on every target where this row (this resolution, on its Pythons) has one now.
    found, failed = release_suggest.look_up_all(
        {
            key: [
                (r.package.version or "", gap_targets(r, targets), [t for t in targets if r.results[t] is Status.WHEEL])
                for r in rows
            ]
            for key, rows in asked.items()
        }
    )
    if failed and not quiet:
        n = len(failed)
        which = _names([f"{asked[key][0].package.name} ({why})" for key, why in failed.items()])
        print(
            f"wheelgap: note: --suggest couldn't look up {n} package{'s' * (n != 1)}: {which}; "
            "no suggestion doesn't mean no newer release has wheels",
            file=sys.stderr,
        )
    return {
        id(r.package): s for key, rows in asked.items() if key in found for r, s in zip(rows, found[key], strict=True)
    }


FIX_HINT = "Run with --fix to see whether wheelgap's gap wheel index has a wheel for these."


def gap_wheel_candidates(
    gaps: list[Row], targets: list[Target], sources: list[Source], index_url: str | None
) -> tuple[list[Row], list[Row], dict[int, Source], frozenset[str]]:
    """Rows that could use a gap wheel, without reading the gap wheel list."""
    private = origin.private_names(sources, simple_index=index_url)
    source_of = {id(p): s for s in sources for original in s.packages for p in original.variants}
    candidates = [r for r in gaps if gapwheels.candidates(r, targets)]
    wanted = [r for r in candidates if gapwheels.eligible(r, source_of[id(r.package)], index_url, private)]
    return candidates, wanted, source_of, private


def gap_wheel_fills(
    gaps: list[Row],
    targets: list[Target],
    sources: list[Source],
    index_url: str | None,
    index_how: str,
    quiet: bool,
    *,
    read_cache: bool = True,
    write_cache: bool = True,
) -> list[gapwheels.Fill]:
    """--fix: the gap cells a wheel on wheelgap's gap wheel index fills. The list is read in one request that names no
    package, only when a gap of a package that is positively PyPI's could use it (gapwheels.eligible), and never when
    packages come from another index. Notes on stderr say when it wasn't read, couldn't be, or nothing fits."""
    other = origin.other_index(index_url)
    if other is not None:
        if not quiet:
            how = index_how if other == "--index-url" else other
            print(
                f"wheelgap: note: --fix is off with {how}: packages may come from another index, "
                "so the gap wheel list wasn't read",
                file=sys.stderr,
            )
        return []
    candidates, wanted, source_of, private = gap_wheel_candidates(gaps, targets, sources, index_url)
    # Only those the origin rule turned away: a --no-binary package wouldn't use a wheel anyway.
    unchecked = [
        r.package.name
        for r in candidates
        if r not in wanted and r.package.version is not None and not r.package.no_binary
    ]
    if unchecked and not quiet:
        # Without it, a run that couldn't check these would read as "no gap wheel fits them".
        names = ", ".join(dict.fromkeys(unchecked))
        print(
            f"wheelgap: note: --fix didn't check {names}: wheelgap can't tell they come from PyPI "
            "(see how-it-works.md#pypi-origin), so the gap wheel index isn't suggested for them",
            file=sys.stderr,
        )
    fills: list[gapwheels.Fill] = []
    if wanted:
        wheels = gapwheels.look_up(read_cache=read_cache, write_cache=write_cache)
        if wheels is None:
            # Even with -q: without it, a run that couldn't look would read as "no gap wheel fits".
            print("wheelgap: note: couldn't read the gap wheel list, so --fix has no suggestions", file=sys.stderr)
            return []
        fills = gapwheels.fillable(wanted, targets, source_of, wheels, private, index_url)
        if not fills and not quiet:
            print("wheelgap: note: --fix: no wheel on the gap wheel index fills these gaps", file=sys.stderr)
    return fills


def main(argv: list[str] | None = None, *, project: Path | None = None) -> int:
    """The command line. `project`: the folder whose pyproject.toml and .python-version apply when no input names one
    (the MCP server's check_dist passes the dist folder's project, not the server's working directory)."""
    for stream in (sys.stdout, sys.stderr):
        # Don't crash printing ✓ to a non-UTF-8 pipe (e.g. Windows cp1252).
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            # Windows pipes (CI logs) default to cp1252, which would turn ✓ into "?"; write UTF-8 instead.
            # A console (isatty) already handles Unicode, and PYTHONIOENCODING is the user's choice.
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if encoding != "utf8" and not stream.isatty() and not os.environ.get("PYTHONIOENCODING"):
                reconfigure(encoding="utf-8", errors="replace")
            else:
                reconfigure(errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    # `wheelgap mcp`: the MCP server (as `wheelgap-mcp`), so MCP registries can start it from the `wheelgap` package
    # (`uvx wheelgap@VERSION mcp`). Unlike diff, a file or folder named mcp here doesn't make it an input: the MCP
    # client picks the working directory, often a project with an mcp/ folder. `wheelgap ./mcp` checks such a folder,
    # and `wheelgap mcp==1.0` the PyPI package. Its stdout is the protocol, not a log, so it isn't guarded.
    if argv[:1] == ["mcp"]:
        from .mcp import main as mcp_main

        return mcp_main(argv[1:], prog="wheelgap mcp")
    # Lockfile text must never run as a GitHub Actions workflow command in a CI log (see logsafe).
    with guarded_output():
        return _main(argv, project)


def _main(argv: list[str], project: Path | None = None) -> int:
    # `wheelgap diff OLD NEW`: a subcommand only when "diff" comes first and isn't a file or folder here, so
    # `wheelgap ./diff` still checks a folder named diff (and `wheelgap diff==1.0` the PyPI package).
    if argv[:1] == ["diff"]:
        if not Path("diff").exists():
            from .diff import main as diff_main

            return diff_main(argv[1:], prog="wheelgap diff")
        print(
            "wheelgap: note: reading ./diff as an input; for the comparison, run python -m wheelgap.diff",
            file=sys.stderr,
        )
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.json and args.markdown:
        parser.error("--json and --markdown can't be used together")
    if args.print_gap_wheel_snippet:
        if not args.fix:
            parser.error("--print-gap-wheel-snippet needs --fix")
        given = [
            flag
            for flag, value in (
                ("--json", args.json),
                ("--markdown", args.markdown),
                ("--explain", args.explain),
                ("--all", args.all),
                ("--from-log", args.from_log is not None),
                ("--dist", bool(args.dist)),
                ("--junit", args.junit is not None),
                ("--sarif", args.sarif is not None),
                ("--gitlab-code-quality", args.gitlab_code_quality is not None),
                ("--badge", args.badge is not None),
                ("--write-constraints", args.write_constraints is not None),
                ("--github-annotations", args.github_annotations),
            )
            if value
        ]
        if given:
            parser.error(
                "--print-gap-wheel-snippet prints only the configuration for one uv.lock, requirements file or "
                f"package names, so it can't be used with {', '.join(given)}"
            )
    if args.list_targets and args.markdown:
        parser.error("--list-targets has no Markdown form (use --json)")
    if args.offline and args.online:
        parser.error("--offline and --online can't be used together")
    if args.from_log is not None:
        given = [
            flag
            for flag, value in (
                ("INPUT", args.inputs),
                ("--dist", args.dist),
                ("--deps", args.deps),
                ("--environment", args.environment is not None),
                ("--suggest-targets", args.suggest_targets is not None),
                ("--list-targets", args.list_targets),
                ("--clear-cache", args.clear_cache),
            )
            if value
        ]
        if given:
            parser.error(f"--from-log checks the packages in the log, so it can't be used with {', '.join(given)}")
        if not args.dockerfile and not (args.python and args.target):
            # The log doesn't say where it ran, and the machine wheelgap runs on is often not that one.
            parser.error(
                "--from-log needs the Python and platform the install ran on: pass -p and -t, "
                "such as -p 3.13 -t win-arm64 (or --dockerfile for a docker build)"
            )
        args.explain = True
        if args.suggest is None:  # --no-suggest still turns it off
            args.suggest = True
    if args.suggest_targets is not None:
        if args.markdown:
            parser.error("--suggest-targets has no Markdown form (use --json)")
        if args.inputs:
            parser.error("--suggest-targets takes the project folder as its value: --suggest-targets DIR")
        given = [
            flag
            for flag, value in (
                ("-p", args.python),
                ("-t", args.target),
                ("--dockerfile", args.dockerfile),
                ("--build-arg", args.build_arg),
                ("--list-targets", args.list_targets),
            )
            if value
        ]
        if given:
            parser.error(f"--suggest-targets only suggests, so it can't be used with {', '.join(given)}")
        try:
            found = suggest_targets(Path(args.suggest_targets))
        except OSError as e:
            print(f"wheelgap: error: {e}", file=sys.stderr)
            return input_error_json(e) if args.json else 2
        if args.json:
            print(json.dumps(target_suggestions_json(found), indent=2))
        else:
            print("\n".join(render_target_suggestions(found)))
        return 0
    if args.conda_forge and not args.explain:
        parser.error("--conda-forge only adds to --explain; pass --explain too")
    if args.piwheels and not args.explain:
        parser.error("--piwheels only adds to --explain; pass --explain too")
    build_args: dict[str, str] = {}
    for item in args.build_arg or []:
        name, eq, value = item.partition("=")
        if not eq or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name, re.ASCII):
            parser.error(f"--build-arg {item}: expected NAME=VALUE")
        build_args[name] = value  # a later one wins, as in docker build
    if build_args and not args.dockerfile:
        parser.error("--build-arg only sets the ARGs of --dockerfile; pass --dockerfile too")
    if args.suggest and not args.explain:
        parser.error("--suggest only adds to --explain; pass --explain too")
    if args.write_constraints is not None and not (
        args.suggest or (args.suggest is None and args.deps and args.explain)
    ):
        parser.error(
            "--write-constraints writes what --suggest finds; pass --explain --suggest (or --deps --explain) too"
        )
    if args.notes and not args.explain:
        parser.error("--notes only adds to --explain; pass --explain too")
    if args.dist and args.inputs:
        parser.error("--dist DIR takes no other inputs")
    if args.dockerfile_context is not None:
        if not args.dockerfile_inputs:
            parser.error("--dockerfile-context is only used with --dockerfile-inputs; pass --dockerfile-inputs too")
        # An empty DIR (an unset variable in a script) must not quietly fall back to the Dockerfile's folder, where
        # a different requirements.txt may sit, and Path("") would be the current folder.
        if not args.dockerfile_context:
            parser.error("--dockerfile-context: DIR is empty; pass the build context's folder (. for this one)")
        if not Path(args.dockerfile_context).is_dir():
            parser.error(f"--dockerfile-context: not a directory: {args.dockerfile_context}")
    if args.dockerfile_inputs:
        if not args.dockerfile:
            parser.error("--dockerfile-inputs reads what --dockerfile installs; pass --dockerfile too")
        given = [flag for flag, value in (("INPUT", args.inputs), ("--dist", args.dist)) if value]
        if args.from_log is not None:
            given.append("--from-log")
        if given:
            parser.error(
                "--dockerfile-inputs takes the packages from the Dockerfile, "
                f"so it can't be used with {', '.join(given)}"
            )
    installs: list[tuple[int, Path]] = []
    # Where [tool.wheelgap] and .python-version are looked for: the Dockerfile's folder (or --dockerfile-context DIR)
    # when it supplies the inputs.
    if args.dockerfile_inputs:
        context_dir = Path(args.dockerfile_context) if args.dockerfile_context else Path(args.dockerfile).parent
        config_inputs = [str(context_dir)]
    else:
        config_inputs = args.inputs
    try:
        if args.dockerfile_inputs:
            context = Path(args.dockerfile_context) if args.dockerfile_context else None
            installs = dockerfile_requirements(Path(args.dockerfile), build_args, context=context)
            args.inputs = [str(requirements) for _, requirements in installs]
        if args.clear_cache:
            folder, count = cache.clear()
            print(f"wheelgap: removed {count} cached lookup{'' if count == 1 else 's'} from {folder}", file=sys.stderr)
            if not args.inputs and not args.list_targets:
                return 0
        if args.list_targets:
            pyproject = find_pyproject(config_inputs or ([str(project)] if project else []))
            print_target_list(args, load_config(pyproject) if pyproject else {})
            return 0
        pyproject = find_pyproject(config_inputs or ([str(project)] if project else []))
        config = load_config(pyproject) if pyproject else {}
        # quiet = true is a project default, so --from-log (which explains each package) on the command line wins over
        # it, as --no-quiet does; --quiet on the command line still applies.
        quiet = args.quiet if args.quiet is not None else config.get("quiet", False) and args.from_log is None
        # offline = true is a project default, so --online on the command line wins over it.
        offline = args.offline or (config.get("offline", False) and not args.online)
        offline_how = "--offline" if args.offline else "offline = true in [tool.wheelgap]"
        # Quiet only changes the text output, so it can't clash with --explain when printing JSON or Markdown.
        if quiet and args.explain and not (args.json or args.markdown):
            if args.from_log is not None:
                parser.error("--from-log explains each package, so it can't be used with --quiet")
            parser.error("--explain cannot be used with quiet mode (--quiet or quiet = true; --no-quiet turns it off)")
        docker_musl = False
        if args.dockerfile:
            # The image beats pyproject.toml; flags still win.
            image = read_dockerfile(Path(args.dockerfile), build_args)
            if not quiet:
                for install_line, requirements in installs:
                    print(
                        f"wheelgap: note: {args.dockerfile}:{install_line}: RUN pip install -r reads {requirements}",
                        file=sys.stderr,
                    )
                for name, value in image.build_args:
                    if name in image.given_args:
                        note = f"FROM uses --build-arg {name}={value}"
                    else:
                        note = f"FROM uses the default of ARG {name}={value}; pass --build-arg if the build sets one"
                    print(f"wheelgap: note: {args.dockerfile}: {note}", file=sys.stderr)
                for name in image.undeclared_args:
                    print(
                        f"wheelgap: note: {args.dockerfile}: no ARG {name} before the first FROM, so "
                        f"--build-arg {name} doesn't change the image (Docker ignores it there too)",
                        file=sys.stderr,
                    )
            docker_musl = image.platform.startswith("musl-")
            if docker_musl and args.target:
                args.target = [musl_docker_names(item) for item in args.target]
            config.update(python=[image.python], targets=[image.platform])
            config.update({k: v for k, v in (("glibc", image.glibc), ("musl", image.musl)) if v})
        glibc_version, musl_version = libc_versions(args, config)
        pythons = (
            args.python
            or config.get("python")
            or read_python_version(project_dir(config_inputs or ([str(project)] if project else [])))
            or [running_python()]
        )
        platforms = args.target or config.get("targets", [])
        if not platforms and pyproject:
            platforms = load_uv_targets(pyproject, pythons)
            if platforms and not quiet:
                note = (
                    f"wheelgap: note: targets from {pyproject} [tool.uv] required-environments: {', '.join(platforms)}"
                )
                if any(p.startswith("musl-") for p in platforms):
                    note += "; Linux markers select both glibc and musl targets"
                print(note, file=sys.stderr)
        targets = build_targets(
            pythons,
            platforms,
            glibc_version,
            musl_version,
            args.macos or config.get("macos", DEFAULT_MACOS),
            args.ios or config.get("ios", DEFAULT_IOS),
            args.android_api or config.get("android-api", DEFAULT_ANDROID_API),
        )
        if not quiet and (resolved := next_python_note(pythons)):
            print(f"wheelgap: note: {resolved}", file=sys.stderr)
        ignore_how = "--ignore" if args.ignore else "ignore in [tool.wheelgap]"
        ignores = [
            entry
            for value in args.ignore or config.get("ignore", [])
            for entry in split_ignore_value(value, ignore_how, docker_musl)
        ]
        # Like --ignore, --advisory replaces the configured list (and --advisory "" clears it).
        advisory_how = "--advisory" if args.advisory else "advisory in [tool.wheelgap]"
        checked_platforms = list(dict.fromkeys(t.platform for t in targets))
        advisory_platforms, unchecked_advisory = resolve_advisory(
            args.advisory or config.get("advisory", []), advisory_how, docker_musl, checked_platforms
        )
        if unchecked_advisory:
            if args.advisory:  # asked for on this run, so not checking it is a mistake
                raise TargetError(
                    f"--advisory {', '.join(unchecked_advisory)}: matches no checked target "
                    f"(this run checks {', '.join(checked_platforms)}); add it to -t as well"
                )
            if not quiet:  # a project default for runs that check other targets (-t, --dockerfile): say so
                print(
                    f"wheelgap: note: advisory in [tool.wheelgap] names targets this run doesn't check: "
                    f"{', '.join(unchecked_advisory)}",
                    file=sys.stderr,
                )
        advisory = frozenset(t for t in targets if t.platform in advisory_platforms)
        if advisory and len(advisory) == len(targets) and not quiet:
            # Then nothing can fail the run, not even a failed lookup (unknown) during an index outage.
            print(
                f"wheelgap: note: every checked target is advisory ({advisory_how}), so this run won't fail, "
                "not even when a lookup fails",
                file=sys.stderr,
            )
        index_url = args.index_url if args.index_url is not None else config.get("index-url")
        if index_url is not None:
            _, auth = split_credentials(index_url)
            if auth is not None and args.index_url is None:
                raise SourceError(
                    "[tool.wheelgap] index-url must not contain credentials: "
                    "pass the URL with --index-url from a CI secret instead"
                )
        else:
            check_index_url()  # WHEELGAP_INDEX_URL only matters for the JSON API
        if args.from_log is not None:
            failures = failed_builds(read_log(args.from_log), other_index=origin.other_index(index_url) is not None)
            listed = ", ".join(f"{name} {version}" for name, version in failures)
            print(f"wheelgap: note: the install log says these failed to build: {listed}", file=sys.stderr)
            sources = [log_source(failures)]
        else:
            sources = (
                [parse_dist(Path(args.dist))] if args.dist else load_inputs(args.inputs or ["."], args.environment)
            )
        if args.print_gap_wheel_snippet and (
            len(sources) != 1
            or sources[0].kind not in gapwheels.SNIPPET_KINDS
            or sources[0].uv_script is not None  # a script's lock: its config goes in the script, as for a script
        ):
            # Decided from the inputs, before any lookup, so the exit code doesn't depend on what the index has.
            raise SourceError(
                f"--print-gap-wheel-snippet has no snippet for {', '.join(s.label for s in sources)}: it takes one "
                "uv.lock (not a script's), one requirements file or package names on the command line "
                "(--fix alone covers the others)"
            )
        for source in sources:
            check_report_targets(source, targets)
            if source.passed_over is not None and not quiet:
                print(
                    f"wheelgap: note: {source.path} was checked; {source.passed_over} is in the same folder too "
                    "(pass it, or --environment, to check it instead)",
                    file=sys.stderr,
                )
        extra_names = args.extra if args.extra is not None else config.get("extras")
        group_names = args.group if args.group is not None else config.get("groups")
        if args.only_group:
            if args.extra is None:
                extra_names = None
            if args.group is None:
                group_names = None
        if args.no_dev and args.group is None and group_names is not None:
            group_names = [name for name in group_names if canonicalize_name(name) != "dev"]
        from_cli = args.extra is not None or args.group is not None or args.all_extras or args.no_dev
        # pyproject.toml inputs take --extra / --all-extras for [project] optional-dependencies. Configured
        # extras and groups don't apply to them; groups are selected explicitly as with pip --group.
        pyproject_sources = [s for s in sources if s.pyproject_extras is not None]
        lock_sources = [s for s in sources if s.pyproject_extras is None]
        if pyproject_sources:
            if any(s.kind == "script" for s in pyproject_sources) and (
                args.group is not None or args.only_group or args.no_dev or args.extra or args.all_extras
            ):
                raise SourceError(
                    "--extra, --all-extras, --group, --only-group and --no-dev don't apply to a script: "
                    "its # /// script block has no extras or dependency groups"
                )
            if args.no_dev:
                raise SourceError(
                    "--no-dev doesn't apply to pyproject.toml: no dependency group is selected by default"
                )
            for source in pyproject_sources:
                if source.pyproject_groups_only:
                    if args.extra or args.all_extras:
                        raise SourceError(f"{source.label}: no [project] optional-dependencies to select from")
                    if not (args.group or args.only_group):
                        group_names = pyproject_group_names(source.pyproject_groups)
                        if group_names:
                            raise SourceError(
                                f"{source.label}: no [project] table; pick one with --group or --only-group: "
                                f"{', '.join(group_names)}"
                            )
                        raise SourceError(f"{source.label}: no [project] table and no groups to select")
                if args.only_group and (args.extra or args.all_extras):
                    raise SourceError(
                        "--only-group cannot be combined with --extra or --all-extras for pyproject.toml inputs"
                    )
                select_pyproject_groups(source, args.group or [], args.only_group or [])
                select_pyproject_extras(source, args.extra or [], args.all_extras)
        if (from_cli or args.only_group or extra_names is not None or group_names is not None) and (
            lock_sources or not pyproject_sources
        ):
            select_uv_dependencies(
                lock_sources,
                UvSelection(
                    extras=frozenset(canonicalize_name(name) for name in extra_names or []),
                    all_extras=args.all_extras,
                    groups=frozenset(canonicalize_name(name) for name in group_names or []),
                    no_dev=args.no_dev,
                    only_groups=frozenset(canonicalize_name(name) for name in args.only_group or []),
                ),
                # Configured extras apply to uv.lock and Poetry 2.x poetry.lock inputs, configured groups to those
                # and pdm.lock inputs; on the command line they need one of those.
                strict=bool(from_cli or args.only_group),
                configured_extras=args.extra is None and extra_names is not None,
                poetry=bool(from_cli or args.only_group) or group_names is not None or extra_names is not None,
                pdm=bool(from_cli or args.only_group) or group_names is not None,
            )
        if args.deps:
            dependency_sources = [s for s in sources if s.specs or s.pyproject_extras is not None]
            if not dependency_sources:
                raise SourceError(
                    "--deps applies to package names on the command line, requirements files, a conda "
                    "environment.yml, pyproject.toml or a script's # /// script block; a lockfile already lists "
                    "what it installs"
                )
            if offline:
                raise SourceError(
                    f"{offline_how}: --deps would look packages up on PyPI; drop --deps, or {undo_offline(offline_how)}"
                )
            if index_url is not None:
                raise SourceError(
                    "--deps reads dependencies from PyPI's JSON API, which a simple index (--index-url, or "
                    "index-url in [tool.wheelgap]) doesn't have"
                )
            for source in dependency_sources:
                # A command-line name is PyPI's by definition; a file may point pip elsewhere, where --deps
                # can't see, so it refuses rather than answer for the wrong index.
                if not (source.requirements or source.pyproject_extras is not None):
                    continue
                instead = (
                    ", so check its lock (uv lock --script) instead"
                    if source.kind == "script"
                    else ", so check a lockfile instead"
                )
                if not source.pypi_only:
                    what = "the script" if source.kind == "script" else "the project"
                    raise SourceError(
                        f"{source.label}: {what} configures an installer index or source; "
                        f"--deps uses only PyPI{instead}"
                    )
                if (env := origin.index_env()) is not None:
                    raise SourceError(
                        f"{source.label}: {env} may point pip or uv at another index, and --deps uses only PyPI"
                        + instead
                    )
            # Wheel metadata files are cached on disk like pinned lookups: --online asks again, --no-cache skips it.
            resolver = Resolver(read_cache=not (args.no_cache or args.online), write_cache=not args.no_cache)
            for source in dependency_sources:
                if pinned_complete(source):
                    print(
                        f"wheelgap: note: {source.label} already lists what it installs "
                        "(every requirement pinned, with hashes); checking it as written",
                        file=sys.stderr,
                    )
                    continue
                if source.deps_blockers:
                    raise SourceError(
                        f"{source.label}: --deps can't follow this the way pip does ({source.deps_blockers[0]}), "
                        "so check a lockfile instead (pip-compile, or uv pip compile)"
                    )
                resolve_deps(source, targets, resolver)
        if args.suggest is None:
            # On by default only when --deps resolved every input from PyPI: then the way out sends no name --deps
            # didn't. A lockfile or an already-pinned file among the inputs keeps --suggest opt-in.
            args.suggest = bool(
                args.deps and args.explain and sources and all(s.deps_paths is not None for s in sources)
            )
            if args.write_constraints is not None and not args.suggest:
                raise SourceError(
                    "--write-constraints needs --suggest: --deps turns it on only when it resolved every input, so "
                    "pass --suggest to look the others up too"
                )
        if args.dist:
            if args.online:
                raise SourceError("--dist checks the files in DIR as they are; --online would check PyPI's instead")
            if offline:
                # --dist refuses --online too (above), so the config key's way out is the key itself.
                undo = "drop --offline" if args.offline else "set offline = false in [tool.wheelgap] (or drop the key)"
                raise SourceError(f"{offline_how}: --dist looks the last release up on PyPI; drop --dist, or {undo}")
            if index_url is not None:
                raise SourceError(
                    "--dist's last-release check reads PyPI's JSON API, which a simple index (--index-url, or "
                    "index-url in [tool.wheelgap]) doesn't have"
                )
        if offline:
            require_offline(sources, offline_how, simple_index=index_url)
            if args.conda_forge:
                raise SourceError(
                    f"{offline_how}: --conda-forge would look packages up on anaconda.org; "
                    f"drop --conda-forge, or {undo_offline(offline_how)}"
                )
            if args.piwheels:
                raise SourceError(
                    f"{offline_how}: --piwheels would look packages up on piwheels.org; "
                    f"drop --piwheels, or {undo_offline(offline_how)}"
                )
            if args.suggest:
                raise SourceError(
                    f"{offline_how}: --suggest would look packages up on PyPI; "
                    f"drop --suggest, or {undo_offline(offline_how)}"
                )
            if args.fix:
                raise SourceError(
                    f"{offline_how}: --fix would read the gap wheel list from wheelgap.github.io; "
                    f"drop --fix, or {undo_offline(offline_how)}"
                )
        fail_on = args.fail_on or config.get("fail-on", "source")
        pyodide_lock = None
        if args.pyodide_lock:
            if offline:
                refuse_pyodide_url(args.pyodide_lock, offline_how)
            pyodide_lock = load_pyodide_lock(args.pyodide_lock)
            check_targets(pyodide_lock, targets)  # asked for explicitly, so a mismatch is an error
        elif (lock_spec := config.get("pyodide-lock")) and any(PLATFORMS[t.platform][0] == "pyodide" for t in targets):
            # A project default: only read (or fetched) on runs that check a pyodide target, so it never
            # affects `wheelgap -t linux` in the same project. A relative path is relative to pyproject.toml.
            if pyproject and not lock_spec.lower().startswith(("http://", "https://")):
                lock_spec = str(pyproject.parent / lock_spec)
            if offline:
                refuse_pyodide_url(lock_spec, offline_how)
            pyodide_lock = load_pyodide_lock(lock_spec)
            check_targets(pyodide_lock, targets)  # pyodide targets but none for this lock's Python: say so
    except (TargetError, SourceError, DockerfileError, PyodideLockError, OSError) as e:
        print(f"wheelgap: error: {e}", file=sys.stderr)
        return input_error_json(e) if args.json else 2
    except Exception as e:  # malformed TOML and friends
        print(f"wheelgap: error: {type(e).__name__}: {e}", file=sys.stderr)
        return input_error_json(e) if args.json else 2

    if args.deps and not quiet:
        count = len({p.key for s in sources if s.deps_paths is not None for p in s.packages if not p.skip})
        if count:
            print(
                f"wheelgap: note: --deps resolved {count} package{'s' if count != 1 else ''} from PyPI's "
                "metadata, taking the newest release that meets every constraint on each target; a lockfile "
                "(uv lock, pip-compile) is exact",
                file=sys.stderr,
            )
    if not quiet:
        note_unpinned_requirements(sources)
        for source in sources:
            if source.poetry_extras_unsure is not None:
                print(f"wheelgap: note: {source.label}: {source.poetry_extras_unsure}", file=sys.stderr)
    for source in sources:
        # Always, like the note below: either may mean uv applies another upload cutoff than the one checked.
        if source.exclude_newer_unread is not None:
            print(f"wheelgap: note: {source.label}: {source.exclude_newer_unread}", file=sys.stderr)
        if source.pyproject_extras is not None and os.environ.get("UV_EXCLUDE_NEWER"):
            print(
                f"wheelgap: note: {source.label}: UV_EXCLUDE_NEWER is set, which uv uses over the file's "
                "exclude-newer; wheelgap doesn't read it",
                file=sys.stderr,
            )
        if source.pyproject_extras is not None:  # always: this answer differs from a lockfile's
            if source.deps_paths is None:
                print(
                    f"wheelgap: note: {source.label} lists direct dependencies only; "
                    + (
                        "their own dependencies aren't checked "
                        "(add --deps to check what the script installs from PyPI), "
                        if source.kind == "script"
                        else "their own dependencies aren't checked (use a lockfile for that, or add --deps for PyPI), "
                    )
                    + "and ranges were checked at the newest matching release"
                    + (
                        " uploaded before exclude-newer"
                        if source.exclude_newer is not None
                        or any(cutoff is not None for cutoff in source.exclude_newer_package.values())
                        else ""
                    ),
                    file=sys.stderr,
                )
            if source.requires_python and not quiet:  # the answer is right either way, so -q drops these
                try:
                    SpecifierSet(source.requires_python)
                except InvalidSpecifier:
                    print(
                        f"wheelgap: note: {source.label}: ignoring requires-python "
                        f"{source.requires_python!r}, which isn't a valid specifier",
                        file=sys.stderr,
                    )
                else:
                    if outside := pythons_outside_lock(source, targets, bounds_only=False):
                        print(
                            f"wheelgap: note: {source.label} requires Python {source.requires_python}, "
                            f"which leaves out Python {', '.join(outside)}; those targets are checked anyway "
                            "because you asked for them",
                            file=sys.stderr,
                        )

    needed_by_source = [selected_packages(source, targets) for source in sources]
    packages = [package for needed in needed_by_source for package in needed]
    for s, needed in zip(sources, needed_by_source, strict=True):
        if s.uv_selection is not None and not s.uv_selection.only_groups and not quiet:
            kept, start = {id(p) for p in needed}, set(uv_selected_roots(s))
            members = {s.packages[i].name for i in s.roots if i not in start and id(s.packages[i]) not in kept}
            if unchecked := sorted(members):
                print(
                    f"wheelgap: note: {s.label}: the selection checks the root project and what it installs; "
                    f"workspace members outside it aren't checked: {', '.join(unchecked)}",
                    file=sys.stderr,
                )
    # --online asks the index again but saves the answer, so a plain run afterwards doesn't go back to an older one.
    cached = {"read_cache": not (args.no_cache or args.online), "write_cache": not args.no_cache}
    pythons = tuple(dict.fromkeys(f"{t.major}.{t.minor}" for t in targets))
    other_index = fill_all(
        packages,
        refresh=args.online,
        pythons=pythons,
        simple_index=index_url,
        read_cache=cached["read_cache"],
        write_cache=cached["write_cache"],
    )
    if args.online and other_index and not quiet:
        print(
            f"wheelgap: note: --online looks up PyPI packages only; kept the locked files of {other_index} "
            f"package{'s' if other_index != 1 else ''} from other indexes",
            file=sys.stderr,
        )
    for s, needed in zip(sources, needed_by_source, strict=True):
        # A pyproject.toml has no lock to re-check, so it never goes down the uv.lock path below.
        if s.pyproject_extras is None and (outside := pythons_outside_lock(s, targets)):
            if offline:  # no re-check, so every locked package may lack wheels for those Pythons
                print(
                    f"wheelgap: note: {s.label} is locked for Python {s.requires_python}, so it may be missing "
                    f"wheels for Python {', '.join(outside)}; with {offline_how} its packages weren't re-checked "
                    "on the index, so they may show false gaps for those Pythons",
                    file=sys.stderr,
                )
                continue
            if args.online:  # fill_all looked up the PyPI packages; the others kept the lock's files
                other = sum(1 for p in needed if keeps_locked_files(p))
                failed = 0
                if not other:
                    continue
            else:
                other, failed = recheck_lock(needed, simple_index=index_url, **cached)
            note = (
                f"wheelgap: note: {s.label} is locked for Python {s.requires_python}, so it may be missing "
                f"wheels for Python {', '.join(outside)}; looked its PyPI packages up on the index too"
            )
            unchecked = [f"{other} from other indexes"] * bool(other)
            unchecked += [f"{failed} that couldn't be looked up"] * bool(failed)
            if unchecked:
                note += f"; {' and '.join(unchecked)} may show false gaps for those Pythons"
            if unchecked or not quiet:  # quiet runs stay quiet unless the answer may be wrong
                print(note, file=sys.stderr)
    apply_locked_python_ranges(packages, pythons, online=args.online)
    if args.online:
        for source, needed in zip(sources, needed_by_source, strict=True):
            if source.uv_packages is not None:
                restore_uv_python_files(needed, pythons)
    skipped = [p for p in packages if p.skip]
    rows = [r for r in check(sources, targets) if r.needed]
    if pyodide_lock:
        apply_bundled(rows, targets, pyodide_lock)
    rows.sort(key=lambda r: r.package.key)
    ignore = {canonicalize_name(name) for _, name, platforms in ignores if platforms is None}
    checked = {t.platform for t in targets}
    ignore_on: dict[str, set[str]] = {}  # NAME:TARGET, for packages not also ignored everywhere
    for _, name, platforms in ignores:
        if platforms is not None and canonicalize_name(name) not in ignore:
            ignore_on.setdefault(canonicalize_name(name), set()).update(checked.intersection(platforms))
    ignore_on = {key: platforms for key, platforms in ignore_on.items() if platforms}
    if ignores and not quiet:
        # Every package read, selected or not.
        known_keys = {p.key for s in sources for p in s.packages}.union(*(s.unselected_keys for s in sources))
        unmatched = list(dict.fromkeys(item for item, name, _ in ignores if canonicalize_name(name) not in known_keys))
        if unmatched:
            print(f"wheelgap: note: --ignore matches no package: {', '.join(unmatched)}", file=sys.stderr)
        # A real target that this run doesn't check (e.g. a config ignore for another image): harmless, but say so.
        unchecked = list(
            dict.fromkeys(
                item
                for item, name, platforms in ignores
                if platforms is not None and canonicalize_name(name) in known_keys and checked.isdisjoint(platforms)
            )
        )
        if unchecked:
            print(f"wheelgap: note: --ignore matches no checked target: {', '.join(unchecked)}", file=sys.stderr)
    # One entry per package, named as its first input writes it (PyYAML, not pyyaml).
    first_names = {r.package.key: r.package.name for r in reversed(rows) if r.package.key in ignore | ignore_on.keys()}
    plat_order = list(dict.fromkeys(t.platform for t in targets))
    ignored = sorted(
        (
            (name, None if key in ignore else [p for p in plat_order if p in ignore_on[key]])
            for key, name in first_names.items()
        ),
        key=lambda entry: entry[0].lower(),
    )
    rows = [r for r in rows if r.package.key not in ignore]
    for r in rows:
        if r.package.key in ignore_on:
            r.ignored = frozenset(t for t in targets if t.platform in ignore_on[r.package.key])
    # A uv.lock checked with --online (sources.restore_uv_python_files): the answer is uv's, and pip's differs.
    pip_refusals = len({(r.package.key, r.package.version) for r in rows if r.package.pip_refuses})
    if pip_refusals and not quiet:
        what, it = ("package is", "it") if pip_refusals == 1 else ("packages are", "them")
        print(
            f"wheelgap: note: {pip_refusals} {what} checked as uv installs {it}, though pip would refuse {it}: "
            "uv ignores Requires-Python upper bounds (the text report's --explain says which, and --json lists "
            "the files in pip_refuses)",
            file=sys.stderr,
        )
    gaps = [r for r in rows if not r.ok]
    if args.from_log is not None:
        multi_py = len({t.python for t in targets}) > 1
        for r in rows:
            if fits := [t.label(multi_py) for t, status in r.results.items() if status == Status.WHEEL]:
                print(
                    f"wheelgap: note: the install log says {r.package.name} failed to build, but "
                    f"{r.package.name} {r.package.version} has a wheel for {', '.join(fits)}: the log likely "
                    "comes from another platform or Python than the one checked (-p, -t)",
                    file=sys.stderr,
                )
    source_labels = [s.label for s in sources]
    row_notes: RowNotes = {}
    row_builds: RowBuilds = {}
    # Opt-in, since it sends package names to wheelgap.github.io: --explain on a lockfile makes no requests by default.
    # Never offline, nor when packages come from another index (--index-url, a WHEELGAP_INDEX_URL mirror).
    notes_asked = args.notes if args.notes is not None else config.get("notes")
    elsewhere = origin.other_index(index_url)
    index_how = "--index-url" if args.index_url is not None else "index-url in [tool.wheelgap]"
    notes_off = offline_how if offline else (index_how if elsewhere == "--index-url" else elsewhere)
    source_of = {id(p): s for s in sources for original in s.packages for p in original.variants}
    if args.explain and notes_asked and notes_off and not quiet:
        print(
            f"wheelgap: note: the tracker's notes are off with {notes_off}, so no package names leave for "
            "wheelgap.github.io",
            file=sys.stderr,
        )
    elif args.explain and gaps and not notes_off:
        private = origin.private_names(sources, simple_index=index_url)
        if notes_asked:
            row_notes, row_builds, cut = tracker_notes(gaps, targets, source_of, private=private, **cached)
            if cut and not quiet:
                why = (
                    "wheelgap.github.io didn't answer in time"
                    if cut == tracker.TIMED_OUT
                    else "couldn't reach wheelgap.github.io"
                )
                print(
                    Style(use_color(args.color)).dim(
                        f"wheelgap: note: {why}, so some of the tracker's notes may be missing"
                    ),
                    file=sys.stderr,
                )
        elif notes_asked is None and not quiet and any(notes_eligible(r, targets, source_of, private) for r in gaps):
            print(
                Style(use_color(args.color)).dim(
                    "wheelgap: add --notes to show the tracker's notes for these packages "
                    "(sends their names to wheelgap.github.io)"
                ),
                file=sys.stderr,
            )
    conda_forge = conda_forge_hints(gaps, targets, sources, index_url, quiet, cached) if args.conda_forge else {}
    pi_wheels = piwheels_hints(gaps, targets, sources, index_url, quiet, cached) if args.piwheels else {}
    # None: not asked for. Like the hints above, it never changes a status or the exit code.
    suggestions = version_suggestions(gaps, targets, sources, index_url, quiet) if args.suggest else None
    # With --fail-on missing, source builds are accepted; missing and unknown packages still fail. Gaps on
    # --advisory targets never fail the run.
    fail_statuses = GAP_STATUSES if fail_on == "source" else (Status.MISSING, Status.UNKNOWN)
    failing = [r for r in gaps if any(s in fail_statuses for s in required_statuses(r, advisory))]
    dropped: dict[str, str] | None = None  # --dist: {target label: last release's version}; None if not compared
    if args.dist:
        pkg = sources[0].packages[0]
        dim = Style(use_color(args.color)).dim
        # These notes print even with -q: without them a gate that didn't compare would pass silently.
        if env := origin.index_env():
            # The project may live on that index, and PyPI's project of the same name may not be this one.
            print(
                dim(
                    f"wheelgap: note: {env} is set, so --dist didn't compare {pkg.name} with PyPI's releases "
                    "(it may not be the same project)"
                ),
                file=sys.stderr,
            )
        else:
            try:
                latest = previous_release(pkg.key, pkg.version or "")
            except Exception as e:
                print(
                    dim(
                        f"wheelgap: note: couldn't look {pkg.name} up on the index ({type(e).__name__}), "
                        "so --dist's dropped-target check didn't run"
                    ),
                    file=sys.stderr,
                )
            else:
                if latest is None:
                    dropped = {}  # a first release: nothing to compare, so nothing can be dropped
                    if not quiet:
                        print(
                            dim(
                                f"wheelgap: note: {pkg.name} has no earlier release on the index; "
                                "--dist compares nothing"
                            ),
                            file=sys.stderr,
                        )
                else:
                    last_version, last_files = latest
                    pkg.found_on_pypi = True
                    multi_py = len({t.python for t in targets}) > 1
                    # As for gaps: an --ignore'd target or package, and an --advisory target, never fail the run.
                    skip = ignore_on.get(pkg.key, set())
                    dropped = {
                        t.label(multi_py): last_version
                        for t in dropped_targets(pkg.files or [], last_files, targets)
                        if pkg.key not in ignore and t.platform not in skip and t not in advisory
                    }
    code = 0 if args.exit_zero or (not failing and not dropped) else 1
    # The run passes only because --fail-on missing accepts a source build on a required target.
    source_allowed = not failing and not dropped and any(Status.SOURCE in required_statuses(r, advisory) for r in gaps)

    build_info: dict[int, BuildInfo] | None = None  # None: not asked for
    off_by = origin.other_index(index_url) or origin.index_env()  # no package counts as PyPI's for sure
    want_build_info = args.build_info if args.build_info is not None else config.get("build-info", False)
    if args.explain and want_build_info and not offline and off_by:
        if not quiet:
            note = f"wheelgap: note: --build-info is off: packages may come from another index ({off_by})"
            print(Style(use_color(args.color)).dim(note), file=sys.stderr)
    elif args.explain and want_build_info and not offline:
        # What each explained `source` release needs to build, from its sdist (only for packages positively from
        # PyPI: see build.where). Nothing here changes the exit code, and a release whose sdist gives no answer just
        # gets no line.
        source_of = {id(p): s for s in sources for original in s.packages for p in original.variants}
        wanted = [(r.package, source_of[id(r.package)]) for r in gaps if source_targets(r, targets)]
        private = origin.private_names(sources, simple_index=index_url)
        build_info, stopped = lookup_all(wanted, simple_index=index_url, private=private, **cached)
        if stopped is not None and not quiet:
            print(Style(use_color(args.color)).dim(f"wheelgap: note: {stopped}"), file=sys.stderr)

    # None: not asked for. Like --suggest, it never changes a status or the exit code.
    fills = (
        gap_wheel_fills(
            gaps,
            targets,
            sources,
            index_url,
            index_how,
            quiet,
            read_cache=not (args.no_cache or args.online),
            write_cache=not args.no_cache,
        )
        if args.fix
        else None
    )
    fix_hint = (
        not args.fix
        and not args.json
        and not quiet
        and not offline
        and origin.other_index(index_url) is None
        and bool(gap_wheel_candidates(gaps, targets, sources, index_url)[1])
    )

    if args.print_gap_wheel_snippet:  # one input of gapwheels.SNIPPET_KINDS, checked when it was read
        multi_py = len({t.python for t in targets}) > 1
        if snippet := gapwheels.project_snippet_lines(fills or [], sources[0], multi_py):
            print("\n".join(snippet))
        return code

    issues_urls = explain_issues_urls(rows, sources, index_url) if args.explain else None

    if args.junit is not None:  # an empty PATH (an unset CI variable) is an error, not "no report"
        try:
            write_junit(args.junit, rows, targets, fail_on, advisory)
        except OSError as e:
            print(f"wheelgap: error: {e}", file=sys.stderr)
            return input_error_json(e) if args.json else 2

    if args.sarif is not None:  # an empty PATH is an error, as for --junit
        try:
            write_sarif(args.sarif, rows, targets, sources, fail_on, advisory)
        except OSError as e:
            print(f"wheelgap: error: {e}", file=sys.stderr)
            return input_error_json(e) if args.json else 2

    if args.gitlab_code_quality is not None:  # written even when the check fails; an empty PATH is an error
        try:
            write_codequality(args.gitlab_code_quality, rows, targets, sources, fail_on, advisory)
        except OSError as e:
            print(f"wheelgap: error: {e}", file=sys.stderr)
            return input_error_json(e) if args.json else 2

    if args.badge is not None:  # written even when the check fails, so CI can publish it; an empty PATH is an error
        try:
            write_badge(args.badge, rows, targets)
        except OSError as e:
            print(f"wheelgap: error: {e}", file=sys.stderr)
            return input_error_json(e) if args.json else 2

    if args.write_constraints is not None and suggestions is not None:  # an empty FILE is an error, as for --junit
        try:
            pins = write_constraints(args.write_constraints, rows, targets, suggestions)
        except OSError as e:
            print(f"wheelgap: error: {e}", file=sys.stderr)
            return input_error_json(e) if args.json else 2
        if not quiet:
            note = (
                f"wheelgap: note: wrote {pins} pin{'s' * (pins != 1)} to {args.write_constraints}, not checked against "
                f"the rest of the dependency tree: check with `pip install -c {args.write_constraints} ...` "
                f"or `uv pip compile -c {args.write_constraints} ...`"
            )
            print(Style(use_color(args.color)).dim(note), file=sys.stderr)

    if args.github_annotations:
        # Workflow commands work on stdout and stderr alike; stderr keeps --json and --markdown output clean.
        # These are wheelgap's own (escaped in github.workflow_command), so they go past the logsafe guard.
        for command in github_annotations(rows, targets, sources, failing, advisory, fills):
            write_command(command, sys.stderr if args.json or args.markdown else sys.stdout)

    if args.json:
        print(
            report_json(
                rows,
                targets,
                source_labels,
                skipped,
                ignored,
                args.explain,
                pyodide_lock,
                advisory,
                conda_forge,
                row_notes,
                build_info,
                suggestions,
                row_builds,
                dropped,
                issues_urls=issues_urls,
                gap_wheels=fills,
                pi_wheels=pi_wheels,
            )
        )
        return code

    multi_py = len({t.python for t in targets}) > 1
    if args.markdown:
        print(
            "\n".join(
                [
                    *render_markdown(
                        rows,
                        targets,
                        source_labels,
                        skipped,
                        ignored,
                        args,
                        pyodide_lock,
                        source_allowed,
                        advisory,
                        conda_forge,
                        row_notes,
                        build_info,
                        row_builds,
                        suggestions=suggestions,
                        dropped=dropped,
                        issues_urls=issues_urls,
                        pi_wheels=pi_wheels,
                        fix_hint=fix_hint,
                    ),
                    *gapwheels.markdown_lines(fills or [], multi_py),
                ]
            )
        )
        return code

    style = Style(use_color(args.color))
    fix_lines = gapwheels.text_lines(fills or [], multi_py)  # nothing unless a gap wheel fills a gap
    total_pkgs = len(_group_rows(rows))
    if quiet:
        if gaps:
            print(gap_summary(gaps, total_pkgs, style))
            print("gaps: " + ", ".join(group[0].package.name for group in _group_rows(gaps)))
            if blocking := blocking_newer_python(rows, targets):
                print(blocking_line(blocking))
            if line := advisory_line(rows, targets, advisory):
                print(style.dim(line))
        if ignored:
            print(style.dim("ignored: " + ignored_list(ignored)))
        if dropped:
            for label, version in dropped.items():
                print(f"{label}: {version} had a wheel, this release has none")
        if pyodide_lock:  # a bundled build of another version passes, but quiet mode mustn't hide that (#75)
            differs = [
                r
                for r in rows
                if Status.BUNDLED in r.results.values() and bundled_version_differs(r.package, pyodide_lock)
            ]
            if differs:
                print(style.dim(bundled_line(differs, pyodide_lock)))
        for r in rows:
            if r.package.error:  # say why a package is "unknown", even in quiet mode
                print(f"wheelgap: could not check {r.package.name}: {r.package.error}", file=sys.stderr)
        for ln in fix_lines:  # asked for with --fix, so -q keeps it
            print(ln)
        return code

    pys = ", ".join(dict.fromkeys(t.python for t in targets))
    n_pkgs = f"{total_pkgs} package{'s' * (total_pkgs != 1)}"
    print(style.dim(f"wheelgap {__version__} · {' + '.join(source_labels)} · {n_pkgs} · Python {pys}"))
    print()

    shown = rows if args.all else gaps
    if shown:
        for ln in render_table(shown, targets, style, advisory):
            print(ln)
        print()
        hidden = [] if args.all else [r for r in rows if r.ok]
        if hidden:
            print(style.dim(f"{hidden_line(hidden, shown)} (--all to show)"))
        if args.explain and gaps:
            if hidden:
                print()
            for ln in render_explain(
                gaps,
                targets,
                style,
                conda_forge,
                row_notes,
                build_info,
                suggestions,
                row_builds,
                issues_urls=issues_urls,
                published_wheels=fills,
                pi_wheels=pi_wheels,
            ):
                print(ln)

    bundled = [r for r in rows if Status.BUNDLED in r.results.values()]
    if bundled and pyodide_lock:
        print(style.dim(bundled_line(bundled, pyodide_lock)))

    if skipped:
        names = skipped_list(skipped)
        print(style.dim(f"skipped {len(skipped)} non-index package{'s' * (len(skipped) != 1)}: {names}"))

    if ignored:
        print(style.dim("ignored: " + ignored_list(ignored)))

    errors = [r for r in rows if r.package.error]
    for r in errors:
        print(style.dim(f"could not check {r.package.name}: {r.package.error}"))

    n_t = len(targets)
    if not rows:  # "All 0 packages have a wheel" reads like a success when the run checked nothing
        print(style.dim("No packages to check."))
        return 0
    if args.explain and pip_refusals:
        for r in rows:
            if r.package.pip_refuses:
                affected = ", ".join(
                    target.label(multi_py) for target in targets if r.results[target] is not Status.NOT_NEEDED
                )
                # One line per reason, not per file: a release's files nearly always share one Requires-Python.
                for why in dict.fromkeys(r.package.pip_refuses.values()):
                    print(
                        f"{r.package.name} {r.package.version} on {affected}: checked as uv installs it "
                        f"(uv ignores Requires-Python upper bounds); pip refuses it: {why}"
                    )
        print()

    if not gaps:
        what = "The 1 package has" if total_pkgs == 1 else f"All {total_pkgs} packages have"
        every = f"{'every one of ' if n_t > 1 else ''}{n_t} target{'s' * (n_t > 1)}{apart_from_ignored(rows)}"
        print(style.green(f"✓ {what} a wheel for {every}."))
        return code

    for line in target_lines(gaps, targets, style, advisory):
        print(line)
    print(gap_summary(gaps, total_pkgs, style))
    if dropped:
        for label, version in dropped.items():
            print(f"{label}: {version} had a wheel, this release has none")
    if blocking := blocking_newer_python(rows, targets):
        print(blocking_line(blocking))
    if line := advisory_line(rows, targets, advisory):
        print(style.dim(line))
    if not args.explain:
        print(style.dim("Run with --explain to see which wheels were rejected and why."))
    if fix_hint:
        print(style.dim(FIX_HINT))
    if source_allowed and not args.exit_zero:
        print(style.dim("Source builds are allowed (--fail-on missing), so this run passes."))
    for ln in fix_lines:  # a gap wheel fills only a gap, so the runs that return early above have none
        print(ln)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
