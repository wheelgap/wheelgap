"""Read packages (and, where the lockfile records them, their distribution files) from inputs."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tomllib
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import ParseResult, unquote, urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from . import cutoff as cutoffs
from .cutoff import describe, is_duration, parse_cutoff
from .targets import PLATFORMS

if TYPE_CHECKING:
    from .targets import Target

LOCKFILES = (
    "uv.lock",
    "pylock.toml",
    "poetry.lock",
    "pdm.lock",
    "Pipfile.lock",
    "requirements.txt",
    "requirements.lock",
    "pixi.lock",
)


class SourceError(ValueError):
    pass


# A Poetry 2.x lock's extras for an extras selection: (every extra in its [extras] table, the ones selected), canonical.
PoetryExtras = tuple[frozenset[str], frozenset[str]]


@dataclass
class Package:
    name: str
    version: str | None
    # Distribution filenames. None means "unknown, ask the index".
    files: list[str] | None = None
    # Set for packages that don't come from an index (editable, git, path, ...).
    skip: str | None = None
    error: str | None = None
    origin: str = ""
    # Environment markers; the package is installed if any of them is true. None means always.
    markers: list[str] | None = None
    # A version range that isn't an exact pin (numpy>=1.26,<2): the index picks the newest match.
    spec: str | None = None
    # `spec` with its clauses in the order the requirement writes them (packaging sorts them), for --fix to show.
    written_spec: str | None = None
    # Files of a pinned release that the index marks as yanked (see check.status_for).
    yanked: frozenset[str] = frozenset()
    # uv.lock / pylock.toml / poetry.lock / Pipfile.lock: the index the lock says the package came from, e.g.
    # https://pypi.org/simple.
    registry: str | None = None
    # The index picked `version` (an unpinned or ranged requirement): it isn't a pin from the input.
    version_from_index: bool = False
    # uv.lock / pylock.toml: the sdist's URL and sha256, as the lock records them (for --build-info).
    sdist_url: str | None = None
    sdist_sha256: str | None = None
    # This run found the package on pypi.org's JSON API (pypi.fill), so its name is public there.
    found_on_pypi: bool = False
    # `files` are only some of the release's files (pixi.lock records the one it picked per platform), so the
    # package is looked up on its index anyway, and skipped as a private index if it can't be.
    partial_files: bool = False
    # A range or pin resolved for several Python major.minors. Each value owns only that Python's eligible files.
    # Identical resolutions share a Package, so reports can keep one row for them.
    resolutions: dict[str, Package] = field(default_factory=dict)
    # Files of a pinned release whose Requires-Python excludes the target Python, mapped to their --explain reason.
    requires_python_rejected: dict[str, str] = field(default_factory=dict)
    # The release's Requires-Python as the lock records it for the package (pylock.toml's requires-python,
    # poetry.lock's python-versions, pdm.lock's requires_python), applied to the lock's own files per target Python
    # (pypi.apply_locked_python_ranges). Only kept where the lock also records each package's markers (always in a
    # pylock.toml; Poetry from lock-version 2.1; PDM with inherit_metadata). Poetry and PDM lock a package only for
    # the Pythons its markers select, so there a range that leaves a target out says something only for a Python
    # outside the project's own range. Without the markers the package would be reported `missing` on a Python the
    # installer never installs it for (numpy = [{version = "1.26.4", python = "<3.13"}, ...] in pyproject.toml).
    locked_requires_python: str | None = None
    # Yanked status of Python-rejected files, needed when applying uv's upper-bound policy.
    python_rejected_yanked: frozenset[str] = frozenset()
    # Files admitted by uv that pip would refuse for this Python.
    pip_refuses: dict[str, str] = field(default_factory=dict)
    # sha256 hashes listed on the requirement line in a requirements file (--hash=sha256:... or --hash sha256:...).
    # If not empty, only files whose sha256 matches one of these are accepted.
    hashes: frozenset[str] = frozenset()
    # Files of a pinned release whose sha256 hash was not listed in requirements, mapped to their --explain reason.
    hash_rejected: dict[str, str] = field(default_factory=dict)
    # Distribution selection policy from requirements files or uv project configuration.
    no_binary: bool = False
    only_binary: bool = False
    # A requirements file's release control for this package, as pip's ReleaseControl.allows_prereleases gives it
    # (--pre, --all-releases, --only-final): True takes pre-releases, False never does (a pinned one is an error), and
    # None is the default: a pre-release only when the range names one or nothing else matches.
    prereleases: bool | None = None
    # The relevant uv setting(s), used to explain a gap caused by the configured policy.
    install_policy: str | None = None
    # PyPI project metadata URLs (project_urls and home_page).
    project_urls: dict[str, str] | None = None
    home_page: str | None = None
    exclude_newer: datetime | None = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> str:
        return canonicalize_name(self.name)

    @property
    def variants(self) -> list[Package]:
        """The input package and its resolved variants, retaining their source provenance."""
        return list({id(p): p for p in (self, *self.resolutions.values())}.values())


# PEP 508's package name. pip and uv can't install anything else, and the CLI prints names at the start of lines,
# where a lockfile's `::error::...` would otherwise read as a workflow command in a GitHub Actions log.
_NAME_RE = re.compile(r"[A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9]", re.IGNORECASE | re.ASCII)


def package_name(path: Path, name: object) -> str:
    """A lockfile's package name, or SourceError naming the file if it isn't a valid one."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        what = "has no name" if name is None else f"has the name {name!r}, which isn't a valid package name (PEP 508)"
        raise SourceError(f"{path.name}: a package {what}")
    return name


# A dependency edge in a lockfile graph: (name, version or None, marker or None, source table or None)
Edge = tuple[str, str | None, str | None, dict[str, object] | None]


@dataclass(frozen=True)
class UvSelection:
    """Explicit installation selection. None on a uv.lock Source checks what
    `uv sync --all-packages --all-extras --all-groups` installs."""

    extras: frozenset[str] = frozenset()
    all_extras: bool = False
    groups: frozenset[str] = frozenset()
    no_dev: bool = False
    only_groups: frozenset[str] = frozenset()


@dataclass
class DirectoryInput:
    """How a directory input (`wheelgap DIR`) came to the file that was read (load_directory)."""

    directory: Path
    path: Path
    # Where the file was found: "lockfile" (one of LOCKFILES at the root, as always), "pyproject" (the root's
    # pyproject.toml), "requirements" (the root's only requirements file under another name) or "subfolder" (the
    # only input below the root).
    reason: str
    # Files that come first in the search but have nothing to check, each with why.
    empty: list[tuple[Path, str]] = field(default_factory=list)
    # "pyproject": the root's requirements files under other names, which weren't read, each with whether it looks
    # like a compiler's full pin list (_looks_compiled_and_pinned). For the note and --json only, never to choose.
    other_requirements: list[tuple[Path, bool]] = field(default_factory=list)


@dataclass
class Source:
    label: str
    packages: list[Package] = field(default_factory=list)
    # uv.lock only: which packages each package depends on, and where the walk starts.
    # When set, a package applies to a target only if it's reachable along edges whose markers hold.
    edges: dict[int, list[Edge]] | None = None
    roots: list[int] = field(default_factory=list)
    root_edges: list[Edge] = field(default_factory=list)
    # Index into root_edges -> name of the dependency group it comes from, e.g. {0: "dev"}.
    root_groups: dict[int, str] = field(default_factory=dict)
    # uv.lock only: the project's requires-python. uv keeps only the wheels that can serve it.
    requires_python: str | None = None
    # uv.lock only: its supported-markers, the project's [tool.uv] environments. When there are any, uv locked
    # for those environments only: `uv sync` fails anywhere else, and the lock leaves out the packages and wheels
    # that only other environments need (see check.check_lock_environments).
    supported_markers: list[str] = field(default_factory=list)
    # pdm.lock only: its [[metadata.targets]] (`pdm lock --platform/--python/--implementation`), each a table of
    # requires_python, platform, implementation and gil_disabled. `pdm install` refuses an environment none of them
    # takes ("No compatible lock target found"), and a lock made for a platform keeps only that platform's packages
    # and wheels (see check.pdm_target_value).
    pdm_targets: list[dict] = field(default_factory=list)
    # Scripts and pyproject.toml: uv's exclude-newer (#521). A file uploaded at or after the cutoff isn't installed;
    # exclude-newer-package gives the packages it names their own cutoff, or none (uv's `false`: None here).
    exclude_newer: datetime | None = None
    constraint_paths: tuple[Path, ...] = ()
    exclude_newer_package: dict[str, datetime | None] = field(default_factory=dict)
    # A uv setting that may set a cutoff here but that wheelgap doesn't read, for a note.
    exclude_newer_unread: str | None = None
    # The cutoffs above that came from a duration ("3 days"), which uv counts back from the time it resolves and
    # wheelgap from the time of the run: each setting as written, with the cutoff it gave, for a note.
    exclude_newer_durations: list[str] = field(default_factory=list)
    # Retain uv edge provenance for an explicit installation selection. The existing flattened
    # graph remains untouched, preserving conservative behavior when no selectors are supplied.
    uv_packages: list[dict] | None = None
    uv_manifest: dict = field(default_factory=dict)
    uv_pyproject: Path | None = None
    # A script's lock (example.py.lock, from uv lock --script): the script's path, as the lock's was given. uv takes
    # no dependency groups for it from a pyproject.toml beside it, so uv_pyproject is None; --fix's config goes in the
    # script's block.
    uv_script: str | None = None
    uv_default_groups: list[str] = field(default_factory=lambda: ["dev"])
    uv_selection: UvSelection | None = None
    # uv.lock only: its `conflicts` ([tool.uv] conflicts), each set as the names uv's markers give its items
    # (`extra-4-demo-old`, `group-4-demo-cpu`, `project-4-demo`; check.py's _conflict_worlds). None when the lock
    # has none, or when they can't be read, which leaves those names unknown.
    uv_conflicts: list[list[str]] | None = None
    # A requirements file (requirements.txt, .in, requirements.lock): unpinned lines get the newest release.
    requirements: bool = False
    # pyproject.toml only: [project] optional-dependencies by canonical extra name, applied with --extra.
    # None when the input isn't a pyproject.toml.
    pyproject_extras: dict[str, list[str]] | None = None
    # pyproject.toml only: optional-dependencies is listed in [project] dynamic, so the extras can't be read.
    pyproject_dynamic_extras: bool = False
    # PEP 735 groups, expanded only when explicitly selected.
    pyproject_groups: dict[str, object] = field(default_factory=dict)
    # A pyproject.toml with a [dependency-groups] table and no [project] table.
    pyproject_groups_only: bool = False
    # pyproject.toml only: the canonical [project] name, to spot `myproject[extra]` self-references.
    pyproject_name: str | None = None
    # pyproject.toml and scripts: the uv settings and [tool.uv.sources] uv applies to each requirement, including
    # those of extras and groups selected later (_project_package).
    uv_rules: UvRules | None = None
    # poetry.lock only: each package's [[package]] table, kept so --group/--only-group/--no-dev can pick packages
    # by the groups Poetry records on them.
    poetry_tables: list[dict] | None = None
    poetry_pyproject: Path | None = None
    # poetry.lock before lock-version 2.1 with no readable pyproject.toml beside it, or one that declares nothing: why
    # its direct dependencies are unknown. check() then makes a package the walk leaves out on a target `unknown`
    # there when it would have a gap.
    poetry_roots_unknown: str | None = None
    # poetry.lock only: the extras in its [extras] table, which `poetry install --extras` accepts.
    poetry_lock_extras: list[str] = field(default_factory=list)
    # poetry.lock after a selection: the note that says where extras couldn't leave packages out, because Poetry
    # 2.0-2.2 may install them without the extra (see poetry_extras_decide and _poetry_extras_declared_elsewhere).
    poetry_extras_unsure: str | None = None
    # pdm.lock only: each package's `groups` list (None for a package that records none, as before PDM 2.x),
    # kept so --group/--only-group/--no-dev can pick packages. None when the input isn't a pdm.lock.
    pdm_groups: list[list[str] | None] | None = None
    # pdm.lock only: its [metadata] groups (canonical; None if it has none), and whether its [metadata] strategy has
    # inherit_metadata, without which PDM doesn't go by package groups.
    pdm_locked_groups: list[str] | None = None
    pdm_inherit_metadata: bool = False
    pdm_pyproject: Path | None = None
    # poetry.lock after a group selection: the packages it left out, which were still read (for --ignore notes).
    unselected_keys: set[str] = field(default_factory=set)
    # The file this source was read from (None for package names on the command line) and which reader read it
    # (a key of READERS), for --github-annotations.
    path: Path | None = None
    kind: str | None = None
    # The input names no index but PyPI, so a package this run found on pypi.org is a PyPI package (for --conda-forge,
    # which sends only such names). False when unsure: lockfiles rely on each package's registry instead.
    pypi_only: bool = False
    # A requirements file's index options on lines of their own (`--extra-index-url URL`, in it or a file it
    # includes), by their full names and never with their values, which may hold credentials: pip reads them and
    # wheelgap doesn't, so the CLI says the file was checked on PyPI only.
    index_options: list[str] = field(default_factory=list)
    # The same for the other -r files of its pip install line, whose index options pip applies to it too: (the file
    # that sets them, their names).
    line_index_options: list[tuple[str, list[str]]] = field(default_factory=list)
    # A project directory with pixi.lock as well as the lockfile that was picked: the pixi.lock, for a note.
    passed_over: Path | None = None
    # The input was a directory: which of its files this is and why (None for a file or package names).
    found: DirectoryInput | None = None
    # A pip installation report captures one generating environment, not a cross-platform dependency graph.
    pip_report_env: dict[str, str] | None = None
    # Its downloads include a musllinux-only wheel, which pip on glibc doesn't accept, or a manylinux-only one,
    # which pip on musl doesn't accept: proof the resolution isn't one the other libc's pip makes.
    pip_report_musl: bool = False
    pip_report_glibc: bool = False
    # Requirements for --deps to resolve: package names on the command line, selected pyproject.toml
    # requirements, or a requirements file's requirement lines.
    specs: list[str] = field(default_factory=list)
    # A requirements file that looks like pip-compile output (--hash lines or `# via` comments): already
    # pinned, so --deps has nothing to resolve.
    compiled: bool = False
    # A requirements file's -c constraints as clean index specifiers (key -> specifiers), for --deps to seed.
    constraint_specs: dict[str, set[str]] = field(default_factory=dict)
    # Environment marker-bearing constraints apply per target during --deps resolution.
    marker_constraint_specs: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # A requirements file's constraint problems (a non-index constraint, conflicting pins): the resolved package
    # gets the error, as the requirement's own row does without --deps.
    constraint_errors: dict[str, str] = field(default_factory=dict)
    # A requirements file's constraints --deps can't apply the way pip does (a URL, an error): it refuses.
    deps_blockers: list[str] = field(default_factory=list)
    # A requirements file's --no-binary/--only-binary policy, applied to the resolved packages too.
    format_control: _FormatControl | None = None
    # A requirements file's options that pip ignores after a requirement (`jax==0.4.25 -f URL`), for notes:
    # `requirements.txt:2: pip ignores -f after a requirement`, once per file and option.
    ignored_options: list[str] = field(default_factory=list)
    # --deps (deps.py): per target, the packages installed and the dependency path to each, keyed by index into
    # `packages`. check() uses it in place of a lockfile's graph.
    deps_paths: dict[Target, dict[int, list[str]]] | None = None
    # --deps: all distinct direct requirements that reach each transitive package, per target.
    deps_direct: dict[Target, dict[int, tuple[str, ...]]] | None = None

    def cutoff_for(self, key: str) -> datetime | None:
        """The upload cutoff uv applies to this package: its exclude-newer-package entry, else exclude-newer."""
        return self.exclude_newer_package[key] if key in self.exclude_newer_package else self.exclude_newer


def pythons_outside_lock(source: Source, targets: Iterable[Target], *, bounds_only: bool = True) -> list[str]:
    """Target Pythons the source's requires-python leaves out, e.g. ['3.14t'] for `==3.13.*`.

    For a lockfile, uv keeps only the wheels that can serve requires-python (abi3 and pure wheels survive), so
    the lock may be missing wheels for those Pythons. Like uv, only the bounds count there: `!=3.12.*` doesn't
    exclude 3.12. With bounds_only=False (a library's pyproject.toml, which pip checks in full against the
    running Python), every clause counts, so `!=3.12.*` leaves out 3.12 but `!=3.12.0` doesn't.
    """
    if not source.requires_python:
        return []
    try:
        clauses = SpecifierSet(source.requires_python)
        spec = SpecifierSet(",".join(str(s) for s in clauses if not (bounds_only and s.operator == "!=")))
    except InvalidSpecifier:
        return []
    out = []
    for t in targets:
        # A minor is served if any of its releases fits: its first and last patch, or a version the spec names.
        tries = [Version(f"{t.major}.{t.minor}.0"), Version(f"{t.major}.{t.minor}.999")]
        for s in spec:
            try:
                v = Version(s.version.removesuffix(".*"))
            except ValueError:
                continue
            if v.release[:2] == (t.major, t.minor):
                tries.append(v)
        if not any(spec.contains(v, prereleases=True) for v in tries):
            out.append(t.python)
    return list(dict.fromkeys(out))


def uv_ignores_python_rejection(requires: str, python: str) -> bool:
    """Whether uv accepts a file that the index check rejected for this Python."""
    # uv compares the lower edge of the complete release-only range:
    # https://github.com/astral-sh/uv/blob/0.13.9/crates/uv-distribution-types/src/requires_python.rs#L246-L275
    try:
        spec = SpecifierSet(requires)
        target = Version(f"{python}.99")
        if spec.contains(target):
            return False

        # At equal versions an exclusive lower bound is higher than an inclusive one,
        # so tuple comparison can pick the stricter bound.
        lower: tuple[Version, bool] | None = None
        upper: tuple[Version, bool] | None = None
        exclusions: list[tuple[Version, Version | None]] = []
        for clause in spec:
            prefix = clause.version.endswith(".*")
            version = Version(clause.version.removesuffix(".*"))
            release = Version(version.base_version)
            operator = clause.operator
            successor = release
            if prefix or operator == "~=":
                segments = release.release if prefix else release.release[:-1]
                successor = Version(".".join(map(str, (*segments[:-1], segments[-1] + 1))))
                if prefix and version.epoch:
                    successor = Version(f"{version.epoch}!{successor}")
            if operator == "!=":
                exclusions.append((release, successor if prefix else None))
                continue
            if operator in {">", ">=", "==", "===", "~="}:
                candidate = (release, operator == ">")
                lower = candidate if lower is None else max(lower, candidate)
            if operator in {"<", "<=", "==", "===", "~="}:
                candidate = (
                    successor,
                    operator in {"<=", "==", "==="} and not prefix,
                )
                upper = candidate if upper is None else min(upper, candidate)

        if lower is None:
            return True

        for start, end in sorted(exclusions, key=lambda interval: interval[0]):
            if start > lower[0]:
                break
            if end is None:
                if start == lower[0]:
                    lower = (start, True)
            elif end > lower[0]:
                lower = (end, False)

        # uv treats an empty intersection as having no lower bound.
        if upper is not None and (lower[0] > upper[0] or (lower[0] == upper[0] and (lower[1] or not upper[1]))):
            return True
        return lower <= (target, False)
    except (InvalidSpecifier, InvalidVersion):
        return False


def restore_uv_python_files(packages: Iterable[Package], pythons: Iterable[str]) -> None:
    """Apply uv's policy to the looked-up distributions for each Python."""
    for pkg in packages:
        restored_variants: list[Package] = []
        for python in pythons:
            variant = pkg.resolutions.get(python, pkg)
            if variant.files is None or variant.error:
                continue
            # Index resolution coalesces identical answers. Copy before restoring files for just one Python.
            refusals = {
                filename: reason
                for filename, reason in variant.requires_python_rejected.items()
                if uv_ignores_python_rejection(reason.removeprefix("requires Python "), python)
            }
            if not refusals:
                continue
            restored = replace(
                variant,
                files=[*variant.files, *refusals],
                requires_python_rejected={
                    file: reason for file, reason in variant.requires_python_rejected.items() if file not in refusals
                },
                yanked=variant.yanked | (variant.python_rejected_yanked & refusals.keys()),
                pip_refuses=refusals,
                resolutions={},
            )
            variant = next((p for p in restored_variants if p == restored), restored)
            if variant is restored:
                restored_variants.append(restored)
            pkg.resolutions[python] = variant


def _filename(entry: dict, origin: str, package: str) -> str | None:
    url = entry.get("url")
    parts: ParseResult | None = None
    if isinstance(url, str):
        # A lock URL that `urlparse` can't read (an unclosed IPv6 bracket) or whose host holds a space is a
        # broken lock, even when the entry also records a file name: name the file and the package instead of
        # letting ValueError escape. The URL isn't echoed back, since it may carry credentials.
        try:
            parts = urlparse(url)
        except ValueError:
            pass
        if parts is None or any(c.isspace() for c in parts.netloc.rpartition("@")[2]):
            raise SourceError(f"{origin}: package {package} has a file URL that can't be parsed")
    for k in ("filename", "file", "name"):
        if isinstance(entry.get(k), str) and entry[k].endswith((".whl", ".tar.gz", ".zip", ".tar.bz2", ".tgz")):
            return entry[k]
    if parts is not None:
        return unquote(Path(parts.path).name)
    path = entry.get("path")
    if isinstance(path, str):
        return Path(path).name
    return None


def _sdist_source(entry: object, hash_key: str) -> tuple[str | None, str | None]:
    """(URL, sha256) of a uv.lock sdist (hash = "sha256:...") or a pylock.toml one (hashes = {sha256 = ...})."""
    if not isinstance(entry, dict):
        return None, None
    url, digest = entry.get("url"), entry.get(hash_key)
    if isinstance(digest, dict):
        digest = digest.get("sha256")
    elif isinstance(digest, str) and digest.startswith("sha256:"):
        digest = digest.removeprefix("sha256:")
    else:
        digest = None
    if not (isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest)):
        digest = None
    return (url if isinstance(url, str) else None), (digest.lower() if digest else None)


def _uv_edge(path: Path, owner: str, d: object) -> Edge:
    """One uv.lock dependency edge. A field of the wrong type (`marker = 5`) crashes later, inside `packaging`, so
    it's a SourceError here, next to the file. `owner` says whose edge it is: "package a" or "the manifest"."""
    if not isinstance(d, dict):
        raise SourceError(f"{path.name}: {owner} has a dependency that isn't a table")
    if "name" not in d:
        raise SourceError(f"{path.name}: {owner} has a dependency without a `name`")
    if not isinstance(d["name"], str):
        raise SourceError(f"{path.name}: {owner} has a dependency with a `name` that isn't a string")
    for what in ("version", "marker"):
        value = d.get(what)
        if value is not None and not isinstance(value, str):
            raise SourceError(
                f"{path.name}: {owner} has a dependency {d['name']!r} with a `{what}` that isn't a string"
            )
    # uv writes the extras an edge asks for as a list. A plain string would be read as its characters.
    for what in ("extra", "extras"):
        extra = d.get(what)
        if extra is not None and not (isinstance(extra, list) and all(isinstance(e, str) for e in extra)):
            raise SourceError(
                f"{path.name}: {owner} has a dependency {d['name']!r} with an `{what}` that isn't a list of strings"
            )
    edge_source = d.get("source")
    if edge_source is not None and not isinstance(edge_source, dict):
        raise SourceError(f"{path.name}: {owner} has a dependency {d['name']!r} with a `source` that isn't a table")
    return (d["name"], d.get("version"), d.get("marker"), edge_source)


# A uv manifest writes source provenance as flat keys, unlike package dependency edges.
# Keep the normalised table on the manifest entry too: --group/--no-dev walks those
# entries directly, while the default graph walks root_edges.
_UV_MANIFEST_SOURCE_KEYS = {
    "index": "registry",
    "path": "path",
    "url": "url",
    "git": "git",
    "editable": "editable",
    "directory": "directory",
    "virtual": "virtual",
}


def _uv_manifest_edge(path: Path, d: object) -> Edge:
    name, version, marker, source = _uv_edge(path, "the manifest", d)
    assert isinstance(d, dict)
    flat_source: dict[str, object] = {}
    for key, kind in _UV_MANIFEST_SOURCE_KEYS.items():
        if key not in d:
            continue
        value = d[key]
        if not isinstance(value, str):
            reason = f"the manifest has a dependency {name!r} with a `{key}` that isn't a string"
            raise SourceError(f"{path.name}: {reason}")
        flat_source[kind] = value
    if len(flat_source) > 1 or (flat_source and source is not None):
        raise SourceError(f"{path.name}: the manifest has a dependency {name!r} with conflicting sources")
    if flat_source:
        source = flat_source
        d["source"] = source
    return name, version, marker, source


def _uv_edges(path: Path, p: dict) -> list[Edge]:
    """A uv.lock package's dependency edges, from `dependencies` and the two tables of groups."""
    owner = f"package {p.get('name')}"
    groups: list[object] = [p.get("dependencies", [])]
    for table in ("optional-dependencies", "dev-dependencies"):
        value = p.get(table, {})
        if not isinstance(value, dict):
            raise SourceError(f"{path.name}: {owner} has a `{table}` that isn't a table of groups")
        groups.extend(value.values())
    out: list[Edge] = []
    for group in groups:
        if not isinstance(group, list):
            raise SourceError(f"{path.name}: {owner} has a group of dependencies that isn't a list")
        out.extend(_uv_edge(path, owner, d) for d in group)
    return out


_UV_BUILD_RESTRICTIONS = ("no-build", "no-binary", "no-build-package", "no-binary-package")
# The uv settings wheelgap applies: the build restrictions, and no-sources, which turns [tool.uv.sources] off.
_UV_APPLIED_SETTINGS = (*_UV_BUILD_RESTRICTIONS, "no-sources")


