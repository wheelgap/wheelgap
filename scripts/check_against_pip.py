"""Compare wheelgap's answers with pip and uv on real PyPI packages (#226).

    uv run --with pip python scripts/check_against_pip.py                # every package on every target
    uv run --with pip python scripts/check_against_pip.py numpy orjson   # just these packages
    uv run --with pip python scripts/check_against_pip.py -t win-arm64 -t "3.14t linux-x86_64"

    uv run python scripts/check_against_pip.py --installer uv numpy    # use uv as the referee
    uv run --with pip python scripts/check_against_pip.py --json accuracy.json   # also write the results as JSON

For each package, wheelgap finds the latest release and checks it pinned (NAME==VERSION, so yanked files count as
they do for pip, PEP 592) on each target. Then pip is asked what it would install for the same pin, with
`pip install --dry-run --ignore-installed --no-deps --only-binary=:all:` and the target's tags as AGENTS.md lists
them ("No issue fits? Find a real bug"). A dry run picks a file exactly as `pip download` does, but fetches only
the wheel's metadata. The two must agree: pip picks a wheel exactly when wheelgap says `wheel`. Every disagreement is
printed with the pip command that shows it, and the exit code is 1. A check that couldn't be made is listed too, and
makes the exit code 2 when nothing disagreed; so does a crash. One answer isn't compared: a yanked wheel that pip
takes only because --only-binary hides the sdist a normal install would build (wheelgap says `source`, rightly).

"No wheel" needs positive evidence, because pip prints "No matching distribution found" when it can't reach the
index too: pip runs with -vv, and its answer counts only if it fetched the project's page on the index and logged
no retry and no failed fetch. Anything else is an error, not an answer.

The pip arguments are written out here from AGENTS.md's rules, not taken from wheelgap's targets.py, on purpose:
pip is the referee, so a regression in targets.py's tag lists must show up as a disagreement rather than be handed
on to pip as well.

The uv check uses `uv pip compile --no-deps --only-binary :all:`: it resolves the single pin without
installing it or needing a target environment. The emitted pin confirms that a wheel resolved. uv does
not emit the chosen filename in that output, so the report says "wheel". Only explicit wheel incompatibility
diagnostics for that pin count as "no wheel". Targets uv cannot express are listed with a reason and do not
fail a run that made comparisons. uv takes only the Python version from --python-version: the implementation and
the free-threaded ABI in its tags come from the interpreter it resolves with. So uv is given the interpreter that
runs this script (`--python`, left out of the printed commands), and the uv check refuses to run on anything but a
default (GIL) build of CPython. uv also wraps its diagnostics to COLUMNS or the terminal's width, so it runs with
UV_NO_WRAP and without COLUMNS: classify_uv() reads whole lines.

Each check asks wheelgap about a pin (NAME==VERSION), a requirements-style input, and wheelgap answers those as pip
installs them (a uv.lock it answers as uv does). uv and pip are known to differ on one thing that matters here: uv
reads Requires-Python as a lower bound only, so it takes a wheel whose Requires-Python stops below the target's
Python, where pip, and so wheelgap, leaves the file out. When uv takes a wheel and wheelgap left one of the
release's wheels out for a Requires-Python with an upper bound, the check is listed as not compared, with that
reason, and doesn't fail the run: it is uv answering as uv, not a wheelgap error, and the pip check is the referee
for that pin. Every other difference from uv is listed as a disagreement, to be checked against the pip run.

--json FILE writes the run's results as one JSON object (results()): the installer and its version, wheelgap's
version, the UTC time, the packages and targets, the totals with the mismatch rate (disagreements / checks compared,
null when nothing was compared), the same totals per target, and every disagreement and every check that couldn't be
compared. It's written whenever the text report is, from the same counts (totals()).

It needs the network, so the tests never run it; .github/workflows/accuracy.yml runs it every week. pip runs
isolated from pip.conf and PIP_* variables, without a cache, and writes only its small report to a temporary folder
that is deleted after each run.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import sysconfig
import tempfile
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from wheelgap import Result, __version__, check_packages, make_targets
from wheelgap.check import Status
from wheelgap.targets import DEFAULT_GLIBC, PLATFORMS, Target

# Packages with interesting wheel sets: per-Python builds with and without free-threaded wheels, abi3, Rust and
# mypyc builds with py3-none-<platform> wheels, packages without musllinux or Windows on ARM wheels, a Windows-only
# package without an sdist (pywin32), one that ships wheels only for Windows (mysqlclient), a pure-Python wheel
# (requests) and an sdist-only package (docopt).
PACKAGES = [
    *("numpy", "scipy", "pandas", "pyarrow", "cryptography", "pydantic-core", "psycopg2-binary", "psycopg-binary"),
    *("orjson", "lxml", "grpcio", "pillow", "matplotlib", "scikit-learn", "cffi", "charset-normalizer", "markupsafe"),
    *("pyyaml", "msgpack", "ujson", "regex", "rpds-py", "aiohttp", "multidict", "zstandard", "lz4", "bcrypt"),
    *("pynacl", "greenlet", "gevent", "uvloop", "httptools", "watchfiles", "tokenizers", "safetensors", "jiter"),
    *("ruff", "shapely", "pyproj", "contourpy", "duckdb", "tiktoken", "psutil", "wrapt", "mysqlclient", "pywin32"),
    *("protobuf", "pycryptodome", "pyzmq", "numba", "requests", "docopt"),
]

# The targets #226 lists: manylinux and musllinux on x86_64 and aarch64, Windows on x86_64 and ARM, macOS on Apple
# silicon, a free-threaded Python, and an old glibc (2.17, manylinux2014's, where the legacy aliases come in).
OLD_GLIBC = "2.17"

INDEX = "https://pypi.org/simple"
# On every pip run: no pip.conf or PIP_* variables, no cache, no self-check, and -vv so the log shows the index pages.
PIP_OPTIONS = ["--isolated", "--disable-pip-version-check", "--no-input", "--no-cache-dir", "-vv"]
PIP_TIMEOUT = 600


def default_targets() -> list[Target]:
    native = ["linux-x86_64", "linux-aarch64", "musl-x86_64", "musl-aarch64", "win-amd64", "win-arm64", "macos-arm64"]
    return [
        *make_targets("3.14", native),
        *make_targets("3.14t", "linux-x86_64"),
        *make_targets("3.14", "linux-x86_64", glibc=OLD_GLIBC),
    ]


def label(target: Target) -> str:
    """The target's label, plus its glibc when it isn't wheelgap's default (two targets can differ only in that)."""
    glibc = ".".join(map(str, target.glibc))
    old = PLATFORMS[target.platform][0] == "manylinux" and glibc != DEFAULT_GLIBC
    return target.label(True) + (f" glibc {glibc}" if old else "")


