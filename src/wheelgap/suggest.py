"""--explain --suggest: the nearest newer release with a wheel for a package's gap targets.

nearest_with_wheel and suggest only read a `releases` mapping (the project JSON API's, version -> file list);
look_up_all fetches those, through pypi.fetch_releases. A suggestion never changes a status or the exit code.
"""

from __future__ import annotations

import threading
import urllib.error
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from packaging.tags import Tag
from packaging.version import InvalidVersion, Version

from . import pypi
from .check import Status, status_for, wheel_tags
from .pypi import _files, _supports_python
from .targets import Target

TIMEOUT = 10.0  # seconds per request, tried once: a suggestion isn't worth a retry
DEADLINE = 30.0  # seconds for all the lookups of a run; past it, the rest are dropped
WORKERS = 4  # each holds one parsed project page at most (see pypi.RELEASES_MAX_BYTES)
DROPPED = "not asked: the index couldn't be reached, or was too slow"
FAILED = "no usable answer"
LOOKBACK = 50  # older releases --suggest reads for a target no newer release has a wheel for

# (release text, "X.Y") -> the release's live files for that Python and their wheel tags, or None for a malformed
# file list: worked out once per release and Python, however many targets ask.
_Seen = dict[tuple[str, str], tuple[list[str], list[frozenset[Tag]]] | None]


def _candidates(releases: dict, pinned: str) -> list[str]:
    """The releases newer than `pinned`, lowest first. Pre-releases only when `pinned` is one; invalid versions are
    skipped, and an invalid pin raises InvalidVersion."""
    current = Version(pinned)
    found = []
    for text in releases:
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        if version > current and (current.is_prerelease or not version.is_prerelease):
            found.append((version, text))
    return [text for _, text in sorted(found)]


def _older(releases: dict, pinned: str) -> list[str]:
    """The releases older than `pinned`, newest first and at most LOOKBACK of them. Pre-releases only when `pinned` is
    one; invalid versions are skipped, and an invalid pin raises InvalidVersion."""
    current = Version(pinned)
    found = []
    for text in releases:
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        if version < current and (current.is_prerelease or not version.is_prerelease):
            found.append((version, text))
    return [text for _, text in sorted(found, reverse=True)[:LOOKBACK]]


def _has_wheel(releases: dict, text: str, target: Target, seen: _Seen) -> bool:
    python = f"{target.major}.{target.minor}"
    if (text, python) not in seen:
        try:
            entries = _files(releases[text])
        except ValueError:
            seen[text, python] = None  # skipped, as range lookups skip it
        else:
            files = [e["filename"] for e in entries if not e.get("yanked") and _supports_python(e, python)]
            seen[text, python] = files, wheel_tags(files)
    live = seen[text, python]
    return live is not None and status_for(live[0], target, live[1]) is Status.WHEEL


def _admits(releases: dict, text: str, target: Target) -> bool:
    """Whether some live file of the release admits the target's Python by its Requires-Python (any file kind)."""
    try:
        entries = _files(releases[text])
    except ValueError:
        return True  # unreadable: don't claim it leaves the Python out
    python = f"{target.major}.{target.minor}"
    return any(not e.get("yanked") and _supports_python(e, python) for e in entries)


def nearest_with_wheel(releases: dict, pinned: str, target: Target) -> str | None:
    """Return the lowest newer release with a non-yanked, compatible wheel.

    `releases` is the release-to-file-list mapping from the PyPI project JSON API.
    Pre-releases are considered only when `pinned` is itself a pre-release. Invalid
    release versions are skipped; an invalid pin raises `InvalidVersion`.
    File Requires-Python metadata follows the same rules as range lookups, and a release
    whose file list is malformed is skipped, as range lookups skip it.
    """
    seen: _Seen = {}
    return next((text for text in _candidates(releases, pinned) if _has_wheel(releases, text, target, seen)), None)


@dataclass(frozen=True)
class Suggestion:
    found: dict[Target, str]  # gap target -> the release named for it
    loses: tuple[Target, ...] = ()  # gap targets where every newer release with a wheel drops one the row has
    # gap targets no newer release has a wheel for -> the newest older release that has one (of the LOOKBACK newest)
    last: dict[Target, str] = field(default_factory=dict)
    # targets of `last` whose release has no wheel for one the row has a wheel for (--write-constraints leaves it out)
    last_loses: tuple[Target, ...] = ()
    # targets of `loses` -> the nearest newer release with a wheel for it, and the targets of `keep` it has none for
    drops: dict[Target, tuple[str, tuple[Target, ...]]] = field(default_factory=dict)
    # targets of `drops` whose release leaves out each dropped target's Python, an older one, by Requires-Python: no
    # file of it admits that Python (so a lock that forks per Python, as uv's may, could keep the old release there)
    python_drops: tuple[Target, ...] = ()


