"""An MCP server, so coding agents can ask wheelgap whether a package has a wheel for a platform.

Run it with `python -m wheelgap.mcp` (or the `wheelgap-mcp` command). It speaks the Model Context Protocol over
stdio: one JSON-RPC 2.0 message per line on stdin and stdout. It serves both eras of the protocol, request by request:
- 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25, through the initialize handshake;
- 2026-07-28, which has no handshake: each request names its protocol version and the client's capabilities in
  params._meta, server/discover says which versions and capabilities the server has, and each result carries
  resultType and the server's name and version. A request for a version the server doesn't serve gets error -32022,
  listing the versions it does.
Like the rest of wheelgap it needs only the standard library and packaging.

Tool calls run on one worker thread, one at a time, while the main thread keeps reading stdin: it answers ping and
handles notifications/cancelled during a long check_lockfile. One worker, because check_lockfile redirects the
process-wide sys.stdout and sys.stderr around cli.main. A call given a _meta.progressToken gets
notifications/progress as its index lookups finish; a cancelled call sends no more of them, and no result.

Tools (all read-only; they look packages up on PyPI, or the index in WHEELGAP_INDEX_URL):
- check_package: one package (latest release or a given version) on each target.
- check_lockfile: what `wheelgap PATH --json` reports, so project config, notes and skipped packages match the CLI.
- check_dockerfile: what `wheelgap PATH --dockerfile DOCKERFILE --json` reports, plus the image it read.
- diff_lockfiles: what `wheelgap diff OLD NEW --json` reports (the packages that got worse, and new ones with gaps).
- list_targets: the platform names the other tools accept.

Paths may be file:// URIs. If the client has the roots capability, the server asks it for roots/list after
notifications/initialized (and again on notifications/roots/list_changed), and a relative path is resolved against the
first root that contains it, then against the working directory. 2026-07-28 requests are stateless and the server may
not send requests of its own, so there a tools/call with a relative path from a client with the roots capability gets
an input_required result asking for roots/list, and the client retries with the roots in inputResponses. That relative
path must then be in one of those roots (unless the list is empty): it is never looked for in the working directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path, PurePath
from typing import Any, BinaryIO

from packaging.version import InvalidVersion, Version

from . import __version__, pypi
from .api import Result, check_package, make_targets, running_python
from .check import EXPLAIN_LIMIT, Status
from .dockerfile import DockerfileError, read_dockerfile
from .pyodide import PyodideLockError, load_pyodide_lock
from .sources import SourceError
from .targets import (
    ALIASES,
    DEFAULT_GLIBC,
    DEFAULT_MACOS,
    DEFAULT_MUSL,
    DEFAULT_PLATFORMS,
    EVERY_NATIVE_PLATFORM,
    MOBILE_PLATFORMS,
    NATIVE_PLATFORMS,
    PLATFORMS,
    TargetError,
)

# The handshake-era revisions, oldest first. A client asking for one of these gets it back; any other version gets the
# newest, and the client decides whether it can use that (as the MCP lifecycle spec says).
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
# The revisions without a handshake, oldest first: each request carries its version in params._meta.
MODERN_VERSIONS = ("2026-07-28",)
_STRUCTURED_FROM = "2025-06-18"  # the first revision with structuredContent in tool results
_PROGRESS_MESSAGE_FROM = "2025-03-26"  # the first revision with a message in notifications/progress

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022  # from 2026-07-28

# The per-request _meta keys of 2026-07-28 (the first two are required on every request), and the result's.
_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
_ENVELOPE_KEYS = (
    _VERSION_KEY,
    _CAPABILITIES_KEY,
    "io.modelcontextprotocol/clientInfo",
    "io.modelcontextprotocol/logLevel",
)
_SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
_ROOTS_INPUT = "roots"  # our key for roots/list in a 2026 input_required result's inputRequests
_REQUESTED_LIMIT = 100  # how much of an unsupported version string an error repeats
SERVER_INFO = {"name": "wheelgap", "version": __version__}
CAPABILITIES = {"tools": {"listChanged": False}}
# 2026-07-28's caching hints, required on server/discover and tools/list. Both are fixed for the server's lifetime
# and the same for every user: an hour, then the client asks again.
_CACHE_HINTS = {"ttlMs": 3_600_000, "cacheScope": "public"}

INSTRUCTIONS = (
    "wheelgap tells you whether pip or uv can install a package from a prebuilt wheel on a platform, or will have to "
    "build it from source (or can't install it at all). Use check_lockfile on a project's uv.lock, poetry.lock, "
    "pdm.lock, pylock.toml, Pipfile.lock or requirements.txt before changing the platforms it deploys to, "
    "check_dockerfile before changing a Dockerfile's FROM line (it reads the image's Python, platform and libc for "
    "you), check_package before adding a dependency, and diff_lockfiles to see whether a dependency upgrade lost a "
    "wheel anywhere (save the old lockfile under its own name first, e.g. `mkdir -p /tmp/old && git show "
    "HEAD:uv.lock > /tmp/old/uv.lock`). Pass the project's Python version: results "
    "differ between versions. Linux results assume a glibc (manylinux) or musl version: pass the one your image or "
    "distro has."
)

_STATUS_HELP = (
    "Statuses: 'wheel' (a compatible wheel exists), 'source' (no wheel, so installers build the sdist and need a "
    "compiler and headers), 'missing' (nothing installable: no wheel and no sdist, or, on pyodide, iOS and Android, "
    "which can't build sdists, no wheel), 'unknown' (the lookup failed: not a pass), 'n/a' (environment markers say "
    "it isn't installed there), 'bundled' (Pyodide ships its own build). On pyodide, packages Pyodide ships itself "
    "(numpy and many others) show 'source' or 'missing' unless a pyodide_lock is given (check_lockfile also reads "
    "[tool.wheelgap] pyodide-lock)."
)

_PYTHON_PROP = {
    "type": "string",
    "description": "Target Python version(s), comma-separated, e.g. '3.13' or '3.12,3.14t' (t = free-threaded); "
    "'next' and 'next-t' mean the newest CPython wheelgap knows.",
}
_TARGETS_PROP = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Platforms, e.g. ['linux-aarch64', 'musl-x86_64', 'win-arm64', 'pyodide'], Docker platforms such as "
        f"'linux/arm64', or the groups 'all', 'everything' and 'mobile'. Default: {', '.join(DEFAULT_PLATFORMS)}. "
        "list_targets gives every name."
    ),
}
_VERSION_PROPS = {
    "glibc": {
        "type": "string",
        "description": f"glibc version for linux-* targets, e.g. '2.28' for a RHEL 8 image. Default: {DEFAULT_GLIBC}.",
    },
    "musl": {"type": "string", "description": f"musl version for musl-* (Alpine) targets. Default: {DEFAULT_MUSL}."},
    "macos": {"type": "string", "description": f"Oldest macOS version to support. Default: {DEFAULT_MACOS}."},
}
_PYODIDE_LOCK_PROP = {
    "type": "string",
    "description": (
        "Absolute path or https URL of a Pyodide release's pyodide-lock.json, so packages that release ships itself "
        "count as 'bundled' on pyodide targets for its Python. Its Python must be one a pyodide target uses. "
        "A relative path is looked for in the client's roots if it shares them, else in the server's working "
        "directory."
    ),
}
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "check_package",
        "title": "Check a package's wheels",
        "description": (
            "Check whether one PyPI package has a prebuilt wheel for each target platform and Python. Returns each "
            "target's status, the targets with a gap, ok (true only if every target installs without building) and "
            "the libc and macOS versions assumed. With explain: true, also says why: rejected lists, for each "
            f"source or missing target, up to {EXPLAIN_LIMIT} wheels it can't use and the reason for each, closest "
            "misses first (rejected_omitted counts any more), as wheelgap --json --explain does. " + _STATUS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The package name alone, e.g. 'numpy'."},
                "version": {
                    "type": "string",
                    "description": "An exact version, e.g. '2.3.4'. Default: the latest release, which may not "
                    "support an older Python (it then shows gaps an older release wouldn't have).",
                },
                "python": _PYTHON_PROP | {"description": _PYTHON_PROP["description"] + " Default: the server's."},
                "targets": _TARGETS_PROP,
                **_VERSION_PROPS,
                "pyodide_lock": _PYODIDE_LOCK_PROP,
                "explain": {
                    "type": "boolean",
                    "description": "Also return rejected (and via, always empty for one package): why each source "
                    "or missing target can't use the published wheels. Default: false.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "check_lockfile",
        "title": "Check a lockfile's wheels",
        "description": (
            "Check every package a lockfile installs (uv.lock, poetry.lock, pdm.lock, pylock.toml, Pipfile.lock, "
            "requirements.txt, or a project directory containing one), exactly as the wheelgap command line does, and "
            "report the packages without a wheel on some target. Environment markers, .python-version and the "
            "project's [tool.wheelgap] settings (targets, python, glibc, ignore, offline, index-url, ...) apply; "
            "arguments given here override them, except that offline = true in the project can't be turned off. "
            "Notes and errors name command-line flags (--offline is the offline argument here). "
            "Packages from git, paths, URLs or private indexes are listed under "
            "'skipped': they were NOT checked. 'notes' says when an answer may be incomplete. Requirements files are "
            "looked up on the index. 'ok' is true only when no package has a gap; 'passes' is the command line's "
            "verdict (exit code 0), which also accepts gaps on the project's advisory targets (listed in 'advisory'; "
            "each gap's 'advisory_gaps' says which of its 'gaps' are on them) and, with fail-on = \"missing\", "
            "source builds. "
            "With two Pythons checked, one newer (e.g. python '3.14,next'), "
            "'blocking_newer_python' names the packages with a gap on the newer one only; it leaves out packages "
            "that couldn't be checked. " + _STATUS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path or file:// URI of the lockfile or project directory (a relative "
                    "path is looked for in the client's roots if it shares them, else in the server's working "
                    "directory).",
                },
                "python": _PYTHON_PROP
                | {
                    "description": _PYTHON_PROP["description"] + " Default: [tool.wheelgap] python, then "
                    ".python-version next to the lockfile, else the server's."
                },
                "targets": _TARGETS_PROP,
                **_VERSION_PROPS,
                "pyodide_lock": _PYODIDE_LOCK_PROP
                | {"description": _PYODIDE_LOCK_PROP["description"] + " Default: [tool.wheelgap] pyodide-lock."},
                "offline": {
                    "type": "boolean",
                    "description": "Use only the files recorded in the lockfile; never contact the index.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "check_dockerfile",
        "title": "Check a lockfile against a Dockerfile's image",
        "description": (
            "Check a lockfile for the image a Dockerfile's final stage builds on, exactly as `wheelgap PATH "
            "--dockerfile DOCKERFILE --json` does: the image's Python, platform (from FROM --platform, default "
            "linux/amd64) and glibc or musl version (python:3.13-alpine means musl) set the target. Use it before "
            "changing a FROM line, e.g. from -slim to -alpine or to --platform=linux/arm64. Returns what "
            "check_lockfile returns, plus 'image': the python, platform, glibc and musl wheelgap read from the "
            "Dockerfile. A Dockerfile wheelgap can't read the way Docker does is a tool error saying what to pass "
            "instead. " + _STATUS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dockerfile": {
                    "type": "string",
                    "description": "Absolute path or file:// URI of the Dockerfile (a relative path is looked "
                    "for in the client's roots if it shares them, else in the server's working directory).",
                },
                "path": {
                    "type": "string",
                    "description": "Absolute path or file:// URI of the lockfile or project directory. Default: the "
                    "Dockerfile's folder.",
                },
                "targets": _TARGETS_PROP
                | {
                    "description": "Platforms to check instead of the image's, e.g. ['linux/arm64'] (with an Alpine "
                    "image, Docker platforms mean musl). Default: the image's platform."
                },
            },
            "required": ["dockerfile"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "diff_lockfiles",
        "title": "Compare two lockfiles",
        "description": (
            "Compare a lockfile before and after a change (a dependency upgrade, a re-lock), exactly as `wheelgap diff "
            "OLD NEW --json` does, and report what got worse: 'worse' lists packages whose status dropped on a target "
            "(e.g. wheel → source after a version bump), 'new' lists added packages with a gap, and 'ok' is true only "
            "when both are empty. A package that can no longer be checked (unknown, or now from git, a path, a URL or a"
            " private index) counts as worse; 'unchecked' names packages unknown on both sides at the same version. "
            "Pass the old lockfile as 'old' under its own file name, since the name says how to read it: e.g. `mkdir -p"
            " /tmp/old && git show HEAD:uv.lock > /tmp/old/uv.lock` (this works after the upgrade too, until it's "
            "committed). As with `wheelgap diff`, the project's [tool.wheelgap] settings don't apply: pass python and "
            "targets. " + _STATUS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "old": {
                    "type": "string",
                    "description": "Absolute path to the lockfile (or project directory) before the change.",
                },
                "new": {
                    "type": "string",
                    "description": "Absolute path to the lockfile (or project directory) after the change.",
                },
                "python": _PYTHON_PROP | {"description": _PYTHON_PROP["description"] + " Default: the server's."},
                "targets": _TARGETS_PROP,
            },
            "required": ["old", "new"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "list_targets",
        "title": "List target platforms",
        "description": "List the platform names and groups that check_package, check_lockfile, check_dockerfile and "
        "diff_lockfiles accept.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _READ_ONLY | {"openWorldHint": False},
    },
]


class ToolError(Exception):
    """A problem with the tool's arguments or input, reported to the model as a tool result with isError."""