_X86 = ("x86_64", "i686")
_MANYLINUX2014_ARCHES = ("aarch64", "armv7l", "ppc64le", "s390x")


def pip_platforms(target: Target) -> list[str]:
    """The --platform values AGENTS.md says to pass pip for this target. pip takes manylinux and musllinux tags
    literally, so each libc version the target accepts is listed, from its own down."""
    family, arch = PLATFORMS[target.platform]
    if family == "manylinux":
        major, minor = target.glibc
        if major != 2 or arch not in (*_X86, *_MANYLINUX2014_ARCHES):
            raise ValueError(f"AGENTS.md doesn't say which tags to pass pip for {label(target)}")
        floor = 5 if arch in _X86 else 17  # other arches start at manylinux2014 (glibc 2.17)
        out = [f"manylinux_2_{m}_{arch}" for m in range(minor, floor - 1, -1)]
        legacy = (
            [("manylinux2014", 17), ("manylinux2010", 12), ("manylinux1", 5)]
            if arch in _X86
            else [("manylinux2014", 17)]
        )
        out += [f"{name}_{arch}" for name, glibc_minor in legacy if minor >= glibc_minor]
        return out + [f"linux_{arch}"]  # pip on Linux takes plain linux_<arch> too; PyPI allows it only for armv7l
    if family == "musllinux":
        major, minor = target.musl
        return [f"musllinux_{major}_{m}_{arch}" for m in range(minor, -1, -1)]  # 1.2: musllinux_1_2 down to 1_0
    if family == "windows":
        return [arch]
    if family == "macos":
        return [f"macosx_{target.macos[0]}_{target.macos[1]}_{arch}"]  # pip widens it to older macOS itself
    raise ValueError(f"pip can't check {label(target)} with `pip download --platform`")


