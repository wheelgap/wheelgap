"""A small on-disk cache of pinned index lookups (/<name>/<version>/json), so a repeat run needn't ask again,
of --deps' wheel metadata files (each release's Requires-Dist), of --conda-forge lookups (a version's conda-forge
builds, cut down to what matching needs), of --piwheels lookups (a version's piwheels wheel filenames and the apt
packages each needs), of --notes lookups (the coverage tracker's notes and build lab results for
a package), of --build-info answers (what a release's sdist says about its build), and of --fix's gap wheel list (the
whole document, once gapwheels.parse has accepted it).

A cache that can't be read or written is only ever a miss: it never fails a run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MAX_AGE = 24 * 60 * 60  # seconds: new wheels for an existing release show up within a day
GAP_WHEELS_MAX_AGE = 60 * 60  # seconds: new gap wheels are published often, but an hour avoids CI stampedes
GAP_WHEELS_RECHECK_AGE = 5 * 60  # seconds: don't re-fetch an empty-fill cached list fetched minutes ago
PRUNE_EVERY = 24 * 60 * 60  # seconds between two prunes of expired entries
# The only files prune() and clear() ever delete: entries (_path's names) and leftovers of an interrupted save.
_ENTRY = re.compile(r"[0-9a-f]{64}\.json", re.ASCII)
_LEFTOVER = re.compile(r"\.tmp-[^/\\]*\.json")


def _now() -> float:
    return time.time()


def cache_dir() -> Path:
    """The platform's cache folder, as pip and uv choose it (without platformdirs)."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        base = local if os.path.isabs(local) else str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "")
        base = xdg if os.path.isabs(xdg) else str(Path.home() / ".cache")  # the XDG spec ignores relative paths
    return Path(base) / "wheelgap" / "pinned-v2"


def _path(index: str, name: str, version: str) -> Path:
    # Credentials in the index URL (WHEELGAP_INDEX_URL=https://user:token@host/pypi) are left out of the key,
    # and what's left is hashed, so no URL is written in clear text.
    parts = urlsplit(index)
    index = urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
    digest = hashlib.sha256(f"{index}\n{name}\n{version}".encode()).hexdigest()
    return cache_dir() / f"{digest}.json"


def _load_entry(
    index: str, name: str, version: str, key: str, max_age: float = MAX_AGE
) -> tuple[object, float] | tuple[None, None]:
    """(value, saved_timestamp) of a cache entry, or (None, None): not cached, older than max_age, or unreadable."""
    try:
        entry = json.loads(_path(index, name, version).read_text(encoding="utf-8"))
        saved = entry["saved"]
        if isinstance(saved, bool) or not isinstance(saved, int | float) or not 0 <= _now() - saved < max_age:
            return None, None
        return entry[key], float(saved)
    except Exception:  # missing, corrupt or unreadable: a miss
        return None, None


def _load(index: str, name: str, version: str, key: str, max_age: float = MAX_AGE) -> object:
    """The `key` value of a cache entry, or None: not cached, older than max_age, or unreadable."""
    value, _ = _load_entry(index, name, version, key, max_age=max_age)
    return value


def load_files(index: str, name: str, version: str) -> list[dict] | None:
    """The cached file list of a pinned release, or None: not cached, older than MAX_AGE, or unreadable."""
    files = _load(index, name, version, "files")
    if isinstance(files, list) and all(isinstance(f, dict) and isinstance(f.get("filename"), str) for f in files):
        return files
    return None


def save_files(index: str, name: str, version: str, files: list[dict]) -> None:
    """Store a pinned release's file list. Only `filename`, `yanked`, `requires_python` and the sha256 digest are
    kept: they're all fill() reads (the digest for a requirement's --hash), and a file's URL (which a private mirror
    may sign) never reaches the disk."""
    kept: list[dict] = []
    for f in files:
        entry = {k: f[k] for k in ("filename", "yanked", "requires_python") if k in f}
        digests = f.get("digests")
        if isinstance(digests, dict) and isinstance(digests.get("sha256"), str):
            entry["digests"] = {"sha256": digests["sha256"]}
        kept.append(entry)
    _save(index, name, version, "files", kept)


