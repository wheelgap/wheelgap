---
name: check-wheels
description: Check whether a Python project's dependencies have prebuilt wheels for the platforms it ships to (Linux ARM64/Graviton, Alpine/musl, Windows on ARM, macOS, free-threaded 3.14t, Pyodide, iOS/Android), using wheelgap. Use when a Docker or CI build is slow or fails compiling a Python package ("Failed building wheel for", "Microsoft Visual C++ 14.0 or greater is required", "command 'gcc' failed"), when moving to ARM, Alpine or a new Python version, or when asked "will our dependencies install on X?"
license: MIT
---

# Check wheels with wheelgap

wheelgap reads a lockfile and reports, per package and target platform, whether pip/uv would install a prebuilt **wheel**, build from **source** (slow, needs a compiler), or find nothing (**missing**). Lockfiles that record their files (`uv.lock`, `pylock.toml`, `poetry.lock`, `pdm.lock`) are checked offline in about a second; `requirements*.txt`, `Pipfile.lock`, `pixi.lock` and package names are looked up on PyPI.

## If the wheelgap MCP server is available

If the `wheelgap` MCP server is connected, prefer its tools to the command line: `check_lockfile`, `check_dockerfile`, `diff_lockfiles`, `check_package` (one package, latest or a given version) and `list_targets`. Their results are shaped differently from `--json`: `check_lockfile` and `check_dockerfile` return `ok` (true only when no package has a gap) and `gaps` (just the packages with one, each with its `results` and `error`); `diff_lockfiles` returns `worse`, `new` and `ok`. `check_lockfile`'s `passes` is the command line's verdict, which also accepts gaps on `advisory` targets and, with `fail-on = "missing"`, source builds. `check_package` takes `explain: true` to add `rejected`, and `dependencies: true` to also check everything the package installs, as `--deps` does: `dependency_gaps` lists each package in the tree with a gap, with its `results`, `gaps` and `via` path, and `ok` then covers the whole tree. Statuses, `unknown`, `skipped` and the advice below apply as for the command line. Pass paths as absolute paths or `file://` URIs, since the server's working directory may not be the project. The tools take no `--no-dev`, `--group` or `--extra` arguments (`check_lockfile` still applies `groups` and `extras` from `[tool.wheelgap]`), so when you need a selection the project doesn't configure, run the command line instead. To set it up: `claude mcp add wheelgap -- uvx --from wheelgap wheelgap-mcp` (other clients: [MCP setup guide](https://github.com/wheelgap/wheelgap/blob/main/docs/mcp.md)); `uvx wheelgap@latest mcp` starts the same server. If uvx says "An executable named `wheelgap-mcp` is not provided", it's reusing a copy of wheelgap from before 0.9.0: run `uvx --refresh --from wheelgap wheelgap-mcp --version` once.

## Run it

```bash
uvx wheelgap@latest -p 3.13 -t linux-aarch64,musl-aarch64 --json
```

- **Input:** in the project directory, wheelgap finds `uv.lock`, `pylock.toml`, `poetry.lock`, `pdm.lock`, `Pipfile.lock`, `requirements.txt`, `requirements.lock` or `pixi.lock` (the first one present). Pass any other file by path, e.g. `uvx wheelgap@latest requirements-prod.txt …`. For `pixi.lock`, only the PyPI packages of one environment are checked (`--environment NAME`, default `default`); conda packages are `skipped`. A library with no lockfile: pass its `pyproject.toml` (direct dependencies only, unless you add `--deps`; add `--extra NAME` or `--all-extras`). To ask whether `pip install NAME` works on a platform, pass the name with `--deps`, which also checks everything it installs, resolved per target from PyPI's metadata, and `--explain` names the path to each gap (`uvx wheelgap@latest mcp==2.3.0 --deps -p 3.14 -t win-arm64 --explain` gives `via: mcp → PyJWT → cryptography`). Pin a version for a name that's also a subcommand (`mcp`, `diff`). `--deps` also resolves the whole tree a requirements file or `pyproject.toml` installs (a fully pinned `pip-compile` file is checked as written). It isn't pip's resolver: where pip would backtrack it stops with an error, so use a lockfile then.
- **Python (`-p`):** always pass the project's Python, from `.python-version`, `requires-python`, the CI matrix or the Dockerfile. Without `-p`, wheelgap uses `.python-version`, else the Python it runs on, which may be free-threaded (3.14t) and would report gaps an ordinary project doesn't have. Use `-p 3.14t` only to check free-threading readiness. To see what blocks a move to the newest CPython, check the project's Python next to it (`-p 3.13,next`; `next-t` for its free-threaded build): `blocking_newer_python` lists the packages with a gap only on the newer one.
- **Platforms (`-t`):** use what the project ships to.
  - A Dockerfile: `uvx wheelgap@latest --dockerfile Dockerfile --json` takes both Python and platform from the final stage (global `ARG` defaults included); if the build passes `--build-arg NAME=VALUE`, pass the same `--build-arg`.
  - A `[tool.wheelgap]` table in `pyproject.toml`: its defaults apply with no `-p`/`-t`.
  - Otherwise run `uvx wheelgap@latest --suggest-targets --json` and confirm its suggestions with the user: it reads the targets and Pythons the project's GitHub Actions workflows and Dockerfiles name, each with its file and line, lists what it couldn't read, and checks nothing. Failing that, ask, or use `-t all` (the eight common platforms).
  - `uvx wheelgap@latest --list-targets` lists every target and alias.
- **Libc:** if the user names a deployment distro, pass `--distro NAME` (e.g. `ubuntu-22.04`, `debian-12`, `rhel-9`, `amazonlinux-2023`, `alpine`; `--list-targets` lists them). For a distro that isn't listed, verify its libc version and pass `--glibc X.Y` or `--musl X.Y`. None of these select platforms; Alpine still needs a `musl-*` target. With `--dockerfile`, leave `--distro` out: the image already sets the libc, and `--distro` would override it.

## Read the result

- **Don't rely on the exit code alone:** `0` no failing gaps, `1` gaps, `2` bad input (with `--json`, stdout is then `{"error": {"message": ..., "type": ...}}`; the message is on stderr too). `--exit-zero` or `fail-on = "missing"` can make source builds exit 0, so read `summary.with_gaps` and each package's `results`.
- **`results`** maps a target label to `wheel`, `source`, `missing`, `unknown`, `n/a` (the package isn't installed on that target, per its markers) or `bundled` (shipped by Pyodide, with `--pyodide-lock`). Labels come from `targets[].label`: the platform alone with one Python, `"3.13 linux-x86_64"` with several.
- **`unknown`** means a lookup failed; the package's `error` field says why. Never report it as fine.
- **`skipped`** lists packages that weren't checked (editable, git, path), so don't claim every dependency was covered.
- **Installation scope:** with no selection, `uv.lock`, `poetry.lock` and `pdm.lock` checks conservatively include extras and development groups. To match a deployment, pass its selection: `--no-dev`, `--group NAME`, `--only-group NAME` (uv.lock, Poetry 2.x poetry.lock, pdm.lock) and `--extra NAME` (uv.lock, Poetry 2.x poetry.lock). Otherwise compare a reported gap with how the project is really installed before treating it as a deployment failure.
- **Why:** `--explain` adds each rejected wheel and the reason (`rejected`), and for `uv.lock` the dependency path that pulls the package in (`via`). For a package from PyPI that publishes an issue tracker, it also links it (`ask for a wheel: <url>`; `issues_url` in `--json`, `null` when there's none). The text and Markdown tables name the direct dependency behind a transitive gap, such as `cryptography (via mcp)` (with `+N` when more lead to it); `--json --explain` has the full path in `via` instead. `--explain --notes` adds the [coverage tracker](https://wheelgap.github.io/tracker/)'s known workarounds (`notes`), such as a binary package to use instead; it sends the names of packages with gaps to wheelgap.github.io.

## What to tell the user

1. Name the packages with gaps, per platform, with the reason from `--explain`, and which dependency pulls each in.
2. Offer concrete fixes:
   - pin a version that ships the wheel (`--explain --suggest` names the nearest newer release with one, `suggest` in `--json`; when no newer release has one, it names the last older one that did, `last_with_wheel`; `--deps --explain` turns it on by default, `--no-suggest` turns it off). `--write-constraints FILE` also writes those pins as a pip/uv constraints file; it isn't checked against the rest of the tree, so check it with `pip install -c FILE …` or `uv pip compile -c FILE …`;
   - use a `-slim` Debian base instead of Alpine;
   - add build dependencies to the Dockerfile on purpose (`--explain --build-info` says what each source build needs, such as a Rust toolchain or CMake; it downloads sdists);
   - `--fail-on missing` to accept source builds in CI.
3. To accept a known gap without hiding it, use `--ignore NAME` or `ignore = ["NAME"]` in `[tool.wheelgap]`, or `--ignore NAME:TARGET` to accept it on one platform only. To watch a platform the project doesn't support yet without failing on it, add it to `-t` and pass `--advisory TARGET` too (or `advisory = [...]`): its gaps are reported but don't fail the run.

## After a dependency upgrade

`uvx wheelgap@latest diff OLD NEW -p 3.13 -t linux-aarch64 --json` lists the packages that got worse on a target and the new packages with gaps (exit 1 if any). `OLD` can be a git ref such as `origin/main:uv.lock` or `HEAD:uv.lock` (the path is relative to the repository root; fetch the base ref first in a shallow CI checkout); a package that can no longer be checked counts as worse. `[tool.wheelgap]` settings don't apply to `diff`, so always pass `-p` and `-t` (and `--distro` or `--glibc` for an older libc). In GitHub Actions, the wheelgap Action's `compare-to: base` runs this on each pull request.

## Before a release

For a package the project publishes, `uvx wheelgap@latest --dist dist -p 3.13 -t win-arm64,linux-aarch64 --json` checks the wheels and sdist about to be uploaded and fails when a target the previous release on PyPI had a wheel for has none in these files (`dropped` in `--json`). The GitHub Action's `dist: dist` input runs the same check before the upload step. `--badge PATH` also writes a shields.io endpoint file with how many targets have wheels for everything (`7/8 targets`), even when the check fails, for a README badge.

wheelgap checks wheel availability only, not whether a package works without the GIL.

For a quick look at one popular package without running anything, fetch `https://wheelgap.github.io/tracker/data/packages/<normalized-name>.json`. That's a nightly scan of the **latest release** of the top 5,000 PyPI packages, not the version a project pins.
