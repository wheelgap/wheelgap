"""--explain --build-info: what a `source` build needs, from the [build-system] table of the sdist's pyproject.toml.

Only sdists of releases that positively come from PyPI (origin.known_pypi) are read, the file is
the one pip would read (see pypi.fetch_sdist_pyproject), and where any of that is in doubt there is no answer.
What is said is what the requirements prove: a requirement wheelgap doesn't map is shown by name, and "no compiler"
only follows from a backend that can't run one.
"""

from __future__ import annotations

import http.client
import posixpath
import re
import urllib.error
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidSdistFilename, canonicalize_name, parse_sdist_filename
from packaging.version import Version

from . import cache, origin, pypi
from .check import marker_holds
from .sources import Package, Source
from .targets import Target

RUST = "a Rust toolchain"
CMAKE = "CMake and a C/C++ compiler"
COMPILER = "a C/C++ compiler"
NEEDS = {
    "maturin": RUST,
    "setuptools-rust": RUST,
    "scikit-build-core": CMAKE,
    "scikit-build": CMAKE,
    "cmake": CMAKE,
    "cython": COMPILER,
    "meson-python": COMPILER,
}
POETRY = "poetry.core.masonry.api"
# Backends that can't run a compiler, and the requirements that provide them. poetry-core's only counts without a
# build script, and none of them in-tree.
PURE_BACKENDS = {
    "flit_core.buildapi": {"flit-core", "flit"},
    "flit.buildapi": {"flit"},
    POETRY: {"poetry-core"},
}
_BACKEND = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*(:[A-Za-z_]\w*(\.[A-Za-z_]\w*)*)?", re.ASCII)
_SPECIFIER = re.compile(r"[A-Za-z0-9.*+!=<>~,_-]*", re.ASCII)  # `===` takes any text: that isn't shown
PYPI_FILES = "files.pythonhosted.org"

TIMEOUT = 10.0  # seconds, per request
DEADLINE = 30.0  # seconds for all of them
WORKERS = 4

# (index, name, version, sha256 or ""): one lookup and one cache entry each. The sha256 is the lock's, so a lock
# can't answer for another lock's release; "" is a release whose sdist URL the index gives.
Release = tuple[str, str, str, str]


@dataclass(frozen=True)
class BuildInfo:
    requires: tuple[Requirement, ...]
    backend: str | None  # None: pip uses setuptools' legacy backend
    backend_path: bool  # an in-tree backend
    build_script: bool  # [tool.poetry] build: a script poetry-core runs, which may compile


def parse(pyproject: dict | None) -> dict | None:
    """What wheelgap keeps (and caches) from a pyproject.toml, or None where pip wouldn't use it as it stands: no
    [build-system] table (pip then uses setuptools), or one pip rejects, such as a requirement that doesn't parse."""
    system = pyproject.get("build-system") if isinstance(pyproject, dict) else None
    if not isinstance(system, dict):
        return None
    requires, backend, path = system.get("requires"), system.get("build-backend"), system.get("backend-path", [])
    if not (isinstance(requires, list) and all(isinstance(r, str) for r in requires)):
        return None
    if backend is not None and not (isinstance(backend, str) and _BACKEND.fullmatch(backend)):
        return None
    if not (isinstance(path, list) and all(isinstance(p, str) for p in path)):
        return None
    try:
        for text in requires:
            Requirement(text)
    except InvalidRequirement:
        return None
    tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    script = isinstance(poetry, dict) and "build" in poetry
    return {"requires": requires, "backend": backend, "backend_path": bool(path), "build_script": script}


def _info(answer: dict | None) -> BuildInfo | None:
    """A parse() answer, or a cached one, checked again: the cache file may have been edited."""
    if answer is None:
        return None
    backend = answer["backend"]
    if backend is not None and not _BACKEND.fullmatch(backend):
        return None
    try:
        requires = tuple(Requirement(text) for text in answer["requires"])
    except InvalidRequirement:
        return None
    return BuildInfo(requires, backend, answer["backend_path"], answer["build_script"])


SHOWN = 50  # requirements shown at most, then "and N more"
CLIP = 200  # characters of one requirement or backend name shown at most


def _clip(text: str) -> str:
    return text if len(text) <= CLIP else text[: CLIP - 1] + "…"


def shown(req: Requirement) -> str:
    """A requirement as wheelgap prints it: its normalized name, extras and specifiers (never the text as written),
    clipped to CLIP characters."""
    name: str = canonicalize_name(req.name)
    if req.extras:
        name += f"[{','.join(sorted(canonicalize_name(e) for e in req.extras))}]"
    if req.url:
        return _clip(f"{name} (direct URL)")
    spec = str(req.specifier)
    return _clip(name + spec if _SPECIFIER.fullmatch(spec) else name)


