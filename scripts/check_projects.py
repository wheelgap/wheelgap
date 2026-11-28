"""Compare wheelgap's answers for whole projects with what uv and pip do with them.

    uv run --with pip python scripts/check_projects.py manifest CORPUS_DIR -o manifest.json   # list a corpus's inputs
    uv run --with pip python scripts/check_projects.py run manifest.json -o results/          # check them all
    uv run --with pip python scripts/check_projects.py run manifest.json -o results/ --only owner/name -t win-amd64
    uv run --with pip python scripts/check_projects.py repo OWNER/NAME [--ref COMMIT] [-t TARGET]  # one public repo

scripts/check_against_pip.py checks one pinned package at a time. This script checks inputs the way projects use
them: a `uv.lock` with its `pyproject.toml`, a `poetry.lock` with its `pyproject.toml`, or a fully pinned
requirements file, read whole, so the markers, forks,
`[tool.uv] sources`, extras, `conflicts`, dependency groups, `environments` and includes all count. For each input
and target (Python 3.13 and 3.15 on linux-x86_64, linux-aarch64, macos-arm64, win-amd64, musl-x86_64 and win-arm64
by default) it compares two things with a referee: which packages install, at which version, and for each one
whether it installs from a wheel, from source, or not at all (`missing`). Every cell (input, target, package) ends
as agree, disagree or not compared, with the reason, and every disagreement keeps the referee command that shows it.

The manifest is a JSON file listing the inputs: `manifest` writes one from a corpus folder (its corpus.json and
`repos/<owner>__<name>/` folders), taking each repo's root uv.lock (with its pyproject.toml and uv.toml), root
poetry.lock, and root requirements.txt if every requirement in it is pinned with `==`. The inputs are untrusted
data: nothing in them is run, no project is installed and no build backend runs. Each input's files are copied
into a fresh temporary folder (removed afterwards), and uv and pip only read them there:
- uv sync runs as a dry run, which plans from the lock without building; pass 1 adds --no-build, and every uv
  run stops the check if its output says it built anything;
- pip and uv pip compile run with --only-binary :all:, and a requirements file passes on only index options
  (-i/--index-url, --extra-index-url, --trusted-host, --pre, --prefer-binary): any other option, such as an
  --only-binary :none: or --find-links that would bring back an sdist, refuses the file before a referee runs;
  pip's log is also checked for "Preparing metadata", "Getting requirements to build" and "Building wheel";
- Poetry only runs `poetry export` (see below), offline, on the staged poetry.lock and pyproject.toml.
This was checked with a canary sdist whose setup.py writes a file: given in a requirements file with
`--only-binary :none:` and `--find-links`, the file is refused and the canary never runs (it does run, and the pip
check above catches it, when the allow-list is bypassed by hand); locked in a uv.lock from a find-links folder,
uv sync's dry run reports it `source` without running it.

uv.lock: the referee is `uv sync --frozen --dry-run --offline` itself, with --python-platform and --python for the
target and --no-install-project --no-install-workspace --no-install-local, in the copy (it reads the lock and
pyproject.toml; a dry run plans the install without downloading or building anything, and the lock already has
every file). wheelgap's default for a uv.lock is every member, extra and group (`uv sync --all-packages --all-extras
--all-groups`), with both sides of a declared conflict, which `uv sync` refuses to combine. So the referee is the
union of one `uv sync --all-packages --all-extras --all-groups` per way of picking the conflicts (none or one item
from each set, with --no-extra / --no-group for the rest). A second comparison, "default", checks wheelgap's
default selection (`extras=[]`, `groups=[]`: no extras and uv's default groups) against plain `uv sync`.
Per package, uv runs with --no-build first: it stops at the first package that has no wheel for the target
(`marked as --no-build but has no binary distribution`), which is recorded and left out with --no-install-package,
until the sync succeeds; the packages it lists then are `wheel`. Then it runs again without --no-build, still a
dry run, with those packages back in: uv names each one that has neither a wheel nor an sdist (`doesn't have a
source distribution or wheel for the current platform`); those are `missing` and the rest `source`. A lock can hold
the same name and version from two indexes (torch 2.9.1 from PyPI and from download.pytorch.org): uv's install list
doesn't say which, so it's asked once more with --no-binary-package, whose error names the entry's source.
Packages wheelgap skips (git, url, path, editable sources) are left out of both sides; when a name has such an entry
and a registry one too, --no-install-package can't keep the registry one, so that name isn't compared.

Requirements files: the referee is pip (`pip install --dry-run --ignore-installed --no-deps --only-binary=:all:
--report` with the target's tags as AGENTS.md lists them), with the file's requirements copied into one file
without --hash options or includes, and uv (`uv pip compile --no-deps --only-binary :all: --python-platform`). pip
stops at the first requirement without a wheel; that one counts as "no wheel" only with the positive evidence
scripts/check_against_pip.py asks for (pip fetched the project's page, with no retry or failed fetch), and it is
`source` when pip's -vv log shows it skipped an sdist of that exact version only because of --only-binary, else
`missing`. It's then left out and pip runs again. pip evaluates markers for the machine it runs on, not for
--platform, so a requirement with a marker isn't compared with pip; uv evaluates them for the target. uv can't tell
`source` from `missing` here, so that comparison is wheel / no wheel, and uv's known Requires-Python difference
(it ignores an upper bound; wheelgap answers a requirements file as pip does) is not compared, as in
check_against_pip.py. pip's "source" (an sdist that --only-binary skipped) isn't compared when the sdist's
Requires-Python leaves the target out, since pip's log can't show that check. --only-binary keeps pip and uv from
building anything to read metadata (see above for the options that would override it).

poetry.lock: Poetry can't install for another platform (it has no --platform), so the referee is Poetry's own
reading of the lock, then the requirements referee for what it reads. `poetry export --format requirements.txt
--without-hashes` (Poetry 2.5.2 with poetry-plugin-export 1.10.2, pinned, through uvx) writes the packages the lock
installs, each `name==version ; markers`, with the markers Poetry works out from the lock's groups, extras and
dependency edges (`get_project_dependency_packages2` for a lock with per-package groups and markers, the dependency
walk from pyproject.toml for an older one); then `uv pip compile --no-deps --only-binary :all: --python-platform`
evaluates those markers for the target and says wheel or no wheel for each pin, as for a requirements file (uv can't
tell `source` from `missing` there, and its Requires-Python floor rule is not compared). Two comparisons:
"all", wheelgap's default for a poetry.lock against `--all-groups --all-extras`, and "default",
`check_lockfile(extras=[], groups=[])` against a plain `poetry install`: main and every group pyproject.toml doesn't
mark `optional = true`, given as `--with` (export itself takes only main unless told), and no extras. A Poetry 1.x
lock has no "default" comparison while wheelgap refuses a group selection for it (it records no groups per package).
In "default", a package wheelgap lists and the export leaves out is not compared when it is wheelgap's documented
superset: its marker for a selected group joins an extra to a real environment condition (`python_version >= "3.10"
and extra == "syntax"`, which Poetry 2.0-2.2 install without the extra), and the "all" export installs it on that
target. A marker of extras only is decided exactly, and the other direction always counts. A
Python that the lock's `python-versions` leaves out is compared at the target level, as uv's requires-python refusal
is; one it takes only some patch releases of (`>=3.13.2`), or a `python-versions` a SpecifierSet can't hold for sure
(`||`, two clauses with only a space between them), is not compared.
Why that runs nothing of the project (Poetry 2.5.2's source): only `poetry install` installs a project's
`requires-plugins` (`console/commands/install.py`, `ensure_project_plugins`); every command puts the project's
`.poetry/plugins` folder on its path (`PluginManager.add_project_plugin_path`), so the export is refused unless
the staged folder holds only pyproject.toml and poetry.lock; with no poetry.lock, export would lock, so that is
refused too; export itself only reads the lock (`Locker`) and pyproject.toml, and writes the file it's given, here
outside the staged folder. It runs with no POETRY_* settings, Poetry's config, cache and data in a fresh folder, a
null keyring, UV_OFFLINE=1 and every proxy pointing at a closed port, so it can't fetch anything; an export whose log
says it is locking, resolving or building, or that adds or changes a file in the staged folder (names and sha256),
is refused. uvx installs the
pinned Poetry beforehand in a separate step that names nothing of the input. A stale lock (Poetry's "pyproject.toml
changed significantly since poetry.lock was last generated"), which `poetry install` refuses too, is not compared.
This was checked with a canary: a project with a path dependency on a folder whose setup.py writes a file, an in-tree
build backend that writes one, and `requires-plugins` naming a package that doesn't exist, exported in its whole
folder (not only the staged copy) with the environment above: neither file was written, no plugin was installed,
nothing in the folder changed, and the export (`local @ file:///...`) succeeded with every proxy closed. Path, URL
and git lines of an export are left out of both sides, as wheelgap skips those packages.

Emulation limits are not counted against wheelgap. uv's --python-platform sets markers from the triple, not from a
real interpreter: x86_64-pc-windows-msvc says platform_machine `x86_64` where CPython on Windows says `AMD64`, and
platform_release and platform_version are empty; uv's python_full_version is the patch release of the interpreter
it runs (3.13.13), where wheelgap's target is the release in general. A difference in a package whose dependency
path or fork in the lock turns on one of those markers is listed as not compared, agreeing or not, with the reason:
an edge counts when its parent is one uv installs on the target (or the project, a member, a path, git or URL
package, which uv walks through), or is itself in that set, so an edge from a package uv never installs can't hide a
wrong row below it. Targets a referee
refuses (uv: outside the lock's `environments`, or outside the project's requires-python) are compared at the
target level: when wheelgap refuses too, they agree; when wheelgap answers for a Python the project excludes, that is
wheelgap's design (it shows what blocks the upgrade), so it is not compared. So that such a project is compared at
all, a uv.lock whose requires-python allows none of the targets' Pythons (`==3.11.*`), or a poetry.lock whose
python-versions does (`>=3.8,<3.12`), is also checked at the newest Python it allows (3.9 to 3.14) on the same
platforms (--no-own-python turns this off). A project whose
`required-version` needs a newer uv is listed as not compared. uv runs with --python-preference system, so a
project's `python-preference = "only-managed"` doesn't stop it: it picks where the interpreter comes from, not what
installs.

Output (-o DIR): results.json (tool versions, totals, per target and per input kind, every cell with both answers,
the verdict, the reason and the referee command for every cell that doesn't agree) and report.txt (the totals, the
agreement rate per target and per input kind, and the disagreements grouped by what differs, each with a command to
reproduce it). Rates are over per-package cells; target-level cells (package `*`: a target both refuse, one refuses,
or one that couldn't be compared) are counted on their own, with the share of input × targets that had nothing
compared and the inputs with nothing compared at all. The referees' answers are cached in DIR/cache (DIR holds
nothing else of the inputs), keyed by the input's files, the target in full, the tool's version, the referee's exact
command, the macOS deployment target, this script's referee version and, for pip, uv pip compile and the poetry.lock
referee (which read the live index), the day; an answer with an error (a timeout, a network failure, a uv error that
isn't a refusal) isn't kept. So a rerun on a new wheelgap commit only recomputes wheelgap's side. Exit code: 0 when
every compared cell agrees, 1 if any disagrees, 2 if a check couldn't be made.

It needs uv on PATH with CPython 3.13 and 3.15 installed for it (`uv python install 3.13 3.15`), pip in this
environment, and the network for pip and for wheelgap's own lookups (uv.lock checks are offline for uv). The tests
(tests/test_check_projects.py) run offline with a fake referee.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name, parse_sdist_filename
from packaging.version import InvalidVersion, Version

import wheelgap
from wheelgap import Result, __version__, check_lockfile, make_targets
from wheelgap.check import Status
from wheelgap.sources import SourceError
from wheelgap.targets import Target


def _load_pin_checker() -> Any:
    """scripts/check_against_pip.py, for its pip tag lists, uv platform triples and pip evidence rules."""
    path = Path(__file__).with_name("check_against_pip.py")
    spec = importlib.util.spec_from_file_location("wheelgap_check_against_pip", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


pin = _load_pin_checker()

SCHEMA = 1
# Bump a referee's number when its logic changes, so its cached answers are recomputed.
REFEREE_VERSION = {"uv sync": 3, "pip": 1, "uv pip compile": 1, "poetry export": 1}
PYTHONS = ("3.13", "3.15")
PLATFORMS = ("linux-x86_64", "linux-aarch64", "macos-arm64", "win-amd64", "musl-x86_64", "win-arm64")
MAX_RUNS = 400  # per pass: one run per package without a wheel, plus one
UV_TIMEOUT = 300

WHEEL, SOURCE, MISSING, NO_WHEEL, UNKNOWN = "wheel", "source", "missing", "no wheel", "unknown"
AGREE, DISAGREE, NOT_COMPARED = "agree", "disagree", "not compared"

Runner = Callable[[list[str], Path, dict[str, str]], tuple[int, str]]


def run_command(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int = UV_TIMEOUT) -> tuple[int, str]:
    """Run a referee command; its stdout and stderr together, since uv writes its plan and errors to stderr."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, f"error: timed out after {timeout} s"
    except OSError as exc:
        return -1, f"error: {exc}"
    return proc.returncode, f"{proc.stdout}\n{proc.stderr}"


