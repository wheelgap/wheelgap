"""Decide, per package and target, whether a prebuilt wheel exists."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache

from packaging.markers import InvalidMarker, Marker
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion

try:  # packaging's parsed-marker node types (private, so guarded: without them every comparison is unknown)
    from packaging._parser import Value as _Value
    from packaging._parser import Variable as _Variable
except ImportError:  # pragma: no cover
    _Value = _Variable = None  # type: ignore[assignment,misc]

from .sources import Edge, Package, PoetryExtras, Source, uv_extra_names, uv_group_names, uv_selected_roots
from .targets import ANDROID_FLOOR, IOS_FLOOR, MANYLINUX_FLOOR, MANYLINUX_X86_ARCHES, PLATFORMS, Target


class Status(StrEnum):
    WHEEL = "wheel"  # a compatible wheel exists
    SOURCE = "source"  # no compatible wheel, but an sdist: installers will build it
    MISSING = "missing"  # no compatible wheel and no sdist: install fails
    UNKNOWN = "unknown"  # couldn't look the package up
    NOT_NEEDED = "n/a"  # markers say this package isn't installed on the target
    BUNDLED = "bundled"  # no wheel on the index, but Pyodide ships its own build (--pyodide-lock)


# How each status is shown in text output, and its colour.
CELL = {
    Status.WHEEL: ("✓", "green"),
    Status.SOURCE: ("● source", "yellow"),
    Status.MISSING: ("✗ missing", "red"),
    Status.UNKNOWN: ("? unknown", "dim"),
    Status.NOT_NEEDED: ("–", "dim"),
    Status.BUNDLED: ("✓ bundled", "green"),
}


# The archive formats pip accepts as source distributions.
SDIST_SUFFIXES = (
    ".tar.gz",
    ".zip",
    ".tar.bz2",
    ".tgz",
    ".tar.xz",
    ".tar",
    ".tbz",
    ".txz",
    ".tlz",
    ".tar.lz",
    ".tar.lzma",
)


@dataclass
class Row:
    package: Package
    results: dict[Target, Status] = field(default_factory=dict)
    # uv.lock only: the shortest dependency path that installs this package on each target.
    via: dict[Target, list[str]] = field(default_factory=dict)
    # --ignore NAME:TARGET: targets whose result is still reported but doesn't count as a gap.
    ignored: frozenset[Target] = frozenset()

    @property
    def counted(self) -> list[Status]:
        """The results that decide whether this package has a gap: every target's except the ignored ones."""
        return [s for t, s in self.results.items() if t not in self.ignored]

    @property
    def ok(self) -> bool:
        return all(s in (Status.WHEEL, Status.NOT_NEEDED, Status.BUNDLED) for s in self.counted)

    @property
    def fails_install(self) -> bool:
        """True if some target can't install this package at all, or it couldn't be checked."""
        return any(s in (Status.MISSING, Status.UNKNOWN) for s in self.counted)

    @property
    def needed(self) -> bool:
        return any(s is not Status.NOT_NEEDED for s in self.results.values())


# A wheel's tags, or None if its filename doesn't parse. --explain asks again for every file on every target,
# so parse each name once. lru_cache is thread-safe (also on free-threaded Python). The bound counts entries,
# a few MB with realistic names (about 4 MB, keys included), and keeps a long-running process (wheelgap-mcp)
# from growing without limit; one release's wheels fit many times over.
@lru_cache(maxsize=16384)
def _parsed_tags(filename: str) -> frozenset[Tag] | None:
    try:
        return parse_wheel_filename(filename)[3]
    except InvalidWheelFilename:
        return None


def wheel_tags(files: list[str]) -> list[frozenset[Tag]]:
    return [tags for f in files if f.endswith(".whl") and (tags := _parsed_tags(f)) is not None]


def has_sdist(files: list[str]) -> bool:
    return any(f.endswith(SDIST_SUFFIXES) for f in files)


def _installable(files: list[str], target: Target) -> bool:
    return any(target.accepts(t) for t in wheel_tags(files)) or (has_sdist(files) and target.builds_sdists)


