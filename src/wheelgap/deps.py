"""--deps: what the package names on the command line install, read from PyPI's metadata (#353).

For each target, a small resolver picks a release the way pip does without backtracking: for each requirement, the
newest release that meets every constraint on it, supports the target's Python and isn't yanked (a yanked release
only for an exact `==` pin). It follows markers and extras as pip evaluates them on that target, so a dependency
that only Windows installs is only checked on Windows targets, and `pyjwt[crypto]` brings in cryptography while
`pyjwt` doesn't.

It isn't pip's resolver, so it refuses rather than guesses. When a later constraint rules out a release it already
picked, it starts that target again with the constraint known from the start. A constraint it learned that the
finished tree doesn't impose is dropped and the tree resolved again, so a learned constraint never holds a package
back. If the constraints on a package can't all be met, or the rounds go in a circle, it's a DepsError: check a
lockfile instead.

The answer per target is which packages are installed and the dependency path to each (Source.deps_paths); check()
uses it in place of a lockfile's graph. Each release picked becomes a Package with no files, for fill_all to look up.
"""

from __future__ import annotations

import email.parser
import itertools
import urllib.error
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import NormalizedName, canonicalize_name
from packaging.version import InvalidVersion, Version

from . import cache, pypi
from ._download_pool import DownloadPool
from .check import SDIST_SUFFIXES, marker_holds, marker_value, rejection
from .cutoff import describe
from .sources import Package, Source, SourceError
from .targets import Target

MAX_ROUNDS = 50  # restarts per target before giving up
MAX_PACKAGES = 2000  # in one target's tree: past this, something is wrong
METADATA_MAX_BYTES = 1024 * 1024  # a wheel's METADATA file
PREFETCH_WORKERS = 16
PREFETCH_SCAN = 256  # queued requirements looked at per pop when planning prefetches


class DepsError(SourceError):
    """The requirements can't be resolved without a resolver that backtracks, or not at all."""


@dataclass
class _Conflict:
    key: str
    spec: str


@dataclass
class _Tree:
    chosen: dict[str, str] = field(default_factory=dict)  # key -> version
    errors: dict[str, str] = field(default_factory=dict)  # key -> why it has no version
    paths: dict[str, list[str]] = field(default_factory=dict)  # key -> names from a top-level requirement down
    imposed: dict[str, set[str]] = field(default_factory=dict)  # key -> specifiers on it from installed edges
    # key -> (the installed package whose requirement it is, its specifier), for --fix; top-level requirements left out
    imposers: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