def _sets_build_restriction(uv: object) -> list[str]:
    """The build restrictions a [tool.uv] table (or uv.toml) turns on: true, or a non-empty list."""
    return [key for key in _UV_BUILD_RESTRICTIONS if isinstance(uv, dict) and uv.get(key)]


def _uv_config(directory: Path) -> tuple[Path, object] | None:
    """The uv settings uv reads in `directory`: its uv.toml, else a pyproject.toml with a [tool.uv] table. uv skips a
    pyproject.toml without one (and one it can't read) and keeps looking in the folders above."""
    for name in ("uv.toml", "pyproject.toml"):
        config = directory / name
        if not config.is_file():
            continue
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        if name == "uv.toml":
            return config, data
        tool = data.get("tool")
        if isinstance(tool, dict) and "uv" in tool:
            return config, tool["uv"]
    return None


def tool_table(data: dict, path: Path | str) -> dict:
    """Return the [tool] table from parsed TOML, or raise SourceError when tool is not a table."""
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        raise SourceError(f"{path}: [tool] must be a table")
    return tool


# The shared reader of the uv settings uv applies to an input (#810, #811, #812): a project's uv.lock, a script's
# lock, a pyproject.toml and a script all go through _find_uv_settings and _uv_build_policy, and a pyproject.toml
# or script's requirements through UvRules.apply as well.


@dataclass
class _UvLayer:
    """One table of uv settings that applies to an input: a uv.toml, a pyproject.toml's [tool.uv], or a script's
    [tool.uv]. `table` names it in errors ("uv.toml" or "[tool.uv]"), after `config`; `shown` names it in an install
    policy, relative to the input's folder (`[tool.uv]`, `uv.toml`, `../uv.toml`, `../pyproject.toml [tool.uv]`)."""

    settings: dict
    config: Path
    table: str
    shown: str


def _uv_settings_file(config: Path, strict: bool) -> dict | None:
    """The settings in a uv.toml, or in a pyproject.toml's [tool.uv] table (None without one). uv 0.13.1 stops with
    an error on a uv.toml it can't read, but only warns about a pyproject.toml it can't read and looks further up;
    `strict` makes that an error too, for the input's own pyproject.toml."""
    standalone = config.name == "uv.toml"
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8-sig"))  # -sig: Windows editors add a BOM
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        if not (standalone or strict):
            return None
        if isinstance(e, UnicodeDecodeError):
            raise SourceError(f"{config}: isn't UTF-8, so its uv settings can't be read") from None
        if isinstance(e, tomllib.TOMLDecodeError):
            raise SourceError(f"{config}: invalid TOML: {e}") from None
        raise SourceError(f"{config}: can't be read ({e}), so its uv settings can't be read") from None
    if standalone:
        return data
    if not strict:  # uv warns about a [tool] or [tool.uv] that isn't a table and keeps looking
        uv = _table(data, "tool").get("uv")
        return uv if isinstance(uv, dict) else None
    uv = tool_table(data, config).get("uv")
    if uv is not None and not isinstance(uv, dict):
        raise SourceError(f"{config}: [tool.uv] must be a table")
    return uv


def _find_uv_settings(folder: Path, own: Path | None = None) -> tuple[Path, dict] | None:
    """The uv settings uv finds from `folder` (FilesystemOptions::find in uv 0.13.1), with the file they're in: in
    each folder from it up to the filesystem root, its uv.toml, else a pyproject.toml with a [tool.uv] table. uv
    skips a pyproject.toml without one, and one it can't read (`own`, the input's own pyproject.toml, is an error
    instead), and it doesn't stop at a repository root."""
    for directory in (folder, *folder.parents):
        for name in ("uv.toml", "pyproject.toml"):
            config = directory / name
            if config.is_file() and (settings := _uv_settings_file(config, strict=config == own)) is not None:
                return config, settings
    return None


def _refuse_unparseable_workspace_parent(folder: Path, what: Path) -> None:
    """uv sync and uv lock look for a project's workspace through the first pyproject.toml in a folder above it, and
    stop with an error when that one can't be parsed (uv 0.13.1: "Failed to parse"), past a repository root too.
    Settings discovery only warns about it, but the project can't be synced, so refuse rather than answer."""
    for directory in folder.parents:
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            try:
                tomllib.loads(candidate.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
                raise SourceError(
                    f"{what}: {candidate} can't be parsed ({e}), and uv stops with an error when it looks for the "
                    "project's workspace there"
                ) from None
            return  # uv's workspace discovery stops at the first one it can parse


def _input_folder(path: Path) -> Path:
    return Path(os.path.normpath(path.absolute())).parent


def _uv_layer(found: tuple[Path, dict], folder: Path, own: Path | None) -> _UvLayer:
    config, settings = found
    table = "uv.toml" if config.name == "uv.toml" else "[tool.uv]"
    if config == own or (table == "uv.toml" and config.parent == folder):
        return _UvLayer(settings, config, table, table)
    shown = Path(os.path.relpath(config, folder)).as_posix()  # one spelling on every OS, as in a pyproject.toml
    return _UvLayer(settings, config, table, shown if table == "uv.toml" else f"{shown} [tool.uv]")


def _script_uv_layers(script: Path, block: dict) -> list[_UvLayer]:
    """A script's settings, as `uv run` applies them: its own [tool.uv], combined with the settings found from the
    script's folder up (uv 0.13.1 looks there whatever folder it runs in, and only in the folder it runs in when
    there are none; wheelgap can't know that folder)."""
    folder = _input_folder(script)
    layers = [_UvLayer(block, script, "[tool.uv]", "[tool.uv]")]
    if (found := _find_uv_settings(folder)) is not None:
        layers.append(_uv_layer(found, folder, None))
    return layers


def _lock_uv_layers(path: Path) -> list[_UvLayer]:
    """The settings `uv sync` applies to a project's uv.lock: those found from the lock's folder, the workspace
    root, up; or for a script's lock (example.py.lock), what `uv run` applies to the script beside it."""
    if path.name.endswith(".py.lock"):
        script = path.with_name(path.name.removesuffix(".lock"))
        block: dict = {}
        if script.is_file():
            try:
                block = _table(_table(_script_metadata(script), "tool"), "uv")
            except (SourceError, OSError):
                pass  # no readable block beside it: nothing of its own to apply, and the lock itself is still read
        return _script_uv_layers(script, block)
    folder = _input_folder(path)
    own = folder / "pyproject.toml"
    _refuse_unparseable_workspace_parent(folder, path)
    found = _find_uv_settings(folder, own)
    return [] if found is None else [_uv_layer(found, folder, own)]


def uv_lock_settings_above(path: Path) -> tuple[Path, list[str]] | None:
    """The uv.toml or pyproject.toml in a folder above a project's uv.lock whose build restrictions the lock check
    applies, with them (for --dockerfile-inputs, where the image may not have it)."""
    folder = _input_folder(path)
    found = _find_uv_settings(folder, folder / "pyproject.toml")
    if found is None or found[0].parent == folder or not (keys := _sets_build_restriction(found[1])):
        return None
    return found[0], keys


@dataclass
class _BuildPolicy:
    """The build and wheel restrictions uv applies to index packages, and whether no-sources turns
    [tool.uv.sources] off. `no_build` and `no_binary` are the setting that turns one on for every package, as an
    install policy names it (`[tool.uv] no-build`), False when a setting turns it off, or None when none sets it;
    the package lists map each name to the setting that names it (`[tool.uv] no-binary-package`)."""

    no_build: str | bool | None = None
    no_binary: str | bool | None = None
    no_build_package: dict[str, str] = field(default_factory=dict)
    no_binary_package: dict[str, str] = field(default_factory=dict)
    no_sources: bool = False

    def _lists(self) -> tuple[dict[str, str], dict[str, str]]:
        """The package lists uv reads: a list counts only while its blanket setting is unset (uv's NoBuild and
        NoBinary from_args: `no-binary = false` makes no-binary-package do nothing, checked with uv 0.13.1)."""
        return (
            self.no_build_package if self.no_build is None else {},
            self.no_binary_package if self.no_binary is None else {},
        )

    def settings_on(self) -> list[str]:
        firsts = [next(iter(names.values())) for names in self._lists() if names]
        return [setting for setting in (self.no_build, self.no_binary, *firsts) if isinstance(setting, str)]

    def apply(self, pkg: Package) -> None:
        """As uv's BuildOptions.no_build_package and no_binary_package: a blanket setting gives way to the other
        kind's list, so with no-binary a package in no-build-package still takes a wheel, and with no-build a
        package in no-binary-package still builds (uv 0.13.1 does both)."""
        build_list, binary_list = self._lists()
        restrictions = []
        if isinstance(self.no_build, str) and pkg.key not in binary_list:
            pkg.only_binary = True
            restrictions.append(self.no_build)
        elif pkg.key in build_list:
            pkg.only_binary = True
            restrictions.append(build_list[pkg.key])
        if isinstance(self.no_binary, str) and pkg.key not in build_list:
            pkg.no_binary = True
            restrictions.append(self.no_binary)
        elif pkg.key in binary_list:
            pkg.no_binary = True
            restrictions.append(binary_list[pkg.key])
        pkg.install_policy = ", ".join(restrictions) or None


def _uv_build_policy(layers: list[_UvLayer]) -> _BuildPolicy:
    """The restrictions in `layers`, highest precedence first, combined as uv combines a script's [tool.uv] with
    the settings found for it: the first layer that sets no-build, no-binary or no-sources decides it, and the
    package lists are joined (uv 0.13.1 builds a package either names; _BuildPolicy.apply has how the settings
    combine). uv's pip-only [tool.uv.pip] options don't apply to uv sync or uv run."""
    policy = _BuildPolicy()
    decided: set[str] = set()
    for layer in layers:
        for key in ("no-build", "no-binary", "no-sources"):
            if key not in layer.settings:
                continue
            value = layer.settings[key]
            if not isinstance(value, bool):
                raise SourceError(f"{layer.config}: {layer.table} {key} must be a boolean")
            if key in decided:
                continue
            decided.add(key)
            if key == "no-sources":
                policy.no_sources = value
            else:
                setattr(policy, key.replace("-", "_"), f"{layer.shown} {key}" if value else False)
        for key in ("no-build-package", "no-binary-package"):
            values = layer.settings.get(key, [])
            if not isinstance(values, list) or not all(isinstance(name, str) for name in values):
                raise SourceError(f"{layer.config}: {layer.table} {key} must be a list of strings")
            if any(not _NAME_RE.fullmatch(name) for name in values):
                raise SourceError(f"{layer.config}: {layer.table} {key} must contain valid package names")
            names = policy.no_build_package if key == "no-build-package" else policy.no_binary_package
            for name in values:
                names.setdefault(canonicalize_name(name), f"{layer.shown} {key}")
    return policy


def _uv_build_restrictions(path: Path, source: Source) -> None:
    """Apply the build and wheel restrictions of the uv settings `uv sync` (or `uv run`, for a script's lock)
    applies to the index packages in a uv.lock (_lock_uv_layers)."""
    policy = _uv_build_policy(_lock_uv_layers(path))
    for pkg in source.packages:
        if not pkg.skip:  # uv may build first-party/editable dependencies; wheelgap skips these.
            policy.apply(pkg)


# The kinds of [tool.uv.sources] entry uv takes, one per entry.
_UV_SOURCE_KINDS = ("git", "url", "path", "workspace", "index")


@dataclass
class UvRules:
    """What uv does to a pyproject.toml or script's requirements beyond what they say (#810, #811): the build
    restrictions in the settings it applies, and [tool.uv.sources], by canonical name (each entry with the file
    it's in), which take a requirement from git, a path, a URL, a workspace member or a named index instead of the
    default one. `indexes` maps the names of the [[tool.uv.index]] entries a source may name to their URLs, and
    `package_index` says where uv takes a requirement without a source of its own from (_UvPackageIndex)."""

    policy: _BuildPolicy
    sources: dict[str, tuple[object, Path]]
    indexes: dict[str, str]
    package_index: _UvPackageIndex

    def apply(self, pkg: Package) -> None:
        if pkg.skip:  # a direct URL: not from an index whatever the settings
            return
        if not self.policy.no_sources and (found := self.sources.get(pkg.key)) is not None:
            _apply_uv_source(pkg, *found, self.indexes)
        else:
            self.package_index.apply(pkg)
        if pkg.skip or pkg.error:
            return
        self.policy.apply(pkg)


def _is_pypi_index(url: str) -> bool:
    return url.rstrip("/") == "https://pypi.org/simple"


def _shown_index(url: str) -> str:
    """An index URL for a note, without the user and password it may hold."""
    try:
        return urlparse(url)._replace(netloc=urlparse(url).netloc.rpartition("@")[2]).geturl()
    except ValueError:
        return "an index URL wheelgap can't parse"


@dataclass
class _UvPackageIndex:
    """Where uv takes a requirement that has no [tool.uv.sources] entry from, when it isn't PyPI alone: `default`, an
    index in place of PyPI (an [[tool.uv.index]] with `default = true`, index-url or default-index), which no PyPI
    answer may stand for; `no_index`, the setting that turns every index off; or `extra`, an index (an
    [[tool.uv.index]] that isn't `explicit`, extra-index-url) or find-links that uv may take the package from before
    PyPI (its first-index strategy), each as a note names it. Verified with uv 0.13.1: `uv lock` takes markupsafe from
    such an index in all four shapes."""

    default: str | None = None
    no_index: str | None = None
    extra: str | None = None

    def apply(self, pkg: Package) -> None:
        if self.no_index is not None:
            pkg.error = f"{self.no_index} sets no-index, so uv installs {pkg.name} from find-links only"
        elif self.default is not None:
            pkg.registry = self.default  # pypi.fill_all skips it as a private index, unless WHEELGAP_INDEX_URL is it
        elif self.extra is not None:
            pkg.error = (
                f"{self.extra}, which uv may take {pkg.name} from before PyPI, and wheelgap doesn't read that index"
            )


def _uv_package_index(tables: list[tuple[dict, str]]) -> _UvPackageIndex:
    """The _UvPackageIndex of uv settings tables, each with how a note names it (`pyproject.toml: [tool.uv]`). Every
    table that may apply counts, so a PyPI answer is given only when none of them names another index."""
    found = _UvPackageIndex()
    for settings, where in tables:
        defaults: list[str] = []
        extras: list[str] = []
        indexes = settings.get("index", [])
        if not isinstance(indexes, list) or not all(isinstance(i, dict) for i in indexes):
            raise SourceError(f"{where} index must be a list of tables")
        for index in indexes:
            url = index.get("url")
            if not isinstance(url, str):
                raise SourceError(f"{where} has an [[index]] whose url isn't a string")
            if index.get("explicit") is True:
                continue  # only a [tool.uv.sources] entry that names it uses it
            (defaults if index.get("default") is True else extras).append(url)
        for key, into in (("index-url", defaults), ("default-index", defaults)):
            if key in settings:
                if not isinstance(settings[key], str):
                    raise SourceError(f"{where} {key} must be a string")
                into.append(settings[key])
        for key in ("extra-index-url", "find-links"):
            values = settings.get(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise SourceError(f"{where} {key} must be a list of strings")
            # find-links is a folder or page of files, never PyPI's index.
            extras.extend(v if key == "extra-index-url" else f"find-links {v}" for v in values)
        if settings.get("no-index") is True and found.no_index is None:
            found.no_index = where
        if found.default is None and (url := next((u for u in defaults if not _is_pypi_index(u)), None)):
            found.default = url
        if found.extra is None and (url := next((u for u in extras if not _is_pypi_index(u)), None)):
            found.extra = f"{where} names the index {_shown_index(url)}"
    return found


def _apply_uv_source(pkg: Package, entry: object, config: Path, indexes: dict[str, str]) -> None:
    """Take `pkg` from where its [tool.uv.sources] entry says: skip a git, URL, path or workspace source as the
    uv.lock reader does, and give an index source's package that index as its registry, so it's never answered with
    PyPI's files (pypi.fill_all skips it as a private index unless WHEELGAP_INDEX_URL is that index). An entry
    that picks a source by marker, extra or group leaves the package unknown: uv takes it from PyPI wherever none
    of them applies, and wheelgap doesn't follow that per target. So does an index none of the settings read
    defines."""
    where = f"{config}: [tool.uv.sources] {pkg.name}"
    entries: list[object] = entry if isinstance(entry, list) else [entry]
    tables = [e for e in entries if isinstance(e, dict)]
    if not tables or len(tables) != len(entries):
        raise SourceError(f"{where} must be a table or a list of tables")
    if len(tables) > 1 or any(key in e for e in tables for key in ("marker", "extra", "group")):
        pkg.error = (
            f"{config.name}: [tool.uv.sources] gives {pkg.name} a source by marker, extra or group, which wheelgap "
            "doesn't follow per target, so where uv installs it from isn't known"
        )
        return
    (source,) = tables
    kinds = [kind for kind in _UV_SOURCE_KINDS if kind in source]
    if len(kinds) != 1:
        raise SourceError(f"{where} must have exactly one of {', '.join(_UV_SOURCE_KINDS)}")
    kind = kinds[0]
    value = source[kind]
    if kind == "index":
        if not isinstance(value, str):
            raise SourceError(f"{where} has an index that isn't a string")
        if (url := indexes.get(value)) is None:
            # uv 0.13.1 refuses it ("references an undeclared index"), even when a uv.toml or UV_CONFIG_FILE defines
            # it: a source's index must be in the project's own pyproject.toml (or the workspace root's), or a
            # script's block.
            pkg.error = (
                f"{config.name}: [tool.uv.sources] takes {pkg.name} from the index {value!r}, which no "
                "[[tool.uv.index]] of the project defines, so uv refuses to install it"
            )
            return
        pkg.registry = url
    elif kind == "workspace" and value is not True:
        raise SourceError(f"{where} has a workspace that isn't true")
    else:
        pkg.skip = "editable" if kind == "path" and source.get("editable") is True else kind


def _uv_sources(uv: dict, config: Path) -> dict[str, tuple[object, Path]]:
    sources = uv.get("sources", {})
    if not isinstance(sources, dict):
        raise SourceError(f"{config}: [tool.uv.sources] must be a table")
    out: dict[str, tuple[object, Path]] = {}
    for name, entry in sources.items():
        key = canonicalize_name(name)
        if key in out:
            raise SourceError(f"{config}: [tool.uv.sources] names {key!r} twice once normalized")
        out[key] = (entry, config)
    return out


def _uv_indexes(*tables: object) -> dict[str, str]:
    """The named [[tool.uv.index]] (or uv.toml [[index]]) entries of each `index` list, the first of a name first."""
    indexes: dict[str, str] = {}
    for table in tables:
        for index in table if isinstance(table, list) else []:
            if isinstance(index, dict) and isinstance(index.get("name"), str) and isinstance(index.get("url"), str):
                indexes.setdefault(index["name"], index["url"])
    return indexes


def _uv_rules(data: dict, path: Path, script: bool) -> UvRules:
    """The UvRules for a pyproject.toml or script (`data`, a script's block as _project_source gets it). A script's
    settings are its block's [tool.uv] with those `uv run` finds (_script_uv_layers), its sources its block's. For
    a pyproject.toml uv applies the settings found from its folder up (a uv.toml beside it in place of its
    [tool.uv]); for a workspace member, those of the workspace root instead (uv ignores the member's own), and the
    root's [tool.uv.sources] too, under the member's own."""
    own_uv = _table(_table(data, "tool"), "uv")
    sources = _uv_sources(own_uv, path)
    root_index = None
    # Every settings table that may apply to the default index, so a PyPI answer is given only if none names another.
    index_tables = [(own_uv, f"{path}: [tool.uv]")]
    if script:
        layers = _script_uv_layers(path, own_uv)
    else:
        folder = _input_folder(path)
        own = folder / path.name
        _refuse_unparseable_workspace_parent(folder, path)
        workspace = _workspace_root(path, data)
        found = _find_uv_settings(folder, own) if workspace is None else None
        if workspace is not None:
            root, root_data, certain = workspace
            root_uv = _table(_table(root_data, "tool"), "uv")
            root_index = root_uv.get("index")
            index_tables.append((root_uv, f"{root}: [tool.uv]"))
            found = _find_uv_settings(root.parent)
            if certain:
                sources = {**_uv_sources(root_uv, root), **sources}
            else:
                # A member gets the root's settings and sources, a project of its own the settings found for it.
                alone = _find_uv_settings(folder, own)
                differ = [
                    f"{config} sets {', '.join(keys)}"
                    for config, settings in ([] if alone == found else [f for f in (found, alone) if f is not None])
                    if (keys := [key for key in _UV_APPLIED_SETTINGS if key in settings])
                ]
                if root_uv.get("sources"):
                    differ.append(f"{root} has [tool.uv.sources]")
                if differ:
                    raise SourceError(
                        f"{path}: wheelgap can't tell whether this is a member of the workspace at {root} (its "
                        "[tool.uv.workspace] members or exclude use patterns wheelgap doesn't match as uv does), and "
                        f"uv gives a member the workspace root's settings and sources: {differ[0]}"
                    )
                found = alone
        layers = [] if found is None else [_uv_layer(found, folder, own)]
    index_tables += [(layer.settings, f"{layer.config}: {layer.table}") for layer in layers]
    # A source's index must be in the project's own [tool.uv] (or the workspace root's), or a script's block: uv
    # 0.13.1 refuses one only a uv.toml defines.
    indexes = _uv_indexes(own_uv.get("index"), root_index)
    return UvRules(_uv_build_policy(layers), sources, indexes, _uv_package_index(index_tables))


def parse_uv_lock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    manifest = data.get("manifest", {})
    if not isinstance(manifest, dict):
        raise SourceError(f"{path.name}: `manifest` isn't a table")
    # The manifest's lists of edges are walked here and again for a selection, so their shapes are checked first.
    manifest_groups = manifest.get("dependency-groups", {})
    if not isinstance(manifest_groups, dict):
        raise SourceError(f"{path.name}: the manifest has a `dependency-groups` that isn't a table of groups")
    if not all(isinstance(group, list) for group in manifest_groups.values()):
        raise SourceError(f"{path.name}: the manifest has a group of dependencies that isn't a list")
    if not isinstance(manifest.get("requirements", []), list):
        raise SourceError(f"{path.name}: the manifest has a `requirements` that isn't a list")
    members = {canonicalize_name(m) for m in manifest.get("members", [])}
    edges: dict[int, list[Edge]] = {}
    source = Source(path.name, edges=edges, uv_packages=data.get("package", []), uv_manifest=manifest)
    if path.name.endswith(".py.lock"):
        source.uv_script = str(path).removesuffix(".lock")  # as given, so --fix's `uv lock --script` runs from here
    else:
        source.uv_pyproject = path.with_name("pyproject.toml")
    for p in data.get("package", []):
        src = p.get("source", {})
        pkg = Package(package_name(path, p.get("name")), p.get("version"), origin=path.name)
        kind = next((k for k in ("editable", "virtual", "git", "path", "directory", "url") if k in src), None)
        if kind and "registry" not in src:
            pkg.skip = kind
        else:
            pkg.registry = src.get("registry")
            files = [f for f in (_filename(w, path.name, pkg.name) for w in p.get("wheels", [])) if f]
            if p.get("sdist"):
                files.append(_filename(p["sdist"], path.name, pkg.name) or f"{p['name']}-{p.get('version')}.tar.gz")
                pkg.sdist_url, pkg.sdist_sha256 = _sdist_source(p["sdist"], "hash")
            pkg.files = files
        i = len(source.packages)
        source.packages.append(pkg)
        edges[i] = _uv_edges(path, p)
        # Roots are the project itself (source "."), plus workspace members. A path dependency elsewhere
        # (directory = "libs/x") is reached through its dependents' edges and their markers instead.
        if (kind in ("editable", "virtual") and src.get(kind) == ".") or pkg.key in members:
            source.roots.append(i)
    _uv_build_restrictions(path, source)
    for name, group in manifest_groups.items():
        for d in group:
            source.root_groups[len(source.root_edges)] = name
            source.root_edges.append(_uv_manifest_edge(path, d))
    source.root_edges.extend(_uv_manifest_edge(path, d) for d in manifest.get("requirements", []))
    if not source.roots and not source.root_edges:
        source.edges = None  # no idea where the graph starts; treat every package as needed
    if isinstance(data.get("requires-python"), str):
        source.requires_python = data["requires-python"]
    supported = data.get("supported-markers", [])
    if not isinstance(supported, list) or not all(isinstance(marker, str) for marker in supported):
        raise SourceError(f"{path.name}: `supported-markers` isn't a list of strings")
    source.supported_markers = supported
    source.uv_conflicts = _uv_conflicts(data.get("conflicts"))
    return source


def uv_conflict_name(package: str, kind: str, name: str | None = None) -> str:
    """The name uv's lock markers give a conflict item: `extra == 'extra-4-demo-old'` holds when the extra `old` of
    the package `demo` is on. The number is the package name's length. encode_package_extra, encode_package_group and
    encode_project in https://github.com/astral-sh/uv/blob/0.13.9/crates/uv-resolver-types/src/universal_marker.rs"""
    package = canonicalize_name(package)
    if kind == "project":
        return f"project-{len(package)}-{package}"
    return f"{kind}-{len(package)}-{package}-{canonicalize_name(name or '')}"


def _uv_conflicts(conflicts: object) -> list[list[str]] | None:
    """A uv.lock's `conflicts` as lists of uv_conflict_name names, or None when there are none or a set doesn't have
    the shape uv writes (a list of tables, each with a `package` and at most one `extra` or `group`)."""
    if not isinstance(conflicts, list) or not conflicts:
        return None
    sets: list[list[str]] = []
    for items in conflicts:
        if not isinstance(items, list):
            return None
        names: list[str] = []
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("package"), str)
                or len(item.keys() - {"package"}) > 1
            ):
                return None
            kind = next(iter(item.keys() - {"package"}), "project")
            if kind not in ("project", "extra", "group") or (kind != "project" and not isinstance(item[kind], str)):
                return None
            names.append(uv_conflict_name(item["package"], kind, item.get(kind)))
        sets.append(names)
    return sets