def status_for(
    files: list[str] | None,
    target: Target,
    _wheels: list[frozenset[Tag]] | None = None,
    yanked: frozenset[str] = frozenset(),
) -> Status:
    if files is None:
        return Status.UNKNOWN
    if yanked:
        # `yanked` is only non-empty for a release pinned with ==. Like pip, ignore its yanked files
        # unless every file this target could use is yanked; then pip installs them anyway (PEP 592).
        live = [f for f in files if f not in yanked]
        if _installable(live, target):
            files = live
        _wheels = None
    wheels = _wheels if _wheels is not None else wheel_tags(files)
    if any(target.accepts(t) for t in wheels):
        return Status.WHEEL
    return Status.SOURCE if has_sdist(files) and target.builds_sdists else Status.MISSING


_PLAT_RE = re.compile(r"^(manylinux|musllinux|macosx)_(\d+)_(\d+)_(.+)$")
_LEGACY_GLIBC = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}
_LIBC = {"manylinux": "glibc", "musllinux": "musl", "macosx": "macOS"}


_WASM_RE = re.compile(r"^(?:pyodide|pyemscripten|emscripten)_[0-9_]+_wasm32$")
_IOS_RE = re.compile(r"^ios_(\d+)_(\d+)_(.+)$")
_ANDROID_RE = re.compile(r"^android_(\d+)_(.+)$")


def _platform_reason(plat: str, target: Target) -> str:
    family, arch = PLATFORMS[target.platform]
    if family == "pyodide" and _WASM_RE.match(plat):
        return f"{plat} is for a different Pyodide ABI than Python {target.python}"
    if family == "ios" and (m := _IOS_RE.match(plat)) and m.group(3) == arch:
        need = (int(m.group(1)), int(m.group(2)))
        have = ".".join(map(str, target.ios))
        if need < IOS_FLOOR:
            return f"{plat} is older than iOS 12.0, the oldest pip accepts"
        if need > target.ios:
            return f"{plat} needs iOS {need[0]}.{need[1]}, newer than {have} (--ios)"
        return f"{plat} isn't one of the iOS versions pip tries for {have} (x.0 to x.9 of each older release)"
    if family == "android" and (m := _ANDROID_RE.match(plat)) and m.group(2) == arch:
        if int(m.group(1)) < ANDROID_FLOOR:
            return f"{plat} is older than API level {ANDROID_FLOOR}, the oldest pip accepts"
        return f"{plat} needs Android API level {m.group(1)}, newer than {target.android_api} (--android-api)"
    m = _PLAT_RE.match(plat)
    if m:
        kind, major, minor, wheel_arch = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    elif plat.startswith("manylinux") and "_" in plat:  # manylinux2014_aarch64 and friends
        legacy, wheel_arch = plat.split("_", 1)
        kind, (major, minor) = "manylinux", _LEGACY_GLIBC.get(legacy, (0, 0))
    elif plat == f"linux_{arch}" and family == "musllinux":
        return f"{plat} is a plain Linux (glibc) build; wheelgap only counts musllinux wheels for musl"
    else:
        return f"{plat} doesn't match {target.platform}"
    same_arch = wheel_arch == arch or (kind == "macosx" and wheel_arch == "universal2")
    if same_arch and kind == "manylinux" and family == "musllinux":
        return f"{plat} is a glibc wheel; musl (Alpine) needs musllinux"
    if same_arch and kind == "musllinux" and family == "manylinux":
        return f"{plat} is a musl wheel; glibc Linux needs manylinux"
    if (
        same_arch
        and kind == "manylinux"
        and family == "manylinux"
        and major == MANYLINUX_FLOOR[0]
        and arch not in MANYLINUX_X86_ARCHES
        and minor < MANYLINUX_FLOOR[1]
    ):
        return f"{plat} is older than glibc {MANYLINUX_FLOOR[0]}.{MANYLINUX_FLOOR[1]}, the oldest pip accepts on {arch}"
    ceiling = {"manylinux": target.glibc, "musllinux": target.musl, "macosx": target.macos}[kind]
    same_family = kind == family or (kind == "macosx" and family == "macos")
    if same_arch and same_family and major and (major, minor) > ceiling:
        flag = {"manylinux": "--glibc", "musllinux": "--musl", "macosx": "--macos"}[kind]
        have = ".".join(map(str, ceiling))
        return f"{plat} needs {_LIBC[kind]} {major}.{minor}, newer than {have} ({flag})"
    return f"{plat} doesn't match {target.platform}"


