"""Scan the most-downloaded PyPI packages across every target: the data behind the tracker.

    python -m wheelgap.scan --top 5000 --out coverage.json

Each package's latest release is checked against every native platform, plus Pyodide where
there's a Pyodide build of that Python. With --pyodide-lock (one per Pyodide release), packages
that Pyodide ships itself count as `bundled` on that release's Python. The output is one JSON
document (schema below) with per-target totals, so a website or a later scan can diff it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from . import __version__
from .api import Result, check_packages
from .check import Status
from .pyodide import PyodideLock, check_locks, load_pyodide_lock
from .pypi import fetch_json
from .targets import NATIVE_PLATFORMS, PLATFORMS, PYODIDE_PLATFORMS, Target, TargetError, build_targets, parse_python

TOP_PACKAGES_URL = "https://hugovk.dev/top-pypi-packages/top-pypi-packages.min.json"
DEFAULT_PYTHONS = "3.13,3.14,3.14t,3.15,3.15t"
SCHEMA_VERSION = 1


def scan_targets(pythons: str) -> list[Target]:
    """Every native platform for each Python, plus Pyodide where that Python has a Pyodide build."""
    targets: list[Target] = []
    for spec in (p for p in pythons.split(",") if p.strip()):
        major, minor, ft = parse_python(spec)
        platforms = list(NATIVE_PLATFORMS)
        if not ft and (major, minor) in PYODIDE_PLATFORMS:
            platforms.append("pyodide")
        targets += build_targets([spec], platforms)
    return list(dict.fromkeys(targets))


def load_top(source: str, limit: int) -> tuple[list[dict], str | None]:
    """The top `limit` rows ({project, download_count}) from hugovk's top-pypi-packages data."""
    if source.startswith(("http://", "https://")):
        data = fetch_json(source)
    else:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    return data["rows"][:limit], data.get("last_update")


def summarise(results: list[Result], targets: list[Target]) -> dict[str, dict[str, int]]:
    summary = {}
    for t in targets:
        counts = Counter(r.statuses[t].value for r in results)
        summary[t.label(True)] = {
            s.value: counts.get(s.value, 0)
            for s in (Status.WHEEL, Status.SOURCE, Status.MISSING, Status.UNKNOWN, Status.BUNDLED)
        }
    return summary


def scan(
    rows: list[dict], targets: list[Target], workers: int = 16, pyodide_locks: Sequence[PyodideLock] = ()
) -> list[Result]:
    return check_packages([r["project"] for r in rows], targets, workers=workers, pyodide_locks=pyodide_locks)


def _bundled_versions(result: Result, locks: Sequence[PyodideLock]) -> dict[str, str]:
    """The version each lock that made one of the result's cells `bundled` ships, by the lock's Python."""
    key = canonicalize_name(result.name)
    return {
        f"{lock.python[0]}.{lock.python[1]}": lock.packages[key]
        for lock in locks
        if key in lock.packages and any(s is Status.BUNDLED and lock.applies_to(t) for t, s in result.statuses.items())
    }


def _differs(bundled: str, version: str | None) -> bool:
    """Whether Pyodide's version differs from the release the scan checked (by PEP 440, so 0.9 == 0.9.0)."""
    if version is None:
        return False
    try:
        return Version(bundled) != Version(version)
    except InvalidVersion:
        return bundled != version


def _package_entry(
    rank: int, row: dict, result: Result, targets: list[Target], locks: Sequence[PyodideLock] = ()
) -> dict:
    entry = {
        "rank": rank,
        "name": result.name,
        "version": result.version,
        "downloads": row.get("download_count"),
        "pure": result.pure,
        "error": result.error,
        "results": [result.statuses[t].value for t in targets],
    }
    if bundled := _bundled_versions(result, locks):
        # Locks for different Pythons often ship different versions (aiohttp 3.11.13 in one, 3.13.5 in the next), so
        # bundled_versions gives each one; bundled_version is only set when they all agree, never one picked by order.
        versions = set(bundled.values())
        entry["bundled_version"] = versions.pop() if len(versions) == 1 else None
        entry["bundled_versions"] = bundled
        entry["bundled_version_differs"] = any(_differs(v, result.version) for v in bundled.values())
    return entry