def uv_selected_roots(source: Source) -> list[int]:
    # A rooted workspace syncs its project, not every sibling member. With no project
    # (a virtual workspace), all member roots remain eligible, as with uv sync.
    packages = source.uv_packages or []
    project = [
        i
        for i in source.roots
        if any(packages[i].get("source", {}).get(kind) == "." for kind in ("editable", "virtual"))
    ]
    return project or source.roots


def uv_extra_names(package: dict) -> frozenset[str]:
    # uv omits empty optional-dependency groups from the graph, but records their names here.
    return frozenset(canonicalize_name(name) for name in package.get("optional-dependencies", {})) | frozenset(
        canonicalize_name(name) for name in package.get("metadata", {}).get("provides-extras", [])
    )


def uv_group_names(package: dict) -> frozenset[str]:
    return frozenset(canonicalize_name(name) for name in package.get("dev-dependencies", {})) | frozenset(
        canonicalize_name(name) for name in package.get("metadata", {}).get("requires-dev", {})
    )


def select_uv_dependencies(
    sources: list[Source],
    selection: UvSelection,
    strict: bool = True,
    *,
    configured_extras: bool = False,
    poetry: bool = True,
    pdm: bool = True,
) -> None:
    """Apply `selection` to the uv.lock and Poetry 2.x poetry.lock sources, and its groups to pdm.lock sources. With
    strict (selectors from the command line), other inputs are an error; otherwise (from [tool.wheelgap]) they're left
    as they are. configured_extras: the extras come from [tool.wheelgap], which don't apply to pdm.lock inputs, so a
    pdm.lock doesn't reject them. poetry=False leaves poetry.lock inputs as they are, and pdm=False pdm.lock inputs
    (only extras configured)."""
    # `poetry install --only main --extras api` is a production install, so poetry.lock inputs allow it.
    if (
        selection.only_groups
        and (selection.extras or selection.all_extras)
        and (not sources or any(source.poetry_tables is None for source in sources))
    ):
        raise SourceError("--only-group cannot be combined with --extra or --all-extras for uv.lock inputs")
    if selection.no_dev and "dev" in selection.groups | selection.only_groups:
        raise SourceError("--no-dev cannot be combined with an explicit dev group")
    if strict and (not sources or any(source.uv_packages is None for source in sources)):
        others = [source for source in sources if source.uv_packages is None]
        if sources and all(source.poetry_tables is not None or source.pdm_groups is not None for source in others):
            pdm_lock = any(source.pdm_groups is not None for source in others)
            if pdm_lock and (selection.all_extras or (selection.extras and not configured_extras)):
                raise SourceError(
                    "extra selection requires uv.lock or poetry.lock inputs (pdm.lock supports groups only)"
                )
        else:
            unsupported = [s.label for s in others if s.poetry_tables is None and s.pdm_groups is None]
            raise SourceError(
                "extra/group selection requires uv.lock, Poetry 2.x poetry.lock or pdm.lock inputs "
                f"(not {', '.join(unsupported) or 'no input'})"
            )
    for source in sources:
        if source.pdm_groups is not None:
            # A lock without package groups or inherit_metadata: configured groups leave it as it is.
            if pdm and (strict or (source.pdm_inherit_metadata and all(g is not None for g in source.pdm_groups))):
                _select_pdm_groups(source, selection)
            continue
        if source.poetry_tables is not None:
            # A Poetry 1.x lock doesn't record groups or extras per package; a configured selection leaves it as it is.
            if poetry and (strict or source.edges is None):
                _select_poetry(source, selection)
            continue
        if source.uv_packages is None:
            continue
        if source.uv_script is not None and not strict:
            # A script's lock has no extras or groups; [tool.wheelgap]'s belong to the project beside it.
            continue
        pyproject = source.uv_pyproject
        if pyproject is not None and pyproject.is_file():
            # Nothing else may have read this file yet: a uv.toml beside it replaces [tool.uv] for build restrictions.
            try:
                text = pyproject.read_text(encoding="utf-8-sig")  # -sig: Windows editors add a BOM
            except UnicodeDecodeError:
                raise SourceError(f"{pyproject}: isn't UTF-8, so its [tool.uv] default-groups can't be read") from None
            try:
                uv = tomllib.loads(text).get("tool", {}).get("uv", {})
            except tomllib.TOMLDecodeError as exc:
                raise SourceError(f"{pyproject}: invalid TOML: {exc}") from None
            defaults = uv.get("default-groups", ["dev"])
            if defaults == "all":
                source.uv_default_groups = ["*"]
            elif isinstance(defaults, list) and all(isinstance(g, str) for g in defaults):
                source.uv_default_groups = [canonicalize_name(g) for g in defaults]
            else:
                raise SourceError(f"{pyproject}: [tool.uv] default-groups must be a list of strings or 'all'")
        # An empty lock (no packages: a Poetry project uv locked, or one with only empty dependency groups) has no
        # roots, but nothing to install either: it goes on to the checks below, so a group its manifest lists is
        # known, any other extra, group or default group is unknown, as uv sync says.
        if source.edges is None and source.uv_packages:
            raise SourceError(f"{source.label}: cannot select extras/groups without project or manifest roots")
        packages = source.uv_packages or []
        roots = uv_selected_roots(source)
        extras = frozenset(name for i in roots for name in uv_extra_names(packages[i]))
        groups: set[str] = {canonicalize_name(name) for name in source.uv_manifest.get("dependency-groups", {})}
        groups.update(frozenset().union(*(uv_group_names(packages[i]) for i in roots)))
        unknown_defaults = set(source.uv_default_groups) - groups - {"dev", "*"}
        if unknown_defaults:
            raise SourceError(f"{source.label}: unknown default groups: {', '.join(sorted(unknown_defaults))}")
        for label, unknown in (
            ("extras", selection.extras - extras),
            ("groups", (selection.groups | selection.only_groups) - groups),
        ):
            if unknown:
                raise SourceError(f"{source.label}: unknown {label}: {', '.join(sorted(unknown))}")
        source.uv_selection = selection


def _poetry_markers(p: dict, groups: Iterable[str]) -> list[str] | None:
    """A Poetry 2.x package's markers for the given groups: None (always installed) if a group has no marker."""
    markers = p.get("markers")
    if isinstance(markers, str):
        return [markers]
    if not isinstance(markers, dict):
        return None
    # Poetry leaves a group out of the dict when the package is always installed for it.
    if any(g not in markers for g in groups):
        return None
    return [m for g in groups if isinstance(m := markers.get(g), str)] or None


def _select_poetry(source: Source, selection: UvSelection) -> None:
    """Keep the poetry.lock packages that Poetry would install for `selection`, with Poetry's meanings: `main` and
    every non-optional group by default, `--group` adds a group (like `poetry install --with`), `--no-dev` leaves
    out `dev`, and `--only-group` installs only the named groups (`main` too only if named). Extras are only those
    selected, as with `poetry install --extras` / `--all-extras`: a package's markers read `extra` as that set."""
    from .check import poetry_extras_decide  # check imports this module

    tables = source.poetry_tables or []
    if source.edges is not None or any(not isinstance(t.get("groups"), list) for t in tables):
        raise SourceError(
            f"{source.label}: extra/group selection needs a poetry.lock from Poetry 2.x (with package groups)"
        )
    # Poetry checks --extras against the lock's [extras] table, which lists every extra (empty ones too).
    known_extras = frozenset(canonicalize_name(name) for name in source.poetry_lock_extras)
    if selection.extras - known_extras:
        raise SourceError(f"{source.label}: unknown extras: {', '.join(sorted(selection.extras - known_extras))}")
    extras: PoetryExtras | None = (known_extras, known_extras if selection.all_extras else selection.extras)
    known = {"main"} | {canonicalize_name(g) for t in tables for g in t["groups"]}
    optional: set[str] = set()
    pyproject = source.poetry_pyproject
    unsure = None
    if known_extras and (pyproject is None or not pyproject.is_file()):
        unsure = "no pyproject.toml beside it to show how its extras are declared"
    if pyproject is not None and pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        elsewhere, unreadable = _poetry_extras_declared_elsewhere(data)
        if unreadable:
            unsure = f"pyproject.toml has requirements wheelgap can't read ({', '.join(unreadable)})"
        elif elsewhere:
            unsure = f"{', '.join(elsewhere)} in an extra and declared elsewhere in pyproject.toml"
        # PEP 735 groups (Poetry 2.2+): an empty one is in no package's groups, but it's still a known group.
        pep735 = data.get("dependency-groups")
        known.update(canonicalize_name(name) for name in (pep735 if isinstance(pep735, dict) else {}))
        declared = data.get("tool", {}).get("poetry", {})
        if isinstance(declared.get("dev-dependencies"), dict):
            known.add("dev")  # the old [tool.poetry.dev-dependencies] table declares `dev`, even when it's empty
        for name, group in (declared.get("group") or {}).items():
            known.add(canonicalize_name(name))
            if isinstance(group, dict) and group.get("optional") is True:
                optional.add(canonicalize_name(name))
    if unsure is not None:
        # The lock's markers can be wrong for Poetry 2.0-2.2 here: never let extras leave a package out of it.
        source.poetry_extras_unsure = (
            f"{unsure}, so Poetry 2.0-2.2 and 2.3+ may install different packages without an extra; "
            "extras leave no package out of this lock"
        )
        extras = None
    unknown = (selection.groups | selection.only_groups) - known
    if unknown:
        raise SourceError(f"{source.label}: unknown groups: {', '.join(sorted(unknown))}")
    if selection.no_dev and "dev" not in known:
        # Like `poetry install --without dev` on a project with no dev group (Poetry 2.5.1).
        # Without pyproject.toml an empty dev group can't be seen (the lock only lists groups that have packages).
        unseen = "" if pyproject is not None and pyproject.is_file() else "; no pyproject.toml beside it to declare one"
        raise SourceError(f"{source.label}: Group(s) not found: dev (via --no-dev){unseen}")
    if selection.only_groups:
        chosen = set(selection.only_groups | selection.groups)
    else:
        chosen = (known - optional) | selection.groups
    if selection.no_dev:
        chosen.discard("dev")
    kept = []
    kept_anyway: list[str] = []
    for pkg, table in zip(source.packages, tables, strict=True):
        groups = [g for g in table["groups"] if canonicalize_name(g) in chosen]
        if not groups:
            source.unselected_keys.add(pkg.key)
            continue
        markers = _poetry_markers(table, groups)
        if markers is not None and extras is not None:
            decided = [poetry_extras_decide(m, extras) for m in markers]
            if any(holds for holds, _ in decided):
                markers = None  # a selected extra installs it, on every target
            elif all(holds is not None for holds, _ in decided):
                source.unselected_keys.add(pkg.key)  # only for extras that aren't selected
                continue
            elif any(holds is False or ruled_out for holds, ruled_out in decided):
                # Keep the false `extra == "x"` marker beside another group's marker: Poetry 2.0-2.2 install the
                # package everywhere then (B3 in test_poetry_lock_mixed_extra_markers_report_a_superset).
                kept_anyway.append(pkg.name)  # the extras selected would have left it out somewhere
        pkg.markers = markers
        kept.append(pkg)
    if kept_anyway:
        shown = ", ".join(kept_anyway[:5]) + (f" and {len(kept_anyway) - 5} more" if len(kept_anyway) > 5 else "")
        source.poetry_extras_unsure = (
            f"{shown} kept for every extra: their markers combine an extra with other conditions, and Poetry 2.0-2.2 "
            "can install them without the extra"
        )
    source.packages = kept
    source.poetry_tables = None


def _poetry_extras_declared_elsewhere(data: dict) -> tuple[list[str], list[str]]:
    """Names in an extra that pyproject.toml also declares somewhere else: in the main dependencies (in any form), in
    a group, or in another extra entry that isn't the same plain requirement. Poetry 2.x can lock these, and what they
    depend on, with only the extra's marker (`requests; python_version < '3.11'` that an extra also lists becomes
    `extra == "s3"`), and then Poetry 2.3 and later (which install from the lock's markers) leave them out where
    Poetry 2.0 to 2.2 (which re-resolve, as installer.re-resolve does) install them. Found with Poetry 2.5.1 locks
    installed by Poetry 2.0.1, 2.2.1 and 2.5.1. The same plain requirement in several extras locks correctly. Also
    returns the requirement strings it can't read, which could be any of these."""

    def table(value: object) -> dict:
        return value if isinstance(value, dict) else {}

    def strings(value: object) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    unreadable: list[str] = []

    def requirements(specs: list[str]) -> list[Requirement]:
        out: list[Requirement] = []
        for spec in specs:
            try:
                out.append(Requirement(spec))
            except InvalidRequirement:
                unreadable.append(spec)
        return out

    def names(specs: list[str]) -> list[str]:
        return [canonicalize_name(req.name) for req in requirements(specs)]

    project = table(data.get("project"))
    poetry = table(table(data.get("tool")).get("poetry"))
    legacy = {canonicalize_name(name): spec for name, spec in table(poetry.get("dependencies")).items()}
    # Each extra entry, as (name, what it says): a repeated entry is safe only if it's the same plain requirement.
    entries: list[tuple[str, str | None]] = []
    for specs in table(project.get("optional-dependencies")).values():
        for req in requirements(strings(specs)):
            says = f"{sorted(map(canonicalize_name, req.extras))} {req.specifier} {req.url}"
            entries.append((canonicalize_name(req.name), None if req.marker else says))
    pep621 = {name for name, _ in entries}
    for specs in table(poetry.get("extras")).values():
        for legacy_name in map(canonicalize_name, strings(specs)):
            dependency = legacy.get(legacy_name)
            plain = isinstance(dependency, dict) and not {"markers", "python", "platform"} & dependency.keys()
            entries.append((legacy_name, f"tool.poetry {legacy_name}" if plain else None))
    in_extras = {name for name, _ in entries}
    elsewhere: set[str] = set()
    for name in in_extras:
        said = [what for n, what in entries if n == name]
        if len(said) > 1 and (None in said or len(set(said)) > 1):
            elsewhere.add(name)
    # Main dependencies. A [tool.poetry.dependencies] table with optional = true is how a legacy extra declares its
    # dependency, so only that doesn't count; any form counts for a [project] optional-dependencies name.
    elsewhere |= in_extras & set(names(strings(project.get("dependencies"))))
    for name, declared in legacy.items():
        own = isinstance(declared, dict) and declared.get("optional") is True and name not in pep621
        if name != "python" and not own:
            elsewhere.add(name)
    # Groups.
    tables = [table(poetry.get("dev-dependencies"))]
    tables += [table(table(g).get("dependencies")) for g in table(poetry.get("group")).values()]
    elsewhere |= {canonicalize_name(name) for t in tables for name in t}
    elsewhere |= {name for group in table(data.get("dependency-groups")).values() for name in names(strings(group))}
    return sorted(elsewhere & in_extras), unreadable


def _select_pdm_groups(source: Source, selection: UvSelection) -> None:
    """Keep the pdm.lock packages that `pdm install` would install for `selection`, with PDM's meanings: every group
    the lock was made for ([metadata] groups) by default, `--no-dev` leaves out `dev` (like `pdm install --without
    dev`), `--group` only checks that a group is locked, and `--only-group` installs only the named groups (`default`
    too only if named). A package counts if any of its groups is selected."""
    per_package = source.pdm_groups or []
    if not source.pdm_inherit_metadata or any(g is None for g in per_package):
        raise SourceError(
            f"{source.label}: group selection needs a pdm.lock with package groups "
            "(locked with the inherit_metadata strategy)"
        )
    package_groups = [[canonicalize_name(g) for g in groups or []] for groups in per_package]
    if source.pdm_locked_groups is not None:
        locked = set(source.pdm_locked_groups)
    else:  # no [metadata] groups: the groups its packages record
        locked = {"default"} | {g for groups in package_groups for g in groups}
    asked = selection.groups | selection.only_groups
    if asked - locked:
        declared: set[str] = set()
        pyproject = source.pdm_pyproject
        if pyproject is not None and pyproject.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            tables = (
                data.get("project", {}).get("optional-dependencies"),
                data.get("dependency-groups"),
                data.get("tool", {}).get("pdm", {}).get("dev-dependencies"),
            )
            declared = {canonicalize_name(name) for table in tables if isinstance(table, dict) for name in table}
        unknown = asked - locked - declared
        if unknown:
            raise SourceError(f"{source.label}: unknown groups: {', '.join(sorted(unknown))}")
        unlocked = sorted(asked - locked)
        raise SourceError(
            f"{source.label}: groups not in the lock: {', '.join(unlocked)} "
            f"(lock them with `pdm lock {' '.join(f'-G {name}' for name in unlocked)}`)"
        )
    chosen = set(selection.only_groups | selection.groups) if selection.only_groups else locked
    if selection.no_dev:
        chosen.discard("dev")
    kept = []
    for pkg, groups in zip(source.packages, package_groups, strict=True):
        if any(g in chosen for g in groups):
            kept.append(pkg)
        else:
            source.unselected_keys.add(pkg.key)
    source.packages = kept
    source.pdm_groups = None


def parse_pylock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    pkgs = []
    for p in data.get("packages", []):
        pkg = Package(package_name(path, p.get("name")), p.get("version"), origin=path.name, registry=p.get("index"))
        if p.get("marker"):
            pkg.markers = [p["marker"]]
        if any(k in p for k in ("vcs", "directory", "archive")) and "wheels" not in p and "sdist" not in p:
            pkg.skip = next(k for k in ("vcs", "directory", "archive") if k in p)
        else:
            files = [f for f in (_filename(w, path.name, pkg.name) for w in p.get("wheels", [])) if f]
            if p.get("sdist"):
                files.append(_filename(p["sdist"], path.name, pkg.name) or f"{p['name']}-{p.get('version')}.tar.gz")
                pkg.sdist_url, pkg.sdist_sha256 = _sdist_source(p["sdist"], "hashes")
            pkg.files = files
        requires = p.get("requires-python")
        if pkg.files is not None and isinstance(requires, str):
            pkg.locked_requires_python = requires
        pkgs.append(pkg)
    requires = data.get("requires-python")
    return Source(path.name, pkgs, requires_python=requires if isinstance(requires, str) else None)


def _poetry_edges(p: dict) -> list[Edge]:
    """A poetry.lock package's dependencies as edges. A value is a version string, a table, or a list of
    tables for different markers (numpy = [{version = ">=2", markers = "python_version >= '3.10'"}, ...])."""
    out: list[Edge] = []
    for name, spec in p.get("dependencies", {}).items():
        for s in spec if isinstance(spec, list) else [spec]:
            version = s.get("version") if isinstance(s, dict) else s
            marker = s.get("markers") if isinstance(s, dict) else None
            out.append((name, version if isinstance(version, str) and version != "*" else None, marker, None))
    return out


def parse_poetry_lock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    metadata = data.get("metadata", {})
    legacy_files = metadata.get("files", {})
    # Lock files before 2.1 (all of Poetry 1.x) only record markers on dependency edges, not on packages.
    edge_markers_only = _version_tuple(str(metadata.get("lock-version", "2.1"))) < (2, 1)
    pkgs: list[Package] = []
    edges: dict[int, list[Edge]] = {}
    for p in data.get("package", []):
        pkg = Package(package_name(path, p.get("name")), p.get("version"), origin=path.name)
        markers = p.get("markers")
        if markers is not None and not (
            isinstance(markers, str)
            or isinstance(markers, dict)
            and all(isinstance(marker, str) for marker in markers.values())
        ):
            raise SourceError(f"{path.name}: {pkg.name}: markers must be a string or a table of strings")
        if isinstance(markers, str):
            pkg.markers = [markers]
        elif isinstance(markers, dict):
            # Poetry leaves a group out of the dict when the package is always installed for it.
            always = any(g not in markers for g in p.get("groups", []))
            pkg.markers = None if always else ([m for m in markers.values() if isinstance(m, str)] or None)
        src_type = p.get("source", {}).get("type")
        if src_type in ("git", "hg", "svn", "bzr", "directory", "file", "url"):
            pkg.skip = src_type
        else:
            # A "legacy" source is a simple index: remember it so the package isn't looked up on PyPI.
            url = p.get("source", {}).get("url")
            pkg.registry = url if isinstance(url, str) else None
            entries = p.get("files")
            if not entries:
                entries = legacy_files.get(p["name"])
            # No files recorded (old [metadata.hashes] locks, or `files = []`, which Poetry writes when the source
            # gave it no hashes): Poetry installs the best file the index has, unchecked (`if not package.files:
            # return ordered_links[0]` in poetry/installation/chooser.py). So PyPI packages are looked up on the
            # index and packages from another index are skipped (fill_all), rather than read as having no files.
            pkg.files = None if not entries else [f for f in (_filename(e, path.name, pkg.name) for e in entries) if f]
        edges[len(pkgs)] = _poetry_edges(p)
        requires = p.get("python-versions")
        # Not before 2.1: a direct dependency's Python marker is only in pyproject.toml there, so the range could
        # report a package for a Python that Poetry never installs it on (see Package.locked_requires_python).
        if not edge_markers_only and pkg.files is not None and isinstance(requires, str):
            pkg.locked_requires_python = requires
        pkgs.append(pkg)
    # With markers on edges only, walk the graph from the packages nothing depends on, as for uv.lock.
    if edge_markers_only and pkgs:
        roots = set(_graph_roots(pkgs, edges))
        # The project's own dependencies are roots too, even if another package also depends on them
        # under a marker (colorama directly, and via click only on Windows).
        pyproject = path.parent / "pyproject.toml"
        declared = _project_dependencies(pyproject)
        roots |= {i for i, p in enumerate(pkgs) if p.key in (declared or ())}
        source = Source(path.name, pkgs, edges=edges, roots=sorted(roots), poetry_tables=data.get("package", []))
        if not declared:
            # Without them, a direct dependency that another package needs only under a marker would be hidden
            # wherever the marker is false: check() makes such a package `unknown` where it would have a gap (#815).
            source.poetry_roots_unknown = (
                f"there is no pyproject.toml beside {path.name}"
                if not pyproject.is_file()
                else f"the pyproject.toml beside {path.name} "
                + ("can't be read" if declared is None else "declares no dependencies")
            )
        return source
    extras = data.get("extras")
    return Source(
        path.name,
        pkgs,
        poetry_tables=data.get("package", []),
        poetry_pyproject=path.parent / "pyproject.toml",
        poetry_lock_extras=[name for name in extras if isinstance(name, str)] if isinstance(extras, dict) else [],
    )


def _graph_roots(pkgs: list[Package], edges: dict[int, list[Edge]]) -> list[int]:
    """Where to start walking a lock that doesn't record its project: every package in a group of
    packages (a strongly connected component, usually just one package) that nothing outside the group
    depends on. That covers dependency cycles such as sphinx <-> sphinxcontrib-*, which no package
    "depended on by nothing" would reach."""
    by_name: dict[str, list[int]] = {}
    for i, p in enumerate(pkgs):
        by_name.setdefault(p.key, []).append(i)
    succ = {
        i: sorted({j for name, *_ in edges.get(i, []) for j in by_name.get(canonicalize_name(name), [])})
        for i in range(len(pkgs))
    }
    comp = _components(len(pkgs), succ)
    has_outside_parent = {comp[j] for i in succ for j in succ[i] if comp[i] != comp[j]}
    return [i for i in range(len(pkgs)) if comp[i] not in has_outside_parent]


def _components(n: int, succ: dict[int, list[int]]) -> list[int]:
    """Tarjan's strongly connected components, iteratively (lockfiles can be deep)."""
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    comp = [-1] * n
    stack: list[int] = []
    on_stack: set[int] = set()
    counter = count = 0
    for root in range(n):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, child = work.pop()
            if child == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack.add(v)
            if child < len(succ[v]):
                work.append((v, child + 1))
                w = succ[v][child]
                if w not in index:
                    work.append((w, 0))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            if low[v] == index[v]:
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp[w] = count
                    if w == v:
                        break
                count += 1
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return comp