class Resolver:
    """Resolves requirements per target. Index answers are kept for the run, so later targets cost no requests."""

    def __init__(self, read_cache: bool = False, write_cache: bool = False, *, prefetch: bool = True) -> None:
        # The on-disk cache of wheel metadata files (cache.py): a release's Requires-Dist never changes once it's
        # uploaded. Project pages (the release lists) change with every upload, so they're never cached.
        self.read_cache, self.write_cache = read_cache, write_cache
        self._projects: dict[str, dict | Exception] = {}
        self._requires: dict[tuple[str, str, str], list[Requirement] | Exception] = {}
        self.names: dict[str, str] = {}  # key -> the name the index gives it
        # A requirements file's --no-binary/--only-binary policy (key -> (no_binary, only_binary)), set by
        # resolve_deps for the source being resolved: pip picks releases under it, so pick() does too.
        self.policy: Callable[[str], tuple[bool, bool]] | None = None
        self._metadata: dict[str, list[str] | Exception] = {}  # a wheel's .metadata URL -> its Requires-Dist
        # The source's upload cutoff per package (Source.cutoff_for, #521), also set by resolve_deps: uv leaves out
        # every file uploaded at or after it, an exact pin's too.
        self.cutoff: Callable[[str], datetime | None] | None = None
        self.prefetch = prefetch
        self._pool: DownloadPool | None = None
        self._project_jobs: dict[str, Future] = {}
        self._metadata_jobs: dict[str, Future] = {}
        self._release_jobs: dict[tuple[str, str], Future] = {}
        self._planned: set[tuple[str, tuple[str, ...], Target]] = set()

    def _cutoff(self, key: str) -> datetime | None:
        return self.cutoff(key) if self.cutoff else None

    def _project(self, key: str) -> dict:
        if key not in self._projects:
            try:
                job = self._project_jobs.get(key)
                data = (
                    job.result()
                    if job
                    else pypi.fetch_json(f"{pypi.index_url()}/{quote(key)}/json", max_bytes=pypi.RELEASES_MAX_BYTES)
                )
                if not isinstance(data, dict) or not isinstance(data.get("releases"), dict):
                    raise ValueError("the index gave no release list")
                info = data.get("info")
                if isinstance(info, dict) and isinstance(info.get("name"), str):
                    self.names.setdefault(key, info["name"])
                self._projects[key] = data
            except Exception as e:  # reported on the package, as for any lookup
                self._projects[key] = e
        found = self._projects[key]
        if isinstance(found, Exception):
            raise found
        return found

    def _read_metadata(self, wheel: dict) -> list[str]:
        """One worker's lookup: no resolver dictionaries are changed from a worker thread."""
        url = wheel["url"] + ".metadata"
        if self.read_cache:
            if (cached := cache.load_metadata(pypi.index_url(), url)) is not None:
                return cached
        try:
            text = pypi.fetch_text(url, max_bytes=METADATA_MAX_BYTES)
        except urllib.error.HTTPError as e:
            raise ValueError(f"no metadata file for {wheel['filename']} (HTTP {e.code})") from e
        parsed = email.parser.Parser().parsestr(text)
        requires = parsed.get_all("Requires-Dist") or []
        # Only a real metadata file is kept: a proxy's or captive portal's page answers 200 too, and would otherwise
        # be cached as "no dependencies" for a day.
        if self.write_cache and parsed.get("Metadata-Version"):
            cache.save_metadata(pypi.index_url(), url, requires)
        return requires

    def _wheel_requires(self, wheel: dict) -> list[str]:
        url = wheel["url"] + ".metadata"
        if url not in self._metadata:
            try:
                job = self._metadata_jobs.get(url)
                self._metadata[url] = job.result() if job else self._read_metadata(wheel)
            except Exception as e:
                self._metadata[url] = e
        found = self._metadata[url]
        if isinstance(found, Exception):
            raise found
        return found

    def _wheels(self, key: str, version: str, target: Target) -> list[dict]:
        """The wheels whose metadata determines this release's dependencies on the target."""
        files = pypi._files(self._project(key)["releases"].get(version, []))
        files = pypi.before_cutoff(files, self._cutoff(key))
        files = [e for e in files if not e.get("yanked")] or files  # every file of a yanked release is yanked
        wheels = [e for e in files if e["filename"].endswith(".whl") and isinstance(e.get("url"), str)]
        own = [e for e in wheels if rejection(e["filename"], target) is None]
        pure = [e for e in wheels if e["filename"].endswith("-none-any.whl")]
        if own or pure:
            chosen = [(own or pure)[0]]
        else:
            # One wheel per operating system, the target's own first (win_amd64 for win-arm64), so the two that must
            # agree aren't, say, two Linux wheels.
            by_os: dict[str, dict] = {}
            for e in wheels:
                by_os.setdefault(_os_family(e["filename"].removesuffix(".whl").rsplit("-", 1)[-1]), e)
            mine = _os_family(target.platform.replace("-", "_"))
            chosen = sorted(by_os.values(), key=lambda e: _os_family(e["filename"].rsplit("-", 1)[-1]) != mine)[:2]
        return chosen

    def requires(self, key: str, version: str, target: Target) -> list[Requirement]:
        """The release's Requires-Dist, from a wheel's own metadata (PEP 658): the wheel the target installs if there
        is one, else a pure one. A target that builds from source with neither reads two other platforms' wheels and
        takes their answer only if they agree, since an old setup.py can write platform-specific dependencies into
        each wheel without markers. PyPI's JSON `requires_dist` is read from whichever file was uploaded first, so it
        can be empty when the wheels declare dependencies; it's used only for a release with no wheel, and when it's
        missing there too the dependencies are unknown."""
        chosen = self._wheels(key, version, target)
        cache_key = (key, version, ",".join(e["filename"] for e in chosen))
        if cache_key not in self._requires:
            try:
                listed: object
                if chosen:
                    answers = [self._wheel_requires(e) for e in chosen]
                    if any(sorted(a) != sorted(answers[0]) for a in answers):
                        raise ValueError(
                            "its wheels for other platforms list different dependencies, so what a build from source "
                            "here installs can't be told"
                        )
                    listed = answers[0]
                else:
                    job = self._release_jobs.get((key, version))
                    data = (
                        job.result()
                        if job
                        else pypi.fetch_json(f"{pypi.index_url()}/{quote(key)}/{quote(version)}/json")
                    )
                    info = data.get("info") if isinstance(data, dict) else None
                    listed = info.get("requires_dist") if isinstance(info, dict) else None
                    if listed is None:
                        # An old sdist's metadata may leave them out even when setup.py has some.
                        raise ValueError("PyPI lists no dependencies for it and it has no wheel to confirm that")
                if not isinstance(listed, list) or not all(isinstance(r, str) for r in listed):
                    raise ValueError("the index gave dependencies that aren't a list of strings")
                self._requires[cache_key] = [Requirement(r) for r in listed]
            except InvalidRequirement as e:
                self._requires[cache_key] = ValueError(f"a dependency doesn't parse: {e}")
            except Exception as e:
                self._requires[cache_key] = e
        found = self._requires[cache_key]
        if isinstance(found, Exception):
            raise found
        return found

    def _prefetch(
        self,
        queue: deque[tuple[Requirement, frozenset[str], list[str]]],
        tree: _Tree,
        learned: dict[str, set[str]],
        target: Target,
    ) -> None:
        if self._pool is None:
            return
        batch: dict[NormalizedName, list[str]] = {}
        doomed: set[str] = set()  # names the walk will record as a direct URL error, so never looks up
        # A shorter prefix is still certain to be visited; bounding it keeps each pop's scan cheap on a long queue.
        for req, parent_extras, _ in itertools.islice(queue, PREFETCH_SCAN):
            if req.marker is not None and not marker_holds(str(req.marker), target, extras=parent_extras):
                continue
            key = canonicalize_name(req.name)
            if key in tree.errors or key in doomed:
                continue
            if req.url:
                if key not in tree.chosen and key not in batch:
                    doomed.add(key)
                continue
            if key in tree.chosen:
                if req.specifier and not req.specifier.contains(tree.chosen[key], prereleases=True):
                    break
                continue
            # Every queued item precedes any newly discovered edge. Only this prefix is certain to be visited:
            # a duplicate may conflict, a learned constraint may refuse, and the package cap may stop the walk.
            if key in batch or len(tree.chosen) + len(batch) >= MAX_PACKAGES:
                break
            batch[key] = sorted(tree.imposed.get(key, set()) | learned.get(key, set()) | {str(req.specifier)})
            if key in learned or len(batch) == PREFETCH_WORKERS:
                break
        for key in batch:
            if key not in self._projects and key not in self._project_jobs:
                self._project_jobs[key] = self._pool.submit(
                    pypi.fetch_json, f"{pypi.index_url()}/{quote(key)}/json", max_bytes=pypi.RELEASES_MAX_BYTES
                )
        for key, specs in batch.items():
            plan = (key, tuple(specs), target)
            if plan in self._planned:
                continue
            self._planned.add(plan)
            try:
                version = self.pick(key, specs, target)
                if version is None:
                    continue
                chosen = self._wheels(key, version, target)
                if (key, version, ",".join(e["filename"] for e in chosen)) in self._requires:
                    continue
                if chosen:
                    # If the first metadata lookup fails, requires() never asks for the second platform's wheel.
                    wheel = chosen[0]
                    url = wheel["url"] + ".metadata"
                    if url not in self._metadata and url not in self._metadata_jobs:
                        self._metadata_jobs[url] = self._pool.submit(self._read_metadata, wheel)
                elif (key, version) not in self._release_jobs:
                    self._release_jobs[key, version] = self._pool.submit(
                        pypi.fetch_json, f"{pypi.index_url()}/{quote(key)}/{quote(version)}/json"
                    )
            except Exception:
                pass  # The single-threaded walk reports lookup errors in exactly the same place as before.

    def pick(self, key: str, specs: list[str], target: Target) -> str | None:
        """The release pip would pick: the newest meeting every specifier with a file that isn't yanked, supports the
        target's Python and installs there (a matching wheel, or an sdist). An exact `==` pin may pick a yanked release,
        as pip does. When no matching release installs there, the newest matching one, which then shows the gap."""
        python = f"{target.major}.{target.minor}"
        releases = self._project(key)["releases"]
        exact = {s.version for spec in specs for s in SpecifierSet(spec) if s.operator in ("==", "===")}
        combined = SpecifierSet(",".join(s for s in specs if s))
        usable: dict[Version, str] = {}
        installable: dict[Version, str] = {}
        for text, entries in releases.items():
            try:
                version = Version(text)
                files = pypi._files(entries)
            except (InvalidVersion, ValueError):
                continue
            if not combined.contains(version, prereleases=True):
                continue
            pinned = text in exact or str(version) in exact
            files = pypi.before_cutoff(files, self._cutoff(key))
            # pip installs only wheels and sdists: not an .egg, .exe or .msi.
            live = [
                e
                for e in files
                if (pinned or not e.get("yanked"))
                and pypi._supports_python(e, python)
                and e["filename"].endswith((".whl", *SDIST_SUFFIXES))
            ]
            if live:
                usable[version] = text
            no_binary, only_binary = self.policy(key) if self.policy else (False, False)
            if any(
                (e["filename"].endswith(SDIST_SUFFIXES) and not only_binary)
                or (e["filename"].endswith(".whl") and not no_binary and rejection(e["filename"], target) is None)
                for e in live
            ):
                installable[version] = text
        chosen = max(combined.filter(installable), default=None) or max(combined.filter(usable), default=None)
        return usable[chosen] if chosen is not None else None

    def _round(self, roots: list[Requirement], target: Target, learned: dict[str, set[str]]) -> _Tree | _Conflict:
        python = f"{target.major}.{target.minor}"  # for messages
        tree = _Tree()
        extras: dict[str, frozenset[str]] = {}
        queue: deque[tuple[Requirement, frozenset[str], list[str]]] = deque((r, frozenset(), []) for r in roots)
        while queue:
            self._prefetch(queue, tree, learned, target)
            req, parent_extras, path = queue.popleft()
            if req.marker is not None and not marker_holds(str(req.marker), target, extras=parent_extras):
                continue
            key = canonicalize_name(req.name)
            spec = str(req.specifier)
            tree.imposed.setdefault(key, set()).add(spec)
            if path:
                tree.imposers.setdefault(key, []).append((path[-1], spec))
            wanted = frozenset(canonicalize_name(e) for e in req.extras)
            if key in tree.errors:
                continue
            if key in tree.chosen:
                if spec and not SpecifierSet(spec).contains(tree.chosen[key], prereleases=True):
                    return _Conflict(key, spec)
                if wanted <= extras[key]:
                    continue
                extras[key] |= wanted  # newly asked-for extras: walk its dependencies again with them
            else:
                if req.url:
                    tree.errors[key] = "a direct URL dependency: wheelgap can't check it"
                    tree.paths[key] = [*path, req.name]
                    continue
                specs = sorted(tree.imposed[key] | learned.get(key, set()))
                try:
                    version = self.pick(key, specs, target)
                except Exception as e:
                    tree.errors[key] = _lookup_error(e)
                    tree.paths[key] = [*path, self.names.get(key, req.name)]
                    continue
                if version is None:
                    if key in learned:  # learned constraints and this one can't all be met
                        raise DepsError(
                            f"--deps: can't resolve {self.names.get(key, req.name)} for Python {python} on "
                            f"{target.platform}: no release meets {', '.join(s or '(any)' for s in specs)}; a "
                            "resolver that backtracks may find another way, so check a lockfile instead "
                            "(uv lock, or pip-compile)"
                        )
                    shown = ", ".join(s for s in specs if s) or "any version"
                    if (cutoff := self._cutoff(key)) is not None:
                        shown += f" uploaded before exclude-newer ({describe(cutoff)})"
                    tree.errors[key] = f"no release matches {shown} for Python {python}"
                    tree.paths[key] = [*path, self.names.get(key, req.name)]
                    continue
                tree.chosen[key] = version
                extras[key] = wanted
                tree.paths[key] = [*path, self.names.get(key, req.name)]
                if len(tree.chosen) > MAX_PACKAGES:
                    raise DepsError(f"--deps: more than {MAX_PACKAGES} packages; check a lockfile instead")
            try:
                requires = self.requires(key, tree.chosen[key], target)
            except Exception as e:
                tree.errors[key] = f"couldn't read its dependencies: {_lookup_error(e)}"
                continue
            queue.extend((r, extras[key], tree.paths[key]) for r in requires)
        return tree

    def resolve(
        self, roots: list[Requirement], target: Target, constraints: dict[str, set[str]] | None = None
    ) -> _Tree:
        if not self.prefetch:
            return self._resolve(roots, target, constraints)
        # Plans are made under self.policy, which the caller may change between resolves (another source).
        self._planned.clear()
        pool = DownloadPool(PREFETCH_WORKERS)
        self._pool = pool
        try:
            tree = self._resolve(roots, target, constraints)
        except BaseException:
            # Ctrl-C or an error: don't wait for lookups nobody will read, and don't keep cancelled or unfinished
            # ones, so a reused Resolver looks them up again instead of reporting a CancelledError as a failure.
            pool.close(wait=False)
            _drop_unfinished(self._project_jobs)
            _drop_unfinished(self._metadata_jobs)
            _drop_unfinished(self._release_jobs)
            raise
        finally:
            self._pool = None
        pool.close()
        return tree

    def _resolve(
        self, roots: list[Requirement], target: Target, constraints: dict[str, set[str]] | None = None
    ) -> _Tree:
        # A requirements file's -c constraints count as learned specifiers from the start; unlike a constraint the
        # rounds below learn, a seeded one is never dropped for going unused — it applies while its package is in
        # the tree, and a package that never installs leaves it harmlessly unimposed.
        seeded: dict[str, set[str]] = {k: set(specs) for k, specs in (constraints or {}).items()}
        learned: dict[str, set[str]] = {k: set(specs) for k, specs in seeded.items()}
        seen: set[frozenset[tuple[str, str]]] = set()
        for _ in range(MAX_ROUNDS):
            state = frozenset((k, s) for k, specs in learned.items() for s in specs)
            if state in seen:
                break
            seen.add(state)
            out = self._round(roots, target, learned)
            if isinstance(out, _Conflict):
                learned.setdefault(out.key, set()).add(out.spec)
                continue
            # A learned constraint the finished tree doesn't impose would hold a package back: drop it, go again.
            unused = {
                (k, s)
                for k, specs in learned.items()
                for s in specs
                if s not in out.imposed.get(k, set()) and s not in seeded.get(k, set())
            }
            # A release picked before all its constraints were known may not be the one they pick together (a later
            # `>=2.0b1` lets prereleases in): go again with them all known from the start.
            stale = [
                k
                for k, v in out.chosen.items()
                if self.pick(k, sorted(out.imposed[k] | learned.get(k, set())), target) != v
            ]
            if not unused and not stale:
                return out
            for k in stale:
                learned.setdefault(k, set()).update(out.imposed[k])
            for k, s in unused:
                learned[k].discard(s)
            learned = {k: specs for k, specs in learned.items() if specs}
        raise DepsError(
            f"--deps: can't settle on releases for Python {target.major}.{target.minor} on {target.platform} "
            "without a resolver that backtracks; check a lockfile instead (uv lock, or pip-compile)"
        )


