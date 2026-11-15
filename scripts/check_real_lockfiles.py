"""Check that wheelgap still reads real projects' lockfiles (#227).

    uv run python scripts/check_real_lockfiles.py                # every project's current lockfile
    uv run python scripts/check_real_lockfiles.py pluggy cleo    # just these (the folder names in sources.toml)
    uv run python scripts/check_real_lockfiles.py --pinned       # the copies in tests/fixtures/real/, offline

tests/fixtures/real/sources.toml lists real lockfiles from open-source projects: two uv.lock, two poetry.lock, a
pdm.lock, a pylock.toml and a Pipfile.lock. By default this downloads each project's current file from its default
branch (raw.githubusercontent.com, no token; a dev script, so it uses urllib rather than wheelgap's pypi.fetch_json)
and reads it with wheelgap. tests/test_real_lockfiles.py runs the same checks on the pinned copies, offline, and
also pins how many cells are `n/a` and how many markers were read, which this can't know for a file it hasn't seen.

What it finds falls in three kinds:

- **misread**: wheelgap reads the file in a way that would give a wrong or `unknown` answer.
  - It can't read it, or reads no packages.
  - A package has a parse error, or a version, marker or requires-python that doesn't parse.
  - In a lock that records filenames (all but Pipfile.lock), a package has no files recorded, a file that's neither
    a wheel nor an sdist, or a wheel name that doesn't parse.
  - The file has a different number of packages, files, markers, dependencies or dependency versions than
    wheelgap read. These are counted straight from the TOML or JSON, under the old and likely new key names
    (`marker` and `markers`, `wheels` and `wheel`), so a renamed key shows up here.
  - Poetry's lock-version says the lock has a dependency graph (before 2.1) and wheelgap didn't use it, or the
    other way round.
  - Far fewer wheels per package than in the pinned copy.
  - A package is `n/a` on every target (CPython 3.8 to the newest, on every `-t all` platform) and nothing on its
    way in has a marker: its markers or the dependency graph were misread.
  - A package is `unknown` on some target.
- **upstream changed**: the project changed in a way the corpus should follow, so refresh it (AGENTS.md says how).
  - The file is gone (404).
  - A format version the tests don't cover yet (KNOWN_FORMATS).
  - A package wheelgap skips for a reason other than being a local project (a git or URL dependency, say), or one
    with an empty list of files.
- **warning** (doesn't fail): a package is `n/a` on every target, but only through markers, e.g. on
  `implementation_name == 'pypy'` or an old Python: probably right, but worth a look.

Each lockfile gets a line, `ok`, `warn` or `FAIL`, with its format version, and each finding is printed under it.
Text from the lockfile is printed with repr() and without backticks, so it can't start a line or close the job
summary's code block. The exit code is 1 if anything failed. .github/workflows/lockfile-corpus.yml runs this weekly.
It needs the network (except with --pinned), so the tests never run its download.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from packaging.markers import InvalidMarker, Marker
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

import wheelgap
from wheelgap.check import SDIST_SUFFIXES, Status, check
from wheelgap.sources import Edge, Package, Source, load_file

CORPUS = Path(__file__).parents[1] / "tests" / "fixtures" / "real"
MANIFEST = CORPUS / "sources.toml"
# Every reader except Pipfile.lock's gets each package's filenames from the lock, so needs no index lookup.
RECORDS_FILES = {"uv", "pylock", "poetry", "pdm"}
# Skips that mean "the project itself, or a path in its repo".
LOCAL_SKIPS = {"editable", "virtual", "directory", "path"}
PYTHONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "3.14t", "next"]
TARGETS = wheelgap.make_targets(PYTHONS, ["all"])
# Format versions some test reads (here or in tests/fixtures/). A new one needs a look at sources.py, then a line here.
KNOWN_FORMATS: dict[str, dict[str, set]] = {
    "uv": {"version": {1}, "revision": {None, 3, 5}},
    "poetry": {"lock-version": {"1.0", "1.1", "2.0", "2.1"}},
    "pdm": {"lock_version": {"3.0", "4.5.0", "4.5.1"}},
    "pylock": {"lock-version": {"1.0"}},
    "pipfile": {"pipfile-spec": {6}},
}
MARKER_KEYS = ("marker", "markers")
WHEEL_KEYS = ("wheels", "wheel")
# A live file with fewer than this share of the pinned copy's wheels per package has lost its wheels.
WHEEL_DROP = 0.5
MAX_BYTES = 5_000_000
ATTEMPTS = 3

MISREAD, CHANGED, WARNING = "misread", "upstream changed", "warning"


def manifest() -> list[dict]:
    with MANIFEST.open("rb") as f:
        return tomllib.load(f)["lockfile"]


def safe(text: object) -> str:
    """Text from a lockfile, for printing: repr() escapes newlines and control characters, and backticks go too."""
    return repr(str(text)).replace("`", "\\x60")


@dataclass
class Report:
    source: Source | None = None
    format: str = ""
    findings: list[tuple[str, str]] = field(default_factory=list)

    def add(self, kind: str, message: str) -> None:
        if (kind, message) not in self.findings:
            self.findings.append((kind, message))

    def of(self, kind: str) -> list[str]:
        return [m for k, m in self.findings if k == kind]

    @property
    def failed(self) -> bool:
        return any(k != WARNING for k, _ in self.findings)


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _has_marker(entry: object) -> bool:
    return isinstance(entry, dict) and any(entry.get(k) for k in MARKER_KEYS)


def _wheels(entry: dict) -> list:
    return [w for k in WHEEL_KEYS for w in _list(entry.get(k))]


def _files(pkgs: Iterable[Package]) -> list[str]:
    return [f for p in pkgs for f in p.files or []]


def _edges(source: Source) -> list[Edge]:
    return [e for edges in (source.edges or {}).values() for e in edges] + list(source.root_edges)


def _format(kind: str, data: object, report: Report) -> None:
    """Note the file's format version, and an upstream change if it's one no test covers."""
    if kind == "uv":
        values = {"version": _dict(data).get("version"), "revision": _dict(data).get("revision")}
    elif kind in ("poetry", "pdm"):
        key = "lock-version" if kind == "poetry" else "lock_version"
        values = {key: _dict(_dict(data).get("metadata")).get(key)}
    elif kind == "pylock":
        values = {"lock-version": _dict(data).get("lock-version")}
    else:
        values = {"pipfile-spec": _dict(_dict(data).get("_meta")).get("pipfile-spec")}
    report.format = ", ".join(f"{k} {safe(v) if v is not None else '(none)'}" for k, v in values.items())
    for k, v in values.items():
        if v not in KNOWN_FORMATS[kind][k]:
            report.add(
                CHANGED,
                f"{k} {safe(v)} isn't one the tests cover: check sources.py reads it, then add it to "
                "KNOWN_FORMATS in scripts/check_real_lockfiles.py",
            )


def _counts(kind: str, data: object, source: Source) -> list[tuple[str, int, int]]:
    """(what, how many the raw file has, how many wheelgap read), for the things a renamed key would lose."""
    out: list[tuple[str, int, int]] = []
    pkgs = source.packages
    read_markers = sum(p.markers is not None for p in pkgs)
    files = _files(pkgs)
    wheels = sum(f.endswith(".whl") for f in files)
    sdists = len(files) - wheels
    if kind == "uv":
        raw = [p for p in _list(_dict(data).get("package")) if isinstance(p, dict)]
        out += [
            ("packages", len(raw), len(pkgs)),
            ("wheels", sum(len(_wheels(p)) for p in raw), wheels),
            ("sdists", sum(bool(p.get("sdist")) for p in raw), sdists),
        ]
        manifest = _dict(_dict(data).get("manifest"))
        roots = [d for g in _dict(manifest.get("dependency-groups")).values() for d in _list(g)]
        roots += _list(manifest.get("requirements"))
        deps = [d for p in raw for d in _list(p.get("dependencies"))]
        for table in ("optional-dependencies", "dev-dependencies"):
            deps += [d for p in raw for g in _dict(p.get(table)).values() for d in _list(g)]
        if source.edges is not None:
            edges = _edges(source)
            out += [
                ("dependencies", len(deps) + len(roots), len(edges)),
                ("dependency markers", sum(map(_has_marker, deps + roots)), sum(bool(m) for _, _, m, _ in edges)),
                (
                    "dependency versions",
                    sum(bool(_dict(d).get("version")) for d in deps),
                    sum(bool(v) for _, v, _, _ in edges),
                ),
            ]
    elif kind == "poetry":
        raw = [p for p in _list(_dict(data).get("package")) if isinstance(p, dict)]
        legacy = _dict(_dict(_dict(data).get("metadata")).get("files"))
        raw_markers = 0
        for p in raw:
            m = next((p[k] for k in MARKER_KEYS if p.get(k)), None)
            if isinstance(m, dict) and any(g not in m for g in _list(p.get("groups"))):
                continue  # always installed for one of its groups: wheelgap rightly reads no markers
            raw_markers += m is not None
        out += [
            ("packages", len(raw), len(pkgs)),
            ("files", sum(len(_list(p.get("files", legacy.get(p.get("name"))))) for p in raw), len(files)),
            ("package markers", raw_markers, read_markers),
        ]
        if source.edges is not None:
            specs = [
                s
                for p in raw
                for v in _dict(p.get("dependencies")).values()
                for s in (v if isinstance(v, list) else [v])
            ]
            edges = _edges(source)
            out += [
                ("dependencies", len(specs), len(edges)),
                ("dependency markers", sum(map(_has_marker, specs)), sum(bool(m) for _, _, m, _ in edges)),
            ]
    elif kind in ("pdm", "pipfile"):
        if kind == "pdm":
            entries = [(p.get("name"), p) for p in _list(_dict(data).get("package")) if isinstance(p, dict)]
        else:
            sections = [v for k, v in _dict(data).items() if k != "_meta"]
            entries = [(n, p) for s in sections for n, p in _dict(s).items()]
        by_key: dict[tuple[str, object], list[dict]] = {}
        legacy = _dict(_dict(_dict(data).get("metadata")).get("files"))  # pdm.lock before PDM 2.8
        names: dict[tuple[str, object], str] = {}
        for name, p in entries:
            version = p.get("version")
            version = version.removeprefix("==") if kind == "pipfile" and isinstance(version, str) else version
            key = (canonicalize_name(str(name)), version)
            names.setdefault(key, str(name))
            by_key.setdefault(key, []).append(_dict(p))
        out += [
            ("packages", len(by_key), len(pkgs)),
            # One install per name and version: it has markers only if every entry for it has some.
            ("package markers", sum(all(map(_has_marker, ps)) for ps in by_key.values()), read_markers),
        ]
        if kind == "pdm":
            first = {k: ps[0].get("files", legacy.get(f"{names[k]} {k[1]}")) for k, ps in by_key.items()}
            out.append(("files", sum(len(_list(f)) for f in first.values()), len(files)))
    elif kind == "pylock":
        raw = [p for p in _list(_dict(data).get("packages")) if isinstance(p, dict)]
        out += [
            ("packages", len(raw), len(pkgs)),
            ("wheels", sum(len(_wheels(p)) for p in raw), wheels),
            ("sdists", sum(bool(p.get("sdist")) for p in raw), sdists),
            ("package markers", sum(map(_has_marker, raw)), read_markers),
        ]
    return out


def _gated(source: Source, i: int, seen: frozenset[int] = frozenset()) -> bool:
    """Is package i installed only under some marker: its own, or one on every way in to it?"""
    pkg = source.packages[i]
    if pkg.markers is not None:
        return True
    if source.edges is None:
        return False
    ways_in: list[tuple[int | None, str | None]] = [
        (None, m) for n, _, m, _ in source.root_edges if canonicalize_name(n) == pkg.key
    ]
    for parent, edges in source.edges.items():
        for n, v, m, _ in edges:
            # uv pins the version on an edge only where a name has several; Poetry's is a constraint, not a pin.
            if canonicalize_name(n) == pkg.key and (v is None or v == pkg.version or source.kind == "poetry"):
                ways_in.append((parent, m))
    return bool(ways_in) and all(
        m or (p is not None and p not in seen and _gated(source, p, seen | {i})) for p, m in ways_in
    )


def _marker_problem(marker: object, where: str) -> str | None:
    if not marker:
        return None
    try:
        Marker(str(marker))
    except InvalidMarker:
        return f"{where}: marker {safe(marker)} doesn't parse"
    return None


def inspect(path: Path, pinned: Path | None = None) -> Report:
    """Read one lockfile as wheelgap does, and report what wheelgap would get wrong. With `pinned` (a copy of the same
    project's lockfile that reads cleanly), also compare its wheels per package with that copy's."""
    report = Report()
    try:
        source = load_file(path)
    except Exception as e:  # any failure to read is what this check is for
        report.add(MISREAD, f"can't read it: {type(e).__name__}: {safe(e)}")
        return report
    report.source = source
    kind = source.kind or ""
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if kind == "pipfile" else tomllib.loads(text)
    _format(kind, data, report)
    if not source.packages:
        report.add(MISREAD, "no packages")
    if source.requires_python is not None:
        try:
            SpecifierSet(source.requires_python)
        except InvalidSpecifier:
            report.add(MISREAD, f"requires-python {safe(source.requires_python)} doesn't parse")
    for name, _version, marker, _source in _edges(source):
        if problem := _marker_problem(marker, f"dependency on {safe(name)}"):
            report.add(MISREAD, problem)
    if kind == "poetry":
        lock_version = str(_dict(_dict(data).get("metadata")).get("lock-version", "2.1"))
        graph = tuple(int(x) for x in lock_version.split(".") if x.isdigit()) < (2, 1)
        if graph != (source.edges is not None) and source.packages:
            report.add(
                MISREAD,
                f"lock-version {safe(lock_version)}, but wheelgap "
                f"{'did' if source.edges is not None else 'did not'} walk the dependency graph",
            )
    for what, raw, read in _counts(kind, data, source):
        if raw != read:
            report.add(MISREAD, f"the file has {raw} {what}, but wheelgap read {read}")
    for pkg in source.packages:
        label = f"{safe(pkg.name)} {safe(pkg.version)}"
        if pkg.skip:
            if pkg.skip not in LOCAL_SKIPS:
                report.add(
                    CHANGED,
                    f"{label}: skipped as {safe(pkg.skip)} (a new dependency from git, a URL or "
                    "another index?): wheelgap can't check it, so pick another project or allow it here",
                )
            continue
        if pkg.error:
            report.add(MISREAD, f"{label}: {safe(pkg.error)}")
        try:
            Version(str(pkg.version or ""))
        except InvalidVersion:
            report.add(MISREAD, f"{safe(pkg.name)}: version {safe(pkg.version)} doesn't parse")
        for marker in pkg.markers or []:
            if problem := _marker_problem(marker, label):
                report.add(MISREAD, problem)
        if kind not in RECORDS_FILES:
            continue
        if pkg.files is None:
            report.add(MISREAD, f"{label}: no files read")
            continue
        if not pkg.files:
            report.add(CHANGED, f"{label}: the lock lists no files for it")
        for f in pkg.files:
            if f.endswith(".whl"):
                try:
                    parse_wheel_filename(f)
                except InvalidWheelFilename:
                    report.add(MISREAD, f"{label}: wheel name {safe(f)} doesn't parse")
            elif not f.endswith(SDIST_SUFFIXES):
                report.add(MISREAD, f"{label}: {safe(f)} is neither a wheel nor an sdist")
    if pinned is not None and kind in RECORDS_FILES:
        live, before = _wheel_rate(source), _wheel_rate(load_file(pinned))
        if before and live < WHEEL_DROP * before:
            report.add(MISREAD, f"{live:.1f} wheels per package, down from {before:.1f} in the pinned copy")
    for row in check([source], TARGETS):
        label = f"{safe(row.package.name)} {safe(row.package.version)}"
        if not row.needed:
            i = next(j for j, p in enumerate(source.packages) if p is row.package)
            if _gated(source, i):
                report.add(WARNING, f"{label}: n/a on every target, but only through markers (check them by hand)")
            else:
                report.add(MISREAD, f"{label}: n/a on every target, with no marker on the way in to it")
        elif kind in RECORDS_FILES and Status.UNKNOWN in row.results.values():
            report.add(MISREAD, f"{label}: unknown on {Counter(row.results.values())[Status.UNKNOWN]} targets")
    return report


def _wheel_rate(source: Source) -> float:
    pkgs = [p for p in source.packages if not p.skip]
    return sum(f.endswith(".whl") for f in _files(pkgs)) / len(pkgs) if pkgs else 0.0


class Gone(Exception):
    """The file isn't on the default branch any more (HTTP 404)."""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def download(repo: str, path: str, folder: Path) -> Path:
    """The file at `path` on the default branch (HEAD) of a GitHub repo, saved under its own name in `folder`.
    Retries a 429, a 5xx or a dropped connection; raises Gone for a 404 and OSError for anything else."""
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
    request = urllib.request.Request(url, headers={"User-Agent": "wheelgap-lockfile-corpus"})
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise Gone(f"{url} is gone (HTTP 404)") from None
            if (e.code != 429 and e.code < 500) or attempt == ATTEMPTS:
                raise OSError(f"{url}: HTTP {e.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            if attempt == ATTEMPTS:
                raise OSError(f"{url}: {type(e).__name__}: {e}") from None
        else:
            if len(data) > MAX_BYTES:
                raise OSError(f"{url} is bigger than {MAX_BYTES} bytes")
            dest = folder / Path(path).name
            dest.write_bytes(data)
            return dest
        _sleep(5 * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("names", nargs="*", help="projects to check, by folder name in sources.toml (default: all)")
    parser.add_argument("--pinned", action="store_true", help="check the copies in tests/fixtures/real/, offline")
    args = parser.parse_args(argv)
    entries = manifest()
    folders = {e["file"].split("/")[0] for e in entries}
    if unknown := sorted(set(args.names) - folders):
        parser.error(f"not in sources.toml: {', '.join(unknown)} (known: {', '.join(sorted(folders))})")
    failed: dict[str, list[str]] = {}
    for e in entries:
        folder = e["file"].split("/")[0]
        if args.names and folder not in args.names:
            continue
        name = f"{e['repo']} {e['path']} ({'pinned copy' if args.pinned else 'default branch'})"
        report = Report()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                if args.pinned:
                    report = inspect(CORPUS / e["file"])
                else:
                    report = inspect(download(e["repo"], e["path"], Path(tmp)), pinned=CORPUS / e["file"])
            except Gone as err:
                report.add(CHANGED, f"{err}: the project moved or dropped it, so update or replace it in sources.toml")
            except OSError as err:
                report.add("couldn't check", f"can't download it: {err}")
            except Exception as err:  # a lockfile odd enough to break the checks: report it, then go on
                report.add(MISREAD, f"checking it failed: {type(err).__name__}: {safe(err)}")
        status = "FAIL" if report.failed else "warn" if report.findings else "ok  "
        size = f"; {len(report.source.packages)} packages" if report.source else ""
        print(f"{status} {name}: {report.format or 'format unknown'}{size}")
        for kind, message in report.findings:
            print(f"    {kind}: {message}")
        if report.failed:
            failed[name] = sorted({k for k, _ in report.findings if k != WARNING})
    if failed:
        print(f"\n{len(failed)} lockfile{'s' if len(failed) != 1 else ''} failed:")
        for name, kinds in failed.items():
            print(f"    {name}: {', '.join(kinds)}")
        if any(CHANGED in kinds for kinds in failed.values()):
            print("An upstream change needs the corpus refreshed: see AGENTS.md (Tests, Real lockfiles).")
        if any(MISREAD in kinds for kinds in failed.values()):
            print("A misread is a wheelgap bug: fix the reader in src/wheelgap/sources.py.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
