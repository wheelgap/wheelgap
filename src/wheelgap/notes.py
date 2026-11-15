"""--explain --notes: the coverage tracker's notes for a package with a gap, such as "use `psycopg2-binary`".

Notes record what a scan can't know: a binary extra, a system library to install, a conda-forge build. The tracker
publishes them in each package's file, https://wheelgap.github.io/tracker/data/packages/<name>.json (its format:
https://github.com/wheelgap/tracker/blob/main/notes/README.md). They come from a public repository, so they're
untrusted: a note that isn't exactly in the published format is dropped whole, never shown in part.

Looking a package up sends its name to wheelgap.github.io, so the CLI only asks for packages that are positively PyPI's
(see may_send), only with --notes (or notes = true), and never with --offline, --index-url or a WHEELGAP_INDEX_URL
mirror (origin.other_index). The lookups are
optional extras, so they are bounded: one attempt each, a short timeout, a deadline for them all, and none after
the first connection failure.
"""

from __future__ import annotations

import datetime as dt
import http.client
import queue
import re
import threading
import time
import urllib.error
from collections.abc import Collection
from dataclasses import dataclass
from urllib.parse import urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from . import cache, origin, pypi
from .sources import Package, Source
from .targets import Target

TRACKER = "https://wheelgap.github.io/tracker/data/packages"
SCHEMA = 1  # of the tracker's package files: it goes up if a field changes meaning or goes away
TIMEOUT = 10.0  # seconds, for each request (one attempt, no retries)
DEADLINE = 15.0  # seconds, for all the lookups of a run
WORKERS = 16
# The format, as notes/README.md and scripts/check_notes.py in https://github.com/wheelgap/tracker define it.
# The platforms the tracker scans: a note names some of these, or ["all"], which means these and no other target.
PLATFORMS = frozenset(
    {
        "linux-x86_64",
        "linux-aarch64",
        "musl-x86_64",
        "musl-aarch64",
        "macos-x86_64",
        "macos-arm64",
        "win-amd64",
        "win-arm64",
        "pyodide",
    }
)
KINDS = frozenset({"workaround", "alternative", "upstream-issue", "other-source"})
MAX_TEXT = 500  # shorter than this
MAX_LINKS = 5
MAX_LINK = 300  # characters
MAX_NOTES = 20  # per package; any more are dropped
# A versions clause: ==, !=, <=, >=, < or > and a plain release, with .* only after == or != (check_notes.py's
# SPEC_CLAUSE_RE). No ~=, ===, epochs, pre-, post-, dev or local versions.
_CLAUSE = re.compile(r"\s*(?:(?:==|!=)\s*\d+(?:\.\d+)*(?:\.\*)?|(?:<=|>=|<|>)\s*\d+(?:\.\d+)*)\s*", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
# A normalized name is also a safe path segment: no slashes, dots or leading dashes.
_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*", re.ASCII)
# GitHub Actions reads a line as a workflow command when it starts with "::" (after whitespace) and, in the legacy
# form, when it has "##[" anywhere. A note with either is dropped, whatever prints it: a word starting with "::" in
# its text (C++'s std::vector is fine), or "##[" in its text or a link.
_COMMAND = re.compile(r"(?<![\w:])::")
_PYTHON = re.compile(r"^3\.\d+t?$", re.ASCII)
_KEYS = frozenset({"platforms", "kind", "text", "links", "versions", "updated"})


@dataclass(frozen=True)
class Note:
    platforms: tuple[str, ...]  # wheelgap platform names, or ("all",)
    kind: str  # workaround, alternative, upstream-issue, other-source
    text: str
    links: tuple[str, ...]
    versions: str | None  # a version specifier, or None for every release
    updated: str  # YYYY-MM-DD
    python: tuple[str, ...] | None = None

    def covers(self, target: Target) -> bool:
        if self.python is not None and target.python not in self.python:
            return False
        return target.platform in (PLATFORMS if self.platforms == ("all",) else self.platforms)

    def applies_to(self, version: str | None) -> bool:
        """Whether the note is about this release. A note limited to some releases is shown only when the version
        is known and is a plain release the specifier admits, as the tracker matches it: never on a guess."""
        if self.versions is None:
            return True
        if version is None or not self.versions.strip():
            return False
        try:
            v = Version(version)
            if v.epoch or v.pre or v.post is not None or v.dev is not None or v.local:
                return False  # the tracker hides such notes for these releases too
            return SpecifierSet(self.versions).contains(v)
        except (InvalidVersion, InvalidSpecifier):
            return False

    def as_json(self) -> dict:
        out: dict[str, object] = {
            "platforms": list(self.platforms),
            "kind": self.kind,
            "text": self.text,
            "links": list(self.links),
            "versions": self.versions,
            "updated": self.updated,
        }
        if self.python is not None:
            out["python"] = list(self.python)
        return out


def _line(value: object) -> bool:
    """A non-empty string with no line breaks, control or format characters (which could rewrite a terminal)."""
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def _link(value: object) -> bool:
    """An https:// URL with a host, and no whitespace, control or format characters or user@ part."""
    if not isinstance(value, str) or not _line(value) or " " in value or len(value) > MAX_LINK or "##[" in value:
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme == "https" and bool(parts.hostname) and "@" not in parts.netloc
    except ValueError:
        return False


def _date(value: object) -> bool:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:  # 2026-02-30
        return False
    return True


def _note(raw: object) -> Note | None:
    """A published note, or None if it isn't exactly in the format."""
    if not isinstance(raw, dict) or set(raw) not in (_KEYS, _KEYS | {"python"}):
        return None
    platforms, kind, text, links, versions = raw["platforms"], raw["kind"], raw["text"], raw["links"], raw["versions"]
    if not isinstance(platforms, list) or not platforms or len(set(map(str, platforms))) != len(platforms):
        return None
    if platforms != ["all"] and not all(isinstance(p, str) and p in PLATFORMS for p in platforms):
        return None
    if not isinstance(kind, str) or kind not in KINDS or not _date(raw["updated"]):
        return None
    if not isinstance(text, str) or not _line(text) or len(text) >= MAX_TEXT or "##[" in text or _COMMAND.search(text):
        return None
    if not isinstance(links, list) or not 0 < len(links) <= MAX_LINKS or len(set(map(str, links))) != len(links):
        return None
    if not all(_link(link) for link in links):
        return None
    if versions is not None and not (
        isinstance(versions, str) and versions.strip() and all(_CLAUSE.fullmatch(c) for c in versions.split(","))
    ):
        return None
    py: tuple[str, ...] | None = None
    if "python" in raw:
        raw_py = raw["python"]
        if (
            not isinstance(raw_py, list)
            or not raw_py
            or not all(isinstance(p, str) and _PYTHON.fullmatch(p) for p in raw_py)
            or len(set(raw_py)) != len(raw_py)
        ):
            return None
        py = tuple(raw_py)
    return Note(tuple(platforms), kind, text, tuple(links), versions, raw["updated"], py)


def parse(data: object, name: str) -> list[Note] | None:
    """The notes in a package file, or None if the file isn't one for `name` in a schema this version reads."""
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != SCHEMA:
        return None  # not true, which == 1 in Python
    if data.get("name") != name:
        return None
    raw = data.get("notes", [])  # files published before notes existed have none
    if not isinstance(raw, list):
        return None
    return [n for n in map(_note, raw[:MAX_NOTES]) if n is not None]


def may_send(pkg: Package, source: Source, private: Collection[str] = frozenset()) -> bool:
    """Whether the package's name may go to the tracker: origin.known_pypi (the rule --conda-forge uses too), with
    one narrowing. known_pypi trusts a pypi.org registry from any lock, but notes take it only from uv.lock and
    pylock.toml; from another lock (a poetry.lock legacy source of pypi.org, say), only the found-on-pypi.org route
    counts, which those locks never satisfy. The caller has already ruled out another index (origin.other_index).
    `private`: the names another input of the run places elsewhere (origin.private_names), which never go."""
    if not origin.known_pypi(pkg, source, private=private):
        return False
    if source.kind in ("uv", "pylock") or pkg.registry is None:
        return True
    return pkg.found_on_pypi and source.pypi_only and origin.index_env() is None


_CONNECTION = (urllib.error.URLError, OSError, http.client.HTTPException)  # HTTPError is handled before these


TIMED_OUT, UNREACHABLE = "timed out", "unreachable"


def _timed_out(e: BaseException) -> bool:
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


def lookup(
    names: list[str], read_cache: bool = True, write_cache: bool = True
) -> tuple[dict[str, list[Note]], str | None]:
    """Each name's notes, keyed by name, for the names that could be looked up (a 404 is no notes); and why the
    lookups were cut short, if they were: TIMED_OUT (the deadline, or a request's timeout) or UNREACHABLE (no
    connection: DNS, TLS, refused). Nothing here raises: a lookup that fails just leaves its name out."""
    found: dict[str, list[Note]] = {}
    todo = []
    for name in dict.fromkeys(names):
        if not _NAME.fullmatch(name):
            continue
        cached = cache.load_notes(TRACKER, name) if read_cache else None
        if cached is not None:
            found[name] = [n for n in map(_note, cached[:MAX_NOTES]) if n is not None]  # checked again
        else:
            todo.append(name)
    if not todo:
        return found, None

    work: queue.SimpleQueue[str] = queue.SimpleQueue()
    for name in todo:
        work.put(name)
    done: queue.SimpleQueue[tuple[str, object, str | None]] = queue.SimpleQueue()
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                name = work.get_nowait()
            except queue.Empty:
                return
            try:
                data: object = pypi.fetch_json(f"{TRACKER}/{name}.json", timeout=TIMEOUT, attempts=1)
            except urllib.error.HTTPError as e:
                data = {"schema": SCHEMA, "name": name, "notes": []} if e.code == 404 else None
            except _CONNECTION as e:
                stop.set()  # the tracker can't be reached (a firewalled runner): don't try the rest
                done.put((name, None, TIMED_OUT if _timed_out(e) else UNREACHABLE))
                return
            except Exception:  # a body that isn't JSON, and the like
                data = None
            done.put((name, data, None))

    # Daemon threads, so a request still waiting on its timeout can't hold up the end of the run.
    for _ in range(min(WORKERS, len(todo))):
        threading.Thread(target=worker, daemon=True).start()
    deadline = time.monotonic() + DEADLINE
    cut: str | None = None
    for _ in todo:
        try:
            name, data, failed = done.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            cut = TIMED_OUT
            break
        if failed:
            cut = failed
            break
        notes = parse(data, name)
        if notes is not None:
            found[name] = notes
            if write_cache:
                cache.save_notes(TRACKER, name, [n.as_json() for n in notes])
    stop.set()
    return found, cut