def norm_version(version: str) -> str:
    try:
        return str(Version(version))
    except InvalidVersion:
        return version


def key(name: str, version: str, source: str = "") -> str:
    """A cell's package: `name==version`, plus ` @ index` for a uv.lock registry entry."""
    base = f"{canonicalize_name(name)}=={norm_version(version)}"
    return f"{base} @ {source}" if source else base


def split_key(text: str) -> tuple[str, str, str]:
    base, _, source = text.partition(" @ ")
    name, _, version = base.partition("==")
    return name, version, source


# ---------------------------------------------------------------------------------------------------------------
# The manifest


_PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]*\])?\s*===?\s*[^\s,;<>=!~]+\s*(;.*)?$")


def requirement_lines(text: str) -> Iterable[tuple[int, str]]:
    """(line number, logical line) for a requirements file: continuations joined, comments and blank lines dropped."""
    buffer, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw if not buffer else " " + raw.lstrip()
        if not buffer:
            start = number
        if line.rstrip().endswith("\\"):
            buffer += line.rstrip()[:-1]
            continue
        line = buffer + line
        buffer = ""
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if line:
            yield start, line
    if buffer.strip():
        yield start, re.sub(r"(^|\s)#.*$", "", buffer).strip()


def strip_hashes(line: str) -> str:
    return re.sub(r"\s--hash[=\s]\S+", "", line).strip()


def fully_pinned(text: str) -> bool:
    """Every requirement in the file is NAME==VERSION (with or without a marker and hashes), and there is one."""
    reqs = [strip_hashes(line) for _, line in requirement_lines(text) if not line.startswith("-")]
    return bool(reqs) and all(_PINNED.match(line) for line in reqs)


def make_manifest(corpus: Path) -> dict:
    """The inputs to check in a corpus folder: each repo's root uv.lock, poetry.lock and pinned requirements.txt."""
    repos = json.loads((corpus / "corpus.json").read_text(encoding="utf-8"))
    inputs = []
    for repo in repos:
        name = repo["full_name"]
        folder = corpus / "repos" / name.replace("/", "__")
        if not folder.is_dir():
            continue
        commit = repo.get("sha")
        if (folder / "uv.lock").is_file():
            files = ["uv.lock", *(f for f in ("pyproject.toml", "uv.toml") if (folder / f).is_file())]
            inputs.append({"id": name, "kind": "uv.lock", "dir": str(folder), "main": "uv.lock", "files": files})
        if (folder / "poetry.lock").is_file():
            files = ["poetry.lock", *(f for f in ("pyproject.toml",) if (folder / f).is_file())]
            inputs.append(
                {"id": name, "kind": "poetry.lock", "dir": str(folder), "main": "poetry.lock", "files": files}
            )
        req = folder / "requirements.txt"
        if req.is_file() and fully_pinned(req.read_text(encoding="utf-8", errors="replace")):
            files = ["requirements.txt", *included_files(folder, "requirements.txt")]
            inputs.append(
                {"id": name, "kind": "requirements", "dir": str(folder), "main": "requirements.txt", "files": files}
            )
        for entry in inputs:
            if entry["id"] == name:
                entry["commit"] = commit
    return {"schema": SCHEMA, "corpus": str(corpus), "inputs": inputs}


def included_files(folder: Path, name: str, seen: set[str] | None = None) -> list[str]:
    """The files a requirements file includes with -r / -c, relative to the folder, if they are there."""
    seen = seen if seen is not None else {name}
    out: list[str] = []
    try:
        text = (folder / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for _, line in requirement_lines(text):
        match = re.match(r"^(?:-r|--requirement|-c|--constraint)(?:\s*=\s*|\s+|(?=\S))(\S+)$", line)
        if not match:
            continue
        rel = os.path.normpath(str(Path(name).parent / match.group(1)))
        if rel not in seen and not rel.startswith("..") and (folder / rel).is_file():
            seen.add(rel)
            out += [rel, *included_files(folder, rel, seen)]
    return out


# ---------------------------------------------------------------------------------------------------------------
# Staging an input: its files only, copied into a fresh folder


def input_hash(entry: dict) -> str:
    digest = hashlib.sha256()
    for name in sorted(entry["files"]):
        digest.update(name.encode() + b"\0" + (Path(entry["dir"]) / name).read_bytes() + b"\0")
    return digest.hexdigest()[:20]


def stage(entry: dict, work: Path) -> Path:
    """Copy the input's files (only those) into work/<hash>/, so uv and pip read nothing else of the project."""
    folder = work / input_hash(entry)
    if folder.exists():
        shutil.rmtree(folder)
    for name in entry["files"]:
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(entry["dir"]) / name, dest)
    return folder


def check_workdir(work: Path) -> None:
    """uv looks for a workspace in the folders above a project: refuse a work folder that has one above it."""
    for parent in [work.resolve(), *work.resolve().parents]:
        for name in ("pyproject.toml", "uv.toml"):
            if (parent / name).exists():
                raise SystemExit(f"{parent / name} is above the work folder, and uv would read it: pick another")


# ---------------------------------------------------------------------------------------------------------------
# Reading a uv.lock (only what the comparison needs: entries, sources, conflicts and markers)


@dataclass(frozen=True)
class LockEntry:
    name: str
    version: str
    kind: str  # registry, git, url, path, directory, editable, virtual
    url: str
    files: frozenset[str]


# Markers uv's --python-platform sets from the triple rather than from a real interpreter (uv 0.13
# crates/uv-configuration/src/target_triple.rs, TargetTriple::markers): x86_64-pc-windows-msvc says platform_machine
# "x86_64" where CPython on Windows says "AMD64", and platform_release and platform_version are empty. And uv's
# python_full_version is the patch release of the interpreter it runs (3.13.13), where wheelgap's target is 3.13.0.
# Each is (marker, uv's value, a value a real target can have); a None uv value means "the target's own".
def emulated_markers(target: Target) -> list[tuple[str, str, tuple[str, ...]]]:
    """(marker, uv's value, values a real target can have). The real values of platform_release and
    platform_version are free text: each marker is also tried with the strings it compares them with."""
    python = f"{target.major}.{target.minor}"
    out = [
        ("platform_release", "", ("6.0", "23.0.0")),
        ("platform_version", "", ("#1 SMP", "Darwin Kernel Version 23.0.0", "10.0.26100")),
        ("python_full_version", f"{python}.99", (f"{python}.0",)),
    ]
    if target.platform == "win-amd64":
        out.append(("platform_machine", "x86_64", ("AMD64",)))
    return out


_EXTRA_ATOM = re.compile(r"""extra\s*(==|!=)\s*(['"])([^'"]+)\2""")
_LITERAL = re.compile(r"""(['"])([^'"]*)\1""")


def _is_version(text: str) -> bool:
    try:
        Version(text)
    except InvalidVersion:
        return False
    return True


def depends_on_emulation(marker: str, target: Target, variables: Sequence[tuple[str, str, tuple[str, ...]]]) -> bool:
    """Whether a lock marker's value on the target can change between uv's emulated value of one of the variables
    and a real one, for some choice of the extras it names (uv's conflict markers name extras). True when it can't
    be evaluated, so a doubt is never counted against wheelgap."""
    if not any(var in marker for var, _, _ in variables):
        return False
    extras = sorted({m[3] for m in _EXTRA_ATOM.finditer(marker)})
    text = _EXTRA_ATOM.sub(lambda m: f"'{m[3]}' {'in' if m[1] == '==' else 'not in'} extras", marker)
    if len(extras) > 8:
        return True
    literals = tuple(m[2] for m in _LITERAL.finditer(text))
    try:
        parsed = Marker(text)
        base: dict[str, Any] = {k: v for k, v in target.marker_env.items() if k != "extra"}
        for picked in itertools.chain.from_iterable(itertools.combinations(extras, n) for n in range(len(extras) + 1)):
            env = dict(base, extras=set(picked), dependency_groups=set())
            for var, emulated, real in variables:
                if var not in marker:
                    continue
                values = real + (literals if var in ("platform_release", "platform_version") else ())
                if var == "python_full_version":
                    # uv's interpreter is some patch release of the target's Python: try the patch releases named.
                    minor = f"{target.major}.{target.minor}."
                    values += tuple(v for v in literals if v.startswith(minor) and _is_version(v))
                seen = parsed.evaluate(dict(env, **{var: emulated}), context="lock_file")
                if any(parsed.evaluate(dict(env, **{var: v}), context="lock_file") != seen for v in values):
                    return True
    except Exception:
        return True
    return False


@dataclass
class LockInfo:
    entries: list[LockEntry]
    conflicts: list[list[dict[str, str]]]
    edges: dict[str, list[tuple[str, str]]]  # name -> [(dependency name, marker)]
    fork_markers: dict[str, list[str]]  # name -> resolution-markers of its entries
    requires_python: str = ""
    # name -> dependencies from its `dependencies` table with no marker (installed whenever it is)
    required: dict[str, list[str]] = field(default_factory=dict)
    _emulated: dict[tuple[Target, frozenset[str]], set[str]] = field(default_factory=dict, repr=False)
    _depends: dict[Target, dict[str, bool]] = field(default_factory=dict, repr=False)

    @property
    def skipped_names(self) -> set[str]:
        """Names wheelgap skips that --no-install-local doesn't leave out: git and url sources."""
        return {e.name for e in self.entries if e.kind in ("git", "url")}

    @property
    def collisions(self) -> set[str]:
        """Names with a registry entry and a git/url one: --no-install-package can't keep only the registry one."""
        return self.skipped_names & {e.name for e in self.entries if e.kind == "registry"}

    @property
    def duplicates(self) -> set[tuple[str, str]]:
        """(name, version) pairs locked from more than one registry: uv's install list doesn't say which it took."""
        count = Counter((e.name, e.version) for e in self.entries if e.kind == "registry")
        return {pair for pair, n in count.items() if n > 1}

    def registry_sources(self, name: str, version: str) -> list[LockEntry]:
        return [e for e in self.entries if e.kind == "registry" and e.name == name and e.version == version]

    def source_of(self, name: str, version: str, files: Iterable[str] = ()) -> str | None:
        """The registry a wheelgap row or uv install-list line came from; None when it can't be told."""
        entries = self.registry_sources(canonicalize_name(name), norm_version(version))
        if len(entries) == 1:
            return entries[0].url
        wanted = frozenset(files)
        matching = [e for e in entries if wanted and e.files == wanted]
        return matching[0].url if len(matching) == 1 else None

    @property
    def members(self) -> set[str]:
        """Names uv walks through though it doesn't install them from an index here: the project, workspace members,
        path, git and URL packages (left out with --no-install-*, but their dependencies are installed)."""
        return {e.name for e in self.entries if e.kind != "registry"}

    @property
    def solid(self) -> set[str]:
        """Names uv installs at one version on every target whatever the markers say: reached from the project
        (the lock's `.` entry, which every uv sync walks through) by marker-free `dependencies` edges only, each
        to a name locked once. Emulation can't change whether or at which version these install."""
        once = {name for name, n in Counter(e.name for e in self.entries).items() if n == 1}
        todo = [e.name for e in self.entries if e.kind != "registry" and e.url == "."]
        out: set[str] = set()
        while todo:
            name = todo.pop()
            if name not in out:
                out.add(name)
                todo += [dep for dep in self.required.get(name, []) if dep in once]
        return out

    def emulated(self, target: Target, installed: Iterable[str]) -> set[str]:
        """Names whose install or version on the target may turn on a marker uv emulates (emulated_markers()), given
        the names uv installs there (`installed`). A package is in it when its fork markers depend on an emulated
        marker, or when an edge whose marker does leads to it from a package uv installs (or one already in it), and
        then so is everything below it. An edge from a package uv doesn't install can't explain uv's answer, so it
        doesn't count: otherwise a wrong row reached through it would be hidden."""
        live = frozenset(installed) | self.members
        cache_key = (target, live)
        if cache_key in self._emulated:
            return self._emulated[cache_key]
        variables = emulated_markers(target)
        checked: dict[str, bool] = self._depends.setdefault(target, {})

        def depends(marker: str) -> bool:
            if marker not in checked:
                checked[marker] = depends_on_emulation(marker, target, variables)
            return checked[marker]

        solid = self.solid
        out = {name for name, markers in self.fork_markers.items() if any(depends(m) for m in markers)} - solid
        changed = True
        while changed:
            changed = False
            for parent, edges in self.edges.items():
                inside = parent in out
                if not inside and parent not in live:
                    continue
                for dep, marker in edges:
                    if dep not in out and dep not in solid and (inside or (marker and depends(marker))):
                        out.add(dep)
                        changed = True
        self._emulated[cache_key] = out
        return out


