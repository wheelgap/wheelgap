"""Read packages (and, where the lockfile records them, their distribution files) from inputs."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name, canonicalize_version
from packaging.version import Version

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
    # Wheels whose Requires-Python excluded the target Python, mapped to their --explain reason.
    requires_python_rejected: dict[str, str] = field(default_factory=dict)

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


# A dependency edge in a lockfile graph: (name, version or None, marker or None)
Edge = tuple[str, str | None, str | None]


@dataclass(frozen=True)
class UvSelection:
    """Explicit installation selection. None on a Source keeps the conservative all-dependencies walk."""

    extras: frozenset[str] = frozenset()
    all_extras: bool = False
    groups: frozenset[str] = frozenset()
    no_dev: bool = False
    only_groups: frozenset[str] = frozenset()


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
    # Retain uv edge provenance for an explicit installation selection. The existing flattened
    # graph remains untouched, preserving conservative behavior when no selectors are supplied.
    uv_packages: list[dict] | None = None
    uv_manifest: dict = field(default_factory=dict)
    uv_pyproject: Path | None = None
    uv_default_groups: list[str] = field(default_factory=lambda: ["dev"])
    uv_selection: UvSelection | None = None
    # A requirements file (requirements.txt, .in, requirements.lock): unpinned lines get the newest release.
    requirements: bool = False
    # pyproject.toml only: [project] optional-dependencies by canonical extra name, applied with --extra.
    # None when the input isn't a pyproject.toml.
    pyproject_extras: dict[str, list[str]] | None = None
    # pyproject.toml only: optional-dependencies is listed in [project] dynamic, so the extras can't be read.
    pyproject_dynamic_extras: bool = False
    # pyproject.toml only: the canonical [project] name, to spot `myproject[extra]` self-references.
    pyproject_name: str | None = None
    # poetry.lock only: each package's [[package]] table, kept so --group/--only-group/--no-dev can pick packages
    # by the groups Poetry records on them.
    poetry_tables: list[dict] | None = None
    poetry_pyproject: Path | None = None
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
    # A project directory with pixi.lock as well as the lockfile that was picked: the pixi.lock, for a note.
    passed_over: Path | None = None


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


def _filename(entry: dict) -> str | None:
    for k in ("filename", "file", "name"):
        if isinstance(entry.get(k), str) and entry[k].endswith((".whl", ".tar.gz", ".zip", ".tar.bz2", ".tgz")):
            return entry[k]
    url = entry.get("url")
    if isinstance(url, str):
        return unquote(Path(urlparse(url).path).name)
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
    return (d["name"], d.get("version"), d.get("marker"))


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
    source.uv_pyproject = path.with_name("pyproject.toml")
    for p in data.get("package", []):
        src = p.get("source", {})
        pkg = Package(package_name(path, p.get("name")), p.get("version"), origin=path.name)
        kind = next((k for k in ("editable", "virtual", "git", "path", "directory", "url") if k in src), None)
        if kind and "registry" not in src:
            pkg.skip = kind
        else:
            pkg.registry = src.get("registry")
            files = [f for f in (_filename(w) for w in p.get("wheels", [])) if f]
            if p.get("sdist"):
                files.append(_filename(p["sdist"]) or f"{p['name']}-{p.get('version')}.tar.gz")
                pkg.sdist_url, pkg.sdist_sha256 = _sdist_source(p["sdist"], "hash")
            pkg.files = files
        i = len(source.packages)
        source.packages.append(pkg)
        edges[i] = _uv_edges(path, p)
        # Roots are the project itself (source "."), plus workspace members. A path dependency elsewhere
        # (directory = "libs/x") is reached through its dependents' edges and their markers instead.
        if (kind in ("editable", "virtual") and src.get(kind) == ".") or pkg.key in members:
            source.roots.append(i)
    for name, group in manifest_groups.items():
        for d in group:
            source.root_groups[len(source.root_edges)] = name
            source.root_edges.append((_uv_edge(path, "the manifest", d)[0], None, d.get("marker")))
    source.root_edges.extend(
        (_uv_edge(path, "the manifest", d)[0], None, d.get("marker")) for d in manifest.get("requirements", [])
    )
    if not source.roots and not source.root_edges:
        source.edges = None  # no idea where the graph starts; treat every package as needed
    if isinstance(data.get("requires-python"), str):
        source.requires_python = data["requires-python"]
    return source


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
        pyproject = source.uv_pyproject
        if pyproject is not None and pyproject.is_file():
            uv = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("uv", {})
            defaults = uv.get("default-groups", ["dev"])
            if defaults == "all":
                source.uv_default_groups = ["*"]
            elif isinstance(defaults, list) and all(isinstance(g, str) for g in defaults):
                source.uv_default_groups = [canonicalize_name(g) for g in defaults]
            else:
                raise SourceError(f"{pyproject}: [tool.uv] default-groups must be a list of strings or 'all'")
        if source.edges is None:
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
            if all(holds is not None for holds, _ in decided):
                if not any(holds for holds, _ in decided):
                    source.unselected_keys.add(pkg.key)  # only for extras that aren't selected
                    continue
                markers = None  # a selected extra installs it, on every target
            elif any(holds is False or ruled_out for holds, ruled_out in decided):
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
            files = [f for f in (_filename(w) for w in p.get("wheels", [])) if f]
            if p.get("sdist"):
                files.append(_filename(p["sdist"]) or f"{p['name']}-{p.get('version')}.tar.gz")
                pkg.sdist_url, pkg.sdist_sha256 = _sdist_source(p["sdist"], "hashes")
            pkg.files = files
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
            out.append((name, version if isinstance(version, str) and version != "*" else None, marker))
    return out


def parse_poetry_lock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    metadata = data.get("metadata", {})
    legacy_files = metadata.get("files", {})
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
            if entries is None:
                entries = legacy_files.get(p["name"])
            # No files recorded at all (old [metadata.hashes] locks): PyPI packages are looked up on the index,
            # packages from another index are skipped (fill_all).
            pkg.files = None if entries is None else [f for f in (_filename(e) for e in entries) if f]
        edges[len(pkgs)] = _poetry_edges(p)
        pkgs.append(pkg)
    # Lock files before 2.1 (all of Poetry 1.x) only record markers on dependency edges, not on
    # packages, so walk the graph from the packages nothing depends on, as for uv.lock.
    if _version_tuple(str(metadata.get("lock-version", "2.1"))) < (2, 1) and pkgs:
        roots = set(_graph_roots(pkgs, edges))
        # The project's own dependencies are roots too, even if another package also depends on them
        # under a marker (colorama directly, and via click only on Windows).
        declared = _project_dependencies(path.parent / "pyproject.toml")
        roots |= {i for i, p in enumerate(pkgs) if p.key in declared}
        return Source(path.name, pkgs, edges=edges, roots=sorted(roots), poetry_tables=data.get("package", []))
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
        i: sorted({j for name, _, _ in edges.get(i, []) for j in by_name.get(canonicalize_name(name), [])})
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


def _project_dependencies(pyproject: Path) -> set[str]:
    """Names a project declares itself (PEP 621 and Poetry tables), to use as roots for poetry.lock.
    A missing or odd pyproject.toml just means no extra roots: it must never break the run."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return set()

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
    if first.markers is not None:
        first.markers = None if pkg.markers is None else first.markers + pkg.markers


