"""Decide, per package and target, whether a prebuilt wheel exists."""

from __future__ import annotations

import itertools
import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
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
    MarkerGroup,
    Package,
    PoetryExtras,
    Source,
    SourceError,
    UvSelection,
    uv_conflict_name,
    uv_extra_names,
    uv_group_names,
    uv_selected_roots,
)
from .targets import (
    ANDROID_FLOOR,
    IOS_FLOOR,
    MANYLINUX_FLOOR,
    MANYLINUX_X86_ARCHES,
    NEWEST_PYTHON,
    PLATFORMS,
    Target,
)


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
    # Why a target is UNKNOWN when the lookup didn't fail: a Poetry 1.x lock checked without its project's dependencies
    # (#815), where no dependency needs the package there but the project may require it directly.
    unknown_reasons: dict[Target, str] = field(default_factory=dict, repr=False, compare=False)

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


# --explain's reason for an abi3 wheel on a free-threaded target; --fix names it for a gap with no fix (fix.abi3_only).
ABI3_ON_FREETHREADED = "abi3 wheels can't be used on free-threaded Python (only abi3t ones can)"


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
                return ABI3_ON_FREETHREADED
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
    hash_rejected: dict[str, str] | None = None,
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
    if hash_rejected:
        for f, reason in hash_rejected.items():
            if f.endswith(".whl"):
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
    hash_rejected: dict[str, str] | None = None,
) -> list[str]:
    """What --explain says under a gap on `target`: each rejected wheel and why (the closest misses first, at most
    `limit` of them), or that none were published, and a note when the target can't build the sdist there is.

    The command line aligns the reasons in a column, dims them and indents each line; Result.reasons joins them
    unaligned (one space-separated "file  reason" per line) and with no limit.
    """
    rejected = explain(
        files,
        target,
        yanked,
        requires_python_rejected,
        no_binary=no_binary,
        only_binary=only_binary,
        hash_rejected=hash_rejected,
    )
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


@dataclass(frozen=True)
class ConflictWorld:
    """Which of a uv.lock's conflict items are on in one install (see _conflict_worlds): `active` of the `known` ones,
    as uv_conflict_name names. A marker's `extra == 'extra-4-demo-old'` holds exactly when that name is active."""

    active: frozenset[str]
    known: frozenset[str]


def _conflict_atom(node: tuple, world: ConflictWorld) -> bool | None:
    """`extra == NAME` or `extra != NAME` for a conflict item of the lock, as uv evaluates it: whether NAME is in the
    activated set (UniversalMarker::evaluate). Anything else is None."""
    if len(node) != 3 or str(node[1].value) not in ("==", "!="):
        return None
    value = node[2] if isinstance(node[0], _Variable) else node[0]
    if not isinstance(value, _Value) or (name := canonicalize_name(value.value)) not in world.known:
        return None
    return (name in world.active) == (str(node[1].value) == "==")


def _atom(
    node: tuple,
    target: Target | None,
    environment: dict | None = None,
    poetry_extras: PoetryExtras | None = None,
    conflicts: ConflictWorld | None = None,
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
    if conflicts is not None and names == {"extra"} and (decided := _conflict_atom(node, conflicts)) is not None:
        return decided
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
    markers: list,
    target: Target | None,
    environment: dict | None = None,
    poetry_extras: PoetryExtras | None = None,
    conflicts: ConflictWorld | None = None,
) -> bool | None:
    """Kleene evaluation of packaging's parsed marker list: `and` binds tighter than `or`."""
    groups: list[list[bool | None]] = [[]]
    for item in markers:
        if item == "or":
            groups.append([])
        elif item == "and":
            continue
        elif isinstance(item, list):
            groups[-1].append(_evaluate3(item, target, environment, poetry_extras, conflicts))
        else:
            groups[-1].append(_atom(item, target, environment, poetry_extras, conflicts))
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
    conflicts: ConflictWorld | None = None,
) -> bool | None:
    """Whether `marker` holds on `target`, or None when the target can't decide it: a marker that doesn't parse, or one
    that depends on something unknown there (platform_release, platform_version, an `extra` when `extras` is None,
    platform_machine on a target whose machine name isn't fixed). `conflicts` decides a uv.lock's conflict items."""
    if not marker:
        return True
    m = _parsed_marker(marker)
    if m is None:
        return None
    try:
        if extras is None and groups is None:
            return _evaluate3(m._markers, target, conflicts=conflicts)
        # uv's synthetic conflict marker names aren't public extra names: unknown, so a possibly needed wheel isn't
        # hidden, unless `conflicts` says which of them are on.
        synthetic = bool(re.search(r"(?:extra|group|project)-\d+-", marker))
        contexts = extras | {""} if extras is not None and not synthetic else {None}
        results: set[bool | None] = set()
        for extra in contexts:
            environment: dict = {}
            if extra is not None:
                environment.update(extra=extra, extras=extras)
            if groups is not None:
                environment["dependency_groups"] = groups
            results.add(_evaluate3(m._markers, target, environment, conflicts=conflicts))
        return True if True in results else None if None in results else False
    except Exception:  # packaging changed its internals: unknown
        return None