def report(
    rows: list[dict],
    results: list[Result],
    targets: list[Target],
    source: str,
    last_update: str | None,
    pyodide_locks: Sequence[PyodideLock] = (),
) -> dict:
    """The tracker's data. `results` in each package lines up with the top-level `targets` list."""
    return {
        "schema": SCHEMA_VERSION,
        "generated": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "wheelgap": __version__,
        "top_packages": {"source": source, "last_update": last_update},
        # The Pyodide releases whose own builds count as `bundled`; a pyodide target for another Python used none.
        "pyodide_locks": [
            {
                "python": f"{lock.python[0]}.{lock.python[1]}",
                "pyodide_version": lock.pyodide_version,
                "source": lock.source,
            }
            for lock in pyodide_locks
        ],
        "targets": [
            {
                "label": t.label(True),
                "python": t.python,
                "platform": t.platform,
                # The limits a wheel had to meet, so the numbers keep their meaning if defaults change.
                "glibc": ".".join(map(str, t.glibc)),
                "musl": ".".join(map(str, t.musl)),
                "macos": ".".join(map(str, t.macos)),
            }
            for t in targets
        ],
        "lookup_errors": sum(1 for r in results if r.error),
        "summary": summarise(results, targets),
        "packages": [
            _package_entry(rank, row, r, targets, pyodide_locks)
            for rank, (row, r) in enumerate(zip(rows, results, strict=True), start=1)
        ],
    }


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def fraction(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number from 0 to 1") from None
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("must be a number from 0 to 1")
    return number


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m wheelgap.scan", description=__doc__.splitlines()[0])
    p.add_argument(
        "--top", type=positive_int, default=5000, metavar="N", help="how many packages to scan (default: 5000)"
    )
    p.add_argument("--python", default=DEFAULT_PYTHONS, metavar="VERS", help=f"default: {DEFAULT_PYTHONS}")
    p.add_argument("--top-packages", default=TOP_PACKAGES_URL, metavar="URL_OR_PATH", help="top-pypi-packages JSON")
    p.add_argument("--workers", type=positive_int, default=16, help="parallel index lookups (default: 16)")
    p.add_argument("--out", metavar="PATH", help="write here instead of stdout")
    p.add_argument(
        "--pyodide-lock",
        action="append",
        default=[],
        metavar="PATH_OR_URL",
        help="a Pyodide release's pyodide-lock.json: packages it ships count as bundled (repeat, one per Python)",
    )
    p.add_argument(
        "--max-error-rate",
        type=fraction,
        default=0.05,
        metavar="FRACTION",
        help="exit 1 without writing if more than this share of lookups fail (default: 0.05)",
    )
    args = p.parse_args(argv)

    started = time.monotonic()
    try:
        targets = scan_targets(args.python)
    except TargetError as error:
        p.error(f"argument --python: {error}")
    if not targets:
        p.error("argument --python: provide at least one Python version")
    try:
        pyodide_locks = [load_pyodide_lock(spec) for spec in args.pyodide_lock]
        check_locks(pyodide_locks, targets)
    except (OSError, ValueError) as error:  # PyodideLockError and UnicodeDecodeError are ValueErrors
        p.error(f"argument --pyodide-lock: {error}")
    uncovered = [
        t.python
        for t in targets
        if PLATFORMS[t.platform][0] == "pyodide" and not any(lock.applies_to(t) for lock in pyodide_locks)
    ]
    if pyodide_locks and uncovered:
        print(
            f"note: no --pyodide-lock for Python {', '.join(uncovered)}; "
            "pyodide results there don't include packages that Pyodide bundles",
            file=sys.stderr,
        )
    rows, last_update = load_top(args.top_packages, args.top)
    results = scan(rows, targets, args.workers, pyodide_locks)
    data = report(rows, results, targets, args.top_packages, last_update, pyodide_locks)

    errors = data["lookup_errors"]
    took = time.monotonic() - started
    print(
        f"scanned {len(results)} packages x {len(targets)} targets in {took:.0f}s ({errors} lookup errors)",
        file=sys.stderr,
    )
    if results and errors > args.max_error_rate * len(results):
        # A PyPI outage would otherwise publish a dataset full of "unknown"; keep the last good one instead.
        print(f"too many lookup errors ({errors} of {len(results)}); not writing the results", file=sys.stderr)
        return 1
    text = json.dumps(data, separators=(",", ":"))
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
