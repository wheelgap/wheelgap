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
# `  Using cached https://.../cryptography-43.0.0.tar.gz`.
_COLLECT = re.compile(rf" {{0,2}}Collecting ({_NAME})(?:\[[^\]]*\])?(.*)$")
_PIN = re.compile(r"\s*===?\s*([^\s,;*]+)(?:\s|$)")
_FILE = re.compile(r" {0,4}(?:Downloading|Using cached) (\S+)")
# pip, for each package it builds: `Building wheel for X (pyproject.toml) ... error`, then
# `ERROR: Failed building wheel for X` if it fails.
_BUILDING = re.compile(rf"(?:Building wheel for|Failed building wheel for) ({_NAME})(?:\s|$)")
# pip's summary, at column 0: `Failed to build X Y`, and pip 24.1+'s `ERROR: Failed to build installable wheels for
# some pyproject.toml based projects (X, Y)`.
_PIP_INSTALLABLE = re.compile(
    r"(?:ERROR: )?Failed to build installable wheels for some pyproject\.toml based projects \(([^()]*)\)\s*$"
)
_PIP_SUMMARY = re.compile(r"(?:ERROR: )?Failed to build ([^`'\"]+)$")
# uv: `× Failed to build `x==1.0``, `× Failed to download and build `x==1.0``, older uv's `Failed to build: `x==1.0``.
_UV = re.compile(r"Failed to (?:download and )?build:? `([^`]*)`")
_UV_NAME_VERSION = re.compile(rf"({_NAME})(?:==(\S+))?")

_UNCLEAR = "unclear"  # the log collects the package again without a version after naming one
_DIRECT = "direct"  # the log installs the package from a URL or a path, not from an index


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


def failed_builds(text: str) -> list[tuple[str, str | None]]:
    """The (name, version) of each package the log says failed to build, in the order the log names them. The version
    is the one uv names, else the one on the nearest `Collecting`, `Downloading` or `Using cached` line before the
    failure, else None."""
    versions: dict[str, str] = {}  # canonical name -> the latest version the log names, _UNCLEAR or _DIRECT
    built: set[str] = set()  # names pip printed `Building wheel for` or `Failed building wheel for` for
    failures: dict[str, tuple[str, str | None]] = {}

    def failed(name: str, version: str | None) -> None:
        key = canonicalize_name(name)
        if version is None:
            version = versions.get(key)
            if version == _UNCLEAR:
                raise SourceError(
                    f"--from-log: the log collects {name} again without a version after naming one, so which "
                    f"version failed isn't clear; refusing to guess (check {name}==VERSION instead)"
                )
        if version == _DIRECT:
            raise SourceError(
                f"--from-log: the log installs {name} from a URL or a path, not from an index, so it can't be checked"
            )
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

    def pip_summary(names: list[str]) -> None:
        # Only names pip printed `Building wheel for` for: a build's own output at column 0 (`Failed to build the
        # extension`) isn't pip's summary. A line that names any of them must name only them.
        if not any(_NAME_RE.fullmatch(n) and canonicalize_name(n) in built for n in names):
            return
        if not all(_NAME_RE.fullmatch(n) and canonicalize_name(n) in built for n in names):
            raise SourceError("--from-log: can't read pip's list of packages that failed to build; refusing to guess")
        for n in names:
            failed(n, None)

    for line in _ANSI.sub("", text).splitlines():
        if m := _TIMESTAMP.match(line):
            line = line[m.end() :]
        if m := _COLLECT.match(line):
            key, rest = canonicalize_name(m.group(1)), m.group(2)
            if (pin := _PIN.match(rest)) and _is_version(pin.group(1)):
                versions[key] = pin.group(1)
            elif rest.lstrip().startswith("@"):
                versions[key] = _DIRECT
            elif key in versions:
                versions[key] = _UNCLEAR
        elif (m := _FILE.match(line)) and (found := _file_version(m.group(1))):
            versions[canonicalize_name(found[0])] = found[1]
        if m := _BUILDING.search(line):
            built.add(canonicalize_name(m.group(1)))
        if m := _UV.search(line):
            uv = _UV_NAME_VERSION.fullmatch(m.group(1))
            if uv is None or not _NAME_RE.fullmatch(uv.group(1)) or not _is_version(uv.group(2)):
                raise SourceError(
                    "--from-log: uv says a package failed to build, but not as NAME==VERSION (a URL or a path?), "
                    "so it can't be checked"
                )
            failed(uv.group(1), uv.group(2))
        elif m := _PIP_INSTALLABLE.match(line):
            pip_summary([n.strip() for n in m.group(1).split(",")])
        elif m := _PIP_SUMMARY.match(line):
            pip_summary(m.group(1).split())

    if not failures:
        raise SourceError(
            "--from-log: found no package that failed to build (pip's `Failed to build NAME` or uv's "
            "``Failed to build `NAME==VERSION` ``)"
        )
    return list(failures.values())


def _file_version(location: str) -> tuple[str, str] | None:
    """The (name, version) of the file a `Downloading` or `Using cached` line names; None if it's not a distribution."""
    filename = location.split("#", 1)[0].split("?", 1)[0].rsplit("/", 1)[-1]
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


def log_source(failures: list[tuple[str, str | None]]) -> Source:
    """The packages to check, as PyPI names: built from the log's names and versions, never read as paths."""
    source = Source("install log", pypi_only=True)
    for name, version in failures:
        source.packages.append(requirement_to_package(f"{name}=={version}" if version else name, "install log"))
    return source
