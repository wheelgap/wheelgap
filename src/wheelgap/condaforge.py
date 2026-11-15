"""--conda-forge: say when conda-forge has a package of the same name with a build that fits a gap's target.

Only a hint under --explain: a conda-forge build doesn't make `pip install` work, and a conda package of the same name
may be another project. So it never changes a status, the summary or the exit code.

A build counts for a target when all of these hold (see fits()):
- it's of the checked version, on conda-forge's main label and not marked broken;
- it's for the target's conda subdir, or noarch;
- its virtual-package dependencies hold there: __glibc and __osx minimums against the target's glibc and macOS
  versions, and a noarch build's __win, __unix, __linux, __osx or __glibc limit it to those OSes (any other virtual
  package, such as __cuda, can't be told, so the build doesn't count);
- it's for the target's Python: the same CPython minor version and ABI (free-threaded or not) by its python_abi
  dependency; an abi3 build (_python_abi3_support) for any non-free-threaded CPython its cpython constraint admits;
  a noarch build whose python constraints admit that Python.
A build whose Python can't be told never counts.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote

from packaging.specifiers import InvalidSpecifier, Specifier
from packaging.version import InvalidVersion, Version

from . import cache, pypi
from .sources import Package
from .targets import PLATFORMS, Target

# One package's files on anaconda.org, each with its version, labels and attrs (subdir, depends).
API = "https://api.anaconda.org/package/conda-forge"
# Uncompressed, numpy's answer is about 7.5 MB and pyarrow's over 60 MB; gzipped (streamed), about a sixteenth.
# Parsed, an answer takes about 3.5 times its size, so with 3 workers the worst case is about 3 x 16 x 4.5 MB, under
# 250 MB. Past the cap the package gets no hint, and that's cached like an answer.
MAX_BYTES = 16 * 1024 * 1024
WORKERS = 3
# Seconds per socket operation, and each request is tried once: a hint isn't worth a retry. anaconda.org can take
# 6 to 16 seconds to start answering for a big package such as numpy.
TIMEOUT = 15.0
DEADLINE = 30.0  # seconds for all the lookups of a run; past it, the rest are dropped
NOARCH = "noarch"
TOO_LARGE = cache.TOO_LARGE
# Why a lookup gave no usable answer, as the stderr note says it.
TOO_LARGE_ANSWER = f"answer too large (over {MAX_BYTES // (1024 * 1024)} MB)"
FAILED = "no usable answer"
DROPPED = "not asked: anaconda.org couldn't be reached, or was too slow"

# wheelgap platform -> conda subdir. Only the subdirs where conda-forge builds current CPython: it has no Python for
# linux-32, linux-armv7l or linux-s390x, and win-32 stopped at 3.6. musl, Pyodide, iOS and Android never match.
SUBDIRS = {
    "linux-x86_64": "linux-64",
    "linux-aarch64": "linux-aarch64",
    "linux-ppc64le": "linux-ppc64le",
    "linux-riscv64": "linux-riscv64",
    "macos-x86_64": "osx-64",
    "macos-arm64": "osx-arm64",
    "win-amd64": "win-64",
    "win-arm64": "win-arm64",
}
# A build that depends on one of these virtual packages installs only on those OSes (wheelgap's families).
VIRTUAL_OS = {
    "__win": {"windows"},
    "__unix": {"manylinux", "macos"},
    "__linux": {"manylinux"},
    "__glibc": {"manylinux"},
    "__osx": {"macos"},
}

# A build, cut down to what matching needs:
# - subdir;
# - abi: python_abi's build string (cp314, cp314t), "abi3" for an abi3 build, or None;
# - python: the version constraints of its python (and, for abi3, cpython) dependencies, or None when it has none;
# - os: the VIRTUAL_OS names it depends on;
# - glibc, osx: the version constraint of its __glibc / __osx dependency ("" for none), or None without one;
# - unknown: it depends on something wheelgap can't check: a virtual package such as __cuda or __archspec, or a
#   python that isn't CPython (a PyPy build string such as *_73_pypy).
Build = dict
BUILD_KEYS = {"subdir", "abi", "python", "os", "glibc", "osx", "unknown"}
# The build strings of CPython itself in a `python` dependency: *_cpython, *_cp313, *_cp313t.
_CPYTHON_BUILD = re.compile(r"\*?_?cp(ython|\d+t?)")


class Unreachable(Exception):
    """anaconda.org couldn't be reached (DNS, a refused or timed-out connection): the other lookups would fail too."""