def rejection(filename: str, target: Target) -> str | None:
    """Why `target` can't install this wheel, or None if it can."""
    wheel = _parsed_tags(filename)
    if wheel is None:
        return "unparseable wheel filename"
    if target.accepts(wheel):
        return None
    platforms = sorted({t.platform for t in wheel})
    if "any" in platforms or not target.platform_tag_set.isdisjoint(platforms):
        if target.freethreaded:
            stable = [t for t in wheel if t.abi == "abi3t"]
            if stable:  # PEP 803: the free-threaded stable ABI, cpXY-abi3t works on CPython X.Y+ only
                minors = [int(t.interpreter[3:]) for t in stable if t.interpreter[3:].isdigit()]
                return f"abi3t wheels need CPython 3.{min(minors)}+ (free-threaded)" if minors else "abi3t wheel"
            if any(t.abi == "abi3" for t in wheel):
                return "abi3 wheels can't be used on free-threaded Python (only abi3t ones can)"
        built = ", ".join(sorted({f"{t.interpreter}-{t.abi}" for t in wheel}))
        return f"built for {built}, not CPython {target.python}"
    # Name the tag closest to the target: same arch first, and the modern manylinux_2_17_* form
    # rather than its legacy manylinux2014_* alias.
    arch = PLATFORMS[target.platform][1]
    closest = min(platforms, key=lambda p: (not p.endswith(arch), not _PLAT_RE.match(p), p))
    return _platform_reason(closest, target)


_PY_MINOR_RE = re.compile(r"^(?:cp|py)3(\d+)$")
_OS_PREFIX: dict[str, tuple[str, ...]] = {
    "manylinux": ("manylinux",),
    "musllinux": ("musllinux",),
    "macos": ("macosx",),
    "windows": ("win",),
    "pyodide": ("pyodide_", "pyemscripten_", "emscripten_"),
    "ios": ("ios_",),
    "android": ("android_",),
}


def _closeness(filename: str, target: Target) -> tuple[int, int, str]:
    """Sort key: right platform first, then same arch, then same OS, then nearest Python version."""
    wheel = _parsed_tags(filename)
    if wheel is None:
        return (3, 99, filename)
    platforms = {t.platform for t in wheel}
    family, arch = PLATFORMS[target.platform]
    if "any" in platforms or not target.platform_tag_set.isdisjoint(platforms):
        group = 0
    elif any(p.endswith(arch) or (p.endswith("universal2") and family == "macos") for p in platforms):
        group = 1
    elif any(p.startswith(_OS_PREFIX[family]) for p in platforms):
        group = 2
    else:
        group = 3
    minors = [int(m.group(1)) for t in wheel if (m := _PY_MINOR_RE.match(t.interpreter))]
    distance = min((abs(m - target.minor) for m in minors), default=99)
    return (group, distance, filename)


