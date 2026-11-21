"""Python API: check packages and lockfiles without going through the command line.

import wheelgap

targets = wheelgap.make_targets(python="3.13", platforms=["linux-aarch64", "musl-aarch64"])
result = wheelgap.check_package("numpy", "2.3.4", targets=targets)
result.ok                   # True if every target has a wheel
result.statuses[targets[1]] # Status.WHEEL / SOURCE / MISSING / ...
result.reasons.get(targets[1])  # for a source or missing target: the wheels --explain lists and why they don't fit
result.to_dict()            # JSON-friendly, keyed by "3.13 musl-aarch64"
result.to_dict(explain=True)  # also rejected, via, direct and issues_url, as wheelgap --json --explain gives them
"""

from __future__ import annotations

import sys
import sysconfig
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Literal, overload

from packaging.utils import canonicalize_name

from . import origin, pypi
from .check import Row, Status, check, explain, explain_lines, status_for, wheel_tags
from .deps import Resolver, pinned_complete, resolve_deps
from .offline import require_offline
from .pyodide import PyodideLock, bundled_version_differs, bundles, check_locks
from .pypi import apply_locked_python_ranges, fill_all, recheck_lock
from .sources import (
    LOCKFILES,
    Package,
    Source,
    SourceError,
    UvSelection,
    check_report_targets,
    file_kind,
    find_lockfile,
    load_file,
    pyproject_group_names,
    pythons_outside_lock,
    restore_uv_python_files,
    select_pyproject_extras,
    select_pyproject_groups,
    select_uv_dependencies,
    skipped_list,
)
from .targets import (
    DEFAULT_ANDROID_API,
    DEFAULT_GLIBC,
    DEFAULT_IOS,
    DEFAULT_MACOS,
    DEFAULT_MUSL,
    DEFAULT_PLATFORMS,
    Target,
    build_targets,
)

_OK = (Status.WHEEL, Status.NOT_NEEDED, Status.BUNDLED)
_EXPLAINED = (Status.SOURCE, Status.MISSING)  # the statuses --explain explains
# The statuses --explain traces to the user's own requirements (via, direct): the gaps, and a package that couldn't be
# looked up, which is the row a user most needs to trace (#453).
_TRACED = (*_EXPLAINED, Status.UNKNOWN)


def running_python() -> str:
    """The Python this code runs on, e.g. '3.13' or '3.14t'."""
    ft = "t" if sysconfig.get_config_var("Py_GIL_DISABLED") else ""
    return f"{sys.version_info.major}.{sys.version_info.minor}{ft}"


def make_targets(
    python: str | Iterable[str] | None = None,
    platforms: str | Iterable[str] | None = None,
    *,
    glibc: str = DEFAULT_GLIBC,
    musl: str = DEFAULT_MUSL,
    macos: str = DEFAULT_MACOS,
    ios: str = DEFAULT_IOS,
    android_api: str | int = DEFAULT_ANDROID_API,
) -> list[Target]:
    """Targets from the same names the CLI takes: python="3.13,3.14t", platforms=["all", "pyodide", "mobile"].

    python="next" (or "next-t") is the newest CPython this wheelgap version knows, targets.NEWEST_PYTHON.
    """
    pythons = [python] if isinstance(python, str) else list(python or [running_python()])
    plats = [platforms] if isinstance(platforms, str) else list(platforms or DEFAULT_PLATFORMS)
    if not any(p.strip() for item in pythons for p in item.split(",")):
        pythons = [running_python()]
    return build_targets(pythons, plats, glibc, musl, macos, ios, android_api)