def _filename(item: dict) -> str | None:
    if "filename" in item:
        return str(item["filename"])
    for field_name in ("url", "path"):
        if field_name in item:
            return unquote(urlsplit(str(item[field_name])).path.rsplit("/", 1)[-1])
    return None


def read_lock(path: Path) -> LockInfo:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries: list[LockEntry] = []
    edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    forks: dict[str, list[str]] = defaultdict(list)
    required: dict[str, list[str]] = defaultdict(list)
    for package in data.get("package", []):
        name = canonicalize_name(package["name"])
        source = package.get("source", {})
        kind = next(iter(source), "registry")
        url = str(source.get(kind, ""))
        files = [_filename(w) for w in package.get("wheels", [])]
        if "sdist" in package:
            files.append(_filename(package["sdist"]))
        entries.append(
            LockEntry(name, norm_version(str(package.get("version", ""))), kind, url, frozenset(f for f in files if f))
        )
        deps = list(package.get("dependencies", []))
        required[name] += [canonicalize_name(d["name"]) for d in deps if not d.get("marker")]
        for table in ("optional-dependencies", "dev-dependencies"):
            for items in package.get(table, {}).values():
                deps += items
        edges[name] += [(canonicalize_name(d["name"]), str(d.get("marker", ""))) for d in deps]
        forks[name] += [str(m) for m in package.get("resolution-markers", [])]
    return LockInfo(
        entries=entries,
        conflicts=[list(group) for group in data.get("conflicts", [])],
        edges=dict(edges),
        fork_markers=dict(forks),
        required=dict(required),
        requires_python=str(data.get("requires-python", "")),
    )


def emulation_reason(target: Target) -> str:
    if target.platform == "win-amd64":
        return (
            "uv's --python-platform x86_64-pc-windows-msvc sets platform_machine to x86_64 (CPython on Windows says "
            "AMD64), and this package's path in the lock turns on a marker uv emulates"
        )
    return (
        "this package's path in the lock turns on a marker uv's --python-platform can't stand in for "
        "(platform_release, platform_version, or a python_full_version patch release)"
    )


# ---------------------------------------------------------------------------------------------------------------
# The uv.lock referee: uv sync --frozen --dry-run


_INSTALLED = re.compile(r"^ \+ (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s]+)(?: \(.*\))?$", re.M)
_DIST_ERROR = re.compile(
    r"^error: Distribution `(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s`]+)(?: @ (?P<kind>[a-z]+)\+"
    r"(?P<url>[^`]+))?` can't be installed because (?P<why>.+)$",
    re.M,
)
# uv 0.13's LockErrorKind messages (crates/uv-lock/src/lock/mod.rs). With --no-build, every package without a
# usable wheel gets NO_BUILD (or NO_BINARY_NO_BUILD under a no-binary setting), whether or not it has an sdist.
NO_BUILD = "it is marked as `--no-build` but has no binary distribution"
NO_BINARY_NO_BUILD = "it is marked as both `--no-binary` and `--no-build`"
NO_BINARY = "it is marked as `--no-binary` but has no source distribution"
NEITHER = "it doesn't have a source distribution or wheel for the current platform"
WHEEL_ONLY_INCOMPATIBLE = "the binary distribution is incompatible with the current platform"
NO_BINARY_WHEEL_ONLY = "it is marked as `--no-binary` but is itself a binary distribution"
NO_WHEEL_REASONS = (NO_BUILD, NO_BINARY_NO_BUILD)
# Without --no-build: these leave the package uninstallable on the target (a project's own no-build or no-binary
# settings still apply, as they do for wheelgap).
MISSING_REASONS = (NEITHER, WHEEL_ONLY_INCOMPATIBLE, NO_BUILD, NO_BINARY, NO_BINARY_NO_BUILD, NO_BINARY_WHEEL_ONLY)
_ENVIRONMENTS = "The current Python platform is not compatible with the lockfile's supported environments"
_REQUIRES_PYTHON = re.compile(
    r"which is incompatible with the project's Python requirement|is not compatible with the locked Python requirement"
)
_UV_VERSION = re.compile(r"Required uv version `[^`]*` does not match the running version")
_BUILT = re.compile(r"^\s*(Building|Built) ", re.M)


@dataclass(frozen=True)
class DistError:
    name: str
    version: str
    source: str  # the registry URL, or "" for other kinds
    why: str


def parse_installed(output: str) -> list[tuple[str, str]]:
    """The (name, version) pairs of a successful dry run's `+ name==version` lines."""
    return [(canonicalize_name(m["name"]), norm_version(m["version"])) for m in _INSTALLED.finditer(output)]


def parse_dist_error(output: str) -> DistError | None:
    match = _DIST_ERROR.search(output)
    if not match:
        return None
    source = match["url"] if match["kind"] == "registry" else ""
    return DistError(canonicalize_name(match["name"]), norm_version(match["version"]), source or "", match["why"])