_PYTHON_RE = re.compile(r"\d+\.\d+t?|next(-t)?", re.ASCII)
_NAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", re.ASCII)
_GAP = {Status.SOURCE.value, Status.MISSING.value, Status.UNKNOWN.value}


def _check_keys(tool: str, args: dict[str, Any]) -> None:
    """Models guess argument names ('platforms', 'python_version'): refuse them rather than ignore them."""
    known = next(t for t in TOOLS if t["name"] == tool)["inputSchema"]["properties"]
    if unknown := sorted(set(args) - set(known)):
        raise ToolError(f"unknown argument(s): {', '.join(unknown)} (accepted: {', '.join(known) or 'none'})")


def _string(args: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"'{key}' is required")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolError(f"'{key}' must be a non-empty string" if required else f"'{key}' must be a string")
    return value


def _platforms(args: dict[str, Any]) -> list[str]:
    platforms = args.get("targets")
    if platforms is None:
        return []
    if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
        raise ToolError("'targets' must be an array of strings")
    return [p.strip() for p in platforms if p.strip()]


def _pythons(args: dict[str, Any]) -> list[str]:
    python = _string(args, "python") or ""
    parts = [p.strip().lower() for p in python.split(",") if p.strip()]  # the CLI takes Next and 3.14T too
    if bad := [p for p in parts if not _PYTHON_RE.fullmatch(p)]:
        raise ToolError(
            f"can't read Python version(s) {', '.join(map(repr, bad))} (expected e.g. '3.13', '3.14t' or 'next')"
        )
    return parts


