"""--from-log: the packages a failed pip or uv install log says didn't build, with the version the log gives each.

The log is untrusted text. Only a few exact message shapes count, and a failure that can't be read is an error, never
a guess.
"""

from __future__ import annotations

import codecs
import re
import sys
from pathlib import Path
from typing import BinaryIO

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from .sources import _NAME_RE, Source, SourceError, requirement_to_package

# pip and uv end with the summary, so a longer log keeps its last 8 MiB.
MAX_LOG_BYTES = 8 * 1024 * 1024
# More failures than this is a broken build environment, not a few packages without wheels.
MAX_FAILURES = 50

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")  # GitHub Actions' prefix on each log line
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
# pip's own lines, not a build's output (which pip indents further): `Collecting cryptography==43.0.0 (from ...)`,
# `Collecting cryptography>=42`, `Collecting x @ https://...`; `  Downloading cryptography-43.0.0.tar.gz (686 kB)`,
# `  Using cached https://.../cryptography-43.0.0.tar.gz`. `Collecting` only at column 0: under `pip install -v` the pip
# that installs a build's dependencies logs its own, indented, `  Collecting numpy>=2`, which isn't this run's numpy.
# Its indented `    Using cached numpy-2.1.0-...` still counts, so this run's numpy then has more than one version.
_COLLECT = re.compile(rf"Collecting ({_NAME})(?:\[[^\]]*\])?(.*)$")
_PIN = re.compile(r"\s*===?\s*([^\s,;*]+)(?:\s|$)")
_FILE = re.compile(r" {0,4}(?:Downloading|Using cached) (\S+)")
# pip's resolver, when it tries more than one release of a package (backtracking).
_BACKTRACK = re.compile(rf"INFO: pip is (?:still )?looking at multiple versions of ({_NAME})(?:\[[^\]]*\])? ")
# pip's `Looking in indexes: https://pypi.org/simple, https://private.example/simple`, when it's given an index, and
# `Looking in links: /wheels, https://example.com/wheels` for --find-links (with or without --no-index).
_INDEXES = re.compile(r"\s*Looking in indexes: (.+)$")
_LINKS = re.compile(r"\s*Looking in links: (.+)$")
# The end of a pip run that installed what it was asked to; the next run in the same log starts afresh.
_INSTALLED = re.compile(r"Successfully installed ")
_PYPI_SIMPLE = "https://pypi.org/simple"
_USERINFO = re.compile(r"//[^/@]*@")  # pip hides the password, but not always the user name
# pip, for each package it builds: `Building wheel for X (pyproject.toml) ... error`, then
# `ERROR: Failed building wheel for X` if it fails.
_BUILDING = re.compile(rf"(?:Building wheel for|Failed building wheel for) ({_NAME})(?:\s|$)")
# pip's summary, at column 0: `Failed to build X Y`; pip 24.1 to 25.1's `ERROR: Failed to build installable wheels for
# some pyproject.toml based projects (X, Y)` (`ERROR: ERROR: ...` in pip 24.1 to 24.3); and pip 25.2 and later's
# `× Failed to build installable wheels for some pyproject.toml based projects` with the names on the next line,
# `╰─> X, Y` (without the `×` and `╰─>` when the console can't print them).
_PIP_INSTALLABLE = re.compile(
    r"(?:ERROR: ){0,2}Failed to build installable wheels for some pyproject\.toml based projects \(([^()]*)\)\s*$"
)
_PIP_DIAGNOSTIC = re.compile(r"(×\s+)?Failed to build installable wheels for some pyproject\.toml based projects\s*$")
_PIP_DIAGNOSTIC_NAMES = re.compile(r"╰─>\s*(.*)$")
_PIP_SUMMARY = re.compile(r"(?:ERROR: )?Failed to build ([^`'\"]+)$")
# uv: `× Failed to build `x==1.0``, `× Failed to download and build `x==1.0``, older uv's `error: Failed to download
# and build: `x==1.0`` and `Caused by: Failed to build: `x==1.0``; only after the prefixes uv starts these lines with,
# never in a build's output.
_UV = re.compile(r"\s*(?:×|├─▶|╰─▶|error:|Caused by:)\s*Failed to (?:download and )?build:? `([^`]*)`")
_UV_NAME_VERSION = re.compile(rf"({_NAME})(?:==(\S+))?")

_UNCLEAR = "unclear"  # the log collects the package again without a version after naming one
_DIRECT = "direct"  # the log installs the package from a URL or a path, not from an index
_EARLIER = "earlier"  # the package's last `Collecting` line is from an earlier pip run in the log


def read_log(path: str) -> str:
    """The text of the log at `path` (- for stdin): its last MAX_LOG_BYTES, decoded as UTF-16 after a UTF-16 BOM
    (PowerShell's `>` writes one), else as UTF-8, with undecodable bytes replaced."""
    if path == "-":
        return _decode(sys.stdin.buffer)
    try:
        with Path(path).open("rb") as f:
            return _decode(f)
    except OSError as e:
        raise SourceError(f"--from-log: can't read {path}: {e.strerror or type(e).__name__}") from e


