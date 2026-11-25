"""--from-log: the packages a failed pip, uv, Poetry or PDM install log says didn't build, with their logged versions.

The log is untrusted text. Only a few exact message shapes count, and a failure that can't be read is an error, never
a guess.
"""

from __future__ import annotations

import codecs
import re
import shlex
import sys
from pathlib import Path
from typing import BinaryIO

from packaging.requirements import InvalidRequirement, Requirement
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
# Docker BuildKit's plain progress output: `#12 28.56 ` (step and seconds) on each line of a RUN's output, and `28.56 `
# alone in the failed step's excerpt that `docker build` prints at the end.
_BUILDKIT = re.compile(r"(?:#(?P<step>\d+) )?\d+\.\d+(?: |$)")
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
# A pip or uv pip install command in the log (a shell prompt, a Dockerfile's `RUN`, BuildKit's step name): its exact
# pins give a failed package's version when pip's own lines don't.
_INSTALL_COMMAND = re.compile(r"(?:^|[\s>$:;&|(\"'/])pip3?(?:\.\d+)?\s+install\s+(.*)$")
# pip install options that take a value, which isn't a requirement.
_INSTALL_VALUE_OPTIONS = frozenset(
    "-r --requirement -c --constraint -e --editable -i --index-url --extra-index-url -f --find-links -t --target "
    "--prefix --root --python --platform --python-version --implementation --abi --src --cache-dir --log "
    "--progress-bar --upgrade-strategy --report --only-binary --no-binary --trusted-host --proxy --timeout --retries "
    "--exists-action --cert --client-cert --config-settings -C --global-option --keyring-provider -p".split()
)
# pip's own line for a project it doesn't collect from an index: `Processing ./src` (`pip install .`), `Obtaining
# file:///...` (`pip install -e .`).
_LOCAL = re.compile(r" {0,2}(?:Processing|Obtaining) (\S+)")
# pip's error when a package's sdist fails before `Building wheel for`: getting its build requirements, or its
# metadata. pip prints it twice (indented, then at column 0) and names no package: it's the one collected last.
_PREPARE_FAILED = re.compile(
    r" {0,2}(?:(?:× ?)?(?:Getting requirements to build (?:wheel|editable)|Preparing (?:editable )?metadata "
    r"\([^)]*\)) did not run successfully\.?|error: metadata-generation-failed|(?:× ?)?Encountered error while "
    r"generating package metadata\.?)\s*"
)
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
# Captured with Poetry 2.5.1 and PDM 2.29.7, normal and -v (tests/fixtures/install-log-captures.md).
# Poetry prefixes most backend output with `    | `, but what the backend writes to stderr is at column 0, so its
# final note counts only for a package and version one of Poetry's operation lines names earlier in the log:
# `  - Installing NAME (VERSION)`, or `  - Updating NAME (OLD -> NEW)` (and `Downgrading`) for NEW, with a trailing
# status (`: Pending...`, `: Failed`) when the console is a terminal.
_POETRY_NOTE = (
    "Note: This error originates from the build backend, and is likely not a problem with poetry "
    "but one of the following issues with "
)
_POETRY_PACKAGE = re.compile(rf"({_NAME}) \((.+)\)$")
_POETRY_OPERATION = re.compile(
    rf"  - (?:Installing ({_NAME}) \(([^()]+)\)|(?:Updating|Downgrading) ({_NAME}) \([^()]+ -> ([^()]+)\))"
    r"(?:: (?:Pending|Installing|Updating|Downgrading|Downloading|Preparing|Building|Failed)\b.*)?\s*$"
)
# A PDM install can fail for reasons other than building: require its named exception traceback as well.
_PDM_INSTALL = re.compile(rf"  ✖ Install ({_NAME}) (\S+) failed\s*$")
_PDM_ERROR = re.compile(rf"(?:add ({_NAME}) failed:|pdm\.termui: Error occurs adding ({_NAME}):)\s*$")
_PDM_SOURCE = re.compile(r"pdm\.termui: Running PEP 517 backend to build a wheel for <Link (\S+) \(from (\S+)\)>\s*$")


