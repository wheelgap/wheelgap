# MCP Python SDK: dependency wheel availability

## Scan details

- **Project:** [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- **Repository:** `modelcontextprotocol/python-sdk`
- **SDK commit checked:** `77001c96f9507be0f4d009b83210a384a5e08bda`
- **Scan date:** 2026-11-20
- **Scanner:** `wheelgap 0.13.0` (released on PyPI)
- **Lock file:** `uv.lock`
- **Python versions:** 3.11, 3.12, 3.13, 3.14, 3.15
- **Platforms:** `linux-x86_64` and `win-amd64`

The SDK's `.github/workflows/shared.yml` defines a CI matrix for Python 3.11–3.15 on `ubuntu-latest` and `windows-latest`. The wheelgap targets above represent Linux x86-64 and Windows x86-64 respectively. The SDK's `pyproject.toml` also declares `requires-python = ">=3.11"` and Python classifiers for 3.11 through 3.15. The workflow and project configuration were checked at the SDK commit listed above.

## Input and selection

The scan uses no `--group`, `--extra` or `--no-dev` flags, so it checks the whole lock: `mcp`'s runtime dependencies and extras, its default dependency groups (`dev` and `docs`), and the opt-in `translate` and `codegen` groups. CI's test job runs `uv sync --all-extras`, which installs the runtime dependencies, every extra and the default groups, but not `translate` or `codegen`. So a gap below that comes only through `translate` is in the lock but not in what CI installs. Each gap below names the group it comes through.

CI's matrix also has a `lowest-direct` leg (`uv sync --upgrade --resolution lowest-direct`), which resolves fresh instead of using the lock. A lockfile check can't model that leg, so this study covers only the `locked` leg.

## Scan summary

The scan examined 120 packages from the package index across 10 Python/platform combinations (five Python versions on two platforms).

- 115 packages had wheels for every requested target.
- 5 packages had at least one target without a compatible wheel.
- 18 non-index packages were skipped (editable, virtual, or Git dependencies).
- The scan reported 5 packages that would build from source.

A `source` result means wheelgap did not find a compatible published wheel for that package version and target. It does not, by itself, mean that a source build will fail.

## Gaps and `--explain --suggest` findings

The excerpts below are from the `--explain --suggest` output, trimmed: `…` marks rejected-wheel lines left out, so the shown lines and wheelgap's own "and N more" don't add up to every file. The scanner's suggestions are candidate versions with wheels for the listed targets; they are not automatic upgrades or proof of application compatibility. Any dependency change should be re-resolved and tested by the project.

### 1. `cffi==2.0.0`

**Affected targets:** Python 3.15 on Linux x86-64 and Windows x86-64.

**Group:** runtime. `mcp` depends on `pyjwt[crypto]`, which brings in `cryptography` and then `cffi` on every target; on Windows the `dev` group's `trio` also needs it.

Dependency paths reported:

- Linux: `mcp → pyjwt → cryptography → cffi`
- Windows: `mcp → trio → cffi`

```text
cffi 2.0.0 on 3.15 linux-x86_64: source
  via: mcp → pyjwt → cryptography → cffi
  cffi-2.0.0-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl   built for cp314-cp314, not CPython 3.15
  cffi-2.0.0-cp314-cp314t-manylinux2014_x86_64.manylinux_2_17_x86_64.whl  built for cp314-cp314t, not CPython 3.15
  cffi-2.0.0-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl   built for cp313-cp313, not CPython 3.15
  cffi-2.0.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl   built for cp312-cp312, not CPython 3.15
  cffi-2.0.0-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl   built for cp311-cp311, not CPython 3.15
  cffi-2.0.0-cp314-cp314-macosx_10_13_x86_64.whl                          macosx_10_13_x86_64 doesn't match linux-x86_64
  cffi-2.0.0-cp314-cp314-musllinux_1_2_x86_64.whl                         musllinux_1_2_x86_64 is a musl wheel; glibc Linux needs manylinux
  …
  and 51 more (--json lists them all)

cffi 2.0.0 on 3.15 win-amd64: source
  via: mcp → trio → cffi
  cffi-2.0.0-cp314-cp314-win_amd64.whl   built for cp314-cp314, not CPython 3.15
  cffi-2.0.0-cp314-cp314t-win_amd64.whl  built for cp314-cp314t, not CPython 3.15
  cffi-2.0.0-cp313-cp313-win_amd64.whl   built for cp313-cp313, not CPython 3.15
  cffi-2.0.0-cp312-cp312-win_amd64.whl   built for cp312-cp312, not CPython 3.15
  cffi-2.0.0-cp311-cp311-win_amd64.whl   built for cp311-cp311, not CPython 3.15
  cffi-2.0.0-cp314-cp314-win32.whl       win32 doesn't match win-amd64
  cffi-2.0.0-cp314-cp314-win_arm64.whl   win_arm64 doesn't match win-amd64
  …
  and 51 more (--json lists them all)

cffi 2.0.0 -> 2.1.0 has wheels for 3.15 linux-x86_64, 3.15 win-amd64
```

**What could address it:** Check whether the dependency constraints allow `cffi 2.1.0`, which wheelgap identified as having wheels for both affected targets. Re-resolve and test the dependency set before changing the lock file.

### 2. `forbiddenfruit==0.1.4`

**Affected targets:** Python 3.11, 3.12, 3.13, 3.14, and 3.15 on both Linux x86-64 and Windows x86-64 (all 10 targets).

**Group:** `dev` (default group; not a runtime dependency of `mcp`). Dependency path reported: `mcp → blockbuster → forbiddenfruit`.

```text
forbiddenfruit 0.1.4 on 3.11 linux-x86_64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4 on 3.11 win-amd64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4 on 3.12 linux-x86_64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4 on 3.12 win-amd64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4 on 3.13 linux-x86_64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4 on 3.13 win-amd64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4 on 3.14 linux-x86_64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4 on 3.14 win-amd64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4 on 3.15 linux-x86_64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4 on 3.15 win-amd64: source
  via: mcp → blockbuster → forbiddenfruit
  no wheels published for this version

forbiddenfruit 0.1.4: no newer release has a wheel for 3.11 linux-x86_64, 3.11 win-amd64, 3.12 linux-x86_64, 3.12 win-amd64, 3.13 linux-x86_64, 3.13 win-amd64, 3.14 linux-x86_64, 3.14 win-amd64, 3.15 linux-x86_64, 3.15 win-amd64
```

**What could address it:** The scan found no newer release with wheels for any of the tested targets. Options to investigate are whether the upstream dependency can publish wheels, whether the dependency can be removed or replaced, or whether building it from source is an acceptable project choice. The scan does not establish that any of these changes is safe or compatible.

### 3. `jiter==0.16.0`

**Affected targets:** Python 3.15 on Linux x86-64 and Windows x86-64.

**Group:** `translate` (opt-in group; not a runtime dependency of `mcp`, and CI's `uv sync --all-extras` doesn't install it). Dependency path reported: `mcp → anthropic → jiter`.

```text
jiter 0.16.0 on 3.15 linux-x86_64: source
  via: mcp → anthropic → jiter
  jiter-0.16.0-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp314-cp314, not CPython 3.15
  jiter-0.16.0-cp314-cp314t-manylinux_2_17_x86_64.manylinux2014_x86_64.whl built for cp314-cp314t, not CPython 3.15
  jiter-0.16.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp313-cp313, not CPython 3.15
  jiter-0.16.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp312-cp312, not CPython 3.15
  jiter-0.16.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp311-cp311, not CPython 3.15
  …
  and 70 more (--json lists them all)

jiter 0.16.0 on 3.15 win-amd64: source
  via: mcp → anthropic → jiter
  jiter-0.16.0-cp314-cp314-win_amd64.whl   built for cp314-cp314, not CPython 3.15
  jiter-0.16.0-cp314-cp314t-win_amd64.whl  built for cp314-cp314t, not CPython 3.15
  jiter-0.16.0-cp313-cp313-win_amd64.whl   built for cp313-cp313, not CPython 3.15
  jiter-0.16.0-cp312-cp312-win_amd64.whl   built for cp312-cp312, not CPython 3.15
  jiter-0.16.0-cp311-cp311-win_amd64.whl   built for cp311-cp311, not CPython 3.15
  …
  and 70 more (--json lists them all)

jiter 0.16.0 -> 0.17.0 has wheels for 3.15 linux-x86_64, 3.15 win-amd64
```

**What could address it:** Check whether the dependency constraints allow `jiter 0.17.0`, then re-resolve and test. This is a candidate suggested by wheelgap, not a confirmed compatible upgrade.

### 4. `markupsafe==3.0.2`

**Affected targets:** Python 3.14 and 3.15 on Linux x86-64 and Windows x86-64.

**Group:** `docs` (default group; not a runtime dependency of `mcp`). Dependency path reported: `mcp → mkdocstrings → markupsafe`.

```text
markupsafe 3.0.2 on 3.14 linux-x86_64: source
  via: mcp → mkdocstrings → markupsafe
  MarkupSafe-3.0.2-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl   built for cp313-cp313, not CPython 3.14
  MarkupSafe-3.0.2-cp313-cp313t-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp313-cp313t, not CPython 3.14
  MarkupSafe-3.0.2-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl   built for cp312-cp312, not CPython 3.14
  MarkupSafe-3.0.2-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl   built for cp311-cp311, not CPython 3.14
  …
  and 32 more (--json lists them all)

markupsafe 3.0.2 on 3.14 win-amd64: source
  via: mcp → mkdocstrings → markupsafe
  MarkupSafe-3.0.2-cp313-cp313-win_amd64.whl   built for cp313-cp313, not CPython 3.14
  MarkupSafe-3.0.2-cp313-cp313t-win_amd64.whl  built for cp313-cp313t, not CPython 3.14
  MarkupSafe-3.0.2-cp312-cp312-win_amd64.whl   built for cp312-cp312, not CPython 3.14
  MarkupSafe-3.0.2-cp311-cp311-win_amd64.whl   built for cp311-cp311, not CPython 3.14
  …
  and 32 more (--json lists them all)

markupsafe 3.0.2 on 3.15 linux-x86_64: source
  via: mcp → mkdocstrings → markupsafe
  MarkupSafe-3.0.2-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl   built for cp313-cp313, not CPython 3.15
  MarkupSafe-3.0.2-cp313-cp313t-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp313-cp313t, not CPython 3.15
  MarkupSafe-3.0.2-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl   built for cp312-cp312, not CPython 3.15
  MarkupSafe-3.0.2-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl   built for cp311-cp311, not CPython 3.15
  …
  and 32 more (--json lists them all)

markupsafe 3.0.2 on 3.15 win-amd64: source
  via: mcp → mkdocstrings → markupsafe
  MarkupSafe-3.0.2-cp313-cp313-win_amd64.whl   built for cp313-cp313, not CPython 3.15
  MarkupSafe-3.0.2-cp313-cp313t-win_amd64.whl  built for cp313-cp313t, not CPython 3.15
  MarkupSafe-3.0.2-cp312-cp312-win_amd64.whl   built for cp312-cp312, not CPython 3.15
  MarkupSafe-3.0.2-cp311-cp311-win_amd64.whl   built for cp311-cp311, not CPython 3.15
  …
  and 32 more (--json lists them all)

markupsafe 3.0.2 -> 3.0.4 has wheels for 3.14 linux-x86_64, 3.14 win-amd64, 3.15 linux-x86_64, 3.15 win-amd64
```

**What could address it:** Check whether the dependency constraints allow `MarkupSafe 3.0.4`, then re-resolve and test the SDK's dependency set.

### 5. `pyyaml==6.0.2`

**Affected targets:** Python 3.14 and 3.15 on Linux x86-64 and Windows x86-64.

**Group:** `docs` (default group; not a runtime dependency of `mcp`). Dependency path reported: `mcp → pyyaml`.

```text
pyyaml 6.0.2 on 3.14 linux-x86_64: source
  via: mcp → pyyaml
  PyYAML-6.0.2-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp313-cp313, not CPython 3.14
  PyYAML-6.0.2-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp312-cp312, not CPython 3.14
  PyYAML-6.0.2-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp311-cp311, not CPython 3.14
  …
  and 19 more (--json lists them all)

pyyaml 6.0.2 on 3.14 win-amd64: source
  via: mcp → pyyaml
  PyYAML-6.0.2-cp313-cp313-win_amd64.whl  built for cp313-cp313, not CPython 3.14
  PyYAML-6.0.2-cp312-cp312-win_amd64.whl  built for cp312-cp312, not CPython 3.14
  PyYAML-6.0.2-cp311-cp311-win_amd64.whl  built for cp311-cp311, not CPython 3.14
  …
  and 19 more (--json lists them all)

pyyaml 6.0.2 on 3.15 linux-x86_64: source
  via: mcp → pyyaml
  PyYAML-6.0.2-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp313-cp313, not CPython 3.15
  PyYAML-6.0.2-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp312-cp312, not CPython 3.15
  PyYAML-6.0.2-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  built for cp311-cp311, not CPython 3.15
  …
  and 19 more (--json lists them all)

pyyaml 6.0.2 on 3.15 win-amd64: source
  via: mcp → pyyaml
  PyYAML-6.0.2-cp313-cp313-win_amd64.whl  built for cp313-cp313, not CPython 3.15
  PyYAML-6.0.2-cp312-cp312-win_amd64.whl  built for cp312-cp312, not CPython 3.15
  PyYAML-6.0.2-cp311-cp311-win_amd64.whl  built for cp311-cp311, not CPython 3.15
  …
  and 19 more (--json lists them all)

pyyaml 6.0.2 -> 6.0.3 has wheels for 3.14 linux-x86_64, 3.14 win-amd64
pyyaml 6.0.2: no newer release has a wheel for 3.15 linux-x86_64, 3.15 win-amd64
```

**What could address it:** `PyYAML 6.0.3` is a candidate for the Python 3.14 gaps. The scan found no newer release with wheels for the Python 3.15 targets. A dependency update alone therefore does not remove all reported PyYAML gaps in this target set; investigate source builds, upstream wheel availability, or the project's supported Python range.

## Notes and limitations

- Only `cffi` is a runtime dependency of `mcp`; the other gaps come through its development, documentation and translation groups, so they affect contributors and CI rather than users who `pip install mcp`.
- These findings describe wheel availability for the versions recorded in this lock file and the selected targets on the scan date. They are not a claim that the SDK cannot install or run on those targets.
- A missing wheel means installation may need a source build. Whether that succeeds depends on the package, build tooling, compiler availability, and platform environment.
- The `--suggest` versions are wheel-availability candidates only. Confirm version constraints, runtime compatibility, and tests before adopting them.
- The scan skipped 18 non-index dependencies; the summary above applies to the 120 packages it scanned from the package index.

## Reproduction

Download the pinned input files into an empty directory and run wheelgap 0.13.0 on them:

```bash
curl -fL https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/77001c96f9507be0f4d009b83210a384a5e08bda/uv.lock -o uv.lock
curl -fL https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/77001c96f9507be0f4d009b83210a384a5e08bda/pyproject.toml -o pyproject.toml
uvx --from wheelgap==0.13.0 python -m wheelgap uv.lock \
  -p 3.11,3.12,3.13,3.14,3.15 -t linux-x86_64,win-amd64 \
  --explain --suggest --no-cache
```

Linux is modelled as x86-64 with glibc 2.36, wheelgap's default floor. With gaps reported, the scan exits 1.

## References

- [MCP Python SDK repository](https://github.com/modelcontextprotocol/python-sdk)
- [CI matrix at the scanned commit](https://github.com/modelcontextprotocol/python-sdk/blob/77001c96f9507be0f4d009b83210a384a5e08bda/.github/workflows/shared.yml)
- [Project configuration at the scanned commit](https://github.com/modelcontextprotocol/python-sdk/blob/77001c96f9507be0f4d009b83210a384a5e08bda/pyproject.toml)
- [wheelgap 0.13.0 on PyPI](https://pypi.org/project/wheelgap/0.13.0/)
