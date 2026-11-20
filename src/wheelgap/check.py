"""Decide, per package and target, whether a prebuilt wheel exists."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache

from packaging.markers import InvalidMarker, Marker
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

try:  # packaging's parsed-marker node types (private, so guarded: without them every comparison is unknown)
    from packaging._parser import Value as _Value
    from packaging._parser import Variable as _Variable
except ImportError:  # pragma: no cover
    _Value = _Variable = None  # type: ignore[assignment,misc]

from .sources import (
    Edge,
    Package,
    PoetryExtras,
    Source,
    UvSelection,
    uv_extra_names,
    uv_group_names,
    uv_selected_roots,
)
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
    # The input requirement that resolved to this row (a range resolved per Python spawns one Row per release).
    input_package: Package | None = field(default=None, repr=False, compare=False)
    # Direct dependencies that reach a transitive package on a target, for concise text/Markdown hints.
    direct_via: dict[Target, tuple[str, ...]] = field(default_factory=dict, repr=False, compare=False)

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


def _installable(
    files: list[str],
    target: Target,
    *,
    no_binary: bool = False,
    only_binary: bool = False,
) -> bool:
    if not no_binary and any(target.accepts(t) for t in wheel_tags(files)):
        return True
    return not only_binary and has_sdist(files) and target.builds_sdists


def status_for(
    files: list[str] | None,
    target: Target,
    _wheels: list[frozenset[Tag]] | None = None,
    yanked: frozenset[str] = frozenset(),
    *,
    no_binary: bool = False,
    only_binary: bool = False,
) -> Status:
    if files is None:
        return Status.UNKNOWN
    if yanked:
        # `yanked` is only non-empty for a release pinned with ==. Like pip, ignore its yanked files
        # unless every file this target could use is yanked; then pip installs them anyway (PEP 592).
        live = [f for f in files if f not in yanked]
        if _installable(live, target, no_binary=no_binary, only_binary=only_binary):
            files = live
        _wheels = None
    wheels = [] if no_binary else (_wheels if _wheels is not None else wheel_tags(files))
    if any(target.accepts(t) for t in wheels):
        return Status.WHEEL
    if only_binary:
        return Status.MISSING
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
    *,
    no_binary: bool = False,
    only_binary: bool = False,
) -> list[tuple[str, str]]:
    """(filename, reason) for every wheel `target` rejects, the closest misses first.

    A wheel the target accepts but status_for() leaves out because it's yanked (the release has other files the
    target can use) is listed with the reason "yanked", and with `no_binary` an accepted wheel is listed as
    "ignored (--no-binary)".
    """
    skips_yanked = bool(yanked) and _installable(
        [f for f in files if f not in yanked], target, no_binary=no_binary, only_binary=only_binary
    )
    out = []
    for f in files:
        if not f.endswith(".whl"):
            continue
        reason = rejection(f, target)
        if reason is None:
            if no_binary:
                reason = "ignored (--no-binary)"
            elif skips_yanked and f in yanked:
                reason = "yanked"
        if reason:
            out.append((f, reason))
    if requires_python_rejected:
        for f, reason in requires_python_rejected.items():
            if f.endswith(".whl"):  # a wheel the target can't use anyway says why, not that a newer Python fits
                out.append((f, rejection(f, target) or reason))
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
    no_binary: bool = False,
    only_binary: bool = False,
) -> list[str]:
    """What --explain says under a gap on `target`: each rejected wheel and why (the closest misses first, at most
    `limit` of them), or that none were published, and a note when the target can't build the sdist there is.

    The command line aligns the reasons in a column, dims them and indents each line; Result.reasons joins them
    unaligned (one space-separated "file  reason" per line) and with no limit.
    """
    rejected = explain(files, target, yanked, requires_python_rejected, no_binary=no_binary, only_binary=only_binary)
    out = [] if rejected else [dim("no wheels published for this version")]
    shown = rejected[:limit]
    width = max((len(f) for f, _ in shown), default=0) if align else 0
    out += [f"{f.ljust(width)}  {dim(reason)}" for f, reason in shown]
    if len(rejected) > len(shown):
        out.append(dim(f"… and {len(rejected) - len(shown)} more (--json lists them all)"))
    if has_sdist(files):
        if not target.builds_sdists:
            out.append(dim(f"there is an sdist, but {target.platform} can't build packages from source"))
        elif only_binary:
            out.append(dim("there is an sdist, but --only-binary forbids building from source"))
    for f, reason in (requires_python_rejected or {}).items():
        if f.endswith(SDIST_SUFFIXES):
            out.append(dim(f"there is an sdist, but it {reason}"))
    return out


_marker_cache: dict[str, Marker | None] = {}


# Marker variables wheelgap can't know for a target: which extras or dependency groups get requested
# (uv writes its [tool.uv] conflicts as extra == '...' terms) and the OS release/version of the machine
# that installs. A comparison on one of them is "unknown", combined with the known parts in three-valued
# logic: "sys_platform != 'darwin' and extra == 'cpu'" is unknown on Linux (so the package counts) and
# false on macOS (so it doesn't). Only a definite False hides a package. Target.unknown_markers adds
# platform_machine on 32-bit x86 and Arm, where it depends on the machine rather than the Python.
_UNKNOWABLE = frozenset({"extra", "extras", "dependency_groups", "platform_release", "platform_version"})
_FULL_VERSION_MARKERS = frozenset({"python_full_version", "implementation_version"})
_RELEASE = re.compile(r"\d+(?:\.\d+)+")
_atom_cache: dict[tuple[str, Target], bool | None] = {}


def _quote(text: str) -> str | None:
    if '"' not in text:
        return f'"{text}"'
    return f"'{text}'" if "'" not in text else None  # both kinds of quote: can't re-serialise safely


def _evaluate_atom(
    text: str,
    node: tuple,
    target: Target,
    base_env: dict,
    full_names: frozenset[str],
    *,
    lock_file: bool,
) -> bool | None:
    try:
        marker = Marker(text)
        evaluate = (lambda env: marker.evaluate(env, context="lock_file")) if lock_file else marker.evaluate
        if not full_names:
            return evaluate(base_env)
        prefix = f"{target.major}.{target.minor}."
        probes = [f"{prefix}0", f"{prefix}999999"]
        # Also every X.Y.Z the value names, however it's written ('3.13.1.*', 'v3.13.2', '3.13.1;3.13.2'): == and
        # in can hold at exactly one patch release.
        for item in node:
            if isinstance(item, _Value):
                for found in _RELEASE.findall(item.value):
                    release = Version(found).release
                    if release[:2] == (target.major, target.minor) and len(release) >= 3:
                        probes.append(".".join(map(str, release[:3])))
        outcomes = {evaluate({**base_env, **{name: patch for name in full_names}}) for patch in probes}
        return outcomes.pop() if len(outcomes) == 1 else None
    except Exception:  # undefined names, odd comparisons: unknown rather than a confident answer
        return None


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
    full_names = (_FULL_VERSION_MARKERS & names) - (environment or {}).keys()
    if environment is not None:
        return _evaluate_atom(
            text,
            node,
            target,
            {**target.marker_env, **environment},
            full_names,
            lock_file=True,
        )
    key = (text, target)
    if key not in _atom_cache:
        _atom_cache[key] = _evaluate_atom(
            text,
            node,
            target,
            target.marker_env,
            full_names,
            lock_file=False,
        )
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


def marker_value(
    marker: str | None,
    target: Target,
    *,
    extras: frozenset[str] | None = None,
    groups: frozenset[str] | None = None,
) -> bool | None:
    """Whether `marker` holds on `target`, or None when the target can't decide it: a marker that doesn't parse, or one
    that depends on something unknown there (platform_release, platform_version, an `extra` when `extras` is None,
    platform_machine on a target whose machine name isn't fixed)."""
    if not marker:
        return True
    m = _parsed_marker(marker)
    if m is None:
        return None
    try:
        if extras is None and groups is None:
            return _evaluate3(m._markers, target)
        # uv's synthetic conflict marker names aren't public extra names. Preserve their
        # existing unknown/conservative treatment rather than hiding a potentially needed wheel.
        synthetic = bool(re.search(r"(?:extra|group)-\d+-", marker))
        contexts = extras | {""} if extras is not None and not synthetic else {None}
        results: set[bool | None] = set()
        for extra in contexts:
            environment: dict = {}
            if extra is not None:
                environment.update(extra=extra, extras=extras)
            if groups is not None:
                environment["dependency_groups"] = groups
            results.add(_evaluate3(m._markers, target, environment))
        return True if True in results else None if None in results else False
    except Exception:  # packaging changed its internals: unknown
        return None


def marker_holds(
    marker: str | None,
    target: Target,
    *,
    extras: frozenset[str] | None = None,
    groups: frozenset[str] | None = None,
) -> bool:
    """Whether `marker` can hold on `target`: unknown counts as holding, to assume a package is needed rather than
    hide a gap."""
    return marker_value(marker, target, extras=extras, groups=groups) is not False


# How a package was reached: the index of the package that depends on it, the name of the
# dependency group that lists it, or None for a root or a top-level requirement.
Parent = int | str | None


def _uv_source_matches(package: dict, edge_source: dict[str, object]) -> bool:
    """Match uv source tables, including git manifests without a resolved commit.

    uv lock --script writes \x60git = "URL"\x60 in the manifest, while the matching
    package's source has \x60git = "URL#commit"\x60.
    """
    locked = package.get("source")
    if locked == edge_source:
        return True
    if not isinstance(locked, dict) or set(locked) != {"git"} or set(edge_source) != {"git"}:
        return False
    requested, resolved = edge_source["git"], locked["git"]
    return isinstance(requested, str) and isinstance(resolved, str) and resolved.startswith(f"{requested}#")


def _narrow_candidates(
    source: Source,
    candidates: list[int],
    version: str | None,
    edge_source: dict[str, object] | None,
    *,
    allow_range: bool = False,
) -> list[int]:
    """Narrow candidate package indices by version (and range for Poetry) and uv edge `source`."""
    if version:
        # uv edges carry an exact version; Poetry edges a range (numpy = ">=1.26").
        matched = [i for i in candidates if source.packages[i].version == version]
        if not matched and allow_range:
            try:
                spec = SpecifierSet(version)
                matched = [i for i in candidates if _in_range(source.packages[i].version, spec)]
            except InvalidSpecifier:  # Poetry's "*" or "^1.2": no narrowing
                matched = []
        candidates = matched or candidates  # retain conservative fallback for incomplete lock metadata
    if edge_source is not None and source.uv_packages is not None:
        uv_packages = source.uv_packages
        sourced = [i for i in candidates if _uv_source_matches(uv_packages[i], edge_source)]
        candidates = sourced or candidates
    return candidates


def _uv_reachable(source: Source, target: Target, paths: dict[int, list[str]] | None = None) -> dict[int, Parent]:
    """Walk selected uv roots, re-visiting a package when another edge activates more extras."""
    selection = source.uv_selection
    everything = selection is None
    if selection is None:
        selection = UvSelection(all_extras=True)
    packages = source.uv_packages or []
    roots = source.roots if everything else uv_selected_roots(source)
    available_groups = {canonicalize_name(name) for name in source.uv_manifest.get("dependency-groups", {})} | {
        name for i in roots for name in uv_group_names(packages[i])
    }
    if selection.only_groups:
        groups = selection.only_groups | selection.groups
    else:
        defaults = available_groups if "*" in source.uv_default_groups else frozenset(source.uv_default_groups)
        groups = selection.groups | defaults
    if everything:  # `uv sync --all-packages --all-extras --all-groups`
        groups = frozenset(available_groups)
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
            candidates = _narrow_candidates(
                source,
                by_name.get(canonicalize_name(dependency["name"]), []),
                dependency.get("version"),
                dependency.get("source"),
            )
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


def dropped_targets(local: list[str], last: list[tuple[str, str | None]], targets: Iterable[Target]) -> list[Target]:
    """`--dist`: targets the last release had an installable wheel for but the checked files don't. `last` is the
    previous release's files as (filename, Requires-Python): a wheel whose Requires-Python leaves the target's Python
    out wasn't installable there, so not shipping one now isn't a drop."""
    local_wheels = wheel_tags(local)
    last_wheels: list[tuple[frozenset[Tag], str | None]] = []
    for name, requires in last:
        if name.endswith(".whl") and (tags := _parsed_tags(name)) is not None:
            last_wheels.append((tags, requires))

    def fits(requires: str | None, target: Target) -> bool:
        if requires is None:
            return True
        try:  # a real interpreter is on a later patch than X.Y.0, as pip compares it
            return SpecifierSet(requires).contains(f"{target.major}.{target.minor}.99")
        except InvalidSpecifier:
            return True

    return [
        t
        for t in targets
        if any(t.accepts(tags) and fits(requires, t) for tags, requires in last_wheels)
        and not any(t.accepts(tags) for tags in local_wheels)
    ]


def reachable(source: Source, target: Target) -> dict[int, Parent]:
    """Packages installed on `target`, each mapped to the parent it was first reached from.

    The walk is breadth-first from the roots, so following the parents gives a shortest path.
    """
    if source.uv_selection is not None or source.uv_packages is not None:
        return _uv_reachable(source, target)
    by_name: dict[str, list[int]] = {}
    for i, p in enumerate(source.packages):
        by_name.setdefault(p.key, []).append(i)

    def follow(edge: Edge) -> list[int]:
        name, version, marker, edge_source = edge
        if not marker_holds(marker, target):
            return []
        return _narrow_candidates(
            source,
            by_name.get(canonicalize_name(name), []),
            version,
            edge_source,
            allow_range=True,
        )

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


def _selected_direct_dependencies(
    source: Source, target: Target, installed: dict[int, Parent] | dict[int, list[str]], selection: UvSelection | None
) -> dict[int, tuple[str, ...]]:
    """Attribute selected uv dependencies per direct root, keeping requested extras separate. With no --extra/--group
    selection, `selection` is None: the whole lock is checked (all members, extras, and groups)."""
    packages = source.uv_packages or []
    everything = selection is None
    if selection is None:
        selection = UvSelection(all_extras=True)
    roots = source.roots if everything else uv_selected_roots(source)
    available = {canonicalize_name(n) for n in source.uv_manifest.get("dependency-groups", {})} | {
        n for i in roots for n in uv_group_names(packages[i])
    }
    if selection.only_groups:
        selected = selection.only_groups | selection.groups
    else:
        defaults = available if "*" in source.uv_default_groups else frozenset(source.uv_default_groups)
        selected = selection.groups | defaults
    if everything:
        selected = frozenset(available)
    groups = frozenset(selected - {"dev"} if selection.no_dev else selected)
    by_name: dict[str, list[int]] = {}
    for i, pkg in enumerate(source.packages):
        by_name.setdefault(pkg.key, []).append(i)
    chosen = set(installed)
    direct = set(roots) & chosen
    owners: dict[int, dict[str, str]] = {}
    active: dict[tuple[int, str], frozenset[str]] = {}
    queue: deque[tuple[int, str]] = deque()
    # The project (or, in a virtual workspace, every member) is where hints start; other members are named.
    direct_roots = set(uv_selected_roots(source))

    def visit(i: int, owner: tuple[str, str], requested: frozenset[str]) -> None:
        key, label = owner
        state = (i, key)
        previous = active.get(state)
        if previous is None:
            active[state] = requested
            owners.setdefault(i, {})[key] = label
            queue.append(state)
        elif not requested <= previous:
            active[state] = previous | requested
            queue.append(state)

    def follow(items: list[dict], owner: tuple[str, str] | None, extras: frozenset[str]) -> None:
        for d in items:
            if not marker_holds(d.get("marker"), target, extras=extras, groups=groups):
                continue
            matches = _narrow_candidates(
                source, by_name.get(canonicalize_name(d["name"]), []), d.get("version"), d.get("source")
            )
            requested = frozenset(canonicalize_name(name) for name in d.get("extra", d.get("extras", [])))
            for i in matches:
                if i not in chosen:
                    continue
                if owner is None:
                    direct.add(i)
                    visit(i, (source.packages[i].key, source.packages[i].name), requested)
                else:
                    visit(i, owner, requested)

    if not selection.only_groups:
        for i in roots:
            p = packages[i]
            possible = uv_extra_names(p)
            extras = possible if selection.all_extras else selection.extras & possible
            owner = None if i in direct_roots else (source.packages[i].key, source.packages[i].name)
            follow(p.get("dependencies", []), owner, extras)
            for name, optional in p.get("optional-dependencies", {}).items():
                if canonicalize_name(name) in extras:
                    follow(optional, owner, extras)
        follow(source.uv_manifest.get("requirements", []), None, selection.extras)
    for i in roots:  # a member's group, like the project's, starts at the group: --explain shows `(NAME group) → …`
        for name, items in packages[i].get("dev-dependencies", {}).items():
            if canonicalize_name(name) in groups:
                follow(items, None, frozenset())
    for name, items in source.uv_manifest.get("dependency-groups", {}).items():
        if canonicalize_name(name) in groups:
            follow(items, None, frozenset())

    while queue:
        i, key = queue.popleft()
        p, extras = packages[i], active[i, key]
        owner = (key, owners[i][key])
        follow(p.get("dependencies", []), owner, extras)
        for name, optional in p.get("optional-dependencies", {}).items():
            if canonicalize_name(name) in extras:
                follow(optional, owner, extras)
    return {i: tuple(found.values()) for i, found in owners.items() if i not in direct}


def direct_dependencies(
    source: Source, target: Target, installed: dict[int, Parent] | dict[int, list[str]]
) -> dict[int, tuple[str, ...]]:
    """Map transitive packages to distinct direct dependencies, without counting duplicate paths."""
    if source.edges is None:
        return {}
    if source.uv_packages is not None:  # a uv.lock: walk extras per owner, selected or uv's defaults
        return _selected_direct_dependencies(source, target, installed, source.uv_selection)
    by_name: dict[str, list[int]] = {}
    for i, pkg in enumerate(source.packages):
        by_name.setdefault(pkg.key, []).append(i)
    chosen = set(installed)

    def follow(edges: Iterable[Edge]) -> list[int]:
        found: list[int] = []
        for name, version, marker, origin in edges:
            # An edge that depends on an extra: the graph doesn't say which owner asked for that extra, so it
            # credits nobody (no hint) rather than every direct dependency that reaches the package.
            if marker is not None and "extra" in str(marker):
                continue
            if marker_holds(marker, target):
                found.extend(
                    i
                    for i in _narrow_candidates(
                        source, by_name.get(canonicalize_name(name), []), version, origin, allow_range=True
                    )
                    if i in chosen
                )
        return found

    adjacency = {i: follow(source.edges.get(i, [])) for i in chosen}
    direct = set(source.roots) & chosen
    owners: dict[int, dict[str, str]] = {}
    queue: deque[tuple[int, str]] = deque()

    def seed(i: int) -> None:
        direct.add(i)
        key, name = source.packages[i].key, source.packages[i].name
        if key not in owners.setdefault(i, {}):
            owners[i][key] = name
            queue.append((i, key))

    if source.kind == "poetry":
        # Poetry 1.x infers direct packages as its roots. Unlike a uv workspace,
        # these roots themselves are the direct dependencies of the project.
        for i in source.roots:
            seed(i)
    else:
        for i in source.roots:
            for child in adjacency.get(i, []):
                seed(child)
    for k in sorted(range(len(source.root_edges)), key=lambda j: j in source.root_groups):
        for child in follow([source.root_edges[k]]):
            seed(child)
    while queue:
        i, owner = queue.popleft()
        for child in adjacency.get(i, []):
            label = owners[i][owner]
            if owner not in owners.setdefault(child, {}):
                owners[child][owner] = label
                queue.append((child, owner))
    return {i: tuple(found.values()) for i, found in owners.items() if i not in direct}


HINTED = (Status.SOURCE, Status.MISSING, Status.UNKNOWN)  # the statuses the table's (via ...) hint is shown for


def hint_direct_dependencies(
    source: Source, target: Target, installed: dict[int, Parent] | dict[int, list[str]]
) -> dict[int, tuple[str, ...]]:
    """direct_dependencies for the table's `(via ...)` hint. It's only a hint, so a lockfile shape it doesn't expect
    must never cost the check itself: any failure means no hint."""
    try:
        return direct_dependencies(source, target, installed)
    except Exception:
        return {}


def check(sources: list[Source], targets: list[Target]) -> list[Row]:
    rows: list[Row] = []
    for source in sources:
        paths: dict[Target, dict[int, list[str]]] = {target: {} for target in targets}
        installed: dict[Target, dict[int, Parent]] | dict[Target, dict[int, list[str]]] | None
        if source.deps_paths is not None:  # --deps: resolved per target already
            paths = {t: source.deps_paths.get(t, {}) for t in targets}
            installed = paths
        elif source.edges is not None:
            installed = {
                t: _uv_reachable(source, t, paths[t]) if source.uv_packages is not None else reachable(source, t)
                for t in targets
            }
        else:
            installed = None
        # The (via ...) hint, per target, worked out only when a gap needs it, so gap-free runs never pay. Text and
        # Markdown tables show it, and `--json --explain` writes it as `direct`.
        hints: dict[Target, dict[int, tuple[str, ...]]] = {}
        for i, pkg in enumerate(source.packages):
            if pkg.skip:
                continue
            resolved_rows: dict[int, Row] = {}
            parsed: dict[int, list[frozenset[Tag]]] = {}
            for t in targets:
                resolved = pkg.resolutions.get(f"{t.major}.{t.minor}", pkg)
                if id(resolved) not in resolved_rows:
                    resolved_rows[id(resolved)] = Row(
                        resolved, dict.fromkeys(targets, Status.NOT_NEEDED), input_package=pkg
                    )
                    parsed[id(resolved)] = wheel_tags(resolved.files) if resolved.files is not None else []
                row = resolved_rows[id(resolved)]
                files = None if resolved.error else resolved.files
                needed = i in installed[t] if installed is not None else applies(pkg, t)
                row.results[t] = (
                    status_for(
                        files,
                        t,
                        parsed[id(resolved)],
                        resolved.yanked,
                        no_binary=resolved.no_binary,
                        only_binary=resolved.only_binary,
                    )
                    if needed
                    else Status.NOT_NEEDED
                )
                if needed and installed is not None:
                    row.via[t] = (
                        paths[t][i]
                        if source.uv_packages is not None or source.deps_paths is not None
                        else dependency_path(source, installed[t], i)  # type: ignore[arg-type]
                    )
                    if source.deps_paths is not None:
                        direct = (source.deps_direct or {}).get(t, {}).get(i)
                        if direct:
                            row.direct_via[t] = direct
                        elif source.deps_direct is None and len(row.via[t]) > 1:
                            row.direct_via[t] = (row.via[t][0],)
                    elif row.results[t] in HINTED and source.edges is not None:
                        if t not in hints:
                            hints[t] = hint_direct_dependencies(source, t, installed[t])
                        if direct := hints[t].get(i):
                            row.direct_via[t] = direct
            rows.extend(resolved_rows.values())
    return rows