def wanted(targets: Iterable[Target]) -> bool:
    """Whether any of these gap targets could match a conda-forge build."""
    return any(t.platform in SUBDIRS for t in targets)


def _same_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def _build(f: object, version: str) -> Build | None:
    """A file of `version` on the main label and not marked broken, cut down to a Build; None for any other file."""
    if not isinstance(f, dict):
        return None
    attrs, labels, v = f.get("attrs"), f.get("labels"), f.get("version")
    if not (isinstance(attrs, dict) and isinstance(labels, list) and isinstance(v, str)):
        return None
    if not isinstance(attrs.get("subdir"), str) or "main" not in labels or "broken" in labels:
        return None
    if not _same_version(v, version):
        return None
    depends = attrs.get("depends")
    build: Build = {
        "subdir": attrs["subdir"],
        "abi": None,
        "python": None,
        "os": [],
        "glibc": None,
        "osx": None,
        "unknown": False,
    }
    abi3, cpython = False, []
    for dep in depends if isinstance(depends, list) else []:
        parts = dep.split() if isinstance(dep, str) else []
        if not parts:
            continue
        name, spec = parts[0], parts[1:2]  # [name, version, build]: the build string isn't a version constraint
        if name == "python_abi" and len(parts) >= 3:
            build["abi"] = parts[2].removeprefix("*_")
        elif name == "python":
            build["python"] = (build["python"] or []) + spec  # a bare `python` adds no constraint
            if len(parts) >= 3 and not _CPYTHON_BUILD.fullmatch(parts[2]):
                build["unknown"] = True  # PyPy, or another interpreter
        elif name == "cpython":
            cpython += spec
        elif name == "_python_abi3_support":
            abi3 = True
        elif name in VIRTUAL_OS:
            build["os"] = sorted({*build["os"], name})
            if name in ("__glibc", "__osx"):
                build[name[2:]] = ",".join(filter(None, [build[name[2:]], *spec]))
        elif name.startswith("__"):
            build["unknown"] = True
    if abi3 and build["abi"] is None:
        build["abi"] = "abi3"
        build["python"] = (build["python"] or []) + cpython
    return build


def parse_builds(data: object, version: str) -> list[Build]:
    """The builds of `version`, from an anaconda.org package answer."""
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        raise ValueError("unexpected response")
    builds = (b for f in files if (b := _build(f, version)) is not None)
    return list({json.dumps(b, sort_keys=True): b for b in builds}.values())  # one per kind of build


_PART = re.compile(r"(>=|<=|==|!=|~=|>|<|=)?\s*([0-9][0-9A-Za-z.]*?)?(\.?\*)?")


def _part(text: str):
    """One conda version constraint (no `,` or `|`) as a test of a Version, and the version it names; None when it
    can't be told. Globs and bare versions are prefix matches (conda reads `3.9` and `=3.9` as `3.9.*`); the rest
    are packaging specifiers."""
    m = _PART.fullmatch(text.strip())
    if not m or not (m.group(2) or m.group(3)):
        return None
    op, ver, star = m.groups()
    if ver is None:  # `*`: any version
        return (lambda v: True), None
    try:
        named = Version(ver)
    except InvalidVersion:
        return None
    if star or op in (None, "="):
        if op not in (None, "=", "==", "!="):
            return None  # `>=3.9.*` and the like
        comps = ver.split(".")
        if not all(c.isdigit() for c in comps):
            return None
        prefix = [int(c) for c in comps]

        def glob(v: Version) -> bool:
            return list(v.release[: len(prefix)]) + [0] * (len(prefix) - len(v.release)) == prefix

        return ((lambda v: not glob(v)) if op == "!=" else glob), named
    try:
        spec = Specifier(f"{op}{ver}")
    except InvalidSpecifier:
        return None
    return (lambda v: spec.contains(v, prereleases=True)), named