def first_error(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return next((line for line in lines if line.startswith("error:")), lines[-1] if lines else "no output")


def refusal(output: str) -> str | None:
    """Why uv refused the whole target, when it is one of the refusals wheelgap answers for at the target level."""
    if _ENVIRONMENTS in output:
        return "environments"
    if _REQUIRES_PYTHON.search(output):
        return "requires-python"
    if _UV_VERSION.search(output):
        return "uv-version"
    return None


def uv_env(target: Target, confdir: Path) -> dict[str, str]:
    """No UV_* settings, no user or system uv.toml (the project's own files are read), no wrapping, no venv."""
    drop = ("MACOSX_DEPLOYMENT_TARGET", "COLUMNS", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("UV_") and k.upper() not in drop}
    env.update(UV_NO_WRAP="1", XDG_CONFIG_HOME=str(confdir), XDG_CONFIG_DIRS=str(confdir), NO_COLOR="1")
    if target.platform.startswith("macos-"):
        env["MACOSX_DEPLOYMENT_TARGET"] = pin.uv_deployment_target(target)
    return env


def uv_python(target: Target) -> str:
    return f"{target.major}.{target.minor}{'t' if target.freethreaded else ''}"


def uv_sync_base(target: Target, triple: str) -> list[str]:
    return [
        *("uv", "sync", "--frozen", "--dry-run", "--offline", "--no-python-downloads", "--color", "never"),
        # A project's python-preference (only-managed) picks where the interpreter comes from, not what installs.
        *("--python", uv_python(target), "--python-preference", "system", "--python-platform", triple),
        *("--no-install-project", "--no-install-workspace", "--no-install-local"),
    ]


def conflict_selections(conflicts: list[list[dict[str, str]]], limit: int = 64) -> list[list[str]] | str:
    """One flag list per way of picking the conflicts: none or one item of each set, the others turned off.
    A string says why it can't be done (a package-level conflict, or too many combinations)."""
    options = []
    for group in conflicts:
        flags = []
        for item in group:
            if "extra" in item:
                flags.append(("--no-extra", str(item["extra"])))
            elif "group" in item:
                flags.append(("--no-group", str(item["group"])))
            else:
                return "a conflict between packages: uv sync --all-packages can't pick a side"
        options.append([flags, *([f for f in flags if f != keep] for keep in flags)])
    selections, seen = [], set()
    for combo in itertools.product(*options):
        picked = tuple(sorted({flag for part in combo for flag in part}))
        if picked not in seen:
            seen.add(picked)
            selections.append([arg for flag in picked for arg in flag])
        if len(selections) > limit:
            return f"more than {limit} ways to pick the conflicts"
    return selections or [[]]


class RefereeError(Exception):
    pass


@dataclass
class Selection:
    label: str
    flags: list[str]
    status: dict[str, str] = field(default_factory=dict)  # key -> wheel/source/missing
    error: str | None = None
    refused: str | None = None
    command: str = ""  # the last pass-1 command, to reproduce the install list
    runs: int = 0


def _shell(cmd: list[str], env_prefix: str = "") -> str:
    return (env_prefix + " " if env_prefix else "") + shlex.join(cmd)


def sync_selection(
    run: Runner, cwd: Path, env: dict[str, str], base: list[str], selection: Selection, lock: LockInfo
) -> Selection:
    """Fill selection.status from uv: pass 1 (--no-build) finds the wheels, pass 2 sorts the rest."""
    excluded = sorted(lock.skipped_names)

    def go(extra: list[str], exclude: Iterable[str]) -> tuple[int, str, list[str]]:
        cmd = [*base, *selection.flags, *extra, *(a for n in sorted(set(exclude)) for a in ("--no-install-package", n))]
        code, output = run(cmd, cwd, env)
        selection.runs += 1
        if _BUILT.search(output):
            raise RefereeError(f"uv built a package during a dry run: {first_error(output)}")
        if code < 0:  # run_command's timeout or OSError: not an answer, and never cached
            raise RefereeError(first_error(output))
        return code, output, cmd

    nowheel: dict[str, DistError] = {}
    for _ in range(MAX_RUNS):
        code, output, cmd = go(["--no-build"], [*excluded, *nowheel])
        selection.command = shlex.join(cmd)
        if code == 0:
            wheels = parse_installed(output)
            break
        err = parse_dist_error(output)
        if err and err.why in NO_WHEEL_REASONS and err.name not in nowheel and err.name not in excluded:
            nowheel[err.name] = err
            continue
        selection.refused = refusal(output)
        selection.error = first_error(output)
        return selection
    else:
        selection.error = f"more than {MAX_RUNS} packages without a wheel"
        return selection
    final_pass1 = [*excluded, *nowheel]

    missing: dict[str, DistError] = {}
    sources_listed: set[tuple[str, str]] = set()
    if nowheel:
        for _ in range(MAX_RUNS):
            code, output, _cmd = go([], [*excluded, *missing])
            if code == 0:
                sources_listed = set(parse_installed(output))
                break
            err = parse_dist_error(output)
            if err and err.why in MISSING_REASONS and err.name in nowheel and err.name not in missing:
                missing[err.name] = err
                continue
            selection.error = f"without --no-build: {first_error(output)}"
            return selection
        else:
            selection.error = f"more than {MAX_RUNS} packages missing"
            return selection

    def source_of(name: str, version: str, known: str = "") -> str:
        if known:
            return known
        found = lock.source_of(name, version)
        if found is not None or (name, version) not in lock.duplicates:
            return found or ""
        # Two registries lock this name and version: --no-binary-package makes uv name the entry it picked.
        exclude = [n for n in final_pass1 if n != name]
        _code, output, _cmd = go(["--no-build", "--no-binary-package", name], exclude)
        err = parse_dist_error(output)
        if not err or err.name != name or err.version != version or not err.source:
            raise RefereeError(f"couldn't tell which index uv takes {name}=={version} from: {first_error(output)}")
        return err.source

    status = {}
    for name, version in wheels:
        status[key(name, version, source_of(name, version))] = WHEEL
    for name, err in nowheel.items():
        if name in missing:
            status[key(name, err.version, source_of(name, err.version, err.source))] = MISSING
        elif (name, err.version) in sources_listed:
            status[key(name, err.version, source_of(name, err.version, err.source))] = SOURCE
        else:
            raise RefereeError(f"{name}=={err.version} has no wheel, but uv neither lists it nor says it's missing")
    selection.status = status
    return selection


def uv_sync_referee(run: Runner, cwd: Path, target: Target, lock: LockInfo, confdir: Path) -> dict:
    """The uv.lock referee's answer for one target, as a JSON-able dict (it's cached)."""
    triple = pin.uv_platform(target)
    if triple is None:
        return {"error": "uv's --python-platform can't express this target"}
    env = uv_env(target, confdir)
    base = uv_sync_base(target, triple)
    union = conflict_selections(lock.conflicts)
    if isinstance(union, str):
        return {"error": union}
    selections = [Selection("default", [])]
    for flags in union:
        label = "all" + ("" if not flags else " " + " ".join(flags))
        selections.append(Selection(label, ["--all-packages", "--all-extras", "--all-groups", *flags]))
    prefix = f"MACOSX_DEPLOYMENT_TARGET={env['MACOSX_DEPLOYMENT_TARGET']}" if "MACOSX_DEPLOYMENT_TARGET" in env else ""
    out: dict[str, Any] = {"tool": "uv sync", "triple": triple, "selections": []}
    try:
        for selection in selections:
            sync_selection(run, cwd, env, base, selection, lock)
            out["selections"].append(
                {
                    "label": selection.label,
                    "flags": selection.flags,
                    "status": selection.status,
                    "error": selection.error,
                    "refused": selection.refused,
                    "command": _shell(shlex.split(selection.command), prefix) if selection.command else "",
                    "runs": selection.runs,
                }
            )
    except RefereeError as exc:
        return {"error": str(exc)}
    code, output = run(["uv", "python", "find", "--no-python-downloads", uv_python(target)], cwd, env)
    out["interpreter"] = output.strip().splitlines()[0] if code == 0 and output.strip() else ""
    return out


# ---------------------------------------------------------------------------------------------------------------
# The requirements referees: pip, and uv pip compile


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    marker: str
    line: str  # the line as given to the installers, without --hash options


@dataclass
class Flat:
    pins: list[Pin]
    options: list[str]
    refuse: str | None = None


# A whole line that is one index option and its value: pip reads every option on a line (`--pre --no-binary :all:`
# turns builds back on), so a line that only starts with one of these isn't enough. No `$` in the value: pip and uv
# expand ${VAR} from this machine's environment, which would send it to the file's index.
_SAFE_OPTION = re.compile(
    r"^(?:(?:-i|--index-url|--extra-index-url|--trusted-host)(?:\s*=\s*|\s+)(?!-)[^\s$]+|--pre|--prefer-binary)$"
)
_INCLUDE = re.compile(r"^(?:-r|--requirement)(?:\s*=\s*|\s+|(?=\S))(\S+)$")


def flatten(path: Path, seen: set[Path] | None = None, root: Path | None = None) -> Flat:
    """A requirements file's pins and options, with its -r includes read in, for one flat file to give pip and uv.
    An include must stay inside the top file's folder (the staged copy): `-r /etc/x` or `-r ../../x` would read a
    file of this machine's."""
    seen = seen if seen is not None else set()
    root = root if root is not None else path.parent.resolve()
    seen.add(path.resolve())
    flat = Flat([], [])
    for _, line in requirement_lines(path.read_text(encoding="utf-8", errors="replace")):
        if include := _INCLUDE.match(line):
            child = (path.parent / include.group(1)).resolve()
            if not child.is_relative_to(root):
                return Flat([], [], refuse=f"{line}: include outside the input's folder")
            if child in seen or not child.is_file():
                return Flat([], [], refuse=f"{line}: include missing or repeated")
            sub = flatten(child, seen, root)
            if sub.refuse:
                return sub
            flat.pins += sub.pins
            flat.options += sub.options
            continue
        if line.startswith("-"):
            # Only options that change where pip and uv look, never what they may build: anything else (an
            # --only-binary :none: or --no-binary that overrides --only-binary=:all:, --find-links to a local
            # sdist, -c, -e, --config-settings, ...) refuses the file, so no referee ever runs a build backend.
            if not _SAFE_OPTION.match(line):
                return Flat([], [], refuse=f"{line.split()[0]} in the file: only index options are passed to referees")
            flat.options.append(line)
            continue
        text = strip_hashes(line)
        try:
            req = Requirement(text)
        except InvalidRequirement as exc:
            return Flat([], [], refuse=f"{text}: {exc}")
        pinned = [s.version for s in req.specifier if s.operator in ("==", "===")]
        if req.url or len(pinned) != 1 or "*" in pinned[0]:
            return Flat([], [], refuse=f"{text}: not pinned to one version")
        flat.pins.append(Pin(canonicalize_name(req.name), norm_version(pinned[0]), str(req.marker or ""), text))
    return flat


def pip_command(target: Target, requirements: Path, report: Path, cache: Path) -> list[str]:
    abi = f"cp{target.major}{target.minor}{'t' if target.freethreaded else ''}"
    platforms = [arg for plat in pin.pip_platforms(target) for arg in ("--platform", plat)]
    return [
        *(sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--no-deps"),
        *("--only-binary=:all:", "--index-url", pin.INDEX, *platforms),
        *("--python-version", f"{target.major}.{target.minor}", "--implementation", "cp", "--abi", abi),
        *("-r", str(requirements), "--report", str(report)),
        *("--isolated", "--disable-pip-version-check", "--no-input", "-vv", "--cache-dir", str(cache)),
    ]


# What pip logs before it runs a build backend (to read an sdist's metadata or build a wheel).
_PIP_BUILT = re.compile(r"Preparing metadata|Getting requirements to build|Building wheel")
_PIP_NO_MATCH = re.compile(r"No matching distribution found for (?P<req>\S+)")
_PIP_NO_SOURCES = re.compile(r"Skipping link: No sources permitted for [^:]+: (?P<url>\S+)")


def pip_installed(report: Path) -> dict[tuple[str, str], str] | None:
    """(name, version) -> wheel filename from pip's --report; None if it wrote none."""
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        return {
            (canonicalize_name(item["metadata"]["name"]), norm_version(item["metadata"]["version"])): unquote(
                urlsplit(item["download_info"]["url"]).path.rsplit("/", 1)[-1]
            )
            for item in data["install"]
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def pip_sdist_seen(log: str, name: str, version: str) -> bool:
    """pip's -vv log shows an sdist of exactly name==version that only --only-binary kept it from using."""
    for match in _PIP_NO_SOURCES.finditer(log):
        filename = unquote(urlsplit(match["url"]).path.rsplit("/", 1)[-1])
        try:
            sdist_name, sdist_version = parse_sdist_filename(filename)
        except ValueError:
            continue
        if sdist_name == name and str(sdist_version) == version:
            return True
    return False


def pip_referee(run: Runner, folder: Path, target: Target, flat: Flat, cache: Path) -> dict:
    """pip's answer for the file's unmarked pins: wheel, source or missing each, with check_against_pip's evidence."""
    pins = [p for p in flat.pins if not p.marker]
    status: dict[str, str] = {}
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_")}
    runs, command = 0, ""
    with tempfile.TemporaryDirectory(prefix="wheelgap-projects-pip-") as tmp:
        requirements, report = Path(tmp) / "requirements.txt", Path(tmp) / "report.json"
        left = list(pins)
        for _ in range(MAX_RUNS):
            if not left:
                break
            requirements.write_text("\n".join([*flat.options, *(p.line for p in left)]) + "\n", encoding="utf-8")
            report.unlink(missing_ok=True)
            cmd = pip_command(target, requirements, report, cache)
            # As a user would type it: without the report and cache paths, which are this run's own.
            shown = [*cmd[3 : cmd.index("--report")], *cmd[cmd.index("--report") + 2 : cmd.index("--cache-dir")]]
            command = shlex.join(["pip", *shown]).replace(str(requirements), "requirements.txt")
            code, log = run(cmd, folder, env)
            runs += 1
            if built := _PIP_BUILT.search(log):
                return {"error": f"pip started a build ({built[0]}): the input must be refused", "command": command}
            if code == 0:
                planned = pip_installed(report)
                if planned is None:
                    return {"error": "pip succeeded but wrote no report"}
                for p in left:
                    filename = planned.get((p.name, p.version))
                    if filename is None or not filename.endswith(".whl"):
                        return {"error": f"pip's report has no wheel for {p.name}=={p.version}"}
                    status[key(p.name, p.version)] = WHEEL
                left = []
                break
            match = _PIP_NO_MATCH.search(log)
            failed = None
            if match:
                try:
                    req = Requirement(match["req"])
                    failed = next((p for p in left if p.name == canonicalize_name(req.name)), None)
                except InvalidRequirement:
                    failed = None
            if failed is None:
                return {"error": f"pip: {first_error(log.replace('ERROR:', 'error:'))}", "command": command}
            answer = pin.classify(failed.name, code, log, None)
            if answer.error:
                return {"error": f"{failed.name}: {answer.error}", "command": command}
            status[key(failed.name, failed.version)] = (
                SOURCE if pip_sdist_seen(log, failed.name, failed.version) else MISSING
            )
            left = [p for p in left if p is not failed]
    not_compared = {
        key(p.name, p.version): "pip evaluates markers for the machine it runs on" for p in flat.pins if p.marker
    }
    return {"tool": "pip", "status": status, "not_compared": not_compared, "command": command, "runs": runs}


# uv 0.13 names a requirement with a marker as `name{marker}==version`.
_UV_NO_WHEEL = re.compile(
    r"Because (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<marker>\{[^}]*\})?==(?P<version>\S+) has (?:no usable wheels|"
    r"no wheels with a matching (?:Python ABI|Python implementation|platform) tag(?: \(e\.g\., `[^`]+`\))?) and "
    r"you require (?P=name)(?P=marker)?==(?P=version)(?:,| and)"
)


def uv_compile_command(target: Target, triple: str, requirements: Path) -> list[str]:
    return [
        *("uv", "pip", "compile", str(requirements), "--no-deps", "--only-binary", ":all:", "--no-config"),
        *("--no-cache", "--default-index", pin.INDEX, "--python-version", f"{target.major}.{target.minor}"),
        *("--python-platform", triple, "--no-python-downloads", "--no-header", "--no-annotate", "--color", "never"),
        *("--python", sys.executable),
    ]


def uv_pip_referee(run: Runner, folder: Path, target: Target, flat: Flat, confdir: Path) -> dict:
    """uv's answer for every pin: wheel, no wheel, or left out by its marker (not in the output)."""
    triple = pin.uv_platform(target)
    if triple is None:
        return {"error": "uv's --python-platform can't express this target"}
    env = uv_env(target, confdir)
    status: dict[str, str] = {}
    left = list(flat.pins)
    runs, command = 0, ""
    with tempfile.TemporaryDirectory(prefix="wheelgap-projects-uv-") as tmp:
        requirements = Path(tmp) / "requirements.txt"
        for _ in range(MAX_RUNS):
            lines = [
                *flat.options,
                *(p.line + (f" ; {p.marker}" if p.marker and ";" not in p.line else "") for p in left),
            ]
            requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
            cmd = uv_compile_command(target, triple, requirements)
            command = shlex.join([*cmd[:3], "requirements.txt", *cmd[4:-2]])
            if target.platform.startswith("macos-"):
                command = f"MACOSX_DEPLOYMENT_TARGET={env['MACOSX_DEPLOYMENT_TARGET']} {command}"
            code, output = run(cmd, folder, env)
            runs += 1
            if _BUILT.search(output):
                return {"error": f"uv built a package: {first_error(output)}", "command": command}
            if code == 0:
                listed = {
                    (canonicalize_name(n), norm_version(v))
                    for n, v in re.findall(r"^([A-Za-z0-9][\w.-]*)==(\S+)$", output, re.M)
                }
                for p in left:
                    if (p.name, p.version) in listed:
                        status[key(p.name, p.version)] = WHEEL
                break
            match = _UV_NO_WHEEL.search(output)
            failed = match and next((p for p in left if p.name == canonicalize_name(match["name"])), None)
            if not failed:
                return {"error": f"uv: {first_error(output)}", "command": command}
            status[key(failed.name, failed.version)] = NO_WHEEL
            left = [p for p in left if p is not failed]
    return {"tool": "uv pip compile", "status": status, "command": command, "runs": runs}


# ---------------------------------------------------------------------------------------------------------------
# poetry.lock: Poetry's own export of the lock, then uv pip compile for the exported file

POETRY_VERSION = "2.5.2"
POETRY_EXPORT_VERSION = "1.10.2"
POETRY = (
    *("uvx", "--from", f"poetry=={POETRY_VERSION}", "--with", f"poetry-plugin-export=={POETRY_EXPORT_VERSION}"),
    "poetry",
)
# What poetry export may find in the staged folder. With no poetry.lock it would lock (resolve, maybe build), and a
# .poetry/plugins folder there is put on its sys.path, with its .pth files run, by every command.
POETRY_FILES = frozenset({"pyproject.toml", "poetry.lock"})
DEAD_PROXY = "http://127.0.0.1:9"  # nothing listens on the discard port: any request Poetry makes fails at once


def poetry_env(home: Path) -> dict[str, str]:
    """No POETRY_* settings (a user's repositories or credentials), Poetry's config, cache and data in a fresh folder,
    no keyring, and no network: uvx runs the Poetry it has already installed, and every proxy goes nowhere."""
    drop = ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME", "NO_PROXY", "no_proxy")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("POETRY_", "PIP_")) and k not in drop and k.lower() not in ("all_proxy",)
    }
    for name in ("config", "cache", "data"):
        (home / name).mkdir(parents=True, exist_ok=True)
    env.update(
        POETRY_CONFIG_DIR=str(home / "config"),
        POETRY_CACHE_DIR=str(home / "cache"),
        POETRY_DATA_DIR=str(home / "data"),
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
        UV_OFFLINE="1",
        NO_COLOR="1",
    )
    for proxy in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[proxy] = DEAD_PROXY
    return env


def poetry_groups(pyproject: Path) -> list[str] | None:
    """The groups a plain `poetry install` installs besides main: every group pyproject.toml declares that isn't
    `optional = true` (a [tool.poetry.group.NAME] table, the old [tool.poetry.dev-dependencies] one as `dev`, and
    [dependency-groups]). None if pyproject.toml can't be read."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    poetry = data.get("tool", {}).get("poetry", {})
    tables = poetry.get("group", {}) if isinstance(poetry, dict) else {}
    if not isinstance(tables, dict):
        return None
    names = [str(n) for n in tables]
    if isinstance(poetry, dict) and isinstance(poetry.get("dev-dependencies"), dict):
        names.append("dev")
    pep735 = data.get("dependency-groups", {})
    names += [str(n) for n in pep735] if isinstance(pep735, dict) else []
    optional = {canonicalize_name(n) for n, t in tables.items() if isinstance(t, dict) and t.get("optional") is True}
    return sorted({canonicalize_name(n) for n in names} - optional - {"main"})


def poetry_export_command(folder: Path, output: Path, mode: str, groups: Sequence[str]) -> list[str]:
    """`all`: wheelgap's default for a poetry.lock, every group and extra. `default`: a plain `poetry install`, main
    and the non-optional groups, no extras (export itself only takes main unless told)."""
    select = ["--all-groups", "--all-extras"] if mode == "all" else [f"--with={g}" for g in groups]
    return [
        *POETRY,
        *("export", "--directory", str(folder), "--format", "requirements.txt", "--without-hashes", *select),
        *("--output", str(output), "--no-interaction", "--no-ansi"),
    ]


def poetry_python(lock: Path) -> SpecifierSet | None:
    """The project's Python range, from the lock's [metadata] python-versions (Poetry writes ^ and ~ there too); None
    if it can't be read for sure (an `||`, a space between two clauses, a bare version: Poetry's own syntax)."""
    try:
        text = tomllib.loads(lock.read_text(encoding="utf-8")).get("metadata", {}).get("python-versions")
    except (OSError, ValueError):
        return None
    if not isinstance(text, str) or "||" in text:
        return None
    specs = []
    for part in (p.strip() for p in text.split(",")):
        if match := re.fullmatch(r"([\^~])\s*(\d+)((?:\.\d+)*)", part):
            op, release = match[1], [int(n) for n in f"{match[2]}{match[3]}".split(".")]
            # ^3.9.1 is >=3.9.1,<4; ~3.9.1 and ~3.9 are >=3.9(.1),<3.10; ~3 is >=3,<4
            high = f"{release[0] + 1}" if op == "^" or len(release) == 1 else f"{release[0]}.{release[1] + 1}"
            specs += [f">={'.'.join(map(str, release))}", f"<{high}"]
        elif part != "*":
            specs.append(part)
    try:
        return SpecifierSet(",".join(specs))
    except InvalidSpecifier:
        return None


def poetry_lock_has_groups(lock: Path) -> bool:
    """Whether every package in the lock lists its groups (Poetry 2.x, lock-version 2.1), as wheelgap's group
    selection needs."""
    try:
        packages = tomllib.loads(lock.read_text(encoding="utf-8")).get("package", [])
    except (OSError, ValueError):
        return False
    return isinstance(packages, list) and all(
        isinstance(p, dict) and isinstance(p.get("groups"), list) for p in packages
    )


def poetry_referee(
    run: Runner, folder: Path, target: Target, flat: Flat, confdir: Path, python: SpecifierSet | None
) -> dict:
    """uv pip compile's answer for Poetry's export, or a refusal of a Python the project leaves out. A range that
    takes some of the target's patch releases and not others (>=3.13.2) can't say which one Poetry would run on."""
    if python is None:
        return {"error": "poetry.lock's python-versions can't be read for sure"}
    version = f"{target.major}.{target.minor}"
    first, late = (python.contains(f"{version}.{patch}", prereleases=True) for patch in (0, 99))
    if not first and not late:
        return {"refused": "requires-python", "command": f"poetry.lock's python-versions is {python}"}
    if first != late:
        return {"error": f"poetry.lock's python-versions ({python}) takes only some {version} patch releases"}
    return uv_pip_referee(run, folder, target, flat, confdir)


def folder_digest(folder: Path) -> dict[str, str]:
    """Each entry's name and the sha256 of its bytes (a folder's is empty), so a rewritten file shows too."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "" for p in sorted(folder.iterdir())}


