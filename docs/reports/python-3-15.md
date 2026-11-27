# What breaks when you install real Python projects on 3.15 (and 3.15t)

*Published 25 November 2026. Data as of 23 November 2026, 45 days after Python 3.15.0.*

To check **your own** project instead of reading these totals, follow
[Python 3.15 readiness in one command](../python-3-15-ready.md).

Python 3.15.0 came out on 9 October 2026. On 23 November, 45 days later, we checked two things:

1. **The top of PyPI**: the latest release of each of the 4,999 most-downloaded packages, on Python 3.14, 3.15 and free-threaded 3.15t, for Linux x86-64 and aarch64, macOS arm64 and Windows x64.
2. **Real projects**: 83 pinned dependency sets (67 `uv.lock`, 8 `poetry.lock`, 7 pinned `requirements.txt`, 1 `Pipfile.lock`) from the 300 most-starred Python repositories on GitHub.

A **gap** here is a package with no wheel that pip or uv can use for that Python and platform, and native code to build. pip and uv then compile it from source, which needs a compiler and headers. Some gaps have no sdist either, so they can't be installed at all. Pure-Python sdists are not gaps, because they install fine. That distinction matters. On 3.15 Linux x86-64, 355 top packages have only an sdist to offer, and 173 of them (docopt and pyspark, for example) are pure Python. Counting those as gaps would take the total from 226 to 399.

## The top of PyPI is mostly ready for 3.15

**Table 1.** Gaps in the top 4,999 packages (latest releases)

| Platform | 3.14 | 3.15 | of which had a 3.14 wheel | 3.15t | of which had a 3.15 wheel | top 500: 3.15 / 3.15t |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Linux x86-64 | 88 | 226 (4.5%) | 138 | 438 (8.8%) | 212 | 8 / 32 |
| Linux aarch64 | 131 | 263 (5.3%) | 132 | 461 (9.2%) | 198 | 8 / 32 |
| macOS arm64 | 187 | 310 (6.2%) | 123 | 508 (10.2%) | 198 | 9 / 33 |
| Windows x64 | 140 | 267 (5.3%) | 127 | 477 (9.5%) | 210 | 9 / 34 |

Every package in the top 100 now has a 3.15 wheel on all four platforms. The last one to arrive was PyYAML (rank 12), whose 6.0.4 release shipped cp315 wheels on 20 November.

The top 500 has 8 gaps on Linux x86-64, and they aren't all the same problem:

- **zstandard 0.25.0** has 3.14 wheels but no 3.15 wheel on any platform. lz4, fastuuid and ruamel-yaml-clib, and thrift just outside the top 500, have 3.14 wheels and none for 3.15 on all four platforms.
- **onnxruntime 1.32.0** has no 3.15 wheels and no sdist, so it can't be installed.
- **orjson 3.13.0** has cp315 Linux wheels, but they're `manylinux_2_39`. They work on Ubuntu 24.04 but not on Debian 12, whose glibc 2.36 is the floor we check.
- **psycopg2** 2.9.13 ships no Linux wheels; psycopg2-binary has 3.15 wheels. **backports-zstd** has no 3.14 or 3.15 wheels, but it backports a module that 3.14 added.

Of the 226 Linux x86-64 gaps, 138 had a 3.14 wheel, so they're new with 3.15 and need a rebuild. Coverage is creeping up: 4,579 of these packages had a 3.15 Linux x86-64 wheel on 9 November, and 4,600 on 23 November.

## Free-threaded 3.15t is about twice as far behind

3.15t Linux x86-64 has 438 gaps, including two in the top 100 (grpcio, watchfiles) and 32 in the top 500. 212 of those 438 do have a regular 3.15 wheel. For 159 of the 212, that wheel is abi3 (stable ABI) only, and pip and uv won't install abi3 on a free-threaded build. That covers watchfiles, psutil, hf-xet, tokenizers, pynacl, bcrypt, safetensors and duckdb. They already install from a wheel on regular 3.15. They're missing a free-threaded build.

## Real projects: 79 of 83 hit a gap on 3.15

**Table 2.** Projects with at least one gap

| Platform | 3.14 | 3.15 | 3.15t | 3.15: median gap packages | 3.15: clear if every gap package took its latest release |
| --- | ---: | ---: | ---: | ---: | ---: |
| Linux x86-64 | 43 | 79 | 81 | 12 | 29 of 79 |
| Linux aarch64 | 46 | 80 | 81 | 12 | 30 of 80 |
| macOS arm64 | 49 | 79 | 81 | 12 | 29 of 79 |
| Windows x64 | 42 | 79 | 81 | 12 | 30 of 79 |

On 3.15 Linux x86-64, 79 of the 83 projects would compile at least one package or fail to install. The median project has 12 such packages, and 28 projects have a 3.15 gap but none on 3.14. Table 1 looks much better than this because it checks latest releases, while lockfiles pin whatever was current when they were last updated.

## Mostly, the fix already exists upstream

**Table 3.** Packages behind the most gaps (projects affected, any of the four platforms)