@dataclass(frozen=True)
class Result:
    """One package's status on each target."""

    name: str
    version: str | None
    statuses: dict[Target, Status] = field(default_factory=dict)
    files: tuple[str, ...] = ()
    error: str | None = None
    # With pyodide_locks: a shared version when all applicable locks agree, plus whether any bundled
    # version is outside the requested pin or range (the package still counts as ok).
    bundled_version: str | None = None
    bundled_version_differs: bool = False
    # Per SOURCE, MISSING or UNKNOWN target, the dependency path from a root down to this package, as --explain
    # shows it:
    # ("app", "pandas", "numpy"), or ("(dev group)", "pytest") for a dependency group. Filled for inputs with a
    # dependency graph: uv.lock and poetry.lock before lock-version 2.1, and anything checked with deps=True.
    via: dict[Target, tuple[str, ...]] = field(default_factory=dict)
    # Files of a pinned release that the index marks as yanked: pip skips them when the release has others it can use.
    yanked: frozenset[str] = frozenset()
    # Files of a pinned release whose Requires-Python excludes the target Python, mapped to their --explain reason.
    requires_python_rejected: dict[str, str] = field(default_factory=dict)
    no_binary: bool = False
    only_binary: bool = False
    # _result installs per-instance data; the immutable default is not a public dataclass field.
    _resolutions: ClassVar[Mapping[str, Package]] = MappingProxyType({})
    # Each applicable Pyodide lock may ship a different version for its Python.
    bundled_versions: dict[str, str] = field(default_factory=dict)
    # The direct dependencies that lead to each gap target (or unknown one), as the table's (via ...) names them.
    # Only check_lockfile() and check_packages(deps=True) fill it, and only for inputs with a dependency graph.
    direct: dict[Target, tuple[str, ...]] = field(default_factory=dict)
    issues_url: str | None = None
    pip_refuses: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(s in _OK for s in self.statuses.values())

    @property
    def gaps(self) -> list[Target]:
        return [t for t, s in self.statuses.items() if s not in _OK]

    @property
    def pure(self) -> bool:
        """Has a Python 3 wheel that works on every platform (e.g. py3-none-any, but not py2-none-any)."""
        return any(
            t.platform == "any" and t.abi == "none" and t.interpreter.startswith(("py3", "cp3"))
            for tags in wheel_tags(list(self.files))
            for t in tags
        )

    @cached_property
    def rejected(self) -> dict[Target, list[tuple[str, str]]]:
        """Per SOURCE or MISSING target, (filename, reason) for every wheel it can't use, the closest misses first."""
        return {
            t: explain(
                list(p.files or ()),
                t,
                p.yanked,
                p.requires_python_rejected,
                no_binary=self.no_binary,
                only_binary=self.only_binary,
            )
            for t, s in self.statuses.items()
            if s in _EXPLAINED
            for p in [self._resolutions.get(f"{t.major}.{t.minor}", self)]
        }

    @cached_property
    def reasons(self) -> dict[Target, str]:
        """Per SOURCE or MISSING target, the text --explain prints under it, one "file  reason" per line, unaligned
        and with every rejected wheel (or "no wheels published for this version"), plus "there is an sdist, but
        ... can't build packages from source" where that applies."""
        return {
            t: "\n".join(
                explain_lines(
                    list(p.files or ()),
                    t,
                    yanked=p.yanked,
                    requires_python_rejected=p.requires_python_rejected,
                    no_binary=self.no_binary,
                    only_binary=self.only_binary,
                )
            )
            for t, s in self.statuses.items()
            if s in _EXPLAINED
            for p in [self._resolutions.get(f"{t.major}.{t.minor}", self)]
        }

    def to_dict(self, explain: bool = False) -> dict[str, object]:
        """JSON-friendly, keyed by target label. explain=True adds "rejected", "via", "direct" and "issues_url" as
        `wheelgap --json --explain` does: each SOURCE or MISSING target's rejected wheels as {"file", "reason"}
        objects, its dependency path where there is one and the direct dependencies that lead to it (for UNKNOWN
        targets too), and the package's issue tracker URL."""
        out: dict[str, object] = {
            "name": self.name,
            "version": self.version,
            "error": self.error,
            "results": {t.label(True): s.value for t, s in self.statuses.items()},
        }
        if self.bundled_version is not None:
            out["bundled"] = self.bundled_version
        if self.bundled_versions:
            out["bundled_versions"] = dict(self.bundled_versions)
        if self.bundled_version is not None or self.bundled_versions:
            out["bundled_version_differs"] = self.bundled_version_differs
        if self.pip_refuses:
            out["pip_refuses"] = dict(self.pip_refuses)
        if explain:
            out["rejected"] = {
                t.label(True): [{"file": f, "reason": why} for f, why in rejected]
                for t, rejected in self.rejected.items()
            }
            out["via"] = {t.label(True): list(path) for t, path in self.via.items()}
            out["direct"] = {t.label(True): list(names) for t, names in self.direct.items()}
            out["issues_url"] = self.issues_url
        return out


