"""Python API: check packages and lockfiles without going through the command line.

import wheelgap

targets = wheelgap.make_targets(python="3.13", platforms=["linux-aarch64", "musl-aarch64"])
result = wheelgap.check_package("numpy", "2.3.4", targets=targets)
result.ok                   # True if every target has a wheel
result.statuses[targets[1]] # Status.WHEEL / SOURCE / MISSING / ...
result.reasons.get(targets[1])  # for a source or missing target: the wheels --explain lists and why they don't fit
result.to_dict()            # JSON-friendly, keyed by "3.13 musl-aarch64"; explain=True adds rejected and via
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

from .check import Row, Status, check, explain, explain_lines, status_for, wheel_tags
from .deps import resolve_deps
from .offline import require_offline
from .pyodide import PyodideLock, bundled_version_differs, bundles, check_locks
from .pypi import fill_all, recheck_lock
from .sources import (
    Package,
    Source,
    SourceError,
    check_report_targets,
    find_lockfile,
    load_file,
    pythons_outside_lock,
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
    # Per SOURCE or MISSING target, the dependency path from a root down to this package, as --explain shows it:
    # ("app", "pandas", "numpy"), or ("(dev group)", "pytest") for a dependency group. Only check_lockfile() fills
    # it, and only for inputs with a dependency graph (uv.lock, and poetry.lock before lock-version 2.1).
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
        """JSON-friendly, keyed by target label. explain=True adds "rejected" and "via" as `wheelgap --json --explain`
        does: each SOURCE or MISSING target's rejected wheels as {"file", "reason"} objects, and its dependency path
        where there is one."""
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
        if explain:
            out["rejected"] = {
                t.label(True): [{"file": f, "reason": why} for f, why in rejected]
                for t, rejected in self.rejected.items()
            }
            out["via"] = {t.label(True): list(path) for t, path in self.via.items()}
        return out


def _result(
    pkg: Package,
    statuses: dict[Target, Status],
    pyodide_locks: Sequence[PyodideLock] = (),
    via: dict[Target, list[str]] | None = None,
) -> Result:
    applied = [lock for lock in pyodide_locks if any(bundles(lock, pkg.key, t, s) for t, s in statuses.items())]
    statuses = {
        t: Status.BUNDLED if any(bundles(lock, pkg.key, t, s) for lock in applied) else s for t, s in statuses.items()
    }
    bundled_versions = {f"{lock.python[0]}.{lock.python[1]}": lock.packages[pkg.key] for lock in applied}
    unique_versions = set(bundled_versions.values())
    shared_version = next(iter(unique_versions)) if len(unique_versions) == 1 else None
    via = via or {}
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
        {t: tuple(via[t]) for t, s in statuses.items() if s in _EXPLAINED and t in via},
        # A file's yanked flag and its Requires-Python reason are the same for every Python, so these are the union.
        frozenset(f for variant in pkg.variants for f in variant.yanked),
        {f: why for variant in pkg.variants for f, why in variant.requires_python_rejected.items()},
        pkg.no_binary,
        pkg.only_binary,
        bundled_versions=bundled_versions,
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
        source = Source("api", pkgs, specs=[p.name if p.version is None else f"{p.name}=={p.version}" for p in pkgs])
        resolve_deps(source, targets)
        fill_all(source.packages, workers=workers, pythons=pythons)
        return [_result(r.package, dict(r.results), pyodide_locks, r.via) for r in check([source], targets) if r.needed]
    fill_all(pkgs, workers=workers, pythons=pythons)
    return [
        _result(
            p,
            {
                t: status_for(None if resolved.error else resolved.files, t, yanked=resolved.yanked)
                for t in targets
                for resolved in [p.resolutions.get(f"{t.major}.{t.minor}", p)]
            },
            pyodide_locks,
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
) -> list[Result]:
    """Check every package a lockfile (or a project directory) installs on at least one target.

    Environment markers and the uv.lock dependency graph are respected, as in the CLI. A uv.lock whose
    requires-python leaves out a target's Python may be missing wheels for it, so its PyPI packages are also
    looked up on the index (keeping the lock's files if a lookup fails).
    Requirements ranges resolve separately for each target Python major/minor. Distinct resolutions return
    separate Results with their own version and eligible files; targets using another resolution are NOT_NEEDED.
    With offline=True, only locked files are used: inputs needing an index lookup raise SourceError, and the
    other-Python recheck is skipped. Locked files may therefore omit wheels for those Python versions.
    online=True and offline=True cannot be combined.
    pyodide_locks work as in check_packages(), and are checked before the lockfile is read.
    environment picks a pixi.lock environment (default: `default`); in a project directory it makes pixi.lock the
    lockfile read. Naming one for any other lockfile raises SourceError.
    A pip installation report (`pip install --dry-run --report`) is resolved for one environment, so it raises
    SourceError unless every target matches that environment's platform, machine, Python and implementation.
    """
    return _check_lockfile(
        path, targets, online=online, offline=offline, pyodide_locks=pyodide_locks, environment=environment
    )[0]


def _check_lockfile(
    path: str | Path,
    targets: Sequence[Target] | None,
    *,
    online: bool,
    offline: bool,
    pyodide_locks: Iterable[PyodideLock] = (),
    environment: str | None = None,
) -> tuple[list[Result], Source, list[Row]]:
    """check_lockfile(), also returning the source and check rows for diff's skips and suggestions."""
    targets = _package_targets(targets)
    pyodide_locks = tuple(pyodide_locks)
    if online and offline:
        raise ValueError("online=True and offline=True cannot be combined")
    check_locks(pyodide_locks, targets)
    p = Path(path)
    lockfile = find_lockfile(p, environment is not None) if p.is_dir() else p
    if lockfile.name == "pyproject.toml":  # direct dependencies only; the command line says so in a note
        raise SourceError(
            f"{lockfile}: a pyproject.toml lists direct dependencies only, so check_lockfile() and "
            "wheelgap.diff don't read it; pass a lockfile (the wheelgap command checks a pyproject.toml)"
        )
    source: Source = load_file(lockfile, environment)
    if environment is not None and source.kind != "pixi":
        raise SourceError(f"{lockfile}: environment applies to pixi.lock only")
    check_report_targets(source, targets)
    if offline:
        require_offline([source], "offline=True", undo="set offline=False")
    fill_all(source.packages, refresh=online, pythons=tuple(dict.fromkeys(f"{t.major}.{t.minor}" for t in targets)))
    if not online and not offline and pythons_outside_lock(source, targets):
        recheck_lock(source.packages)
    rows: list[Row] = [r for r in check([source], targets) if r.needed]
    return [_result(r.package, dict(r.results), pyodide_locks, r.via) for r in rows], source, rows