def marker_holds(
    marker: str | None,
    target: Target,
    *,
    extras: frozenset[str] | None = None,
    groups: frozenset[str] | None = None,
    conflicts: ConflictWorld | None = None,
) -> bool:
    """Whether `marker` can hold on `target`: unknown counts as holding, to assume a package is needed rather than
    hide a gap."""
    return marker_value(marker, target, extras=extras, groups=groups, conflicts=conflicts) is not False


# How dep_logic 0.8 (PDM 2.29's) names an architecture in a platform string, as wheelgap's targets.py does.
_PDM_ARCHES = {"i386": "x86", "i686": "x86", "amd64": "x86_64", "arm64": "aarch64"}
_PDM_KNOWN_ARCHES = frozenset(
    {"aarch64", "armv6l", "armv7l", "ppc64le", "ppc64", "x86", "x86_64", "s390x", "riscv64", "loongarch64"}
)
_PDM_PLATFORM = re.compile(r"(?P<os>manylinux|macos|musllinux)_(?P<major>\d+?)_(?P<minor>\d+?)_(?P<arch>[a-z0-9_]+)$")
_PDM_ALIASES = {
    "linux": ("manylinux", (2, 40), "x86_64"),
    "windows": ("windows", None, "x86_64"),
    "macos": ("macos", (14, 0), "aarch64"),
    "alpine": ("musllinux", (1, 2), "x86_64"),
    "macos_arm64": ("macos", (14, 0), "aarch64"),
    "macos_x86_64": ("macos", (14, 0), "x86_64"),
}
_PDM_RELEASE_OSES = frozenset({"freebsd", "netbsd", "openbsd", "dragonfly", "illumos", "haiku"})


def _pdm_platform(text: str) -> tuple[str, tuple[int, int] | None, str] | None:
    """A pdm.lock target's platform as (os, version, arch), the way PDM reads it (dep_logic's Platform.parse:
    https://github.com/pdm-project/dep-logic/blob/0.8.0/src/dep_logic/tags/platform.py), or None where PDM
    can't read it. A system other than manylinux, musllinux, macOS and Windows (freebsd_..., emscripten_...) is
    "other"."""
    if text in _PDM_ALIASES:
        return _PDM_ALIASES[text]
    if text.startswith("windows_"):
        os_name, version, arch = "windows", None, text.split("_", 1)[1]
    elif m := _PDM_PLATFORM.match(text):
        os_name, version, arch = m["os"], (int(m["major"]), int(m["minor"])), m["arch"]
    elif "_" in text:
        os_name, version, arch = "other", None, text.split("_", 1)[1]
        if text.split("_", 1)[0] in _PDM_RELEASE_OSES:
            arch = arch.partition("_")[2]
    else:
        return None
    arch = _PDM_ARCHES.get(arch, arch)
    return (os_name, version, arch) if arch in _PDM_KNOWN_ARCHES else None


def _pdm_target_platform(target: Target) -> tuple[str, tuple[int, int] | None, str] | None:
    """A wheelgap target as (os, version, arch) in _pdm_platform's terms; None for Pyodide, iOS and Android."""
    family, arch = PLATFORMS[target.platform]
    if family == "windows":
        return "windows", None, {"win_amd64": "x86_64", "win_arm64": "aarch64", "win32": "x86"}[arch]
    version = {"manylinux": target.glibc, "musllinux": target.musl, "macos": target.macos}.get(family)
    return None if version is None else (family, version, _PDM_ARCHES.get(arch, arch))


def pdm_python_value(requires: str, target: Target) -> bool | None:
    """Whether a lock target's requires_python takes the target's Python, at every patch release (True), at none
    (False), or at some only (None), probing as _evaluate_atom does for python_full_version."""
    if not requires.strip():
        return True
    try:
        spec = SpecifierSet(requires)
    except InvalidSpecifier:
        return None
    probes = [Version(f"{target.major}.{target.minor}.0"), Version(f"{target.major}.{target.minor}.999999")]
    for s in spec:
        try:
            release = Version(s.version.removesuffix(".*")).release
        except InvalidVersion:
            return None
        if release[:2] == (target.major, target.minor) and len(release) >= 3:
            patch = release[2]
            probes += [Version(f"{target.major}.{target.minor}.{p}") for p in (patch - 1, patch, patch + 1) if p >= 0]
    outcomes = {spec.contains(v, prereleases=True) for v in probes}
    return outcomes.pop() if len(outcomes) == 1 else None