def explain_issues_urls(
    items: Iterable[Row | Package],
    sources: list[Source],
    index_url: str | None = None,
) -> dict[int, str | None]:
    """Issues tracker URLs for explained packages that pass the PyPI origin rule, by id(package)."""
    item_list = list(items)
    pkgs = [it.package if isinstance(it, Row) else it for it in item_list]
    if origin.other_index(index_url) is not None:
        return {id(p): None for p in pkgs}
    private = origin.private_names(sources, simple_index=index_url)
    source_of = {id(p): s for s in sources for original in s.packages for p in original.variants}
    out: dict[int, str | None] = {}
    for p in pkgs:
        src = source_of.get(id(p))
        if src and origin.known_pypi(p, src, simple_index=index_url, private=private):
            out[id(p)] = pypi.extract_issues_url(p.project_urls, p.home_page)
        else:
            out[id(p)] = None
    return out


def _result(
    pkg: Package,
    statuses: dict[Target, Status],
    pyodide_locks: Sequence[PyodideLock] = (),
    via: dict[Target, list[str]] | None = None,
    direct_via: dict[Target, tuple[str, ...]] | None = None,
    issues_url: str | None = None,
) -> Result:
    applied = [lock for lock in pyodide_locks if any(bundles(lock, pkg.key, t, s) for t, s in statuses.items())]
    statuses = {
        t: Status.BUNDLED if any(bundles(lock, pkg.key, t, s) for lock in applied) else s for t, s in statuses.items()
    }
    bundled_versions = {f"{lock.python[0]}.{lock.python[1]}": lock.packages[pkg.key] for lock in applied}
    unique_versions = set(bundled_versions.values())
    shared_version = next(iter(unique_versions)) if len(unique_versions) == 1 else None
    via = via or {}
    direct_via = direct_via or {}
    result = Result(
        pkg.name,
        pkg.version,
        statuses,
        tuple(dict.fromkeys(f for variant in pkg.variants for f in variant.files or ())),
        # With several Pythons, a lookup that went wrong records its error on each resolution, not on pkg.
        pkg.error or next((variant.error for variant in pkg.variants if variant.error), None),
        # Preserve the legacy scalar only when all applied locks agree.
        shared_version,
        any(bundled_version_differs(pkg, lock) for lock in applied),
        {t: tuple(via[t]) for t, s in statuses.items() if s in _TRACED and t in via},
        # A file's yanked flag and its Requires-Python reason are the same for every Python, so these are the union.
        frozenset(f for variant in pkg.variants for f in variant.yanked),
        {f: why for variant in pkg.variants for f, why in variant.requires_python_rejected.items()},
        pkg.no_binary,
        pkg.only_binary,
        bundled_versions=bundled_versions,
        direct={
            t: tuple(dict.fromkeys(direct_via[t])) for t, s in statuses.items() if s in _TRACED and direct_via.get(t)
        },
        issues_url=issues_url,
        pip_refuses=dict(pkg.pip_refuses),
    )
    object.__setattr__(result, "_resolutions", pkg.resolutions)
    return result


def _package_targets(targets: Sequence[Target] | None) -> list[Target]:
    resolved = list(targets) if targets is not None else make_targets()
    if not resolved:
        raise ValueError("package checks require at least one target")
    return resolved