def _drop_unfinished(jobs: dict) -> None:
    """Forget prefetch jobs that were cancelled or are still running, so they're looked up again when needed."""
    for job_key in [k for k, job in jobs.items() if job.cancelled() or not job.done()]:
        del jobs[job_key]


def _os_family(platform: str) -> str:
    """The operating system a wheel platform tag (or a target's platform) is for: win, macos, linux, ios..."""
    tag = platform.split(".", 1)[0].lower()
    for prefix, family in (("win", "win"), ("macosx", "macos"), ("macos", "macos"), ("manylinux", "linux"),
                           ("musllinux", "linux"), ("linux", "linux"), ("musl", "linux"), ("ios", "ios"),
                           ("android", "android"), ("pyodide", "pyodide"), ("emscripten", "pyodide")):  # fmt: skip
        if tag.startswith(prefix):
            return family
    return tag


def _lookup_error(e: Exception) -> str:
    code = getattr(e, "code", None)
    if code == 404:
        return "not on index"
    if code is not None:
        return f"HTTP {code}"
    if isinstance(e, ValueError):
        return str(e)
    return f"lookup failed ({type(e).__name__})"


def pinned_complete(source: Source) -> bool:
    """A requirements file that's already complete: every requirement pinned, with --hash lines (in hash mode pip
    refuses any dependency the file doesn't pin), so --deps has nothing to resolve."""
    return source.compiled and all(
        p.version is not None and not p.spec and not p.error
        for p in source.packages
        if not p.skip and frozenset() not in p.marker_sets  # a MarkerGroup's `unknown` stand-in isn't a requirement
    )


