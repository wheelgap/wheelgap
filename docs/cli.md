# Command line

```text
wheelgap [OPTIONS] [INPUT ...]
wheelgap diff [-p VER] [-t PLATFORM] [--glibc X.Y] [--musl X.Y] [--distro NAME] [--macos X.Y] [--json] OLD NEW
wheelgap mcp            (after 0.9.0; or wheelgap-mcp)
```

`INPUT` is a lockfile, a requirements file, a `pyproject.toml`, a directory, or package specs such as `numpy==2.3.4`
(default: the current directory). See [Inputs](inputs.md).

Flags that take a list (`-p`, `-t`, `--extra`, `--group`, `--only-group`, `--ignore`, and after 0.9.0 `--advisory`)
can be repeated; `-p`, `-t`, `--ignore` and `--advisory` also take comma-separated values. Flags win over
[`[tool.wheelgap]`](configuration.md).

`wheelgap --help` lists the flags of the version you run. This page describes `main`; flags marked **after 0.9.0**
aren't in a release yet.

## Targets

### `-p`, `--python` {#python}

`-p VER`: a target Python, such as `3.13`, or `3.14t` for a free-threaded build. Repeatable and comma-separated:
`-p 3.13,3.14t`. Default: the image's Python with `--dockerfile`, then `python` in `[tool.wheelgap]`, then a
`.python-version` file next to the input, then the Python wheelgap runs on.