def pip_args(name: str, version: str, target: Target) -> list[str]:
    """The arguments after `pip` that ask what pip would install of NAME==VERSION on the target."""
    abi = f"cp{target.major}{target.minor}{'t' if target.freethreaded else ''}"
    platforms = [arg for plat in pip_platforms(target) for arg in ("--platform", plat)]
    return [
        *("install", "--dry-run", "--ignore-installed", "--no-deps", "--only-binary=:all:", "--index-url", INDEX),
        *platforms,
        *("--python-version", f"{target.major}.{target.minor}", "--implementation", "cp", "--abi", abi),
        f"{name}=={version}",
    ]


@dataclass(frozen=True)
class InstallerAnswer:
    wheel: str | None = None  # filename, or "wheel" if the installer only confirms availability
    error: str | None = None
    yanked: bool = False  # uv reports a yanked selection without naming its wheel

    @property
    def text(self) -> str:
        return self.wheel or ("error" if self.error else "no wheel")


def _planned(report: Path) -> list[str] | None:
    """The filenames in pip's --report, or None if it wrote none (it doesn't when it fails)."""
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        return [unquote(urlsplit(item["download_info"]["url"]).path.rsplit("/", 1)[-1]) for item in data["install"]]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()  # the normalized name pip asks the index for


def classify(name: str, returncode: int, log: str, planned: list[str] | None) -> InstallerAnswer:
    """pip's answer, trusting "no wheel" only when pip fetched the project's page with no retry or failed fetch."""
    if returncode == 0:
        if planned is not None and len(planned) == 1 and planned[0].endswith(".whl"):
            return InstallerAnswer(wheel=planned[0])
        return InstallerAnswer(error=f"pip succeeded but would install {planned}")
    lines = log.splitlines()
    if trouble := [line.strip() for line in lines if "Retrying (" in line or "Could not fetch URL" in line]:
        return InstallerAnswer(error=f"pip: {trouble[0]}")
    fetched = any(line.startswith(f"Fetched page {INDEX}/{_key(name)}/ ") for line in lines)
    if fetched and any("No matching distribution found" in line for line in lines):
        return InstallerAnswer()
    last = next((line.strip() for line in reversed(lines) if line.strip()), f"exit {returncode}")
    return InstallerAnswer(error=f"pip: {last}" if fetched else f"pip didn't fetch {INDEX}/{_key(name)}/: {last}")


def run_pip(name: str, args: list[str]) -> InstallerAnswer:
    """Run pip with `args` (from pip_args) and read its answer from its report and log."""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
    with tempfile.TemporaryDirectory(prefix="wheelgap-pip-") as tmp:
        report = Path(tmp) / "report.json"
        cmd = [sys.executable, "-m", "pip", *args, *PIP_OPTIONS, "--report", str(report)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT, env=env)
        except subprocess.TimeoutExpired:
            return InstallerAnswer(error=f"pip timed out after {PIP_TIMEOUT} s")
        return classify(name, proc.returncode, f"{proc.stdout}\n{proc.stderr}", _planned(report))


# uv's documented --python-platform triples and accepted libc versions:
# https://docs.astral.sh/uv/reference/cli/#uv-pip-compile--python-platform
_UV_MANYLINUX_ARCH = {"linux-x86_64": "x86_64", "linux-aarch64": "aarch64"}
_UV_MANYLINUX_MINORS = (17, 28, *range(31, 41))
_UV_PLATFORMS = {
    "win-amd64": "x86_64-pc-windows-msvc",
    "win-arm64": "aarch64-pc-windows-msvc",
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x86_64": "x86_64-apple-darwin",
}
_UV_MUSL_PLATFORMS = {"musl-x86_64": "x86_64-unknown-linux-musl", "musl-aarch64": "aarch64-unknown-linux-musl"}
UV_TIMEOUT = 600