def poetry_export(run: Runner, folder: Path, work: Path, mode: str) -> tuple[Flat | None, str, str]:
    """(the exported file's pins, or None; why not; the command, as a user would type it) for one selection mode."""
    found = {p.name for p in folder.iterdir()}
    if found != POETRY_FILES:
        # Without pyproject.toml Poetry looks for one in the folders above; without poetry.lock it would lock.
        return None, f"poetry export would see {sorted(found)}, not just pyproject.toml and poetry.lock", ""
    groups = poetry_groups(folder / "pyproject.toml")
    if groups is None:
        return None, "pyproject.toml can't be read for its groups", ""
    out_dir = work / "_poetry_export" / mode
    out_dir.mkdir(parents=True, exist_ok=True)
    # uvx installs the pinned Poetry first, if it must, with the network and away from the project: nothing of the
    # input is in this command. The export itself then runs offline.
    code, log = run(
        [*POETRY, "--version"], out_dir, {k: v for k, v in os.environ.items() if not k.startswith("POETRY_")}
    )
    if code != 0 or POETRY_VERSION not in log:
        return None, f"Poetry {POETRY_VERSION} isn't available through uvx: {first_error(log)}", ""
    output = out_dir / "requirements.txt"
    cmd = poetry_export_command(folder, output, mode, groups)
    shown = shlex.join(["poetry", *cmd[len(POETRY) :]]).replace(str(folder), ".").replace(str(output), "exported.txt")
    before = folder_digest(folder)
    code, log = run(cmd, folder, poetry_env(work / "_poetry_home"))
    if folder_digest(folder) != before:
        return None, "poetry export wrote into the project folder: the input must be refused", shown
    if re.search(r"\bLocking\b|Resolving dependencies|Building ", log):
        return None, "poetry export started to lock or build: the input must be refused", shown
    if code != 0 or not output.is_file():
        lines = [line.strip() for line in log.splitlines() if line.strip()]
        return None, f"poetry export failed: {lines[-1] if lines else 'no output'}", shown
    # A path, URL or git dependency is exported as `-e file:///...` or `name @ URL`: wheelgap skips those packages,
    # so they're left out of both sides (as for a uv.lock); the referee never sees them.
    lines = output.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not (line.startswith("-e ") or re.match(r"^[A-Za-z0-9][\w.\[\],-]*\s+@\s", line))]
    output.write_text("\n".join(kept) + "\n", encoding="utf-8")
    flat = flatten(output)
    if flat.refuse:
        return None, f"the exported file: {flat.refuse}", shown
    return flat, "", shown


# ---------------------------------------------------------------------------------------------------------------
# wheelgap's side


def wheelgap_answer(path: Path, target: Target, lock: LockInfo | None, mode: str) -> dict:
    """wheelgap's rows on one target: key -> status, for every row that isn't n/a; or why it refused."""
    selectors: dict[str, Any] = {} if mode == "all" else {"extras": [], "groups": []}
    try:
        results = check_lockfile(path, [target], **selectors)
    except SourceError as exc:
        return {"refused": str(exc)}
    rows: dict[str, str] = {}
    ambiguous: list[str] = []
    found: dict[str, Result] = {}
    for result in results:
        status = result.statuses.get(target)
        if status is None or status in (Status.NOT_NEEDED, Status.BUNDLED):
            continue
        if result.version is None:
            rows[key(result.name, "?")] = UNKNOWN
            continue
        source = ""
        if lock is not None:
            source = lock.source_of(result.name, result.version, result.files) or ""
            if not source:
                ambiguous.append(key(result.name, result.version))
        cell = key(result.name, result.version, source)
        rows[cell] = UNKNOWN if result.error else status.value
        found[cell] = result
    return {"rows": rows, "ambiguous": ambiguous, "results": found}


# ---------------------------------------------------------------------------------------------------------------
# Comparing


@dataclass
class Cell:
    input: str
    kind: str
    referee: str
    mode: str
    target: str
    package: str
    wheelgap: str  # a status, or "absent"
    answer: str  # the referee's status, or "absent"
    verdict: str
    why: str = ""
    command: str = ""
    in_default: bool | None = None


ABSENT = "absent"


def same(wheelgap: str, answer: str, referee: str) -> bool:
    if referee in ("uv pip compile", "poetry export"):
        return (wheelgap == WHEEL) == (answer == WHEEL) and (wheelgap == ABSENT) == (answer == ABSENT)
    return wheelgap == answer


def compare(
    *,
    entry: dict,
    referee: str,
    mode: str,
    target: Target,
    wheelgap: dict,
    answer: dict,
    lock: LockInfo | None = None,
    default_keys: set[str] | None = None,
) -> list[Cell]:
    """The cells of one input, target, referee and selection mode."""
    label = pin.label(target)

    def cell(package: str, w: str, a: str, verdict: str, why: str = "", command: str = "") -> Cell:
        return Cell(entry["id"], entry["kind"], referee, mode, label, package, w, a, verdict, why, command)

    if answer.get("error"):
        return [cell("*", "-", "-", NOT_COMPARED, f"{referee}: {answer['error']}", answer.get("command", ""))]
    if "refused" in wheelgap or answer.get("refused"):
        w_refused, a_refused = wheelgap.get("refused"), answer.get("refused")
        if w_refused and a_refused:
            return [
                cell(
                    "*", "refused", "refused", AGREE, f"both refuse the target ({a_refused})", answer.get("command", "")
                )
            ]
        if a_refused == "requires-python" and not w_refused:
            why = f"{'uv' if referee != 'poetry export' else 'Poetry'} refuses a Python outside the project's "
            why += "requires-python; wheelgap answers for it by design"
            return [cell("*", "answered", "refused", NOT_COMPARED, why, answer.get("command", ""))]
        if a_refused == "uv-version":
            why = "the project's required-version needs a newer uv than the referee's"
            return [cell("*", "-", "refused", NOT_COMPARED, why, answer.get("command", ""))]
        what = f"wheelgap refuses: {w_refused}" if w_refused else f"{referee} refuses ({a_refused}), wheelgap answers"
        return [
            cell(
                "*",
                "refused" if w_refused else "answered",
                "refused" if a_refused else "answered",
                DISAGREE,
                what,
                answer.get("command", ""),
            )
        ]

    rows: dict[str, str] = wheelgap["rows"]
    status: dict[str, str] = answer["status"]
    commands: dict[str, str] = answer.get("commands", {})
    skipped: dict[str, str] = answer.get("not_compared", {})
    installed = {split_key(k)[0] for k in status}
    emulated = lock.emulated(target, installed) if lock is not None else set()
    collisions = lock.collisions if lock is not None else set()
    ambiguous = set(wheelgap.get("ambiguous", []))
    results: dict[str, Result] = wheelgap.get("results", {})
    cells = []
    for package in sorted(set(rows) | set(status)):
        w, a = rows.get(package, ABSENT), status.get(package, ABSENT)
        name, version, _ = split_key(package)
        command = commands.get(package, answer.get("command", ""))
        why = ""
        if package in skipped:
            why = skipped[package]
        elif w == UNKNOWN:
            why = "wheelgap says unknown"
        elif name in collisions:
            why = "the lock has this name from a registry and from git or a URL: --no-install-package can't keep one"
        elif key(name, version) in ambiguous and w != ABSENT:
            why = "this name and version come from two indexes, and wheelgap's row matches neither lock entry's files"
        elif name in emulated:  # agreeing or not: uv's answer for it can't vouch for the target's
            why = emulation_reason(target)
        elif referee == "pip" and a == SOURCE and package in results:
            capped = sorted(
                why for f, why in results[package].requires_python_rejected.items() if not f.endswith(".whl")
            )
            if capped:
                why = (
                    f"pip's --only-binary run skips the sdist before it reads its Requires-Python, which {capped[0]}: "
                    "pip can't show here whether it would build it"
                )
        elif (
            referee in ("uv pip compile", "poetry export")
            and w in (SOURCE, MISSING)
            and a == WHEEL
            and package in results
        ):
            known = pin.uv_reads_requires_python_as_a_floor(
                results[package], Status(w), pin.InstallerAnswer(wheel="wheel")
            )
            why = known or ""
        if why:
            cells.append(cell(package, w, a, NOT_COMPARED, why, command))
            continue
        verdict = AGREE if same(w, a, referee) else DISAGREE
        c = cell(package, w, a, verdict, "" if verdict == AGREE else mismatch(w, a), command)
        if default_keys is not None:
            c.in_default = package in default_keys
        cells.append(c)
    return cells