def _holds_for(spec: str, candidates) -> bool | None:
    """Whether some candidate Version meets a conda constraint (`,` is and, `|` is or); None when it can't be told."""
    found = False
    for alt in spec.split("|"):
        parts = [_part(p) for p in alt.split(",")]
        if any(p is None for p in parts):
            return None
        tests = [p[0] for p in parts if p is not None]
        named = [p[1] for p in parts if p is not None and p[1] is not None]
        found = found or any(all(t(c) for t in tests) for c in candidates(named))
    return found


def _holds(spec: str, major: int, minor: int) -> bool | None:
    """Whether a conda version constraint (`>=3.10`, `3.13.*`, `>=2.7,<2.8.0a0`, `3.6|3.7`, `~=3.9.0`) admits some
    X.Y.z release of Python X.Y. None when it can't be told."""

    def candidates(named: list[Version]) -> list[Version]:
        # The constraints can only change at the patch releases they name, so these cover every case.
        patches = {0, 99} | {n.micro + d for n in named if n.release[:2] == (major, minor) for d in (-1, 0, 1)}
        return [Version(f"{major}.{minor}.{p}") for p in sorted(patches) if p >= 0]

    return _holds_for(spec, candidates)


def _admits(spec: str, version: tuple[int, int]) -> bool:
    """Whether a __glibc / __osx constraint holds for exactly this version ("" holds for any)."""
    return not spec or _holds_for(spec, lambda named: [Version(f"{version[0]}.{version[1]}")]) is True


def fits(build: Build, target: Target) -> bool:
    """Whether a build installs on the target's conda subdir and Python (see the module docstring)."""
    own = SUBDIRS.get(target.platform)
    if own is None or build["subdir"] not in (own, NOARCH) or build["unknown"]:
        return False
    family = PLATFORMS[target.platform][0]
    if not all(family in VIRTUAL_OS[name] for name in build["os"]):
        return False
    if build["glibc"] is not None and not _admits(build["glibc"], target.glibc):
        return False
    if build["osx"] is not None and not _admits(build["osx"], target.macos):
        return False
    python, abi = build["python"], build["abi"]
    if python is None and abi is None:
        return False  # not a Python build, or one we can't place
    if any(_holds(spec, target.major, target.minor) is not True for spec in python or []):
        return False
    if abi == "abi3":
        return bool(python) and not target.freethreaded  # the stable ABI isn't free-threaded's
    if abi is not None:
        return abi == f"cp{target.major}{target.minor}{'t' if target.freethreaded else ''}"
    if build["subdir"] == NOARCH:
        return True  # pure Python: any CPython its constraints admit, free-threaded too
    # A compiled build with no python_abi (older ones): its python constraint gives the version, and it's never
    # free-threaded; with no constraint at all, its Python can't be told.
    return bool(python) and not target.freethreaded


def fetch(key: str, version: str) -> list[Build] | str | None:
    """Ask anaconda.org: the builds of `version` ([] for no such package or version), TOO_LARGE, or None for an answer
    that can't be used (403, 429, 5xx, too slow, damaged). Raises Unreachable when anaconda.org can't be reached."""
    try:
        data = pypi.fetch_json(f"{API}/{quote(key, safe='')}", timeout=TIMEOUT, attempts=1, max_bytes=MAX_BYTES)
    except urllib.error.HTTPError as e:
        return [] if e.code == 404 else None  # only a 404 says there's no such package
    except pypi.ResponseTooLarge:
        return TOO_LARGE
    except urllib.error.URLError as e:  # urllib's wrapper for failing to connect at all
        raise Unreachable from e
    except Exception:  # a slow or cut-off answer (TimeoutError, a reset), a damaged body, bad JSON
        return None
    try:
        return parse_builds(data, version)
    except ValueError:
        return None