class LogError(SourceError):
    """A refusal a caller may want to word itself, without repeating the log's text: `kind` is OTHER_INDEX (the log
    used another index than PyPI: its `Looking in indexes:` line, or PDM's build link), FIND_LINKS (its `Looking in
    links:` line) or NO_FAILURE (it names no package that failed to build). The message is --from-log's, and the
    first two quote the log's URLs. Every other refusal is a plain SourceError that names at most a package and a
    version from the log."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


# Said for a failed package the log names no version of, once the latest release is looked up.
LATEST_NOTE = (
    "the log doesn't name the version of {name} that failed to build; checked {name} {version}, the latest release "
    "(if the install pinned another version, check that one)"
)

OTHER_INDEX = "other_index"
FIND_LINKS = "find_links"
NO_FAILURE = "no_failure"

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


def failed_builds(text: str, other_index: bool = False) -> list[tuple[str, str | None]]:
    """The (name, version) of each package the log says failed to build, in the order the log names them. The version
    is the one uv names; for pip, the one the `Collecting`, `Downloading` and `Using cached` lines since the package's
    last `Collecting` line name, in the same pip run (a run ends at `Successfully installed`); for Poetry, the one its
    build-backend note names, if an earlier `Installing`/`Updating`/`Downgrading` line names it too; and for PDM
    (`pdm install -v` only), the one its `✖ Install NAME VERSION failed` line names, with a `BuildError` traceback for
    that package and the build link that says which index the sdist came from. pip's `Getting requirements to build
    wheel` and `Preparing metadata` failures name no package: it's the sdist of the last column-0 `Collecting` line,
    downloaded since. When pip's lines name no version, an exact pin on a `pip install` line in the log gives it, and
    with none the version is None (the caller checks the latest release, and says so), unless the log has pip's
    `Processing`/`Obtaining` line for a local project. A failure whose version the log doesn't make clear is an error,
    and so is a log that used another index than PyPI or --find-links (pip), or that built the package from another
    index (PDM), unless `other_index` (wheelgap itself checks another index, as with --index-url)."""
    # canonical name -> the versions named since its last `Collecting` line (Version -> as written), or _UNCLEAR,
    # _DIRECT or _EARLIER
    versions: dict[str, dict[Version, str] | str] = {}
    # The first package pip's resolver tried more than one release of. Backtracking on one package changes the
    # versions its dependencies are collected at, and pip doesn't log a candidate again when it goes back to it.
    backtracked: str | None = None
    built: set[str] = set()  # names pip printed `Building wheel for` or `Failed building wheel for` for
    failures: dict[str, tuple[str, str | None]] = {}
    # canonical name -> the versions `pip install` lines in the log pin it to exactly
    command_pins: dict[str, set[Version | None]] = {}  # None: an install command names it without an exact pin
    step: str | None = None  # the BuildKit step of the last `#N` line
    steps_left: set[str] = set()
    interleaved = False  # BuildKit steps' output mixed (#7, #8, #7): pip's lines may be two runs'
    local = False  # pip's `Processing` or `Obtaining` line for a project that isn't a distribution file
    # The sdist (name, version) downloaded since the last column-0 `Collecting` line, for the package it collects.
    collecting: str | None = None
    collected_sdist: tuple[str, str] | None = None
    poetry_build = False
    poetry_operations: dict[str, set[str]] = {}  # canonical name -> the versions Poetry's operation lines name
    pdm_versions: dict[str, set[str]] = {}
    pdm_sources: dict[str, set[Version]] = {}
    pdm_other_sources: dict[str, str] = {}  # canonical name -> the index (or other source) it was built from instead
    pdm_verbose = False  # `pdm install -v`: its own lines start with `pdm.termui: `
    pdm_error: str | None = None
    pdm_traceback = False

    def failed(name: str, version: str | None, pip: bool = False) -> None:
        key = canonicalize_name(name)
        if pip:
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

    def interleaving(name: str) -> None:
        # pip's lines name the version before the failure: from two steps, they may be another run's.
        if interleaved:
            raise SourceError(
                "--from-log: the log mixes the output of more than one Docker build step, so which version of "
                f"{name} failed isn't clear; refusing to guess (check {name}==VERSION directly)"
            )

    def pip_version(name: str, key: str) -> str | None:
        interleaving(name)
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
            # `pip install -q` hides `Collecting`, and a log's start may be cut off: an exact pin on the install
            # command gives the version, else it's the latest (said in a note). But for a local project (`pip
            # install .` logs `Processing`, never `Collecting`), PyPI's release of that name may be another project.
            pins = command_pins.get(key, set())
            if len(pins) > 1:
                raise SourceError(
                    f"--from-log: the log's install commands pin {name} to more than one version (or also "
                    f"unpinned), so which version failed is unclear; refusing to guess (check {name}==VERSION directly)"
                )
            if None not in pins and pins:
                return str(next(iter(pins)))
            if local:
                raise SourceError(
                    f"--from-log: the log doesn't say which version of {name} failed to build, and pip's "
                    "`Processing`/`Obtaining` line or the install command says it may be a local project, "
                    "not PyPI's; refusing to guess "
                    f"(check {name}==VERSION directly)"
                )
            return None
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
            failed(n, None, pip=True)

    # After pip 25.2's `Failed to build installable wheels ...`: whether it had the `×`, so the names follow `╰─>`.
    diagnostic: bool | None = None
    for line in _ANSI.sub("", text).splitlines():
        if m := _TIMESTAMP.match(line):
            line = line[m.end() :]
        if m := _BUILDKIT.match(line):
            line = line[m.end() :]
            if (sid := m.group("step")) and sid != step:
                interleaved |= sid in steps_left
                if step is not None:
                    steps_left.add(step)
                step = sid
        # Not a comment, an echo, or a build's output (indented further than a Dockerfile excerpt's `   3 | >>> RUN`)
        if (
            (m := _INSTALL_COMMAND.search(line))
            and len(line) - len(line.lstrip()) < 6
            and not re.match(r"\s*#(?!\d+ )|.*\becho\b", line[: m.start() + 1])
        ):
            pins_found, local_arg = _command_pins(m.group(1))
            local |= local_arg
            for pin_key, pinned in pins_found:
                command_pins.setdefault(pin_key, set()).add(pinned)
        if line.startswith("Synchronizing working set with resolved packages:"):
            pdm_versions.clear()
            pdm_sources.clear()
            pdm_other_sources.clear()
        if line.startswith("pdm.termui: "):
            pdm_verbose = True
        if m := _PDM_SOURCE.fullmatch(line):
            if found := _file_version(m.group(1)):
                source_key = canonicalize_name(found[0])
                if m.group(2).rstrip("/") == f"{_PYPI_SIMPLE}/{source_key}" or other_index:
                    pdm_sources.setdefault(source_key, set()).add(Version(found[1]))
                else:
                    pdm_other_sources[source_key] = _pdm_index(m.group(2), source_key)
        if m := _POETRY_OPERATION.fullmatch(line):
            poetry_operations.setdefault(canonicalize_name(m.group(1) or m.group(3)), set()).add(
                m.group(2) or m.group(4)
            )
        if line == "PEP517 build of a dependency failed":
            poetry_build = True
        if line.startswith(_POETRY_NOTE) and (candidate := _POETRY_PACKAGE.fullmatch(line.removeprefix(_POETRY_NOTE))):
            # A note for a package no operation line names is the backend's own output, not Poetry's.
            if (operations := poetry_operations.get(canonicalize_name(candidate.group(1)))) is not None:
                note_version = candidate.group(2)
                if (
                    not poetry_build
                    or not _is_version(note_version)
                    or not any(_is_version(v) and Version(v) == Version(note_version) for v in operations)
                ):
                    raise SourceError("--from-log: can't read Poetry's failed package and version; refusing to guess")
                failed(candidate.group(1), candidate.group(2))
                poetry_build = False
        if m := _PDM_INSTALL.fullmatch(line):
            pdm_versions.setdefault(canonicalize_name(m.group(1)), set()).add(m.group(2))
        if m := _PDM_ERROR.fullmatch(line):
            if pdm_error is not None:
                raise SourceError("--from-log: PDM's install failure is not a clear build failure; refusing to guess")
            pdm_error = m.group(1) or m.group(2)
            pdm_traceback = False
        elif pdm_error is not None and line == "Traceback (most recent call last):":
            pdm_traceback = True
        elif pdm_error is not None and line.startswith("pdm.exceptions.BuildError: Build backend raised error:"):
            seen = pdm_versions.get(canonicalize_name(pdm_error), set())
            if not pdm_traceback or len(seen) != 1 or not all(_is_version(v) for v in seen):
                raise SourceError("--from-log: can't read PDM's failed package and version; refusing to guess")
            version = next(iter(seen))
            error_key = canonicalize_name(pdm_error)
            if pdm_sources.get(error_key) != {Version(version)}:
                if error_key in pdm_other_sources:
                    raise LogError(
                        f"--from-log: PDM built {pdm_error} from another index than PyPI "
                        f"({_USERINFO.sub('//', pdm_other_sources[error_key])}), so it may not be PyPI's; pass that "
                        "index with --index-url to check it",
                        OTHER_INDEX,
                    )
                if pdm_verbose:
                    raise SourceError(
                        f"--from-log: PDM's log doesn't say which index {pdm_error} {version} was built from; "
                        "refusing to guess"
                    )
                raise SourceError(
                    "--from-log: PDM's log doesn't identify the failed package's index source; rerun pdm install -v "
                    "(normal output also hides URL and path dependencies); refusing to guess"
                )
            failed(pdm_error, version)
            pdm_error = None
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
                raise LogError(
                    f"--from-log: the log used another index than PyPI ({', '.join(others)}), so its packages may "
                    "not be PyPI's; pass that index with --index-url to check it",
                    OTHER_INDEX,
                )
        if (m := _LINKS.match(line)) and not other_index:
            links = ", ".join(_USERINFO.sub("//", url.strip()) for url in m.group(1).split(","))
            raise LogError(
                f"--from-log: the log used --find-links ({links}), so its packages may not be PyPI's; pass the "
                "index they came from with --index-url to check it",
                FIND_LINKS,
            )
        if _INSTALLED.match(line):
            # A later run's failure without its own `Collecting` line must not take this run's version.
            for key in versions:
                versions[key] = _EARLIER
        if (m := _LOCAL.match(line)) and _file_version(m.group(1)) is None:
            local = True
            collecting = collected_sdist = None
        if m := _COLLECT.match(line):
            key, rest = canonicalize_name(m.group(1)), m.group(2)
            collecting, collected_sdist = key, None
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
            if canonicalize_name(found[0]) == collecting and m.group(1).endswith((".tar.gz", ".zip")):
                collected_sdist = found
        if _PREPARE_FAILED.fullmatch(line):
            if collected_sdist is None:
                raise SourceError(
                    "--from-log: pip failed to prepare a package to build, but the log doesn't name its sdist after "
                    "the last `Collecting` line; refusing to guess"
                )
            interleaving(collected_sdist[0])
            failed(*collected_sdist)
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
    if poetry_build:
        raise SourceError("--from-log: can't read Poetry's failed package and version; refusing to guess")
    if pdm_error is not None:
        raise SourceError("--from-log: PDM's install failure is not a clear build failure; refusing to guess")

    if not failures:
        raise LogError(
            "--from-log: found no package that failed to build (pip's `Failed to build NAME` or uv's "
            "``Failed to build `NAME==VERSION` ``, or a supported Poetry/PDM build diagnostic)",
            NO_FAILURE,
        )
    return list(failures.values())


