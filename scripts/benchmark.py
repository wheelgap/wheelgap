"""Time wheelgap on big synthetic lockfiles, offline (#234).

    uv run python scripts/benchmark.py                    # N = 100, 500, 2000 x -t everything x 3.12, 3.13, 3.14t
    uv run python scripts/benchmark.py --sizes 500 --explain
    uv run python scripts/benchmark.py --profile 2000 --explain  # cProfile one in-process run, top functions

Each timing is a fresh `python -m wheelgap LOCK --offline --no-cache --json` process (so nothing is cached
between runs), best of --repeat. The lockfile is a uv.lock with a root project that depends on every package,
some through markers, and realistic wheel lists: pure-Python wheels, sdist-only packages, abi3 wheels and numpy-style
packages with a wheel per Python and platform. It's generated from a fixed seed, so runs are comparable.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import random
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PYTHONS = ["3.12", "3.13", "3.14t"]
URL = "https://files.pythonhosted.org/packages/00/00"

# Platform tags of a numpy-style release: one wheel per Python for each of these.
_NATIVE_PLATFORMS = [
    "manylinux_2_27_x86_64.manylinux_2_28_x86_64",
    "manylinux_2_26_aarch64.manylinux_2_28_aarch64",
    "manylinux_2_17_ppc64le.manylinux2014_ppc64le",
    "manylinux_2_17_s390x.manylinux2014_s390x",
    "musllinux_1_2_x86_64",
    "musllinux_1_2_aarch64",
    "macosx_10_13_x86_64",
    "macosx_11_0_arm64",
    "win_amd64",
    "win_arm64",
    "win32",
]
_NATIVE_ABIS = ["cp311-cp311", "cp312-cp312", "cp313-cp313", "cp313-cp313t", "cp314-cp314", "cp314-cp314t"]
# A package that only ships the common x86_64 wheels for a couple of Pythons.
_PARTIAL_PLATFORMS = ["manylinux_2_17_x86_64.manylinux2014_x86_64", "macosx_10_9_x86_64", "win_amd64"]
_ABI3_PLATFORMS = [
    "manylinux_2_17_x86_64.manylinux2014_x86_64",
    "manylinux_2_17_aarch64.manylinux2014_aarch64",
    "musllinux_1_1_x86_64",
    "macosx_10_12_universal2",
    "win_amd64",
]
_MARKERS = [
    "sys_platform == 'win32'",
    "sys_platform != 'win32'",
    "python_full_version < '3.13'",
    "platform_machine == 'x86_64'",
    "sys_platform == 'linux' and platform_machine == 'aarch64'",
    "implementation_name == 'cpython'",
]


def _wheels(name: str, version: str, kind: str) -> list[str]:
    stem = f"{name}-{version}"
    if kind == "pure":
        return [f"{stem}-py3-none-any.whl"]
    if kind == "native":
        return [f"{stem}-{abi}-{plat}.whl" for abi in _NATIVE_ABIS for plat in _NATIVE_PLATFORMS]
    if kind == "partial":
        return [f"{stem}-{abi}-{plat}.whl" for abi in ("cp312-cp312", "cp313-cp313") for plat in _PARTIAL_PLATFORMS]
    if kind == "abi3":
        return [f"{stem}-cp39-abi3-{plat}.whl" for plat in _ABI3_PLATFORMS]
    return []  # sdist only


def make_lockfile(n: int, seed: int = 234) -> str:
    """The text of a uv.lock with a root project and `n` registry packages."""
    rng = random.Random(seed)
    kinds = rng.choices(["pure", "native", "partial", "abi3", "sdist"], weights=[50, 15, 10, 15, 10], k=n)
    names = [f"pkg{i:05d}" for i in range(n)]
    root = []
    for name in names:
        marker = rng.choice(_MARKERS) if rng.random() < 0.15 else None
        root.append(f'    {{ name = "{name}", marker = "{marker}" }},' if marker else f'    {{ name = "{name}" }},')
    out = [
        "version = 1",
        "revision = 3",
        'requires-python = ">=3.11"',
        "",
        "[[package]]",
        'name = "bench"',
        'version = "0.1.0"',
        'source = { editable = "." }',
        "dependencies = [",
        *root,
        "]",
    ]
    for i, (name, kind) in enumerate(zip(names, kinds, strict=True)):
        version = f"{rng.randint(0, 30)}.{rng.randint(0, 20)}.{rng.randint(0, 9)}"
        out += ["", "[[package]]", f'name = "{name}"', f'version = "{version}"']
        out.append('source = { registry = "https://pypi.org/simple" }')
        if i and rng.random() < 0.5:  # a few edges between packages, so the dependency walk has work to do
            deps = sorted({names[rng.randrange(i)] for _ in range(rng.randint(1, 3))})
            out.append("dependencies = [" + ", ".join(f'{{ name = "{d}" }}' for d in deps) + "]")
        if kind != "native" or rng.random() < 0.5:
            out.append(f'sdist = {{ url = "{URL}/{name}-{version}.tar.gz", hash = "sha256:00", size = 1 }}')
        if wheels := _wheels(name, version, kind):
            out.append("wheels = [")
            out += [f'    {{ url = "{URL}/{w}", hash = "sha256:00", size = 1 }},' for w in wheels]
            out.append("]")
    return "\n".join(out) + "\n"


def write_lockfile(folder: Path, n: int) -> Path:
    path = folder / f"bench-{n}" / "uv.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(make_lockfile(n), encoding="utf-8")
    return path


def argv(lock: Path, pythons: list[str], explain: bool) -> list[str]:
    args = [str(lock), "--offline", "--no-cache", "-t", "everything", "--json"]
    for p in pythons:
        args += ["-p", p]
    return args + ["--explain"] * explain


def run_in_process(lock: Path, pythons: list[str], explain: bool = False) -> str:
    """One run of wheelgap's CLI in this process; returns its --json output."""
    from wheelgap.cli import main

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        main(argv(lock, pythons, explain))
    return out.getvalue()