def _direct_roots(
    tree: _Tree, roots: list[Requirement], resolver: Resolver, target: Target
) -> dict[str, tuple[str, ...]]:
    """Attribute a resolved package to the direct requirements that actually requested it.

    Traverse each root's active extras independently. A shared package requested with an
    extra by one root must not make its optional dependencies look like the other root's.
    Cache the raw requirements here too: Resolver.requires caches metadata, but still
    filters a release's wheels on every call. The target and chosen versions are fixed
    for this walk; extras and markers are evaluated independently for each owner below.
    """
    owners: dict[str, dict[str, str]] = {}
    direct: set[str] = set()
    seen: dict[tuple[str, str], frozenset[str]] = {}
    requirements: dict[str, list[Requirement]] = {}
    queue: deque[tuple[Requirement, frozenset[str], str, str]] = deque()
    for root in roots:
        if root.marker is not None and not marker_holds(str(root.marker), target):
            continue
        key = canonicalize_name(root.name)
        direct.add(key)
        queue.append((root, frozenset(), key, resolver.names.get(key, root.name)))

    while queue:
        req, parent_extras, owner, label = queue.popleft()
        if req.marker is not None and not marker_holds(str(req.marker), target, extras=parent_extras):
            continue
        key = canonicalize_name(req.name)
        if key not in tree.chosen:
            if key in tree.paths:  # its lookup failed (#453): still credit the owner, but it has nothing to walk
                owners.setdefault(key, {})[owner] = label
            continue
        owners.setdefault(key, {})[owner] = label
        wanted = frozenset(canonicalize_name(extra) for extra in req.extras)
        state = (key, owner)
        previous = seen.get(state)
        if previous is not None and wanted <= previous:
            continue
        seen[state] = wanted if previous is None else previous | wanted
        if key in tree.errors:
            continue
        try:
            if key not in requirements:
                requirements[key] = resolver.requires(key, tree.chosen[key], target)
        except Exception:
            continue  # Resolver already recorded the metadata error; hints must not change results
        queue.extend((child, seen[state], owner, label) for child in requirements[key])
    return {key: tuple(labels.values()) for key, labels in owners.items() if key not in direct}