def requires_for(info: BuildInfo, targets: list[Target]) -> list[Requirement]:
    """The requirements whose marker holds (or may hold) on at least one of `targets`."""
    return [r for r in info.requires if r.marker is None or any(marker_holds(str(r.marker), t) for t in targets)]


def _pure(info: BuildInfo, requires: list[Requirement]) -> bool:
    """The backend can't run a compiler: a pure one, not in-tree, provided by a requirement from an index."""
    providers = PURE_BACKENDS.get(info.backend or "")
    if not providers or info.backend_path or (info.backend == POETRY and info.build_script):
        return False
    return any(canonicalize_name(r.name) in providers and not r.url for r in requires)


def describe(info: BuildInfo, targets: list[Target]) -> str | None:
    """'at least a Rust toolchain (maturin>=1,<2), setuptools', 'no compiler (flit-core>=3.2)', or None."""
    requires = requires_for(info, targets)
    needs: dict[str, list[str]] = {}
    others: list[str] = []
    for req in requires[:SHOWN]:
        label = NEEDS.get(canonicalize_name(req.name))
        (needs.setdefault(label, []) if label else others).append(shown(req))
    items = [
        f"{label} ({', '.join(dict.fromkeys(names))})"
        for label in (RUST, CMAKE, COMPILER)
        if (names := needs.get(label))
    ]
    items += dict.fromkeys(others)
    if len(requires) > SHOWN:
        items.append(f"and {len(requires) - SHOWN} more")
    if info.backend_path and info.backend:
        items.append(f"in-tree backend {_clip(info.backend)}")
    if not any(canonicalize_name(r.name) in NEEDS for r in requires) and _pure(info, requires):
        return f"no compiler ({', '.join(items)})"
    return f"at least {', '.join(items)}" if items else None


def _sdist(pkg: Package) -> str | None:
    """The release's one sdist, if it's a .tar.gz named for this release: with several, which one pip picks isn't
    certain."""
    sdists = [f for f in pkg.files or [] if not f.endswith(".whl")]
    if len(sdists) != 1 or not sdists[0].endswith(".tar.gz") or not pkg.version:
        return None
    try:
        name, version = parse_sdist_filename(sdists[0])
        if name != pkg.key or version != Version(pkg.version):
            return None
    except (InvalidSdistFilename, ValueError):  # InvalidVersion is a ValueError
        return None
    return sdists[0]


def _on_pypi_files(url: str) -> bool:
    """A lock's sdist URL is fetched only from PyPI's file host: a lockfile doesn't get to choose where wheelgap
    connects to. PyPI never reuses a filename, so there the filename is the file."""
    if not (url.isascii() and url.isprintable()) or " " in url:  # http.client would refuse it anyway
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.hostname == PYPI_FILES and parts.port in (None, 443)
    except ValueError:  # a malformed port or host
        return False


def where(
    pkg: Package, source: Source, simple_index: str | None = None, private: Collection[str] = frozenset()
) -> tuple[Release, str | None] | None:
    """(release, sdist URL) for a release whose sdist wheelgap may read, or None.

    The package must be positively PyPI's (origin.known_pypi, the rule --conda-forge follows too): never a pdm.lock
    entry, a poetry.lock or pixi.lock entry that records no index, a pylock.toml entry without an index, a private
    index or a direct URL, whose names may be private and whose PyPI namesakes are other packages; never a name another
    input of the run places elsewhere (`private`, from origin.private_names); and nothing in a run with --index-url or
    WHEELGAP_INDEX_URL. When the lock records an sdist URL (uv.lock, pylock.toml), that URL is used
    only if it's on PyPI's file host and names this release's sdist (otherwise None: no answer); when it records none
    (a poetry.lock or pixi.lock entry on pypi.org, say), the URL is None to ask PyPI's JSON API for it."""
    sdist = _sdist(pkg)
    if (
        sdist is None
        or not pkg.version
        or not origin.known_pypi(pkg, source, simple_index=simple_index, private=private)
    ):
        return None
    if pkg.sdist_url and isinstance(pkg.registry, str):  # known_pypi says that registry is PyPI's
        if unquote(posixpath.basename(urlsplit(pkg.sdist_url).path)) != sdist or not _on_pypi_files(pkg.sdist_url):
            return None
        return (pkg.registry.rstrip("/"), pkg.key, pkg.version, pkg.sdist_sha256 or ""), pkg.sdist_url
    return (pypi.index_url(), pkg.key, pkg.version, ""), None