def uv_platform(target: Target) -> str | None:
    if target.freethreaded:
        return None  # uv pip compile --python-version does not accept a free-threaded version
    if arch := _UV_MANYLINUX_ARCH.get(target.platform):
        major, minor = target.glibc
        if major == 2 and minor in _UV_MANYLINUX_MINORS:
            return f"{arch}-manylinux_2_{minor}"
    if target.musl == (1, 2) and target.platform in _UV_MUSL_PLATFORMS:
        return _UV_MUSL_PLATFORMS[target.platform]
    return _UV_PLATFORMS.get(target.platform)


def uv_command(target: Target, platform: str) -> list[str]:
    return [
        "uv",
        "pip",
        "compile",
        "-",
        "--no-deps",
        "--only-binary",
        ":all:",
        "--no-config",
        "--no-cache",
        "--default-index",
        INDEX,
        "--python-version",
        f"{target.major}.{target.minor}",
        "--python-platform",
        platform,
        "--no-python-downloads",
        "--no-header",
        "--no-annotate",
        "--color",
        "never",
    ]


def uv_deployment_target(target: Target) -> str:
    return ".".join(map(str, target.macos))


def classify_uv(pin: str, returncode: int, stdout: str, stderr: str) -> InstallerAnswer:
    # uv 0.13's no-solution diagnostic names the exact pin before and after the incompatibility.
    lines = stderr.splitlines()
    if returncode == 0 and stdout.strip() == pin:
        yanked = any(line.startswith(f"warning: `{pin}` is yanked") for line in lines)
        return InstallerAnswer(wheel="wheel", yanked=yanked)
    cause = re.compile(
        rf"\s*cause: Because {re.escape(pin)} has (?:no usable wheels|"
        rf"no wheels with a matching (?:Python ABI|Python implementation|platform) tag(?: \(e\.g\., `[^`]+`\))?) "
        rf"and you require {re.escape(pin)}, we can conclude that your requirements are unsatisfiable\."
    )
    errors = [line for line in lines if line.startswith("error:")]
    if (
        returncode == 1
        and errors == ["error: No solution found when resolving dependencies"]
        and any(cause.fullmatch(line) for line in lines)
    ):
        return InstallerAnswer()
    detail = next(iter(errors), f"unexpected output (exit {returncode})")
    # uv's reason is on the "cause:" line: without it, every unrecognised diagnostic reads "No solution found".
    because = next((line.strip() for line in lines if line.lstrip().startswith("cause:")), None)
    return InstallerAnswer(error=f"uv: {detail}" + (f" ({because})" if because else ""))


def default_cpython() -> bool:
    """Whether this interpreter is a default (GIL) build of CPython. uv is run with it (run_uv), and uv's tags take
    their implementation and free-threaded ABI from the interpreter, not from --python-version: on a free-threaded
    build or another implementation, uv would answer for that instead of for the target."""
    return sys.implementation.name == "cpython" and not sysconfig.get_config_var("Py_GIL_DISABLED")