def mismatch(wheelgap: str, answer: str) -> str:
    if answer == ABSENT:
        return "wheelgap lists a package the referee doesn't install"
    if wheelgap == ABSENT:
        return "the referee installs a package wheelgap doesn't list"
    return f"wheelgap says {wheelgap}, the referee {answer}"


def union(answer: dict) -> tuple[dict, set[str] | None, dict]:
    """The uv sync referee's union answer (every selection but `default`), its default keys, and the default
    answer, each as {"status": ..., "error": ..., "refused": ..., "commands": ...}."""
    if answer.get("error"):
        return answer, None, answer
    selections = answer["selections"]
    default = next(s for s in selections if s["label"] == "default")
    rest = [s for s in selections if s["label"] != "default"]
    ok = [s for s in rest if not s["error"]]
    if not ok:
        first = rest[0] if rest else default
        out = {
            "error": None if first["refused"] else first["error"],
            "refused": first["refused"],
            "command": first["command"],
        }
        if not first["refused"]:
            out["error"] = first["error"]
    else:
        status: dict[str, str] = {}
        commands: dict[str, str] = {}
        clash = []
        for s in ok:
            for package, st in s["status"].items():
                if status.setdefault(package, st) != st:
                    clash.append(package)
                commands.setdefault(package, s["command"])
        out = {"status": status, "commands": commands, "command": ok[0]["command"]}
        if clash:
            out["not_compared"] = {p: "uv gives this entry different answers in two selections" for p in clash}
        refused_sides = [s["label"] for s in rest if s["error"]]
        if refused_sides:
            out["partial"] = refused_sides
    if default["error"]:
        default_answer = {"error": None, "refused": default["refused"], "command": default["command"]}
        if not default["refused"]:
            default_answer["error"] = default["error"]
    else:
        default_answer = {"status": default["status"], "command": default["command"]}
    default_keys = set(default["status"]) if not default["error"] else None
    return out, default_keys, default_answer


# ---------------------------------------------------------------------------------------------------------------
# Running, caching and reporting


def tool_versions() -> dict[str, str]:
    def first_line(cmd: list[str]) -> str:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"unavailable ({exc})"
        if proc.returncode != 0:
            return "unavailable"
        return next(iter((proc.stdout or proc.stderr).strip().splitlines()), "")

    pip_version = re.sub(r" from .* \(python", " (python", first_line([sys.executable, "-m", "pip", "--version"]))
    # The wheelgap being measured is the one imported, which needn't be this script's checkout: name the last commit
    # that changed its source (what the answers depend on), the checkout's HEAD, and whether the source is edited.
    package = Path(wheelgap.__file__).resolve().parent
    git = ["git", "-C", str(package)]
    src = first_line([*git, "log", "-1", "--format=%H", "--", "."])
    dirty = first_line([*git, "status", "--porcelain", "--", "."]) not in ("", "unavailable")
    return {
        "uv": first_line(["uv", "--version"]),
        "pip": pip_version,
        "poetry": f"poetry {POETRY_VERSION}, poetry-plugin-export {POETRY_EXPORT_VERSION} (uvx)",
        "wheelgap": __version__,
        "wheelgap_commit": src + (" (edited)" if dirty else ""),
        "checkout_head": first_line([*git, "rev-parse", "HEAD"]),
    }


def cache_path(cache: Path, digest: str, referee: str, target: Target, version: str, command: Sequence[str]) -> Path:
    """Where a referee's answer is kept: keyed by the input's files (digest), the target in full, the tool's version,
    the referee's exact command for the target and its macOS deployment target, this script's referee version, and,
    for every referee but uv sync (pip and uv pip compile read the live index), today's UTC date."""
    parts = [str(REFEREE_VERSION[referee]), referee, version, repr(target), shlex.join(command)]
    if target.platform.startswith("macos-"):
        parts.append(pin.uv_deployment_target(target))
    if referee != "uv sync":
        parts.append(datetime.datetime.now(datetime.UTC).date().isoformat())
    tag = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
    return cache / digest / f"{referee.replace(' ', '-')}--{pin.label(target).replace(' ', '_')}--{tag}.json"


def cacheable(answer: dict) -> bool:
    """An answer worth keeping: no error (one may be the network's or a timeout's), and every uv sync selection
    either answered or was a refusal uv makes for the input itself (environments, requires-python, uv version)."""
    if answer.get("error"):
        return False
    return all(not s.get("error") or s.get("refused") for s in answer.get("selections", []))