def _project_dependencies(pyproject: Path) -> set[str] | None:
    """Names a project declares itself (PEP 621 and Poetry tables), to use as roots for poetry.lock, or None when
    the pyproject.toml is missing or isn't UTF-8 TOML. Odd values in a readable one are left out, never an error."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8-sig"))
    except Exception:
        return None

    def table(value: object) -> dict:
        return value if isinstance(value, dict) else {}

    def strings(value: object) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    project = table(data.get("project"))
    specs = strings(project.get("dependencies"))
    for extra in table(project.get("optional-dependencies")).values():
        specs += strings(extra)
    for group in table(data.get("dependency-groups")).values():
        specs += strings(group)
    names: set[str] = set()
    for spec in specs:
        try:
            names.add(canonicalize_name(Requirement(spec).name))
        except InvalidRequirement:
            continue
    poetry = table(table(data.get("tool")).get("poetry"))
    tables = [table(poetry.get("dependencies")), table(poetry.get("dev-dependencies"))]
    tables += [table(table(g).get("dependencies")) for g in table(poetry.get("group")).values()]
    names |= {canonicalize_name(name) for t in tables for name in t if name.lower() != "python"}
    return names


def _version_tuple(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in text.split("."))
    except ValueError:
        return (0,)


def _add_once(pkgs: list[Package], seen: dict[tuple[str, str | None], Package], pkg: Package) -> None:
    """Append `pkg` unless the same name and version is already there; then widen that entry's markers.

    Pipenv lists a package used by several categories (default, develop, docs...) in each of them, and PDM
    writes one entry per extras variant (`coverage` and `coverage[toml]`). Either way it's one install,
    needed if any entry is.
    """
    key = (pkg.key, pkg.version)
    first = seen.get(key)
    if first is None:
        seen[key] = pkg
        pkgs.append(pkg)
        return
    if pkg.skip == "private index":
        first.skip = pkg.skip  # Pipenv may pin the index in one category only: keep the name off pypi.org anyway
    if pkg.registry is not None and pkg.registry != first.registry:
        if first.registry is None:
            first.registry = pkg.registry  # the other entry is from PyPI: this index decides for both
        else:
            first.skip = "private index"  # pinned to two indexes: no single lookup is safe for both
    if first.locked_requires_python != pkg.locked_requires_python:
        first.locked_requires_python = None  # conflicting entries can't establish a release-wide restriction
    if first.markers is not None:
        first.markers = None if pkg.markers is None else first.markers + pkg.markers


def parse_pdm_lock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    metadata = data.get("metadata", {})
    legacy_files = metadata.get("files", {})
    locked_groups = metadata.get("groups")
    strategy = metadata.get("strategy")
    inherit_metadata = isinstance(strategy, list) and "inherit_metadata" in strategy
    pkgs: list[Package] = []
    seen: dict[tuple[str, str | None], Package] = {}
    # Each package's groups, merged over its entries (PDM writes one per extras variant).
    groups: dict[tuple[str, str | None], list[str] | None] = {}
    for p in data.get("package", []):
        pkg = Package(package_name(path, p.get("name")), p.get("version"), origin=path.name)
        recorded = p.get("groups")
        recorded = [g for g in recorded if isinstance(g, str)] if isinstance(recorded, list) else None
        key = (pkg.key, pkg.version)
        before = groups.get(key, [])
        groups[key] = None if recorded is None or (key in groups and before is None) else [*(before or []), *recorded]
        if p.get("marker"):
            pkg.markers = [p["marker"]]
        src_type = next((k for k in ("git", "hg", "svn", "bzr", "path", "url", "file") if k in p), None)
        if src_type:
            pkg.skip = src_type
        else:
            entries = p.get("files")
            if entries is None:
                # Older lockfiles (PDM before 2.8) keep files in one [metadata.files] table
                # keyed by "name version", with the name as PDM wrote it, which isn't always
                # normalized ("Jinja2 3.1.2" for jinja2): try the exact key, then match names.
                entries = legacy_files.get(f"{p['name']} {p.get('version')}")
                if entries is None:
                    target_name = canonicalize_name(p["name"])
                    target_version = p.get("version")
                    for key, value in legacy_files.items():
                        key_name, _, key_version = key.rpartition(" ")
                        if key_version == target_version and canonicalize_name(key_name) == target_name:
                            entries = value
                            break
            if entries is not None:
                pkg.files = [f for f in (_filename(e, path.name, pkg.name) for e in entries) if f]
            # Not in either place: leave files as None so the PyPI lookup fills them in.
        requires = p.get("requires_python")
        # Only with inherit_metadata, which records each package's marker (see Package.locked_requires_python).
        if inherit_metadata and pkg.files is not None and isinstance(requires, str):
            pkg.locked_requires_python = requires
        _add_once(pkgs, seen, pkg)
    targets = metadata.get("targets", [])
    if not isinstance(targets, list) or not all(isinstance(t, dict) for t in targets):
        raise SourceError(f"{path.name}: `metadata.targets` isn't a list of tables")
    for t in targets:
        if not all(isinstance(t.get(k, ""), str) for k in ("requires_python", "platform", "implementation")) or (
            not isinstance(t.get("gil_disabled", False), bool)
        ):
            raise SourceError(
                f"{path.name}: a `metadata.targets` entry needs strings for requires_python, platform and "
                "implementation, and a boolean gil_disabled"
            )
    return Source(
        path.name,
        pkgs,
        pdm_targets=targets,
        pdm_groups=[groups[(pkg.key, pkg.version)] for pkg in pkgs],
        pdm_locked_groups=(
            [canonicalize_name(g) for g in locked_groups if isinstance(g, str)]
            if isinstance(locked_groups, list)
            else None
        ),
        pdm_inherit_metadata=inherit_metadata,
        pdm_pyproject=path.parent / "pyproject.toml",
    )


def parse_pipfile_lock(path: Path) -> Source:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SourceError(f"{path.name}: not a Pipfile.lock (expected a JSON object)")
    pkgs: list[Package] = []
    seen: dict[tuple[str, str | None], Package] = {}
    for key, val in data.items():
        if key != "_meta" and not isinstance(val, dict):
            raise SourceError(f"{path.name}: {key} isn't a package section")
    sections = [s for s in ("default", "develop") if s in data]
    sections += [key for key in data if key not in ("_meta", "default", "develop")]
    meta = data.get("_meta")
    sources = meta.get("sources") if isinstance(meta, dict) else None
    # Pipenv writes "index": "<source name>" for a package pinned to one of the Pipfile's [[source]] entries.
    source_urls = {
        s["name"]: s["url"]
        for s in (sources if isinstance(sources, list) else [])
        if isinstance(s, dict) and isinstance(s.get("name"), str) and isinstance(s.get("url"), str)
    }
    for section in sections:
        for name, p in data[section].items():
            package_name(path, name)
            if not isinstance(p, dict):
                raise SourceError(f"{path.name}: {section}.{name} isn't a package entry")
            version = p.get("version")
            if version is not None and not isinstance(version, str):
                raise SourceError(f"{path.name}: {section}.{name} has a version that isn't a string")
            pkg = Package(name, version.removeprefix("==") if version else None, origin=path.name)
            if p.get("markers"):
                pkg.markers = [p["markers"]]
            pkg.skip = next((kind for kind in ("git", "hg", "svn", "bzr", "path", "file") if kind in p), None)
            if pkg.skip is None and p.get("editable"):
                pkg.skip = "editable"
            index = p.get("index")
            if pkg.skip is None and index is not None:
                url = source_urls.get(str(index))
                if url is None:  # an index the lock doesn't describe: no way to tell where the name may go
                    pkg.skip = "private index"
                elif not _is_public_pypi(url):
                    pkg.registry = url  # pypi.fill_all skips it, unless WHEELGAP_INDEX_URL is that index
            _add_once(pkgs, seen, pkg)
    # A package with no "index" comes from the Pipfile's default source, which may be private: only a lock whose
    # sources are all PyPI says where every package came from.
    urls = [s.get("url") if isinstance(s, dict) else None for s in (sources if isinstance(sources, list) else [])]
    only_pypi = bool(urls) and all(_is_public_pypi(u) for u in urls) and not _nearby_config_names_an_index(path)
    return Source(path.name, pkgs, pypi_only=only_pypi)


def _is_public_pypi(url: str | None) -> bool:
    try:
        return url is not None and urlparse(url).hostname in ("pypi.org", "pypi.python.org")
    except ValueError:  # e.g. "https://[bad/simple": not something we can call PyPI
        return False


def _pinned_version(req: Requirement) -> str | None:
    specs = list(req.specifier)
    if len(specs) == 1 and specs[0].operator in ("==", "===") and "*" not in specs[0].version:
        return specs[0].version
    return None


# uv settings (in [tool.uv], [tool.uv.pip] or uv.toml) that point it at an index or a folder of distributions.
_UV_INDEX_KEYS = ("index", "sources", "index-url", "extra-index-url", "find-links", "no-index", "default-index")


def _table(data: object, name: str) -> dict:
    value = data.get(name) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def _uv_names_an_index(uv: dict) -> bool:
    return any(k in uv for k in _UV_INDEX_KEYS) or any(k in _table(uv, "pip") for k in _UV_INDEX_KEYS)


def _names_an_index(pyproject: dict) -> bool:
    """Whether a pyproject.toml configures an index or source for an installer (uv, Poetry, PDM, pixi's PyPI options),
    so its dependencies may come from somewhere other than PyPI."""
    tool = _table(pyproject, "tool")
    pixi = _table(_table(tool, "pixi"), "pypi-options")
    return (
        _uv_names_an_index(_table(tool, "uv"))
        or "source" in _table(tool, "poetry")
        or "source" in _table(tool, "pdm")
        or any(k in pixi for k in ("index-url", "extra-index-urls", "find-links"))
    )


def _pdm_names_an_index(pdm: dict) -> bool:
    """pdm.toml: [pypi] (url, or a named [pypi.<name>] index) or a source."""
    return "pypi" in pdm or "source" in pdm


# Project config files an installer reads from the input's folder or a folder above it, and how to tell whether one
# points it at an index.
_CONFIG_FILES = {"uv.toml": _uv_names_an_index, "pyproject.toml": _names_an_index, "pdm.toml": _pdm_names_an_index}


def _nearby_config_names_an_index(path: Path) -> bool:
    """Whether a uv.toml, pyproject.toml or pdm.toml in the input's folder, or a folder above it up to the
    repository root (the first with a .git), configures an index: uv applies [tool.uv] to `uv pip install -r` and
    looks in parent folders, and `uv pip compile` writes no index lines. One that can't be read counts as naming
    one. User and system settings (pip.conf, ~/.config/uv/uv.toml, /etc/uv/uv.toml, .netrc) aren't read."""
    folder = path.resolve().parent
    for directory in (folder, *folder.parents):
        for name, names_an_index in _CONFIG_FILES.items():
            config = directory / name
            if not config.is_file():
                continue
            try:
                if names_an_index(tomllib.loads(config.read_text(encoding="utf-8-sig"))):
                    return True
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                return True
        if (directory / ".git").exists():
            break
    return False


def parse_pyproject(path: Path) -> Source:
    """Read [project] dependencies and record available PEP 735 groups."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SourceError(f"{path}: invalid TOML: {e}") from None
    return _project_source(data, path)


# Canonical expression from the inline script metadata specification (PEP 723):
# https://packaging.python.org/en/latest/specifications/inline-script-metadata/
_SCRIPT_BLOCK = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")


def _script_metadata(path: Path) -> dict:
    """The TOML of a script's one PEP 723 `# /// script` block; never execute the Python file."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise SourceError(f"{path}: isn't UTF-8, so its # /// script block can't be read") from None
    # As the specification reads it: every block, of any type, then the `script` ones.
    blocks = [m for m in _SCRIPT_BLOCK.finditer(text) if m.group("type") == "script"]
    if not blocks:
        raise SourceError(f"{path}: no # /// script block, so there are no dependencies to read")
    if len(blocks) > 1:
        raise SourceError(f"{path}: more than one # /// script block")
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:] for line in blocks[0].group("content").splitlines(keepends=True)
    )
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as e:
        raise SourceError(f"{path}: invalid TOML in the # /// script block: {e}") from None
    return data


def parse_script(path: Path) -> Source:
    """Read a script's one PEP 723 `# /// script` block; never execute the Python file."""
    data = _script_metadata(path)
    return _project_source(
        {
            "project": {key: data[key] for key in ("dependencies", "requires-python") if key in data},
            "tool": data.get("tool", {}),
        },
        path,
        script=True,
    )


_EXCLUDE_NEWER_KEYS = ("exclude-newer", "exclude-newer-package")


def _cutoff_keys(settings: object) -> list[str]:
    return [key for key in _EXCLUDE_NEWER_KEYS if isinstance(settings, dict) and key in settings]


def _settings_beside(pyproject: Path, data: dict) -> tuple[dict, Path]:
    """The uv settings uv reads for a pyproject.toml's folder: a uv.toml beside it, in place of [tool.uv], else its
    [tool.uv] table. With the file they came from."""
    config = pyproject.with_name("uv.toml")
    if config.is_file():
        try:
            return tomllib.loads(config.read_text(encoding="utf-8-sig")), config
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise SourceError(f"{config}: can't be read ({e}); uv reads its settings in place of [tool.uv]") from None
    return _table(_table(data, "tool"), "uv"), pyproject


def _glob_matches(project: Path, root: Path, globs: object, whole_parts: bool) -> bool | None:
    """Whether a pattern in a [tool.uv.workspace] members list (`whole_parts`: a `*` stays within one folder name)
    or exclude list matches `project`, as uv's glob crate does. None when none does but one wheelgap can't match the
    way uv does (with `**` or `..`), or the list isn't a list of strings, might: a guess could be uv's other answer."""
    if not isinstance(globs, list) or not all(isinstance(glob, str) for glob in globs):
        return None
    unsure = False
    for glob in globs:
        pattern = Path(os.path.normpath(glob))  # uv normalizes the pattern (`./`, `a/../b`) first too
        if not pattern.parts or any(part == ".." or "**" in part for part in pattern.parts):
            unsure = True
            continue
        target = project if pattern.is_absolute() else project.relative_to(root)
        if whole_parts:
            if len(target.parts) == len(pattern.parts) and all(map(fnmatchcase, target.parts, pattern.parts)):
                return True
        elif fnmatchcase(target.as_posix(), pattern.as_posix()):
            return True
    return None if unsure else False


def _workspace_root(path: Path, data: dict) -> tuple[Path, dict, bool] | None:
    """The workspace root of a pyproject.toml that's a uv workspace member, whose settings uv reads in place of the
    member's own (uv loads settings from `workspace.install_path()`) (#521). As uv finds it: the first pyproject.toml
    in a folder above decides, a member if its [tool.uv.workspace] members include this project and its exclude
    doesn't; up to the repository root (the first folder with a .git). Returns the root's pyproject.toml, its data,
    and whether the membership is certain, or None for a project that isn't a member."""
    uv = _table(_table(data, "tool"), "uv")
    if not isinstance(data.get("project"), dict) or "workspace" in uv or uv.get("managed") is False:
        return None  # a workspace root itself, or a file uv doesn't treat as a workspace project
    project = Path(os.path.normpath(path.absolute())).parent
    for directory in project.parents:
        if (root := directory / "pyproject.toml").is_file():
            try:
                root_data = tomllib.loads(root.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                return None  # uv can't find a workspace here either: it stops with an error
            workspace = _table(_table(root_data, "tool"), "uv").get("workspace")
            if workspace is None:
                return None  # a folder of another project, such as its examples: uv ignores it
            if not isinstance(workspace, dict):
                return root, root_data, False
            included = _glob_matches(project, directory, workspace.get("members", []), whole_parts=True)
            excluded = _glob_matches(project, directory, workspace.get("exclude", []), whole_parts=False)
            if included is False or excluded is True:
                return None
            return root, root_data, bool(included and excluded is False)
        if (directory / ".git").exists():
            break
    return None


def _read_exclude_newer(source: Source, data: dict, path: Path, script: bool) -> None:
    """uv's exclude-newer and exclude-newer-package for this input (#521): from a script's own [tool.uv], or for a
    pyproject.toml from a uv.toml beside it when there is one (uv then ignores [tool.uv]), else its [tool.uv]. For a
    workspace member, uv reads the workspace root's settings in the same way and ignores the member's. A cutoff is a
    timestamp, a date or a duration counted back from now (cutoff.parse_cutoff); anything else is refused. (uv
    0.13.1 itself warns about a value it can't read and then ignores the file's settings; wheelgap stops, since the
    project meant to set a cutoff.)"""
    workspace = None if script else _workspace_root(path, data)
    if script:
        settings, config = _table(_table(data, "tool"), "uv"), path
    elif workspace is None:
        settings, config = _settings_beside(path, data)
    else:
        root, root_data, certain = workspace
        settings, config = _settings_beside(root, root_data)
        # The member's own settings, which uv doesn't read (so one it couldn't read either isn't an error here).
        own_config, own = _uv_config(path.parent) or (path, {})
        if not certain:
            if _cutoff_keys(own) or _cutoff_keys(settings):
                raise SourceError(
                    f"{path}: wheelgap can't tell whether this is a member of the workspace at {root} (its "
                    "[tool.uv.workspace] members or exclude use patterns wheelgap doesn't match as uv does), "
                    "and uv takes a member's exclude-newer from the workspace root's settings, not its own"
                )
            return
        if keys := _cutoff_keys(own):
            source.exclude_newer_unread = (
                f"{own_config} sets {' and '.join(keys)}, which uv ignores in a workspace member: it uses the "
                f"workspace root's settings ({config}), and so does wheelgap"
            )
    where = f"{config}:" if config.name == "uv.toml" else f"{config}: [tool.uv]"
    now = cutoffs.current_time()  # one time for every duration in the file

    def read(setting: str, value: object, or_false: str = "") -> datetime:
        try:
            cutoff = parse_cutoff(value, now)
        except ValueError as e:
            table = " (wheelgap doesn't read uv's table form)" if isinstance(value, dict) else ""
            raise SourceError(
                f"{where} {setting} must be a date, an RFC 3339 timestamp{or_false or ' or'} a duration: {e}{table}"
            ) from None
        if is_duration(value):
            source.exclude_newer_durations.append(f'{setting} = "{value}" is {describe(cutoff)}')
        return cutoff

    # `exclude-newer = false` turns off a cutoff from settings uv reads before these (the user's own): none here.
    if settings.get("exclude-newer", False) is not False:
        source.exclude_newer = read("exclude-newer", settings["exclude-newer"], ", false or")
    per_package = settings.get("exclude-newer-package", {})
    if not isinstance(per_package, dict):
        raise SourceError(f"{where} exclude-newer-package must be a table of package names")
    for name, value in per_package.items():
        key = canonicalize_name(name)
        if key in source.exclude_newer_package:
            raise SourceError(f"{where} exclude-newer-package names {key!r} twice once normalized")
        source.exclude_newer_package[key] = (
            None if value is False else read(f"exclude-newer-package {name}", value, ", false or")
        )
    if workspace is None:  # a workspace root always has settings: uv looks no further up
        source.exclude_newer_unread = _unread_exclude_newer(path, script)


def _unread_exclude_newer(path: Path, script: bool) -> str | None:
    """A uv.toml or pyproject.toml [tool.uv] in a folder above that uv may take exclude-newer from, which wheelgap
    doesn't read: for a script, the nearest one from its folder up; for a pyproject.toml outside a workspace, the
    nearest one above its folder when it has no uv.toml or [tool.uv] of its own (uv reads the nearest settings
    only). Up to the repository root (the first with a .git)."""
    folder = path.resolve().parent
    for directory in (folder, *folder.parents):
        if (found := _uv_config(directory)) is not None:
            if not script and directory == folder:
                return None  # the pyproject.toml's own settings, or a uv.toml beside it: read already
            config, uv = found
            if keys := _cutoff_keys(uv):
                return f"{config} sets {' and '.join(keys)}, " + (
                    "which uv may apply here too; wheelgap reads only the script's own block"
                    if script
                    else "which uv applies here, as this pyproject.toml has no uv settings of its own; wheelgap "
                    "doesn't read settings from a folder above"
                )
            return None  # uv reads the nearest settings only
        if (directory / ".git").exists():
            break
    return None


def _builds_a_package(data: dict, path: Path) -> bool:
    """Whether a pyproject.toml with no [project] table still describes a buildable project: a [build-system]
    table, a setup.py or setup.cfg beside it, [tool.flit.metadata], or PDM-legacy [tool.pdm] dependencies."""
    if "build-system" in data or (path.parent / "setup.py").exists() or (path.parent / "setup.cfg").exists():
        return True
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return False
    flit = tool.get("flit")
    pdm = tool.get("pdm")
    return (isinstance(flit, dict) and "metadata" in flit) or (isinstance(pdm, dict) and "dependencies" in pdm)


def _has_poetry_dependencies(data: dict) -> bool:
    """Whether [tool.poetry.dependencies] names anything but python."""
    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    poetry_deps = poetry.get("dependencies") if isinstance(poetry, dict) else None
    return isinstance(poetry_deps, dict) and any(dep.lower() != "python" for dep in poetry_deps)


def _project_source(data: dict, path: Path, script: bool = False) -> Source:
    """Shared requirement and provenance handling for project and script metadata (`script`: a # /// script block,
    which errors name instead of [project])."""
    groups_only = "project" not in data and isinstance(data.get("dependency-groups"), dict)
    project = {} if groups_only else data.get("project")
    if not isinstance(project, dict):
        raise SourceError(f"{path}: no [project] table, so there are no dependencies to read")
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list):
        raise SourceError(f"{path}: [project] dynamic must be a list of strings")
    if "dependencies" in dynamic:
        raise SourceError(
            f"{path}: [project] dependencies is dynamic (set by the build backend), so wheelgap can't read it; "
            "check a lockfile instead"
        )
    if not script and not project.get("dependencies") and not project.get("optional-dependencies"):
        # poetry-core 2 builds [tool.poetry.dependencies] into the package whenever [project] lists no dependencies
        # or optional-dependencies (missing or empty: DependencyGroup.dependencies' "legacy mode"). A groups-only file
        # with them is a Poetry project too: --group would leave its dependencies out.
        if _has_poetry_dependencies(data):
            raise SourceError(
                f"{path}: the dependencies are in [tool.poetry.dependencies], not [project] dependencies, "
                "so wheelgap can't read them; check poetry.lock instead"
            )
    if groups_only and _builds_a_package(data, path):
        # A setuptools, flit or PDM-legacy project keeps its dependencies outside [project]: checking only a group
        # would leave them out, and a group entry like `mypkg[test]` would look up an unrelated PyPI package.
        raise SourceError(
            f"{path}: no [project] table; the project's dependencies are in setup.py/setup.cfg or its build "
            "backend's settings, which wheelgap doesn't read; check a lockfile instead"
        )
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
        where = "the # /// script block's dependencies" if script else "[project] dependencies"
        raise SourceError(f"{path}: {where} must be a list of strings")
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict) or not all(
        isinstance(v, list) and all(isinstance(d, str) for d in v) for v in optional.values()
    ):
        raise SourceError(f"{path}: [project] optional-dependencies must be a table of lists of strings")
    extras: dict[str, list[str]] = {}
    for extra_name, lines in optional.items():
        key = canonicalize_name(extra_name)
        if key in extras:
            raise SourceError(
                f"{path}: [project] optional-dependencies has two extras that are both {key!r} once normalized "
                "(PEP 685)"
            )
        extras[key] = list(lines)
    groups = data.get("dependency-groups", {})
    if not isinstance(groups, dict):
        raise SourceError(f"{path}: [dependency-groups] must be a table")
    normalized_groups: dict[str, object] = {}
    for name, entries in groups.items():
        key = canonicalize_name(name)
        if key in normalized_groups:
            raise SourceError(f"{path}: [dependency-groups] has two groups that are both {key!r} once normalized")
        normalized_groups[key] = entries
    source = Source(
        path.name,
        pyproject_extras=extras,
        pyproject_groups=normalized_groups,
        pyproject_groups_only=groups_only,
        pyproject_dynamic_extras="optional-dependencies" in dynamic,
        pypi_only=not (_names_an_index(data) or _nearby_config_names_an_index(path)),
    )
    _read_exclude_newer(source, data, path, script)
    source.uv_rules = _uv_rules(data, path, script)
    if settings := source.uv_rules.policy.settings_on():
        source.deps_blockers.append(f"{settings[0]}, which uv applies to the dependencies it resolves too")
    if isinstance(requires_python := project.get("requires-python"), str):
        source.requires_python = requires_python
    if isinstance(name := project.get("name"), str):
        source.pyproject_name = canonicalize_name(name)
    self_extras: list[str] = []
    for line in dependencies:
        pkg = _project_package(source, line)
        if pkg.key != source.pyproject_name:
            source.packages.append(pkg)
            source.specs.append(line)
            continue
        # `myproject[extra]` in its own dependencies: installing the project installs that extra too.
        self_extras.extend(_self_reference_extras(source, line, Requirement(line)))
    _add_extras(source, self_extras)
    return source


def select_pyproject_extras(source: Source, extras: Iterable[str], all_extras: bool) -> None:
    """Add the requirements of the chosen [project] optional-dependencies groups to a pyproject.toml source.
    A self-reference such as `myproject[test]` inside an extra pulls in that extra, as pip does."""
    available = source.pyproject_extras
    if available is None:
        return
    wanted: list[str] = list(available) if all_extras else [canonicalize_name(e) for e in extras]
    if source.pyproject_dynamic_extras and (all_extras or wanted):
        raise SourceError(
            f"{source.label}: [project] optional-dependencies is dynamic (set by the build backend), "
            "so its extras can't be read"
        )
    if unknown := sorted(set(wanted) - set(available)):
        raise SourceError(f"{source.label}: unknown extras: {', '.join(unknown)}")
    _add_extras(source, wanted)


def select_pyproject_groups(source: Source, groups: Iterable[str], only_groups: Iterable[str]) -> None:
    """Add explicitly selected PEP 735 groups, expanding include-group entries without cycles. With
    --only-group the project's own dependencies are left out, unless a selected group requires the project
    itself (`myproject` or `myproject[extra]`), which installs them."""
    only = [canonicalize_name(name) for name in only_groups]
    wanted = [canonicalize_name(name) for name in groups] + only
    if unknown := sorted(set(wanted) - set(source.pyproject_groups)):
        raise SourceError(f"{source.label}: unknown groups: {', '.join(unknown)}")
    lines: list[str] = []
    visited: set[str] = set()

    def expand(name: str, active: set[str]) -> None:
        if name in active:
            raise SourceError(f"{source.label}: dependency group cycle involving {name!r}")
        if name in visited:
            return
        entries = source.pyproject_groups[name]
        if not isinstance(entries, list):
            raise SourceError(f"{source.label}: dependency group {name!r} must be a list")
        for entry in entries:
            if isinstance(entry, str):
                lines.append(entry)
            elif (
                isinstance(entry, dict) and set(entry) == {"include-group"} and isinstance(entry["include-group"], str)
            ):
                included = canonicalize_name(entry["include-group"])
                if included not in source.pyproject_groups:
                    raise SourceError(
                        f"{source.label}: dependency group {name!r} includes unknown group {entry['include-group']!r}"
                    )
                expand(included, active | {name})
            else:
                raise SourceError(f"{source.label}: invalid dependency group entry in {name!r}: {entry!r}")
        visited.add(name)

    for name in wanted:
        expand(name, set())
    requirements: list[tuple[str, Requirement]] = []
    for line in lines:
        try:
            requirements.append((line, Requirement(line)))
        except InvalidRequirement as e:
            raise SourceError(f"{source.label}: can't parse requirement {line!r}: {e}") from None
    names_project = source.pyproject_name is not None and any(
        canonicalize_name(req.name) == source.pyproject_name for _, req in requirements
    )
    if only and not names_project:
        source.packages.clear()
        source.specs.clear()
    self_extras: list[str] = []
    for line, req in requirements:
        if source.pyproject_name is not None and canonicalize_name(req.name) == source.pyproject_name:
            # Installing the project installs its dependencies, and `myproject[extra]` that extra too.
            self_extras.extend(_self_reference_extras(source, line, req))
            continue
        pkg = _project_package(source, line)
        if line not in source.specs:
            source.specs.append(line)
        if pkg not in source.packages:
            source.packages.append(pkg)
    _add_extras(source, self_extras)