def run_uv(name: str, version: str, target: Target, platform: str) -> InstallerAnswer:
    # No UV_* settings, and no COLUMNS: uv wraps its diagnostics to it, and classify_uv() reads whole lines.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("UV_") and key.upper() not in ("MACOSX_DEPLOYMENT_TARGET", "COLUMNS")
    }
    env["UV_NO_WRAP"] = "1"
    if target.platform.startswith("macos-"):
        env["MACOSX_DEPLOYMENT_TARGET"] = uv_deployment_target(target)
    pin = f"{_key(name)}=={version}"
    # This interpreter, which main() has checked with default_cpython(), not whichever one uv finds.
    cmd = [*uv_command(target, platform), "--python", sys.executable]
    try:
        proc = subprocess.run(cmd, input=pin + "\n", capture_output=True, text=True, timeout=UV_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return InstallerAnswer(error=f"uv timed out after {UV_TIMEOUT} s")
    except OSError as exc:
        return InstallerAnswer(error=f"uv: {exc}")
    return classify_uv(pin, proc.returncode, proc.stdout, proc.stderr)


@dataclass(frozen=True)
class Check:
    name: str
    version: str | None
    target: Target
    wheelgap: Status
    answer: InstallerAnswer | None
    error: str | None = None
    yanked: frozenset[str] = frozenset()  # the release's yanked files
    skipped: str | None = None  # why this check isn't a comparison, when that isn't a failure of the run

    @property
    def only_binary_yanked(self) -> bool:
        """The installer took a yanked wheel while wheelgap found a source build hidden by --only-binary."""
        return (
            self.wheelgap is Status.SOURCE
            and self.answer is not None
            and (self.answer.yanked or self.answer.wheel in self.yanked)
        )

    @property
    def compared(self) -> bool:
        return (
            self.answer is not None
            and self.answer.error is None
            and self.wheelgap is not Status.UNKNOWN
            and not self.only_binary_yanked
            and self.skipped is None
        )

    @property
    def agrees(self) -> bool:
        assert self.answer is not None
        return (self.wheelgap is Status.WHEEL) == (self.answer.wheel is not None)

    def command(self, installer: str) -> str:
        assert self.version is not None
        if installer == "pip":
            return shlex.join(["pip", *pip_args(self.name, self.version, self.target)])
        platform = uv_platform(self.target)
        assert platform is not None
        pin = shlex.quote(f"{_key(self.name)}=={self.version}")
        command = shlex.join(uv_command(self.target, platform))
        if self.target.platform.startswith("macos-"):
            command = f"MACOSX_DEPLOYMENT_TARGET={uv_deployment_target(self.target)} {command}"
        return f"printf '%s\\n' {pin} | {command}"


def uv_reads_requires_python_as_a_floor(result: Result, status: Status, answer: InstallerAnswer) -> str | None:
    """Why uv's wheel isn't compared with wheelgap's answer, when the difference is the known one between uv and
    pip: uv reads Requires-Python as a lower bound only, so it takes a wheel that pip, and wheelgap for a pin, leaves
    out because its Requires-Python stops below the target's Python. It needs positive evidence: uv took a wheel,
    wheelgap found none, and wheelgap left a wheel of the release out for a Requires-Python with an upper bound.
    (requires_python_rejected covers every Python among the targets; they share one Python here.)"""
    if answer.wheel is None or status not in (Status.SOURCE, Status.MISSING):
        return None
    capped = sorted(
        why for file, why in result.requires_python_rejected.items() if file.endswith(".whl") and "<" in why
    )
    if not capped:
        return None
    return (
        f"uv took a wheel that {capped[0]}: uv ignores Requires-Python's upper bound, "
        "and wheelgap answers a pin as pip does"
    )


def check_one(
    result: Result, targets: Sequence[Target], ask: Callable[[str, str, Target], InstallerAnswer], installer: str
) -> list[Check]:
    """Ask the installer about one package on every supported target."""
    checks = []
    for target in targets:
        if installer == "uv" and uv_platform(target) is None:
            reason = (
                "uv pip compile cannot target free-threaded Python"
                if target.freethreaded
                else "uv pip compile cannot express this platform or libc version"
            )
            checks.append(Check(result.name, result.version, target, Status.UNKNOWN, None, skipped=reason))
        elif result.error or result.version is None:
            checks.append(
                Check(result.name, result.version, target, Status.UNKNOWN, None, result.error or "no version")
            )
        else:
            answer = ask(result.name, result.version, target)
            status = result.statuses[target]
            # Not for a yanked wheel: that one is only_binary_yanked's to explain.
            floor = installer == "uv" and not answer.yanked
            known = uv_reads_requires_python_as_a_floor(result, status, answer) if floor else None
            checks.append(
                Check(result.name, result.version, target, status, answer, yanked=result.yanked, skipped=known)
            )
    return checks


def compare(
    packages: Sequence[str],
    targets: Sequence[Target],
    pip: Callable[[str, list[str]], InstallerAnswer] = run_pip,
    workers: int = 4,
    *,
    installer: str = "pip",
) -> list[Check]:
    if installer == "pip":
        for target in targets:
            pip_platforms(target)  # fail before asking pip anything for a target AGENTS.md has no pip tags for

    def ask(name: str, version: str, target: Target) -> InstallerAnswer:
        if installer == "pip":
            return pip(name, pip_args(name, version, target))
        platform = uv_platform(target)
        assert platform is not None
        return run_uv(name, version, target, platform)

    # The latest release, then that release pinned, so wheelgap answers the question pip gets (yanked files included).
    supported = targets if installer == "pip" else [target for target in targets if uv_platform(target) is not None]
    if not supported:
        return [row for name in packages for row in check_one(Result(name, None), targets, ask, installer)]
    latest = check_packages(packages, supported)
    found = [(r.name, r.version) for r in latest if not r.error and r.version]
    pinned = iter(check_packages(found, supported) if found else [])
    results = [next(pinned) if not r.error and r.version else r for r in latest]
    checks: list[Check] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, rows in enumerate(pool.map(lambda r: check_one(r, targets, ask, installer), results), 1):
            bad = sum(c.compared and not c.agrees for c in rows)
            print(f"[{done}/{len(results)}] {rows[0].name} {rows[0].version}: {bad} disagreement(s)", file=sys.stderr)
            checks.extend(rows)
    return checks


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip() for row in rows]