def explain(
    files: list[str],
    target: Target,
    yanked: frozenset[str] = frozenset(),
    requires_python_rejected: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """(filename, reason) for every wheel `target` rejects, the closest misses first.

    A wheel the target accepts but status_for() leaves out because it's yanked (the release has other files the
    target can use) is listed with the reason "yanked".
    """
    skips_yanked = bool(yanked) and _installable([f for f in files if f not in yanked], target)
    out = []
    for f in files:
        if not f.endswith(".whl"):
            continue
        reason = rejection(f, target)
        if reason is None and skips_yanked and f in yanked:
            reason = "yanked"
        if reason:
            out.append((f, reason))
    if requires_python_rejected:
        for f, reason in requires_python_rejected.items():
            if f.endswith(".whl"):
                out.append((f, reason))
    return sorted(out, key=lambda fr: _closeness(fr[0], target))


# How many rejected wheels --explain's text (and wheelgap-mcp's check_package) lists per cell.
EXPLAIN_LIMIT = 8


def explain_lines(
    files: list[str],
    target: Target,
    *,
    yanked: frozenset[str] = frozenset(),
    requires_python_rejected: dict[str, str] | None = None,
    limit: int | None = None,
    dim: Callable[[str], str] = lambda text: text,
    align: bool = False,
) -> list[str]:
    """What --explain says under a gap on `target`: each rejected wheel and why (the closest misses first, at most
    `limit` of them), or that none were published, and a note when the target can't build the sdist there is.

    The command line aligns the reasons in a column, dims them and indents each line; Result.reasons joins them
    unaligned (one space-separated "file  reason" per line) and with no limit.
    """
    rejected = explain(files, target, yanked, requires_python_rejected)
    out = [] if rejected else [dim("no wheels published for this version")]
    shown = rejected[:limit]
    width = max((len(f) for f, _ in shown), default=0) if align else 0
    out += [f"{f.ljust(width)}  {dim(reason)}" for f, reason in shown]
    if len(rejected) > len(shown):
        out.append(dim(f"… and {len(rejected) - len(shown)} more (--json lists them all)"))
    if not target.builds_sdists and has_sdist(files):
        out.append(dim(f"there is an sdist, but {target.platform} can't build packages from source"))
    return out


_marker_cache: dict[str, Marker | None] = {}


# Marker variables wheelgap can't know for a target: which extras or dependency groups get requested
# (uv writes its [tool.uv] conflicts as extra == '...' terms) and the OS release/version of the machine
# that installs. A comparison on one of them is "unknown", combined with the known parts in three-valued
# logic: "sys_platform != 'darwin' and extra == 'cpu'" is unknown on Linux (so the package counts) and
# false on macOS (so it doesn't). Only a definite False hides a package. Target.unknown_markers adds
# platform_machine on 32-bit x86 and Arm, where it depends on the machine rather than the Python.
_UNKNOWABLE = frozenset({"extra", "extras", "dependency_groups", "platform_release", "platform_version"})
_atom_cache: dict[tuple[str, Target], bool | None] = {}


def _quote(text: str) -> str | None:
    if '"' not in text:
        return f'"{text}"'
    return f"'{text}'" if "'" not in text else None  # both kinds of quote: can't re-serialise safely


def _atom(
    node: tuple, target: Target | None, environment: dict | None = None, poetry_extras: PoetryExtras | None = None
) -> bool | None:
    """One comparison: True/False, or None when it involves something we can't know (or, with no target, anything
    but a Poetry extra)."""
    if _Variable is None or _Value is None:
        return None  # packaging's internals changed: treat every comparison as unknown (never hide)
    parts: list[str] = []
    names: set[str] = set()
    for n in node:
        if isinstance(n, _Variable):
            names.add(n.value)
            parts.append(n.value)
        elif isinstance(n, _Value):
            quoted = _quote(n.value)
            if quoted is None:
                return None
            parts.append(quoted)
        else:
            parts.append(str(n.value))  # the operator
    if poetry_extras is not None and names == {"extra"}:
        return _poetry_extra(node, poetry_extras)
    if target is None or not names or names & ((_UNKNOWABLE - (environment or {}).keys()) | target.unknown_markers):
        return None
    text = " ".join(parts)
    if environment is not None:
        try:
            return Marker(text).evaluate({**target.marker_env, **environment}, context="lock_file")
        except Exception:
            return None
    key = (text, target)
    if key not in _atom_cache:
        try:
            _atom_cache[key] = Marker(text).evaluate(target.marker_env)
        except Exception:  # undefined names, odd comparisons: unknown rather than a confident answer
            _atom_cache[key] = None
    return _atom_cache[key]


def _poetry_extra(node: tuple, extras: PoetryExtras) -> bool | None:
    """An `extra` comparison as Poetry evaluates a lock's markers: `extra` stands for the set of extras selected, so
    `extra == "api"` holds if api is one of them and `extra != "api"` if it isn't (SingleMarker.validate in
    https://github.com/python-poetry/poetry-core/blob/2.5.0/src/poetry/core/version/markers.py, called from
    Transaction.calculate_operations in Poetry 2.5's src/poetry/puzzle/transaction.py). A name that isn't one of the
    project's extras, or any other operator, is unknown."""
    known, selected = extras
    if len(node) != 3 or str(node[1].value) not in ("==", "!="):
        return None
    value = node[2] if isinstance(node[0], _Variable) else node[0]
    if not isinstance(value, _Value) or (name := canonicalize_name(value.value)) not in known:
        return None
    return (name in selected) == (str(node[1].value) == "==")


def _evaluate3(
    markers: list, target: Target | None, environment: dict | None = None, poetry_extras: PoetryExtras | None = None
) -> bool | None:
    """Kleene evaluation of packaging's parsed marker list: `and` binds tighter than `or`."""
    groups: list[list[bool | None]] = [[]]
    for item in markers:
        if item == "or":
            groups.append([])
        elif item == "and":
            continue
        elif isinstance(item, list):
            groups[-1].append(_evaluate3(item, target, environment, poetry_extras))
        else:
            groups[-1].append(_atom(item, target, environment, poetry_extras))
    results: list[bool | None] = []
    for group in groups:
        results.append(False if False in group else None if None in group else True)
    return True if True in results else None if None in results else False


def _parsed_marker(marker: str) -> Marker | None:
    if marker not in _marker_cache:
        try:
            _marker_cache[marker] = Marker(marker)
        except InvalidMarker:
            _marker_cache[marker] = None
    return _marker_cache[marker]


def poetry_marker_holds(marker: str, target: Target | None, extras: PoetryExtras) -> bool:
    """Whether a Poetry 2.x lock marker can hold on `target` with these extras (see _poetry_extra). With no target,
    whether it can hold on any target: only its extra comparisons are known then."""
    m = _parsed_marker(marker)
    if m is None:
        return True  # can't parse it: assume the package is needed rather than hide a gap
    try:
        return _evaluate3(m._markers, target, poetry_extras=extras) is not False
    except Exception:  # packaging changed its internals: count it as needed rather than hide a gap
        return True


def poetry_extras_decide(marker: str, extras: PoetryExtras) -> tuple[bool | None, bool]:
    """How the selected extras decide a Poetry 2.x lock marker. If it's only `extra` comparisons on the project's
    extras (`extra == "a" or extra == "b"`), the selection alone decides it: (True or False, False). Anything else
    (`sys_platform == "win32" or extra == "a"`) also depends on how the package is reached, which Poetry 2.0-2.2 (and
    installer.re-resolve) read differently from the lock, so it's (None, ruled_out): evaluated per target with every
    extra unknown, a superset, where ruled_out says whether the selection rules out one of its extra comparisons."""
    m = _parsed_marker(marker)
    if m is None or _Variable is None:
        return None, False
    atoms: list[tuple] = []

    def walk(items: list) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item)
            elif isinstance(item, tuple):
                atoms.append(item)

    try:
        walk(m._markers)
        decided = [
            _poetry_extra(atom, extras) if {n.value for n in atom if isinstance(n, _Variable)} == {"extra"} else None
            for atom in atoms
        ]
        if atoms and all(d is not None for d in decided):
            return _evaluate3(m._markers, None, poetry_extras=extras) is not False, False
        return None, False in decided
    except Exception:  # packaging changed its internals: count it as needed rather than hide a gap
        return None, False