def pdm_target_value(entry: dict, target: Target) -> bool | None:
    """Whether `pdm install` takes one of a pdm.lock's `[[metadata.targets]]` on `target`: True, False, or None
    when it can't be told (a patch-level requires_python, a platform or key PDM 2.29 doesn't know).

    PDM installs where a lock target compares as LOWER_OR_EQUAL or HIGHER, and fails with "No compatible lock target
    found" where every one is INCOMPATIBLE (dep_logic's EnvSpec.compare, called from pdm/cli/actions.py
    resolve_from_lockfile): the Pythons must overlap, an implementation the target names must be the same one with
    the same gil_disabled, and a platform must have the same operating system and architecture. A platform's glibc,
    musl or macOS version doesn't matter: PDM installs a manylinux_2_40 lock on glibc 2.36, with a warning, and the
    lock keeps every file an older version would take.
    """
    if set(entry) - {"requires_python", "platform", "implementation", "gil_disabled"} or "requires_python" not in entry:
        return None  # PDM 2.29 can't read it either (EnvSpec.from_spec(**entry))
    values = {pdm_python_value(entry["requires_python"], target)}
    implementation, gil_disabled = entry.get("implementation"), entry.get("gil_disabled", False)
    if implementation:  # wheelgap's targets are CPython
        if implementation not in ("cpython", "pypy", "pyston") or (gil_disabled and implementation != "cpython"):
            values.add(None)
        else:
            values.add(implementation == "cpython" and gil_disabled == target.freethreaded)
    if platform := entry.get("platform"):
        locked, wanted = _pdm_platform(platform), _pdm_target_platform(target)
        if locked is None or (wanted is None and locked[0] == "other"):
            values.add(None)
        else:
            values.add(wanted is not None and locked[0] == wanted[0] and locked[2] == wanted[2])
    return False if False in values else None if None in values else True


def _pdm_target_text(entry: dict) -> str:
    parts = [f"requires_python {entry.get('requires_python') or 'any'}"]
    parts += [f"{key} {entry[key]}" for key in ("platform", "implementation") if entry.get(key)]
    if entry.get("implementation"):  # only with an implementation does PDM compare it (EnvSpec.from_spec)
        parts.append(f"gil_disabled {'true' if entry.get('gil_disabled') else 'false'}")
    return ", ".join(parts)


def _locks_environments(source: Source) -> bool:
    return bool(source.supported_markers or source.pdm_targets)


def _locked_for(source: Source) -> tuple[str, str, str, str]:
    """For messages: what the lock was locked for, those environments as written, the command that fails outside
    them, and how to lock for more."""
    if source.pdm_targets and not source.supported_markers:
        return (
            "`pdm lock` targets",
            "; ".join(_pdm_target_text(entry) for entry in source.pdm_targets),
            "`pdm install`",
            "lock for them too with `pdm lock --append --platform ... --python ...`",
        )
    return (
        "`[tool.uv] environments`",
        "; ".join(source.supported_markers),
        "`uv sync`",
        "add them to environments and run `uv lock`",
    )


def lock_environment_targets(source: Source, targets: Iterable[Target]) -> tuple[list[Target], list[Target]]:
    """The targets a lock's environments leave out, and those they can't be decided for.

    For a uv.lock, the environments are its supported-markers, the project's `[tool.uv] environments`. `uv sync`
    installs the lock only where one of them holds, and fails anywhere else ("The current Python platform is not
    compatible with the lockfile's supported environments"):
    https://github.com/astral-sh/uv/blob/0.13.9/crates/uv-environment-operations/src/sync.rs#L174-L196
    A target is undecided when no marker holds for sure and one depends on something the target doesn't fix
    (platform_release, platform_machine on win32, a patch release of Python) or doesn't parse.

    For a pdm.lock, they are its `[[metadata.targets]]`, as pdm_target_value reads them.
    """
    outside: list[Target] = []
    undecided: list[Target] = []
    if _locks_environments(source):
        for target in targets:
            values = {marker_value(marker, target) for marker in source.supported_markers} | {
                pdm_target_value(entry, target) for entry in source.pdm_targets
            }
            if True not in values:
                (undecided if None in values else outside).append(target)
    return outside, undecided


def _pdm_covered_python(source: Source, targets: list[Target]) -> str | None:
    """The newest Python, X.Y, that a pdm.lock's targets take on one of `targets`' platforms, for a hint."""
    for minor in range(NEWEST_PYTHON[1], 7, -1):
        for target in targets:
            candidate = replace(target, major=3, minor=minor, freethreaded=False)
            if any(pdm_target_value(entry, candidate) for entry in source.pdm_targets):
                return f"3.{minor}"
    return None