def totals(checks: Sequence[Check]) -> dict[str, int]:
    """The counts behind the text report and the --json file, made once so the two can't disagree."""
    compared = [c for c in checks if c.compared]
    disagree = sum(1 for c in compared if not c.agrees)
    return {
        "checks": len(checks),
        "compared": len(compared),
        "agree": len(compared) - disagree,
        "disagree": disagree,
        "not_compared": len(checks) - len(compared),
    }


def mismatch_rate(counts: dict[str, int]) -> float | None:
    """Disagreements as a fraction of the checks compared; None when nothing was compared."""
    return counts["disagree"] / counts["compared"] if counts["compared"] else None


def why_not_compared(check: Check, installer: str) -> str:
    """The reason a check that wasn't compared gets in the report."""
    if check.only_binary_yanked:
        return f"{installer} took a yanked wheel because --only-binary hides the sdist a normal install builds"
    answer = check.answer
    return (
        check.skipped
        or check.error
        or (answer.error if answer and answer.error else f"wheelgap says {check.wheelgap.value}")
    )


def results(
    checks: Sequence[Check],
    *,
    installer: str,
    installer_version: str,
    packages: Sequence[str],
    targets: Sequence[Target],
) -> dict:
    """--json's object. `installer_version`: the installer's own version line (`pip --version`, `uv --version`).
    A disagreement's answer from the installer is under `installer_result`, next to `wheelgap`."""
    counts = totals(checks)
    return {
        "schema": 1,
        "installer": installer,
        "installer_version": installer_version,
        "wheelgap_version": __version__,
        "time": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "packages": list(packages),
        "targets": [label(t) for t in targets],
        "totals": counts,
        "mismatch_rate": mismatch_rate(counts),
        "by_target": {label(t): totals([c for c in checks if c.target == t]) for t in targets},
        "disagreements": [
            {
                "name": c.name,
                "version": c.version,
                "target": label(c.target),
                "wheelgap": c.wheelgap.value,
                "installer_result": c.answer.text if c.answer else "",
                "command": c.command(installer),
            }
            for c in checks
            if c.compared and not c.agrees
        ],
        "not_compared": [
            {"name": c.name, "version": c.version, "target": label(c.target), "why": why_not_compared(c, installer)}
            for c in checks
            if not c.compared
        ],
    }