def _self_reference_extras(source: Source, line: str, req: Requirement) -> list[str]:
    """The extras that `myproject[extra]`, a requirement on the project itself, pulls in."""
    if req.marker or req.url or req.specifier:
        raise SourceError(
            f"{source.label}: {line!r} refers to the project itself with a marker, URL or version, "
            "which isn't supported"
        )
    wanted: list[str] = [canonicalize_name(e) for e in req.extras]
    if wanted and source.pyproject_dynamic_extras:
        raise SourceError(
            f"{source.label}: {line!r} names extras, but [project] optional-dependencies is dynamic "
            "(set by the build backend), so its extras can't be read"
        )
    missing = sorted(set(wanted) - set(source.pyproject_extras or {}))
    if missing:
        raise SourceError(f"{source.label}: {line!r} names unknown extras: {', '.join(missing)}")
    return wanted


def _add_extras(source: Source, wanted: list[str]) -> None:
    """Add each wanted extra's requirements, following self-references, and each identical requirement once."""
    available = source.pyproject_extras or {}
    seen: set[str] = set()
    while wanted:
        extra = wanted.pop(0)
        if extra in seen:
            continue
        seen.add(extra)
        for line in available.get(extra, []):
            try:
                req = Requirement(line)
            except InvalidRequirement as e:
                raise SourceError(f"{source.label}: can't parse requirement {line!r}: {e}") from None
            if source.pyproject_name is not None and canonicalize_name(req.name) == source.pyproject_name:
                wanted.extend(_self_reference_extras(source, line, req))
                continue
            pkg = _project_package(source, line)
            if line not in source.specs:
                source.specs.append(line)
            if pkg not in source.packages:  # also in another extra or in dependencies: one row
                source.packages.append(pkg)

    for pkg in source.packages:  # pinned or not: uv leaves out a pinned release's later files too
        pkg.exclude_newer = source.cutoff_for(pkg.key)


def _project_package(source: Source, line: str) -> Package:
    """A pyproject.toml or script requirement, taken from where uv takes it (Source.uv_rules)."""
    pkg = requirement_to_package(line, source.label)
    if source.uv_rules is not None:
        source.uv_rules.apply(pkg)
    return pkg


def requirement_to_package(text: str, origin: str, hashes: frozenset[str] = frozenset()) -> Package:
    try:
        req = Requirement(text)
    except InvalidRequirement as e:
        raise SourceError(f"{origin}: can't parse requirement {text!r}: {e}") from None
    markers = [str(req.marker)] if req.marker else None
    if req.url:
        return Package(req.name, None, skip="url", origin=origin, markers=markers, hashes=hashes)
    version = _pinned_version(req)
    spec = str(req.specifier) if req.specifier and version is None else None
    written = _as_written(text, req.specifier) if spec else None
    return Package(req.name, version, origin=origin, markers=markers, spec=spec, hashes=hashes, written_spec=written)


def _as_written(text: str, specifier: SpecifierSet) -> str:
    """The specifier's clauses in the order `text` writes them, or packaging's order when that can't be told (a clause
    that isn't found exactly once)."""
    flat = re.sub(r"\s+", "", text.split(";", 1)[0])
    found = []
    for clause in specifier:
        hits = [m.start() for m in re.finditer(re.escape(str(clause)) + r"(?=,|\)|$)", flat)]
        if len(hits) != 1:
            return str(specifier)
        found.append((hits[0], str(clause)))
    return ",".join(clause for _, clause in sorted(found))


# "name @ url", "name@url" and "name[extra] @ url": PEP 508 direct references, which Requirement parses.
# A bare URL can't match: its scheme ("git+ssh:") has characters a package name can't.
_NAMED_URL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?\s*@")
_EGG_RE = re.compile(r"[#&]egg=([A-Za-z0-9][A-Za-z0-9._-]*)")
_COMMENT_RE = re.compile(r"\s#")
_FULL_LINE_COMMENT_RE = re.compile(r"^\s*#")
_VIA_COMMENT_RE = re.compile(r"^#\s*via\b")


# pip options that point at an index or a folder of distributions other than PyPI's (or turn PyPI off).
# --pypi-url is pip's other name for --index-url.
_INDEX_OPTIONS = ("--index-url", "--pypi-url", "--extra-index-url", "--find-links", "--no-index")


def _names_index_option(line: str) -> bool:
    """Whether a requirements line has an index option anywhere on it, as pip reads them: `-i URL`, `-iURL`,
    `--index-url=URL`, `-f`, `--find-links`, `--extra-index-url`, `--no-index`, or an abbreviation of a long one
    (optparse accepts unambiguous prefixes)."""
    for token in line.split():
        if token.startswith(("-i", "-f")) and not token.startswith("--"):
            return True
        option = token.split("=", 1)[0]
        if len(option) > 2 and option.startswith("--") and any(name.startswith(option) for name in _INDEX_OPTIONS):
            return True
    return False


def _index_option_names(lines: Iterable[str]) -> list[str]:
    """The index options pip reads on these lines of options, by their long names (`-i URL` is `--index-url`, the
    abbreviation `--extra URL` is `--extra-index-url`), in order and once each. Tokens are read as pip's optparse reads
    them (_REQUIREMENT_LINE_OPTIONS), so a value (`--trusted-host -ifoo`) and an ambiguous abbreviation (`--no`, which
    pip refuses) name none. A requirement line is passed over: pip ignores an index option after a requirement, and
    _requirement_options notes that. So is a line with -r or -c: pip reads the file it names and never applies the
    line's other options (`_parse_and_recurse` doesn't hand the line to `handle_option_line`), so pip 26.3 doesn't
    look in `--find-links wheels -r base.txt`'s folder."""
    names: dict[str, None] = {}
    for line in lines:
        if not line.startswith("-"):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        found: list[str] = []
        include = False
        i = 0
        while i < len(tokens):
            token = tokens[i]
            i += 1
            if token == "--":
                break
            if token.startswith("--"):
                option, eq, _ = token.partition("=")
                matches = (
                    [option]
                    if option in _LONG_LINE_OPTIONS
                    else [n for n in _LONG_LINE_OPTIONS if n.startswith(option)]
                )
                if len(matches) != 1:
                    continue
                name, entry, attached = matches[0], _LONG_LINE_OPTIONS[matches[0]], bool(eq)
            elif token.startswith("-") and token[:2] in _SHORT_LINE_OPTIONS:
                entry = _SHORT_LINE_OPTIONS[token[:2]]
                name, attached = entry[0][1], len(token) > 2  # `-fURL` holds its value
            else:
                continue
            include = include or name in ("--requirement", "--constraint")
            if name in _INDEX_OPTIONS:
                found.append(name)
            if entry[1] and not attached:  # `-i URL`, `--extra-index-url URL`: the next token is the value
                i += 1
        if not include:
            names.update(dict.fromkeys(found))
    return list(names)


def _abbreviated_include(line: str) -> bool:
    """An abbreviation of --requirement or --constraint (`--requirem f`, `--cons f`), which pip reads but wheelgap
    doesn't follow: the file it names may set an index."""
    for token in line.split():
        option = token.split("=", 1)[0]
        if (
            len(option) > 2
            and option not in ("--requirement", "--constraint")
            and any(name.startswith(option) for name in ("--requirement", "--constraint"))
        ):
            return True
    return False


# Line options that take a value, and those that don't, from pip's get_line_parser (SUPPORTED_OPTIONS and
# SUPPORTED_OPTIONS_REQ in pip/_internal/req/req_file.py), less the includes and editables _line_include handles.
_VALUE_OPTIONS = (
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "--no-binary",
    "--only-binary",
    "--trusted-host",
    "--use-feature",
    "--hash",
    "--config-settings",
    "--all-releases",
    "--only-final",
)
_FLAG_OPTIONS = ("--no-index", "--pre", "--prefer-binary", "--require-hashes")


def _takes_value(token: str) -> bool:
    """Whether optparse reads the token after this one as its value: `-i URL`, `--no-binary :all:`, or an
    abbreviation optparse expands to one of these (`--extra URL`)."""
    if token in ("-i", "-f", "-C"):  # -C is short for --config-settings
        return True
    if "=" in token or not token.startswith("--") or len(token) <= 2:
        return False
    if token in _VALUE_OPTIONS or token in _FLAG_OPTIONS:
        return token in _VALUE_OPTIONS
    matches = [name for name in _VALUE_OPTIONS + _FLAG_OPTIONS if name.startswith(token)]
    return len(matches) == 1 and matches[0] in _VALUE_OPTIONS


def _line_include(tokens: list[str], label: str) -> tuple[bool, str] | None:
    """The file pip reads for a line of options, as (whether it's a constraints file, its path). optparse collects
    every -r and -c on the line, and pip follows only the first -r, or the first -c if there's no -r
    (`_parse_and_recurse` in pip/_internal/req/req_file.py). The spellings are `-r f`, `-rf`, `-r=f`,
    `--requirement f` and `--requirement=f` (and the same for -c). `-r=f` is read as `f`, as wheelgap always has, though
    pip's optparse would read a file named `=f`. None for a line pip reads as an editable
    requirement, or one with an abbreviated include, which _abbreviated_include reports instead."""

    def editable(token: str) -> bool:
        option = token.partition("=")[0]
        return (token.startswith("-e") and not token.startswith("--")) or (
            len(option) > 3 and "--editable".startswith(option)
        )

    found: dict[bool, list[str]] = {False: [], True: []}
    unread = ""  # an editable or an abbreviated include, which this function doesn't follow
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token == "--":  # optparse reads no options after it
            break
        option, eq, value = token.partition("=")
        if option in ("--requirement", "--constraint"):
            name, constraint = option, option == "--constraint"
        elif token.startswith(("-r", "-c")) and not token.startswith("--"):
            name, constraint = token[:2], token.startswith("-c")
            eq, value = ("=", token[2:].removeprefix("=")) if len(token) > 2 else ("", "")
        else:
            if editable(token):
                unread = unread or "an editable"
            elif _abbreviated_include(token):
                unread = unread or "an abbreviated -r or -c"
            if (
                _takes_value(token)
                or (editable(token) and not eq and token in ("-e", "--editable"))
                # `--cons -C -r f`: -C is --cons's value, so pip reads the -r
                or (token.startswith("--") and _abbreviated_include(token) and not eq)
            ):
                i += 1  # optparse takes the next token as this option's value: `--no-binary -r f` names no include
            continue
        if not eq:
            value = tokens[i] if i < len(tokens) else ""
            i += 1
        if not value:
            raise SourceError(f"{label}: {name} needs a file name")
        found[constraint].append(value)
    if unread:
        if editable(tokens[0]) or not (found[False] or found[True]):
            return None  # read as an editable, or reported by _abbreviated_include, as before
        # pip's answer depends on options wheelgap doesn't read here, so a guess could leave packages out.
        raise SourceError(f"{label}: wheelgap can't read a -r or -c on a line with {unread}: {' '.join(tokens)}")
    if found[False]:
        return False, found[False][0]
    if found[True]:
        return True, found[True][0]
    return None


class _FormatControl:
    """Matches pip's FormatControl for --no-binary and --only-binary options in requirements files, and the other
    finder options of option lines that change which file or release pip picks: its ReleaseControl (--pre,
    --all-releases, --only-final; pip/_internal/models/release_control.py) and --prefer-binary."""

    def __init__(self) -> None:
        self.no_binary: set[str] = set()
        self.only_binary: set[str] = set()
        self.all_releases: set[str] = set()
        self.only_final: set[str] = set()
        # Where a release option or --prefer-binary was first read (`base.txt:1 sets --pre`), for --deps to refuse and
        # the CLI's note.
        self.release_set: str | None = None
        self.prefer_binary: str | None = None

    def apply_release(self, option: str, value: str) -> None:
        """--all-releases or --only-final, as ReleaseControl.handle_mutual_excludes reads its value: `:all:` clears
        both sets, and a name after it counts only after a `:none:`."""
        target, other = (
            (self.all_releases, self.only_final) if option == "--all-releases" else (self.only_final, self.all_releases)
        )
        new = value.split(",")
        while ":all:" in new:
            other.clear()
            target.clear()
            target.add(":all:")
            del new[: new.index(":all:") + 1]
            if ":none:" not in new:
                return
        for item in new:
            if item == ":none:":
                target.clear()
                continue
            key = canonicalize_name(item)
            other.discard(key)
            target.add(key)

    def allows_prereleases(self, key: str) -> bool | None:
        """ReleaseControl.allows_prereleases: a package named in a set wins over :all:."""
        if key in self.all_releases:
            return True
        if key in self.only_final:
            return False
        if ":all:" in self.all_releases:
            return True
        if ":all:" in self.only_final:
            return False
        return None

    def apply(self, option: str, value: str) -> None:
        if option in ("--all-releases", "--only-final"):
            self.apply_release(option, value)
            return
        target, other = (
            (self.no_binary, self.only_binary) if option == "--no-binary" else (self.only_binary, self.no_binary)
        )
        for item in value.split(","):
            if item == ":all:":
                other.clear()
                target.clear()
                target.add(":all:")
            elif item == ":none:":
                target.clear()
            else:
                key = canonicalize_name(item)
                other.discard(key)
                target.add(key)

    def policy(self, key: str) -> tuple[bool, bool]:
        if key in self.only_binary:
            return (False, True)
        if key in self.no_binary:
            return (True, False)
        if ":all:" in self.only_binary:
            return (False, True)
        if ":all:" in self.no_binary:
            return (True, False)
        return (False, False)


def _numbered_lines(text: str) -> list[tuple[int, str]]:
    """Joins backslash-continued non-comment lines without inserting spaces, matching pip's join_lines. Each comes
    with the number of the line it starts on, which is the one pip names in its errors."""
    joined: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        is_comment = _FULL_LINE_COMMENT_RE.match(line) is not None
        if not pending:
            start = number
        if not line.endswith("\\") or is_comment:
            if is_comment:
                line = " " + line
            if pending:
                pending.append(line)
                joined.append((start, "".join(pending)))
                pending.clear()
            else:
                joined.append((start, line))
        else:
            pending.append(line.strip("\\"))
    if pending:
        joined.append((start, "".join(pending)))
    return joined


def _join_continued_lines(text: str) -> list[str]:
    return [line for _, line in _numbered_lines(text)]


def _format_option(token: str) -> tuple[str, str | None] | None:
    """--no-binary or --only-binary and its =VALUE, if any, from an option token. optparse accepts unambiguous
    prefixes: --no-b on (--no- is also --no-index), and --o on for --only-binary (pip 26's --only-final makes that
    --only-b, but older pips take the shorter ones)."""
    option, eq, value = token.partition("=")
    if option.startswith("--no-b") and "--no-binary".startswith(option):
        name = "--no-binary"
    elif option.startswith("--o") and "--only-binary".startswith(option):
        name = "--only-binary"
    else:
        return None
    return name, value if eq else None


def _apply_format_options(line: str, format_control: _FormatControl) -> bool:
    if not line.startswith("-"):
        return False
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    if not tokens:
        return False
    i = 0
    while i < len(tokens):
        found = _format_option(tokens[i])
        if not found:
            i += 1
            continue
        option, given = found
        if given is not None:
            val = given
            i += 1
        elif i + 1 < len(tokens):
            val = tokens[i + 1]
            i += 2
        else:
            break
        if val:
            format_control.apply(option, val)
    return True


def _apply_release_options(line: str, format_control: _FormatControl, where: str, handled: bool) -> None:
    """The release options and --prefer-binary of a line of options, as pip 26.3 applies them. --all-releases and
    --only-final change the install's settings from their parser callbacks, so they count on any line pip parses.
    --pre and --prefer-binary count only where pip hands the line to handle_option_line (`handled`): not on a line
    with -r or -c, whose file pip reads instead, so `--pre -r base.txt` takes no pre-release. `--pre` adds `:all:` to
    the releases it allows, as handle_option_line does, without clearing --only-final's names."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        return  # _read_requirements has refused the line already
    refused = f"{where}: pip refuses this line:"
    found: list[tuple[str, str | None]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token == "--":
            break
        value: str | None
        if token.startswith("--"):
            option, eq, given = token.partition("=")
            matches = (
                [option] if option in _LONG_LINE_OPTIONS else [n for n in _LONG_LINE_OPTIONS if n.startswith(option)]
            )
            if len(matches) != 1:
                continue
            name, entry, value = matches[0], _LONG_LINE_OPTIONS[matches[0]], given if eq else None
        elif token.startswith("-") and token[:2] in _SHORT_LINE_OPTIONS:
            entry = _SHORT_LINE_OPTIONS[token[:2]]
            name, value = entry[0][1], token[2:] or None
        else:
            continue
        if entry[1] and value is None:
            value = tokens[i] if i < len(tokens) else None
            i += 1
        found.append((name, value))
    # A line with -e is a requirement line to pip (ParsedLine.requirement is the editable), so handle_line never
    # hands it to handle_option_line: pip 26.3 takes no pre-release for `--pre -e ./pkg`.
    handled = handled and all(name != "--editable" for name, _ in found)
    for name, value in found:
        if name in ("--all-releases", "--only-final"):
            if value is None:
                raise SourceError(f"{refused} {name} option requires 1 argument")
            if value.startswith("-"):
                raise SourceError(f"{refused} --all-releases / --only-final option requires 1 argument.")
            format_control.apply_release(name, value)
            format_control.release_set = format_control.release_set or f"{where} sets {name}"
        elif name == "--pre" and handled:
            format_control.all_releases.add(":all:")
            format_control.release_set = format_control.release_set or f"{where} sets --pre"
        elif name == "--prefer-binary" and handled:
            format_control.prefer_binary = format_control.prefer_binary or where


def _is_prerelease(version: str) -> bool:
    try:
        return Version(version).is_prerelease
    except InvalidVersion:  # an === pin of a version PEP 440 can't read: pip's filter keeps it
        return False


def _pin_specifier(version: str) -> str:
    """The specifier of an exact pin: `==`, or `===` for a version PEP 440 can't read (which only `===` can pin)."""
    try:
        Version(version)
    except InvalidVersion:
        return f"==={version}"
    return f"=={version}"


def _specifier(entry: Package) -> str | None:
    """What a requirement or constraint asks of the version, or None for a bare name, which asks nothing."""
    return _pin_specifier(entry.version) if entry.version else entry.spec


def _allows(version: str, specifiers: list[str]) -> bool:
    """Whether pip takes `version` under all these specifiers at once, as it combines a requirement's with its
    constraints': a pre-release counts (a pin names it), and `==1.0` takes `1.0+cpu`, as PEP 440's matching does."""
    return all(SpecifierSet(specifier).contains(version, prereleases=True) for specifier in specifiers)


def _constraint_error(entries: list[Package]) -> str | None:
    """Why these constraints on one package can't be applied as an index version or range, if they can't."""
    if any(entry.skip for entry in entries):
        return "constraint isn't an index version or range"
    # 1.9 and 1.9.0 are one version, and 1.0 with 1.0+cpu is 1.0+cpu (pip installs it): pins conflict only when no
    # one of them matches them all.
    pins = [entry.version for entry in entries if entry.version]
    if pins and not any(_allows(pin, [_pin_specifier(other) for other in pins]) for pin in pins):
        return "constraints specify conflicting exact versions"
    return None


def _constraint_conflict(pkg: Package, entries: list[Package]) -> str | None:
    """Apply a requirement's unmarked constraints to it as pip does, or say why pip refuses them. A pin keeps its
    version, or takes a constraint's local version that matches it (`==1.0` with `==1.0+cpu`); an exact constraint
    becomes the version of an unpinned or ranged requirement; ranges are intersected, and a range that no release
    matches is reported when the index is asked."""
    specifiers = [specifier for entry in entries if (specifier := _specifier(entry))]
    pins = [entry.version for entry in entries if entry.version]
    own = _specifier(pkg)
    if not pkg.version and not pins:
        spec = SpecifierSet(pkg.spec or "")
        for specifier in specifiers:
            spec &= SpecifierSet(specifier)
        pkg.spec = str(spec) or None
        return None
    candidates = ([pkg.version] if pkg.version else []) + pins
    chosen = next((v for v in candidates if _allows(v, specifiers + ([own] if own else []))), None)
    if chosen is not None:
        pkg.version, pkg.spec = chosen, None
        return None
    # Name a constraint that the requirement's pin, or else the constraints' pin, falls outside.
    shown = pkg.version or pins[0]
    if own and not pkg.version and not _allows(shown, [own]):
        blamed = next(entry for entry in entries if entry.version)
        return f"requirement {pkg.name}{own} conflicts with the constraint {blamed.name}{_specifier(blamed)}"
    rejecting = [entry for entry in entries if (s := _specifier(entry)) and not _allows(shown, [s])]
    blamed = next((entry for entry in rejecting if not entry.version), rejecting[0])
    first = f"requirement {pkg.name}{own}" if pkg.version else f"the constraint {pkg.name}=={shown}"
    return f"{first} conflicts with the constraint {blamed.name}{_specifier(blamed)}"


def parse_requirements(
    path: Path, constraint_paths: tuple[Path, ...] = (), line_files: tuple[Path, ...] = ()
) -> Source:
    """`constraint_paths` are constraints files given beside the file (`pip install -r FILE -c CONSTRAINTS`): pip
    reads them before FILE, so they count as a `-c CONSTRAINTS` line at the top of it would.

    `line_files` are all the -r files of that one install (`pip install -r a.txt -r b.txt`), in the order of the line,
    FILE among them. pip reads them into one install with one finder (get_requirements in
    pip/_internal/cli/req_command.py at 26.3), after the constraints files and before it resolves, so the option
    lines of every file (--no-binary, --only-binary, --pre, an index option) and the -c lines inside them count for
    FILE's requirements too, in either order. Only FILE's own requirements are the source's packages."""
    constraints: list[Package] = []
    seen: dict[bool, set[Path]] = {False: set(), True: set()}  # requirement files, constraint files
    index_options: list[str] = []
    format_control = _FormatControl()
    req_lines: list[str] = []
    compiler_signs: list[str] = []
    ignored_options: dict[tuple[Path, str], str] = {}
    pkgs: list[Package] = []
    for constraint_path in constraint_paths:
        # What it returns is the requirements of a -r inside the constraints file (pip's rule): keep them, as
        # _read_requirements does for a -c line.
        pkgs.extend(
            _read_requirements(
                constraint_path,
                seen,
                constraints,
                constraint=True,
                index_options=index_options,
                format_control=format_control,
                req_lines=req_lines,
                compiler_signs=compiler_signs,
                ignored_options=ignored_options,
            )
        )
    own = Path(os.path.abspath(path))
    line_index_options: list[tuple[str, list[str]]] = []
    for file in line_files or (path,):
        if Path(os.path.abspath(file)) == own:
            pkgs.extend(
                _read_requirements(
                    path,
                    seen,
                    constraints,
                    index_options=index_options,
                    format_control=format_control,
                    req_lines=req_lines,
                    compiler_signs=compiler_signs,
                    ignored_options=ignored_options,
                )
            )
        else:
            line_index_options += _read_line_options(file, seen, constraints, format_control)
    shared = [line for _, lines in line_index_options for line in lines]
    source = _finalize_requirements(
        path, pkgs, constraints, index_options + shared, format_control, req_lines, compiler_signs
    )
    source.index_options = _index_option_names(index_options)
    source.line_index_options = [(name, _index_option_names(lines)) for name, lines in line_index_options]
    source.ignored_options = list(ignored_options.values())
    return source


def _read_line_options(
    file: Path, seen: dict[bool, set[Path]], constraints: list[Package], format_control: _FormatControl
) -> list[tuple[str, list[str]]]:
    """Read another -r file of a pip install line for what it sets for the whole install: its options into
    `format_control`, its -c lines into `constraints`, and (returned) its index option lines, as (its name, lines).
    Its requirements are its own source's. It gets its own record of requirement files read, so a file both include
    still lists its packages in each; the constraints files read are shared (`seen`), so none is added twice."""
    index_options: list[str] = []
    _read_requirements(
        file,
        {False: set(), True: seen[True]},
        constraints,
        index_options=index_options,
        format_control=format_control,
    )
    return [(file.name, index_options)] if index_options else []


def parse_named_requirements(label: str, names: list[str], line_files: tuple[Path, ...]) -> Source:
    """Requirements named on a pip install line (`pip install -r a.txt fastlib==2.0.0`), with what the line's -r and -c
    files set for the whole install, as pip reads them with one finder (pip 26.3: --no-binary :all: in a.txt leaves
    fastlib no wheel, in either order): the option lines, the -c lines inside them, and their index options. (A -c
    FILE beside names on the line isn't read: dockerfile_requirements refuses it.)"""
    constraints: list[Package] = []
    seen: dict[bool, set[Path]] = {False: set(), True: set()}
    format_control = _FormatControl()
    line_index_options: list[tuple[str, list[str]]] = []
    for file in line_files:
        line_index_options += _read_line_options(file, seen, constraints, format_control)
    pkgs = [requirement_to_package(name, label) for name in names]
    shared = [line for _, lines in line_index_options for line in lines]
    read = _finalize_requirements(Path(label), pkgs, constraints, shared, format_control, [], [])
    source = Source(label, read.packages, pypi_only=not shared)
    source.specs = list(names)
    source.line_index_options = [(name, _index_option_names(lines)) for name, lines in line_index_options]
    source.format_control = read.format_control
    source.constraint_specs = read.constraint_specs
    source.marker_constraint_specs = read.marker_constraint_specs
    source.constraint_errors = read.constraint_errors
    source.deps_blockers = read.deps_blockers
    return source