def check_lock_environments(source: Source, targets: Iterable[Target], *, default_python: bool = False) -> None:
    """SourceError unless every target is in the environments a uv.lock or pdm.lock was locked for.

    uv resolves such a lock for its environments only: it drops the packages only other environments need (a
    Windows-only dependency in a Linux-only lock) and the wheels for other platforms, so reading it for another
    target would report gaps that aren't there and miss packages that are. `uv sync` refuses to install it there.
    PDM does the same for a lock made with `pdm lock --platform` (or `--python`), and `pdm install` refuses it.
    """
    targets = list(targets)
    outside, undecided = lock_environment_targets(source, targets)
    if not outside and not undecided:
        return
    # A pdm.lock's targets name Pythons too, so its message always does (the running one may be the cause).
    with_python = len({t.python for t in targets}) > 1 or bool(source.pdm_targets)

    def names(some: list[Target]) -> str:
        return ", ".join(t.label(with_python) for t in some)

    what, environments, installer, more = _locked_for(source)
    if not outside:
        raise SourceError(
            f"{source.label}: can't tell whether this lock covers {names(undecided)}: it was locked for "
            f"{what} only ({environments}), which those targets don't decide, and {installer} fails "
            f"outside them; check targets the {'markers' if source.supported_markers else 'lock targets'} decide"
        )
    covered = [t for t in targets if t not in outside and t not in undecided]
    raise SourceError(
        f"{source.label}: this lock doesn't cover {names(outside)}: it was locked for {what} "
        f"only ({environments}), so {installer} fails there and the lock leaves out the packages and wheels other "
        "environments need; "
        + (
            f"of the targets asked for it covers {names(covered)}"
            if covered
            else "it covers none of the targets asked for"
        )
        + f" (to check the others, {more})"
        + _default_python_hint(source, outside, default_python)
    )


def _default_python_hint(source: Source, outside: list[Target], default_python: bool) -> str:
    """For a pdm.lock checked on the running Python (no -p, `python` setting or .python-version): say so, and which
    Python the lock covers."""
    if not default_python or not source.pdm_targets:
        return ""
    pythons = ", ".join(dict.fromkeys(t.python for t in outside))
    covered = _pdm_covered_python(source, outside)
    return (
        f"; Python {pythons} is the default, the one wheelgap runs on (no -p, `python` setting or .python-version): "
        + (f"pass -p {covered} for a Python the lock covers, or " if covered else "")
        + f"run `pdm lock --append --python {outside[0].major}.{outside[0].minor}` to lock for it"
    )


def narrow_to_lock_environments(
    sources: Iterable[Source], targets: list[Target], *, every: bool = False
) -> tuple[list[Target], list[str]]:
    """Targets nobody named (the default platforms, or --advisory ones) without the platforms a lock made for some
    environments only (a uv.lock's `[tool.uv] environments`, a pdm.lock's targets) leaves out for every Python asked
    for, and those platforms. With several locks a platform goes when one of them leaves it out; with every=True
    (`wheelgap diff`) only when all of them do, so a platform one side gives up stays and check_lock_environments
    refuses it. A platform covered for some of the Pythons only stays, and is refused there too. Nothing goes when no
    target would be left."""
    sources = list(sources)
    if not any(_locks_environments(source) for source in sources):
        return targets, []
    # A lock without environments leaves nothing out, so with every=True it keeps every platform.
    outside = [set(lock_environment_targets(source, targets)[0]) for source in sources]
    unlocked = set.intersection(*outside) if every else set.union(*outside)
    locked = {t.platform for t in targets if t not in unlocked}
    left_out = list(dict.fromkeys(t.platform for t in targets if t.platform not in locked))
    if not locked or not left_out:
        return targets, []
    return [t for t in targets if t.platform in locked], left_out


def lock_environments_label(sources: Iterable[Source]) -> str:
    """For notes: the locks made for some environments only, and what they were locked for ("uv.lock is locked for
    `[tool.uv] environments` only")."""
    limited = [source for source in sources if _locks_environments(source)]
    labels = list(dict.fromkeys(source.label for source in limited))
    whats = list(dict.fromkeys(_locked_for(source)[0] for source in limited))
    if len(labels) == 1:
        return f"{labels[0]} is locked for {whats[0]} only"
    return f"{', '.join(labels[:-1])} and {labels[-1]} are each locked for {' or '.join(whats)} only"


def lock_environments_note(sources: Iterable[Source], left_out: list[str], kept: list[Target], what: str) -> str:
    sources = list(sources)
    checked = ", ".join(dict.fromkeys(t.platform for t in kept))
    several = len({source.label for source in sources if _locks_environments(source)}) > 1
    return (
        f"{lock_environments_label(sources)}, which leave out {', '.join(left_out)}: "
        f"checking the {what} {'they all cover' if several else 'it covers'} ({checked})"
    )


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