def report(checks: list[Check], out=sys.stdout, *, installer: str = "pip") -> int:
    """Print the disagreements and the checks that couldn't be made. 1 if any disagree, else 2 if any couldn't be
    made (apart from unsupported targets and uv's known difference from pip), else 0."""
    compared = [c for c in checks if c.compared]
    wrong = [c for c in compared if not c.agrees]
    failed = [c for c in checks if not c.compared]
    counts = totals(checks)
    wheels = sum(1 for c in compared if c.agrees and c.wheelgap is Status.WHEEL)
    rate = mismatch_rate(counts)
    said = (
        "n/a (nothing compared)"
        if rate is None
        else f"{rate:.1%} ({counts['disagree']} of {counts['compared']} compared)"
    )
    print(
        f"{counts['checks']} checks: {counts['agree']} agree with {installer} ({wheels} wheel, "
        f"{counts['agree'] - wheels} no wheel), {counts['disagree']} disagree, "
        f"{counts['not_compared']} couldn't be compared. Mismatch rate {said}.",
        file=out,
    )
    if installer == "uv":
        print(
            "wheelgap answers a pin as pip installs it. Where uv took a wheel only because it ignores "
            "Requires-Python's upper bound, the check is listed as not compared, not as a disagreement.",
            file=out,
        )
    if wrong:
        last_word = (
            "pip has the last word"
            if installer == "pip"
            else "a pin follows pip, so check the pip run before calling one a wheelgap error"
        )
        print(f"\nDisagreements ({last_word}):\n", file=out)
        rows = [["package", "version", "target", "wheelgap", installer]]
        rows += [
            [c.name, c.version or "-", label(c.target), c.wheelgap.value, c.answer.text if c.answer else ""]
            for c in wrong
        ]
        print("\n".join(_table(rows)), file=out)
        print(f"\n{installer} commands:\n", file=out)
        for c in wrong:
            print(f"# {c.name} {c.version} on {label(c.target)}\n{c.command(installer)}", file=out)
    if failed:
        print("\nCouldn't compare:\n", file=out)
        rows = [["package", "version", "target", "why"]]
        rows += [[c.name, c.version or "-", label(c.target), why_not_compared(c, installer)] for c in failed]
        print("\n".join(_table(rows)), file=out)
    return 1 if wrong else 2 if not compared or any(c.skipped is None for c in failed) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("packages", nargs="*", help="packages to check (default: the built-in list of about 50)")
    parser.add_argument(
        "-t", "--target", action="append", default=[], help="only this target: a label, or a platform name"
    )
    parser.add_argument("--installer", choices=("pip", "uv"), default="pip", help="installer to compare against")
    parser.add_argument("--workers", type=int, default=4, help="packages checked at once (default 4)")
    parser.add_argument(
        "--json", metavar="FILE", help="also write the results to FILE as JSON: totals, mismatch rate, each difference"
    )
    args = parser.parse_args(argv)
    targets = default_targets()
    if args.target:
        targets = [t for t in targets if label(t) in args.target or t.platform in args.target]
        if not targets:
            known = ", ".join(map(label, default_targets()))
            parser.error(f"no target matches {args.target}; the targets are: {known}")
    if args.installer == "uv" and not default_cpython():
        parser.error(
            "uv takes the implementation and free-threaded ABI of its tags from the Python that runs this script, "
            "so the uv check needs a default (GIL) build of CPython: run it with `uv run --python 3.14 python ...`"
        )
    version_command = [sys.executable, "-m", "pip", "--version"] if args.installer == "pip" else ["uv", "--version"]
    try:
        version = subprocess.run(version_command, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        parser.error("uv isn't on PATH; install uv or run with --installer pip")
    if version.returncode != 0:
        if args.installer == "pip":
            parser.error("pip isn't installed in this environment; run it with `uv run --with pip python ...`")
        parser.error(f"couldn't get uv's version: {version.stderr.strip()}")
    print(version.stdout.strip(), file=sys.stderr)
    packages = args.packages or PACKAGES
    names = ", ".join(map(label, targets))
    print(f"Checking {len(packages)} packages on {len(targets)} targets: {names}", file=sys.stderr)
    checks = compare(packages, targets, workers=args.workers, installer=args.installer)
    if args.json:
        data = results(
            checks,
            installer=args.installer,
            # pip's line names the folder it runs from: leave that out of a file meant to be shared.
            installer_version=re.sub(r" from .* \(python", " (python", version.stdout.strip()),
            packages=packages,
            targets=targets,
        )
        Path(args.json).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return report(checks, installer=args.installer)


def run(argv: list[str] | None = None) -> int:
    """main(), but a crash exits 2 ("couldn't check"), so it's never mistaken for a disagreement (1)."""
    try:
        return main(argv)
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(run())