class _Phase:
    """Shared by the lookups of one run: a deadline, and why the lookups stopped, if they did."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.stopped: str | None = None  # "time" or "connection"
        self.not_read = 0

    def stop(self, why: str) -> None:
        self.stopped = self.stopped or why
        self.not_read += 1

    def timeout(self) -> float:
        return max(0.1, min(TIMEOUT, self.deadline - pypi._clock()))


def _locate(release: Release, sdist: str, phase: _Phase) -> tuple[str | None, str | None]:
    """The sdist's URL and sha256 from the JSON API (the cached file lists keep no URLs)."""
    index, name, version, _ = release
    url = f"{index}/{quote(name)}/{quote(version)}/json"
    data = pypi.fetch_json(url, timeout=phase.timeout(), attempts=1, deadline=phase.deadline)
    entry = next((u for u in data["urls"] if isinstance(u, dict) and u.get("filename") == sdist), None)
    if entry is None or not isinstance(entry.get("url"), str):
        return None, None
    digests = entry.get("digests")
    sha256 = digests.get("sha256") if isinstance(digests, dict) else None
    return entry["url"], sha256 if isinstance(sha256, str) else None


def lookup(
    pkg: Package,
    source: Source,
    phase: _Phase,
    read_cache: bool = False,
    write_cache: bool = False,
    simple_index: str | None = None,
    private: Collection[str] = frozenset(),
) -> BuildInfo | None:
    """A release's build info, or None. Only an answer from a download that matched its sha256 is cached (a lock's
    URL with no sha256 never is); a network failure stops the lookups still to come."""
    found, sdist = where(pkg, source, simple_index, private), _sdist(pkg)
    if found is None or sdist is None:
        return None
    release, url = found
    if read_cache and (hit := cache.load_build(*release)) is not None:
        return _info(hit["answer"])
    if phase.stopped:
        phase.not_read += 1
        return None
    if pypi._clock() >= phase.deadline:
        phase.stop("time")
        return None
    sha256 = release[3] or None
    try:
        if url is None:
            url, sha256 = _locate(release, sdist, phase)
            if url is None:
                return None
        pyproject, verified = pypi.fetch_sdist_pyproject(url, sha256, phase.deadline, phase.timeout())
        answer = parse(pyproject)
    except pypi.SdistRefused as e:
        answer, verified = None, e.verified
    except pypi.DeadlineExceeded:
        phase.stop("time")
        return None
    except urllib.error.HTTPError:  # the server answered: the others may still work
        return None
    except (http.client.InvalidURL, ValueError):  # this package's URL, not the network: the others may still work
        return None
    except (OSError, http.client.HTTPException):  # no connection, a timeout, a refused redirect: stop there
        phase.stop("connection")
        return None
    except Exception:  # a hash mismatch, a truncated download, an odd JSON answer: no answer, and not cached
        return None
    if write_cache and verified:  # a lock's URL without a sha256 never is
        cache.save_build(*release, answer)
    return _info(answer)


def lookup_all(
    packages: list[tuple[Package, Source]],
    read_cache: bool = False,
    write_cache: bool = False,
    simple_index: str | None = None,
    private: Collection[str] = frozenset(),
) -> tuple[dict[int, BuildInfo], str | None]:
    """Build info by id() of each (package, the source it was read from), one lookup per release (see where)
    however many targets or inputs list it, at most WORKERS at a time and DEADLINE seconds in all. Also returns a
    note when the lookups stopped early, else None."""
    todo: dict[Release, tuple[Package, Source]] = {}
    release_of: dict[int, Release] = {}
    for p, s in packages:
        if (found := where(p, s, simple_index, private)) is not None:
            release_of[id(p)] = found[0]
            todo.setdefault(found[0], (p, s))
    start = pypi._clock()
    phase = _Phase(start + DEADLINE)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        infos = list(
            pool.map(
                lambda ps: lookup(ps[0], ps[1], phase, read_cache, write_cache, simple_index, private), todo.values()
            )
        )
    by_release = {release: info for release, info in zip(todo, infos, strict=True) if info is not None}
    found_infos = {key: by_release[r] for key, r in release_of.items() if r in by_release}
    if phase.stopped is None:
        return found_infos, None
    why = f"after {pypi._clock() - start:.0f} s" if phase.stopped == "time" else "after a connection failure"
    n = phase.not_read
    return found_infos, f"build info: stopped {why}; {n} package{'s' * (n != 1)} not read"