def marker_holds(
    marker: str | None,
    target: Target,
    *,
    extras: frozenset[str] | None = None,
    groups: frozenset[str] | None = None,
) -> bool:
    if not marker:
        return True
    m = _parsed_marker(marker)
    if m is None:
        return True  # can't parse it: assume the package is needed rather than hide a gap
    try:
        if extras is None and groups is None:
            return _evaluate3(m._markers, target) is not False
        # uv's synthetic conflict marker names aren't public extra names. Preserve their
        # existing unknown/conservative treatment rather than hiding a potentially needed wheel.
        synthetic = bool(re.search(r"(?:extra|group)-\d+-", marker))
        contexts = extras | {""} if extras is not None and not synthetic else {None}
        for extra in contexts:
            environment: dict = {}
            if extra is not None:
                environment.update(extra=extra, extras=extras)
            if groups is not None:
                environment["dependency_groups"] = groups
            if _evaluate3(m._markers, target, environment) is not False:
                return True
        return False
    except Exception:  # packaging changed its internals: count it as needed rather than hide a gap
        return True


# How a package was reached: the index of the package that depends on it, the name of the
# dependency group that lists it, or None for a root or a top-level requirement.
Parent = int | str | None


def _uv_reachable(source: Source, target: Target, paths: dict[int, list[str]] | None = None) -> dict[int, Parent]:
    """Walk selected uv roots, re-visiting a package when another edge activates more extras."""
    selection = source.uv_selection
    assert selection is not None
    packages = source.uv_packages or []
    roots = uv_selected_roots(source)
    available_groups = {canonicalize_name(name) for name in source.uv_manifest.get("dependency-groups", {})} | {
        name for i in roots for name in uv_group_names(packages[i])
    }
    if selection.only_groups:
        groups = selection.only_groups | selection.groups
    else:
        defaults = available_groups if "*" in source.uv_default_groups else frozenset(source.uv_default_groups)
        groups = selection.groups | defaults
    if selection.no_dev:
        groups = groups - {"dev"}
    groups = frozenset(groups)
    by_name: dict[str, list[int]] = {}
    for i, package in enumerate(source.packages):
        by_name.setdefault(package.key, []).append(i)
    parents: dict[int, Parent] = {}
    active: dict[int, frozenset[str]] = {}
    queue: deque[tuple[int, frozenset[str], list[str]]] = deque()
    paths = paths if paths is not None else {}

    def visit(i: int, parent: Parent, extras: frozenset[str], path: list[str]) -> None:
        if i not in parents:
            parents[i] = parent
            active[i] = extras
            paths[i] = path
            queue.append((i, extras, path))
        elif not extras <= active[i]:
            active[i] |= extras
            queue.append((i, active[i], path))

    def follow(dependencies: list[dict], parent: Parent, extras: frozenset[str], path: list[str]) -> None:
        for dependency in dependencies:
            if not marker_holds(dependency.get("marker"), target, extras=extras, groups=groups):
                continue
            candidates = by_name.get(canonicalize_name(dependency["name"]), [])
            if version := dependency.get("version"):
                exact = [i for i in candidates if source.packages[i].version == version]
                candidates = exact or candidates  # retain conservative fallback for incomplete lock metadata
            requested = frozenset(
                canonicalize_name(extra) for extra in dependency.get("extra", dependency.get("extras", []))
            )
            for i in candidates:
                visit(i, parent, requested, [*path, source.packages[i].name])

    def dependencies(i: int, extras: frozenset[str], path: list[str]) -> None:
        package = packages[i]
        follow(package.get("dependencies", []), i, extras, path)
        for name, optional in package.get("optional-dependencies", {}).items():
            if canonicalize_name(name) in extras:
                follow(optional, i, extras, path)

    if not selection.only_groups:
        for i in roots:
            optional = uv_extra_names(packages[i])
            visit(i, None, optional if selection.all_extras else selection.extras & optional, [source.packages[i].name])
        # Runtime paths take priority over equally short group paths in --explain.
        for _ in roots:
            dependencies(*queue.popleft())
        follow(source.uv_manifest.get("requirements", []), None, selection.extras, [])
    for i in roots:
        for name, group in packages[i].get("dev-dependencies", {}).items():
            if canonicalize_name(name) in groups:
                follow(group, canonicalize_name(name), frozenset(), [f"({canonicalize_name(name)} group)"])
    for name, group in source.uv_manifest.get("dependency-groups", {}).items():
        if canonicalize_name(name) in groups:
            follow(group, canonicalize_name(name), frozenset(), [f"({canonicalize_name(name)} group)"])
    while queue:
        dependencies(*queue.popleft())
    return parents