def _uv_manifest_fork(source: Source, candidates: list[int], target: Target) -> list[int]:
    """The entries of a uv.lock manifest dependency that `uv sync` can install on `target`.

    The `[manifest]` requirements (a script's dependencies) and dependency groups (a virtual workspace root's) name
    only the package, not the `version` and `marker` a package's edges carry, so a package uv forked per Python or
    platform matches an entry per fork. uv takes the one entry whose own `resolution-markers` hold on the target, or
    that has none (find_by_markers in https://github.com/astral-sh/uv/blob/0.13.9/crates/uv-lock/src/lock/mod.rs,
    commit 6b2d39dcc390, called for the manifest's requirements and dependency groups in to_resolution_from_packages
    in crates/uv-lock/src/lock/installable.rs, which `uv sync` and `uv sync --script` always run with the manifest).
    It evaluates them without extras; it writes them simplified against requires-python, which doesn't change them
    on a target inside it. A marker the target can't decide counts as holding, and when no entry fits (uv would fail:
    MissingRootPackage) every entry is kept, so a gap is never hidden."""
    if len(candidates) < 2:
        return candidates
    packages = source.uv_packages or []
    kept: list[int] = []
    for i in candidates:
        markers = packages[i].get("resolution-markers", [])
        if not isinstance(markers, list) or not all(isinstance(m, str) for m in markers):
            return candidates
        if not markers or any(marker_holds(m, target) for m in markers):
            kept.append(i)
    return kept or candidates


def _uv_reachable(source: Source, target: Target, paths: dict[int, list[str]] | None = None) -> dict[int, Parent]:
    """Walk selected uv roots, re-visiting a package when another edge activates more extras. In a lock with
    `conflicts`, the packages any of the installs _conflict_worlds lists makes, each with its shortest path."""
    worlds = _conflict_worlds(source, target)
    if worlds is None:
        return _uv_walk(source, target, paths)[0]
    parents: dict[int, Parent] = {}
    shortest: dict[int, list[str]] = {}
    for _world, _dropped, found, found_paths in worlds:
        for i, parent in found.items():
            if i not in parents or len(found_paths[i]) < len(shortest[i]):
                parents[i], shortest[i] = parent, found_paths[i]
    if paths is not None:
        paths.update(shortest)
    return parents


# More ways of picking a lock's conflicts than this, and its conflict items stay unknown.
_MAX_CONFLICT_PICKS = 64


def _conflict_worlds(
    source: Source, target: Target
) -> list[tuple[ConflictWorld, frozenset[str], dict[int, Parent], dict[int, list[str]]]] | None:
    """The installs `uv sync` can make of a uv.lock with `conflicts`, on `target`, for its selection: each as the
    conflict items it turns on, the ones the pick turned off, and what the walk reaches. None when the lock has no
    conflicts, or when this can't be worked out as uv does, which leaves the items unknown as before.

    uv writes a lock's markers with its conflict items as `extra == 'extra-4-demo-old'` terms, and with clauses that
    hold only when two sides of one conflict are on, such as `extra == 'extra-4-demo-new' and extra ==
    'extra-4-demo-old'` (UniversalMarker::imbibe keeps them so a lock reader can find a conflict an edge turns on). At
    install time it evaluates each marker against the items that are on: the project, each selected extra that has
    dependencies and each selected group of the packages it installs, plus every extra an edge asks for (`pkg[x]`),
    found in a first walk; and it refuses an install that turns on two extras of one conflict
    (to_resolution_from_packages in
    https://github.com/astral-sh/uv/blob/0.13.9/crates/uv-lock/src/lock/installable.rs). A selection that asks for
    two items of one conflict (as wheelgap's default, every extra and group, does), `uv sync` refuses
    (detect_conflicts in crates/uv-environment-operations/src/lib.rs), so wheelgap takes each way of turning on none
    or one of them, as `uv sync --no-extra ...` would, and checks what any of them installs.
    """
    sets = source.uv_conflicts
    # A conflict between whole projects would mean leaving members out of the install: not worked out here.
    if not sets or any(name.startswith("project-") for items in sets for name in items):
        return None
    known = frozenset(name for items in sets for name in items)
    selection = source.uv_selection
    everything = selection is None
    if selection is None:
        selection = UvSelection(all_extras=True)
    packages = source.uv_packages or []
    roots = source.roots if everything else uv_selected_roots(source)
    groups = _uv_groups(source, selection, everything, roots)
    asked: set[str] = set()  # what the selection asks for, as detect_conflicts counts it
    seed: set[str] = set()  # what uv turns on for it before following edges
    for i in roots:
        name = canonicalize_name(packages[i].get("name", ""))
        if not selection.only_groups:
            seed.add(uv_conflict_name(name, "project"))
            wanted = known if selection.all_extras else {uv_conflict_name(name, "extra", e) for e in selection.extras}
            asked |= {n for n in known if n.startswith(f"extra-{len(name)}-{name}-") and n in wanted}
            for extra in packages[i].get("optional-dependencies", {}):
                if (n := uv_conflict_name(name, "extra", extra)) in wanted:
                    seed.add(n)
        chosen = {uv_conflict_name(name, "group", g) for g in groups}
        asked |= {n for n in known if n.startswith(f"group-{len(name)}-{name}-") and (everything or n in chosen)}
        seed |= {
            n for g in packages[i].get("dev-dependencies", {}) if (n := uv_conflict_name(name, "group", g)) in chosen
        }
    choices: list[list[frozenset[str]]] = []
    for items in sets:
        on = [name for name in dict.fromkeys(items) if name in asked]
        if len(on) >= 2:  # none of them, or one
            choices.append([frozenset(on), *(frozenset(on) - {keep} for keep in on)])
    picks = list(dict.fromkeys(frozenset().union(*combo) for combo in itertools.product(*choices)))
    if len(picks) > _MAX_CONFLICT_PICKS:
        return None
    worlds = []
    for dropped in picks:
        start = frozenset(seed - dropped)
        active = start
        for _ in range(len(known) + 2):
            world = ConflictWorld(active, known)
            found_paths: dict[int, list[str]] = {}
            found, turned_on = _uv_walk(source, target, found_paths, world, dropped, start)
            if turned_on <= active:
                break
            active |= turned_on
        else:
            return None
        if start | turned_on != active:
            # An extra an edge turned on is no longer reached once it's on: uv's answer depends on the order it
            # walks the lock in.
            return None
        if any(sum(name in active for name in items if name.startswith("extra-")) >= 2 for items in sets):
            continue  # uv refuses this install: an edge turns on a second extra of a conflict
        worlds.append((world, dropped, found, found_paths))
    return worlds or None