After 0.9.0, `next` and `next-t` are the newest CPython this wheelgap knows and its free-threaded build. See
[Python versions](targets.md#python-versions).

### `-t`, `--target` {#target}

`-t PLATFORM`: a target platform, by name, alias or Docker platform name (`linux/arm64`). Repeatable and
comma-separated. `all` is the eight common native platforms, `everything` adds armv7l, ppc64le, s390x, riscv64 and
i686 Linux and `win32`, and `mobile` is iOS and Android. Default: `linux-x86_64,linux-aarch64,macos-arm64,win-amd64`,
unless `--dockerfile`, `[tool.wheelgap] targets` or uv's `required-environments` say otherwise. See
[Targets](targets.md).

### `--glibc` {#glibc}

`--glibc X.Y`: the glibc version of `linux-*` targets (default 2.36). Wheels for a newer glibc are rejected.

### `--musl` {#musl}

`--musl X.Y`: the musl version of `musl-*` targets (default 1.2). For Alpine older than 3.13, pass `--musl 1.1`.

### `--distro` {#distro}

`--distro NAME`: take the glibc or musl version from a distro, such as `ubuntu-22.04`, `debian-12`, `rhel-9`,
`amazonlinux-2023` or `alpine`. It doesn't select targets: keep using `-t`. See [Distros](targets.md#distros).

### `--macos` {#macos}

`--macos X.Y`: the minimum macOS of `macos-*` targets (default 14.0).

### `--ios` {#ios}

`--ios X.Y`: the minimum iOS of `ios-*` targets (default 13.0).

### `--android-api` {#android-api}

`--android-api N`: the minimum Android API level of `android-*` targets (default 24).

### `--dockerfile` {#dockerfile}

`--dockerfile PATH`: take the Python, the platform and the glibc or musl version from the image of the Dockerfile's
final stage, such as `FROM python:3.13-alpine`. `-p`, `-t`, `--glibc` and `--musl` still win. See
[From a Dockerfile](targets.md#from-a-dockerfile).

### `--pyodide-lock` {#pyodide-lock}

`--pyodide-lock PATH_OR_HTTPS_URL`: a Pyodide release's `pyodide-lock.json`. Packages Pyodide ships itself count as
`✓ bundled` on `-t pyodide`. See [Pyodide](targets.md#pyodide).

### `--advisory` {#advisory}

**After 0.9.0.** `--advisory TARGET`: report gaps on `TARGET` without letting them fail the run.

Its gaps are reported as usual but never fail the run: the column is headed `win-arm64 (advisory)` in the table and the
Markdown report, the summary includes a line like
`3 gaps in 2 packages on advisory targets linux-aarch64, win-arm64 don't fail the run.` (it counts each package once
per advisory target, while the line above it counts packages), and `--json` lists the advisory target labels under
`advisory`, while `packages`, `summary.with_gaps` and `summary.per_target` still count those gaps.

- Advisory works per platform, so it marks every Python checked on that platform (`"3.13 win-arm64"` and
  `"3.14t win-arm64"`): a free-threaded `3.14t` can't be made advisory on its own.
- It takes anything `-t` does (aliases like `windows-arm64`, Docker names like `linux/arm64`), repeated or
  comma-separated, and replaces the configured `advisory` list (`--advisory ""` clears it for one run).
- It only marks targets, so a target must also be checked: an `--advisory` target that isn't is an error (exit 2),
  while a configured one that a run doesn't check gets a note on stderr.
- A group (`all`, `everything`, `mobile`) means whichever of its targets the run checks, so `--advisory all` makes
  every checked target advisory, and it's an error only when the run checks none of them. When every checked target
  is advisory, the run can't fail, not even when a lookup fails during an index outage, so a note on stderr says so
  (`-q` hides it).
- With `--fail-on missing`, a source build on a required target still passes and a missing package on one still
  fails, whatever the advisory targets show. An `--ignore NAME:TARGET` gap on an advisory target stays ignored and
  isn't counted as an advisory gap.
- In `--junit` reports an advisory gap is a skipped testcase with the message `advisory: source` (or `missing`,
  `unknown`), not a failure.
- `--advisory` (after 0.9.0) doesn't work with `wheelgap diff` yet.

## Dependency selection

These select which dependencies of a `uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock` or `pyproject.toml` count. See
[Inputs](inputs.md#uvlock) for the full rules per format.

### `--extra` {#extra}

`--extra NAME`: include a uv project extra, or a `pyproject.toml` `[project.optional-dependencies]` group, and after
0.9.0 a Poetry 2.x `poetry.lock` extra (like `poetry install --extras NAME`). Repeatable. Not supported for
`pdm.lock`.

### `--all-extras` {#all-extras}

Include every uv project extra (or every `pyproject.toml` extra, and after 0.9.0 every Poetry 2.x `poetry.lock`
extra).

### `--group` {#group}

`--group NAME`: include a dependency group (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`). Repeatable.

### `--no-dev` {#no-dev}

Leave out the `dev` group (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`).

### `--only-group` {#only-group}

`--only-group NAME`: check only the named dependency groups, without the project's own dependencies or default
groups. Repeatable; combine it with `--group` to add more groups, but not with `--extra` or `--all-extras`.

### `--environment` {#environment}

**After 0.9.0.** `--environment NAME`: the `pixi.lock` environment to check (default: `default`). It also makes a
directory's `pixi.lock` the lockfile read. Naming one for any other input is an error. See
[pixi.lock](inputs.md#pixilock).

## Index access

### `--online` {#online}

Re-check lockfile packages against PyPI instead of trusting the files the lockfile lists, for when a project has
uploaded new wheels for an existing version since you locked, or a `uv.lock` was made with `environments`
restrictions. Packages the lock says came from another index keep their locked files, so their names are never sent
to PyPI. Can't be combined with `--offline`.

### `--offline` {#offline}

Never contact an index, for CI and coding agents without network. Inputs that need a lookup (requirements files,
`pyproject.toml`, `Pipfile.lock`, package names, lockfile entries with no file list, and after 0.9.0 `pixi.lock`)
make it exit 2 with an error
naming the packages, so use a lockfile instead. A `--pyodide-lock` URL is refused too: pass a downloaded copy. The
[`requires-python` re-check](inputs.md#uvlock) is skipped, with a note that those Pythons may show false gaps.
`offline = true` in `[tool.wheelgap]` makes it the project default; `--online` overrides that for one run.

### `--no-cache` {#no-cache}

Don't read or save the 24-hour [cache](how-it-works.md#cache) of pinned package lookups for this run.

### `--clear-cache` {#clear-cache}

Delete the cache of pinned package lookups first. With no `INPUT`, do only that. It touches only wheelgap's own entry
files, and never follows a link.

### `--index-url` {#index-url}

`--index-url URL`: look packages up in this simple index (PEP 691 JSON) instead of PyPI's JSON API. Lockfiles that list
their files are still read offline unless `--online`. The URL must be https (plain http only for localhost).
Credentials can go in the URL (`https://user:token@host/simple`) and never appear in output; in CI,
`--index-url "$PRIVATE_INDEX"` keeps them out of your repo. An index that only serves HTML (PEP 503) gets a clear
error. See [Private indexes](how-it-works.md#private-indexes).

## Output

### `--all` {#all}

Show every package in the table, not just the ones with gaps.

### `--explain` {#explain}

For each gap, list the wheels that were rejected and why, closest misses first, and the dependency path that installs
the package where the input has one. With `--json`, adds `rejected` and `via` to each package. Text output can't
combine it with quiet mode. See [Output](output.md#explain).

After 0.9.0, a wheel that would fit but whose release was yanked is named with the reason `yanked`.

### `--conda-forge` {#conda-forge}

**After 0.9.0.** With `--explain`, also say when conda-forge has a package with exactly the same name and a build of
the same version that fits a gap's target. Only glibc Linux, macOS and Windows targets with a current conda-forge
Python can match, and it never changes a status, the summary or the exit code. Only names known to be on PyPI are sent
to anaconda.org; a run with `--index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag.
See [conda-forge hint](output.md#conda-forge) and [the PyPI-origin rule](how-it-works.md#pypi-origin).

### `--notes`, `--no-notes` {#notes}

**After 0.9.0.** With `--explain`, also show the [coverage tracker](https://wheelgap.github.io/tracker/)'s notes for a
package with a gap, such as a binary package to use instead. It sends the names of packages with gaps to
wheelgap.github.io, so it's opt-in, and only for names that pass [the PyPI-origin rule](how-it-works.md#pypi-origin);
never with `--offline`, `--index-url` or a `WHEELGAP_INDEX_URL` mirror. `notes = true` in `[tool.wheelgap]` turns it
on by default, and `--no-notes` turns it off for one run. See [Tracker notes](output.md#notes).

### `--build-info` {#build-info}

**After 0.9.0.** With `--explain`, also say what each `source` build needs (`build needs: at least a Rust toolchain
(maturin<2.0,>=1.9)`), from the `[build-system]` table of the sdist's `pyproject.toml`. It downloads sdists, so it's
opt-in, and only for packages that pass [the PyPI-origin rule](how-it-works.md#pypi-origin); it's off with `--offline`,
`--index-url`, `WHEELGAP_INDEX_URL` or a pip or uv index setting in the environment. `build-info = true` in
`[tool.wheelgap]` turns it on by default. See [Build requirements](output.md#build-info).

### `--suggest` {#suggest}

**After 0.9.0.** With `--explain`, also name the lowest newer release with a wheel for a pinned package's gap targets
(`fastlib 2.0.0 -> 2.1.0 has wheels for musl-aarch64, win-amd64`) that keeps the wheels the package has on its other
targets, or each target's nearest when no one release has them all. It looks each package with a gap up on pypi.org
once, only for names that pass [the PyPI-origin rule](how-it-works.md#pypi-origin), and never changes a status, the
summary or the exit code. A run with `--index-url` or `WHEELGAP_INDEX_URL` looks nothing up, `--offline` refuses the
flag, and it has no Markdown form yet. With `--json`, adds `suggest` to each package with a gap. See
[Newer releases](output.md#suggest).

### `-q`, `--quiet`, `--no-quiet` {#quiet}

Print only the gap summary and the affected package names. Clean runs without ignored packages are silent, and JSON
and Markdown output are unchanged. `--no-quiet` overrides `quiet = true` in `[tool.wheelgap]`.

### `--json` {#json}

Print JSON instead of the table. See [Output](output.md#json) for the shape. Can't be combined with `--markdown`.

### `--markdown` {#markdown}

Print a Markdown report instead of the table, for a job summary (`wheelgap --markdown >> "$GITHUB_STEP_SUMMARY"`) or
a pull request comment. See [Output](output.md#markdown).

### `--junit` {#junit}

`--junit PATH`: also write a JUnit XML report to `PATH`, alongside the usual output. See
[Output](output.md#junit-xml).

### `--github-annotations` {#github-annotations}

**After 0.9.0.** Also mark each package with a gap on its own line of the lockfile in GitHub Actions, as an
annotation. The GitHub Action on `main` passes it; see [CI](ci.md#annotations).

### `--color` {#color}

`--color {auto,always,never}`: color the table. `auto` (the default) colors a terminal, and respects the `NO_COLOR` and
`FORCE_COLOR` environment variables.

## Exit status

### `--ignore` {#ignore}

`--ignore NAME[:TARGET]`: accept a known gap. Matched packages are excluded from the table, counts and exit status,
and listed as ignored in text (including quiet mode), JSON and Markdown. Repeatable or comma-separated, with
case-insensitive package-name matching and equivalent hyphens, underscores and dots. Any `--ignore` flags replace the
configured `ignore` list rather than adding to it, and `--ignore ""` clears it for one run.

To accept a gap on one target only, add the target after a colon: `--ignore psycopg2:musl-x86_64` still checks
psycopg2 everywhere else. The target takes anything `-t` does (`alpine`, `linux/amd64`, `all`), one per entry
(`--ignore psycopg2:alpine,psycopg2:win-arm64`). An unknown target is an error (exit 2), and so is a second target
after a comma (`psycopg2:alpine,win-arm64`), which would otherwise read as a package name. The package keeps its real
status in the report (`● source (ignored)` in the table), but a gap on that target doesn't count towards the summary
or the exit status, isn't explained by `--explain`, and is a skipped testcase in `--junit`. Reports list it as
`ignored: psycopg2 (musl-x86_64)`, and `--json` puts it in `ignored_targets` (`ignored` keeps only packages ignored
everywhere). A target that isn't checked in this run gets a note on stderr.

### `--exit-zero` {#exit-zero}

Exit 0 even when gaps are found, for informational runs. Bad input still exits 2.

### `--fail-on` {#fail-on}

`--fail-on {source,missing}`: exit 1 on any gap (`source`, the default), or only when a package can't be installed
(`missing`). A package that couldn't be checked (`unknown`) exits 1 either way.

## Other

### `--list-targets` {#list-targets}

List the platforms, their aliases and compatibility limits (and the `--distro` names), then exit. With `--json`, print
the platforms as JSON. Can't be combined with `--markdown`.

### `--suggest-targets` {#suggest-targets}

**After 0.9.0.** `--suggest-targets [DIR]`: read the platforms and Pythons that `DIR` (default: the current directory)
already names in its GitHub Actions workflows and Dockerfiles, print each with the file and line it came from, then a
`[tool.wheelgap]` snippet to paste into `pyproject.toml`. It only suggests: nothing is changed or checked, and it exits
0. With `--json`, it prints the same as an object. It can't be combined with `-p`, `-t`, `--dockerfile`,
`--list-targets`, `--markdown` or input files, and a `DIR` that isn't a folder exits 2. What it can't read for sure is
listed with its file and line rather than guessed. See
[Suggest targets from your CI and Dockerfiles](targets.md#suggest-targets).

### `-V`, `--version` {#version}

Print wheelgap's version, with the versions of `packaging` and of the Python it runs on, for bug reports.

### `-h`, `--help` {#help}

Show the flags and some examples, then exit.

## Subcommands

### `wheelgap diff` {#diff}

Compare two lockfiles: which packages got worse on a target, and which new packages have gaps. It takes `-p`, `-t`
and `--json` (and, after 0.9.0, `--glibc`, `--musl`, `--distro` and `--macos`). See [Comparing two lockfiles](diff.md).

### `wheelgap mcp` {#mcp}

**After 0.9.0.** Start the MCP server, as `wheelgap-mcp` does. See [MCP server](mcp.md).

## Other commands

### `python -m wheelgap.scan` {#scan}

Not a `wheelgap` subcommand: a module that checks the latest release of the most-downloaded PyPI packages, the data
behind the [coverage tracker](https://wheelgap.github.io/tracker/). See
[How it works](how-it-works.md#scanning-the-top-pypi-packages).

## Environment variables

| variable | effect |
| --- | --- |
| `WHEELGAP_INDEX_URL` | the PyPI JSON API to use instead of `https://pypi.org/pypi`, such as a mirror's; see [Pipfile.lock](inputs.md#pipfilelock) |
| `NO_COLOR` | no color with `--color auto` |
| `FORCE_COLOR` | color with `--color auto`, even when stdout isn't a terminal |
| `GITHUB_WORKSPACE` | with `--github-annotations` (after 0.9.0), annotation paths are relative to it |