def _info_key(index: str) -> str:
    return f"info-v1 {index}"


def load_project_urls(index: str, name: str, version: str) -> tuple[dict[str, str] | None, str | None]:
    """The cached project metadata URLs of a pinned release, or (None, None)."""
    info = _load(_info_key(index), name, version, "info")
    if isinstance(info, dict):
        urls = info.get("project_urls")
        home = info.get("home_page")
        project_urls = (
            {k: v for k, v in urls.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(urls, dict)
            else None
        )
        home_page = home if isinstance(home, str) else None
        return project_urls, home_page
    return None, None


def save_project_urls(
    index: str, name: str, version: str, project_urls: dict[str, str] | None, home_page: str | None
) -> None:
    """Store a pinned release's project metadata URLs."""
    if project_urls is not None or home_page is not None:
        _save(_info_key(index), name, version, "info", {"project_urls": project_urls, "home_page": home_page})


# --conda-forge entries: keyed apart from PyPI's, and versioned, since what's kept may change.
CONDA_FORGE_KEY = "condaforge-v3 https://api.anaconda.org/package/conda-forge"
TOO_LARGE = "too large"  # the answer was over condaforge.MAX_BYTES


def _str_or_none(value: object) -> bool:
    return value is None or isinstance(value, str)


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _is_build(b: object) -> bool:
    """A condaforge.Build, as condaforge._build makes it."""
    return (
        isinstance(b, dict)
        and set(b) == {"subdir", "abi", "python", "os", "glibc", "osx", "unknown"}
        and isinstance(b["subdir"], str)
        and _str_or_none(b["abi"])
        and (b["python"] is None or _strings(b["python"]))
        and _strings(b["os"])
        and _str_or_none(b["glibc"])
        and _str_or_none(b["osx"])
        and isinstance(b["unknown"], bool)
    )


def load_builds(name: str, version: str) -> list[dict] | str | None:
    """--conda-forge: the cached builds of a version ([] for none), TOO_LARGE, or None: not cached, older than
    MAX_AGE, or unreadable."""
    builds = _load(CONDA_FORGE_KEY, name, version, "builds")
    if builds == TOO_LARGE:
        return TOO_LARGE
    if isinstance(builds, list) and all(_is_build(b) for b in builds):
        return builds
    return None


def save_builds(name: str, version: str, builds: list[dict] | str) -> None:
    _save(CONDA_FORGE_KEY, name, version, "builds", builds)


# --piwheels entries: keyed apart from PyPI's, and versioned.
PIWHEELS_KEY = "piwheels-v1 https://www.piwheels.org/project"


def load_piwheels(name: str, version: str) -> dict[str, list[str]] | str | None:
    """--piwheels: the cached wheels of a version (filename -> apt packages, {} for none), TOO_LARGE, or None: not
    cached, older than MAX_AGE, or unreadable."""
    wheels = _load(PIWHEELS_KEY, name, version, "wheels")
    if wheels == TOO_LARGE:
        return TOO_LARGE
    if isinstance(wheels, dict) and all(isinstance(w, str) and _strings(apt) for w, apt in wheels.items()):
        return wheels
    return None


def save_piwheels(name: str, version: str, wheels: dict[str, list[str]] | str) -> None:
    _save(PIWHEELS_KEY, name, version, "wheels", wheels)


# --build-info entries: keyed apart from the file lists of the same release, and by the sdist's sha256 as a lock gives
# it ("" when the index gives the URL), so one lockfile can't answer for another's copy of a release.
_BUILD_FIELDS: dict[str, type | tuple[type, ...]] = {
    "requires": list,
    "backend": (str, type(None)),
    "backend_path": bool,
    "build_script": bool,
}


def _build_key(index: str, sha256: str) -> str:
    return f"build-v2 {sha256} {index}"


def load_build(index: str, name: str, version: str, sha256: str) -> dict | None:
    """A release's cached build info: {"answer": None} when its sdist gives no answer, else {"answer": {"requires",
    "backend", "backend_path", "build_script"}} (see build.py). None for a miss."""
    entry = _load(_build_key(index, sha256), name, version, "build")
    if not isinstance(entry, dict) or set(entry) != {"answer"}:
        return None
    answer = entry["answer"]
    if answer is None:
        return {"answer": None}
    if (
        isinstance(answer, dict)
        and set(answer) == set(_BUILD_FIELDS)
        and all(isinstance(answer[k], t) for k, t in _BUILD_FIELDS.items())
        and all(isinstance(r, str) for r in answer["requires"])
    ):
        return {"answer": answer}
    return None


def save_build(index: str, name: str, version: str, sha256: str, answer: dict | None) -> None:
    """Store what a release's sdist says about its build, or None when it gives no answer for good."""
    _save(_build_key(index, sha256), name, version, "build", {"answer": answer})


def load_gap_wheels(url: str, max_age: float = GAP_WHEELS_MAX_AGE) -> dict | None:
    """--fix: the gap wheel list read from `url` (pypi.GAP_WHEELS_URL), or None for a miss. gapwheels.parse checks it
    again."""
    doc = _load(f"gap-wheels-v1 {url}", "list", "schema-1", "document", max_age=max_age)
    return doc if isinstance(doc, dict) and doc.get("schema") == 1 and isinstance(doc.get("wheels"), list) else None


def load_gap_wheels_with_time(url: str, max_age: float = GAP_WHEELS_MAX_AGE) -> tuple[dict | None, float | None]:
    """--fix: (gap wheel list, saved_timestamp) read from `url`, or (None, None)."""
    doc, saved = _load_entry(f"gap-wheels-v1 {url}", "list", "schema-1", "document", max_age=max_age)
    if isinstance(doc, dict) and doc.get("schema") == 1 and isinstance(doc.get("wheels"), list) and saved is not None:
        return doc, saved
    return None, None


def save_gap_wheels(url: str, doc: dict) -> None:
    """Store the gap wheel list read from `url`, once gapwheels.parse has accepted it (a failed fetch saves nothing,
    so it never replaces the last good list)."""
    _save(f"gap-wheels-v1 {url}", "list", "schema-1", "document", doc)


def load_notes(source: str, name: str) -> list | None:
    """--notes: a package's cached tracker notes (see notes.py) from `source`, or None. An empty list is a cached
    404. notes.py checks each note again."""
    notes = _load(source, name, "notes", "notes")  # "notes" is no version, so it can't collide with a release
    return notes if isinstance(notes, list) else None


def save_notes(source: str, name: str, notes: list[dict]) -> None:
    """Store a package's tracker notes, as notes.py checked them."""
    _save(source, name, "notes", "notes", notes)


def load_lab(source: str, name: str) -> list | None:
    """--notes: a package's cached build lab results (see notes.py) from `source`, or None. An empty list is a cached
    404, or a file with none. notes.py checks each result again."""
    builds = _load(source, name, "build-lab-v1", "builds")  # versioned, so a later format is looked up again
    return builds if isinstance(builds, list) else None


def save_lab(source: str, name: str, builds: list[dict]) -> None:
    """Store a package's build lab results, as notes.py checked them."""
    _save(source, name, "build-lab-v1", "builds", builds)


def _metadata_key(index: str) -> str:
    return f"metadata-v1 {index}"


def load_metadata(index: str, url: str) -> list[str] | None:
    """--deps: the cached Requires-Dist of the wheel metadata file at `url` (PEP 658), or None for a miss. An uploaded
    file's metadata never changes, so the URL is the whole key; it is hashed, like every key, never written out."""
    requires = _load(_metadata_key(index), url, "metadata", "requires")
    if isinstance(requires, list) and all(isinstance(r, str) for r in requires):
        return requires
    return None


def save_metadata(index: str, url: str, requires: list[str]) -> None:
    """Store a wheel metadata file's Requires-Dist. Only an answer is kept: a failed fetch is asked again next run."""
    _save(_metadata_key(index), url, "metadata", "requires", requires)


def _save(index: str, name: str, version: str, key: str, value: object) -> None:
    try:
        path = _path(index, name, version)
        path.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # private to the user (not on Windows)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        # Written next to the target, then renamed over it, so a reader never sees half a file.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"saved": _now(), key: value}, f)
            os.utime(tmp, (_now(), _now()))  # prune() reads the age from the mtime
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception:  # read-only home, full disk, a race on Windows...: the next run just looks it up again
        pass
    prune()


def _own_folder(folder: Path) -> bool:
    """The cache folder is a real directory, not a symlink or Windows junction to somewhere else."""
    try:
        info = os.lstat(folder)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    # Python 3.11 on Windows reports a junction as a plain folder; its reparse tag gives it away.
    tag = getattr(info, "st_reparse_tag", 0)  # Windows only
    if tag and tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1):
        return False
    isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+
    return not (isjunction is not None and isjunction(folder))