def _uv_groups(source: Source, selection: UvSelection, everything: bool, roots: list[int]) -> frozenset[str]:
    """The dependency groups a uv.lock walk follows for `selection`."""
    packages = source.uv_packages or []
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
    return frozenset(groups)


def _uv_walk(
    source: Source,
    target: Target,
    paths: dict[int, list[str]] | None = None,
    world: ConflictWorld | None = None,
    dropped: frozenset[str] = frozenset(),
    early: frozenset[str] = frozenset(),
) -> tuple[dict[int, Parent], frozenset[str]]:
    """_uv_reachable's walk, and with a conflict `world` the conflict items its edges turn on (`pkg[x]`). `dropped`:
    conflict items of the roots the pick leaves off (their extras and groups aren't followed). `early`: the items uv
    has on when it evaluates the roots' group edges, before its first walk (the roots' selected extras and groups)."""
    selection = source.uv_selection
    everything = selection is None
    if selection is None:
        selection = UvSelection(all_extras=True)
    packages = source.uv_packages or []
    roots = source.roots if everything else uv_selected_roots(source)
    groups = _uv_groups(source, selection, everything, roots)
    by_name: dict[str, list[int]] = {}
    for i, package in enumerate(source.packages):
        by_name.setdefault(package.key, []).append(i)
    turned_on: set[str] = set()
    group_on: set[str] = set()  # extras the roots' group edges turned on so far, in uv's order
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

    def follow(
        dependencies: list[dict], parent: Parent, extras: frozenset[str], path: list[str], manifest: str = ""
    ) -> None:
        for dependency in dependencies:
            requested = frozenset(
                canonicalize_name(extra) for extra in dependency.get("extra", dependency.get("extras", []))
            )
            conflicts = world
            if world is not None:
                # uv counts an edge's own extras as on for its marker, and turns them on (except for the manifest's
                # requirements); it evaluates the manifest's edges with nothing on.
                own = frozenset(uv_conflict_name(dependency["name"], "extra", e) for e in requested) & world.known
                if manifest == "root-group":
                    # uv evaluates a root's group edges before its first walk, with only the roots' selected items
                    # and the extras earlier group edges turned on (to_resolution_from_packages; its TODO).
                    conflicts = ConflictWorld(early | group_on | own, world.known)
                else:
                    conflicts = ConflictWorld(frozenset() if manifest else world.active | own, world.known)
            if not marker_holds(dependency.get("marker"), target, extras=extras, groups=groups, conflicts=conflicts):
                continue
            if world is not None and manifest != "requirements":
                turned_on.update(own)
                if manifest == "root-group":
                    group_on.update(own)
            candidates = _narrow_candidates(
                source,
                by_name.get(canonicalize_name(dependency["name"]), []),
                dependency.get("version"),
                dependency.get("source"),
            )
            if manifest in ("requirements", "group"):
                candidates = _uv_manifest_fork(source, candidates, target)
            for i in candidates:
                visit(i, parent, requested, [*path, source.packages[i].name])

    def dependencies(i: int, extras: frozenset[str], path: list[str]) -> None:
        package = packages[i]
        follow(package.get("dependencies", []), i, extras, path)
        for name, optional in package.get("optional-dependencies", {}).items():
            if canonicalize_name(name) in extras:
                follow(optional, i, extras, path)

    def off(i: int, kind: str, name: str) -> bool:
        return bool(dropped) and uv_conflict_name(packages[i].get("name", ""), kind, name) in dropped

    if not selection.only_groups:
        for i in roots:
            optional = uv_extra_names(packages[i])
            extras = optional if selection.all_extras else selection.extras & optional
            visit(i, None, frozenset(e for e in extras if not off(i, "extra", e)), [source.packages[i].name])
        # Runtime paths take priority over equally short group paths in --explain.
        for _ in roots:
            dependencies(*queue.popleft())
        follow(source.uv_manifest.get("requirements", []), None, selection.extras, [], "requirements")
    for i in roots:
        for name, group in packages[i].get("dev-dependencies", {}).items():
            if canonicalize_name(name) in groups and not off(i, "group", name):
                follow(
                    group, canonicalize_name(name), frozenset(), [f"({canonicalize_name(name)} group)"], "root-group"
                )
    for name, group in source.uv_manifest.get("dependency-groups", {}).items():
        if canonicalize_name(name) in groups:
            follow(group, canonicalize_name(name), frozenset(), [f"({canonicalize_name(name)} group)"], "group")
    while queue:
        dependencies(*queue.popleft())
    return parents, frozenset(turned_on)


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
    if pkg.marker_group is not None:
        return group_answer(pkg.marker_group, target) is pkg
    return pkg.markers is None or any(marker_holds(m, target) for m in pkg.markers)