def _decode(f: BinaryIO) -> str:
    head = b""
    buf = bytearray()
    total = 0
    while chunk := f.read(1024 * 1024):
        if not total:
            head = chunk[:2]
        total += len(chunk)
        buf += chunk
        if len(buf) > 2 * MAX_LOG_BYTES:
            del buf[: len(buf) - MAX_LOG_BYTES]
    if len(buf) > MAX_LOG_BYTES:
        del buf[: len(buf) - MAX_LOG_BYTES]
    skipped = total - len(buf)
    encoding = {codecs.BOM_UTF16_LE: "utf-16-le", codecs.BOM_UTF16_BE: "utf-16-be"}.get(head, "utf-8")
    if encoding != "utf-8":
        if not skipped:
            del buf[:2]  # the BOM
        elif skipped % 2:
            del buf[:1]  # back on a code unit boundary
    return bytes(buf).decode(encoding, "replace").removeprefix("\ufeff")


def failed_builds(text: str, other_index: bool = False) -> list[tuple[str, str]]:
    """The (name, version) of each package the log says failed to build, in the order the log names them. The version
    is the one uv names, else the one the `Collecting`, `Downloading` and `Using cached` lines since the package's last
    `Collecting` line name, in the same pip run (a run ends at `Successfully installed`). A failure whose version the
    log doesn't make clear is an error, and so is a pip log that used another index than PyPI or --find-links, unless
    `other_index` (wheelgap itself checks another index, as with --index-url)."""
    # canonical name -> the versions named since its last `Collecting` line (Version -> as written), or _UNCLEAR,
    # _DIRECT or _EARLIER
    versions: dict[str, dict[Version, str] | str] = {}
    # The first package pip's resolver tried more than one release of. Backtracking on one package changes the
    # versions its dependencies are collected at, and pip doesn't log a candidate again when it goes back to it.
    backtracked: str | None = None
    built: set[str] = set()  # names pip printed `Building wheel for` or `Failed building wheel for` for
    failures: dict[str, tuple[str, str]] = {}

    def failed(name: str, version: str | None) -> None:
        key = canonicalize_name(name)
        if version is None:
            version = pip_version(name, key)
        if key in failures and failures[key][1] != version:
            raise SourceError(
                f"--from-log: the log says {name} failed to build at more than one version; refusing to guess"
            )
        failures.setdefault(key, (name, version))
        if len(failures) > MAX_FAILURES:
            raise SourceError(
                f"--from-log: the log names more than {MAX_FAILURES} packages that failed to build, which looks "
                "like a broken build environment rather than missing wheels"
            )

    def pip_version(name: str, key: str) -> str:
        # pip doesn't say which version failed, so it must be the one version the log names for it.
        seen = versions.get(key)
        if seen == _UNCLEAR:
            raise SourceError(
                f"--from-log: the log collects {name} again without a version after naming one, so which "
                f"version failed isn't clear; refusing to guess (check {name}==VERSION directly)"
            )
        if seen == _EARLIER:
            raise SourceError(
                f"--from-log: the log's last `Collecting` line for {name} is from an earlier pip run (before its "
                "`Successfully installed`), so which version failed isn't clear; refusing to guess (check "
                f"{name}==VERSION directly, or give wheelgap the log of one pip run)"
            )
        if seen == _DIRECT:
            raise SourceError(
                f"--from-log: the log installs {name} from a URL or a path, not from an index, so it can't be checked"
            )
        if not isinstance(seen, dict) or not seen:
            # A local project (`pip install .` logs `Processing`, never `Collecting`), `pip install -q`, or a log
            # whose start was cut off. PyPI's newest release of that name may be another project altogether.
            raise SourceError(
                f"--from-log: the log doesn't say which version of {name} failed to build (pip logs `Processing` "
                "for a local project, and `pip install -q` hides `Collecting`); refusing to guess "
                f"(check {name}==VERSION directly)"
            )
        if len(seen) > 1:
            raise SourceError(
                f"--from-log: pip tried more than one version of {name}, so which version failed isn't clear; "
                f"refusing to guess (check {name}==VERSION directly)"
            )
        if backtracked is not None:
            raise SourceError(
                f"--from-log: pip backtracked (it looked at multiple versions of {backtracked}), so which version of "
                f"{name} failed isn't clear; refusing to guess (check {name}==VERSION directly)"
            )
        return next(iter(seen.values()))

    def named(key: str, version: str) -> None:
        seen = versions.setdefault(key, {})
        if seen in (_UNCLEAR, _EARLIER):
            versions[key] = seen = {}
        if isinstance(seen, dict):  # a URL or a path stays one, whatever file it downloads
            seen.setdefault(Version(version), version)

    def pip_summary(names: list[str]) -> None:
        # Only names pip printed `Building wheel for` for: a build's own output at column 0 (`Failed to build the
        # extension`) isn't pip's summary. A line that names any of them must name only them.
        if not any(_NAME_RE.fullmatch(n) and canonicalize_name(n) in built for n in names):
            return
        if not all(_NAME_RE.fullmatch(n) and canonicalize_name(n) in built for n in names):
            raise SourceError("--from-log: can't read pip's list of packages that failed to build; refusing to guess")
        for n in names:
            failed(n, None)

    # After pip 25.2's `Failed to build installable wheels ...`: whether it had the `×`, so the names follow `╰─>`.
    diagnostic: bool | None = None
    for line in _ANSI.sub("", text).splitlines():
        if m := _TIMESTAMP.match(line):
            line = line[m.end() :]
        if diagnostic is not None:
            if not line.strip():
                continue
            if diagnostic:  # this line must be `╰─> NAMES`
                if not (m := _PIP_DIAGNOSTIC_NAMES.match(line)):
                    raise SourceError(
                        "--from-log: can't read pip's list of packages that failed to build; refusing to guess"
                    )
                line = m.group(1)
            pip_summary([n.strip() for n in line.split(",")])  # else it's skipped if it names no built package
            diagnostic = None
            continue
        if m := _INDEXES.match(line):
            others = [url for url in map(str.strip, m.group(1).split(",")) if url.rstrip("/") != _PYPI_SIMPLE]
            if others and not other_index:
                others = [_USERINFO.sub("//", url) for url in others]
                raise SourceError(
                    f"--from-log: the log used another index than PyPI ({', '.join(others)}), so its packages may "
                    "not be PyPI's; pass that index with --index-url to check it"
                )
        if (m := _LINKS.match(line)) and not other_index:
            links = ", ".join(_USERINFO.sub("//", url.strip()) for url in m.group(1).split(","))
            raise SourceError(
                f"--from-log: the log used --find-links ({links}), so its packages may not be PyPI's; pass the "
                "index they came from with --index-url to check it"
            )
        if _INSTALLED.match(line):
            # A later run's failure without its own `Collecting` line must not take this run's version.
            for key in versions:
                versions[key] = _EARLIER
        if m := _COLLECT.match(line):
            key, rest = canonicalize_name(m.group(1)), m.group(2)
            if (pin := _PIN.match(rest)) and _is_version(pin.group(1)):
                versions[key] = {Version(pin.group(1)): pin.group(1)}
            elif rest.lstrip().startswith("@"):
                versions[key] = _DIRECT
            elif key in versions:
                versions[key] = _UNCLEAR
            else:
                versions[key] = {}
        elif (m := _FILE.match(line)) and (found := _file_version(m.group(1))):
            named(canonicalize_name(found[0]), found[1])
        if backtracked is None and (m := _BACKTRACK.match(line)):
            backtracked = m.group(1)
        if m := _BUILDING.search(line):
            built.add(canonicalize_name(m.group(1)))
        if m := _UV.match(line):
            uv = _UV_NAME_VERSION.fullmatch(m.group(1))
            if uv is None or not _NAME_RE.fullmatch(uv.group(1)) or not _is_version(uv.group(2)):
                raise SourceError(
                    "--from-log: uv says a package failed to build, but not as NAME==VERSION (a URL or a path?), "
                    "so it can't be checked"
                )
            failed(uv.group(1), uv.group(2))
        elif m := _PIP_INSTALLABLE.match(line):
            pip_summary([n.strip() for n in m.group(1).split(",")])
        elif m := _PIP_DIAGNOSTIC.match(line):
            diagnostic = m.group(1) is not None
        elif m := _PIP_SUMMARY.match(line):
            pip_summary(m.group(1).split())
    if diagnostic:
        raise SourceError("--from-log: can't read pip's list of packages that failed to build; refusing to guess")

    if not failures:
        raise SourceError(
            "--from-log: found no package that failed to build (pip's `Failed to build NAME` or uv's "
            "``Failed to build `NAME==VERSION` ``)"
        )
    return list(failures.values())


def _file_version(location: str) -> tuple[str, str] | None:
    """The (name, version) of the file a `Downloading` or `Using cached` line names (or of the distribution whose
    `.metadata` file it is); None if it's not a distribution."""
    filename = location.split("#", 1)[0].split("?", 1)[0].rsplit("/", 1)[-1].removesuffix(".metadata")
    try:
        if filename.endswith(".whl"):
            name, version, _, _ = parse_wheel_filename(filename)
        elif filename.endswith((".tar.gz", ".zip")):
            name, version = parse_sdist_filename(filename)
        else:
            return None
    except (InvalidSdistFilename, InvalidWheelFilename, InvalidVersion):
        return None
    return name, str(version)


def _is_version(version: str | None) -> bool:
    if version is None:
        return True
    try:
        Version(version)
    except InvalidVersion:
        return False
    return True


def log_source(failures: list[tuple[str, str]]) -> Source:
    """The packages to check, as PyPI names: built from the log's names and versions, never read as paths."""
    source = Source("install log", pypi_only=True)
    for name, version in failures:
        source.packages.append(requirement_to_package(f"{name}=={version}", "install log"))
    return source
