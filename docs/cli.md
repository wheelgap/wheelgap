# Command line

```text
wheelgap [OPTIONS] [INPUT ...]
wheelgap diff [-p VER] [-t PLATFORM] [--glibc X.Y] [--musl X.Y] [--distro NAME] [--macos X.Y] [--json] [--deps] OLD NEW
wheelgap mcp            (or wheelgap-mcp)
```

`INPUT` is a lockfile, a requirements file, a `pyproject.toml`, a script with a `# /// script` block (PEP 723), a
directory, or package specs such as `numpy==2.3.4` (default: the current directory). See [Inputs](inputs.md).

Flags that take a list (`-p`, `-t`, `--extra`, `--group`, `--only-group`, `--ignore`, `--advisory`)
can be repeated; `-p`, `-t`, `--ignore` and `--advisory` also take comma-separated values. Flags win over
[`[tool.wheelgap]`](configuration.md).

`wheelgap --help` lists the flags of the version you run. This page describes `main`; flags marked **after 0.13.0**
aren't in a release yet.

## Targets

### `-p`, `--python` {#python}

`-p VER`: a target Python, such as `3.13`, or `3.14t` for a free-threaded build. Repeatable and comma-separated:
`-p 3.13,3.14t`. Default: the image's Python with `--dockerfile`, then `python` in `[tool.wheelgap]`, then a
`.python-version` file next to the input, then the Python wheelgap runs on.

`next` and `next-t` are the newest CPython this wheelgap knows and its free-threaded build. See
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

### `--build-arg` {#build-arg}