def parse_pdm_lock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    metadata = data.get("metadata", {})
    legacy_files = metadata.get("files", {})
    locked_groups = metadata.get("groups")
    strategy = metadata.get("strategy")
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
                pkg.files = [f for f in (_filename(e) for e in entries) if f]
            # Not in either place: leave files as None so the PyPI lookup fills them in.
        _add_once(pkgs, seen, pkg)
    return Source(
        path.name,
        pkgs,
        pdm_groups=[groups[(pkg.key, pkg.version)] for pkg in pkgs],
        pdm_locked_groups=(
            [canonicalize_name(g) for g in locked_groups if isinstance(g, str)]
            if isinstance(locked_groups, list)
            else None
        ),
        pdm_inherit_metadata=isinstance(strategy, list) and "inherit_metadata" in strategy,
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
    """A library's direct dependencies: [project] dependencies, read as requirement lines. Their own
    dependencies aren't known without a lockfile, so they aren't checked."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SourceError(f"{path}: invalid TOML: {e}") from None
    project = data.get("project")
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
    if not project.get("dependencies") and not project.get("optional-dependencies"):
        # poetry-core 2 builds [tool.poetry.dependencies] into the package whenever [project] lists no dependencies
        # or optional-dependencies (missing or empty: DependencyGroup.dependencies' "legacy mode").
        tool = data.get("tool")
        poetry = tool.get("poetry") if isinstance(tool, dict) else None
        poetry_deps = poetry.get("dependencies") if isinstance(poetry, dict) else None
        if isinstance(poetry_deps, dict) and any(dep.lower() != "python" for dep in poetry_deps):
            raise SourceError(
                f"{path}: the dependencies are in [tool.poetry.dependencies], not [project] dependencies, "
                "so wheelgap can't read them; check poetry.lock instead"
            )
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
        raise SourceError(f"{path}: [project] dependencies must be a list of strings")
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
    source = Source(
        path.name,
        pyproject_extras=extras,
        pyproject_dynamic_extras="optional-dependencies" in dynamic,
        pypi_only=not (_names_an_index(data) or _nearby_config_names_an_index(path)),
    )
    if isinstance(requires_python := project.get("requires-python"), str):
        source.requires_python = requires_python
    if isinstance(name := project.get("name"), str):
        source.pyproject_name = canonicalize_name(name)
    self_extras: list[str] = []
    for line in dependencies:
        pkg = requirement_to_package(line, path.name)
        if pkg.key != source.pyproject_name:
            source.packages.append(pkg)
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
            pkg = requirement_to_package(line, source.label)
            if pkg not in source.packages:  # also in another extra or in dependencies: one row
                source.packages.append(pkg)


def requirement_to_package(text: str, origin: str) -> Package:
    try:
        req = Requirement(text)
    except InvalidRequirement as e:
        raise SourceError(f"{origin}: can't parse requirement {text!r}: {e}") from None
    markers = [str(req.marker)] if req.marker else None
    if req.url:
        return Package(req.name, None, skip="url", origin=origin, markers=markers)
    version = _pinned_version(req)
    spec = str(req.specifier) if req.specifier and version is None else None
    return Package(req.name, version, origin=origin, markers=markers, spec=spec)


# "name @ url", "name@url" and "name[extra] @ url": PEP 508 direct references, which Requirement parses.
# A bare URL can't match: its scheme ("git+ssh:") has characters a package name can't.
_NAMED_URL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?\s*@")
_EGG_RE = re.compile(r"[#&]egg=([A-Za-z0-9][A-Za-z0-9._-]*)")
_COMMENT_RE = re.compile(r"\s#")
_INCLUDE_RE = re.compile(r"^(?:-r|--requirement)(?:\s+|=)?(\S.*)$")
_CONSTRAINT_RE = re.compile(r"^(?:-c(?:\s+|=)?|--constraint(?:\s+|=))(\S.*)$")


# pip options that point at an index or a folder of distributions other than PyPI's (or turn PyPI off).
_INDEX_OPTIONS = ("--index-url", "--extra-index-url", "--find-links", "--no-index")


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


def _abbreviated_include(line: str) -> bool:
    """An abbreviation of --requirement or --constraint (`--requirem f`, `--cons f`), which pip reads but wheelgap
    doesn't follow: the file it names may set an index."""
    option = line.split(None, 1)[0].split("=", 1)[0]
    return (
        len(option) > 2
        and option not in ("--requirement", "--constraint")
        and any(name.startswith(option) for name in ("--requirement", "--constraint"))
    )


def parse_requirements(path: Path) -> Source:
    constraints: list[Package] = []
    seen: dict[bool, set[Path]] = {False: set(), True: set()}  # requirement files, constraint files
    index_options: list[str] = []
    pkgs = _read_requirements(path, seen, constraints, index_options=index_options)
    by_name: dict[str, list[Package]] = {}
    for entry in constraints:
        by_name.setdefault(entry.key, []).append(entry)
    for pkg in pkgs:
        if pkg.skip or pkg.version or not (entries := by_name.get(pkg.key)):
            continue
        if any(entry.markers for entry in entries):
            pkg.error = "constraint has an environment marker; target-dependent constraints aren't supported"
        elif any(entry.skip for entry in entries):
            pkg.error = "constraint isn't an index version or range"
        # 1.9 and 1.9.0 are one version; reversed() keeps the first spelling.
        elif len(pins := {canonicalize_version(e.version): e.version for e in reversed(entries) if e.version}) > 1:
            pkg.error = "constraints specify conflicting exact versions"
        elif pins:
            pkg.version, pkg.spec = next(iter(pins.values())), None
        else:
            spec = SpecifierSet(pkg.spec or "")
            for entry in entries:
                spec &= SpecifierSet(entry.spec or "")
            pkg.spec = str(spec) or None
    return Source(
        path.name, pkgs, requirements=True, pypi_only=not index_options and not _nearby_config_names_an_index(path)
    )


def _read_requirements(
    path: Path,
    seen: dict[bool, set[Path]],
    constraints: list[Package],
    constraint: bool = False,
    index_options: list[str] | None = None,
) -> list[Package]:
    """`index_options` collects the lines that may point pip at another index: index options (--index-url,
    --extra-index-url, --find-links, --no-index), includes that aren't read, and abbreviated include options."""
    path = path.resolve()
    if path in seen[constraint]:
        return []
    seen[constraint].add(path)
    pkgs: list[Package] = []
    append = constraints.append if constraint else pkgs.append
    # utf-8-sig: a byte order mark (Windows editors) mustn't hide an option on the first line.
    lines = path.read_text(encoding="utf-8-sig").replace("\\\n", " ").splitlines()
    for raw in lines:
        line = _COMMENT_RE.split(raw, 1)[0].strip()  # " # note" or "<TAB># note"
        if not line or line.startswith("#"):
            continue
        if index_options is not None and (_names_index_option(line) or _abbreviated_include(line)):
            index_options.append(line)
        if m := _CONSTRAINT_RE.match(line):
            target = m.group(1).strip()
            # A URL (-c https://releases.openstack.org/...) or ${VAR} is left to pip: it's skipped, as before. Unread,
            # it may name an index, so the input isn't PyPI-only.
            if "://" not in target and "${" not in target:
                _read_requirements(path.parent / target, seen, constraints, True, index_options)
            elif index_options is not None:
                index_options.append(line)
            continue
        if m := _INCLUDE_RE.match(line):  # -r file, -rfile, --requirement file, --requirement=file
            pkgs.extend(
                _read_requirements(path.parent / m.group(1).strip(), seen, constraints, constraint, index_options)
            )
            continue
        if line.startswith("-e ") or line.startswith("--editable"):
            name = line.split("#egg=", 1)[1] if "#egg=" in line else line.split(None, 1)[-1]
            append(Package(name, None, skip="editable", origin=path.name))
            continue
        if line.startswith("-"):
            continue  # pip options: --index-url, --hash continuation, ...
        tokens = line.split()
        # Drop --hash=sha256:... and "--hash sha256:..." (option and value as two tokens).
        line = " ".join(
            t for k, t in enumerate(tokens) if not t.startswith("--hash") and (k == 0 or tokens[k - 1] != "--hash")
        )
        if line.startswith((".", "/")):
            append(Package(line, None, skip="path", origin=path.name))
            continue
        if "://" in line and not _NAMED_URL_RE.match(line):
            # A bare URL (git+ssh://git@github.com/org/repo.git#egg=foo): never from the index.
            # "name @ url" lines are left to Requirement, which knows the name and markers.
            egg = _EGG_RE.search(line)
            name = egg.group(1) if egg else line.split(";", 1)[0].strip()
            append(Package(name, None, skip="url", origin=path.name))
            continue
        append(requirement_to_package(line, path.name))
    return pkgs


class _YamlError(Exception):
    def __init__(self, number: int, message: str):
        super().__init__(f"line {number}: {message}")


class _BlockYaml:
    """The block-style YAML subset pixi writes (serde_yaml): mappings, sequences (also "indentless" ones under a
    key), plain and quoted one-line scalars, and the empty `[]` and `{}`. Scalars stay strings. Anything else
    (anchors, aliases, tags, block scalars, flow collections, multi-line scalars, comments, tabs) is an error rather
    than a guess at what YAML would make of it."""

    _ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0"}
    # Tabs, control characters (libyaml refuses most of them), line breaks other than \n and \r (which
    # str.splitlines would also split on), a byte order mark after the start, lone surrogates, and the
    # noncharacters U+FFFE and U+FFFF, which libyaml refuses too.
    _BAD_CHAR = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\ufeff\ud800-\udfff\ufffe\uffff]")

    def __init__(self, text: str):
        self.lines: list[tuple[int, int, str]] = []  # (line number, indent, content)
        for number, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
            if bad := self._BAD_CHAR.search(raw):
                raise _YamlError(number, f"unsupported character {bad.group()!r} (tabs and control characters)")
            line = raw.rstrip(" ")
            if not line:
                continue
            content = line.lstrip(" ")
            if content.startswith("#") or (line == content and content.startswith(("---", "..."))):
                raise _YamlError(number, "comments and document markers aren't supported")
            self.lines.append((number, len(line) - len(content), content))
        self.i = 0

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