def group_answer(group: MarkerGroup, target: Target) -> Package | None:
    """The package of a name required under different markers that `target` installs, as pip combines the lines whose
    markers hold there: the member for exactly that set of lines, or a line's own package when its release meets every
    line that holds (one less lookup to show). Where a marker can't be decided (platform_machine on linux-armv7l, a
    patch release of Python), the answer stands only if it can't change: the release checked for the lines that hold
    for sure meets every line that may hold too. Otherwise the group's `unknown` member; None if no line holds."""
    values = [True if m is None else _any_marker(m, target) for m in group.markers]
    held = frozenset(i for i, value in enumerate(values) if value is True)
    maybe = frozenset(i for i, value in enumerate(values) if value is None)
    if not held and not maybe:
        return None
    fallback = next(m for m in group.members if frozenset() in m.marker_sets)
    python = f"{target.major}.{target.minor}"

    def meets(pkg: Package, lines: frozenset[int]) -> bool:
        """Whether pip would take `pkg`'s checked release for these lines too: each allows it and lists its hashes."""
        resolved = pkg.resolutions.get(python, pkg)
        version = resolved.version
        if pkg.skip or resolved.error or version is None:
            return False
        try:
            own = SpecifierSet(pkg.spec or "")
            together = own
            for i in lines:
                line = group.lines[i]
                if line.skip or line.error or (line.hashes, line.other_hashes) != (pkg.hashes, pkg.other_hashes):
                    return False
                specifier = _pin(line.version) if line.version else line.spec
                if specifier:
                    if not SpecifierSet(specifier).contains(version, prereleases=True):
                        return False
                    together &= SpecifierSet(specifier)
        except (InvalidSpecifier, InvalidVersion):
            return False
        # A release the index picked for `pkg`'s own range: if a line names a pre-release (tensorflow>=2.16.0rc0),
        # pip takes pre-releases for the lines together, so a newer one may win (pip 26.3 installs
        # tensorflow 2.22.0rc1 for that with tensorflow>=2.15). Then only the merged lines' own lookup can say.
        picked = resolved.version_from_index or pkg.version_from_index
        return not (picked and pkg.prereleases is None and together.prereleases and not own.prereleases)

    # A line that may hold but asks exactly what one that holds asks (`py-cpuinfo; platform_machine == "aarch64"`
    # beside `py-cpuinfo`, on linux-armv7l) changes nothing either way.
    maybe = frozenset(i for i in maybe if not any(_same_ask(group.lines[i], group.lines[j]) for j in held))
    singles = {i: m for m in group.members for s in m.marker_sets if len(s) == 1 for i in s}
    if held:
        answer = next((m for m in group.members if held in m.marker_sets), None)
        if answer is None:
            return fallback
        # A line's own package when its release meets every line that holds: `scipy>=1.13` alone and with
        # `scipy>=1.15; python_version >= "3.13"` check the same newest release.
        answer = next((singles[i] for i in sorted(held) if i in singles and meets(singles[i], held)), answer)
        return answer if not maybe or meets(answer, maybe) else fallback
    # Only lines that may hold: whichever do, pip takes the same release if each line's own is one release they all
    # allow.
    own = [singles.get(i) for i in sorted(maybe)]
    first = own[0]
    if first is None or any(m is None or not meets(m, maybe) for m in own):
        return fallback
    versions = {m.resolutions.get(python, m).version for m in own if m is not None}
    return first if len(versions) == 1 else fallback