def _versions(args: dict[str, Any]) -> dict[str, str]:
    out = {}
    for key in _VERSION_PROPS:
        value = _string(args, key)
        if value is not None and value.strip():
            out[key] = value.strip()
    return out


def _lock_spec(args: dict[str, Any]) -> str:
    spec = (_string(args, "pyodide_lock") or "").strip()
    if not spec or "://" in spec:
        return spec
    if _ROOTS_ONLY.get() and _is_relative(spec):  # a 2026 client named its roots: it must be in one of them
        return str(_existing(spec))
    return str(Path(spec).expanduser())


def _check_package(args: dict[str, Any]) -> dict[str, Any]:
    _check_keys("check_package", args)
    explain = args.get("explain")
    if explain is None:  # absent or null: like the other optional arguments
        explain = False
    if not isinstance(explain, bool):
        raise ToolError("'explain' must be true or false")
    name = (_string(args, "name", required=True) or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ToolError(f"{name!r} isn't a package name: pass the name alone, and an exact version as 'version'")
    version = (_string(args, "version") or "").strip() or None
    if version is not None:
        try:
            Version(version)
        except InvalidVersion:
            raise ToolError(f"'version' must be an exact version such as '2.3.4', not {version!r}") from None
    versions = _versions(args)
    try:
        targets = make_targets(_pythons(args) or None, _platforms(args) or None, **versions)
    except TargetError as e:
        raise ToolError(str(e)) from e
    lock_spec = _lock_spec(args)
    try:
        locks = [load_pyodide_lock(lock_spec)] if lock_spec else []
    except ValueError as e:  # PyodideLockError, or a file that isn't UTF-8
        raise ToolError(f"{lock_spec}: {e}" if isinstance(e, UnicodeDecodeError) else str(e)) from e
    except OSError as e:  # a lock path that can't be read
        raise ToolError(f"{lock_spec}: {e.strerror or e}") from e
    try:
        result: Result = check_package(name, version, targets, pyodide_locks=locks)
    except PyodideLockError as e:  # a lock for a Python no pyodide target uses
        raise ToolError(str(e)) from e
    out: dict[str, Any] = result.to_dict(explain=explain)
    if explain:  # as many wheels per cell as --explain's text shows, so a reply stays small
        out["rejected_omitted"] = {
            label: len(wheels) - EXPLAIN_LIMIT
            for label, wheels in out["rejected"].items()
            if len(wheels) > EXPLAIN_LIMIT
        }
        out["rejected"] = {label: wheels[:EXPLAIN_LIMIT] for label, wheels in out["rejected"].items()}
    out["ok"] = result.ok
    out["gaps"] = [t.label(True) for t in result.gaps]
    out["targets"] = [t.label(True) for t in targets]
    out["assumes"] = {
        "glibc": versions.get("glibc", DEFAULT_GLIBC),
        "musl": versions.get("musl", DEFAULT_MUSL),
        "macos": versions.get("macos", DEFAULT_MACOS),
    }
    return out


def _check_lockfile(args: dict[str, Any]) -> dict[str, Any]:
    """Run the command line itself with --json, so config, notes and skipped packages are exactly the CLI's."""
    _check_keys("check_lockfile", args)
    raw = _string(args, "path", required=True) or ""
    offline = args.get("offline")
    if offline is not None and not isinstance(offline, bool):
        raise ToolError("'offline' must be true or false")
    path = _existing(raw)
    # --name=value forms, and -- before the path, so no value can be read as another option.
    argv = ["--json"]
    argv += [f"--python={p}" for p in _pythons(args)]
    argv += [f"--target={p}" for p in _platforms(args)]
    argv += [f"--{key}={value}" for key, value in _versions(args).items()]
    if lock_spec := _lock_spec(args):
        argv.append(f"--pyodide-lock={lock_spec}")
    if offline:
        argv.append("--offline")
    return _run_cli(argv, path)


def _check_dockerfile(args: dict[str, Any]) -> dict[str, Any]:
    """check_lockfile with --dockerfile, plus the image the CLI reads from the Dockerfile."""
    _check_keys("check_dockerfile", args)
    dockerfile = _existing(_string(args, "dockerfile", required=True) or "")
    if dockerfile.is_dir():
        raise ToolError(f"{args['dockerfile']}: is a directory (pass the Dockerfile itself)")
    raw = (_string(args, "path") or "").strip()
    path = _existing(raw) if raw else dockerfile.parent
    try:
        image = read_dockerfile(dockerfile)
    except DockerfileError as e:  # wheelgap's message; its CLI advice, where it gives some, translated
        message = str(e).replace("pass -p/-t instead", "use check_lockfile with python and targets instead")
        raise ToolError(message) from e
    except OSError as e:  # a Dockerfile that can't be read
        raise ToolError(f"{args['dockerfile']}: {e.strerror or e}") from e
    argv = ["--json", f"--dockerfile={dockerfile}"]
    argv += [f"--target={p}" for p in _platforms(args)]
    try:
        out = _run_cli(argv, path)
    except ToolError as e:
        if raw or "no lockfile found" not in str(e):
            raise
        raise ToolError(f"{e} (pass 'path': the lockfile or project directory)") from e
    out["image"] = {"python": image.python, "platform": image.platform, "glibc": image.glibc, "musl": image.musl}
    return out


# The client's roots (from roots/list) for the call running on this thread; Server._call sets it.
_ROOTS: ContextVar[tuple[Path, ...]] = ContextVar("wheelgap_mcp_roots", default=())
# True when a relative path must be in one of those roots: a 2026 client named its roots for this very call.
_ROOTS_ONLY: ContextVar[bool] = ContextVar("wheelgap_mcp_roots_only", default=False)
_URI_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def _file_uri_path(uri: str) -> Path | None:
    """The local path of a file:// URI (Windows file:///C:/... too), or None if it isn't one on this machine."""
    parts = urllib.parse.urlsplit(uri)
    if parts.scheme.lower() != "file":
        return None
    if parts.netloc.lower() in ("", "localhost"):
        return Path(urllib.request.url2pathname(parts.path))
    if os.name == "nt":  # file://server/share/... is a UNC path there
        return Path(urllib.request.url2pathname(f"//{parts.netloc}{parts.path}"))
    return None


def _diff_lockfiles(args: dict[str, Any]) -> dict[str, Any]:
    """`wheelgap diff OLD NEW --json`, through diff.py's functions rather than its main(), plus ok."""
    from . import diff
    from .api import _check_lockfile as check_with_source

    _check_keys("diff_lockfiles", args)
    paths = [_existing(_string(args, key, required=True) or "") for key in ("old", "new")]
    try:
        targets = make_targets(_pythons(args) or None, _platforms(args) or None)
    except TargetError as e:
        raise ToolError(str(e)) from e
    sides = []
    for path in paths:
        try:
            # As in `wheelgap diff`: no offline mode, so a lock made for other Pythons is re-checked on the index
            # rather than compared on files it may be missing (which could cancel out and pass on a guess).
            results, source = check_with_source(path, targets, online=False, offline=False)
        except OSError as e:  # a lockfile that can't be read
            raise ToolError(f"{path}: {e.strerror or e}") from e
        except SourceError:
            raise
        except ValueError as e:  # malformed TOML or JSON, not UTF-8: the file's problem, not a bug here
            raise ToolError(f"{path}: {type(e).__name__}: {e}") from e
        packages = diff.merge(results, targets)
        diff.add_skipped(packages, source, targets)
        sides.append(packages)
    report = diff.compare(sides[0], sides[1], targets)
    data: dict[str, Any] = json.loads(diff.json_report(report, targets))
    del data["version"]  # the server's version is in its serverInfo (initialize's, or the result's _meta)
    data["ok"] = not report.worse and not report.new
    data["paths"] = {"old": str(paths[0].resolve()), "new": str(paths[1].resolve())}
    return data


def _existing(raw: str) -> Path:
    """An argument naming a file or directory: a path or a file:// URI. A relative path is looked for in the client's
    roots first (the first root that contains it wins), then in the server's working directory, unless the roots came
    with a 2026 call: then a same-named file elsewhere would be another project's, so it's an error."""
    if _URI_RE.match(raw):
        uri_path = _file_uri_path(raw)
        if uri_path is None:
            raise ToolError(f"{raw}: only file:// URIs for this machine are accepted (or pass an absolute path)")
        path = uri_path
    else:
        path = Path(raw).expanduser()
        if not path.is_absolute() and _ROOTS_ONLY.get():
            path = _in_roots(raw, path)
        elif not path.is_absolute():
            path = next((root / path for root in _ROOTS.get() if (root / path).exists()), path)
    if not path.exists():
        raise ToolError(f"{raw}: no such file or directory (pass an absolute path)")
    return path


def _in_roots(raw: str, path: PurePath) -> Path:
    """A relative path on a 2026 call whose client named its roots: the first root that contains it, never the
    working directory, and never outside the root (no ../ out of it)."""
    if _anchored(path):  # \\foo or C:foo on Windows: joining it to a root would drop the root, or its folder
        raise ToolError(f"{_clip(raw)}: neither relative nor absolute; pass an absolute path")
    for root in _ROOTS.get():
        candidate = root / path
        try:
            found = candidate.resolve().is_relative_to(root.resolve()) and candidate.exists()
        except (OSError, RuntimeError):  # a symlink loop, a name too long (exists() raises that before 3.14), ...
            found = False
        if found:
            return candidate
    searched = _clip(", ".join(str(root) for root in _ROOTS.get()) or "none of them is a local folder")
    raise ToolError(f"{_clip(raw)}: not in any of the client's roots ({searched}); pass an absolute path")


def _anchored(path: PurePath) -> bool:
    """A Windows path with a drive or a root but not both (C:uv.lock, \\uv.lock): not absolute, but not relative
    to a folder either. Never true on POSIX."""
    return not path.is_absolute() and bool(path.drive or path.root)


def _clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _run_cli(argv: list[str], path: Path) -> dict[str, Any]:
    """`wheelgap ARGV -- PATH` with --json in ARGV: its result as the lockfile tools return it."""
    from . import cli

    argv = [*argv, "--", str(path)]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
    except SystemExit as e:  # argparse rejected a value
        code = e.code if isinstance(e.code, int) else 2
    messages = [line.removeprefix("wheelgap: ") for line in err.getvalue().splitlines() if line.strip()]
    notes = [m.removeprefix("note: ") for m in messages if m.startswith("note: ")]
    # SystemExit is a BaseException: without the handler above it would end the server, not just this call.
    if code not in (0, 1):
        errors = [m.removeprefix("error: ") for m in messages if not m.startswith("note: ")]
        raise ToolError("; ".join(errors) or f"wheelgap exited with status {code}")
    data = json.loads(out.getvalue())
    # The CLI leaves the Python out of labels when there's only one; always give it, as check_package does.
    label = {t["label"]: f"{t['python']} {t['platform']}" for t in data["targets"]}
    platform_of = {t["label"]: t["platform"] for t in data["targets"]}
    ignored_on = {i["name"]: set(i["platforms"]) for i in data["ignored_targets"]}
    # Older wheelgap JSON has no "advisory" key; it's always there now, but stay readable either way.
    advisory = [label[cell] for cell in data.get("advisory", [])]
    gaps = []
    for package in data["packages"]:
        cells = [
            label[cell]
            for cell, status in package["results"].items()
            if status in _GAP and platform_of[cell] not in ignored_on.get(package["name"], ())
        ]
        if cells:
            results = {label[cell]: status for cell, status in package["results"].items()}
            on_advisory = [cell for cell in cells if cell in advisory]
            gaps.append(package | {"results": results, "gaps": cells, "advisory_gaps": on_advisory})
    # A bundled build at another version still passes, as in the CLI, but a pin micropip can't install must be said.
    differs = [p for p in data["packages"] if p.get("bundled_version_differs")]
    for p in differs:
        notes.append(
            f"{p['name']}: Pyodide ships {p['bundled']}, not the {p['version']} asked for; it counts as bundled, "
            "but micropip may not install that version"
        )
    if data["skipped"]:
        n = len(data["skipped"])
        notes.append(f"{n} package{'s' if n != 1 else ''} weren't checked (see skipped); ok covers the others only")
    return {
        "path": str(path.resolve()),
        "targets": list(label.values()),
        "packages": data["summary"]["packages"],
        "ok": not gaps,
        # The command line's verdict: gaps on advisory targets (and, with fail-on = "missing", source builds) don't
        # fail it, so this can be true while ok is false.
        "passes": code == 0,
        "advisory": advisory,
        "unknown": sum(1 for g in gaps if Status.UNKNOWN.value in g["results"].values()),
        "bundled_version_differs": [
            {"name": p["name"], "version": p["version"], "bundled": p["bundled"]} for p in differs
        ],
        "gaps": gaps,
        "skipped": data["skipped"],
        "ignored": data["ignored"],
        "ignored_targets": data["ignored_targets"],
        "notes": notes,
    } | ({"blocking_newer_python": data["blocking_newer_python"]} if "blocking_newer_python" in data else {})


def _list_targets(args: dict[str, Any]) -> dict[str, Any]:
    _check_keys("list_targets", args)
    return {
        "platforms": list(PLATFORMS),
        "default": list(DEFAULT_PLATFORMS),
        "groups": {"all": NATIVE_PLATFORMS, "everything": EVERY_NATIVE_PLATFORM, "mobile": MOBILE_PLATFORMS},
        "aliases": dict(ALIASES),
        "defaults": {"glibc": DEFAULT_GLIBC, "musl": DEFAULT_MUSL, "macos": DEFAULT_MACOS},
        "server_python": running_python(),
    }


_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "check_package": _check_package,
    "check_lockfile": _check_lockfile,
    "check_dockerfile": _check_dockerfile,
    "diff_lockfiles": _diff_lockfiles,
    "list_targets": _list_targets,
}