def _finalize_requirements(
    path: Path,
    pkgs: list[Package],
    constraints: list[Package],
    index_options: list[str],
    format_control: _FormatControl,
    req_lines: list[str],
    compiler_signs: list[str],
) -> Source:
    by_name: dict[str, list[Package]] = {}
    for entry in constraints:
        by_name.setdefault(entry.key, []).append(entry)
    constraint_specs: dict[str, set[str]] = {}
    marker_constraint_specs: dict[str, list[tuple[str, str]]] = {}
    constraint_errors: dict[str, str] = {}
    for pkg in pkgs:
        if pkg.skip or not (entries := by_name.get(pkg.key)):
            continue
        # --deps applies a marked constraint per target (resolve_deps), so only the unmarked ones can break it here.
        unmarked = [entry for entry in entries if not entry.markers]
        if unmarked_error := _constraint_error(unmarked):
            constraint_errors[pkg.key] = unmarked_error
        # A marked constraint that a pinned requirement already meets changes nothing, true or false; any other
        # depends on the target.
        if any(
            entry.markers
            and not (pkg.version and ((specifier := _specifier(entry)) is None or _allows(pkg.version, [specifier])))
            for entry in entries
        ):
            pkg.error = "constraint has an environment marker; target-dependent constraints aren't supported"
        elif unmarked_error:
            pkg.error = unmarked_error
        elif conflict := _constraint_conflict(pkg, unmarked):
            pkg.error = f"{conflict} (pip refuses this)"
    # A constraint on a package the file doesn't require only ever counts as a transitive dep's specifier,
    # which --deps sees; a constraint the file already called out as broken is left to constraint_errors.
    deps_blockers: list[str] = []
    for entry in constraints:
        # URL/path constraints remain unfollowable; marker constraints apply per target in deps.py.
        if entry.skip:
            deps_blockers.append(f"the constraint on {entry.name} isn't an index release ({entry.skip})")
            continue
        if entry.key in constraint_errors:
            continue
        # A bare name constrains nothing; a version or range does.
        if specifier := f"=={entry.version}" if entry.version else entry.spec:
            if entry.markers:
                marker_constraint_specs.setdefault(entry.key, []).extend(
                    (marker, specifier) for marker in entry.markers
                )
            else:
                constraint_specs.setdefault(entry.key, set()).add(specifier)
    for pkg in pkgs:
        if not pkg.skip:
            pkg.no_binary, pkg.only_binary = format_control.policy(pkg.key)
            pkg.prereleases = format_control.allows_prereleases(pkg.key)
            if pkg.prereleases is False and pkg.version and not pkg.error and _is_prerelease(pkg.version):
                # pip filters the candidates with prereleases=False, which leaves out even the one a pin names: pip
                # 26.3 with `--only-final :all:` and `demo==2.0rc1` says "Could not find a final version".
                pkg.error = (
                    f"pip installs only final releases of {pkg.name} here (--only-final), so it finds no "
                    f"{pkg.version} (pip refuses this)"
                )
    # These change which release pip picks for every dependency too, and the resolver doesn't read them yet.
    if format_control.release_set:
        deps_blockers.append(f"{format_control.release_set}, which changes the releases pip picks for dependencies")
    if format_control.prefer_binary:
        deps_blockers.append(
            f"{format_control.prefer_binary} sets --prefer-binary, which changes the releases pip picks for "
            "dependencies"
        )
    return Source(
        path.name,
        pkgs,
        requirements=True,
        pypi_only=not index_options and not _nearby_config_names_an_index(path),
        index_options=_index_option_names(index_options),
        specs=req_lines,
        compiled="hash" in compiler_signs,  # hash mode makes pip refuse anything unpinned: the file is complete
        constraint_specs=constraint_specs,
        marker_constraint_specs=marker_constraint_specs,
        constraint_errors=constraint_errors,
        format_control=format_control,
        deps_blockers=deps_blockers + [f"{key}: {error}" for key, error in constraint_errors.items()],
    )


def _shown(p: Path, stack: list[Path]) -> str:
    # Names are relative to the folder the run started in, so two files with the same name in different
    # folders stay distinguishable (a file outside that folder is ../name). Forward slashes on every
    # platform, as the include lines themselves are written.
    try:
        return Path(os.path.relpath(p, stack[0].parent)).as_posix()
    except ValueError:  # on another Windows drive: no relative path exists
        return p.name


def _cycle_error(path: Path, line: str, target_path: Path, stack: list[Path]) -> SourceError:
    # The chain names files as they were written, so a cycle through a link names the link, as pip names it.
    return SourceError(
        f"{_shown(path, stack)}: {line} includes a file that is already being read "
        f"({' -> '.join(_shown(p, stack) for p in (*stack, target_path))}); pip refuses this"
    )


def _include_error(path: Path, number: int | None, line: str, error: OSError, stack: list[Path]) -> SourceError:
    """An included file that can't be read, named by the line that includes it (pip stops here too: "Could not
    open requirements file")."""
    where = _shown(path, stack) + (f":{number}" if number is not None else "")
    why = "file not found" if isinstance(error, FileNotFoundError) else (error.strerror or str(error)).lower()
    return SourceError(f"{where}: {line}: {why}")


# What pip 26.3 does with each option it reads after a requirement on the same line. It parses the rest of the line
# with the parser it uses for option lines (break_args_options and build_parser in pip/_internal/req/req_file.py), so
# an unknown option, an ambiguous abbreviation or a missing value is an error. handle_line then hands the requirement
# only --hash and --config-settings, and never calls handle_option_line for the line, so the index, include and
# finder options are dropped ("ignored"). Some still take effect, because their parser callback changes the install's
# own settings as the line is parsed: --no-binary and --only-binary ("format", which wheelgap reads) and
# --all-releases and --only-final ("release", which wheelgap reads too), for the whole install. -e makes the line's
# requirement an editable one, which pip refuses for a name. --use-feature turns the feature on for the run (its
# parser list is shared), and no feature changes which file pip picks. Each entry: spellings, takes a value, kind.
_REQUIREMENT_LINE_OPTIONS: tuple[tuple[tuple[str, ...], bool, str], ...] = (
    (("-i", "--index-url", "--pypi-url"), True, "ignored"),
    (("--extra-index-url",), True, "ignored"),
    (("--no-index",), False, "ignored"),
    (("-c", "--constraint"), True, "ignored"),
    (("-r", "--requirement"), True, "ignored"),
    (("-e", "--editable"), True, "editable"),
    (("-f", "--find-links"), True, "ignored"),
    (("--no-binary",), True, "format"),
    (("--only-binary",), True, "format"),
    (("--prefer-binary",), False, "ignored"),
    (("--require-hashes",), False, "ignored"),
    (("--no-require-hashes",), False, "ignored"),
    (("--pre",), False, "ignored"),
    (("--all-releases",), True, "release"),
    (("--only-final",), True, "release"),
    (("--trusted-host",), True, "ignored"),
    (("--use-feature",), True, "feature"),
    (("--hash",), True, "hash"),
    (("-C", "--config-settings"), True, "requirement"),
)
_USE_FEATURE_CHOICES = (
    "fast-deps",
    "inprocess-build-deps",
    "venv-isolation",
    "truststore",
    "no-binary-enable-wheel-cache",
    "build-constraint",
)
_LONG_LINE_OPTIONS = {name: entry for entry in _REQUIREMENT_LINE_OPTIONS for name in entry[0] if name.startswith("--")}
_SHORT_LINE_OPTIONS = {name: entry for entry in _REQUIREMENT_LINE_OPTIONS for name in entry[0] if len(name) == 2}