`--build-arg NAME=VALUE` (repeatable, with `--dockerfile`): the value `docker build --build-arg`
gives a global ARG, so `FROM python:${PYTHON_VERSION}-slim` is read with the Python the build picks. It replaces the
ARG's default, or gives a value to an ARG with none; values follow the same rules as defaults (no quotes, escapes or
`$`). A name no ARG before the first `FROM` declares is ignored, as Docker ignores it there, with a note. See
[From a Dockerfile](targets.md#from-a-dockerfile).

### `--pyodide-lock` {#pyodide-lock}

`--pyodide-lock PATH_OR_HTTPS_URL`: a Pyodide release's `pyodide-lock.json`. Packages Pyodide ships itself count as
`✓ bundled` on `-t pyodide`. See [Pyodide](targets.md#pyodide).

### `--advisory` {#advisory}

`--advisory TARGET`: report gaps on `TARGET` without letting them fail the run.

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
- `--advisory` doesn't work with `wheelgap diff` yet.

## Dependency selection

These select which dependencies of a `uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock` or `pyproject.toml` count. See
[Inputs](inputs.md#uvlock) for the full rules per format.

### `--extra` {#extra}

`--extra NAME`: include a uv project extra, or a `pyproject.toml` `[project.optional-dependencies]` group, or a
Poetry 2.x `poetry.lock` extra (like `poetry install --extras NAME`). Repeatable. Not supported for
`pdm.lock`.

### `--all-extras` {#all-extras}

Include every uv project extra (or every `pyproject.toml` extra, or every Poetry 2.x `poetry.lock` extra).

### `--group` {#group}

`--group NAME`: include a dependency group (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`, or a
`pyproject.toml` `[dependency-groups]` group). Repeatable. With `pyproject.toml`, no group is included by default.

### `--no-dev` {#no-dev}

Leave out the `dev` group (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`). An error for `pyproject.toml`,
which includes no group by default.

### `--only-group` {#only-group}

`--only-group NAME`: check only the named dependency groups, without the project's own dependencies or default
groups (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`, or `pyproject.toml` `[dependency-groups]`).
Repeatable; combine it with `--group` to add more groups, but not with `--extra` or `--all-extras`.

### `--environment` {#environment}

`--environment NAME`: the `pixi.lock` environment to check (default: `default`). It also makes a
directory's `pixi.lock` the lockfile read. Naming one for any other input is an error. See
[pixi.lock](inputs.md#pixilock).

## Index access

### `--dist` {#dist}

Check the files you're about to upload — the `dist/` folder `python -m build` or `uv build` leaves —
as the release they make: for each target, whether one of the wheels installs there or the build falls back to the
sdist, with the same statuses and `--explain` reasons as a lockfile row. It takes no other inputs; the project's name
and version come from the wheel and sdist filenames, and a folder that mixes projects or versions is refused.

The baseline is the newest release on PyPI older than this one (so a 1.9.3 maintenance release is compared with 1.9.2,
not with 2.0), looked up by the project's name (a final release ignores earlier pre-releases, so a 1.0 whose only
predecessor is 1.0rc1 counts as a first release): a target it had a wheel for that these files don't, on a Python that
wheel's Requires-Python allowed, is reported as dropped (`win-arm64: 46.0.3 had a wheel, this release has none`) and
fails the run, on top of the usual gap exit. `--json` has them as `dropped`, a map of target label to the last release's
version (`{}` when nothing was dropped or the project has no release to compare — a first release — and absent when the
lookup failed). The comparison needs PyPI's JSON API, so `--dist` is an error with `--offline` or `--index-url` (and
with `--online`, which would check PyPI's files instead of these). An `--ignore`d or `--advisory` target is left out, as
for gaps. The comparison is skipped with a note when a pip or uv index variable (`PIP_INDEX_URL` and the like) is set,
since PyPI's project of that name may not be yours. If the previous release couldn't be looked up, a note says the
comparison didn't run; both notes show even with `-q`, so a gate that didn't compare never passes silently.
### `--deps` {#deps}

For package names on the command line, requirements files, the `pip:` list of a conda `environment.yml`, and
dependencies declared in `pyproject.toml` or a script's `# /// script` block, also check what they install.
For a project file, this includes any extras you select with `--extra` or `--all-extras`. wheelgap resolves the
tree for each target from PyPI's metadata: for each requirement, the newest release that meets every constraint on it,
supports the target's Python and isn't yanked (a yanked release only for an `==` pin), following markers and extras as
pip evaluates them on that target. `--explain` shows the path to each gap (`via: mcp → PyJWT → cryptography`), and
`--json --explain` has it as `via`. `--explain` with `--deps` also turns on [`--suggest`](#suggest),
so each gap ends with the release to move to (`--no-suggest` turns it off). A release picked differently for two Pythons is a row for each.

With a requirements file, each requirement line is a root — pinned or not, with its markers and extras — and `-r`
includes are read too. A `-c` constraint file's constraints apply to the resolved tree, to dependencies as well as
named packages, and one wheelgap can't apply marks the package `unknown`, as without `--deps`. `--no-binary` and
`--only-binary` lines apply to the resolved packages as they do to the file's own. Editable, path and URL lines stay
skipped. A file that already lists what it installs — every requirement pinned, with `pip-compile`'s `--hash` lines or
`# via` comments — needs no resolving: wheelgap says so and checks it as written.

It isn't pip's resolver. When a later constraint rules out a release it already picked, it starts again with that
constraint known; if the constraints on a package can't all be met, it stops with an error rather than guess. A release
whose dependencies it can't read (an sdist-only release with no `Requires-Dist` on PyPI, say) is `unknown`. Where pip
would backtrack to an older release of a package to satisfy an upper bound (`"numpy<2" pandas`), it stops with that
error too. A package with no release that installs on a target (no matching wheel or sdist) is shown at its newest
matching release, as `missing`, rather than walking back to an old release of whatever depends on it. Dependencies are
read from a wheel's own metadata file, since PyPI's JSON page can leave them out. A marker it can't evaluate for a
target (`platform_release`) counts as true there. It looks
up each package in the tree on the PyPI JSON API, so it doesn't work with `--offline`, `--index-url` or a lockfile
input. It also refuses a project that configures another installer index or source in its own files, or
when a pip or uv index variable (`PIP_INDEX_URL`, `UV_INDEX_URL` and the like) is set; it can't see a `pip.conf` or
your user-level uv config, so with a private index there, use a lockfile. A lockfile from `uv lock` or
`pip-compile` is exact, so prefer one when you have it.

### `--online` {#online}

Re-check lockfile packages against PyPI instead of trusting the files the lockfile lists, for when a project has
uploaded new wheels for an existing version since you locked, or a `uv.lock` was made with `environments`
restrictions. Packages the lock says came from another index keep their locked files, so their names are never sent
to PyPI. Can't be combined with `--offline`.

### `--offline` {#offline}

Never contact an index, for CI and coding agents without network. Inputs that need a lookup (requirements files,
`pyproject.toml`, `Pipfile.lock`, package names, lockfile entries with no file list, `pixi.lock` and `environment.yml`)
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
combine it with quiet mode. See [Output](output.md#explain). With [`--fix`](#fix) (**after 0.13.0**), a gap that a
gap wheel fills also gets a `gap wheel` line.

A wheel that would fit but whose release was yanked is named with the reason `yanked`.

### `--from-log` {#from-log}

`--from-log FILE` reads a failed pip, uv, Poetry or PDM install log (`-` reads stdin), finds the
packages that failed to build, and checks each one as `--explain --suggest` does: why no wheel fits, and the release
that has one. The log doesn't say where the install ran, so pass `-p` and `-t` for that machine (or `--dockerfile` for
a docker build); without them it's an error.

```bash
wheelgap --from-log install.log -p 3.13 -t win-arm64
pip install -r requirements.txt 2>&1 | wheelgap --from-log - -p 3.13 -t win-arm64
```

It reads pip's summary at the end (`Failed to build NAME`; pip 24.1 to 25.1's `Failed to build installable wheels for
some pyproject.toml based projects (NAME)`; and pip 25.2 and later's `× Failed to build installable wheels for some
pyproject.toml based projects` with the names on the `╰─> NAME` line after it), counting only names pip also printed
`Building wheel for NAME` for, and uv's ``Failed to build `NAME==VERSION` `` on uv's own error lines (after `×`, `├─▶`,
`╰─▶`, `error:` or `Caused by:`). pip leaves the version out, so it comes from the `Collecting NAME==VERSION`,
`Downloading` and `Using cached` lines since the package's last `Collecting` line at column 0 (under `pip install -v`,
the indented `Collecting` lines of the pip that installs build dependencies don't count), in the same pip run: a
column-0 `Successfully installed` ends a run, so a later run's failure without its own `Collecting` line is an error.
pip doesn't mark where a run starts, so give `--from-log` the log of one pip run. uv's log doesn't name the index it
used, so a uv failure is checked on PyPI (or the index wheelgap is given). It's an error rather than a guess when the
log doesn't name a version (`pip install .` logs `Processing` instead, and `pip install -q` hides `Collecting`: check
`NAME==VERSION` directly then), when it names more than one or pip backtracked (`INFO: pip is looking at multiple
versions of ...`), when it collects the package again without a version after naming one, when the package came from a
URL or a path, when pip's `Looking in indexes:` names another index than https://pypi.org/simple or the log has `Looking
in links:` (`--find-links`, with or without `--no-index`) and wheelgap isn't given an index too (`--index-url`,
`index-url` in `[tool.wheelgap]`, or `WHEELGAP_INDEX_URL`), when a failure line can't be read, or when more than 50
packages failed. Colour codes and GitHub Actions timestamps are skipped, a UTF-16 log (PowerShell's `>`) is read too,
and of a longer log only the last 8 MiB is read.

Poetry 2.x logs (captured with 2.5.1; normal or `-v`) are read from Poetry's final build-backend note, `Note: This
error originates from the build backend, ... issues with NAME (VERSION)`, after its `PEP517 build of a dependency
failed` header. The backend's own output, which Poetry indents under `|` but can also print at column 0 (what the
backend writes to stderr), can't be told apart from Poetry's lines, so the note counts only when an earlier Poetry
operation line names the same package and version, `- Installing NAME (VERSION)` or `- Updating NAME (OLD -> NEW)` (and
`Downgrading`) for NEW. Poetry's note doesn't name the index, so, as with uv, pass the index the install used with
`--index-url` when it wasn't PyPI.

PDM logs (captured with 2.29.7) need `pdm install -v`: normal output prints the same name and version for an index,
URL or local-path dependency, so it's refused with a request to rerun with `-v`. In verbose output the failure counts
only with all three: PDM's `✖ Install NAME VERSION failed` line, its `Error occurs adding NAME:` with a
`pdm.exceptions.BuildError` traceback, and the `Running PEP 517 backend to build a wheel for <Link ... (from URL)>`
line that says which index the sdist came from. A download or other install error is refused rather than reported as a
build failure, and a package built from another index than PyPI is an error that names the index, unless wheelgap is
given that index with `--index-url`.

Not read, so they give an error rather than a guess: Poetry 1.x logs, whose `ChefBuildError` diagnostics differ (`found
no package that failed to build`); a PDM sync that updates a package, whose failure line is `✖ Update NAME OLD -> NEW
failed`; and a PDM log from a console that can't print Unicode, where PDM writes `x` instead of `✖`. Other Poetry and
PDM versions whose diagnostics differ are refused too.

A note lists the packages the log says failed, and another says when one has a wheel for a checked target after all:
then the log likely comes from another platform or Python than `-p` and `-t`. `--no-suggest` turns `--suggest` off.
`quiet = true` in `[tool.wheelgap]` doesn't apply to it, as `--no-quiet` overrides it; `--quiet` is an error, as with
`--explain`, unless the output is `--json` or `--markdown`. It takes no other inputs, and can't be combined with
`--dist`, `--deps`, `--environment`, `--suggest-targets`, `--list-targets` or `--clear-cache`.

### `--conda-forge` {#conda-forge}

With `--explain`, also say when conda-forge has a package with exactly the same name and a build of
the same version that fits a gap's target. Only glibc Linux, macOS and Windows targets with a current conda-forge
Python can match, and it never changes a status, the summary or the exit code. Only names known to be on PyPI are sent
to anaconda.org; a run with `--index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag.
See [conda-forge hint](output.md#conda-forge) and [the PyPI-origin rule](how-it-works.md#pypi-origin).

### `--piwheels` {#piwheels}

With `--explain`, also say when [piwheels](https://www.piwheels.org/) has a wheel of the same version for a
`linux-armv7l` gap (32-bit Raspberry Pi OS). piwheels has no aarch64 wheels, so other targets are never looked up. It
never changes a status, the summary or the exit code. Only names known to be on PyPI are sent to piwheels.org; a run with `--index-url`
or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag.
See [piwheels hint](output.md#piwheels) and [the PyPI-origin rule](how-it-works.md#pypi-origin).

### `--notes`, `--no-notes` {#notes}

With `--explain`, also show the [coverage tracker](https://wheelgap.github.io/tracker/)'s notes for a
package with a gap, such as a binary package to use instead. It sends the names of packages with gaps to
wheelgap.github.io, so it's opt-in, and only for names that pass [the PyPI-origin rule](how-it-works.md#pypi-origin);
never with `--offline`, `--index-url` or a `WHEELGAP_INDEX_URL` mirror. `notes = true` in `[tool.wheelgap]` turns it
on by default, and `--no-notes` turns it off for one run. See [Tracker notes](output.md#notes).
It also shows the tracker's [build lab results](output.md#build-lab) for a `source` cell, from the
same file: whether exactly that release built from its sdist and imported on GitHub's runners for that target. They're
the lab's dated observations on GitHub's runners, which have compilers and many system libraries preinstalled, so a
build that works there can still need setup on your machine. Nothing the lab built is downloaded.

### `--build-info`, `--no-build-info` {#build-info}

With `--explain`, also say what each `source` build needs (`build needs: at least a Rust toolchain
(maturin<2.0,>=1.9)`), from the `[build-system]` table of the sdist's `pyproject.toml`. It downloads sdists, so it's
opt-in, and only for packages that pass [the PyPI-origin rule](how-it-works.md#pypi-origin); it's off with `--offline`,
`--index-url`, `WHEELGAP_INDEX_URL` or a pip or uv index setting in the environment. `build-info = true` in
`[tool.wheelgap]` turns it on by default; `--no-build-info` turns it off. See [Build requirements](output.md#build-info).

### `--suggest`, `--no-suggest` {#suggest}

With `--explain`, also name the lowest newer release with a wheel for a pinned package's gap targets
(`fastlib 2.0.0 -> 2.1.0 has wheels for musl-aarch64, win-amd64`) that keeps the wheels the package has on its other
targets, or each target's nearest when no one release has them all. It looks each package with a gap up on pypi.org
once, only for names that pass [the PyPI-origin rule](how-it-works.md#pypi-origin), and never changes a status, the
summary or the exit code. A run with `--index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the
flag. With `--markdown`, it renders suggestions as a Markdown list under the explanation. With `--json`, adds `suggest` to each package with a gap. See
[Newer releases](output.md#suggest).

With `--deps --explain` it's on by default, since `--deps` already looks every package in the tree
up on PyPI: each gap ends with the way out, the nearest newer release with the wheel or the last older one that had
it. That's only when `--deps` resolved every input; with a lockfile or an already-pinned requirements file among them,
it stays opt-in, since those packages' names weren't sent to PyPI. `--no-suggest` turns it off.

### `--write-constraints` {#write-constraints}

`--write-constraints FILE` (with `--explain --suggest`, or `--deps --explain`, which turns `--suggest` on): also write what `--suggest` found to a
pip/uv constraints file, one pin per package:

```text
cryptography==46.0.3  # wheels for win-arm64; 50.0.2 has none
```

A package gets a pin only when a single release has wheels for every gap target `--suggest` named a release for, and
it keeps every wheel the checked release has: a newer one as `--suggest` names it, or the last older one that had a
wheel. A package whose gaps need different releases, or whose only release with a wheel for a target drops one
elsewhere, is left out with a comment saying why, and so is a version range that resolved to different releases on
different Pythons. The file isn't checked against the rest of the dependency tree, so check that it still resolves:
`pip install -c FILE -r requirements.txt` or `uv pip compile -c FILE requirements.in`. Gaps on `--advisory` targets count
too, so a pin can move a package for a target that wouldn't fail the run. It's written even when the check fails; an
empty `FILE` is an error, as for `--junit`.

### `--fix` {#fix}

After the usual output, say which gaps a wheel on wheelgap's [gap wheel index](https://wheelgap.github.io/wheels/)
fills, and how to install from it: a `[tool.uv.index]` and `[tool.uv.sources]` snippet for `pyproject.toml` with a
uv.lock input, the same entries for the `# /// script` block of a script (or of the script beside a script's lock,
`example.py.lock`, then `uv lock --script example.py`), and an `--extra-index-url` line for pip otherwise. A cell counts only when it's `source` or `missing`
(and not `--ignore`d), the package passes [the PyPI-origin rule](how-it-works.md#pypi-origin), and the index has a
wheel of exactly that version whose tags the target accepts. It reads the index's list from wheelgap.github.io in one
request that names no package, and only when a gap could use it. A list it could read is kept in the 24-hour
[cache](how-it-works.md#cache) (`--no-cache` neither reads nor saves it, and `--online` reads it again). It never
changes a status, the summary or the exit code: wheelgap still reports what PyPI has. `--offline` refuses the flag
with an error; with `--index-url`, `index-url` in `[tool.wheelgap]` or a `WHEELGAP_INDEX_URL` mirror it's off and
reads nothing. If the list can't be read, a note on stderr says so. With `--json` it adds
[`gap_wheels`](output.md#fix). There's no config key for it.
See [Installing from the gap wheel index](recipes.md#installing-from-the-gap-wheel-index).

With `--explain` too (**after 0.13.0**), each package a gap wheel fills gets a `gap wheel` line under its first filled
target, naming the targets it fills and saying the wheel is unofficial: built by wheelgap from the PyPI sdist, not from
the package's maintainers. For inputs pip installs from as they are (a requirements file, a `pyproject.toml`, a pip
report, an `environment.yml`'s `pip:` list or package names) it gives the `pip install --extra-index-url` command;
for a lockfile it points to the `--fix` section, since that lockfile's own tool has to pick the index up, and for a
script likewise, since `uv run` reads the index from its `# /// script` block. With `--json`, each package gets
[`gap_wheel`](output.md#fix).

### `--print-gap-wheel-snippet` {#print-gap-wheel-snippet}

With [`--fix`](#fix) (**after 0.13.0**), print only the configuration that installs the gap wheels, instead of the
usual output, for you to copy into your project. It never writes to your project. Comment lines say the wheels are unofficial
(built by wheelgap from the PyPI sdist, not by the package's maintainers), link to how they're built, and name each
package and the targets it fills. The exit code is the check's own, so a run with gaps still exits 1 after printing a
snippet; when no gap wheel fits, stdout is empty.

- For one requirements file or package names on the command line: the `--extra-index-url` line, with pip's
  dependency-confusion caveat, and for each wheel the `--hash=sha256:` to add to its line if the requirements pin
  hashes.
- For one uv.lock: the `[[tool.uv.index]]` (with `explicit = true`) and `[tool.uv.sources]` entries for
  `pyproject.toml`, with a note for each package uv won't take from the index until it's listed as a direct
  dependency. Add them to an existing `[tool.uv.sources]` table rather than a second one, then run `uv lock`.

It refuses, with exit code 2 and before any lookup, more than one input and any other input: `pyproject.toml`, a
script or a script's lock (`example.py.lock`), `pylock.toml`, `poetry.lock`, `pdm.lock`, `Pipfile.lock`, `pixi.lock`, a pip report and an `environment.yml`.
Use `--fix` alone for those. It can't be used with `--explain` (and its add-ons), `--all`, `--from-log`, `--dist`, `--json`, `--markdown`, `--junit`, `--sarif`, `--gitlab-code-quality`, `--badge`,
`--write-constraints` or `--github-annotations`.

```console
wheelgap requirements.txt -p 3.14t -t win-arm64 --fix --print-gap-wheel-snippet
wheelgap uv.lock -p 3.14t -t win-arm64 --fix --print-gap-wheel-snippet
```

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

### `--sarif` {#sarif}

`--sarif PATH`: also write a
[SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) report to `PATH`, alongside the usual
output, for GitHub code scanning. It's written even when the check fails, and an empty `PATH` is an error, as for
`--junit`. See [Output](output.md#sarif) and [CI](ci.md#code-scanning).

### `--gitlab-code-quality` {#gitlab-code-quality}

`--gitlab-code-quality PATH`: also write a GitLab Code Quality JSON report to `PATH`, alongside the usual output,
even when the check fails. An empty `PATH` is an error. See [Output](output.md#gitlab-code-quality) and
[GitLab CI](ci.md#gitlab-ci).

### `--badge` {#badge}

`--badge PATH`: also write a [shields.io endpoint](https://shields.io/badges/endpoint-badge) file
to `PATH`, alongside the usual output, with how many of the checked targets have wheels for everything:
`{"schemaVersion": 1, "label": "wheels", "message": "7/8 targets", "color": "yellow"}`. A target counts when nothing on
it is `source` or `missing` (a gap `--ignore` accepts there doesn't count against it; an `--advisory` target's gaps
do). The colour is `brightgreen` when every target counts, `red` when none does and `yellow` in between. If any result
is `unknown`, the message is `unknown` (`lightgrey`) instead of a count. The file is written even when the check fails,
so CI can publish it; an empty `PATH` is an error, as for `--junit`. See [Recipes](recipes.md#readme-badge).

### `--github-annotations` {#github-annotations}

Also mark each package with a gap on its own line of the lockfile in GitHub Actions, as an
annotation. The GitHub Action passes it; see [CI](ci.md#annotations).

With `--deps` on a requirements file, a direct requirement's
gap is on its own line, as without `--deps`. A transitive gap goes on the line
of the requirement that pulls it in when exactly one does on every target where
it has a gap, and its message says `(via …)`. When more than one does, or a
line isn't certain, the annotation names the file without a line, and the
message names them all.

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

`--suggest-targets [DIR]`: read the platforms and Pythons that `DIR` (default: the current directory)
already names in its GitHub Actions workflows, `.gitlab-ci.yml` and Dockerfiles, print each with the file and line it
came from, then a `[tool.wheelgap]` snippet to paste into `pyproject.toml`. It only suggests: nothing is changed or
checked, and it exits 0. With `--json`, it prints the same as an object. It can't be combined with `-p`, `-t`,
`--dockerfile`, `--build-arg`, `--list-targets`, `--markdown` or input files, and a `DIR` that isn't a folder exits 2.
What it can't read for sure is listed with its file and line rather than guessed. See
[Suggest targets from your CI and Dockerfiles](targets.md#suggest-targets).

### `-V`, `--version` {#version}

Print wheelgap's version, with the versions of `packaging` and of the Python it runs on, for bug reports.

### `-h`, `--help` {#help}

Show the flags and some examples, then exit.

## Subcommands

### `wheelgap diff` {#diff}

Compare two lockfiles, or two pinned PyPI package versions with `--deps`: which packages got worse on a target,
and which new packages have gaps. It takes `-p`, `-t`, `--json`, `--glibc`, `--musl`, `--distro`, `--macos` and `--deps`.
See [Comparing two lockfiles](diff.md).

### `wheelgap mcp` {#mcp}

Start the MCP server, as `wheelgap-mcp` does. See [MCP server](mcp.md).

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
| `GITHUB_WORKSPACE` | with `--github-annotations`, annotation paths are relative to it |
