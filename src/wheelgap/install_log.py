"""Read failed pip/uv wheel builds from installer logs without executing log content."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from packaging.utils import canonicalize_name

from .sources import _NAME_RE, SourceError

MAX_LOG_BYTES = 1024 * 1024

# Version pins appear in pip's preceding "Collecting" and wheel-building progress messages.
_COLLECT = re.compile(r"\bCollecting\s+([A-Za-z0-9][A-Za-z0-9._-]*)(?:==([A-Za-z0-9][A-Za-z0-9._+!-]*))?", re.I)
_BUILD = re.compile(r"\bBuilding wheel for\s+([A-Za-z0-9][A-Za-z0-9._-]*)(?:\s*\([^\n]*?\))?", re.I)
_PIP_ERROR = re.compile(r"(?:ERROR:\s*)?Failed building wheel for\s+(.+)$", re.I)
_PIP_SUMMARY = re.compile(r"^\s*(?:ERROR:\s*)?Failed to build\s+(.+)$", re.I)
_UV_ERROR = re.compile(r"Failed to build\s+[`'\"]([^ `'\"\r\n]+)[`'\"]", re.I)
_NAME_VERSION = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:==([A-Za-z0-9][A-Za-z0-9._+!-]*))?$")


def read_log(path: str) -> str:
    """Cap reads (also stdin) before decoding; logs can contain arbitrary untrusted text."""
    if path == "-":
        data = sys.stdin.buffer.read(MAX_LOG_BYTES + 1)
    else:
        try:
            with Path(path).open("rb") as f:
                data = f.read(MAX_LOG_BYTES + 1)
        except OSError as e:
            raise SourceError(f"--from-log: cannot read {path}: {e.strerror or type(e).__name__}") from e
    if len(data) > MAX_LOG_BYTES:
        raise SourceError(f"--from-log: input exceeds {MAX_LOG_BYTES} bytes")
    return data.decode("utf-8", "replace")


def failed_builds(text: str) -> tuple[list[str], list[str]]:
    """Package specs for build failures, and names without a recoverable version."""
    versions: dict[str, set[str]] = {}
    failures: dict[str, str | None] = {}

    def remember_version(name: str, version: str | None) -> None:
        if version and _NAME_RE.fullmatch(name):
            versions.setdefault(canonicalize_name(name), set()).add(version)

    def failed(token: str) -> None:
        m = _NAME_VERSION.fullmatch(token.strip())
        if m is None or not _NAME_RE.fullmatch(m.group(1)):
            raise SourceError("--from-log: cannot read failed package name/version; refusing to guess")
        name, version = m.groups()
        key = canonicalize_name(name)
        if key in failures and failures[key] and version and version != failures[key]:
            raise SourceError(f"--from-log: conflicting failed versions for {name}")
        failures[key] = version or failures.get(key)
        remember_version(name, version)

    for line in text.splitlines():
        if m := _COLLECT.search(line):
            remember_version(*m.groups())
        if m := _BUILD.search(line):
            remember_version(m.group(1), None)
        if m := _UV_ERROR.search(line):
            failed(m.group(1))
        elif m := _PIP_ERROR.search(line):
            failed(m.group(1).strip())
        elif m := _PIP_SUMMARY.search(line):
            # pip's summary may include several package names; only separate on whitespace/commas.
            tokens = m.group(1).strip().strip(".").replace(",", " ").split()
            if not tokens:
                raise SourceError("--from-log: failed package list is empty")
            for token in tokens:
                failed(token)

    if not failures:
        raise SourceError("--from-log: no supported pip or uv wheel-build failure found")
    specs = []
    unpinned = []
    for key, version in failures.items():
        found = versions.get(key, set())
        if version is None:
            if len(found) == 1:
                version = next(iter(found))
            elif len(found) > 1:
                raise SourceError(f"--from-log: ambiguous versions for {key}; refusing to guess")
        if version:
            specs.append(f"{key}=={version}")
        else:
            specs.append(key)
            unpinned.append(key)
    return specs, unpinned