def _requirement_options(
    text: str, where: str
) -> tuple[bool, list[tuple[str, str]], list[tuple[str, str]], frozenset[str]]:
    """Reads the options after a requirement (`name>=1 --no-binary name`) as pip's optparse reads them: long options
    by any unambiguous prefix, `--opt=value`, `-fVALUE`. Returns whether there's a --hash; each --no-binary,
    --only-binary, --all-releases or --only-final as (long name, value) in order, for the install's format and release
    control; what pip ignores there, for a note: each option as (the name it was written with, its long name), and
    words that aren't options, which optparse sets aside, as one entry; and the frozenset of sha256 digests from
    --hash options. SourceError where pip refuses the line, or where it applies an option that wheelgap doesn't read
    (-e)."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        raise SourceError(f"{where}: pip can't split this line (an unclosed quote?): {text}") from None
    refused = f"{where}: pip refuses this line:"
    hashed = False
    formats: list[tuple[str, str]] = []
    ignored: list[tuple[str, str]] = []
    sha256_hashes: set[str] = set()
    k = 0
    while k < len(tokens):
        token = tokens[k]
        k += 1
        if token == "--" or not token.startswith("-") or token == "-":
            # optparse sets these words aside (and every word after --), and pip drops them
            if token != "--" or k < len(tokens):
                ignored.append(("words that aren't options", ""))
            if token == "--":
                break
            continue
        value: str | None
        if token.startswith("--"):
            option, eq, given = token.partition("=")
            matches = (
                [option] if option in _LONG_LINE_OPTIONS else [n for n in _LONG_LINE_OPTIONS if n.startswith(option)]
            )
            if not matches:
                raise SourceError(f"{refused} no such option: {option}")
            if len(matches) > 1:
                raise SourceError(f"{refused} ambiguous option: {option} ({', '.join(sorted(matches))}?)")
            name = matches[0]
            names, takes_value, kind = _LONG_LINE_OPTIONS[name]
            value = given if eq else None
            if value is not None and not takes_value:
                raise SourceError(f"{refused} {name} option does not take a value")
        else:
            name = token[:2]
            if name not in _SHORT_LINE_OPTIONS:
                raise SourceError(f"{refused} no such option: {name}")
            names, takes_value, kind = _SHORT_LINE_OPTIONS[name]
            value = token[2:] or None  # every short option pip reads here takes a value: -fURL
        if takes_value and value is None:
            if k >= len(tokens):
                raise SourceError(f"{refused} {name} option requires 1 argument")
            value = tokens[k]  # optparse takes the next word, even one that starts with "-"
            k += 1
        given_value = value or ""
        # The checks pip's parser callbacks make (the first is a CommandError, the others optparse errors).
        if kind == "format" and given_value.startswith("-"):
            raise SourceError(f"{refused} --no-binary / --only-binary option requires 1 argument.")
        if kind == "release" and given_value.startswith("-"):
            raise SourceError(f"{refused} --all-releases / --only-final option requires 1 argument.")
        if kind == "hash":
            if ":" not in given_value:
                raise SourceError(
                    f"{refused} Arguments to --hash must be a hash name followed by a value, "
                    "like --hash=sha256:abcde..."
                )
            algo, _, digest = given_value.partition(":")
            if algo not in ("sha256", "sha384", "sha512"):
                raise SourceError(f"{refused} Allowed hash algorithms for --hash are sha256, sha384, sha512.")
            if algo == "sha256":
                sha256_hashes.add(digest.lower())
        if kind == "requirement" and "=" not in given_value:
            raise SourceError(f"{refused} Arguments to {name} must be of the form KEY=VAL")
        if kind == "feature" and value not in _USE_FEATURE_CHOICES:
            raise SourceError(f"{refused} option --use-feature: invalid choice: {value!r}")
        if kind == "editable":
            raise SourceError(
                f"{where}: pip reads {name} after a requirement as making that requirement editable, and refuses it "
                'for a name ("is not a valid editable requirement"); put the editable on a line of its own'
            )
        if kind == "ignored":
            ignored.append((name, names[-1]))  # -f and --find-links are one option
        hashed = hashed or kind == "hash"
        if kind in ("format", "release") and given_value:
            # The value pip's callback reads, never a word after another option; the callback applies it to the whole
            # install, exactly as on a line of its own.
            formats.append((name, given_value))
    return hashed, formats, ignored, frozenset(sha256_hashes)


def _read_requirements(
    path: Path,
    seen: dict[bool, set[Path]],
    constraints: list[Package],
    constraint: bool = False,
    index_options: list[str] | None = None,
    format_control: _FormatControl | None = None,
    req_lines: list[str] | None = None,
    compiler_signs: list[str] | None = None,
    stack: list[Path] | None = None,
    raw_lines: list[str] | None = None,
    ignored_options: dict[tuple[Path, str], str] | None = None,
) -> list[Package]:
    """`index_options` collects the lines that may point pip at another index: index options (--index-url,
    --extra-index-url, --find-links, --no-index), includes that aren't read, and abbreviated include options.
    `req_lines` collects requirement lines that aren't constraints, for --deps; `compiler_signs` notes the
    `--hash` options and `# via` comments that mark pip-compile output, in requirement files only.
    `stack` is the chain of files being read, `-r` and `-c` together, as pip keeps it: by the path each
    file was named with. `ignored_options` collects the options pip ignores after a requirement, once per file and
    option, keyed by (resolved file, option), each a `path:line: ...` note.

    A file is read through the folder it was named in (`os.path.abspath`, no symlink resolution), as pip
    reads it, so an include in a symlinked file resolves against the link's folder, not the target's.
    An include is a cycle when that named path is already in `stack`, which is pip's rule: one shared
    file linked into two folders may be on the chain under both names, since its includes are other files
    each time. The "already read" identity is the resolved path instead: a file reached by two names
    lists its own lines once (pip would list them twice), but its includes are followed again, from the
    folder of the new name."""
    path = Path(os.path.abspath(path))
    resolved = path.resolve()
    if stack is None:
        stack = []
    already_seen = resolved in seen[constraint]
    seen[constraint].add(resolved)
    stack.append(path)
    pkgs: list[Package] = []
    try:
        append = constraints.append if constraint else pkgs.append
        # utf-8-sig: a byte order mark (Windows editors) mustn't hide an option on the first line.
        numbered: list[tuple[int | None, str]] = (
            [(None, raw) for raw in raw_lines]
            if raw_lines is not None
            else list(_numbered_lines(path.read_text(encoding="utf-8-sig")))
        )
        for number, raw in numbered:
            if compiler_signs is not None and not constraint and _VIA_COMMENT_RE.match(raw.lstrip()):
                compiler_signs.append("via")  # pip-compile writes "# via <name>" comment lines
            line = _COMMENT_RE.split(raw, 1)[0].strip()  # " # note" or "<TAB># note"
            if not line or line.startswith("#"):
                continue
            tokens = None
            if line.startswith("-"):
                try:
                    tokens = shlex.split(line)  # as pip reads a line of options
                except ValueError:
                    raise SourceError(f"{path.name}: pip can't split this line (an unclosed quote?): {line}") from None
            # Checked on the split tokens too, so a quoted option (`--pre "--req" more.txt`) is seen as pip sees it.
            if (
                not already_seen
                and index_options is not None
                and any(
                    _names_index_option(text) or _abbreviated_include(text)
                    for text in (line, " ".join(tokens or []))
                    if text
                )
            ):
                index_options.append(line)
            include = None
            if tokens is not None:
                include = _line_include(tokens, path.name)
            if include and format_control is not None:
                _apply_format_options(line, format_control)  # pip applies `--no-binary X -r f` before reading f
                where = _shown(path, stack) + (f":{number}" if number is not None else "")
                _apply_release_options(line, format_control, where, handled=False)
            if include and include[0]:
                target = include[1]
                # A URL (-c https://releases.openstack.org/...) or ${VAR} is left to pip: it's skipped, as before.
                # Unread, it may name an index, so the input isn't PyPI-only.
                if "://" not in target and "${" not in target:
                    # The include is read from the including file's folder as named, and that named
                    # path is what the cycle check compares, as pip does.
                    target_path = Path(os.path.abspath(path.parent / target))
                    if target_path in stack:
                        # An include naming a file already being read is an error, as pip refuses it:
                        # the chain is one chain for -r and -c together, so a loop through a
                        # constraints file is refused too.
                        raise _cycle_error(path, line, target_path, stack)
                    # A -r inside the constraints file is requirements, not more constraints (pip's rule),
                    # so what it returns is packages, not constraints: keep them. req_lines and compiler_signs
                    # are passed through for that -r file only: the constraints file's own lines add to neither.
                    try:
                        included = _read_requirements(
                            path.parent / target,
                            seen,
                            constraints,
                            True,
                            index_options,
                            format_control,
                            req_lines,
                            compiler_signs,
                            stack=stack,
                            ignored_options=ignored_options,
                        )
                    except OSError as e:  # the constraints file itself: one it includes is a SourceError by now
                        raise _include_error(path, number, line, e, stack) from e
                    pkgs.extend(included)
                elif not already_seen and index_options is not None:
                    index_options.append(line)
                continue
            if include:  # -r file, -rfile, --requirement file, --requirement=file, after other options too
                target_path = Path(os.path.abspath(path.parent / include[1]))
                if target_path in stack:
                    raise _cycle_error(path, line, target_path, stack)
                try:
                    included = _read_requirements(
                        path.parent / include[1],
                        seen,
                        constraints,
                        False,  # a -r is requirements even inside a constraints file, as pip reads it
                        index_options,
                        format_control,
                        req_lines,
                        compiler_signs,
                        stack,
                        ignored_options=ignored_options,
                    )
                except OSError as e:  # the included file itself: one it includes is a SourceError by now
                    raise _include_error(path, number, line, e, stack) from e
                pkgs.extend(included)
                continue
            if line.startswith("-e ") or line.startswith("--editable"):
                if format_control is not None:
                    # pip's parser callbacks apply `-e ./pkg --all-releases demo` and `-e ./pkg --no-binary demo` as
                    # it reads the line, though the line is a requirement to pip (pip 26.3 ran both).
                    _apply_format_options(line, format_control)
                    where = _shown(path, stack) + (f":{number}" if number is not None else "")
                    _apply_release_options(line, format_control, where, handled=False)
                if not already_seen:
                    name = line.split("#egg=", 1)[1] if "#egg=" in line else line.split(None, 1)[-1]
                    append(Package(name, None, skip="editable", origin=path.name))
                continue
            if line.startswith("-"):
                if format_control is not None:
                    _apply_format_options(line, format_control)
                    where = _shown(path, stack) + (f":{number}" if number is not None else "")
                    _apply_release_options(line, format_control, where, handled=True)
                continue  # pip options: --index-url, --no-binary, --hash continuation, ...
            # The requirement ends where pip ends it: before the first word, split at single spaces, that starts
            # with "-" (break_args_options), so "name\t--no-binary x" is one requirement pip can't parse. The rest is
            # options: --hash=sha256:..., "--hash sha256:...", --no-binary NAMES.
            words = line.split(" ")
            first_option = next((k for k, word in enumerate(words) if word.startswith("-")), len(words))
            options = " ".join(word for word in words[first_option:] if word)
            hashed = False
            req_hashes: frozenset[str] = frozenset()
            if options:
                where = _shown(path, stack) + (f":{number}" if number is not None else "")
                hashed, formats, ignored, req_hashes = _requirement_options(options, where)
                if ignored_options is not None:
                    for shown, option in ignored:
                        note = f"{where}: pip ignores {shown} after a requirement"
                        ignored_options.setdefault((resolved, option), note)
                if format_control is not None:
                    # Each time the line is read, as for an option line: pip reads a file included twice twice,
                    # and the later option wins.
                    for format_option, format_value in formats:
                        format_control.apply(format_option, format_value)
                        if format_option in ("--all-releases", "--only-final"):
                            format_control.release_set = format_control.release_set or f"{where} sets {format_option}"
            if already_seen:
                continue
            # Only on requirement lines: a --hash or "# via" in a constraints file never marked the input compiled.
            if hashed and compiler_signs is not None and not constraint:
                compiler_signs.append("hash")
            line = " ".join(words[:first_option]).strip()
            if line.startswith((".", "/")):
                append(Package(line, None, skip="path", origin=path.name, hashes=req_hashes))
                continue
            if "://" in line and not _NAMED_URL_RE.match(line):
                # A bare URL (git+ssh://git@github.com/org/repo.git#egg=foo): never from the index.
                # "name @ url" lines are left to Requirement, which knows the name and markers.
                egg = _EGG_RE.search(line)
                name = egg.group(1) if egg else line.split(";", 1)[0].strip()
                append(Package(name, None, skip="url", origin=path.name, hashes=req_hashes))
                continue
            append(requirement_to_package(line, path.name, hashes=req_hashes))
            if req_lines is not None and not constraint:
                req_lines.append(line)
    finally:
        stack.remove(path)
    return pkgs


class _YamlError(Exception):
    def __init__(self, number: int, message: str):
        super().__init__(f"line {number}: {message}")


class _FlowList(list[object]):
    """A sequence _BlockYaml read from flow style (`[a, b]`), which a caller can refuse where it wants block style."""


class _BlockYaml:
    """The block-style YAML subset pixi writes (serde_yaml): mappings, sequences (also "indentless" ones under a
    key), plain and quoted one-line scalars, and the empty `[]` and `{}`. Scalars stay strings. Anything else
    (anchors, aliases, tags, block scalars, flow collections, multi-line scalars, comments, tabs) is an error rather
    than a guess at what YAML would make of it.

    `comments=True` (conda environment files, which people write by hand) also reads `#` comments: whole-line ones,
    and from a whitespace-then-`#` to the end of a line (after a closing quote too, but never inside a quoted
    scalar), as YAML does. That's safe here because block and multi-line scalars, where `#` isn't a comment, are
    errors anyway. It also reads a one-line flow sequence of plain scalars (`channels: [conda-forge, defaults]`) as a
    _FlowList, for the caller to refuse where it wants block style."""

    _ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0"}
    # Tabs, control characters (libyaml refuses most of them), line breaks other than \n and \r (which
    # str.splitlines would also split on), a byte order mark after the start, lone surrogates, and the
    # noncharacters U+FFFE and U+FFFF, which libyaml refuses too.
    _BAD_CHAR = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\ufeff\ud800-\udfff\ufffe\uffff]")

    def __init__(self, text: str, comments: bool = False):
        self.comments = comments
        self.lines: list[tuple[int, int, str]] = []  # (line number, indent, content)
        for number, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
            if bad := self._BAD_CHAR.search(raw):
                raise _YamlError(number, f"unsupported character {bad.group()!r} (tabs and control characters)")
            line = raw.rstrip(" ")
            if not line:
                continue
            content = line.lstrip(" ")
            if comments and content.startswith("#"):
                continue
            if content.startswith("#") or (line == content and content.startswith(("---", "..."))):
                raise _YamlError(number, "comments and document markers aren't supported")
            indent = len(line) - len(content)
            if comments:
                content = self._strip_comment(content, number)
            self.lines.append((number, indent, content))
        self.i = 0

    def _strip_comment(self, content: str, number: int) -> str:
        """`content` without a trailing `# comment` (and the spaces before it). A `#` starts a comment only after a
        space and outside a quoted scalar, which starts where a node does: at the start, after `- ` or after `: `."""
        i, node_start = 0, True
        while i < len(content):
            c = content[i]
            if c == " ":
                i += 1
            elif c == "#" and (i == 0 or content[i - 1] == " "):
                return content[:i].rstrip(" ")
            elif node_start and c in "'\"":
                end = self._quoted_end(content[i:], number)
                if end is None:
                    return content  # unclosed: _scalar says so
                i += end
                node_start = False
            elif c in "-:" and content[i + 1 : i + 2] in ("", " ") and (c == ":" or node_start):
                i += 1
                node_start = True
            else:
                i += 1
                node_start = False
        return content

    def read(self) -> object:
        if not self.lines:
            return None
        value = self._node(0)
        if self.i < len(self.lines):
            raise _YamlError(self.lines[self.i][0], "unexpected indentation")
        return value

    def _node(self, indent: int) -> object:
        number, at, content = self.lines[self.i]
        if at != indent:
            raise _YamlError(number, "unexpected indentation")
        return self._sequence(indent) if self._is_item(content) else self._mapping(indent)

    @staticmethod
    def _is_item(content: str) -> bool:
        return content == "-" or content.startswith("- ")

    def _sequence(self, indent: int) -> list:
        items: list = []
        while self.i < len(self.lines):
            number, at, content = self.lines[self.i]
            if at < indent or (at == indent and not self._is_item(content)):
                break
            if at > indent or content == "-":
                raise _YamlError(number, "unexpected indentation")
            rest = content[2:].lstrip(" ")
            if self._is_item(rest) or self._split_key(rest, number) is not None:
                # "- key: value": a mapping whose first line is this one, at the item's column
                if self._is_item(rest):
                    raise _YamlError(number, "nested sequences aren't supported")
                self.lines[self.i] = (number, at + len(content) - len(rest), rest)
                items.append(self._mapping(at + len(content) - len(rest)))
            else:
                items.append(self._scalar(rest, number))
                self.i += 1
        return items

    def _mapping(self, indent: int) -> dict:
        out: dict = {}
        while self.i < len(self.lines):
            number, at, content = self.lines[self.i]
            if at < indent:
                break
            if at > indent or self._is_item(content):
                raise _YamlError(number, "unexpected indentation")
            split = self._split_key(content, number)
            if split is None:
                raise _YamlError(number, "expected `key: value`")
            key, rest = split
            if key in out:
                raise _YamlError(number, f"duplicate key {key!r}")
            self.i += 1
            if rest:
                out[key] = self._scalar(rest, number)
            elif self.i < len(self.lines) and self.lines[self.i][1] > indent:
                out[key] = self._node(self.lines[self.i][1])
            elif self.i < len(self.lines) and self.lines[self.i][1] == indent and self._is_item(self.lines[self.i][2]):
                out[key] = self._sequence(indent)
            else:
                out[key] = None
        return out

    def _split_key(self, content: str, number: int) -> tuple[str, str] | None:
        """(key, rest of the line) for `key: rest` or `key:`, else None."""
        if content[0] in "'\"":
            end = self._quoted_end(content, number)
            if end is None or not (content[end:] == ":" or content[end:].startswith(": ")):
                return None
            return self._key(content[:end], number), content[end + 1 :].strip(" ")
        if content.endswith(":"):
            key, rest = content[:-1], ""
        elif ": " in content:
            key, rest = content.split(": ", 1)
            rest = rest.strip(" ")
        else:
            return None
        return self._key(key, number), rest

    def _key(self, text: str, number: int) -> str:
        if text != text.strip(" "):
            raise _YamlError(number, f"unsupported key {text!r} (a space before the colon)")
        key = self._scalar(text, number)
        if not isinstance(key, str):
            raise _YamlError(number, f"unsupported key {text!r}")
        return key

    @staticmethod
    def _quoted_end(text: str, number: int) -> int | None:
        """Where the quoted scalar that starts `text` ends (just past its closing quote), or None if it isn't closed
        on this line."""
        quote, i = text[0], 1
        while i < len(text):
            if quote == '"' and text[i] == "\\":
                i += 2
                continue
            if text[i] == quote:
                if quote == "'" and text[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                return i + 1
            i += 1
        return None

    def _scalar(self, text: str, number: int) -> object:
        if not text:
            raise _YamlError(number, "empty key")
        if text in ("[]", "{}"):
            return [] if text == "[]" else {}
        if self.comments and text[0] == "[" and text[-1] == "]":
            return self._flow_sequence(text, number)
        if text[0] in "'\"":
            if self._quoted_end(text, number) != len(text):
                raise _YamlError(number, "a quoted string must end on its own line")
            if text[0] == "'":
                return text[1:-1].replace("''", "'")
            return self._unescape(text[1:-1], number)
        if (
            text[0] in "[]{}&*!|>%@`#?,:"
            or text == "-"
            or text.startswith("- ")
            or " #" in text
            or ": " in text
            or text[-1] == ":"
        ):
            raise _YamlError(number, f"unsupported YAML: {text[:40]!r}")
        return text

    def _flow_sequence(self, text: str, number: int) -> _FlowList:
        """A one-line flow sequence of plain scalars, like `[conda-forge, defaults]`; anything richer is an error."""
        items = [item.strip(" ") for item in text[1:-1].split(",")]
        if any(not item or item[0] in "'\"" or any(c in item for c in "[]{}") for item in items):
            raise _YamlError(number, f"unsupported YAML: {text[:40]!r} (only a flow sequence of plain scalars)")
        return _FlowList(self._scalar(item, number) for item in items)

    def _unescape(self, text: str, number: int) -> str:
        out, i = [], 0
        while i < len(text):
            c = text[i]
            if c != "\\":
                out.append(c)
                i += 1
                continue
            code = text[i + 1 : i + 2]
            width = {"x": 2, "u": 4, "U": 8}.get(code)
            if code in self._ESCAPES:
                out.append(self._ESCAPES[code])
                i += 2
            elif (
                width and re.fullmatch(r"[0-9A-Fa-f]+", digits := text[i + 2 : i + 2 + width]) and len(digits) == width
            ):
                point = int(digits, 16)
                if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
                    raise _YamlError(number, f"escape \\{code}{digits} isn't a Unicode character")
                out.append(chr(point))
                i += 2 + width
            else:
                raise _YamlError(number, f"unsupported escape \\{code}")
        return "".join(out)


# pixi.lock versions this reader knows (rattler_lock's FileFormatVersion, crates/rattler_lock/src/file_format_version.rs
# in conda/rattler): 6, and 7, which pixi writes since 0.68. A newer pixi rewrites an older lock when it locks again;
# a newer version may change what a field means, so it's refused rather than guessed at.
PIXI_LOCK_VERSIONS = ("6", "7")
# conda subdir -> (sys_platform, platform_machine) of the Python that runs there. 32-bit x86 and Arm are left out:
# their platform_machine is the machine's (see targets._UNKNOWN_MACHINE_ARCHES), so no target matches them for sure.
_CONDA_MARKERS = {
    "linux-64": ("linux", "x86_64"),
    "linux-aarch64": ("linux", "aarch64"),
    "linux-ppc64le": ("linux", "ppc64le"),
    "linux-s390x": ("linux", "s390x"),
    "linux-riscv64": ("linux", "riscv64"),
    "osx-64": ("darwin", "x86_64"),
    "osx-arm64": ("darwin", "arm64"),
    "win-64": ("win32", "AMD64"),
    "win-arm64": ("win32", "ARM64"),
}
_PIXI_ENV_KEYS = {"channels", "indexes", "find-links", "options", "packages"}
_PIXI_PYPI_KEYS = {
    "6": {"pypi", "name", "version", "sha256", "md5", "requires_dist", "requires_python", "editable"},
    "7": {"pypi", "name", "version", "sha256", "md5", "index", "requires_dist", "requires_python"}
    | {"build_packages", "host_packages"},
}
_CONDA_ARCHIVE_RE = re.compile(r"^(.+)-([^-]+)-([^-]+)\.(?:conda|tar\.bz2)$")


def _pixi_conda_name(location: str, selector: dict, entry: dict | None) -> tuple[str, str | None]:
    """A conda package's name and version, from what the lock records or its archive's filename."""
    for record in (selector, entry or {}):
        if isinstance(record.get("name"), str):
            version = record.get("version")
            return record["name"], version if isinstance(version, str) else None
    if m := _CONDA_ARCHIVE_RE.match(unquote(Path(urlparse(location).path).name)):
        return m.group(1), m.group(2)
    return location.split("[", 1)[0].split(" @ ", 1)[0].strip() or location, None


def _pixi_pypi_package(entry: dict, indexes: list[str], where: str) -> tuple[Package, tuple[str, str]]:
    """A pixi.lock `pypi:` entry as a Package, and the key that merges it with the same package on other platforms."""
    location, name = entry["pypi"], package_name(Path(where), entry.get("name"))
    pkg = Package(name, entry.get("version"), origin=where)
    scheme = m.group(1).lower() if (m := re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", location)) else None
    if scheme in ("http", "https"):
        url = urlparse(location)
        filename = unquote(Path(url.path).name)
        index = entry.get("index")
        if isinstance(index, str):
            pkg.registry = index  # v7 records the index for a package that didn't come from PyPI
        elif url.hostname == "files.pythonhosted.org":
            pass  # PyPI's own file host: the release is on PyPI
        elif all(i.rstrip("/") == "https://pypi.org/simple" for i in indexes):
            pkg.skip = "url"  # PyPI serves files only from files.pythonhosted.org, so this is a direct URL
        else:
            pkg.skip = "private index"  # another index, or a direct URL: the lock doesn't say which
        if not pkg.skip:
            if pkg.version is None:
                raise SourceError(f"{where}: {name} comes from an index but has no version")
            pkg.files, pkg.partial_files = [filename], True
            return pkg, (pkg.key, pkg.version)
    elif scheme is None or scheme == "file":
        pkg.skip = "editable" if entry.get("editable") == "true" else "path"
    elif "+" in scheme:
        pkg.skip = scheme.split("+", 1)[0]  # git+https://..., hg+..., svn+...
    else:
        raise SourceError(f"{where}: {name} has a location wheelgap can't read: {location!r}")
    return pkg, (pkg.key, f"{pkg.skip} {location}")


def parse_pixi_lock(path: Path, environment: str | None = None) -> Source:
    """One environment of a pixi.lock (`default` unless named): its PyPI packages with the files pixi picked for
    each platform, and its conda packages, skipped. A package missing from some of the lock's platforms gets a
    marker that hides it on those platforms (for the environment's Python there) and nowhere else."""
    try:
        text = path.read_text(encoding="utf-8-sig")  # serde_yaml takes a leading byte order mark too
    except UnicodeDecodeError as e:
        raise SourceError(f"{path.name}: not valid UTF-8 (byte {e.start}), so it can't be read for sure") from None
    top = re.search(r"^version:[ ]*(.*?)[ ]*$", text, re.MULTILINE)
    if top is None:
        raise SourceError(f"{path.name}: no top-level `version`, so this isn't a pixi.lock wheelgap can read")
    version = top.group(1).strip("'\"")
    if version not in PIXI_LOCK_VERSIONS:
        raise SourceError(
            f"{path.name}: pixi.lock version {top.group(1)!r} isn't supported (wheelgap reads versions "
            f"{' and '.join(PIXI_LOCK_VERSIONS)}); run `pixi lock` with a pixi that writes one of them, or update "
            "wheelgap"
        )
    try:
        data = _BlockYaml(text).read()
    except _YamlError as e:
        raise SourceError(f"{path.name}: can't read it for sure: {e}") from None

    def bad(what: str) -> SourceError:
        return SourceError(f"{path.name}: can't read it for sure: {what}")

    if not isinstance(data, dict) or data.get("version") != version:
        raise bad("the top level isn't a mapping with that `version`")
    allowed = {"version", "environments", "packages"} | ({"platforms"} if version == "7" else set())
    if unknown := sorted(set(data) - allowed):
        raise bad(f"unknown top-level key {unknown[0]!r} for version {version}")
    subdirs: dict[str, str] = {}  # platform name -> conda subdir
    if version == "7":
        for p in data.get("platforms") or []:
            if not isinstance(p, dict) or not isinstance(p.get("name"), str):
                raise bad("a `platforms` entry has no name")
            subdir = p.get("subdir", p["name"])
            if not isinstance(subdir, str):
                raise bad(f"platform {p['name']!r} has a `subdir` that isn't a string")
            subdirs[p["name"]] = subdir
    environments, entries = data.get("environments"), data.get("packages") or []
    if not isinstance(environments, dict) or not isinstance(entries, list):
        raise bad("`environments` must be a mapping and `packages` a list")
    name = "default" if environment is None else environment
    if not name:
        raise SourceError(f"{path.name}: the environment name is empty")
    env = environments.get(name)
    if env is None:
        names = ", ".join(sorted(environments)) or "none"
        raise SourceError(f"{path.name}: no environment {name!r} (it has: {names}); pick one with --environment")
    if not isinstance(env, dict):
        raise bad(f"environment {name!r} isn't a mapping")
    if unknown := sorted(set(env) - _PIXI_ENV_KEYS):
        raise bad(f"environment {name!r} has an unknown key {unknown[0]!r}")
    indexes = env.get("indexes")
    if indexes is not None and not (isinstance(indexes, list) and all(isinstance(i, str) for i in indexes)):
        raise bad(f"environment {name!r}: `indexes` must be a list of URLs")
    per_platform = env.get("packages") or {}
    if not isinstance(per_platform, dict):
        raise bad(f"environment {name!r}: `packages` must be a mapping of platforms")

    pypi: dict[str, dict] = {}
    conda: dict[str, dict] = {}
    for entry in entries:
        kinds = [k for k in ("conda", "pypi", "conda_source")[: 3 if version == "7" else 2] if k in (entry or {})]
        if not isinstance(entry, dict) or len(kinds) != 1 or not isinstance(entry[kinds[0]], str):
            raise bad("a `packages` entry isn't exactly one of conda, pypi" + (", conda_source" * (version == "7")))
        if kinds[0] != "pypi":
            conda[entry[kinds[0]]] = entry
            continue
        if unknown := sorted(set(entry) - _PIXI_PYPI_KEYS[version]):
            raise bad(f"PyPI package {entry['pypi']!r} has an unknown key {unknown[0]!r}")
        for field_name in ("name", "version", "index", "editable"):
            if field_name in entry and not isinstance(entry[field_name], str):
                raise bad(f"PyPI package {entry['pypi']!r}: `{field_name}` isn't a string")
        if "name" not in entry or (version == "6" and "version" not in entry):
            raise bad(f"PyPI package {entry['pypi']!r} has no name" + (" or version" * (version == "6")))
        if entry["pypi"] in pypi:
            raise bad(f"two PyPI packages at {entry['pypi']!r}")
        pypi[entry["pypi"]] = entry

    pythons: dict[str, str] = {}  # platform -> the environment's Python there ("3.12")
    found: dict[tuple[str, str], tuple[Package, set[str]]] = {}
    skipped: dict[str, Package] = {}  # by name: one row however many builds or versions
    for platform, selectors in per_platform.items():
        if version == "7" and platform not in subdirs:
            raise bad(f"environment {name!r} uses platform {platform!r}, which `platforms` doesn't list")
        if not isinstance(selectors, list):
            raise bad(f"environment {name!r}: {platform} must list packages")
        for s in selectors:
            kinds = [k for k in ("conda", "pypi", "conda_source") if isinstance(s, dict) and k in s]
            if len(kinds) != 1 or not isinstance(s[kinds[0]], str) or (kinds[0] == "conda_source" and version != "7"):
                raise bad(f"environment {name!r}: a {platform} package isn't exactly one of conda, pypi")
            location = s[kinds[0]]
            if kinds[0] != "pypi":
                conda_name, conda_version = _pixi_conda_name(location, s, conda.get(location))
                if canonicalize_name(conda_name) == "python" and conda_version:
                    if m := re.match(r"(\d+)\.(\d+)", conda_version):
                        pythons[platform] = f"{m.group(1)}.{m.group(2)}"
                pkg = Package(conda_name, conda_version, skip="conda", origin=path.name)
                skipped.setdefault(pkg.key, pkg)
                continue
            if len(s) != 1:  # a PyPI selector is its location alone
                raise bad(f"environment {name!r}: {platform} PyPI package {location!r} has other keys")
            entry = pypi.get(location)
            if entry is None:
                raise bad(
                    f"environment {name!r}: {platform} lists PyPI package {location!r}, which isn't in `packages`"
                )
            pkg, key = _pixi_pypi_package(entry, list(indexes or []), path.name)
            if not pkg.skip and indexes is None:
                raise bad(f"environment {name!r} has PyPI packages but no `indexes`")
            if key not in found:
                found[key] = (pkg, set())
            first, platforms = found[key]
            platforms.add(platform)
            if first.files is not None and pkg.files and pkg.files[0] not in first.files:
                first.files.append(pkg.files[0])
            if first.registry != pkg.registry:
                first.skip, first.files = "private index", None  # from two indexes: no single lookup is safe

    # Locked platforms by the markers pip would see there: sys_platform, platform_machine and the environment's
    # Python. Platforms that share them (linux-64 and a linux-64-cuda with subdir linux-64, say) are one group.
    groups: dict[tuple[str, str, str], set[str]] = {}
    # Platforms whose Python isn't known (no conda `python` package, or one built from source), by their markers:
    # one of them may be the same Python as a group, so a package it installs is never hidden on that machine.
    unknown_python: dict[tuple[str, str], set[str]] = {}
    for platform in per_platform:
        machine = _CONDA_MARKERS.get(subdirs[platform] if platform in subdirs else platform)
        if machine and platform in pythons:
            groups.setdefault((*machine, pythons[platform]), set()).add(platform)
        elif machine:
            unknown_python.setdefault(machine, set()).add(platform)
    # A package missing from every platform of a group isn't installed for those markers. Hide it on exactly them;
    # every other target, such as another Python or a platform the lock doesn't cover, still counts it.
    for pkg, platforms in found.values():
        clauses = [
            f"(sys_platform != '{sys_platform}' or platform_machine != '{machine}' or python_version != '{python}')"
            for (sys_platform, machine, python), members in groups.items()
            if not members & platforms and not unknown_python.get((sys_platform, machine), set()) & platforms
        ]
        pkg.markers = [" and ".join(clauses)] if clauses else None
    pkgs = [pkg for pkg, _ in found.values()]
    return Source(path.name, pkgs + sorted(skipped.values(), key=lambda p: p.key))


def parse_pip_report(path: Path) -> Source:
    """pip install --dry-run --report JSON: pinned installation results, not a cross-platform lockfile."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except RecursionError as e:  # nested too deeply for the json module
        raise SourceError(f"{path.name}: invalid pip installation report: nested too deeply") from e
    except (OSError, ValueError) as e:
        raise SourceError(f"{path.name}: invalid pip installation report: {e}") from e
    if not isinstance(data, dict) or "version" not in data or "install" not in data:
        raise SourceError(
            f"{path.name}: isn't a pip installation report (no `version` and `install` keys); "
            "make one with `pip install --dry-run --report report.json`"
        )
    if data["version"] != "1":
        raise SourceError(
            f"{path.name}: pip installation report version {data['version']!r} isn't supported (only '1')"
        )
    if not isinstance(data["install"], list):
        raise SourceError(f"{path.name}: the pip installation report's `install` isn't a list")
    environment = data.get("environment")
    if environment is not None and (
        not isinstance(environment, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items())
    ):
        raise SourceError(f"{path.name}: invalid pip report environment")
    source = Source(path.name, pip_report_env=environment if environment is not None else {})
    seen: dict[str, str] = {}
    public = True
    mirrors: set[str] = set()  # hosts other than PyPI's that pip downloaded from (not as a direct URL)
    for number, entry in enumerate(data["install"], 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("metadata"), dict):
            raise SourceError(f"{path.name}: install entry {number} has no metadata")
        metadata = entry["metadata"]
        name = package_name(path, metadata.get("name"))
        version = metadata.get("version")
        if not isinstance(version, str):
            raise SourceError(f"{path.name}: {name} has no version")
        try:
            Version(version)
        except InvalidVersion as e:
            raise SourceError(f"{path.name}: {name} has an invalid version {version!r}") from e
        key = canonicalize_name(name)
        if key in seen:
            raise SourceError(f"{path.name}: duplicate install entry for {name}")
        seen[key] = version
        info = entry.get("download_info")
        if info is not None and not isinstance(info, dict):
            raise SourceError(f"{path.name}: {name} has invalid download_info")
        info = info or {}
        url = info.get("url")
        if url is not None and not isinstance(url, str):
            raise SourceError(f"{path.name}: {name} has invalid download URL")
        try:
            parsed = urlparse(url) if url else None
            host = parsed.hostname if parsed else None
        except ValueError as e:
            raise SourceError(f"{path.name}: {name} has invalid download URL") from e
        if parsed and parsed.path.endswith(".whl"):
            try:
                *_, wheel_tags = parse_wheel_filename(unquote(parsed.path.rsplit("/", 1)[-1]))
            except InvalidWheelFilename:
                pass
            else:
                platforms = {tag.platform for tag in wheel_tags}
                # Evidence either way only when the other libc's pip couldn't accept the file: a wheel tagged
                # for both families (or plain linux_, which both accept) is no evidence.
                musllinux = any(t.startswith("musllinux") for t in platforms)
                manylinux = any(t.startswith("manylinux") for t in platforms)
                plain = any(t.startswith("linux") for t in platforms)
                if musllinux and not manylinux and not plain:
                    source.pip_report_musl = True
                if manylinux and not musllinux and not plain:
                    source.pip_report_glibc = True
        public_file = bool(parsed and parsed.scheme == "https" and host == "files.pythonhosted.org")
        skip = None
        if info.get("vcs_info") is not None:
            skip = "git"
        elif info.get("dir_info") is not None or (parsed and parsed.scheme == "file"):
            skip = "path"
        elif not public_file or entry.get("is_direct"):
            # An absent URL gives no positive evidence this pin came from PyPI.
            skip = "url"
            if host and host != "files.pythonhosted.org" and not entry.get("is_direct"):
                mirrors.add(host)
        if not public_file:
            public = False
        source.packages.append(Package(name, version, skip=skip, origin=path.name))
    if mirrors and all(p.skip for p in source.packages):
        # pip read another index (an Artifactory or devpi mirror, say): checking nothing would look like a pass.
        raise SourceError(
            f"{path.name}: nothing to check: pip downloaded these packages from {', '.join(sorted(mirrors))}, "
            "not PyPI (files.pythonhosted.org), and wheelgap reads only the packages a report got from PyPI"
        )
    source.pypi_only = public
    return source


# The markers that decide which packages pip installs on a target; a pip report holds the values it resolved for.
_REPORT_MARKERS = ("sys_platform", "platform_machine", "python_version", "implementation_name")


def check_report_targets(source: Source, targets: Iterable[Target]) -> None:
    """SourceError unless every target is the environment a pip installation report was resolved for.

    pip resolves a report for the machine it runs on, so on any other target it can install other packages
    (Windows-only dependencies, say) and leave some of these out: checking one there would give wrong answers.
    """
    if source.kind != "pip-report":
        return
    env = source.pip_report_env or {}
    if not all(env.get(key) for key in _REPORT_MARKERS):
        raise SourceError(
            f"{source.label}: the pip installation report doesn't say which environment pip resolved it for "
            f"(its `environment` needs {', '.join(_REPORT_MARKERS)}), so wheelgap can't tell which target it fits; "
            "make the report with `pip install --dry-run --report`"
        )
    report = {**env, "python_version": ".".join(env["python_version"].split(".")[:2])}
    # A target whose platform_machine wheelgap can't pin down (win32 on 64-bit Windows says AMD64 or ARM64,
    # linux-i686 in a container says x86_64, linux-armv7l says aarch64 or armv8l) is compared on the other keys.
    other = [
        t.label(True)
        for t in targets
        if any(report[key] != t.marker_env.get(key) for key in _REPORT_MARKERS if key not in t.unknown_markers)
    ]
    if other:
        where = ", ".join(f"{key} {env[key]}" for key in _REPORT_MARKERS)
        raise SourceError(
            f"{source.label}: pip resolved this report for one environment ({where}), and other targets can "
            f"install other packages, so it can't be checked on {', '.join(other)}: check that target only, "
            "or make a report on each target"
        )
    # musl and glibc record the same environment, but pip on musl accepts musllinux wheels and not manylinux
    # ones, so it can install other packages. A wheel only one libc's pip accepts is proof the resolution isn't
    # one the other's makes, so the other family is refused; a report with no such wheel either way is taken at
    # face value, and one with both (pip run with two --platform values) is refused for both.
    if source.pip_report_musl and source.pip_report_glibc:
        if wrong := [t.label(True) for t in targets if PLATFORMS[t.platform][0] in ("manylinux", "musllinux")]:
            raise SourceError(
                f"{source.label}: this report downloaded both a musllinux wheel pip on glibc doesn't accept and a "
                "manylinux wheel pip on musl doesn't accept (pip run with two --platform values?), so it isn't a "
                f"resolution either libc's pip makes — it can't be checked on {', '.join(wrong)}: make one report "
                "on glibc and one on musl"
            )
    if source.pip_report_musl:
        if wrong := [t.label(True) for t in targets if PLATFORMS[t.platform][0] == "manylinux"]:
            raise SourceError(
                f"{source.label}: a download in this report is a musllinux wheel, which pip on glibc doesn't "
                "accept, so this resolution isn't one glibc pip makes — it can't be checked on "
                f"{', '.join(wrong)}: check a musl target, or make a report on glibc"
            )
    if source.pip_report_glibc:
        if wrong := [t.label(True) for t in targets if PLATFORMS[t.platform][0] == "musllinux"]:
            raise SourceError(
                f"{source.label}: a download in this report is a manylinux wheel, which pip on musl doesn't "
                "accept, so this resolution isn't one musl pip makes — it can't be checked on "
                f"{', '.join(wrong)}: check a glibc target, or make a report on musl"
            )


def _conda_package_name(spec: str) -> str:
    """The package name from a conda MatchSpec like `python=3.12` or `conda-forge::numpy>=1.26`."""
    tail = spec.split("::", 1)[-1].strip()
    return re.split(r"[\s=<>!~\[]", tail, maxsplit=1)[0].strip() or spec


def parse_conda_env(path: Path) -> Source:
    """A conda environment.yml / environment.yaml: its `pip:` list read as a requirements file, and its conda
    dependencies listed as skipped (`conda`)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise SourceError(f"{path.name}: not valid UTF-8 (byte {e.start}), so it can't be read for sure") from None
    try:
        doc = _BlockYaml(text, comments=True).read()
    except _YamlError as e:
        raise SourceError(f"{path.name}: {e}") from None
    if not isinstance(doc, dict):
        raise SourceError(f"{path.name}: expected a YAML mapping")
    raw_deps = doc.get("dependencies")
    if raw_deps is None:
        raw_deps = []
    if not isinstance(raw_deps, list) or isinstance(raw_deps, _FlowList):
        raise SourceError(f"{path.name}: `dependencies` must be a block-style list (`- name` lines)")

    conda_skips: list[Package] = []
    pip_lines: list[str] = []
    for entry in raw_deps:
        if isinstance(entry, str):
            conda_skips.append(Package(_conda_package_name(entry), None, skip="conda", origin=path.name))
        elif isinstance(entry, dict):
            if not entry:
                raise SourceError(f"{path.name}: unsupported empty mapping in `dependencies`")
            if list(entry.keys()) != ["pip"]:
                keys = ", ".join(str(k) for k in entry)
                raise SourceError(f"{path.name}: unsupported mapping in `dependencies` ({keys})")
            pip_list = entry["pip"]
            if pip_list is None:
                continue
            if not isinstance(pip_list, list) or isinstance(pip_list, _FlowList):
                raise SourceError(f"{path.name}: `pip` dependencies must be a block-style list (`- name` lines)")
            for item in pip_list:
                if not isinstance(item, str):
                    raise SourceError(f"{path.name}: `pip` dependency entries must be strings")
                pip_lines.append(item)
        else:
            raise SourceError(f"{path.name}: unsupported entry in `dependencies`")

    constraints: list[Package] = []
    seen: dict[bool, set[Path]] = {False: set(), True: set()}
    index_options: list[str] = []
    format_control = _FormatControl()
    req_lines: list[str] = []
    compiler_signs: list[str] = []
    ignored_options: dict[tuple[Path, str], str] = {}
    pkgs = _read_requirements(
        path,
        seen,
        constraints,
        index_options=index_options,
        format_control=format_control,
        req_lines=req_lines,
        compiler_signs=compiler_signs,
        raw_lines=pip_lines,
        ignored_options=ignored_options,
    )
    pkgs.extend(conda_skips)
    source = _finalize_requirements(path, pkgs, constraints, index_options, format_control, req_lines, compiler_signs)
    source.ignored_options = list(ignored_options.values())
    return source


READERS = {
    "pyproject": parse_pyproject,
    "script": parse_script,
    "uv": parse_uv_lock,
    "pylock": parse_pylock,
    "poetry": parse_poetry_lock,
    "pdm": parse_pdm_lock,
    "pipfile": parse_pipfile_lock,
    "requirements": parse_requirements,
    "pip-report": parse_pip_report,
    "pixi": parse_pixi_lock,
    "conda-env": parse_conda_env,
}
PARSERS = {
    "uv.lock": "uv",
    "pylock.toml": "pylock",
    "poetry.lock": "poetry",
    "pdm.lock": "pdm",
    "Pipfile.lock": "pipfile",
    "pixi.lock": "pixi",
}


def load_file(
    path: Path,
    environment: str | None = None,
    constraint_paths: tuple[Path, ...] = (),
    *,
    kind: str | None = None,
    line_files: tuple[Path, ...] = (),
) -> Source:
    """Read one input file. `environment` picks a pixi.lock environment (default: `default`). `constraint_paths` are
    the constraints files given beside a requirements file, and `line_files` the -r files of its install line (see
    parse_requirements). `kind` overrides `file_kind(path)` when the caller already knows the format (for example
    `pip install -r FILE` in a Dockerfile)."""
    kind = kind or file_kind(path)
    if constraint_paths and kind != "requirements":
        raise SourceError(f"{path.name}: constraints files (pip install -c) only apply to a requirements file")
    if len(line_files) > 1 and kind != "requirements":
        raise SourceError(f"{path.name}: other -r files of an install line only apply to a requirements file")
    if kind == "pixi":
        source = parse_pixi_lock(path, environment)
    elif constraint_paths or len(line_files) > 1:
        source = parse_requirements(path, constraint_paths, line_files)
    else:
        source = READERS[kind](path)
    source.path, source.kind, source.constraint_paths = path, kind, constraint_paths
    _check_field_types(path, source)
    return source


def _check_field_types(path: Path, source: Source) -> None:
    """SourceError for a package field of the wrong type (`version = 1`, a list of markers), which would otherwise
    crash later, far from the file."""
    for pkg in source.packages:
        for what, value in (("version", pkg.version), ("index", pkg.registry)):
            if value is not None and not isinstance(value, str):
                raise SourceError(f"{path.name}: package {pkg.name} has a {what} that isn't a string")
        for what, values in (("marker", pkg.markers), ("file name", pkg.files)):
            if values is not None and not all(isinstance(v, str) for v in values):
                raise SourceError(f"{path.name}: package {pkg.name} has a {what} that isn't a string")


def file_kind(path: Path) -> str:
    """Which reader (a key of READERS) reads this file, by its name."""
    name = path.name
    if name == "pyproject.toml":
        return "pyproject"
    if name.endswith(".py"):
        return "script"
    if name.startswith("environment") and name.endswith((".yml", ".yaml")):
        return "conda-env"
    if name in PARSERS:
        return PARSERS[name]
    if name.startswith("pylock.") and name.endswith(".toml"):
        return "pylock"
    if name.endswith(".py.lock"):  # uv lock --script example.py, before requirements*.lock (requirements.py.lock)
        return "uv"
    if name.startswith("requirements") and name.endswith(".lock"):
        return "requirements"
    if name.endswith(".lock") and "uv" in name:
        return "uv"
    if name.endswith((".txt", ".in")):
        return "requirements"
    if name.endswith(".json"):
        return "pip-report"
    raise SourceError(
        f"don't know how to read {path} "
        "(expected uv.lock, script.py.lock, pylock.toml, poetry.lock, pdm.lock, Pipfile.lock, pixi.lock, "
        "requirements.lock, requirements.txt, pip report.json, environment.yml, pyproject.toml "
        "or a script.py with a # /// script block)"
    )


def find_lockfile(directory: Path, pixi: bool = False) -> Path:
    """The directory's lockfile, in LOCKFILES order; pixi.lock first with `pixi` (an environment was named)."""
    if pixi and (directory / "pixi.lock").is_file():
        return directory / "pixi.lock"
    for name in LOCKFILES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    hint = "Pass a file, or package names like `wheelgap numpy psycopg2`."
    pyproject = directory / "pyproject.toml"
    project_hint = _pyproject_hint(pyproject)
    if project_hint:
        hint = project_hint + " " + hint
    raise SourceError(f"no lockfile found in {directory} (looked for {', '.join(LOCKFILES)}). {hint}")


class DirectoryChoiceError(SourceError):
    """A directory with no input at its root that directory mode reads on its own, and more than one file to choose
    from (or one it won't pick by itself). `candidates` are those files, for --json."""

    def __init__(self, message: str, candidates: list[Path]) -> None:
        super().__init__(message)
        self.candidates = candidates


# How far below a directory its inputs are looked for when the root has none (backend/requirements.txt is 1 deep),
# and how many folders that search opens before it gives up, so a huge tree stays fast.
NESTED_DEPTH = 3
NESTED_FOLDERS = 2000
# Candidates a "pick one" error lists before it counts the rest.
CANDIDATES_LISTED = 10
# Never searched: installed packages, not a project's inputs. Hidden folders and virtualenvs (a pyvenv.cfg) are
# left out as well.
_NEVER_SEARCHED = frozenset({"node_modules", "site-packages", "__pycache__"})
# Words in a folder's or a requirements file's name (tests/, e2e_tests/, docs/, requirements-dev.txt) that say its
# packages are for the tests, the docs, an example or development, not the project itself. Such an input is offered
# when there is nothing else, and never checked on its own.
_SIDE_WORDS = frozenset(
    {"test", "tests", "testing", "example", "examples", "sample", "samples", "demo", "demos", "doc", "docs"}
    | {"bench", "benchmark", "benchmarks", "fixtures", "templates"}
    | {"dev", "develop", "development", "lint", "ci", "typing"}
)


def _is_side(name: str) -> bool:
    return not _SIDE_WORDS.isdisjoint(re.split(r"[^a-z0-9]+", name.lower()))


def _nothing_to_check(source: Source) -> str | None:
    """Why a file gives a run nothing to check, or None when it lists a package from an index."""
    if any(not p.skip for p in source.packages):
        return None
    reasons = sorted({p.skip for p in source.packages if p.skip})
    what = f"only skipped entries ({', '.join(reasons)})" if reasons else "no packages"
    if source.kind == "pixi":
        return f"{what} in its default environment"
    return what


def _command(path: Path) -> str:
    """`wheelgap PATH` as it can be pasted into a shell."""
    quoted = subprocess.list2cmdline([str(path)]) if os.name == "nt" else shlex.quote(str(path))
    return f"wheelgap {quoted}"


# Text files in a requirements/ folder that aren't requirements files, by the start of their name (any case).
_NOT_REQUIREMENTS: tuple[str, ...] = (
    *("constraint", "readme", "license", "licence", "copying", "notice", "changelog", "changes"),
    *("history", "authors", "contributing", "todo", "notes"),
)


def _other_requirements(directory: Path) -> list[Path]:
    """Requirements files directory mode doesn't read by name: requirements*.txt and requirements*.in at the root,
    and *.txt and *.in in requirements/ (not constraints*, README.txt and the like: _NOT_REQUIREMENTS). A .in beside
    its compiled .txt is left out."""
    found: list[Path] = []
    for folder, prefix in ((directory, "requirements"), (directory / "requirements", "")):
        try:
            names = sorted(entry.name for entry in os.scandir(folder) if entry.is_file())
        except OSError:
            continue
        for name in names:
            stem, dot, suffix = name.rpartition(".")
            if not dot or suffix not in ("txt", "in") or not name.startswith(prefix):
                continue
            if not prefix and stem.lower().startswith(_NOT_REQUIREMENTS):
                continue
            if suffix == "in" and f"{stem}.txt" in names:
                continue
            found.append(folder / name)
    return found


# The first line pip-compile and uv (`uv pip compile`, `uv export`) write.
_COMPILER_HEADER_RE = re.compile(r"^#\s*This file (?:is|was) autogenerated by (?:pip-compile|uv)\b", re.MULTILINE)


# A file beside pyproject.toml is opened only for a hint, so a bigger one is passed over unread (compiled locks of
# large projects are well under this).
_HINT_MAX_BYTES = 2_000_000


def _looks_compiled_and_pinned(path: Path) -> bool:
    """Whether a requirements file looks like a compiler's full pin list (pip-compile, `uv pip compile`, `uv export`):
    it has their mark (their header, `# via` comments or `--hash` options), and it reads without an error, lists at
    least one package from an index, and pins each to one version (`==` or `===`, from its line or a -c constraint).
    A hint for the note beside pyproject.toml only: such a file may as well be the build tools' or the docs' pins
    (flask's requirements/build.txt), so it is never read in pyproject.toml's place. Only the file itself is read:
    one over _HINT_MAX_BYTES, or one with a -r or -c (which may name any file: a FIFO, a device, a huge one), doesn't
    look compiled."""
    try:
        if path.stat().st_size > _HINT_MAX_BYTES:
            return False
        text = path.read_text(encoding="utf-8-sig")
        for _, raw in _numbered_lines(text):
            line = _COMMENT_RE.split(raw, 1)[0].strip()
            if line.startswith("-") and (_line_include(shlex.split(line), path.name) or _abbreviated_include(line)):
                return False
        source = load_file(path)
    except Exception:  # unreadable or malformed: it doesn't look like anything
        return False
    marked = (
        source.compiled
        or _COMPILER_HEADER_RE.search(text) is not None
        or any(_VIA_COMMENT_RE.match(line.lstrip()) for line in text.splitlines())
    )
    packages = [p for p in source.packages if not p.skip]
    return marked and bool(packages) and all(p.version is not None and p.error is None for p in packages)


def _requirements_beside_pyproject(directory: Path) -> list[tuple[Path, bool]]:
    """The requirements files under other names at a root whose pyproject.toml was read (_other_requirements, and
    requirements*.lock at the root, which `wheelgap FILE` reads but directory mode doesn't), each with whether it
    looks compiled and pinned; those first."""
    try:
        locks = sorted(
            directory / entry.name
            for entry in os.scandir(directory)
            if entry.is_file()
            and entry.name.startswith("requirements")
            and entry.name.endswith(".lock")
            and not entry.name.endswith(".py.lock")  # a script's lock, by file_kind
            and entry.name not in LOCKFILES
        )
    except OSError:
        locks = []
    found = [(path, _looks_compiled_and_pinned(path)) for path in [*_other_requirements(directory), *locks]]
    return [entry for entry in found if entry[1]] + [entry for entry in found if not entry[1]]


def _unread_project(directory: Path) -> str | None:
    """The file at the root that holds the project's own dependencies where wheelgap doesn't read them (setup.py,
    setup.cfg, or a pyproject.toml load_file refused or took no project from), or None. With one, an input in a
    subfolder isn't taken for the project's: it is more likely a tool's or a service's beside it."""
    for name in ("setup.py", "setup.cfg"):
        if (directory / name).is_file():
            return name
    try:
        data = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValueError:
        return "pyproject.toml"
    if "project" in data or _builds_a_package(data, directory / "pyproject.toml") or _has_poetry_dependencies(data):
        return "pyproject.toml"
    return None


def _folder_input(folder: Path, names: set[str]) -> Path | None:
    """The file `wheelgap FOLDER` would read from a subfolder: its first lockfile in LOCKFILES order, else a
    pyproject.toml that lists [project] dependencies."""
    for name in LOCKFILES:
        if name in names:
            return folder / name
    if "pyproject.toml" in names:
        try:
            source = load_file(folder / "pyproject.toml")
        except Exception:  # unreadable or malformed: `wheelgap FOLDER` would have no answer either
            return None
        if not source.pyproject_groups_only and _nothing_to_check(source) is None:
            return folder / "pyproject.toml"
    return None


def _nested_inputs(directory: Path) -> tuple[list[Path], list[Path], str]:
    """Inputs up to NESTED_DEPTH folders below DIRECTORY, one per folder: (the project's, those under a folder
    _is_side names, what kept the search from seeing the whole tree, in brackets, or ""). Breadth first and sorted,
    so the answer doesn't depend on the file system's order. Symlinks aren't followed, and hidden folders,
    virtualenvs and installed packages are left out."""
    main: list[Path] = []
    side: list[Path] = []
    opened = 0
    unread = ""
    queue: deque[tuple[Path, int, bool]] = deque([(directory, 0, False)])
    while queue:
        folder, depth, is_side = queue.popleft()
        if opened == NESTED_FOLDERS:
            return main, side, f" (the search stopped after {NESTED_FOLDERS} folders)"
        opened += 1
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
            files = {entry.name for entry in entries if entry.is_file(follow_symlinks=False)}
            folders = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
        except OSError:
            # It may hold an input, so what was found elsewhere isn't known to be the only one.
            unread = unread or f" ({folder} couldn't be read)"
            continue
        if depth:
            if "pyvenv.cfg" in files:
                continue  # a virtualenv
            if (found := _folder_input(folder, files)) is not None:
                (side if is_side else main).append(found)
        if depth < NESTED_DEPTH:
            for name in folders:
                if not name.startswith(".") and name not in _NEVER_SEARCHED:
                    queue.append((folder / name, depth + 1, is_side or _is_side(name)))
    return main, side, unread


def _choice_error(directory: Path, what: str, candidates: list[Path]) -> DirectoryChoiceError:
    lines = [f"  {_command(path)}" for path in candidates[:CANDIDATES_LISTED]]
    if len(candidates) > CANDIDATES_LISTED:
        lines.append(f"  (and {len(candidates) - CANDIDATES_LISTED} more)")
    return DirectoryChoiceError(
        f"no lockfile, requirements.txt or pyproject.toml dependencies at the root of {directory}{what}:\n"
        + "\n".join(lines),
        candidates,
    )


def _load_picked(path: Path) -> Source:
    """load_file for a file the search picked: the user didn't name it, so an error that doesn't must."""
    try:
        return load_file(path)
    except UnicodeDecodeError as e:
        raise SourceError(f"{path}: not UTF-8 text ({e.reason} at byte {e.start})") from None


def _load_lockfile(directory: Path, environment: str | None = None) -> Source | None:
    """The directory's first lockfile in LOCKFILES order (pixi.lock first when an environment is named), read; None
    if it has none. A file with nothing to check (a requirements.txt that only says `.`, a pixi.lock of conda
    packages) gives way to the next one that has something, then to pyproject.toml; if none has, it is the answer as
    it always was."""
    pixi_lock = directory / "pixi.lock"
    if environment is not None and pixi_lock.is_file():
        source = load_file(pixi_lock, environment)
        source.found = DirectoryInput(directory, pixi_lock, "lockfile")
        return source
    pyproject = directory / "pyproject.toml"
    at_root = [directory / name for name in LOCKFILES if (directory / name).is_file()]
    if not at_root:
        return None
    first = load_file(at_root[0], environment)
    first.found = DirectoryInput(directory, at_root[0], "lockfile")
    if at_root[0] != pixi_lock and pixi_lock.is_file():
        first.passed_over = pixi_lock
    why = _nothing_to_check(first)
    if why is None:
        return first
    empty = [(at_root[0], why)]
    for path in [*at_root[1:], *([pyproject] if pyproject.is_file() else [])]:
        try:
            other = load_file(path)
        except Exception:
            continue  # unreadable or malformed: no better than the file that was found first
        if other.pyproject_groups_only:
            continue
        why = _nothing_to_check(other)
        if why is None:
            other.found = DirectoryInput(directory, path, "pyproject" if path == pyproject else "lockfile", empty)
            return other
        empty.append((path, why))
    return first


def _load_subfolder(directory: Path, path: Path) -> Source:
    """The one input in DIRECTORY's subfolders, read as `wheelgap FOLDER` reads its folder, and labelled with its
    path in DIRECTORY (scripts/requirements.txt): a bare requirements.txt in the header, --markdown or --json's
    sources would read as the root's."""
    try:
        source = _load_lockfile(path.parent) if path.name in LOCKFILES else load_file(path)
    except UnicodeDecodeError as e:
        raise SourceError(f"{path}: not UTF-8 text ({e.reason} at byte {e.start})") from None
    assert source is not None
    read = source.found.path if source.found is not None else path
    source.found = DirectoryInput(directory, read, "subfolder", source.found.empty if source.found is not None else [])
    source.label = read.relative_to(directory).as_posix()
    return source


def load_directory(directory: Path, environment: str | None = None) -> Source:
    """Read a directory input. Its first lockfile in LOCKFILES order is read, as always (_load_lockfile). With none
    of those names: the root's pyproject.toml if it lists [project] dependencies, else its only requirements file
    under another name, else the only input in a subfolder. More than one to choose from is a DirectoryChoiceError
    that lists them: wheelgap never picks one of several. `source.found` says what was read."""
    if (at_root := _load_lockfile(directory, environment)) is not None:
        return at_root
    pyproject = directory / "pyproject.toml"

    # A pyproject.toml with a [project] table that lists nothing is the answer only if nothing else turns up.
    last_resort: Source | None = None
    problem: str | None = None
    if pyproject.is_file():
        try:
            source = load_file(pyproject)
        except SourceError as e:
            problem = str(e)
        except Exception as e:  # malformed in a way its reader has no message for
            problem = f"{pyproject}: {type(e).__name__}: {e}"
        else:
            if not source.pyproject_groups_only:
                source.found = DirectoryInput(directory, pyproject, "pyproject")
                if _nothing_to_check(source) is None or source.pyproject_extras:
                    source.found.other_requirements = _requirements_beside_pyproject(directory)
                    return source
                last_resort = source
    empty = [(pyproject, "no dependencies")] if last_resort is not None else []

    others = _other_requirements(directory)
    if len(others) > 1:
        raise _choice_error(directory, ", and more than one requirements file to choose from. Check one", others)
    if others and not _is_side(others[0].name.rpartition(".")[0]):
        source = _load_picked(others[0])
        source.found = DirectoryInput(directory, others[0], "requirements", empty)
        return source

    main, side, stopped = _nested_inputs(directory)
    if len(main) == 1 and not stopped:
        # Taken for the project's dependencies only where the root isn't a project of its own, and where no other
        # requirements file could as well be the project's: the root's (dev or docs by its name, or it would have
        # been read above) and, beside a subfolder's requirements file, the ones under another name
        # (docker/main/requirements-wheels.txt).
        rivals = list(others)
        if file_kind(main[0]) == "requirements":
            rivals += [
                path
                for path in _other_requirements(main[0].parent)
                if path.name not in LOCKFILES and not _is_side(path.name.rpartition(".")[0])
            ]
        if last_resort is not None:
            why = "Its pyproject.toml lists no dependencies"
        elif (unread := _unread_project(directory)) is not None:
            why = f"Its own dependencies are in {unread}, which wheelgap can't read"
        elif rivals:
            raise _choice_error(
                directory,
                ", and more than one file that could hold the project's dependencies. Check one",
                [main[0], *rivals],
            )
        else:
            return _load_subfolder(directory, main[0])
        raise _choice_error(
            directory,
            f". {why}, so the one input in its subfolders isn't taken for the project's. To check that one",
            main,
        )
    if main:
        raise _choice_error(directory, f", but inputs in its subfolders{stopped}. Check one", main)
    if last_resort is not None:
        return last_resort
    if others or side:
        raise _choice_error(
            directory,
            f", and the only inputs it has are, by their names, for development, tests, docs or examples{stopped}, "
            "which wheelgap doesn't check on its own. To check one",
            [*others, *side],
        )

    looked = (
        f"{', '.join(LOCKFILES)}, a pyproject.toml with [project] dependencies, requirements*.txt and "
        f"requirements/*.txt there, and the lockfile names and pyproject.toml up to {NESTED_DEPTH} folders down"
    )
    hint = "Pass a file, or package names like `wheelgap numpy psycopg2`."
    if problem is not None:
        hint = f"Its pyproject.toml can't be checked ({problem}). {hint}"
    elif project_hint := _pyproject_hint(pyproject):
        hint = f"{project_hint} {hint}"
    raise SourceError(f"no lockfile found in {directory}{stopped} (looked for {looked}). {hint}")


# A dependency group's name as PEP 735 allows it: letters, digits, `.`, `-` and `_`, starting and ending with a
# letter or digit.
_GROUP_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def pyproject_group_names(names: Iterable[str]) -> list[str]:
    """The dependency group names a message may repeat, normalized and sorted: only those PEP 735 allows. The names
    are text from the file, and these messages go to a terminal, a CI log or an MCP client."""
    return sorted({canonicalize_name(name) for name in names if _GROUP_NAME.fullmatch(name)})


def _pyproject_hint(pyproject: Path) -> str | None:
    """For the "no lockfile" error only: the command that checks the folder's pyproject.toml instead, when there is
    one that would work. A missing or unreadable file just means no hint."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data.get("project"), dict):
        return f"To check the direct dependencies in its pyproject.toml, run `wheelgap {pyproject}`."
    groups = data.get("dependency-groups")
    if "project" in data or not isinstance(groups, dict):
        return None
    # What _project_source refuses with --group: a setuptools, flit, PDM-legacy or Poetry project keeps its own
    # dependencies outside [project], so the hint would only lead to that error.
    if _builds_a_package(data, pyproject) or _has_poetry_dependencies(data):
        return None
    keys = [canonicalize_name(name) for name in groups]
    if len(set(keys)) != len(keys):
        return None  # two groups that are the same once normalized: refused whichever group is asked for
    names = pyproject_group_names(groups)
    if not names:
        return None
    return (
        f"To check a dependency group in its pyproject.toml, run `wheelgap {pyproject} --group NAME` "
        f"(groups: {', '.join(names)})."
    )


def parse_dist(path: Path) -> Source:
    """`--dist DIR`: the wheels and sdist of one release about to be uploaded (`python -m build`, `uv build`).
    Name and version come from the filenames; a folder that mixes projects or versions is refused."""
    from .check import SDIST_SUFFIXES  # sources.py is imported by check.py

    try:
        filenames = sorted(entry.name for entry in path.iterdir())
    except OSError as e:
        raise SourceError(f"--dist: {path}: {e.strerror or e}") from e
    files: list[str] = []
    names: dict[str, str] = {}
    versions: set[Version] = set()
    for filename in filenames:
        try:
            if filename.endswith(".whl"):
                name, version, *_ = parse_wheel_filename(filename)
            elif filename.endswith(SDIST_SUFFIXES):
                name, version = parse_sdist_filename(filename)
            else:
                continue
        except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion):
            continue  # not a distribution filename: left out, as pip's own upload would
        names.setdefault(str(canonicalize_name(name)), str(name))
        versions.add(version)
        files.append(filename)
    if not files:
        raise SourceError(f"--dist: {path}: no wheels or sdists")
    if len(names) > 1 or len(versions) > 1:
        raise SourceError(f"--dist: {path}: files for more than one project or version; build into an empty dist/")
    display = next(iter(names.values()))
    pkg = Package(display, str(next(iter(versions))), files=files, origin=path.name)
    return Source(path.name, [pkg])


def load_inputs(
    inputs: list[str],
    environment: str | None = None,
    *,
    requirements_constraints: Mapping[Path, tuple[Path, ...]] | None = None,
    file_kinds: Mapping[Path, str] | None = None,
    requirements_line_files: Mapping[Path, tuple[Path, ...]] | None = None,
) -> list[Source]:
    """Each input is a file, a directory (its input is found by load_directory) or a requirement like 'numpy==2.3.4'.
    `environment` picks the pixi.lock environment; with no pixi.lock input it's an error.
    `requirements_constraints` maps a requirements file among the inputs to the constraints files given beside it
    (--dockerfile-inputs: `pip install -r FILE -c CONSTRAINTS`), and `requirements_line_files` to all the -r files of
    its install line (`pip install -r a.txt -r b.txt`), whose options pip applies to each of them.
    `file_kinds` overrides `file_kind()` for specific files (e.g. `pip install -r` files with any name)."""
    if environment == "":
        raise SourceError("--environment needs a name")
    sources: list[Source] = []
    adhoc = Source("command line", pypi_only=True)  # names to look up on PyPI
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            sources.append(load_directory(p, environment))
        elif p.is_file():
            constraints = requirements_constraints.get(p, ()) if requirements_constraints is not None else ()
            kind = file_kinds.get(p) if file_kinds is not None else None
            line_files = requirements_line_files.get(p, ()) if requirements_line_files is not None else ()
            sources.append(load_file(p, environment, constraints, kind=kind, line_files=line_files))
        elif "/" in item or item.endswith((".lock", ".txt", ".toml", ".json", ".in", ".yml", ".yaml")):
            raise SourceError(f"{item}: no such file")
        else:
            adhoc.packages.append(requirement_to_package(item, "command line"))
            adhoc.specs.append(item)
    if adhoc.packages:
        sources.append(adhoc)
    if environment is not None and not any(s.kind == "pixi" for s in sources):
        raise SourceError("--environment applies to pixi.lock inputs only")
    return sources


# A pixi environment often has 100+ conda packages: past this many, text and Markdown count them (--json lists them).
CONDA_LISTED = 5


def skipped_list(skipped: list[Package], name: Callable[[str], str] = str) -> str:
    """Each skipped package and why, with more than CONDA_LISTED conda packages counted rather than listed."""
    conda = sum(1 for p in skipped if p.skip == "conda")
    items = [f"{name(p.name)} ({p.skip})" for p in skipped if p.skip != "conda" or conda <= CONDA_LISTED]
    if conda > CONDA_LISTED:
        items.append(f"{conda} conda packages (not on PyPI)")
    return ", ".join(items)