class _Call:
    """A tools/call that is queued or running on the worker."""

    def __init__(
        self,
        token: str | int | None,
        version: str | None = None,
        roots: tuple[Path, ...] | None = None,
        roots_only: bool = False,
    ) -> None:
        self.token = token
        self.version = version  # a 2026 request's own protocol version; None for the handshake's
        self.roots = roots  # a 2026 request's roots (from its inputResponses); None for the handshake's
        self.roots_only = roots_only  # a relative path must be in those roots (the client listed some)
        self.cancelled = False
        self.base = 0  # lookups in fill_all runs that have finished (check_lockfile may run more than one)
        self.total = 0  # lookups in the current fill_all run
        self.sent = -1  # the last progress sent: the spec says each one must be higher


class Server:
    """The protocol state for one stdio connection.

    With `send`, tools/call runs on a worker thread and its result goes out through `send` (handle returns None for
    it); without, handle returns the result itself.
    """

    def __init__(self, send: Callable[[object], None] | None = None) -> None:
        self.protocol_version = PROTOCOL_VERSIONS[-1]
        self._send = send
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wheelgap-mcp") if send else None
        self._calls: dict[str | int, _Call] = {}
        self._lock = threading.Lock()  # guards _calls and each _Call's cancelled flag
        self._client_roots = False  # the client declared the roots capability in initialize
        self._roots_request: str | None = None  # the id of our roots/list request still waiting for its answer
        self._roots_sent = 0
        self._roots: tuple[Path, ...] = ()

    def close(self, wait: bool = True) -> None:
        """Wait for queued and running calls to send their results (or, with wait=False, drop the queued ones)."""
        if not wait:  # Ctrl-C: the running call may still finish its lookups, but nothing more is written
            with self._lock:
                for call in self._calls.values():
                    call.cancelled = True
                self._send = None
        if self._worker is not None:
            self._worker.shutdown(wait=wait, cancel_futures=not wait)

    def handle(self, message: object) -> dict[str, Any] | list[dict[str, Any]] | None:
        """The response to one parsed message (or batch), or None for notifications and calls left to the worker."""
        if isinstance(message, list):  # JSON-RPC batches (only 2025-03-26 allowed them, but they cost nothing)
            if not message:
                return _error(None, INVALID_REQUEST, "empty batch")
            if self._worker is not None and any(
                isinstance(m, dict) and m.get("method") == "tools/call" for m in message
            ):
                # Its reply is one array, so the whole batch waits its turn on the worker.
                self._worker.submit(self._batch_on_worker, message)
                return None
            return self._batch(message)
        return self._one(message)

    def _batch(self, message: list[object]) -> list[dict[str, Any]] | None:
        replies = [r for r in (self._one(m, inline=True) for m in message) if r is not None]
        return replies or None

    def _batch_on_worker(self, message: list[object]) -> None:
        try:
            reply: object = self._batch(message)
        except Exception as e:  # a bug: the client still gets an answer rather than waiting forever
            reply = _error(None, INTERNAL_ERROR, f"internal error: {type(e).__name__}: {e}")
        with self._lock:
            if reply is not None and self._send is not None:
                self._send(reply)

    def _one(self, message: object, inline: bool = False) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(_id(message), INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        params = message.get("params")
        if "id" not in message or ("method" not in message and ("result" in message or "error" in message)):
            # A notification (initialized, cancelled, ...) or a response to us: nothing to send.
            if method == "notifications/cancelled" and isinstance(params, dict):
                self._cancel(params.get("requestId"))
            elif method in ("notifications/initialized", "notifications/roots/list_changed"):
                self._ask_for_roots()
            elif (
                "method" not in message and self._roots_request is not None and message.get("id") == self._roots_request
            ):
                self._roots_answer(message)
            return None
        msg_id = message["id"]
        if not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer")
        if not isinstance(method, str):
            return _error(msg_id, INVALID_REQUEST, "method must be a string")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        meta = params.get("_meta")
        # 2026's per-request keys in _meta mark a 2026 request (a malformed one still gets its -32602); legacy ones may
        # carry _meta too (a progressToken, or 2025-11-25's io.modelcontextprotocol/related-task). initialize is
        # always the handshake, even with those keys, since 2026 has no initialize.
        if method == "server/discover" or (
            method != "initialize" and isinstance(meta, dict) and any(key in meta for key in _ENVELOPE_KEYS)
        ):
            return self._modern(msg_id, method, params, inline)
        if method == "initialize":
            return _reply(msg_id, self._initialize(params))
        if method == "ping":
            return _reply(msg_id, {})
        if method == "tools/list":
            return _reply(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._tools_call(msg_id, params, None, None, inline)
        return _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _modern(self, msg_id: str | int, method: str, params: dict[str, Any], inline: bool) -> dict[str, Any] | None:
        """A 2026-07-28 request: served from its own params, with none of the handshake's state (not its version, nor
        the roots it asked the client for)."""
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        if missing := [key for key in (_VERSION_KEY, _CAPABILITIES_KEY) if key not in meta]:
            return _error(msg_id, INVALID_PARAMS, f"params._meta must have {' and '.join(missing)}")
        version = meta[_VERSION_KEY]
        if not isinstance(version, str):
            return _error(msg_id, INVALID_PARAMS, f"{_VERSION_KEY} must be a string")
        if not isinstance(meta[_CAPABILITIES_KEY], dict):
            return _error(msg_id, INVALID_PARAMS, f"{_CAPABILITIES_KEY} must be an object")
        if version not in MODERN_VERSIONS:
            # Every version served, newest first: a client that also speaks a handshake-era one can fall back to
            # initialize with it, rather than give up.
            supported = [*reversed(MODERN_VERSIONS), *reversed(PROTOCOL_VERSIONS)]
            requested = version[:_REQUESTED_LIMIT]
            if version in PROTOCOL_VERSIONS:
                message = f"Protocol version {requested} is served only through the initialize handshake."
            else:
                message = f"Unsupported protocol version {requested!r}."
            return _error(
                msg_id, UNSUPPORTED_PROTOCOL_VERSION, message, {"supported": supported, "requested": requested}
            )
        if method == "server/discover":
            # Only the versions a request can name in _meta: the handshake-era ones need initialize.
            return _reply(
                msg_id,
                _complete(
                    {
                        "supportedVersions": list(reversed(MODERN_VERSIONS)),
                        "capabilities": CAPABILITIES,
                        "instructions": INSTRUCTIONS,
                    }
                    | _CACHE_HINTS
                ),
            )
        if method == "tools/list":
            return _reply(msg_id, _complete({"tools": TOOLS} | _CACHE_HINTS))
        if method == "tools/call":
            responses = params.get("inputResponses")
            if responses is not None and not isinstance(responses, dict):
                return _error(msg_id, INVALID_PARAMS, "inputResponses must be an object")
            answer = (responses or {}).get(_ROOTS_INPUT)
            if answer is not None:  # the retry after our input_required
                if not isinstance(answer, dict) or not isinstance(answer.get("roots"), list):
                    return _error(msg_id, INVALID_PARAMS, f"inputResponses.{_ROOTS_INPUT} must be a roots/list result")
                roots = _root_paths(answer["roots"])
                roots_only = bool(answer["roots"])  # an empty list: the client has none, so the working directory
            elif meta[_CAPABILITIES_KEY].get("roots") not in (None, False) and _relative_paths(params):
                # The client has roots (even a malformed declaration means it meant to), and a relative path may name
                # a file in them rather than in the working directory: ask for them (a 2026 server can't send
                # roots/list itself), then the client retries. Arguments that would fail anyway fail now instead.
                if problem := _argument_problem(params):
                    return _reply(msg_id, _complete({"content": [{"type": "text", "text": problem}], "isError": True}))
                return _reply(
                    msg_id,
                    _complete({"inputRequests": {_ROOTS_INPUT: {"method": "roots/list"}}}, "input_required"),
                )
            else:
                roots, roots_only = (), False
            return self._tools_call(msg_id, params, version, roots, inline, roots_only=roots_only)
        # ping, logging/setLevel and the rest are gone in 2026, and this server has no prompts or resources.
        return _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _tools_call(
        self,
        msg_id: str | int,
        params: dict[str, Any],
        version: str | None,
        roots: tuple[Path, ...] | None,
        inline: bool,
        *,
        roots_only: bool = False,
    ) -> dict[str, Any] | None:
        """tools/call, on the worker unless inline. version and roots are a 2026 request's own, or None for the
        handshake's."""
        name = params.get("name")
        if not isinstance(name, str) or name not in _HANDLERS:
            return _error(msg_id, INVALID_PARAMS, f"unknown tool: {name!r}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(msg_id, INVALID_PARAMS, "arguments must be an object")
        if inline or self._worker is None:
            return _reply(msg_id, self._call(name, arguments, version, roots, roots_only))
        meta = params.get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        if not isinstance(token, (str, int)) or isinstance(token, bool):
            token = None
        call = _Call(token, version, roots, roots_only)
        with self._lock:
            self._calls[msg_id] = call
        self._worker.submit(self._run, msg_id, call, name, arguments)
        return None

    def _cancel(self, msg_id: object) -> None:
        """notifications/cancelled: an unknown or finished request is ignored, as the spec says."""
        if isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool):
            with self._lock:
                if call := self._calls.get(msg_id):
                    call.cancelled = True

    def _run(self, msg_id: str | int, call: _Call, name: str, arguments: dict[str, Any]) -> None:
        """On the worker: run one call and send its result, unless it was cancelled."""
        if call.cancelled:  # cancelled while it waited for the worker: don't start it
            with self._lock:
                if self._calls.get(msg_id) is call:
                    del self._calls[msg_id]
            return
        try:
            if call.token is None:
                result = self._call(name, arguments, call.version, call.roots, call.roots_only)
            else:
                with pypi.reporting_progress(lambda done, total: self._progress(call, done, total)):
                    result = self._call(name, arguments, call.version, call.roots, call.roots_only)
            reply = _reply(msg_id, result)
        except Exception as e:  # a bug: the client still gets an answer rather than waiting forever
            reply = _error(msg_id, INTERNAL_ERROR, f"internal error: {type(e).__name__}: {e}")
        try:
            with self._lock:  # the lock, so a cancel handled before this point always drops the result
                if not call.cancelled and self._send is not None:
                    self._send(reply)
        finally:
            with self._lock:
                if self._calls.get(msg_id) is call:
                    del self._calls[msg_id]

    def _progress(self, call: _Call, done: int, total: int) -> None:
        """Called by fill_all on the worker. Progress counts up over every fill_all run in the call."""
        if done == 0:  # a new fill_all run: the previous one (if any) finished all its lookups
            call.base += call.total
            call.total = total
        progress, overall = call.base + done, call.base + call.total
        if progress <= call.sent:
            return
        params: dict[str, Any] = {"progressToken": call.token, "progress": progress, "total": overall}
        if (call.version or self.protocol_version) >= _PROGRESS_MESSAGE_FROM:
            params["message"] = f"{progress} of {overall} index lookups done"
        with self._lock:
            if call.cancelled or self._send is None:
                return
            call.sent = progress
            self._send({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})

    def _ask_for_roots(self) -> None:
        """Send roots/list if the client has roots. Its answer arrives on stdin like any other message and is read
        by the same loop (see _roots_answer), so nothing waits for it: a client that never answers can't stall the
        server, and a call handled before the answer resolves relative paths against the working directory, as
        it would without roots. The worker only ever reads the tuple, which is replaced whole."""
        if not self._client_roots or self._send is None:
            return
        self._roots_sent += 1
        self._roots_request = f"wheelgap-roots-{self._roots_sent}"
        self._send({"jsonrpc": "2.0", "id": self._roots_request, "method": "roots/list"})

    def _roots_answer(self, message: dict[str, Any]) -> None:
        self._roots_request = None
        result = message.get("result")
        self._roots = _root_paths(result.get("roots") if isinstance(result, dict) else None)

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        capabilities = params.get("capabilities")
        self._client_roots = isinstance(capabilities, dict) and isinstance(capabilities.get("roots"), dict)
        asked = params.get("protocolVersion")
        self.protocol_version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        version: str | None = None,
        roots: tuple[Path, ...] | None = None,
        roots_only: bool = False,
    ) -> dict[str, Any]:
        """A tool's result. version and roots are a 2026 request's own (with resultType and serverInfo in the
        result), or None for the handshake's."""
        if version is None:
            return self._tool_result(name, arguments, self.protocol_version, self._roots, False)
        return _complete(self._tool_result(name, arguments, version, roots or (), roots_only))

    def _tool_result(
        self, name: str, arguments: dict[str, Any], version: str, roots: tuple[Path, ...], roots_only: bool
    ) -> dict[str, Any]:
        token, only_token = _ROOTS.set(roots), _ROOTS_ONLY.set(roots_only)
        try:
            # wheelgap's own messages go to stderr already; this keeps any stray print off the protocol stream.
            with contextlib.redirect_stdout(sys.stderr):
                data = _HANDLERS[name](arguments)
            text = json.dumps(data, indent=1)
        except (ToolError, TargetError, SourceError) as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # a bug: report it to the model rather than end the connection
            return {"content": [{"type": "text", "text": f"internal error: {type(e).__name__}: {e}"}], "isError": True}
        finally:
            _ROOTS.reset(token)
            _ROOTS_ONLY.reset(only_token)
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
        if version >= _STRUCTURED_FROM:
            result["structuredContent"] = data
        return result


def _id(message: object) -> Any:
    msg_id = message.get("id") if isinstance(message, dict) else None
    return msg_id if isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool) else None