def look_up(key: str, version: str, read_cache: bool = False, write_cache: bool = False) -> list[Build] | str | None:
    """fetch(), through the 24-hour cache. Answers (builds, none, too large) are cached; failures aren't."""
    if read_cache and (cached := cache.load_builds(key, version)) is not None:
        return cached
    found = fetch(key, version)
    if found is not None and write_cache:
        cache.save_builds(key, version, found)
    return found


@dataclass
class Lookups:
    found: dict[tuple[str, str], list[Build]]  # (name, version) -> builds, for the lookups that gave an answer
    unchecked: dict[str, str]  # name -> why it has no usable answer: TOO_LARGE, FAILED or DROPPED
    stopped: bool  # anaconda.org couldn't be reached, or the deadline passed, so some lookups were dropped


def look_up_all(
    wanted: Iterable[tuple[str, str]], read_cache: bool = False, write_cache: bool = False, deadline: float = DEADLINE
) -> Lookups:
    """Look up each (name, version), at most WORKERS at a time. The first lookup that can't reach anaconda.org stops the
    rest, and so does the deadline. Workers are daemon threads, so a stalled request can't hold up the run's exit."""
    todo: deque[tuple[str, str]] = deque(dict.fromkeys(wanted))
    asked = list(todo)
    results: dict[tuple[str, str], list[Build] | str | None] = {}
    lock = threading.Lock()
    finished, stop = threading.Event(), threading.Event()

    def worker() -> None:
        while not stop.is_set():
            with lock:
                if not todo:
                    return
                item = todo.popleft()
            try:
                result = look_up(*item, read_cache, write_cache)
            except Unreachable:
                stop.set()
                finished.set()
                return
            with lock:
                if not stop.is_set():
                    results[item] = result
                    if len(results) == len(asked):
                        finished.set()

    if asked:
        start = time.monotonic()
        for _ in range(min(WORKERS, len(asked))):
            threading.Thread(target=worker, daemon=True).start()
        finished.wait(max(0.0, deadline - (time.monotonic() - start)))
        stop.set()
    with lock:
        found = {item: r for item, r in results.items() if isinstance(r, list)}
        unchecked: dict[str, str] = {}
        for item in asked:
            if item not in found:
                result = results.get(item)
                unchecked.setdefault(
                    item[0], TOO_LARGE_ANSWER if result == TOO_LARGE else FAILED if item in results else DROPPED
                )
        return Lookups(found, unchecked, len(results) < len(asked))


@dataclass
class Hint:
    name: str  # the conda-forge name looked up: the canonical PyPI name
    version: str  # the checked version, which every build counted is of
    builds: dict[Target, list[str]]  # gap target -> the subdirs of the builds that fit it (its own and/or noarch)


def hint(pkg: Package, targets: Iterable[Target], found: dict[tuple[str, str], list[Build]]) -> Hint | None:
    """The hint for a package's gap targets, or None when no build fits any of them. The caller checks that the
    package is PyPI's (origin.known_pypi)."""
    if not pkg.version or not (builds := found.get((pkg.key, pkg.version))):
        return None
    matched = {}
    for t in targets:
        subdirs = sorted({b["subdir"] for b in builds if fits(b, t)}, key=lambda s: s == NOARCH)
        if subdirs:
            matched[t] = subdirs
    return Hint(pkg.key, pkg.version, matched) if matched else None


def hint_text(pkg: Package, h: Hint, multi_py: bool) -> str:
    """`numpy: conda-forge has a package named numpy with 2.3.4 builds for linux-riscv64, win-arm64 (noarch)`."""
    labels = [t.label(multi_py) + (" (noarch)" if subdirs == [NOARCH] else "") for t, subdirs in h.builds.items()]
    return f"{pkg.name}: conda-forge has a package named {h.name} with {h.version} builds for {', '.join(labels)}"