def suggest(releases: dict, pinned: str, targets: Iterable[Target], keep: Iterable[Target] = ()) -> Suggestion:
    """For a release `pinned` with gaps on `targets` and a wheel on `keep`: only releases that keep a wheel on every
    target of `keep` count. A gap target that no such release has a wheel for is left out (in `loses` when some other
    newer release has one), and the others all get the lowest such release with a wheel for every one of them; when
    there is none, each gets its own nearest. A gap target no newer release has a wheel for gets the newest older
    release with one in `last`, if one of the LOOKBACK newest has it. Keys keep the order of `targets`."""
    targets, keep = list(targets), list(keep)
    candidates = _candidates(releases, pinned)
    seen: _Seen = {}
    kept: dict[str, bool] = {}

    def keeps(v: str) -> bool:
        if v not in kept:
            kept[v] = all(_has_wheel(releases, v, k, seen) for k in keep)
        return kept[v]

    nearest: dict[Target, int] = {}
    loses = []
    drops: dict[Target, tuple[str, tuple[Target, ...]]] = {}
    python_drops: list[Target] = []
    last: dict[Target, str] = {}
    older: list[str] | None = None
    for t in targets:
        with_wheel = [i for i, v in enumerate(candidates) if _has_wheel(releases, v, t, seen)]
        if (i := next((i for i in with_wheel if keeps(candidates[i])), None)) is not None:
            nearest[t] = i
        elif with_wheel:
            loses.append(t)
            nearest_drop = candidates[with_wheel[0]]
            dropped = tuple(k for k in keep if not _has_wheel(releases, nearest_drop, k, seen))
            drops[t] = nearest_drop, dropped
            if dropped and all((k.major, k.minor) < (t.major, t.minor) for k in dropped):
                if not any(_admits(releases, nearest_drop, k) for k in dropped):
                    python_drops.append(t)
        else:
            older = _older(releases, pinned) if older is None else older
            if (v := next((v for v in older if _has_wheel(releases, v, t, seen)), None)) is not None:
                last[t] = v
    last_loses = tuple(t for t, v in last.items() if not keeps(v))
    if nearest:
        # A release with a wheel for every target that has one at all is no lower than the highest of the nearest.
        for v in candidates[max(nearest.values()) :]:
            if keeps(v) and all(_has_wheel(releases, v, t, seen) for t in nearest):
                return Suggestion(dict.fromkeys(nearest, v), tuple(loses), last, last_loses, drops, tuple(python_drops))
    found = {t: candidates[i] for t, i in nearest.items()}
    return Suggestion(found, tuple(loses), last, last_loses, drops, tuple(python_drops))


Wanted = dict[str, Sequence[tuple[str, Sequence[Target], Sequence[Target]]]]
"""By canonical name: the (pinned version, gap targets, targets with a wheel) of each row of that package."""


def look_up_all(wanted: Wanted, deadline: float = DEADLINE) -> tuple[dict[str, list[Suggestion]], dict[str, str]]:
    """Fetch each package's releases once, at most WORKERS at a time and `deadline` seconds in all, and suggest for each
    of its rows (in the order given). Returns the suggestions by name, and why the others got none, by name. The first
    failure to reach the index drops the lookups still to come, and so does the deadline. Each project page is
    dropped once its rows are answered, so at most WORKERS are held at a time."""
    end = pypi._clock() + deadline
    stop = threading.Event()

    def one(key: str) -> list[Suggestion] | str:
        left = end - pypi._clock()
        if stop.is_set() or left <= 0:
            return DROPPED
        try:
            releases = pypi.fetch_releases(key, timeout=min(TIMEOUT, left), deadline=end)
        except urllib.error.HTTPError as e:  # the server answered: the others may still work
            return f"HTTP {e.code}"
        except pypi.ResponseTooLarge:
            return f"answer over {pypi.RELEASES_MAX_BYTES // (1024 * 1024)} MB"
        except (pypi.DeadlineExceeded, urllib.error.URLError):  # out of time, or no connection at all: stop there
            stop.set()
            return DROPPED
        except Exception:  # a slow or cut-off answer (TimeoutError, a reset), a damaged body, bad JSON, no releases
            return FAILED
        try:
            return [suggest(releases, pinned, gaps, keep) for pinned, gaps, keep in wanted[key]]
        except InvalidVersion:
            return "a checked version isn't a valid version"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        answers = dict(zip(wanted, pool.map(one, wanted), strict=True))
    found = {key: a for key, a in answers.items() if not isinstance(a, str)}
    failed = {key: a for key, a in answers.items() if isinstance(a, str)}
    return found, failed


def suggest_lines(name: str, pinned: str, gaps: Sequence[Target], suggestion: Suggestion, multi_py: bool) -> list[str]:
    """`foo 1.2.0 -> 1.4.0 has wheels for musl-aarch64, win-amd64`, one line per suggested version (lowest first),
    then `foo 1.2.0: no newer release has a wheel for linux-riscv64` for the gap targets with none (ending in
    `; the last one that did is 1.0.3` when an older release has one, a line per such release, newest first), and
    `... for win-arm64 without losing one elsewhere` for those whose every newer release with a wheel drops another."""
    found, loses = suggestion.found, set(suggestion.loses)
    by_version: dict[str, list[str]] = {}
    for t in gaps:
        if t in found:
            by_version.setdefault(found[t], []).append(t.label(multi_py))
    out = [
        f"{name} {pinned} -> {v} has wheels for {', '.join(labels)}"
        for v, labels in sorted(by_version.items(), key=lambda item: Version(item[0]))
    ]
    none = [t for t in gaps if t not in found and t not in loses]
    if never := [t.label(multi_py) for t in none if t not in suggestion.last]:
        out.append(f"{name} {pinned}: no newer release has a wheel for {', '.join(never)}")
    by_last: dict[str, list[str]] = {}
    for t in none:
        if t in suggestion.last:
            by_last.setdefault(suggestion.last[t], []).append(t.label(multi_py))
    out += [
        f"{name} {pinned}: no newer release has a wheel for {', '.join(labels)}; the last one that did is {v}"
        for v, labels in sorted(by_last.items(), key=lambda item: Version(item[0]), reverse=True)
    ]
    if lost := [t.label(multi_py) for t in gaps if t in loses]:
        out.append(f"{name} {pinned}: no newer release has a wheel for {', '.join(lost)} without losing one elsewhere")
    return out