def check_packages(
    packages: Iterable[str | tuple[str, str | None]],
    targets: Sequence[Target] | None = None,
    *,
    workers: int = 16,
    pyodide_locks: Iterable[PyodideLock] = (),
    deps: bool = False,
) -> list[Result]:
    """Check several packages, looking their files up on the index in parallel.

    Each package is a name ("numpy", the latest release) or a (name, version) pair.
    deps=True also checks everything they install, resolved for each target from PyPI's metadata as --deps does:
    one Result per package in the tree, with `via` giving each gap target's path from a package passed in. A tree
    that can't be resolved raises the DepsError (a SourceError) the command line reports.
    pyodide_locks (from wheelgap.pyodide.load_pyodide_lock, one per Python) mark a package that a lock's Pyodide
    ships itself as BUNDLED on pyodide targets for that lock's Python, where it would otherwise be SOURCE or MISSING.
    As with --pyodide-lock, a lock whose Python no pyodide target uses raises PyodideLockError (a ValueError) before
    any lookup, and so do two locks for the same Python. A result with a BUNDLED target has
    bundled_versions (one version per Python), bundled_version (only when all applicable locks agree) and
    bundled_version_differs (True if any bundled version is outside the requested pin or range).
    """
    targets = _package_targets(targets)
    pyodide_locks = tuple(pyodide_locks)
    if workers < 1:
        raise ValueError("workers must be at least 1")
    check_locks(pyodide_locks, targets)
    pkgs = [Package(p, None) if isinstance(p, str) else Package(p[0], p[1]) for p in packages]
    for p in pkgs:
        p.origin = "api"
    pythons = tuple(dict.fromkeys(f"{t.major}.{t.minor}" for t in targets))
    if deps:
        if urls := [p.name for p in pkgs if "@" in p.name or "://" in p.name]:
            raise ValueError(f"deps=True looks names up on PyPI, so it can't check a URL: {', '.join(urls)}")
        source = Source(
            "api", pkgs, specs=[p.name if p.version is None else f"{p.name}=={p.version}" for p in pkgs], pypi_only=True
        )
        resolve_deps(source, targets)
        fill_all(source.packages, workers=workers, pythons=pythons)
        rows = [r for r in check([source], targets) if r.needed]
        issues = explain_issues_urls(rows, [source])
        return [
            _result(r.package, dict(r.results), pyodide_locks, r.via, r.direct_via, issues.get(id(r.package)))
            for r in rows
        ]
    source = Source("api", pkgs, pypi_only=True)
    fill_all(pkgs, workers=workers, pythons=pythons)
    issues = explain_issues_urls(pkgs, [source])
    return [
        _result(
            p,
            {
                t: status_for(None if resolved.error else resolved.files, t, yanked=resolved.yanked)
                for t in targets
                for resolved in [p.resolutions.get(f"{t.major}.{t.minor}", p)]
            },
            pyodide_locks,
            issues_url=issues.get(id(p)),
        )
        for p in pkgs
    ]


@overload
def check_package(
    name: str,
    version: str | None = None,
    targets: Sequence[Target] | None = None,
    *,
    files: Iterable[str] | None = None,
    pyodide_locks: Iterable[PyodideLock] = (),
    deps: Literal[False] = False,
) -> Result: ...


@overload
def check_package(
    name: str,
    version: str | None = None,
    targets: Sequence[Target] | None = None,
    *,
    files: Iterable[str] | None = None,
    pyodide_locks: Iterable[PyodideLock] = (),
    deps: Literal[True],
) -> list[Result]: ...


@overload
def check_package(
    name: str,
    version: str | None = None,
    targets: Sequence[Target] | None = None,
    *,
    files: Iterable[str] | None = None,
    pyodide_locks: Iterable[PyodideLock] = (),
    deps: bool = False,
) -> Result | list[Result]: ...


def check_package(
    name: str,
    version: str | None = None,
    targets: Sequence[Target] | None = None,
    *,
    files: Iterable[str] | None = None,
    pyodide_locks: Iterable[PyodideLock] = (),
    deps: bool = False,
) -> Result | list[Result]:
    """Check one package. Pass `files` (distribution filenames) to skip the index lookup.

    pyodide_locks work as in check_packages(). deps=True returns a list: a Result for every package in its tree,
    as check_packages(deps=True) does.
    """
    pyodide_locks = tuple(pyodide_locks)
    if deps:
        if files is not None:
            raise ValueError("deps=True looks the dependencies up on PyPI, so it can't be combined with files")
        return check_packages([(name, version)], targets, pyodide_locks=pyodide_locks, deps=True)
    if files is None:
        return check_packages([(name, version)], targets, pyodide_locks=pyodide_locks)[0]
    targets = _package_targets(targets)
    check_locks(pyodide_locks, targets)
    pkg = Package(name, version)
    pkg.files = list(files)
    return _result(pkg, {t: status_for(pkg.files, t) for t in targets}, pyodide_locks)