def reachable(source: Source, target: Target) -> dict[int, Parent]:
    """Packages installed on `target`, each mapped to the parent it was first reached from.

    The walk is breadth-first from the roots, so following the parents gives a shortest path.
    """
    if source.uv_selection is not None:
        return _uv_reachable(source, target)
    by_name: dict[str, list[int]] = {}
    for i, p in enumerate(source.packages):
        by_name.setdefault(p.key, []).append(i)

    def follow(edge: Edge) -> list[int]:
        name, version, marker = edge
        if not marker_holds(marker, target):
            return []
        candidates = by_name.get(canonicalize_name(name), [])
        if version is not None:
            # uv edges carry an exact version; Poetry edges a range (numpy = ">=1.26").
            exact = [i for i in candidates if source.packages[i].version == version]
            if exact:
                return exact
            try:
                spec = SpecifierSet(version)
                ranged = [i for i in candidates if _in_range(source.packages[i].version, spec)]
            except InvalidSpecifier:  # Poetry's "*" or "^1.2": no narrowing
                ranged = []
            candidates = ranged or candidates
        return candidates

    parents: dict[int, Parent] = dict.fromkeys(source.roots)
    queue: deque[int] = deque()

    def visit(j: int, parent: Parent) -> None:
        if j not in parents:
            parents[j] = parent
            queue.append(j)

    # The first level, most telling route first: the project's own dependencies, then top-level
    # requirements, then dependency groups. The set of installed packages is the same in any order;
    # this only decides which route `via` names when a package is reachable more than one way, so a
    # runtime dependency that's also in the dev group isn't reported as "(dev group) → numpy".
    for i in source.roots:
        for edge in (source.edges or {}).get(i, []):
            for j in follow(edge):
                visit(j, i)
    grouped = sorted(range(len(source.root_edges)), key=lambda k: k in source.root_groups)
    for k in grouped:
        for j in follow(source.root_edges[k]):
            visit(j, source.root_groups.get(k))
    while queue:
        i = queue.popleft()
        for edge in (source.edges or {}).get(i, []):
            for j in follow(edge):
                if j not in parents:
                    parents[j] = i
                    queue.append(j)
    return parents


