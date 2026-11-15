# Inputs

wheelgap takes one or more inputs: lockfiles, requirements files, a `pyproject.toml`, directories, or package names.
With no input it reads the current directory.

```bash
wheelgap                          # the lockfile in the current directory
wheelgap uv.lock                  # one file
wheelgap services/api services/worker   # the lockfile in each directory
wheelgap psycopg2 numpy==2.3.4    # package names, with or without a version
```

| input | read without network? | dependency path in `--explain` |
| --- | --- | --- |
| [`uv.lock`](#uvlock) | yes | yes |
| [`pylock.toml`](#pylocktoml) | yes | no |
| [`poetry.lock`](#poetrylock) | yes, if it lists files | lock-version before 2.1 only |
| [`pdm.lock`](#pdmlock) | yes, if it lists files | no |
| [`Pipfile.lock`](#pipfilelock) | no | no |
| [`pixi.lock`](#pixilock) (after 0.9.0) | no | no |
| [requirements files](#requirements-files) | no | no |
| [`pyproject.toml`](#pyprojecttoml) | no | no |
| [package names](#package-names) | no | no |

"Read without network" means the lockfile records every file published for each pinned version, so wheelgap
needs no index lookup (except when its `requires-python` leaves out a Python you check; see
[uv.lock](#uvlock)). The other inputs are looked up on the PyPI JSON API, or on the index you choose; see
[How it works](how-it-works.md#index-lookups).

## Which file is read

A **directory** is searched for, in this order, `uv.lock`, `pylock.toml`, `poetry.lock`, `pdm.lock`,
`Pipfile.lock`, `requirements.txt` and `requirements.lock` (and, after 0.9.0, `pixi.lock`), and the first one found is
read. After 0.9.0, `--environment` makes a directory's `pixi.lock` the one read, and when a directory has `pixi.lock`
next to another lockfile, a note on stderr names the `pixi.lock` it passed over. A directory with none of them is an
error; if it has a `pyproject.toml` with a `[project]` table, the error suggests checking that file
instead. wheelgap doesn't switch to `pyproject.toml` on its own.

A **file** is read by its name:

| file name | read as |
| --- | --- |
| `uv.lock`, or another `*.lock` name containing `uv` | `uv.lock` |
| `pylock.toml`, `pylock.*.toml` | `pylock.toml` |
| `poetry.lock` | `poetry.lock` |
| `pdm.lock` | `pdm.lock` |
| `Pipfile.lock` | `Pipfile.lock` |
| `pixi.lock` (after 0.9.0) | `pixi.lock` |
| `requirements*.lock`, `*.txt`, `*.in` | a requirements file |
| `pyproject.toml` | a project's direct dependencies |

Any other name is an error. An argument that isn't an existing file or directory is read as a
[package name](#package-names), unless it contains `/` or ends in `.lock`, `.txt` or `.toml`, which is an error
(`no such file`).

Packages that can't come from an index, such as Git, path or URL dependencies, are **skipped**: they're listed on a
`skipped` line (and in `skipped` in `--json`) with the reason, and don't count as gaps.

## uv.lock

**Read:** every package's wheel and sdist filenames, and the dependency graph with its environment markers. The graph
is walked per target from the project and workspace members (and the lock's dependency groups), so a package that
only a Windows-only dependency pulls in isn't reported on Linux.

**Skipped:** packages whose source is `editable`, `virtual`, `git`, `path`, `directory` or `url`. Packages from
another index keep their locked files and are never looked up on PyPI.

**Python versions outside `requires-python`:** uv only keeps the wheels that can serve the lock's `requires-python`.
If it leaves out a Python you check (say `==3.13.*`, checked with `-p 3.14t`), wheelgap also looks the lock's PyPI
packages up on the index, keeps the locked files if a lookup fails, and prints a note. `--offline` skips that
lookup, with a note that those Pythons may show false gaps.

**Dependency selection:** with no selection flags, every project extra and dependency group counts. To match what a
deployment installs:

```bash
# Match a production uv sync --no-dev --extra api
wheelgap uv.lock --no-dev --extra api -t linux-aarch64
# Include every project extra, or add a dependency group
wheelgap uv.lock --all-extras
wheelgap uv.lock --group docs
# Check only a group, without project dependencies or default groups
wheelgap uv.lock --only-group docs
```

`--extra`, `--group` and `--only-group` are repeatable. Once any selector is used, the project's runtime dependencies
and uv's default groups (`dev` unless `[tool.uv] default-groups` changes it) count, unless `--only-group` is given;
project extras are included only when requested. `--no-dev` removes `dev`, while `--only-group` excludes project
dependencies and default groups. Combine `--only-group` with `--group` to add more groups, but not with
`--extra`/`--all-extras`. An explicit `dev` group conflicts with `--no-dev`. Unknown extra or group names are errors.

With selection enabled, rooted workspaces start at the root project (members outside the selection aren't checked,
and a note on stderr names them); virtual workspaces start at their members. Dependency-requested extras are followed
transitively, including local workspace dependencies, rather than enabling every extra of every dependency.
Unresolved marker conditions (including uv's synthetic conflict markers and unknown OS releases) remain
conservative: wheelgap can still report a possible gap rather than silently hide one. Selection itself needs no
network.

Default selections can go in [`[tool.wheelgap]`](configuration.md#extras) as `extras` and `groups`.

**Default targets:** wheelgap reads `[tool.uv] required-environments` from the project's `pyproject.toml` as default
targets; see [Targets](targets.md#how-targets-are-chosen).

## pylock.toml

**Read:** each package's wheel and sdist filenames, its `marker`, its `index`, and the file's `requires-python`
(with the same re-check as `uv.lock` for Pythons it leaves out).

**Skipped:** `vcs`, `directory` and `archive` packages that list no wheels or sdist.

`pylock.toml` has no dependency graph that wheelgap reads, so `--explain` shows no dependency path, and dependency
selection flags don't apply.

## poetry.lock

**Read:** each package's files (from its `files` list, or from `[metadata.files]` in older locks) and its markers.

**Skipped:** packages whose source type is `git`, `hg`, `svn`, `bzr`, `directory`, `file` or `url`. A package from
another index (a `legacy` source) keeps its locked files and isn't looked up on PyPI. Old locks that record only
hashes and no file names are looked up on the index; packages in them from another index are skipped as
`private index`.

**Lock versions before 2.1** (all of Poetry 1.x) record markers only on dependency edges, so wheelgap walks the
dependency graph from the packages nothing depends on and from the dependencies declared in the `pyproject.toml`
next to the lock. `--explain` shows the dependency path for these locks.

**Dependency selection** (Poetry 2.x locks): `--group`, `--only-group` and `--no-dev` work with Poetry's meanings, and
configured `groups` apply too. `main` and every group not marked `optional = true` in `pyproject.toml` count;
`--group` adds a group (like `poetry install --with`), `--no-dev` leaves out `dev`, and `--only-group` checks only the
named groups (`main` only if named). As with `poetry install --without dev`, `--no-dev` is an error when neither the
lock nor its `pyproject.toml` has a `dev` group. A package counts if any of its groups is selected, with the markers of those
groups. A command-line selection on a Poetry 1.x lock (no per-package groups) is an error; a configured selection
leaves a 1.x lock unfiltered.

**Extras** (after 0.9.0): `--extra NAME` and `--all-extras`, and configured `extras`, work like
`poetry install --extras NAME` and `--all-extras`, as Poetry 2.3 and later install with default settings. Once any
selector is used, a package whose lock markers are only extra comparisons (`extra == "s3"`) counts only if one of those
extras is selected, with extra names normalized as Poetry does (`Fancy_Name` is `fancy-name`). An extra that isn't in
the lock's `[extras]` table is an error, and so is `--extra` on a Poetry 1.x lock. Unlike `uv.lock`, `--only-group`
combines with `--extra` here, as in `poetry install --only main --extras api`. Group selections follow Poetry too, so
`--no-dev` (or configured `groups`) doesn't check packages that only an extra installs.

Poetry 2.0-2.2 (and later versions with `installer.re-resolve`) can install more than the lock's markers say, so where
the lock alone can't tell, wheelgap checks a superset of what any Poetry 2.x installs, and a note on stderr says so:

- a package whose markers combine an extra with other conditions (`sys_platform == "win32" or extra == "a"`) is checked
  as if any extra might be selected;
- extras leave no package out of a lock with no `pyproject.toml` beside it, or whose `pyproject.toml` declares a name
  from an extra somewhere else too (in the dependencies, a group, or another extra with a marker) or has a requirement
  wheelgap can't read.

In 0.9.0, `--extra` isn't supported for `poetry.lock`.

## pdm.lock

**Read:** each package's files (from its `files` list, or from `[metadata.files]` before PDM 2.8) and its `marker`.
A package with no recorded files is looked up on the index.

**Skipped:** `git`, `hg`, `svn`, `bzr`, `path`, `url` and `file` packages.

**Dependency groups:** the same selectors work with PDM's meanings, and configured `groups` apply too. Every group the
lock was made for (its `[metadata] groups`: `default`, the dev groups and any optional-dependencies groups locked with
`pdm lock -G`) counts, as with a plain `pdm install`; `--no-dev` leaves out `dev` (like `pdm install --without dev`),
and `--only-group` checks only the named groups (`default` only if named), so PDM's `--prod` is
`--only-group default` here. `--group` only checks that the group is locked, since it already counts. A group that's
in `pyproject.toml` but not in the lock is an error (lock it with `pdm lock -G <name>`), as in PDM. A package counts
if any of its groups is selected. `--extra` isn't supported for `pdm.lock` (PDM installs optional-dependencies as
groups, with `pdm install -G NAME`, and they count once locked), and a command-line selection on a lock
without package groups (one locked without the `inherit_metadata` strategy) is an error; configured `groups` leave
such a lock unfiltered.

## Pipfile.lock

**Read:** the `default` and `develop` sections and any custom package categories (such as `docs` or `tests`), with
their pinned versions and markers. A `Pipfile.lock` doesn't record file names, so each package is looked up on PyPI.

**Skipped:** Git, Mercurial, Subversion, Bazaar, path, file and editable dependencies. Packages pinned to an `index`
that isn't PyPI are skipped too (`private index`), so their names aren't sent to pypi.org.

A PyPI mirror counts as a private index here, since wheelgap can't tell it apart from an internal one, unless
`WHEELGAP_INDEX_URL` points at the JSON API of that same index (for a source `https://mirror.example.com/simple`,
set `WHEELGAP_INDEX_URL=https://mirror.example.com/pypi`): then those packages are looked up there, on the index the
lock already names. It must be the same repository, not just the same host: `<repo>/simple` matches `<repo>/pypi` with
the same scheme, host and port, so another repository on the same Artifactory or Nexus server stays skipped. The
index has to serve PyPI's JSON API, including pinned versions (`<WHEELGAP_INDEX_URL>/<name>/<version>/json`); on a
mirror that only serves the simple API, such as the Tsinghua or Aliyun mirrors, those packages come back as lookup
errors like `not on index` instead. `--index-url` lookups don't use this.

## pixi.lock

!!! note "After 0.9.0"
    `pixi.lock` input and `--environment` are not in 0.9.0.

wheelgap checks the PyPI packages of one pixi environment: `default`, or the one `--environment NAME` names. Lock
versions 6 and 7 are read (pixi writes 7 since 0.68); a lock version wheelgap doesn't know, or YAML outside the plain
subset pixi writes, is an error rather than a guess.

**Read:** the environment's PyPI packages and their pinned versions. pixi records just the one file it picked for each
of the lock's platforms, for the environment's Python and pixi's own glibc and macOS floors, so that can't answer
other targets: each PyPI package is also looked up on PyPI for its pinned version, a failed lookup is `unknown`, and
`--offline` refuses the file.

**Skipped:** conda packages (`conda`), since wheelgap checks PyPI wheels only; past five, the text and Markdown output
count them instead of listing them (`--json` lists each one). A package from another index is skipped
(`private index`) unless `WHEELGAP_INDEX_URL` points at that same index, as are Git, path and direct-URL packages. A
direct URL that points at a file on `files.pythonhosted.org` can't be told apart from a package that came from PyPI, so
it's checked as one.

**Platforms:** a package the lock leaves out on one of its platforms (say `pywin32`, locked for `win-64` only) shows
`n/a` on targets with that platform's `sys_platform`, `platform_machine` and Python, and still counts everywhere else.
Platforms that share those (a `linux-64` and a `linux-64-cuda` with the same subdir) hide a package only when it's
missing from all of them.

`check_lockfile()` takes `environment=` too; the MCP tools and `[tool.wheelgap]` read the `default` environment only.
Naming an environment for any other lockfile is an error.

## Requirements files

`requirements.txt`, `requirements.lock` and other requirements files are looked up on the index. Feed wheelgap a
fully pinned file (from Rye, `uv pip compile`, `pip-compile` or `uv export`): it checks the lines you give it and
doesn't resolve dependencies. Rye's default (non-universal) locks are resolved for the machine that created them and
carry no platform markers.

**Read:** requirement lines with their markers, and files included with `-r` / `--requirement`. A pinned line
(`numpy==2.3.4`) is checked at that version; a range or a bare name uses the newest matching release (see
[How it works](how-it-works.md#version-ranges)).

**`--no-binary` and `--only-binary`** lines (`:all:`, `:none:` or package names) apply as pip applies them, in order
and to every requirement, including those in `-r` and `-c` files: with `--no-binary` a package's wheels are ignored
(`source` if it has an sdist), and with `--only-binary` a package with no fitting wheel is `missing`, since pip won't
build it. `--explain` says which option did it.

**Constraint files** (`-c FILE`, `-cFILE`, `--constraint FILE` or `--constraint=FILE`) restrict unpinned requirements
without adding packages. Exact constraints select a version; ranges are intersected. A constraint wheelgap can't apply
leaves the package `unknown` rather than guessed: one with an environment marker, a URL or path, or two different
exact versions. Constraint files given as a URL or an environment variable (`-c https://…`, `-c ${PIP_CONSTRAINT}`)
are skipped.

**Skipped:** editable lines (`-e`), local paths (lines starting with `.` or `/`) and URLs (`git+https://…`,
`name @ https://…`). Other pip options in the file, such as `--index-url` or `--hash`, are ignored: use wheelgap's
own [`--index-url`](cli.md#index-url) to look packages up on another index.

## pyproject.toml

Libraries usually don't commit a lockfile. Pass the library's `pyproject.toml` to check the dependencies it declares
in `[project] dependencies`:

```bash
wheelgap pyproject.toml -t win-arm64 -t pyodide
# Add [project.optional-dependencies] groups
wheelgap pyproject.toml --extra test
wheelgap pyproject.toml --all-extras
```

Each requirement is read like a line of a requirements file, so ranges and bare names use the newest matching release
and markers are respected. A range picks the newest release whose `Requires-Python` allows each `-p` version, so with
several it can pick a different release per Python ([how it works](how-it-works.md)). A bare name always checks the
latest release, even for a `-p` version that can't install it, so it can show false gaps. Only direct
dependencies are checked: their own dependencies aren't, and wheelgap always prints a note saying so. Use a lockfile
for the full picture.

If `[project] requires-python` leaves out a `-p` Python (every clause counts, as pip applies them), that target is
still checked and a note names it; an invalid `requires-python` is ignored with a note.

`[dependency-groups]` isn't read yet, so `--group`, `--only-group` and `--no-dev` are errors here. A `dependencies`
listed in `[project] dynamic` is an input error (exit 2), as are dynamic `optional-dependencies` with
`--extra`/`--all-extras` and Poetry dependencies kept in `[tool.poetry.dependencies]` (check `poetry.lock` instead).
`check_lockfile()` and `wheelgap diff` don't read `pyproject.toml` as an input.

## Package names

Without a lockfile, name the packages, with or without a version or range:

```bash
wheelgap psycopg2 uvloop numpy==2.3.4 -t windows,win-arm64
```

Each argument is read as a requirement (PEP 508). A pinned version is checked as given; a bare name or a range uses
the newest matching release. An argument that is also the name of a file or directory here is read as that file or
directory, so pin the package to check it on PyPI instead (`wheelgap numpy==2.3.4`). The same goes for the subcommand
names as the first argument: `wheelgap diff==2023.12.6` checks the PyPI package `diff`, and, after 0.9.0,
`wheelgap mcp==2.3.0` the package `mcp`.