def _command_pins(args: str) -> tuple[list[tuple[str, Version | None]], bool]:
    """The (canonical name, version) of each requirement among a pip install command's arguments (None without an
    exact pin), and whether it installs a local path (`.`, `-e .`, `C:\\src\\foo`)."""
    try:
        words = shlex.split(args, comments=True)
    except ValueError:  # an unclosed quote: the end of a command quoted inside another line (Docker's error)
        words = [word.strip("'\"") for word in args.split()]
    pins: list[tuple[str, Version | None]] = []
    local = False
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word in ("-e", "--editable"):
            local = True
        if word.startswith("-"):
            skip = word in _INSTALL_VALUE_OPTIONS
            continue
        if word.startswith((".", "/", "~")) or "/" in word or "\\" in word or re.match(r"[A-Za-z]:", word):
            local = True
            continue
        try:
            req = Requirement(word)
        except InvalidRequirement:
            continue
        clauses = list(req.specifier)
        exact = not req.url and len(clauses) == 1 and clauses[0].operator in ("==", "===")
        if exact and "*" not in clauses[0].version and _is_version(clauses[0].version):
            pins.append((canonicalize_name(req.name), Version(clauses[0].version)))
        else:
            pins.append((canonicalize_name(req.name), None))
    return pins, local


def _pdm_index(url: str, key: str) -> str:
    """The index a PDM build link's `(from URL)` names: the URL without its last path segment when that is the
    package's project page (`https://example.com/simple/NAME/`), else the URL as it is."""
    base, _, last = url.rstrip("/").rpartition("/")
    if url.startswith(("https://", "http://")) and base.count("/") > 2 and canonicalize_name(last) == key:
        return base
    return url.rstrip("/")


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


def log_source(failures: list[tuple[str, str | None]]) -> Source:
    """The packages to check, as PyPI names: built from the log's names and versions (the latest release when the log
    names none), never read as paths."""
    source = Source("install log", pypi_only=True)
    for name, version in failures:
        source.packages.append(requirement_to_package(name if version is None else f"{name}=={version}", "install log"))
    return source
