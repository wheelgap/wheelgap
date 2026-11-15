"""Packages that Pyodide ships in its own distribution, read from a release's pyodide-lock.json."""

from __future__ import annotations

import http.client
import json
import re
import sys
import urllib.error
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .check import Row, Status
from .sources import Package
from .targets import PLATFORMS, Target

# Pyodide's ABI version for each Python (the `abi_version` in pyodide-lock.json).
PYODIDE_ABI = {(3, 12): "2024_0", (3, 13): "2025_0", (3, 14): "2026_0"}


class PyodideLockError(ValueError):
    pass


# A Pyodide release in a URL path (`.../pyodide/v0.29.5/full/...`) or a lock's file name.
_RELEASE = re.compile(r"v?(\d+\.\d+\.\d+)")
_RELEASE_FILENAMES = (
    re.compile(r"pyodide-lock[._-]v?(\d+\.\d+\.\d+)\.json"),
    re.compile(r"pyodide[._-]v?(\d+\.\d+\.\d+)[._-]lock\.json"),
    re.compile(r"v?(\d+\.\d+\.\d+)[._-]pyodide-lock\.json"),  # the tracker's 0.29.5-pyodide-lock.json
)


def _release_from_source(source: str) -> str | None:
    """The Pyodide release a lock's URL or file name names, when info.version is missing.

    A path only counts on Pyodide's own release URLs (`cdn.jsdelivr.net/pyodide/v0.29.5/...` and
    `github.com/pyodide/pyodide/releases/download/0.29.5/...`): elsewhere a number in the path may be another
    project's release or a mirror's own numbering. Otherwise only a lock file named `pyodide-lock-0.29.5.json`
    (and the like) counts. When the path and the file name disagree, neither is trusted.
    """
    from_path = None
    if source.lower().startswith(("http://", "https://")):
        url = urlsplit(source)
        host, parts = (url.hostname or "").lower(), url.path.split("/")
        name = parts[-1]
        release = None
        if host == "cdn.jsdelivr.net" and parts[1:2] == ["pyodide"] and len(parts) > 3:
            release = parts[2]
        elif host == "github.com" and parts[1:5] == ["pyodide", "pyodide", "releases", "download"] and len(parts) > 6:
            release = parts[5]
        if release is not None and (match := _RELEASE.fullmatch(release)):
            from_path = match.group(1)
    else:
        name = Path(source).name
    from_name = next((m.group(1) for pattern in _RELEASE_FILENAMES if (m := pattern.fullmatch(name))), None)
    if from_path and from_name and from_path != from_name:
        return None
    return from_path or from_name


@dataclass(frozen=True)
class PyodideLock:
    source: str
    pyodide_version: str
    python: tuple[int, int]
    abi: str | None
    packages: dict[str, str]  # canonical name -> bundled version

    def applies_to(self, target: Target) -> bool:
        return PLATFORMS[target.platform][0] == "pyodide" and (target.major, target.minor) == self.python


def parse_pyodide_lock(data: dict, source: str) -> PyodideLock:
    if not isinstance(data, dict):
        raise PyodideLockError(f"{source}: not a pyodide-lock.json (expected a JSON object)")
    info = data.get("info")
    packages = data.get("packages")
    if not isinstance(info, dict) or not isinstance(packages, dict):
        raise PyodideLockError(f"{source}: not a pyodide-lock.json (expected 'info' and 'packages')")
    try:
        major, minor = (int(p) for p in str(info["python"]).split(".")[:2])
    except (KeyError, ValueError):
        raise PyodideLockError(f"{source}: info.python is missing or not a version") from None
    abi = info.get("abi_version")
    expected = PYODIDE_ABI.get((major, minor))
    if abi and expected and abi != expected:
        raise PyodideLockError(f"{source}: abi_version {abi} doesn't match Python {major}.{minor} ({expected})")
    bundled = {
        str(canonicalize_name(p["name"])): str(p["version"])
        for p in packages.values()
        # Only real installable wheels. CPython stdlib modules and shared libraries
        # are filtered by package_type; test bundles (aiohttp-tests, ...) ship as
        # .tar/.zip and aren't installable distributions.
        if isinstance(p, dict)
        and p.get("package_type", "package") == "package"
        and ("file_name" not in p or str(p["file_name"]).lower().endswith(".whl"))
        and "name" in p
        and "version" in p
    }
    version = info.get("version") or _release_from_source(source) or "?"
    return PyodideLock(source, str(version), (major, minor), abi, bundled)