| Package | 3.15 | 3.15t | Latest release (date) | 3.15 wheel in latest | 3.15t wheel in latest |
| --- | ---: | ---: | --- | --- | --- |
| pyyaml | 69 | 69 | 6.0.4 (2026-11-20) | all four | all four |
| markupsafe | 47 | 47 | 3.0.4 (2026-10-02) | all four | all four |
| numpy | 37 | 37 | 2.5.5 (2026-11-15) | all four | all four |
| pydantic-core | 36 | 36 | 2.50.5 (2026-11-19) | all four | all four |
| cffi | 32 | 32 | 2.1.1 (2026-08-03) | all four | all four |
| orjson | 27 | 27 | 3.13.0 (2026-10-07) | macOS arm64, Windows x64 | none |
| cryptography | 0 | 37 | 50.0.2 (2026-09-30) | all four | all four |
| hf-xet | 0 | 37 | 1.8.0 (2026-10-30) | all four | none |
| psutil | 3 | 36 | 7.2.2 (2026-01-28) | all four | none |
| tokenizers | 0 | 31 | 0.23.3 (2026-10-09) | all four | none |

PyYAML causes a gap in 69 projects: 56 pin 6.0.3, 12 pin 6.0.2 and 1 pins 6.0.1. MarkupSafe shipped 3.15 wheels on 2 October, a week before Python 3.15.0, and 47 projects still pin an older release. Of the 20 packages behind the most 3.15 gaps, 17 have 3.15 wheels on all four platforms in their latest release. The three that don't are orjson, zstandard and onnxruntime. Counted by gap, 907 of 1,100 on Linux x86-64 (82%) are in packages whose latest release has the wheel.

Still, upgrading everything would clear only 29 of the 79 projects on Linux x86-64. The rest each have a package whose latest release still lacks a 3.15 wheel, and other pins can block an upgrade anyway. On 3.15t Linux x86-64, only 56% of gaps (905 of 1,610) have a fix upstream, and only 10 of 81 projects would be clear.

## What to do

1. **Re-lock first.** `uv lock --upgrade-package pyyaml` (or `--upgrade`), `poetry update pyyaml`, or `pip-compile --upgrade`. `wheelgap --fix` names the release with the wheel for each gap and prints the command for your lockfile.
2. **For 3.15t, look for abi3-only wheels.** A dependency in that state needs one free-threaded build, which is a concrete thing to ask its maintainers for.
3. **For packages with no upstream wheel**, wheelgap's [gap wheel index](https://wheelgap.github.io/wheels/) has unofficial wheels built from the PyPI sdist (not by the maintainers). For 3.15 it has 17 wheels for 11 package versions, including zstandard, lz4, thrift, orjson and scikit-image, on Linux x86-64 and Windows x64 only, and none yet for 3.15t. They fill 7 of the 226 top-package gaps on Linux x86-64, and every 3.15 Linux x86-64 gap in 7 of the 79 projects. It's a stopgap: tree-sitter's upstream 0.27.0 shipped 3.15 wheels on 23 November, after we'd built 0.26.1.

## Try it

[wheelgap](https://github.com/wheelgap/wheelgap) is open source (MIT). In a project folder (`uv.lock`, `poetry.lock`, `requirements.txt`, `pyproject.toml` and more):

```bash
uvx wheelgap -p 3.15,3.15t          # dependencies with no wheel, per platform
uvx wheelgap -p 3.15,3.15t --fix    # also: the upgrade that fixes each gap, then a gap wheel where there's none
```

Or paste a GitHub repository into <https://wheelgap.github.io/tracker/check/>. It reads your platforms from CI and Dockerfiles and names the release that fixes each gap.

## How we measured, and caveats

- **Top packages**: the wheelgap tracker's nightly scan of 23 November (wheelgap 0.9.0) over hugovk's top-pypi-packages list of 1 November. That list has 5,000 names, one of which isn't on PyPI. The scan checks latest releases against floors of glibc 2.36, musl 1.2 and macOS 14.0.
- **Pure vs native** comes from PyPI's file lists. For sdist-only releases we listed the archive's file names (nothing run) and treated C/C++/Cython/Rust/Fortran sources as native. That's a heuristic: some extensions are optional and fall back to pure Python. So a gap means "compiles or can't install", not always "fails".
- **Cross-checks**: every non-wheel cell on the 3.14 to 3.15t targets matched PyPI's file lists on 23 November except for one package (Requires-Python <3.14). On 60 sampled cells, pip 26.3 (`--dry-run --only-binary=:all:`) agreed every time.
- **Projects**: from non-archived GitHub repos with language Python and over 3,000 stars, fetched on 21 November. We left out 13 inputs that would give wrong answers: `uv.lock` files limited by `[tool.uv] environments`, and locks that pull from other indexes. The rest were checked with wheelgap's unreleased main branch, which reads a lockfile's recorded files as `uv sync` does. An earlier run on this corpus matched `uv sync` on all 129,259 cells it compared, but that run didn't cover 3.15t.
- Only four platforms are reported here. The nightly history starts on 9 November, and wheelgap's version changed on two of those nights, so the trend is approximate.
