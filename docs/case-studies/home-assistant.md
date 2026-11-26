# Home Assistant Core: four PyPI gaps in the container requirements, covered by the project's own wheels

[Home Assistant Core](https://github.com/home-assistant/core) is an open-source home-automation platform. This study
pins revision
[`9882010fb2e5f5b571a9cfa3336661b59d16deca`](https://github.com/home-assistant/core/tree/9882010fb2e5f5b571a9cfa3336661b59d16deca)
(the `dev` branch, version 2027.1.0.dev0, committed on 26 November 2026), uses **wheelgap 0.14.0**, and records package
availability checked on **27 November 2026**.

The result is a little different from the other studies, because Home Assistant's container build does not rely on PyPI
alone. wheelgap reads public PyPI, so it reports **4 source builds** in the core requirements and **116 rows** (112
distinct pins) in the full set. Home Assistant's own [wheel build](https://github.com/home-assistant/core/blob/9882010fb2e5f5b571a9cfa3336661b59d16deca/.github/workflows/wheels.yml)
publishes wheels for the same pins on the same platforms, so those gaps describe PyPI, not what the container build
needs. This page shows both sides.

## Input, selection and targets

The container [`Dockerfile`](https://github.com/home-assistant/core/blob/9882010fb2e5f5b571a9cfa3336661b59d16deca/Dockerfile)
installs two checked-in files, both with `uv pip install --no-build`, so a package with no wheel fails the build
instead of compiling:

- [`requirements.txt`](https://github.com/home-assistant/core/blob/9882010fb2e5f5b571a9cfa3336661b59d16deca/requirements.txt),
  the core dependencies (61 packages, with its `-c homeassistant/package_constraints.txt` constraint file).
- [`requirements_all.txt`](https://github.com/home-assistant/core/blob/9882010fb2e5f5b571a9cfa3336661b59d16deca/requirements_all.txt),
  the core file plus every integration's pins (1,255 packages).

The targets come from three places:

- **Python 3.14**: the repo's [`.python-version`](https://github.com/home-assistant/core/blob/9882010fb2e5f5b571a9cfa3336661b59d16deca/.python-version)
  is 3.14.10, and the wheel build targets `cp314`.
- **`musl-x86_64` and `musl-aarch64`**: the
  [builder workflow](https://github.com/home-assistant/core/blob/9882010fb2e5f5b571a9cfa3336661b59d16deca/.github/workflows/builder.yml)
  builds `amd64` and `aarch64` images from `ghcr.io/home-assistant/{arch}-homeassistant-base:2026.10.3`. That base
  image's [Dockerfile](https://github.com/home-assistant/docker/blob/2026.10.3/Dockerfile) defaults to
  `base-python:3.14-alpine3.24`, and the wheel build tags its wheels `musllinux_1_2`. So Alpine, and wheelgap's default
  musl floor of 1.2. This study did not build or pull the image, so the Alpine version is the default named in that
  Dockerfile, not a measurement.

wheelgap's `--dockerfile` option can't read these: the project's `Dockerfile` takes its base image from a
`BUILD_FROM` argument, and wheelgap accepts only official Python images there (`--build-arg BUILD_FROM=ghcr.io/home-assistant/base-python:…`
is refused with "pass -p/-t instead"). So the targets above are set by hand.

## Reproduce

Download the pinned files into an empty directory. The constraint file keeps the path the requirements file names:

```bash
B=https://raw.githubusercontent.com/home-assistant/core/9882010fb2e5f5b571a9cfa3336661b59d16deca
mkdir homeassistant
curl -fL $B/requirements.txt -o requirements.txt
curl -fL $B/requirements_all.txt -o requirements_all.txt
curl -fL $B/homeassistant/package_constraints.txt -o homeassistant/package_constraints.txt
uvx --from wheelgap==0.14.0 python -m wheelgap requirements.txt \
  -p 3.14 -t musl-x86_64,musl-aarch64 --explain --suggest --no-cache --no-notes
```

wheelgap notes that four lines are unpinned (`certifi`, `packaging`, `typing-extensions`, `urllib3`), so it checked the
newest matching release for those. The scan exits **1**. Its table, trimmed to the four gap rows (the `--explain`
output for each follows below):

```text
wheelgap 0.14.0 · requirements.txt · 61 packages · Python 3.14

package            version  musl-x86_64  musl-aarch64
gazetteer-matcher  1.1.0    ● source     ● source
pymicro-vad        1.0.1    ● source     ● source
pyspeex-noise      1.0.2    ● source     ● source
PyTurboJPEG        1.8.3    ● source     ● source

57 packages with wheels for every target hidden (--all to show)
musl-x86_64   4 from source
musl-aarch64  4 from source
4 of 61 packages have gaps: 4 will build from source.
```

The same command on `requirements_all.txt` reports **116 of 1,255** packages with a source build on each target,
the same on both. The four above appear in it twice (once through the `-r requirements.txt` it includes, once under
the integration that pins them), which leaves 112 distinct name and version pins.

## The four core gaps

All four are source builds on both targets. `--explain --suggest` gives the reason for each:

| Package | What PyPI has for this pin | Newest release with a wheel that installs on musl |
| --- | --- | --- |
| gazetteer-matcher 1.1.0 | `abi3` wheels for macOS, Windows and glibc Linux, none for musl | none |
| pymicro-vad 1.0.1 | `cp39` to `cp312` wheels for glibc Linux and macOS, none for musl or 3.14 | none |
| pyspeex-noise 1.0.2 | no wheels, sdist only | none |
| PyTurboJPEG 1.8.3 | no wheels, sdist only | 2.5.0 (a `py3-none-any` wheel) |

```text
gazetteer-matcher 1.1.0 on musl-x86_64: ● source
  gazetteer_matcher-1.1.0-cp311-abi3-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl  manylinux_2_24_x86_64 is a glibc wheel; musl (Alpine) needs musllinux
  …
gazetteer-matcher 1.1.0: no newer release has a wheel for musl-x86_64, musl-aarch64

PyTurboJPEG 1.8.3 on musl-x86_64: ● source
  no wheels published for this version
PyTurboJPEG 1.8.3 -> 2.5.0 has wheels for musl-x86_64, musl-aarch64
```

pip agrees. For each pin, on each musllinux architecture:

```bash
pip download --no-deps --only-binary=:all: --platform musllinux_1_2_x86_64 \
  --python-version 3.14 --implementation cp --abi cp314 pymicro-vad==1.0.1
```

fails with `No matching distribution found` (the `aarch64` form too), and the same for the other three pins.

## How the container build gets wheels anyway

Home Assistant's [wheels workflow](https://github.com/home-assistant/core/blob/9882010fb2e5f5b571a9cfa3336661b59d16deca/.github/workflows/wheels.yml)
builds `musllinux_1_2` wheels for `cp314` on `amd64` and `aarch64`, and the result is listed publicly at
[wheels.home-assistant.io/musllinux/](https://wheels.home-assistant.io/musllinux/). Looking up the pins in that listing
on 27 November 2026:

| Package | File on the index |
| --- | --- |
| gazetteer-matcher 1.1.0 | `gazetteer_matcher-1.1.0-cp311-abi3-musllinux_1_2_x86_64.whl` and `…_aarch64.whl` |
| pymicro-vad 1.0.1 | `pymicro_vad-1.0.1-cp314-cp314-musllinux_1_2_x86_64.whl` and `…_aarch64.whl` |
| pyspeex-noise 1.0.2 | `pyspeex_noise-1.0.2-cp314-cp314-musllinux_1_2_x86_64.whl` and `…_aarch64.whl` |
| PyTurboJPEG 1.8.3 | `pyturbojpeg-1.8.3-py3-none-any.whl` |

The same lookup over all 112 distinct pins in `requirements_all.txt` finds a wheel for each: 103 are `py3-none-any`
wheels of packages that publish only an sdist on PyPI, and 9 are native packages with musllinux wheels (`aiokafka`,
`cached-ipaddress`, `ephem`, `gazetteer-matcher`, `py-nanotts`, `pyads`, `pyitachip2ir2`, `pymicro-vad` and
`pyspeex-noise`). The match used the pin's name and version and a tag that fits `cp314` on `musllinux_1_2` for both
architectures, or `py3-none-any`.

What this does not show: how the base image points `uv` at that index. This study read the workflow and the
Dockerfiles but did not pull or run the image, so it does not claim the index is what the container build installs
from, only that the wheels exist there and the PyPI-only gaps are closed by them. A user who installs these pins from
PyPI alone on Alpine would build them from source.

## What would fix it

For the container build, nothing: the pins already have wheels from the project's index. Outside it, on PyPI alone:

- PyTurboJPEG 1.8.3 → **2.5.0** ships a `py3-none-any` wheel, which installs on Alpine.
- gazetteer-matcher, pymicro-vad and pyspeex-noise have no newer release with a musllinux wheel. wheelgap links the
  trackers where a wheel can be requested: [pymicro-vad](https://github.com/rhasspy/pymicro-vad/issues) and
  [pyspeex-noise](https://github.com/rhasspy/pyspeex-noise/issues).

This study reports what wheelgap found and makes no claim about the project's packaging choices.