def _own_files(folder: Path) -> Iterator[os.DirEntry[str]]:
    """Regular files wheelgap wrote in the folder: entries and save leftovers. Links are never followed."""
    with os.scandir(folder) as entries:
        for entry in entries:
            if (_ENTRY.fullmatch(entry.name) or _LEFTOVER.fullmatch(entry.name)) and entry.is_file(
                follow_symlinks=False
            ):
                yield entry


def _clear_folder(folder: Path, *, best_effort: bool = False) -> int:
    """Delete wheelgap's own files and remove an empty cache folder, never following a link. With
    best_effort, a file that can't be deleted is skipped instead of stopping the rest."""
    if not _own_folder(folder):
        return 0
    skip = OSError if best_effort else FileNotFoundError  # another run may have removed it
    count = 0
    for entry in _own_files(folder):
        with contextlib.suppress(skip):
            os.unlink(entry.path)
            count += bool(_ENTRY.fullmatch(entry.name))
    with contextlib.suppress(skip):
        (folder / "last-prune").unlink()  # a link here is removed itself, not its target
    with contextlib.suppress(OSError):  # not empty: another run saved meanwhile, or someone else's file
        folder.rmdir()
    return count


def prune(max_age: float = MAX_AGE) -> None:
    """Delete current entries older than max_age, at most once a day, and all obsolete pinned-v1 entries.
    A `last-prune` marker in the current folder holds the time of its last run in its mtime. Only wheelgap's
    own files are touched, never through a link, and nothing here ever fails a run."""
    try:
        folder = cache_dir()
        with contextlib.suppress(OSError):
            _clear_folder(folder.parent / "pinned-v1", best_effort=True)  # obsolete: entries of any age
        if not _own_folder(folder):
            return
        marker = folder / "last-prune"
        if marker.is_symlink():
            return
        with contextlib.suppress(FileNotFoundError):
            if 0 <= _now() - marker.stat().st_mtime < PRUNE_EVERY:
                return
        marker.touch(mode=0o600)
        os.utime(marker, (_now(), _now()))
        for entry in _own_files(folder):
            with contextlib.suppress(OSError):  # another run may have replaced or removed it
                if _now() - entry.stat(follow_symlinks=False).st_mtime >= max_age:
                    os.unlink(entry.path)
    except Exception:  # no folder yet, one we can't list, no home folder at all...: try again next time
        pass


def clear() -> tuple[Path, int]:
    """--clear-cache: delete wheelgap's files in the current and obsolete pinned-v1 cache folders.
    Returns the current folder and the total number of entries removed. Links are never followed."""
    folder = cache_dir()
    if (folder.exists() or folder.is_symlink()) and not _own_folder(folder):
        raise OSError(f"{folder} is a link or not a folder, so wheelgap won't clear it; remove it yourself")
    count = _clear_folder(folder)
    with contextlib.suppress(OSError):  # an obsolete folder we can't empty never fails --clear-cache
        count += _clear_folder(folder.parent / "pinned-v1", best_effort=True)
    return folder, count