def time_subprocess(lock: Path, pythons: list[str], explain: bool, repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        done = subprocess.run(
            [sys.executable, "-m", "wheelgap", *argv(lock, pythons, explain)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.perf_counter() - start
        if done.returncode not in (0, 1):  # 1 just means gaps; anything else didn't check the lockfile
            raise RuntimeError(f"wheelgap exited {done.returncode}:\n{done.stderr}")
        best = min(best, elapsed)
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 500, 2000], help="package counts")
    parser.add_argument("-p", "--python", action="append", help=f"Pythons to check (default: {' '.join(PYTHONS)})")
    parser.add_argument("--repeat", type=int, default=5, help="runs per size; the fastest counts (default: 5)")
    parser.add_argument("--explain", action="store_true", help="also pass --explain")
    parser.add_argument("--profile", type=int, metavar="N", help="cProfile one in-process run with N packages")
    parser.add_argument("--keep", type=Path, metavar="DIR", help="write the lockfiles here instead of a temp folder")
    args = parser.parse_args()
    pythons = args.python or PYTHONS
    with tempfile.TemporaryDirectory() as tmp:
        folder = args.keep or Path(tmp)
        if args.profile:
            lock = write_lockfile(folder, args.profile)
            profiler = cProfile.Profile()
            profiler.runcall(run_in_process, lock, pythons, args.explain)
            pstats.Stats(profiler).sort_stats("tottime").print_stats(25)
            return
        print(f"-t everything, -p {' '.join(pythons)}{', --explain' if args.explain else ''}; best of {args.repeat}")
        print("| packages | wheels | seconds |")
        print("| ---: | ---: | ---: |")
        for n in args.sizes:
            lock = write_lockfile(folder, n)
            wheels = lock.read_text(encoding="utf-8").count(".whl")
            print(f"| {n} | {wheels} | {time_subprocess(lock, pythons, args.explain, args.repeat):.2f} |", flush=True)


if __name__ == "__main__":
    main()