def check_lockfile(
    path: str | Path,
    targets: Sequence[Target] | None = None,
    *,
    online: bool = False,
    offline: bool = False,
    pyodide_locks: Iterable[PyodideLock] = (),
    environment: str | None = None,
    deps: bool = False,
    allow_skipped: bool = False,
    extras: Iterable[str] | None = None,
    all_extras: bool = False,
    groups: Iterable[str] | None = None,
    only_groups: Iterable[str] = (),
    no_dev: bool = False,
) -> list[Result]:
    """Check every package a lockfile (or a project directory) installs on at least one target.

    Environment markers and the uv.lock dependency graph are respected, as in the CLI. A uv.lock whose
    requires-python leaves out a target's Python may be missing wheels for it, so its PyPI packages are also
    looked up on the index (keeping the lock's files if a lookup fails).
    Requirements ranges resolve separately for each target Python major/minor. Distinct resolutions return
    separate Results with their own version and eligible files; targets using another resolution are NOT_NEEDED.
    deps=True resolves the dependency tree for requirements files, a conda environment.yml (its pip: list),
    pyproject.toml and script (PEP 723 `# /// script`) inputs from PyPI metadata, as --deps does: one Result per package
    in the tree, with `via` giving each gap target's path from a direct dependency. A tree that can't be resolved raises
    DepsError (a SourceError). With deps=True, pyproject.toml (and a project directory containing only one) and a script
    are accepted; lockfile inputs (uv.lock, poetry.lock, ...) and offline=True are refused. Requirements that aren't
    index packages (-e ., local paths, URLs) can't be resolved from PyPI: with deps=True they raise SourceError, unless
    the file is compiled with hashes (it already pins what they install) or allow_skipped=True is passed to check the
    rest. allow_skipped does nothing without deps=True.
    With offline=True, only locked files are used: inputs needing an index lookup raise SourceError, and the
    other-Python recheck is skipped. Locked files may therefore omit wheels for those Python versions.
    online=True and offline=True cannot be combined.
    pyodide_locks work as in check_packages(), and are checked before the lockfile is read.
    environment picks a pixi.lock environment (default: `default`); in a project directory it makes pixi.lock the
    lockfile read. Naming one for any other lockfile raises SourceError.
    A pip installation report (`pip install --dry-run --report`) is resolved for one environment, so it raises
    SourceError unless every target matches that environment's platform, machine, Python and implementation.
    extras, all_extras, groups, only_groups and no_dev select the same install as the CLI's corresponding flags.
    With no selectors, lockfiles keep the conservative all-extras/all-groups check. An explicit empty extras or
    groups iterable selects the install with no extras and the installer's default groups. pyproject.toml with
    deps=True accepts extras and PEP 735 groups, but not no_dev; scripts accept no extras or groups. Unsupported
    selectors, unknown names and conflicting combinations raise SourceError before lookups. [tool.wheelgap] is
    not read; installer metadata (such as uv's default-groups) still determines what its default install contains.
    """
    return _check_lockfile(
        path,
        targets,
        online=online,
        offline=offline,
        pyodide_locks=pyodide_locks,
        environment=environment,
        deps=deps,
        allow_skipped=allow_skipped,
        extras=extras,
        all_extras=all_extras,
        groups=groups,
        only_groups=only_groups,
        no_dev=no_dev,
    )[0]


