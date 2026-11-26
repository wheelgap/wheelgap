# dlt: two runtime gaps on Python 3.14, both fixed one release up

[dlt](https://github.com/dlt-hub/dlt) is a data-loading library: it moves data from APIs and databases into
destinations such as warehouses and lakehouses. This study pins revision
[`84e87f8f13080f0d03f3cca6a804aec11fb324bd`](https://github.com/dlt-hub/dlt/tree/84e87f8f13080f0d03f3cca6a804aec11fb324bd)
(dlt 1.32.0), uses **wheelgap 0.14.0**, and records package availability checked on **25 November 2026**.

## Input, selection and targets

The input is the unchanged [uv.lock](https://github.com/dlt-hub/dlt/blob/84e87f8f13080f0d03f3cca6a804aec11fb324bd/uv.lock),
with its adjacent [pyproject.toml](https://github.com/dlt-hub/dlt/blob/84e87f8f13080f0d03f3cca6a804aec11fb324bd/pyproject.toml).
The library supports Python 3.10 to 3.14 (`requires-python = ">=3.10, <3.15"`). The
[main.yml workflow](https://github.com/dlt-hub/dlt/blob/84e87f8f13080f0d03f3cca6a804aec11fb324bd/.github/workflows/main.yml)
tests Python 3.10 through 3.14 on Ubuntu, plus 3.11 on Windows and macOS. This study checks the newest supported
Python, 3.14, on:

- `linux-x86_64`, with wheelgap's default glibc floor **2.36**. This is an explicit floor, not a measurement of the CI runner's libc.
- `win-amd64`.
- `macos-arm64` as an illustrative Apple-silicon target; the workflow's macOS label alone does not establish that runner's architecture.

The scan uses `--no-dev`, so it checks the runtime dependencies a user installs — the core library without its
destination extras — and leaves out the development groups.

## Reproduce

Download both pinned input files into an empty directory:

```bash
curl -fL https://raw.githubusercontent.com/dlt-hub/dlt/84e87f8f13080f0d03f3cca6a804aec11fb324bd/uv.lock -o uv.lock
curl -fL https://raw.githubusercontent.com/dlt-hub/dlt/84e87f8f13080f0d03f3cca6a804aec11fb324bd/pyproject.toml -o pyproject.toml
uvx --from wheelgap==0.14.0 python -m wheelgap uv.lock \
  -p 3.14 -t linux-x86_64,win-amd64,macos-arm64 --no-dev \
  --json --explain --suggest --no-cache > report.json
```

The scan exited **1**. Its JSON summary, one row per target, was:

| Target | Wheel | Source | Missing | Unknown |
| --- | ---: | ---: | ---: | ---: |
| `linux-x86_64` | 36 | 1 | 0 | 0 |
| `win-amd64` | 37 | 1 | 1 | 0 |
| `macos-arm64` | 36 | 1 | 0 | 0 |

(The rows count 38 packages per target; colorama and pywin32 are `n/a` off Windows, so the 39th row is the second
gap.) The report contains **39 packages**: the runtime dependencies. Two have gaps, both because their wheels stop at
CPython 3.13:

```text
package  version  linux-x86_64  win-amd64  macos-arm64
pywin32  310      –             ✗ missing  –
pyyaml   6.0.2    ● source      ● source   ● source
```

### PyYAML 6.0.2: source on all three targets

PyYAML 6.0.2 publishes wheels up to `cp313`, so on Python 3.14 pip builds it from the sdist everywhere:

```text
pyyaml 6.0.2 on linux-x86_64: ● source
  via: dlt → pyyaml
  PyYAML-6.0.2-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp313-cp313, not CPython 3.14
  PyYAML-6.0.2-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp312-cp312, not CPython 3.14
```

`--suggest` names the fix: **6.0.3** has CPython 3.14 wheels on all three targets, so `upgrade to 6.0.3` closes every
PyYAML row. Its C extension is optional: without a compiler or libyaml's headers the build falls back to pure Python,
so the install usually still works, but it runs a build first and can end up without the faster libyaml-based loader.

### pywin32 310: missing on win-amd64

pywin32 is a Windows-only dependency (`n/a` on the other two targets). Release 310 publishes wheels up to `cp313` and
no sdist pip can build, so on Python 3.14 there is nothing to install at all:

```text
pywin32 310 on win-amd64: ✗ missing
  via: dlt → pywin32
  pywin32-310-cp313-cp313-win_amd64.whl  built for cp313-cp313, not CPython 3.14
  pywin32-310-cp312-cp312-win_amd64.whl  built for cp312-cp312, not CPython 3.14
```

`--suggest` again names the fix one release up: **311** has a CPython 3.14 win-amd64 wheel.

## What would fix it

Both gaps close with small bumps in the lock: PyYAML 6.0.2 → **6.0.3** and pywin32 310 → **311**. Neither requires a
different package or a platform change. dlt's Windows CI job pins Python 3.11, where both pins have wheels, so the
gaps only appear for a user who installs dlt on Python 3.14 — which its `requires-python` allows. This study reports
what wheelgap found and makes no claim about the project's release choices.