READERS = {
    "pyproject": parse_pyproject,
    "uv": parse_uv_lock,
    "pylock": parse_pylock,
    "poetry": parse_poetry_lock,
    "pdm": parse_pdm_lock,
    "pipfile": parse_pipfile_lock,
    "requirements": parse_requirements,
    "pixi": parse_pixi_lock,
}
PARSERS = {
    "uv.lock": "uv",
    "pylock.toml": "pylock",
    "poetry.lock": "poetry",
    "pdm.lock": "pdm",
    "Pipfile.lock": "pipfile",
    "pixi.lock": "pixi",
}


def load_file(path: Path, environment: str | None = None) -> Source:
    """Read one input file. `environment` picks a pixi.lock environment (default: `default`)."""
    kind = file_kind(path)
    source = parse_pixi_lock(path, environment) if kind == "pixi" else READERS[kind](path)
    source.path, source.kind = path, kind
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
    if name in PARSERS:
        return PARSERS[name]
    if name.startswith("pylock.") and name.endswith(".toml"):
        return "pylock"
    if name.startswith("requirements") and name.endswith(".lock"):
        return "requirements"
    if name.endswith(".lock") and "uv" in name:
        return "uv"
    if name.endswith((".txt", ".in")):
        return "requirements"
    raise SourceError(
        f"don't know how to read {path} "
        "(expected uv.lock, pylock.toml, poetry.lock, pdm.lock, Pipfile.lock, pixi.lock, requirements.lock, "
        "requirements.txt or pyproject.toml)"
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
    if _has_project_table(directory / "pyproject.toml"):
        pyproject = directory / "pyproject.toml"
        hint = f"To check the direct dependencies in its pyproject.toml, run `wheelgap {pyproject}`. " + hint
    raise SourceError(f"no lockfile found in {directory} (looked for {', '.join(LOCKFILES)}). {hint}")


def _has_project_table(pyproject: Path) -> bool:
    """For the "no lockfile" hint only: a missing or unreadable file just means no hint."""
    try:
        return isinstance(tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project"), dict)
    except (OSError, ValueError):
        return False


def load_inputs(inputs: list[str], environment: str | None = None) -> list[Source]:
    """Each input is a file, a directory (lockfile auto-detected) or a requirement like 'numpy==2.3.4'.
    `environment` picks the pixi.lock environment; with no pixi.lock input it's an error."""
    if environment == "":
        raise SourceError("--environment needs a name")
    sources: list[Source] = []
    adhoc = Source("command line", pypi_only=True)  # names to look up on PyPI
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            sources.append(load_file(find_lockfile(p, environment is not None), environment))
            if sources[-1].kind != "pixi" and (p / "pixi.lock").is_file():
                sources[-1].passed_over = p / "pixi.lock"
        elif p.is_file():
            sources.append(load_file(p, environment))
        elif "/" in item or item.endswith((".lock", ".txt", ".toml")):
            raise SourceError(f"{item}: no such file")
        else:
            adhoc.packages.append(requirement_to_package(item, "command line"))
    if adhoc.packages:
        sources.append(adhoc)
    if environment is not None and not any(s.kind == "pixi" for s in sources):
        raise SourceError("--environment applies to pixi.lock inputs only")
    return sources