def _same_ask(a: Package, b: Package) -> bool:
    fields = ("version", "spec", "hashes", "other_hashes", "skip", "error")
    return all(getattr(a, f) == getattr(b, f) for f in fields)


def _any_marker(markers: list[str], target: Target) -> bool | None:
    values = [marker_value(m, target) for m in markers]
    return True if True in values else None if None in values else False


def _pin(version: str) -> str:
    try:
        Version(version)
    except InvalidVersion:
        return f"==={version}"
    return f"=={version}"


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

    # A lock with conflicts: an edge counts if it holds in one of the installs (the hint follows only packages
    # some install reaches anyway).
    worlds = [world for world, *_ in _conflict_worlds(source, target) or []]

    def follow(items: list[dict], owner: tuple[str, str] | None, extras: frozenset[str]) -> None:
        for d in items:
            if worlds:
                if not any(
                    marker_holds(d.get("marker"), target, extras=extras, groups=groups, conflicts=world)
                    for world in worlds
                ):
                    continue
            elif not marker_holds(d.get("marker"), target, extras=extras, groups=groups):
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
        # Poetry 1.x without its project's dependencies (#815): the names each target installs, for left-out packages.
        installed_keys: dict[Target, set[str]] = {}
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
                status = (
                    status_for(
                        files,
                        t,
                        parsed[id(resolved)],
                        resolved.yanked,
                        no_binary=resolved.no_binary,
                        only_binary=resolved.only_binary,
                    )
                    if needed or source.poetry_roots_unknown
                    else Status.NOT_NEEDED
                )
                if needed and status is Status.MISSING and resolved.other_hashes:
                    # No file with a listed sha256 fits here, so pip doesn't filter by them (filter_unallowed_hashes)
                    # and checks its pick against the line's sha384/sha512 hashes too, which wheelgap can't match.
                    status = Status.UNKNOWN
                if not needed and status not in (Status.WHEEL, Status.NOT_NEEDED) and installed is not None:
                    # No dependency installs it here, but Poetry installs a direct dependency everywhere, and only the
                    # pyproject.toml can say whether it is one. Another version of the name installed here settles it.
                    if t not in installed_keys:
                        installed_keys[t] = {source.packages[j].key for j in installed[t]}
                    if pkg.key not in installed_keys[t]:
                        row.results[t] = Status.UNKNOWN
                        row.unknown_reasons[t] = poetry_roots_reason(source)
                        continue
                row.results[t] = status if needed else Status.NOT_NEEDED
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
    return _drop_unused_group_rows(rows)


def _drop_unused_group_rows(rows: list[Row]) -> list[Row]:
    """Leave out the rows of a MarkerGroup's packages that no target installs: the lines merged for an environment
    that isn't checked, and the `unknown` one, whose reason would otherwise be printed. If none of the group's rows is
    needed, its lines' own rows stay, as for any package whose marker no target holds."""
    used = {
        id(group)
        for row in rows
        if row.needed and (group := (row.input_package or row.package).marker_group) is not None
    }
    return [
        row
        for row in rows
        if (group := (row.input_package or row.package).marker_group) is None
        or row.needed
        or (id(group) not in used and any(len(s) == 1 for s in (row.input_package or row.package).marker_sets))
    ]


def poetry_roots_reason(source: Source) -> str:
    """Why a package a Poetry 1.x lock's walk leaves out is UNKNOWN on a target (#815)."""
    return (
        "no dependency installs it here, but Poetry installs it on every target if the project requires it directly, "
        "and a Poetry 1.x lock doesn't record that: the pyproject.toml beside the lock is needed to tell "
        f"({source.poetry_roots_unknown})"
    )


def poetry_roots_note(sources: list[Source], rows: list[Row]) -> str | None:
    """The run's note for the packages poetry_roots_reason made UNKNOWN, naming at most five."""
    names = list(dict.fromkeys(r.package.name for r in rows if any(t not in r.ignored for t in r.unknown_reasons)))
    why = "; ".join(source.poetry_roots_unknown for source in sources if source.poetry_roots_unknown)
    if not names or not why:
        return None
    shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    return (
        f"a Poetry 1.x lock doesn't record the project's own dependencies, and {why}: {shown} "
        f"{'is' if len(names) == 1 else 'are'} `unknown` where no dependency installs "
        f"{'it' if len(names) == 1 else 'them'} and there's no wheel, since Poetry installs a direct dependency "
        "there too; check the lock beside its pyproject.toml"
    )