def resolve_deps(source: Source, targets: list[Target], resolver: Resolver | None = None) -> None:
    """Replace the source's packages with what its requirements install on each target."""
    resolver = resolver or Resolver()
    resolver.cutoff = source.cutoff_for
    # A requirements file's options, or those a Dockerfile line's -r files set for the names on it.
    resolver.policy = source.format_control.policy if source.format_control else None
    roots = [Requirement(text) for text in source.specs]
    kept = [p for p in source.packages if p.skip]  # a name @ URL: nothing to look up, as without --deps
    # The input's own line for each direct requirement, for --fix to say where it is and what range it writes (#821):
    # only when the input has one line for the name, so there's no guessing which.
    lines: dict[str, list[Package]] = {}
    for p in source.packages:
        if not p.skip:
            lines.setdefault(p.key, []).append(p)
    roots = [r for r in roots if not r.url]
    packages: list[Package] = list(kept)
    index: dict[tuple[str, str | None, str | None], int] = {}
    deps_paths: dict[Target, dict[int, list[str]]] = {}
    deps_direct: dict[Target, dict[int, tuple[str, ...]]] = {}
    deps_imposed: dict[Target, dict[int, list[tuple[str, str]]]] = {}
    for target in targets:
        # pip evaluates a constraint's marker with `extra` as "" (no extras), so `; extra == "s3"` never applies.
        constraints = {key: set(specs) for key, specs in source.constraint_specs.items()}
        undecided: dict[str, str] = {}
        for key, entries in source.marker_constraint_specs.items():
            for marker, specifier in entries:
                holds = marker_value(marker, target, extras=frozenset())
                if holds:
                    constraints.setdefault(key, set()).add(specifier)
                elif holds is None:
                    undecided.setdefault(key, f"{key}{specifier}; {marker}")
        tree = resolver.resolve(roots, target, constraints=constraints)
        # A constraint on a package the tree doesn't install imposes nothing, whether or not its marker holds.
        if reached := [text for key, text in undecided.items() if key in tree.paths]:
            raise DepsError(
                f"{source.label}: --deps can't tell whether the constraint `{reached[0]}` applies on "
                f"{target.label(True)}, so check a lockfile instead (pip-compile, or uv pip compile)"
            )
        reach: dict[int, list[str]] = {}
        direct: dict[int, tuple[str, ...]] = {}
        imposed: dict[int, list[tuple[str, str]]] = {}
        direct_names = _direct_roots(tree, roots, resolver, target)
        for key, path in tree.paths.items():
            version, error = tree.chosen.get(key), tree.errors.get(key)
            if version is None and error is None:
                continue
            ident = (key, version, None if version else error)
            if ident not in index:
                name = resolver.names.get(key, path[-1])
                index[ident] = len(packages)
                own = lines[key][0] if len(lines.get(key, [])) == 1 else None
                packages.append(
                    Package(
                        name,
                        version,
                        error=error if version is None else None,
                        # A direct requirement keeps its input's file; where it's reached through another package
                        # (its path, per target), --fix goes by that instead.
                        origin=own.origin if own is not None and own.origin else "--deps",
                        written_spec=own.written_spec or own.spec if own is not None else None,
                        exclude_newer=source.cutoff_for(key),
                    )
                )
            i = index[ident]
            if version is not None and error is not None:
                packages[i].error = error  # its dependencies couldn't be read: unknown, not a clean bill
            reach[i] = path
            if key in tree.imposers:
                imposed[i] = [(resolver.names.get(canonicalize_name(n), n), s) for n, s in tree.imposers[key]]
            if origin_names := direct_names.get(key):
                direct[i] = tuple(resolver.names.get(name, name) for name in origin_names)
        deps_paths[target] = reach
        deps_direct[target] = direct
        deps_imposed[target] = imposed
    if fc := source.format_control:  # --no-binary/--only-binary lines apply to the resolved packages too
        for p in packages:
            if not p.skip:
                p.no_binary, p.only_binary = fc.policy(p.key)
    source.packages = packages
    source.deps_paths = deps_paths
    source.deps_direct = deps_direct
    source.deps_imposed = deps_imposed