def dependency_path(source: Source, parents: dict[int, Parent], i: int) -> list[str]:
    """Names from where the walk started down to package `i`, e.g. ["(dev group)", "pytest"]."""
    path = [source.packages[i].name]
    parent = parents[i]
    while isinstance(parent, int):
        path.append(source.packages[parent].name)
        parent = parents[parent]
    if isinstance(parent, str):
        path.append(f"({parent} group)")
    return path[::-1]


def _in_range(version: str | None, spec: SpecifierSet) -> bool:
    try:
        return version is not None and spec.contains(version, prereleases=True)
    except InvalidVersion:
        return False


def applies(pkg: Package, target: Target) -> bool:
    return pkg.markers is None or any(marker_holds(m, target) for m in pkg.markers)


def check(sources: list[Source], targets: list[Target]) -> list[Row]:
    rows: list[Row] = []
    for source in sources:
        paths: dict[Target, dict[int, list[str]]] = {target: {} for target in targets}
        installed = (
            {
                t: _uv_reachable(source, t, paths[t]) if source.uv_selection is not None else reachable(source, t)
                for t in targets
            }
            if source.edges is not None
            else None
        )
        for i, pkg in enumerate(source.packages):
            if pkg.skip:
                continue
            resolved_rows: dict[int, Row] = {}
            parsed: dict[int, list[frozenset[Tag]]] = {}
            for t in targets:
                resolved = pkg.resolutions.get(f"{t.major}.{t.minor}", pkg)
                if id(resolved) not in resolved_rows:
                    resolved_rows[id(resolved)] = Row(resolved, dict.fromkeys(targets, Status.NOT_NEEDED))
                    parsed[id(resolved)] = wheel_tags(resolved.files) if resolved.files is not None else []
                row = resolved_rows[id(resolved)]
                files = None if resolved.error else resolved.files
                needed = i in installed[t] if installed is not None else applies(pkg, t)
                row.results[t] = (
                    status_for(files, t, parsed[id(resolved)], resolved.yanked) if needed else Status.NOT_NEEDED
                )
                if needed and installed is not None:
                    row.via[t] = (
                        paths[t][i] if source.uv_selection is not None else dependency_path(source, installed[t], i)
                    )
            rows.extend(resolved_rows.values())
    return rows