def cached(path: Path, compute: Callable[[], dict]) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    answer = compute()
    if cacheable(answer):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(answer, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return answer


def default_targets() -> list[Target]:
    return [t for python in PYTHONS for t in make_targets(python, list(PLATFORMS))]


def pick_targets(names: Sequence[str]) -> list[Target]:
    targets = default_targets()
    if names:
        targets = [t for t in targets if pin.label(t) in names or t.platform in names]
    return targets


OWN_PYTHONS = ("3.14", "3.12", "3.11", "3.10", "3.9")


def own_targets(requires_python: str, targets: Sequence[Target]) -> list[Target]:
    """When a uv.lock's requires-python (or a poetry.lock's python-versions) leaves out every Python among the
    targets (`==3.11.*`), the newest Python it allows on the same platforms, so the project is compared at all (uv
    and Poetry refuse a Python outside it)."""
    try:
        spec = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return []
    allowed = [p for p in OWN_PYTHONS if spec.contains(f"{p}.99") or spec.contains(f"{p}.0")]
    pythons = {(t.major, t.minor) for t in targets}
    if not allowed or any(
        spec.contains(f"{major}.{minor}.99") or spec.contains(f"{major}.{minor}.0") for major, minor in pythons
    ):
        return []
    platforms = list(dict.fromkeys(t.platform for t in targets))
    return make_targets(allowed[0], platforms)


def check_input(
    entry: dict,
    targets: Sequence[Target],
    out: Path,
    versions: dict[str, str],
    run: Runner,
    workers: int,
    own_python: bool = True,
) -> list[Cell]:
    """Every cell of one input on every target."""
    work = Path(tempfile.mkdtemp(prefix="wheelgap-projects-"))
    try:
        return _check_staged(entry, targets, out, versions, run, workers, own_python, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _check_staged(
    entry: dict,
    targets: Sequence[Target],
    out: Path,
    versions: dict[str, str],
    run: Runner,
    workers: int,
    own_python: bool,
    work: Path,
) -> list[Cell]:
    """check_input()'s work, with the input copied into `work` (a fresh temporary folder: uv must find no project
    above it, and nothing of the input is left behind)."""
    kind = entry["kind"]
    check_workdir(work)
    folder = stage(entry, work)
    digest = folder.name
    confdir = work / "_no_uv_config"
    confdir.mkdir(parents=True, exist_ok=True)
    main = folder / entry["main"]
    cells: list[Cell] = []
    cache = out / "cache"
    if kind == "uv.lock":
        lock = read_lock(main)
        version = versions["uv"]
        if own_python:
            targets = [*targets, *own_targets(lock.requires_python, targets)]

        def ask(target: Target) -> dict:
            command = uv_sync_base(target, pin.uv_platform(target) or "")
            return cached(
                cache_path(cache, digest, "uv sync", target, version, command),
                lambda: uv_sync_referee(run, folder, target, lock, confdir),
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            answers = list(pool.map(ask, targets))
        for target, answer in zip(targets, answers, strict=True):
            all_answer, default_keys, default_answer = union(answer)
            for mode, ref in (("all", all_answer), ("default", default_answer)):
                ours = wheelgap_answer(main, target, lock, mode)
                cells += compare(
                    entry=entry,
                    referee="uv sync",
                    mode=mode,
                    target=target,
                    wheelgap=ours,
                    answer=ref,
                    lock=lock,
                    default_keys=default_keys if mode == "all" else None,
                )
                if mode == "all" and all_answer.get("partial"):
                    for c in cells:
                        if (
                            c.target == pin.label(target)
                            and c.verdict == DISAGREE
                            and c.wheelgap != ABSENT
                            and c.answer == ABSENT
                        ):
                            c.verdict, c.why = (
                                NOT_COMPARED,
                                "uv refused some ways of picking the conflicts: " + ", ".join(all_answer["partial"]),
                            )
        return cells
    if kind == "poetry.lock":
        python = poetry_python(main) if main.is_file() else None
        if own_python and python is not None:
            targets = [*targets, *own_targets(str(python), targets)]  # as for a uv.lock
        everything: dict[str, dict] = {}  # the "all" export's answers, by target label
        for mode in ("all", "default"):
            got, by_target = _check_poetry(entry, targets, folder, work, out, versions, run, workers, mode, everything)
            cells += got
            everything = by_target if mode == "all" else everything
        return cells
    flat = flatten(main)
    if flat.refuse:
        return [
            Cell(entry["id"], kind, "-", "all", pin.label(t), "*", "-", "-", NOT_COMPARED, flat.refuse) for t in targets
        ]
    pip_cache = out / "cache" / "_pip-http"

    def ask_pip(target: Target) -> dict:
        command = pip_command(target, Path("requirements.txt"), Path("report.json"), Path("cache"))
        return cached(
            cache_path(cache, digest, "pip", target, versions["pip"], command),
            lambda: pip_referee(run, folder, target, flat, pip_cache),
        )

    def ask_uv(target: Target) -> dict:
        command = uv_compile_command(target, pin.uv_platform(target) or "", Path("requirements.txt"))
        return cached(
            cache_path(cache, digest, "uv pip compile", target, versions["uv"], command),
            lambda: uv_pip_referee(run, folder, target, flat, confdir),
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pips = list(pool.map(ask_pip, targets))
        uvs = list(pool.map(ask_uv, targets))
    for target, pip_answer, uv_answer in zip(targets, pips, uvs, strict=True):
        ours = wheelgap_answer(main, target, None, "all")
        marked = {key(p.name, p.version) for p in flat.pins if p.marker}
        pip_side = (
            dict(ours, rows={k: v for k, v in ours.get("rows", {}).items() if k not in marked})
            if "rows" in ours
            else ours
        )
        if "status" in pip_answer:
            pip_answer = dict(pip_answer, status={**pip_answer["status"]})
        cells += compare(entry=entry, referee="pip", mode="all", target=target, wheelgap=pip_side, answer=pip_answer)
        cells += [
            Cell(
                entry["id"],
                kind,
                "pip",
                "all",
                pin.label(target),
                p,
                ours.get("rows", {}).get(p, ABSENT),
                "-",
                NOT_COMPARED,
                why,
            )
            for p, why in pip_answer.get("not_compared", {}).items()
        ]
        cells += compare(
            entry=entry, referee="uv pip compile", mode="all", target=target, wheelgap=ours, answer=uv_answer
        )
    return cells


def _check_poetry(
    entry: dict,
    targets: Sequence[Target],
    folder: Path,
    work: Path,
    out: Path,
    versions: dict[str, str],
    run: Runner,
    workers: int,
    mode: str,
    everything: dict[str, dict] | None = None,
) -> tuple[list[Cell], dict[str, dict]]:
    """A poetry.lock's cells for one selection mode: Poetry exports the lock, uv pip compile answers for the export,
    and wheelgap answers for the lock itself. Also the referee's answers by target label. `everything`: the "all"
    mode's answers, for the "default" mode's superset rule."""
    kind, main = entry["kind"], folder / entry["main"]
    if mode == "default" and targets and main.is_file() and not poetry_lock_has_groups(main):
        refused = wheelgap_answer(main, targets[0], None, mode).get("refused")
        if refused:
            # wheelgap refuses a group selection here (by design: the lock doesn't say which group needs what), so
            # there is nothing to compare; the "all" comparison still covers every package. Only while it refuses.
            why = f"a Poetry 1.x lock records no groups per package, and wheelgap refuses a group selection: {refused}"
            cells = [
                Cell(entry["id"], kind, "poetry export", mode, pin.label(t), "*", "-", "-", NOT_COMPARED, why)
                for t in targets
            ]
            return cells, {}
    exported, why, shown = poetry_export(run, main.parent, work, mode)  # the lock's own folder
    if exported is None:
        cells = [
            Cell(entry["id"], kind, "poetry export", mode, pin.label(t), "*", "-", "-", NOT_COMPARED, why, shown)
            for t in targets
        ]
        return cells, {}
    python = poetry_python(main)
    confdir = work / "_no_uv_config"
    version = f"{versions['uv']}; poetry {POETRY_VERSION}, poetry-plugin-export {POETRY_EXPORT_VERSION}"
    # The export's content is part of the key: the same input and Poetry give the same file.
    exported_digest = hashlib.sha256("\n".join(p.line for p in exported.pins).encode()).hexdigest()[:12]

    def ask(target: Target) -> dict:
        command = [*shown.split(), *uv_compile_command(target, pin.uv_platform(target) or "", Path("exported.txt"))]
        answer = cached(
            cache_path(out / "cache", folder.name, "poetry export", target, f"{version}; {exported_digest}", command),
            lambda: poetry_referee(run, folder, target, exported, confdir, python),
        )
        if answer.get("command"):
            answer = dict(answer, command=f"{shown} && {answer['command'].replace('requirements.txt', 'exported.txt')}")
        return answer

    with ThreadPoolExecutor(max_workers=workers) as pool:
        answers = list(pool.map(ask, targets))
    cells = []
    with_extra: set[str] = set()
    if mode == "default":
        groups = poetry_groups(main.parent / "pyproject.toml") or []
        with_extra = poetry_extra_marked(main, {"main", *groups})
    for target, answer in zip(targets, answers, strict=True):
        ours = wheelgap_answer(main, target, None, mode)
        in_all = (everything or {}).get(pin.label(target), {}).get("status", {})
        for c in compare(entry=entry, referee="poetry export", mode=mode, target=target, wheelgap=ours, answer=answer):
            if c.package in with_extra and c.package in in_all and c.answer == ABSENT and c.wheelgap != ABSENT:
                # wheelgap's superset, by design: with no extra selected it keeps a package whose marker joins an
                # extra to other conditions, which Poetry 2.0-2.2 install without the extra (2.3 and later don't).
                # Only when Poetry's export with every extra installs it on this target.
                c.verdict, c.why = NOT_COMPARED, POETRY_KEPT_FOR_EVERY_EXTRA
            cells.append(c)
    return cells, {pin.label(t): a for t, a in zip(targets, answers, strict=True)}


POETRY_KEPT_FOR_EVERY_EXTRA = (
    "wheelgap keeps this package for every extra, with a note: its marker joins an extra to other conditions, and "
    "Poetry 2.0-2.2 install it without the extra (the pinned Poetry doesn't)"
)


# The PEP 508 environment markers other than `extra`.
_MARKER_VARIABLE = re.compile(
    r"\b(?:python_version|python_full_version|os_name|sys_platform|platform_release|platform_system|platform_version|"
    r"platform_machine|platform_python_implementation|implementation_name|implementation_version)\b"
)


def extra_with_other_conditions(marker: str) -> bool:
    """Whether a marker names an extra and also a real environment variable (`python_version >= "3.10" and extra ==
    "x"`), not only extras joined by `and`, `or` and parentheses (`extra == "x" or extra == "y"`), which wheelgap
    decides exactly."""
    try:
        Marker(marker)
    except ValueError:  # InvalidMarker
        return False
    return bool(_EXTRA_ATOM.search(marker)) and bool(_MARKER_VARIABLE.search(_LITERAL.sub("''", marker)))


def poetry_extra_marked(lock: Path, groups: set[str]) -> set[str]:
    """The lock's packages (as cell keys) installed for one of `groups` whose markers for those groups join an extra
    to other conditions. A package with no marker for one of them is always installed, so it isn't one."""
    try:
        packages = tomllib.loads(lock.read_text(encoding="utf-8")).get("package", [])
    except (OSError, ValueError):
        return set()
    wanted = {canonicalize_name(g) for g in groups}
    found = set()
    for p in packages if isinstance(packages, list) else []:
        if not isinstance(p, dict) or not isinstance(p.get("groups"), list):
            continue
        selected = [g for g in p["groups"] if isinstance(g, str) and canonicalize_name(g) in wanted]
        markers = p.get("markers")
        if not selected or markers is None:
            continue
        if isinstance(markers, dict):
            if any(g not in markers for g in selected):
                continue
            texts = [markers[g] for g in selected]
        else:
            texts = [markers]
        if any(isinstance(t, str) and extra_with_other_conditions(t) for t in texts):
            found.add(key(str(p.get("name", "")), str(p.get("version", ""))))
    return found


def totals(cells: Sequence[Cell]) -> dict[str, Any]:
    compared = [c for c in cells if c.verdict != NOT_COMPARED]
    agree = sum(c.verdict == AGREE for c in compared)
    return {
        "cells": len(cells),
        "compared": len(compared),
        "agree": agree,
        "disagree": len(compared) - agree,
        "not_compared": len(cells) - len(compared),
        "agreement": round(agree / len(compared), 4) if compared else None,
    }


def breakdown(cells: Sequence[Cell], attr: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Cell]] = defaultdict(list)
    for c in cells:
        groups[getattr(c, attr)].append(c)
    return {k: totals(v) for k, v in sorted(groups.items())}


def package_cells(cells: Sequence[Cell]) -> list[Cell]:
    """The per-package cells: the agreement rates are over these only."""
    return [c for c in cells if c.package != "*"]


def target_cells(cells: Sequence[Cell]) -> list[Cell]:
    """The target-level cells (package `*`): a target both refuse, one refuses, or that couldn't be compared."""
    return [c for c in cells if c.package == "*"]


def coverage(cells: Sequence[Cell]) -> dict[str, Any]:
    """How much of the input × target grid had any package compared, and the inputs with nothing compared at all."""
    pairs = {(c.input, c.target) for c in cells}
    compared = {(c.input, c.target) for c in package_cells(cells) if c.verdict != NOT_COMPARED}
    inputs = sorted({c.input for c in cells})
    none = [i for i in inputs if not any(p[0] == i for p in compared)]
    return {
        "input_targets": len(pairs),
        "input_targets_compared": len(compared),
        "excluded_share": round(1 - len(compared) / len(pairs), 4) if pairs else None,
        "inputs_with_nothing_compared": none,
    }


def cause(c: Cell) -> str:
    """A disagreement's group: the referee and mode, then what differs."""
    return f"{c.kind} vs {c.referee} ({c.mode}): {c.why}"


def results_json(
    cells: Sequence[Cell], versions: dict[str, str], targets: Sequence[Target], manifest: dict, seconds: float
) -> dict:
    sets: dict[str, list[Cell]] = defaultdict(list)
    for c in cells:
        sets[f"{c.kind} vs {c.referee} ({c.mode})"].append(c)
    return {
        "schema": SCHEMA,
        "referee_version": REFEREE_VERSION,
        "time": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "seconds": round(seconds, 1),
        "tools": versions,
        "targets": [pin.label(t) for t in targets],
        "inputs": len({(e["id"], e["kind"]) for e in manifest["inputs"]}),
        # Rates are per package; target-level cells (package "*") are counted on their own.
        "totals": {name: totals(package_cells(group)) for name, group in sorted(sets.items())},
        "target_level": {name: totals(target_cells(group)) for name, group in sorted(sets.items())},
        "coverage": {name: coverage(group) for name, group in sorted(sets.items())},
        "by_target": {name: breakdown(package_cells(group), "target") for name, group in sorted(sets.items())},
        # An agreeing cell's command is the one its target's other cells share: kept only where it explains something.
        "cells": [dict(c.__dict__, command="") if c.verdict == AGREE else c.__dict__ for c in cells],
    }


def report(cells: Sequence[Cell], versions: dict[str, str], seconds: float, out=None) -> int:
    out = out or sys.stdout
    sets: dict[str, list[Cell]] = defaultdict(list)
    for c in cells:
        sets[f"{c.kind} vs {c.referee} ({c.mode})"].append(c)
    print(
        f"Tools: {versions['uv']}; {versions['pip']}; "
        + (
            f"{versions['poetry']}; "
            if any(c.referee == "poetry export" for c in cells) and "poetry" in versions
            else ""
        )
        + f"wheelgap {versions['wheelgap']}, source last changed in "
        f"{versions['wheelgap_commit'][:12]} (checkout at {versions.get('checkout_head', '?')[:12]}). {seconds:.0f} s.",
        file=out,
    )
    for name, whole in sorted(sets.items()):
        group = package_cells(whole)
        t = totals(group)
        rate = "n/a" if t["agreement"] is None else f"{t['agreement']:.2%}"
        inputs = len({c.input for c in whole})
        level = target_cells(whole)
        # An input can consist entirely of target-level cells (poetry.lock has no
        # referee, so every cell is `*`). The summary must account for those cells
        # even though they stay out of the agreement rate, otherwise it says
        # "0 cells" directly above a list of dozens of not-compared cells.
        level_note = ""
        if level:
            level_not_compared = sum(c.verdict == NOT_COMPARED for c in level)
            level_note = f"; {len(level)} target-level cells outside the rate ({level_not_compared} not compared)"
        print(
            f"\n{name}: {inputs} inputs, {t['cells']} cells: {t['compared']} compared, {t['agree']} agree, "
            f"{t['disagree']} disagree ({rate} agree), {t['not_compared']} not compared{level_note}.",
            file=out,
        )
        if level:
            reasons = Counter(f"{c.verdict}: {c.why}" for c in level)
            print(f"  Target-level cells (not in the rate): {len(level)}", file=out)
            for why, n in reasons.most_common():
                print(f"    {n:5d}  {why[:150]}", file=out)
        cov = coverage(whole)
        if cov["input_targets"]:
            share = cov["excluded_share"] or 0.0
            print(
                f"  Input × targets with a package compared: {cov['input_targets_compared']} of "
                f"{cov['input_targets']} ({share:.1%} excluded).",
                file=out,
            )
            if cov["inputs_with_nothing_compared"]:
                print("  Nothing compared: " + ", ".join(cov["inputs_with_nothing_compared"]), file=out)
        rows = [["target", "compared", "agree", "disagree", "not compared", "agreement"]]
        for target, tt in breakdown(group, "target").items():
            pct = "n/a" if tt["agreement"] is None else f"{tt['agreement']:.2%}"
            rows.append(
                [target, str(tt["compared"]), str(tt["agree"]), str(tt["disagree"]), str(tt["not_compared"]), pct]
            )
        print("\n".join("  " + line for line in pin._table(rows)), file=out)
        if name.startswith("uv.lock") and name.endswith("(all)"):
            listed = [
                c for c in group if c.verdict != NOT_COMPARED and c.wheelgap != ABSENT and c.in_default is not None
            ]
            extra = sum(1 for c in listed if c.answer != ABSENT and not c.in_default)
            print(
                f"  Of the {len(listed)} compared rows wheelgap lists, {extra} are installed only with extras, groups "
                "or "
                "members a plain `uv sync` leaves out (wheelgap's default checks them all: by design).",
                file=out,
            )
    wrong = [c for c in cells if c.verdict == DISAGREE]
    if wrong:
        print("\nDisagreements, grouped by what differs:", file=out)
        groups: dict[str, list[Cell]] = defaultdict(list)
        for c in wrong:
            groups[cause(c)].append(c)
        for name, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            names = sorted({c.input for c in group})
            print(
                f"\n- {name}: {len(group)} cells in {len(names)} inputs "
                f"({', '.join(names[:6])}{', ...' if len(names) > 6 else ''})",
                file=out,
            )
            by_package = Counter(split_key(c.package)[0] for c in group)
            print("  packages: " + ", ".join(f"{n} ({k})" for n, k in by_package.most_common(12)), file=out)
            example = group[0]
            print(
                f"  e.g. {example.input} {example.target} {example.package}: wheelgap {example.wheelgap}, "
                f"{example.referee} {example.answer}",
                file=out,
            )
            if example.command:
                print(f"  referee: {example.command}", file=out)
    skipped = Counter(c.why for c in cells if c.verdict == NOT_COMPARED)
    if skipped:
        print("\nNot compared, by reason:", file=out)
        for why, n in skipped.most_common():
            print(f"  {n:6d}  {why}", file=out)
    errors = [
        c
        for c in cells
        if c.verdict == NOT_COMPARED
        and c.package == "*"
        and ": " in c.why
        and "refuse" not in c.why
        and "no referee" not in c.why
    ]
    return 1 if wrong else 2 if errors else 0


def _urllib_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "wheelgap-check-projects"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return bytes(resp.read())
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def _fetch_optional(fetch: Callable[[str], bytes], url: str) -> bytes | None:
    try:
        return fetch(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except FileNotFoundError:
        return None


_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


def resolve_repo_commit(repo: str, ref: str | None = None, fetch: Callable[[str], bytes] = _urllib_fetch) -> str:
    """Resolve OWNER/NAME (and optional ref) to a full commit SHA via GitHub's API."""
    if not _REPO_RE.fullmatch(repo):
        raise ValueError(f"expected OWNER/NAME matching ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$, got {repo!r}")
    if ref is None:
        meta = json.loads(fetch(f"https://api.github.com/repos/{repo}").decode("utf-8"))
        ref = meta["default_branch"]
    commit = json.loads(fetch(f"https://api.github.com/repos/{repo}/commits/{ref}").decode("utf-8"))
    return str(commit["sha"])


def fetch_repo_corpus(repo: str, sha: str, corpus_dir: Path, fetch: Callable[[str], bytes] = _urllib_fetch) -> dict:
    """Download a repo's root lockfiles/pinned requirements into corpus_dir and build its manifest."""
    folder = corpus_dir / "repos" / repo.replace("/", "__")
    folder.mkdir(parents=True, exist_ok=True)
    raw_base = f"https://raw.githubusercontent.com/{repo}/{sha}"

    def save(rel_path: str) -> bytes | None:
        data = _fetch_optional(fetch, f"{raw_base}/{rel_path}")
        if data is None:
            return None
        dest = folder / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return data

    has_pyproject = False
    if save("uv.lock") is not None:
        has_pyproject = save("pyproject.toml") is not None
        save("uv.toml")
    if save("poetry.lock") is not None and not has_pyproject:
        save("pyproject.toml")
    req_bytes = save("requirements.txt")
    if req_bytes is not None and fully_pinned(req_bytes.decode("utf-8", errors="replace")):
        pending = ["requirements.txt"]
        seen = {"requirements.txt"}
        while pending:
            cur = pending.pop(0)
            text = (folder / cur).read_text(encoding="utf-8", errors="replace")
            for _, line in requirement_lines(text):
                match = re.match(r"^(?:-r|--requirement|-c|--constraint)(?:\s*=\s*|\s+|(?=\S))(\S+)$", line)
                if not match:
                    continue
                rel = os.path.normpath(str(Path(cur).parent / match.group(1))).replace("\\", "/")
                if rel not in seen and not rel.startswith("..") and not Path(rel).is_absolute():
                    seen.add(rel)
                    if save(rel) is not None:
                        pending.append(rel)

    corpus_meta = [{"full_name": repo, "sha": sha}]
    (corpus_dir / "corpus.json").write_text(json.dumps(corpus_meta, indent=1) + "\n", encoding="utf-8")
    return make_manifest(corpus_dir)


def wheelgap_repro_command(entry: dict, cell: Cell) -> str:
    py_ver, _, plat = cell.target.partition(" ")
    cmd = f"uvx wheelgap {entry['main']} -p {py_ver} -t {plat} --explain"
    if entry["kind"] == "uv.lock" and cell.mode == "default":
        cmd += "  # default selection (no extras/groups)"
    return cmd


def format_bug_report(repo: str, sha: str, entry: dict, cell: Cell) -> str:
    """Format a ready-to-paste disagreement report with both reproduction commands."""
    lines = [
        f"--- Bug report: {repo} @ {sha} ---",
        f"Repository: {repo}",
        f"Commit: {sha}",
        f"Input: {entry['main']} ({cell.kind}, mode={cell.mode})",
        f"Target: {cell.target}",
        f"Package: {cell.package}",
        f"wheelgap: {cell.wheelgap}",
        f"{cell.referee}: {cell.answer}" + (f" ({cell.why})" if cell.why else ""),
        f"wheelgap command: {wheelgap_repro_command(entry, cell)}",
        f"referee command: {cell.command or '(none)'}",
    ]
    return "\n".join(lines)


def check_repo(
    repo: str,
    *,
    ref: str | None = None,
    target_names: Sequence[str] = (),
    workers: int = 6,
    own_python: bool = True,
    fetch: Callable[[str], bytes] = _urllib_fetch,
    run: Runner = run_command,
    versions: dict[str, str] | None = None,
    out: Any = None,
) -> int:
    """Fetch one public repo at a commit, compare with referees, and print summary + bug reports."""
    out = out or sys.stdout
    targets = pick_targets(target_names)
    if not targets:
        raise ValueError(f"no target matches {list(target_names)}")
    sha = resolve_repo_commit(repo, ref, fetch=fetch)
    versions = versions or tool_versions()
    with tempfile.TemporaryDirectory(prefix="wheelgap-repo-") as tmp:
        tmp_path = Path(tmp)
        corpus_dir = tmp_path / "corpus"
        out_dir = tmp_path / "out"
        corpus_dir.mkdir()
        out_dir.mkdir()
        manifest = fetch_repo_corpus(repo, sha, corpus_dir, fetch=fetch)
        inputs = manifest["inputs"]
        if not inputs:
            print(f"No supported uv.lock, poetry.lock, or pinned requirements.txt found in {repo}@{sha}.", file=out)
            return 2
        cells: list[Cell] = []
        entries: dict[tuple[str, str], dict] = {}
        for entry in inputs:
            entries[(entry["id"], entry["kind"])] = entry
            cells += check_input(entry, targets, out_dir, versions, run, workers, own_python)
        summary = totals(package_cells(cells))
        level = target_cells(cells)
        level_note = ""
        if level:  # e.g. poetry.lock, whose cells are all target-level (no referee)
            level_not_compared = sum(c.verdict == NOT_COMPARED for c in level)
            level_note = f"; {len(level)} target-level cells outside the rate ({level_not_compared} not compared)"
        print(f"Repo: {repo} @ {sha}", file=out)
        print(
            f"Summary: {summary['compared']} compared, {summary['agree']} agreed, "
            f"{summary['disagree']} disagreed, {summary['not_compared']} not compared{level_note}.",
            file=out,
        )
        for cell in level:
            if cell.verdict != AGREE:
                print(f"Target-level ({cell.kind}, {cell.mode}, {cell.target}): {cell.verdict}: {cell.why}", file=out)
        wrong = [c for c in cells if c.verdict == DISAGREE]
        for cell in wrong:
            entry = entries[(cell.input, cell.kind)]
            print("", file=out)
            print(format_bug_report(repo, sha, entry, cell), file=out)
        errors = [
            c
            for c in level
            if c.verdict == NOT_COMPARED and ": " in c.why and "refuse" not in c.why and "no referee" not in c.why
        ]
        return 1 if wrong else 2 if errors and summary["compared"] == 0 else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[1].split(".")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("manifest", help="list a corpus folder's inputs in a manifest file")
    make.add_argument("corpus", type=Path)
    make.add_argument("-o", "--output", type=Path, required=True)
    repo_cmd = sub.add_parser("repo", help="fetch one public GitHub repo and check it against referees")
    repo_cmd.add_argument("repo", help="OWNER/NAME")
    repo_cmd.add_argument("--ref", default=None, help="branch, tag, or commit SHA (default: default branch head)")
    repo_cmd.add_argument("-t", "--target", action="append", default=[], help="only this target: a label or a platform")
    repo_cmd.add_argument("--workers", type=int, default=6, help="referee runs at once (default 6)")
    repo_cmd.add_argument(
        "--no-own-python",
        action="store_true",
        help="don't add the newest Python a uv.lock or poetry.lock allows when it allows none of the targets' Pythons",
    )
    check = sub.add_parser("run", help="check the manifest's inputs")
    check.add_argument("manifest", type=Path)
    check.add_argument(
        "-o", "--output", type=Path, required=True, help="folder for results.json, report.txt and the cache"
    )
    check.add_argument("-t", "--target", action="append", default=[], help="only this target: a label or a platform")
    check.add_argument("--only", action="append", default=[], help="only this input id (owner/name)")
    check.add_argument(
        "--kind", action="append", default=[], help="only this input kind (uv.lock, requirements, poetry.lock)"
    )
    check.add_argument("--workers", type=int, default=6, help="referee runs at once (default 6)")
    check.add_argument(
        "--no-own-python",
        action="store_true",
        help="don't add the newest Python a uv.lock or poetry.lock allows when it allows none of the targets' Pythons",
    )
    args = parser.parse_args(argv)
    if args.command == "manifest":
        data = make_manifest(args.corpus)
        args.output.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        kinds = Counter(e["kind"] for e in data["inputs"])
        print(
            f"{len(data['inputs'])} inputs: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())), file=sys.stderr
        )
        return 0
    if not pin.default_cpython():
        parser.error("run it on a default (GIL) build of CPython: uv pip compile takes its tags' ABI from it")
    if args.command == "repo":
        try:
            return check_repo(
                args.repo,
                ref=args.ref,
                target_names=args.target,
                workers=args.workers,
                own_python=not args.no_own_python,
            )
        except ValueError as exc:
            parser.error(str(exc))
            return 2
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    inputs = [
        e
        for e in manifest["inputs"]
        if (not args.only or e["id"] in args.only) and (not args.kind or e["kind"] in args.kind)
    ]
    targets = pick_targets(args.target)
    if not targets:
        parser.error(f"no target matches {args.target}")
    args.output = args.output.resolve()  # the referees run in the input's folder: no relative paths
    args.output.mkdir(parents=True, exist_ok=True)
    versions = tool_versions()
    print(f"{versions}; {len(inputs)} inputs on {len(targets)} targets", file=sys.stderr)
    start = datetime.datetime.now()
    cells: list[Cell] = []
    for number, entry in enumerate(inputs, 1):
        try:
            got = check_input(entry, targets, args.output, versions, run_command, args.workers, not args.no_own_python)
        except Exception as exc:  # one input's crash is "couldn't compare", never an answer
            got = [
                Cell(
                    entry["id"], entry["kind"], "-", "all", pin.label(t), "*", "-", "-", NOT_COMPARED, f"crash: {exc!r}"
                )
                for t in targets
            ]
        bad = sum(c.verdict == DISAGREE for c in got)
        print(
            f"[{number}/{len(inputs)}] {entry['id']} {entry['kind']}: {len(got)} cells, {bad} disagree", file=sys.stderr
        )
        cells += got
    seconds = (datetime.datetime.now() - start).total_seconds()
    data = results_json(cells, versions, targets, {"inputs": inputs}, seconds)
    (args.output / "results.json").write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
    with open(args.output / "report.txt", "w", encoding="utf-8") as text:
        code = report(cells, versions, seconds, out=text)
    sys.stdout.write((args.output / "report.txt").read_text(encoding="utf-8"))
    return code


if __name__ == "__main__":
    sys.exit(main())