def _reply(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _complete(result: dict[str, Any], result_type: str = "complete") -> dict[str, Any]:
    """A 2026-07-28 result: every one says its resultType, and the server names itself in each one's _meta."""
    return {"resultType": result_type, **result, "_meta": {_SERVER_INFO_KEY: SERVER_INFO}}


# The arguments each tool resolves with _existing (pyodide_lock: on 2026 roots calls), so against the client's
# roots when relative.
_PATH_ARGUMENTS = {
    "check_package": ("pyodide_lock",),
    "check_lockfile": ("path", "pyodide_lock"),
    "check_dockerfile": ("dockerfile", "path"),
    "diff_lockfiles": ("old", "new"),
}


def _relative_paths(params: dict[str, Any]) -> bool:
    """Whether a tools/call passes a relative path that _existing would look for in the client's roots."""
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return False
    tool = params.get("name")
    values = [arguments.get(key) for key in _PATH_ARGUMENTS.get(tool, ())] if isinstance(tool, str) else []
    return any(isinstance(v, str) and v.strip() and not _URI_RE.match(v) and _is_relative(v) for v in values)


def _argument_problem(params: dict[str, Any]) -> str | None:
    """What a tool would reject in its arguments' names and types (the checks that need no files or lookups), so a
    call that can't succeed isn't sent round for roots first."""
    name, arguments = params.get("name"), params.get("arguments")
    if not isinstance(name, str) or name not in _HANDLERS or not isinstance(arguments, dict):
        return None  # _tools_call's protocol errors
    try:
        _check_keys(name, arguments)
        schema = next(t for t in TOOLS if t["name"] == name)["inputSchema"]
        for key in schema.get("required", []):
            _string(arguments, key, required=True)
        for key, prop in schema["properties"].items():
            if prop["type"] == "string":
                _string(arguments, key)
            elif prop["type"] == "boolean" and arguments.get(key) is not None:
                if not isinstance(arguments[key], bool):
                    raise ToolError(f"'{key}' must be true or false")
            elif prop["type"] == "array":
                _platforms(arguments)
    except ToolError as e:
        return str(e)
    return None


def _is_relative(raw: str) -> bool:
    try:
        return not Path(raw).expanduser().is_absolute()
    except RuntimeError:  # ~user with no such user: the tool reports it
        return False


def _root_paths(roots: object) -> tuple[Path, ...]:
    """The local folders in a roots/list result's roots (other URIs and malformed entries are left out)."""
    paths = []
    for root in roots if isinstance(roots, list) else ():
        uri = root.get("uri") if isinstance(root, dict) else None
        path = _file_uri_path(uri) if isinstance(uri, str) else None
        if path is not None and path.is_absolute():  # file:, file://localhost or file:../x would be the cwd's
            paths.append(path)
    return tuple(paths)


def _error(msg_id: Any, code: int, message: str, data: object = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def serve(stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Answer messages from stdin until it closes, then wait for the calls still queued or running."""
    write_lock = threading.Lock()

    def send(reply: object) -> None:
        # ensure_ascii keeps every message on one line whatever the package metadata contains.
        data = json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
        with write_lock:  # the worker writes too: a line is never split by another
            stdout.write(data)
            stdout.flush()

    server = Server(send)
    finished = False
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8-sig"))
            except (ValueError, RecursionError) as e:  # bad UTF-8 or JSON, an over-long integer, absurd nesting
                reply: object = _error(None, PARSE_ERROR, f"parse error: {type(e).__name__}")
            else:
                try:
                    reply = server.handle(message)
                except Exception as e:  # never let one bad message end the connection
                    reply = _error(_id(message), INTERNAL_ERROR, f"internal error: {type(e).__name__}: {e}")
            if reply is not None:
                send(reply)
        finished = True
    finally:
        server.close(wait=finished)  # on Ctrl-C, don't start the calls still queued


# For `wheelgap mcp`, which always starts the server: how to check what `wheelgap mcp` used to mean.
_SUBCOMMAND_HINT = (
    "To check a folder named mcp, run `wheelgap ./mcp`; to check the PyPI package mcp, pin it: `wheelgap mcp==VERSION`."
)


def main(argv: list[str] | None = None, prog: str = "wheelgap-mcp") -> int:
    args = sys.argv[1:] if argv is None else argv
    subcommand = prog == "wheelgap mcp"
    if args in (["--version"], ["-V"]):
        print(f"wheelgap-mcp {__version__}")
        return 0
    if args:
        print(
            f"usage: {prog}\n\nAn MCP server for wheelgap over stdio. Add it to your agent's MCP settings with "
            "the command `uvx --from wheelgap wheelgap-mcp` (or `uvx wheelgap mcp`)."
            + (f"\n\n{_SUBCOMMAND_HINT}" if subcommand else ""),
            file=sys.stderr,
        )
        return 0 if args in (["-h"], ["--help"]) else 2
    if subcommand and sys.stdin is not None and sys.stdin.isatty():
        print(f"wheelgap mcp: waiting for an MCP client on stdin (Ctrl-C to quit). {_SUBCOMMAND_HINT}", file=sys.stderr)
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