def load_pyodide_lock(spec: str) -> PyodideLock:
    """Read a pyodide-lock.json from a path, or fetch it when given an https:// URL."""
    scheme = spec.split("://", 1)[0].lower() if "://" in spec else ""
    if scheme == "http":
        raise PyodideLockError(f"{spec}: plain http:// URLs are not allowed; use https://")
    if scheme == "https":
        from .pypi import fetch_json

        try:
            data = fetch_json(spec)
        except urllib.error.HTTPError as e:
            raise PyodideLockError(f"couldn't fetch {spec}: HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise PyodideLockError(f"couldn't fetch {spec}: {e.reason}") from None
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise PyodideLockError(f"couldn't fetch {spec}: {e}") from None
    else:
        try:
            data = json.loads(Path(spec).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise PyodideLockError(f"{spec}: not valid JSON ({e})") from None
    return parse_pyodide_lock(data, spec)


def check_targets(lock: PyodideLock, targets: list[Target]) -> None:
    """Error if no pyodide target can use the lock; warn about pyodide targets it doesn't cover."""
    uncovered = sorted({t.python for t in targets if PLATFORMS[t.platform][0] == "pyodide" and not lock.applies_to(t)})
    if uncovered and any(lock.applies_to(t) for t in targets):
        py = f"{lock.python[0]}.{lock.python[1]}"
        print(
            f"wheelgap: note: {lock.source} is for Python {py}; pyodide results for Python {', '.join(uncovered)} "
            "don't include packages that Pyodide bundles",
            file=sys.stderr,
        )
    if not any(lock.applies_to(t) for t in targets):
        have = ", ".join(sorted({t.python for t in targets if PLATFORMS[t.platform][0] == "pyodide"})) or "none"
        py = f"{lock.python[0]}.{lock.python[1]}"
        raise PyodideLockError(
            f"{lock.source} is for Python {py} (Pyodide {lock.pyodide_version}), "
            f"but no pyodide target uses Python {py} (pyodide targets: {have}); try -p {py} -t pyodide"
        )


def check_locks(locks: Sequence[PyodideLock], targets: Sequence[Target]) -> None:
    """Error unless each lock is for a different Python and some pyodide target uses it.

    For the API and the scanner: a lock that applied to nothing would leave its packages as gaps without saying so,
    and two locks for one Python would count packages that either release bundles.
    """
    seen: dict[tuple[int, int], str] = {}
    for lock in locks:
        py = f"{lock.python[0]}.{lock.python[1]}"
        if lock.python in seen:
            raise PyodideLockError(f"{seen[lock.python]} and {lock.source} are both for Python {py}; pass one")
        seen[lock.python] = lock.source
        if not any(lock.applies_to(t) for t in targets):
            have = ", ".join(sorted({t.python for t in targets if PLATFORMS[t.platform][0] == "pyodide"})) or "none"
            raise PyodideLockError(
                f"{lock.source} is for Python {py} (Pyodide {lock.pyodide_version}), "
                f"but no pyodide target uses Python {py} (pyodide targets: {have})"
            )


def bundles(lock: PyodideLock, key: str, target: Target, status: Status) -> bool:
    """Whether a gap on this target counts as BUNDLED because Pyodide ships the package (canonical `key`) itself.

    Only SOURCE and MISSING become BUNDLED. UNKNOWN means the lookup failed (a typo'd pin, a network
    error), and that must stay a gap rather than pass because the name happens to be in the lock.
    """
    return key in lock.packages and lock.applies_to(target) and status in (Status.SOURCE, Status.MISSING)


def bundled_version_differs(pkg: Package, lock: PyodideLock) -> bool:
    """Pyodide ships a version the input doesn't allow: micropip.install of that requirement would fail.

    A pin is compared as a version (0.9 == 0.9.0), a range by whether it contains Pyodide's version, and a
    requirement with neither (the index picked the newest release) never differs.
    """
    have = lock.packages[pkg.key]
    if pkg.version_from_index:
        return pkg.spec is not None and not SpecifierSet(pkg.spec).contains(have, prereleases=True)
    if not pkg.version:
        return False
    try:
        return Version(pkg.version) != Version(have)
    except InvalidVersion:
        return pkg.version != have


def apply_bundled(rows: list[Row], targets: list[Target], lock: PyodideLock) -> None:
    """Mark gaps on matching pyodide targets as BUNDLED when Pyodide ships the package itself."""
    for r in rows:
        for t in targets:
            if bundles(lock, r.package.key, t, r.results[t]):
                r.results[t] = Status.BUNDLED