def _check_lockfile(
    path: str | Path,
    targets: Sequence[Target] | None,
    *,
    online: bool,
    offline: bool,
    pyodide_locks: Iterable[PyodideLock] = (),
    environment: str | None = None,
    deps: bool = False,
    allow_skipped: bool = False,
    extras: Iterable[str] | None = None,
    all_extras: bool = False,
    groups: Iterable[str] | None = None,
    only_groups: Iterable[str] = (),
    no_dev: bool = False,
) -> tuple[list[Result], Source, list[Row]]:
    """check_lockfile(), also returning the source and check rows for diff's skips and suggestions."""
    targets = _package_targets(targets)
    pyodide_locks = tuple(pyodide_locks)
    if online and offline:
        raise ValueError("online=True and offline=True cannot be combined")
    check_locks(pyodide_locks, targets)
    p = Path(path)
    if p.is_dir():
        if deps and not any((p / name).is_file() for name in LOCKFILES) and (p / "pyproject.toml").is_file():
            lockfile = p / "pyproject.toml"
        else:
            lockfile = find_lockfile(p, environment is not None)
    else:
        lockfile = p

    try:
        kind: str | None = file_kind(lockfile)
    except SourceError:
        kind = None  # load_file says why (no such file, or a name it can't read)
    if kind in ("pyproject", "script") and not deps and lockfile.is_file():
        what = "a pyproject.toml" if kind == "pyproject" else "a script's # /// script block"
        raise SourceError(
            f"{lockfile}: {what} lists direct dependencies only, so check_lockfile() and "
            f"wheelgap.diff don't read it; pass a lockfile (the wheelgap command checks {what})"
        )
    source: Source = load_file(lockfile, environment)
    if environment is not None and source.kind != "pixi":
        raise SourceError(f"{lockfile}: environment applies to pixi.lock only")
    # A bare string is one name, as for make_targets(), not its letters.
    only_groups = (only_groups,) if isinstance(only_groups, str) else tuple(only_groups)
    selected = extras is not None or groups is not None or all_extras or no_dev or bool(only_groups)
    extra_names = (extras,) if isinstance(extras, str) else tuple(extras or ())
    group_names = (groups,) if isinstance(groups, str) else tuple(groups or ())
    if source.pyproject_extras is not None:
        if source.kind == "script" and (group_names or only_groups or no_dev or extra_names or all_extras):
            raise SourceError("extra/group selection doesn't apply to a script: it has no extras or dependency groups")
        if no_dev:
            raise SourceError("--no-dev doesn't apply to pyproject.toml: no dependency group is selected by default")
        if source.pyproject_groups_only:
            if extra_names or all_extras:
                raise SourceError(f"{source.label}: no [project] optional-dependencies to select from")
            if not (group_names or only_groups):
                group_names_listed = pyproject_group_names(source.pyproject_groups)
                if group_names_listed:
                    raise SourceError(
                        f"{source.label}: no [project] table; pick one with groups or only_groups: "
                        f"{', '.join(group_names_listed)}"
                    )
                raise SourceError(f"{source.label}: no [project] table and no groups to select")
        if only_groups and (extra_names or all_extras):
            raise SourceError("--only-group cannot be combined with --extra or --all-extras for pyproject.toml inputs")
        select_pyproject_groups(source, group_names, only_groups)
        select_pyproject_extras(source, extra_names, all_extras)
    elif selected:
        select_uv_dependencies(
            [source],
            UvSelection(
                extras=frozenset(canonicalize_name(name) for name in extra_names),
                all_extras=all_extras,
                groups=frozenset(canonicalize_name(name) for name in group_names),
                only_groups=frozenset(canonicalize_name(name) for name in only_groups),
                no_dev=no_dev,
            ),
        )

    if deps:
        if not (source.specs or source.pyproject_extras is not None):
            raise SourceError(
                "--deps applies to package names on the command line, requirements files, a conda "
                "environment.yml, pyproject.toml or a script's # /// script block; a lockfile already lists what "
                "it installs"
            )
        if offline:
            raise SourceError(
                "offline=True: deps=True would look packages up on PyPI; pass deps=False, or set offline=False"
            )
        instead = (
            ", so check its lock (uv lock --script) instead"
            if source.kind == "script"
            else ", so check a lockfile instead"
        )
        if not source.pypi_only:
            what = "the script" if source.kind == "script" else "the project"
            raise SourceError(
                f"{source.label}: {what} configures an installer index or source; --deps uses only PyPI{instead}"
            )
        if (env := origin.index_env()) is not None:
            raise SourceError(
                f"{source.label}: {env} may point pip or uv at another index, and --deps uses only PyPI" + instead
            )
        if not pinned_complete(source):
            # A compiled file already pins what these install; anything else would be resolved without them.
            if not allow_skipped and (skipped := [p for p in source.packages if p.skip]):
                raise SourceError(
                    f"{source.label}: deps=True can't check what non-index packages install: "
                    f"{skipped_list(skipped)}; pass allow_skipped=True to check the rest"
                    + ("" if source.kind == "script" else ", or check a lockfile")
                )
            if source.deps_blockers:
                raise SourceError(
                    f"{source.label}: --deps can't follow this the way pip does ({source.deps_blockers[0]}), "
                    "so check a lockfile instead (pip-compile, or uv pip compile)"
                )
            resolver = Resolver()  # no on-disk cache from the Python API, as check_packages(deps=True)
            resolve_deps(source, targets, resolver)
    else:
        check_report_targets(source, targets)
        if offline:
            require_offline([source], "offline=True", undo="set offline=False")

    pythons = tuple(dict.fromkeys(f"{t.major}.{t.minor}" for t in targets))
    fill_all(source.packages, refresh=online, pythons=pythons)
    # A pyproject.toml or a resolved tree has no lock to re-check (as the command line: its files are PyPI's own).
    lock = source.pyproject_extras is None and not (deps and not pinned_complete(source))
    if lock and not online and not offline and pythons_outside_lock(source, targets):
        recheck_lock(source.packages)
    apply_locked_python_ranges(source.packages, pythons, online=online)
    if online and source.uv_packages is not None:
        restore_uv_python_files(source.packages, pythons)
    rows: list[Row] = [r for r in check([source], targets) if r.needed]
    issues = explain_issues_urls(rows, [source])
    return (
        [
            _result(r.package, dict(r.results), pyodide_locks, r.via, r.direct_via, issues.get(id(r.package)))
            for r in rows
        ],
        source,
        rows,
    )
