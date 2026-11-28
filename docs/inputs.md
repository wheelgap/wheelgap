# Inputs

wheelgap takes one or more inputs: lockfiles, requirements files, a conda `environment.yml`, a pip installation
report, a `pyproject.toml`, a script with a `# /// script` block, directories, or package names.
With no input it reads the current directory.

```bash
wheelgap                          # the current directory: its lockfile, or what it has instead
wheelgap uv.lock                  # one file
wheelgap services/api services/worker   # the same for each directory
wheelgap psycopg2 numpy==2.3.4    # package names, with or without a version
```

| input | read without network? | dependency path in `--explain` |
| --- | --- | --- |
| [`uv.lock`](#uvlock) | yes | yes |
| [`pylock.toml`](#pylocktoml) | yes | no |
| [`poetry.lock`](#poetrylock) | yes, if it lists files | lock-version before 2.1 only |
| [`pdm.lock`](#pdmlock) | yes, if it lists files | no |
| [`Pipfile.lock`](#pipfilelock) | no | no |
| [`pixi.lock`](#pixilock) | no | no |
| [requirements files](#requirements-files) | no | with `--deps` |
| [`environment.yml`](#environmentyml) | no | with `--deps` |
| [pip installation report](#pip-installation-report) | no | no |
| [`pyproject.toml`](#pyprojecttoml) | no | with `--deps` |
| [Python scripts](#python-scripts) | no | with `--deps` |
| [package names](#package-names) | no | with `--deps` |

"Read without network" means the lockfile records every file published for each pinned version, so wheelgap
needs no index lookup (except when its `requires-python` leaves out a Python you check; see
[uv.lock](#uvlock)). The other inputs are looked up on the PyPI JSON API, or on the index you choose; see
[How it works](how-it-works.md#index-lookups).

## Which file is read

A **directory** is searched for, in this order, `uv.lock`, `pylock.toml`, `poetry.lock`, `pdm.lock`,
`Pipfile.lock`, `requirements.txt`, `requirements.lock` and `pixi.lock`, and the first one found is read.
`--environment` makes a directory's `pixi.lock` the one read, and when a directory has `pixi.lock`
next to another lockfile, a note on stderr names the `pixi.lock` it passed over.

In a directory with none of those names, wheelgap looks further (before, this was an error). Each step
is tried only if the one before found nothing, a note on stderr says which file was checked and why (also with
`--quiet`), and `--json` has it in [`directory_inputs`](output.md#json):

1. **`pyproject.toml`** at the root, if it has a `[project]` table that lists dependencies or extras. It is checked as
   [`wheelgap pyproject.toml`](#pyprojecttoml) checks it: the direct dependencies only, with the usual note that says
   so and names `--deps`. It comes first even when the root also has requirements files under other names (step 2,
   and `requirements*.lock`): wheelgap can't tell from a file whether it pins what the project installs or only its
   build tools or docs (flask's `requirements/build.txt`). The note names them instead, as commands to
   run (the first 3, then a count). One that looks like `pip-compile`, `uv pip compile` or `uv export` output is
   named first: `requirements-lock.txt looks compiled and pinned: wheelgap DIR/requirements-lock.txt checks what it
   pins`. It looks so when it has their mark (their header, `# via` comments or `--hash` options) and every package
   it lists from an index is pinned with `==` or `===`. Only the file itself is read for this hint: one over
   2 MB, or one with a `-r` or `-c`, doesn't look so. `--json` lists the same files in `directory_inputs`.
2. **A requirements file under another name** at the root: `requirements*.txt` or `requirements*.in`, or a `.txt` or
   `.in` file in `requirements/` (not a `.in` next to the `.txt` compiled from it, and not a name that starts, in
   any case, with `constraint`, `readme`, `license`, `licence`, `copying`, `notice`, `changelog`, `changes`,
   `history`, `authors`, `contributing`, `todo` or `notes`). If there
   is exactly one, it is checked. If there are several, wheelgap doesn't choose: it exits 2 and lists each as a
   command to run (`wheelgap requirements/base.txt`).
3. **An input in a subfolder**, up to 3 folders down (`backend/requirements.txt`, `services/api/uv.lock`): for each
   folder, the file `wheelgap FOLDER` would read (the first lockfile name above, else a `pyproject.toml` that lists
   `[project]` dependencies). Hidden folders, `node_modules`, `site-packages`, `__pycache__` and virtualenvs aren't
   searched, symlinks aren't followed, and the search opens at most 2000 folders. If there is exactly one, it is
   checked as `wheelgap FOLDER` checks that folder (so a `requirements.txt` there with nothing to check gives way to
   the folder's `pyproject.toml`), and the header, `--markdown` and `--json`'s `sources` name it by its path in the
   directory (`scripts/requirements.txt`), since a bare `requirements.txt` would read as the root's. Several are
   listed as commands (the first 10, then a count) with exit code 2. So is the one input when another requirements
   file could as well be the project's: one under another name beside a subfolder's `requirements.txt`
   (`docker/main/requirements-wheels.txt`), or a `requirements-dev.txt` at the root.

wheelgap only checks a file on its own when it can take it for the project's dependencies. So these are listed as
a command to run (exit 2) instead of being checked, even when there is only one:

- a requirements file or a folder whose name says it is for development, tests, docs or examples: a name with
  one of these words in it, set off by `-`, `_`, `.` or the name's ends: `dev`, `develop`, `development`, `test`,
  `tests`, `testing`, `doc`, `docs`, `example`, `examples`, `sample`, `samples`, `demo`, `demos`, `bench`,
  `benchmark`, `benchmarks`, `lint`, `ci`, `typing`, `fixtures`, `templates`; as in `requirements-dev.txt`,
  `docs/requirements.txt` or `e2e_tests/uv.lock` (but not `devtools/`). Such folders are only looked at when no other subfolder has an
  input;
- the one input in a subfolder, when the root is a project itself: it has a `setup.py`, a `setup.cfg`, or a
  `pyproject.toml` with a `[project]` table, a `[build-system]` table or Poetry dependencies. The project's own
  dependencies are at the root, where wheelgap couldn't read any, not in the subfolder;
- anything found by a search that didn't see every folder: it stopped at 2000 folders, or a folder couldn't be
  read.

A `pyproject.toml` with a `[project]` table that lists nothing is the answer (`No packages to check.`, exit 0) when
step 2 finds no requirements file and step 3 no subfolder input, leaving aside those whose name says development,
tests, docs or examples. A directory where nothing is found at all is an error that says what was looked for, and
why its `pyproject.toml` couldn't be checked if it has one.

**A file with nothing to check**: when the first lockfile found lists no package from an index (a
`requirements.txt` that only says `.`, a `pixi.lock` with conda packages only, a `uv.lock` with only the project
itself), wheelgap reads the next of the names above that has one, then the root's `pyproject.toml`, and a note says
which file it passed over and why. If none has anything either, the first file is read as before. With
`--environment`, the `pixi.lock` is read whatever it holds.

The files a directory already had an answer from are read as before: the order of the names above hasn't changed, and
a root file always comes before a subfolder's. The Python API's `check_lockfile()` and `wheelgap diff` have no notes
to say which file they picked, so they still read a directory's lockfile by name only.

A **file** is read by its name:

| file name | read as |
| --- | --- |
| `uv.lock`, a script's `*.py.lock`, or another `*.lock` name containing `uv` | `uv.lock` |
| `pylock.toml`, `pylock.*.toml` | `pylock.toml` |
| `poetry.lock` | `poetry.lock` |
| `pdm.lock` | `pdm.lock` |
| `Pipfile.lock` | `Pipfile.lock` |
| `pixi.lock` | `pixi.lock` |
| `requirements*.lock` (but not `requirements*.py.lock`), `*.txt`, `*.in` | a requirements file |
| `*.json` | a pip installation report |
| `environment*.yml`, `environment*.yaml` | a conda environment file (pip packages) |
| `pyproject.toml` | a project's direct dependencies |
| `*.py` | a script's `# /// script` block (PEP 723) |

Any other name is an error. An argument that isn't an existing file or directory is read as a
[package name](#package-names), unless it contains `/` or ends in `.lock`, `.txt`, `.toml`, `.json`, `.in`, `.yml` or `.yaml`, which is an error
(`no such file`).

Packages that can't come from an index, such as Git, path or URL dependencies, are **skipped**: they're listed on a
`skipped` line (and in `skipped` in `--json`) with the reason, and don't count as gaps.

## uv.lock

**Read:** every package's wheel and sdist filenames, and the dependency graph with its environment markers. The graph
is walked per target from the project and workspace members (and the lock's dependency groups), so a package that
only a Windows-only dependency pulls in isn't reported on Linux.

**Answers follow `uv sync`**, with and without `--online` (0.13.0 followed pip with `--online`). uv
[checks only the lower bound of the `Requires-Python` range](https://docs.astral.sh/uv/reference/internals/resolver/#requires-python), so
a pin whose release says `<3.15` still counts on Python 3.15, where pip would refuse it (when installing from a
requirements file exported from the lock, for example). With `--online`, wheelgap says where the two
differ: a note on stderr gives the number of such packages, `--explain` names them, and `--json` lists the files in
each package's `pip_refuses`. Without `--online` it can't tell, because the lock doesn't record each file's
`Requires-Python`. The same rule covers every kind of clause: on Python 3.15, uv also takes a file that says
`==3.14.*`, `~=3.14.0` or `>=3.10,!=3.15.*`, since the lowest Python each allows (3.14, 3.14.0, 3.10) isn't above
3.15. A file whose lowest allowed Python is above the target stays left out, as with pip: `>=3.16` or `==3.16.*` on
3.15, `==3.14.*` on 3.13, and `>=3.15,!=3.15.*` on 3.15 (it starts at 3.16).

**Skipped:** packages whose source is `editable`, `virtual`, `git`, `path`, `directory` or `url`. Packages from
another index keep their locked files and are never looked up on PyPI.

A script's lock from `uv lock --script example.py`, `example.py.lock`, has the same format and is read the same way,
from the script's `[manifest] requirements`: `wheelgap example.py.lock` and `wheelgap diff old.py.lock new.py.lock`.
The lock doesn't record `no-build`, `no-binary`, `no-build-package` or `no-binary-package`, so they're read from the
script beside it, as `uv run` applies them (see below).

**Build restrictions:** wheelgap reads `no-build`, `no-build-package`, `no-binary` and
`no-binary-package` from the uv settings `uv sync` applies: the nearest `uv.toml`, or `pyproject.toml` with a
`[tool.uv]` table, from the lock's folder up. A `uv.toml` takes precedence over a `pyproject.toml` in the same folder,
a `pyproject.toml` without `[tool.uv]` is skipped, and the search goes on past a repository root to the top of the
filesystem, as uv 0.13.1 does. The first `pyproject.toml` above the project's folder must parse: `uv sync` and
`uv lock` stop with an error on one that doesn't while looking for the project's workspace, so wheelgap refuses the
input too (exit 2). For a script's lock, the script's own `[tool.uv]` comes first and
is combined with the nearest settings from the script's folder up, as `uv run` combines them: the script's
`no-build` and `no-binary` win, and the package lists are joined. When nothing is found from the script's folder
up, `uv run` uses the settings of the folder it runs in, which wheelgap can't know. These settings affect
index packages (not local, Git or editable packages that wheelgap skips). `no-build` forbids
source builds; `no-binary` excludes wheels. As in uv, a package list overrides the other kind's blanket setting: with
`no-binary = true`, a package in `no-build-package` still takes a wheel, and with `no-build = true`, one in
`no-binary-package` still builds; `no-binary = false` (or `no-build = false`) turns its package list off.
`--explain` identifies the applicable settings, with the file's path when
it's in a folder above (`../uv.toml no-binary-package`). Invalid setting types, and a `uv.toml` that can't be read, are
input errors. Settings in your user-level uv config or in `UV_NO_BUILD`-style environment variables aren't read.
`--dockerfile-inputs` refuses a `uv sync` line when settings above the lock's folder set a build restriction, since
the image doesn't have them. `wheelgap diff` with a git ref reads that ref's `pyproject.toml` and `uv.toml` beside
the lock.

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

Without any selector, a `uv.lock` is checked the way `uv sync --all-packages --all-extras --all-groups` installs it:
every workspace member with all its extras and groups, and a dependency's extras only where some edge requests them on
that target. Where a lock declares conflicting extras or groups (`[tool.uv] conflicts`), which `uv sync --all-extras`
refuses, wheelgap checks both sides: every way of turning on none or one item of each conflict, as `uv sync
--all-extras --no-extra ...` would, and a package counts if any of them installs it.

**Conflict markers**: uv writes which side of a conflict a dependency needs into the lock's markers
(`extra == 'extra-4-demo-old'` for the extra `old` of `demo`, `extra == 'group-4-demo-cpu'` for a group), with clauses
that hold only when two sides of one conflict are on at once. wheelgap evaluates them as `uv sync` does: against the
items each install turns on (the selected extras and groups of the packages it installs, and every extra a dependency
asks for, such as `lib[cpu]`), so a package that only an install uv refuses would bring (pywin32 through
`(sys_platform == 'win32' and extra == 'extra-4-demo-old') or (extra == 'extra-4-demo-new' and extra ==
'extra-4-demo-old')` on Linux) is `n/a`, and `--extra old` checks the `old` side only. A project's own dependency groups are
decided as `uv sync` decides them, before it follows any edges: an extra that a dependency turns on (`lib[cpu]`) is
still off for them, so a group may bring a package of the other side too, as uv's install plan does. An install in which a
dependency turns on a second extra of one conflict, which `uv sync` refuses, isn't counted. A selection that asks for two
sides of one conflict (`--all-extras`, `--extra old --extra new`), which `uv sync` refuses, is checked like the
default. A conflict between whole projects (`{ package = "a" }`), a `conflicts` table uv wouldn't write, or more than
64 ways of picking the conflicts leave the conflict markers unknown, as in 0.13.0, so a package may be listed that no
install needs.

With selection enabled, rooted workspaces start at the root project (members outside the selection aren't checked,
and a note on stderr names them); virtual workspaces start at their members. Dependency-requested extras are followed
transitively, including local workspace dependencies, rather than enabling every extra of every dependency.
Unresolved marker conditions (such as unknown OS releases, and the conflict markers above that wheelgap doesn't work
out) remain conservative: wheelgap can still report a possible gap rather than silently hide one. Selection itself
needs no network.

Default selections can go in [`[tool.wheelgap]`](configuration.md#extras) as `extras` and `groups`.

**Default targets:** wheelgap reads `[tool.uv] required-environments` from the project's `pyproject.toml` as default
targets; see [Targets](targets.md#how-targets-are-chosen).

**A lock limited by `[tool.uv] environments`**: with
[`environments`](https://docs.astral.sh/uv/concepts/resolution/#limited-resolution-environments) set, uv locks for
those environments only. The lock records them as `supported-markers`, leaves out the packages and wheels that only
other environments need, and `uv sync` fails anywhere else ("The current Python platform is not compatible with the
lockfile's supported environments"). wheelgap follows uv:

- a target inside the environments is checked as always;
- a target you asked for (`-t`, `[tool.wheelgap] targets`, `--dockerfile`, a `-p`) that is outside them is an error
  (exit code 2, and the [error object](output.md#json) with `--json`) that names the targets the lock doesn't cover
  and the ones it does, instead of a table of gaps that only say the lock left that platform out. `--online` doesn't
  change this: it can look up the wheels again, but not the packages the lock left out;
- the built-in default platforms are narrowed to the ones the lock covers, with a note on stderr, so
  `wheelgap uv.lock` on a Linux-only lock checks `linux-x86_64` and `linux-aarch64`. A default platform the lock
  covers for only some of the Pythons you asked for stays, and is the error above. `wheelgap diff` drops only the
  platforms both locks leave out, so a platform one lock covers and the other doesn't (one the new lock gives up or
  adds) is the error above too, and so is any platform when one side isn't a `uv.lock`;
- an [`--advisory`](cli.md#advisory) target the lock leaves out isn't checked, with a note, since it never fails the
  run.

A target is inside when any of the markers holds for it. They are evaluated for the target's `sys_platform`,
`platform_machine`, `os_name`, `platform_system`, `platform_python_implementation`, `implementation_name`,
`python_version` and `python_full_version`. Where no marker holds for sure and one can't be decided for the target
(`platform_release`, `platform_machine` on `win32`, a patch release such as `python_full_version >= '3.13.2'`),
wheelgap says it can't tell and stops, also with exit code 2, rather than guess. To check a platform the lock doesn't
cover, add it to `environments` and run `uv lock`.

## pylock.toml

**Read:** each package's wheel and sdist filenames, its `marker`, its `index`, and the file's `requires-python`
(with the same re-check as `uv.lock` for Pythons it leaves out).

A package's own `requires-python` is applied to the target Python: where the range leaves the Python
out, the pin is `missing` there, because an installer must refuse it
([the pylock.toml specification](https://packaging.python.org/en/latest/specifications/pylock-toml/#installation)),
and `--explain` gives the range. With `--online`, a package looked up on the index goes by the index's per-file
`Requires-Python` instead.

**Skipped:** `vcs`, `directory` and `archive` packages that list no wheels or sdist.

`pylock.toml` has no dependency graph that wheelgap reads, so `--explain` shows no dependency path, and dependency
selection flags don't apply.

## poetry.lock

**Read:** each package's files (from its `files` list, or from `[metadata.files]` in older locks) and its markers.

A package's `python-versions` is applied to the target Python in locks from lock-version 2.1 on: where
the range leaves the Python out, the pin is `missing` there and `--explain` gives the range. Poetry only locks a
package for the Pythons its markers select, so this changes an answer only for a Python outside the range the project
was locked for (`-p 3.14` when the project says `<3.14`), where it shows which pins hold the project back. `*`, and a
Poetry constraint that isn't a PEP 440 specifier (`^3.9`), restrict nothing. With `--online`, a package looked up on
the index goes by the index's per-file `Requires-Python` instead.

**Skipped:** packages whose source type is `git`, `hg`, `svn`, `bzr`, `directory`, `file` or `url`. A package from
another index (a `legacy` source) keeps its locked files and isn't looked up on PyPI. A package with no file names
(old locks that record only hashes, or `files = []`, which Poetry writes when the source gave it no hashes) is looked
up on the index, since Poetry then installs the best file the index has; such a package from another index is skipped
as `private index`.

**Lock versions before 2.1** (all of Poetry 1.x) record markers only on dependency edges, so wheelgap walks the
dependency graph from the packages nothing depends on and from the dependencies declared in the `pyproject.toml`
next to the lock. Without a `pyproject.toml` there that declares them (none, one that can't be read, or one with no
dependencies at all), wheelgap can't tell a direct dependency that another package needs only under a marker
(`colorama`, which `click` needs on Windows only) from one installed only under that marker, and Poetry installs a
direct dependency everywhere. So a package that no dependency installs on a target, and that has no wheel there, is
`unknown` on that target rather than `n/a`, unless another version of it is installed there. `--explain` and
`unknown_reasons` in `--json` say why, and a note on stderr names the packages. It counts as any `unknown` does, and
`--ignore` takes it; where a left-out package has a wheel anyway, the answer is the same either way and nothing changes.
`--explain` shows the dependency path for these locks. `python-versions` isn't applied to them: the
lock doesn't say which Pythons a direct dependency is installed for (`numpy = [{version = "1.26.4", python = "<3.13"},
...]` is only in `pyproject.toml`), so the range would report a pin as `missing` on a Python Poetry never installs it
on.

**Dependency selection** (Poetry 2.x locks): `--group`, `--only-group` and `--no-dev` work with Poetry's meanings, and
configured `groups` apply too. `main` and every group not marked `optional = true` in `pyproject.toml` count;
`--group` adds a group (like `poetry install --with`), `--no-dev` leaves out `dev`, and `--only-group` checks only the
named groups (`main` only if named). As with `poetry install --without dev`, `--no-dev` is an error when neither the
lock nor its `pyproject.toml` has a `dev` group. A package counts if any of its groups is selected, with the markers of those
groups. A command-line selection on a Poetry 1.x lock (no per-package groups) is an error; a configured selection
leaves a 1.x lock unfiltered.

**Extras:** `--extra NAME` and `--all-extras`, and configured `extras`, work like
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

## pdm.lock

**Read:** each package's files (from its `files` list, or from `[metadata.files]` before PDM 2.8) and its `marker`.
A package with no recorded files is looked up on the index.

A package's `requires_python` is applied to the target Python in locks made with the
`inherit_metadata` strategy (PDM's default since 2.11), which records each package's marker: where the range leaves
the Python out, the pin is `missing` there and `--explain` gives the range. As with `poetry.lock`, this changes an
answer only for a Python outside the range the project was locked for. It isn't applied to a lock without
`inherit_metadata`, which doesn't say which Pythons a package is installed for. With `--online`, a package looked up
on the index goes by the index's per-file `Requires-Python` instead.

**Skipped:** `git`, `hg`, `svn`, `bzr`, `path`, `url` and `file` packages.

**Lock targets:** a lock made with `pdm lock --platform`, `--python` or `--implementation` records them as
`[[metadata.targets]]` (a plain `pdm lock` records the project's `requires-python` there), keeps only the packages and
wheels those targets need, and `pdm install` fails anywhere else ("No compatible lock target found"). wheelgap
follows PDM as it does [`[tool.uv] environments`](#uvlock): a target you asked for outside them is an error (exit code
2) naming the targets the lock doesn't cover and the ones it does, and the built-in default platforms are narrowed to
the ones it covers, with a note. A target is inside when one lock target takes it, as PDM 2.29 decides: its
`requires_python` takes the target's Python, its `implementation` (if any) is CPython with the target's
`gil_disabled`, and its `platform` (if any) has the target's operating system (manylinux, musllinux, macOS or
Windows) and architecture. The glibc, musl or macOS version doesn't count, since PDM installs a `manylinux_2_40` lock
on glibc 2.36 too, with a warning. A `requires_python` that holds for some patch releases only (`>=3.13.2` for
`-p 3.13`), and a platform or key PDM doesn't know, can't be decided: wheelgap says so and stops, also with exit code 2.
To check a target the lock doesn't cover, add it with `pdm lock --append --platform ... --python ...`. The error names each target with its Python, since the Python can be the cause, and when no `-p`, `python` setting or `.python-version` picked it (so it's the one wheelgap runs on), it says so and names a Python the lock covers.

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

## Pip installation report

wheelgap reads the report `pip install --dry-run --report` writes: exactly what pip resolved, without
installing anything. Make the report on the target you want to check, and check that target only:

```bash
python -m pip install --dry-run --ignore-installed --report report.json -r requirements.txt
wheelgap report.json -p 3.14 -t linux-aarch64   # on a Linux aarch64 machine with Python 3.14
```

wheelgap reads `version: "1"` reports and the `metadata.name` / `metadata.version` pins under `install`, and looks
each one up on the index to check its wheels. VCS checkouts, local paths and direct URLs are skipped, and so are
packages pip didn't download from PyPI (`files.pythonhosted.org`); a report whose packages all came from another
index, such as an Artifactory or devpi mirror, is an error rather than a run with nothing checked.

A pip report is a snapshot of what pip resolved for one environment, **not** a cross-platform lockfile: on another
platform, machine or Python, pip can install other packages (Windows-only dependencies, say) and leave some of these
out. So wheelgap refuses a target that differs from the report's `environment` in `sys_platform`,
`platform_machine`, `python_version` or `implementation_name`, and a report without one. For `win32`, `linux-i686`
and `linux-armv7l` it doesn't compare `platform_machine`, since a matching machine can report `AMD64`, `x86_64` or
`aarch64` there. To check several targets, make a report on each and check each against its own target.
`check_lockfile()` refuses the same way.

musl and glibc Linux record the same environment, but pip resolves a different set on each — pip on musl accepts
`musllinux` wheels and not `manylinux` ones. A wheel only one libc's pip accepts is proof the resolution isn't one
the other's pip makes, so a report with a `manylinux` wheel in its downloads is refused on `musl-*` targets, one
with a `musllinux` wheel on the glibc `linux-*` ones, and one with both (pip run with two `--platform` values) on
the two families. A report with neither is taken at face value on both families, though it may not be a resolution both make: it may be pure Python,
or an Alpine report that pinned an older sdist where glibc pip would have taken a newer manylinux-only release.

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

**`--pre`, `--all-releases`, `--only-final` and `--prefer-binary`** lines are read as pip 26.3 reads them (after
0.13.0), in the file and in the files it includes. With `--pre` (anywhere in the file, before or after the requirement),
a range or a bare name is checked at the newest matching release, pre-releases included, as pip installs it:
`tokenizers>=0.23` is `1.0.0rc5`, not `0.23.3`. `--all-releases NAMES` allows pre-releases for those packages (`:all:`
for every package), and `--only-final NAMES` never takes one; a package named wins over `:all:`, and later lines change
earlier ones, as in pip. Under `--only-final`, a pin to a pre-release (`demo==2.0rc1`) is `unknown`, since pip finds no
version for it. A pin to any other version isn't affected. pip ignores `--pre` and `--prefer-binary` on a line with `-r`
or `-c` (`--pre -r base.txt`): it reads the file and drops the line's other options, and wheelgap does the same. pip
ignores them on a line with `-e` (`--pre -e ./pkg`) too, which it reads as a requirement. `--all-releases` and
`--only-final` count on both kinds of line, and after a requirement too, because pip's parser applies them as it reads
the line (and so do `--no-binary` and `--only-binary`). `--prefer-binary` makes pip take an older release that has a
wheel over a newer one with only an sdist; wheelgap still checks the newest, and says so on stderr when the file has an
unpinned requirement: `wheelgap: note: requirements.txt:1 sets --prefer-binary: for an unpinned requirement pip may take
an older release that has a wheel, and wheelgap checked the newest` (`-q` drops it). [`--deps`](cli.md#deps) refuses a
file with any of these four options, since they change every dependency's release too.

The same two options written after a requirement (`albumentations>=0.3.2 --no-binary qudida,albumentations`, after
0.13.0) are read too, and mean what pip makes of them: the option counts for the whole install, exactly as on a line
of its own, not only for that requirement. `--config-settings` after a requirement is accepted as before.

`--hash` options on a requirement (`--hash=sha256:…` or `--hash sha256:…`) limit which of the
release's files pip may install, and wheelgap keeps only those: a file whose `sha256` from PyPI's JSON API is listed.
Listing only the sdist's hash gives `source`, as pip and uv build it then; listing only another platform's wheels gives
`missing`. If no file of the release has a listed hash, the package is `unknown` (`none of the hashes in
requirements.txt matches a file of NAME VERSION`), as pip refuses the download. `--explain` names each wheel left
out: `its hash isn't listed in requirements.txt`. As pip does, a file the index gives no `sha256` for (some mirrors
behind `WHEELGAP_INDEX_URL`) isn't left out, since pip checks it after the download. Not read yet, so the answer is
the one without them: `sha384` and `sha512` hashes (PyPI gives no such digest), `--hash` in a constraints file, and
the lookups of [`--index-url`](cli.md#index-url). `--offline` refuses a requirements file as before: its
files come from the index.

Other options after a requirement are read as pip 26.3 reads them there. pip parses them and then
ignores `-f` / `--find-links`, `-i` / `--index-url`, `--extra-index-url`, `--no-index`, `--pre`, `--prefer-binary`,
`-r`, `-c`, `--trusted-host`, `--require-hashes` and `--no-require-hashes`: the requirement is installed as if they
weren't there, from the same index and at the same version, and no file they name is read. wheelgap ignores them
too and says so on stderr, once per file and option (`-q` drops it):
`wheelgap: note: requirements.txt:2: pip ignores -f after a requirement`. To use another index or folder, put the
option on a line of its own. `--use-feature` is accepted without a note, since no feature changes which file pip
picks, and `--all-releases` / `--only-final` are read as on a line of their own (see above). The rest is an input
error that names the file and line: an option pip doesn't have, an ambiguous abbreviation, a missing value or one
pip's parser refuses (`--hash=abc`, `-C foo`, `--no-binary --pre`, `--only-final --pre`) (pip refuses the line too);
and `-e` (pip reads it as making the requirement itself editable, and refuses that for a name).

An included file that can't be read (`-r FILE` or `-c FILE` naming a file that isn't there) is an input error that
names the including file and line, `requirements.txt:3: -r base.txt: file not found`; pip stops there too.

**Constraint files** (`-c FILE`, `-cFILE`, `--constraint FILE` or `--constraint=FILE`) restrict unpinned requirements
without adding packages. Exact constraints select a version; ranges are intersected. A requirement that conflicts with
a constraint is an error, as pip refuses it: a pinned version that an exact constraint or a constraint's
range leaves out, or an exact constraint that the requirement's range or another constraint leaves out. The package is
reported `unknown` and the run fails. Versions match as pip matches them: `==1.9` and `==1.9.0` agree, and `==1.0`
with `==1.0+cpu` is `1.0+cpu`. A constraint wheelgap can't apply leaves the package `unknown` rather than guessed: one
with an environment marker (unless the requirement is pinned to a version it allows), a URL or path, or two exact
versions that disagree. Constraint files given as a URL or an environment variable (`-c https://…`,
`-c ${PIP_CONSTRAINT}`) are skipped.

A `-r FILE` line inside a constraints file is read as pip reads it: `FILE` is a requirements file, so its packages
are checked, while the constraints file's own lines stay constraints. A `-c` inside any file is a constraints file.
An include cycle (two files that include each other, or a file that includes itself, with `-r` or `-c`) is an input
error naming the chain, as pip refuses it; a file included twice from two different files still lists its packages once.
An include in a symlinked requirements file is read from the folder the link is in, as pip reads it, not from the folder
the link points into.

**Skipped:** editable lines (`-e`), local paths (lines starting with `.` or `/`) and URLs (`git+https://…`,
`name @ https://…`). Other pip options in the file, such as `--index-url`, are ignored: use wheelgap's
own [`--index-url`](cli.md#index-url) to look packages up on another index. An index option on a line of its own
(`-i` / `--index-url` / `--pypi-url`, `--extra-index-url`, `-f` / `--find-links` or `--no-index`, in the file or one
it includes) changes where pip looks, so wheelgap's answer is PyPI's alone, and it says so on stderr,
once per input, naming the option but never its value: `wheelgap: note: requirements.txt sets --extra-index-url,
which wheelgap doesn't read: its packages were checked on PyPI only, so a package pip finds elsewhere may install
differently`. `-q` drops the note, and a run with `--index-url` or a `WHEELGAP_INDEX_URL` mirror doesn't print it,
since that run doesn't check PyPI. An index option on a line with `-r` or `-c` (`--find-links wheels -r base.txt`)
gets no note: pip 26.3 reads the file and ignores the line's other options, so it looks on PyPI too.

**`--deps`:** with [`--deps`](cli.md#deps) a requirements file is resolved instead — every requirement line is a
root, and the whole tree it installs is checked per target. Constraint files and `--no-binary` / `--only-binary`
apply to the resolved packages too. A fully pinned file that already lists what it installs (`pip-compile` output
with `--hash` lines or `# via` comments) needs no resolving: wheelgap says so and checks it as written.

With `--deps`, a constraint with an environment marker applies only on the targets where its marker
holds, as pip evaluates it: `urllib3<2; python_version < "3.12"` constrains Python 3.11 but not Python 3.14, and
`extra == "…"` never holds on a constraint. A constraint on a package the tree doesn't install imposes nothing. When a
target can't decide a marker on an installed package (`platform_release`, `platform_version`, or `platform_machine`
on a target such as `linux-armv7l`), wheelgap refuses rather than guess, as it does for a URL or path constraint.

## environment.yml

Pass a conda `environment.yml` or `environment.yaml` (or another `environment*.yml` / `environment*.yaml` name, such
as `environment-dev.yml`) by explicit path (directory search doesn't pick it up) to check the PyPI packages listed
under `dependencies:` → `pip:`:

```bash
wheelgap environment.yml -p 3.12 -t linux-aarch64,win-arm64
```

**Read:** each item in a `pip:` list is read as a [requirements file](#requirements-files) line, so pins, ranges,
markers, `-r` / `-c` includes, `--no-binary` / `--only-binary` and `--deps` work the same way. An `--index-url` or
`--extra-index-url` entry in `pip:` marks the file as not PyPI-only, and gets the same note, just as in a requirements file.

**Skipped:** conda dependencies (such as `python=3.12`, `numpy`, `pip`) are listed as skipped (`conda`), and editable,
path and URL lines under `pip:` are skipped as in a requirements file. When the `pip:` list is empty (or absent),
wheelgap reports `No packages to check.` and exits `0`.

**`--deps` and conda packages:** with `--deps`, every dependency of the `pip:` packages is looked up on PyPI, including
ones conda installs (such as `numpy`). pip skips a requirement that conda already satisfied in the environment, so
wheelgap can report a gap that pip would never hit there, or check a different version than conda installed.

The file is read with wheelgap's strict block-YAML reader. `#` comments (whole-line, or after a space at the end of a
line) and a one-line `channels: [conda-forge, defaults]` are read; `dependencies` and `pip:` must be block lists
(`- name` lines). YAML constructs it doesn't understand (anchors, aliases, other flow collections, block scalars,
tabs) are input errors (exit 2) rather than guesses.

## pyproject.toml

Libraries usually don't commit a lockfile. Pass the library's `pyproject.toml` to check the dependencies it declares
in `[project] dependencies`:

```bash
wheelgap pyproject.toml -t win-arm64 -t pyodide
# Add [project.optional-dependencies] groups
wheelgap pyproject.toml --extra test
wheelgap pyproject.toml --all-extras
# Add PEP 735 dependency groups, or check only those groups
wheelgap pyproject.toml --group test
wheelgap pyproject.toml --only-group docs
# Resolve what the declared requirements and the selected extra install
wheelgap pyproject.toml --extra test --deps -t win-arm64 --explain
```

Each requirement is read like a line of a requirements file, so ranges and bare names use the newest matching release
and markers are respected. A range picks the newest release whose `Requires-Python` allows each `-p` version, so with
several it can pick a different release per Python ([how it works](how-it-works.md)). A bare name always checks the
latest release, even for a `-p` version that can't install it, so it can show false gaps. Only direct
dependencies are checked by default and wheelgap prints a note saying so. Add [`--deps`](cli.md#deps) to resolve them
and the selected extras and dependency groups using PyPI's metadata for each target. Projects that configure a custom
installer index or source need a lockfile for this check.

If `[project] requires-python` leaves out a `-p` Python (every clause counts, as pip applies them), that target is
still checked and a note names it; an invalid `requires-python` is ignored with a note.

`[dependency-groups]` (PEP 735) is read with no group selected by default, as with `pip install --group`. `--group NAME`
adds a group to the project's dependencies; `--only-group NAME` checks only the named groups (combine it with `--group`
to add more, but not with `--extra` or `--all-extras`). Group names are normalized, `{include-group = "name"}` entries
include another group, and duplicate requirements count once. A requirement on the project itself in a group installs
the project: `mylib` adds the project's dependencies, and `mylib[test]` adds them and the `test` extra, with
`--only-group` too. Unknown groups, include cycles, malformed entries and names that collide once normalized are input
errors (exit 2); a group that isn't selected isn't read further than its name. `--no-dev` stays an error because no
group is included by default. A `dependencies` listed in `[project] dynamic` is an input error (exit 2), as are dynamic
`optional-dependencies` with `--extra`/`--all-extras` and Poetry dependencies kept in `[tool.poetry.dependencies]`
(check `poetry.lock` instead).

A `pyproject.toml` with `[dependency-groups]` and no `[project]` table requires `--group NAME` or `--only-group NAME`.
Both flags check the selected groups, including any included groups, and work with `--deps`. `--extra` and
`--all-extras` are input errors because the file has no `[project] optional-dependencies`.
A file like that which still builds a package stays an input error (exit 2): one with a `[build-system]` table, a
`setup.py` or `setup.cfg` beside it, `[tool.flit.metadata]` or `[tool.pdm] dependencies`, since its dependencies are
kept where wheelgap doesn't read them (check a lockfile instead).

`check_lockfile()` and `wheelgap diff` don't read `pyproject.toml` as an input.

**uv settings and sources:** the requirements are answered the way uv installs them. uv's `no-build`, `no-binary`,
`no-build-package` and `no-binary-package` are applied as for a [`uv.lock`](#uvlock), from the nearest `uv.toml` or
`pyproject.toml` `[tool.uv]` from the file's folder up (for a uv workspace member, the workspace root's settings, as
uv ignores the member's own). `--deps` refuses a file that sets one. `[tool.uv.sources]` is followed too, the
workspace root's included for a member (the member's own entry wins): a `git`, `url`, `path` or `workspace` source is
skipped with that reason (`editable` for an editable path), and an `index` source takes the package from that
`[[tool.uv.index]]`, so it's skipped as a private index unless that index is PyPI (or `WHEELGAP_INDEX_URL`). A
package whose source depends on a marker, extra or group is `unknown`, and so is one whose source names an index the
project's own `[[tool.uv.index]]` (or the workspace root's) doesn't define, which uv refuses even when a `uv.toml`
defines it. `no-sources = true` turns the sources off. A package without a source of its own goes to PyPI only when
the uv settings name no other index: with an `[[tool.uv.index]]` marked `default = true`, `index-url` or
`default-index`, it's skipped as a private index (unless that index is PyPI or `WHEELGAP_INDEX_URL`), and with
an index that isn't `explicit`, `extra-index-url`, `find-links` or `no-index`, it's `unknown`, since uv may take it
from there first. Either way its name isn't sent to PyPI. When wheelgap can't tell whether the file is a workspace
member and the answer would depend on it, it's an input error, and so is a first `pyproject.toml` above it that
doesn't parse, as for a [`uv.lock`](#uvlock). Settings in your user-level uv config or in `UV_NO_BUILD`-style
environment variables aren't read.

uv's `exclude-newer` upload cutoff is applied: see [Resolution upload cutoffs](#resolution-upload-cutoffs).

## Python scripts

A single-file script can declare what it needs in a `# /// script` block
([PEP 723 inline script metadata](https://packaging.python.org/en/latest/specifications/inline-script-metadata/)),
which `uv run` and `pipx run` install before running it. Pass the `.py` file to check those dependencies:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "rich"]
# ///
```

```bash
wheelgap example.py -t win-arm64
# Resolve what the dependencies install
wheelgap example.py --deps -t win-arm64 --explain
```

The script is never run or imported: wheelgap finds the blocks with the specification's regular expression and reads
the `script` one as TOML. A file with no `script` block, two `script` blocks, invalid TOML or a file that isn't UTF-8
is an input error (exit 2); a `# /// script` line inside another block's content isn't a block of its own.

`dependencies` is read like `[project] dependencies` in a [`pyproject.toml`](#pyprojecttoml): only the direct
requirements are checked, with a note saying so, unless you add [`--deps`](cli.md#deps) to resolve what they install
for each target. If `requires-python` leaves out a `-p` Python, that target is still checked and a note names it.
A script has no extras or dependency groups, so `--extra`, `--all-extras`, `--group`, `--only-group` and `--no-dev` are
errors here.

If the block's `[tool.uv]` sets an index or sources (or a `uv.toml` or `pyproject.toml` beside the script or above it
does), its packages may not be PyPI's: `--deps` refuses the script, and their names aren't sent to the services other
flags use ([which names leave the run](how-it-works.md#pypi-origin)). [`--fix`](cli.md#fix) gives the `[tool.uv]`
lines to add to the block for `uv run`. To check what the script installs, pass its lock instead
(`example.py.lock`, from `uv lock --script`), which is read as a [`uv.lock`](#uvlock). Without `--deps`,
`check_lockfile()` and `wheelgap diff` don't read a script.

The block's `[tool.uv.sources]` and build restrictions are followed as in a [`pyproject.toml`](#pyprojecttoml), with
the block's settings combined with the nearest `uv.toml` or `pyproject.toml` `[tool.uv]` from the script's folder up,
as `uv run` combines them (see [uv.lock](#uvlock)). When nothing is found from the script's folder up, `uv run` uses
the settings of the folder it runs in, which wheelgap can't know. Settings in your user-level uv config or in
`UV_NO_BUILD`-style environment variables aren't read.

A `.py` argument that isn't an existing file, such as `discord.py`, is still a [package name](#package-names).

The block's `[tool.uv]` can also set an [upload cutoff](#resolution-upload-cutoffs).

## Resolution upload cutoffs

For `pyproject.toml` inputs and PEP 723 scripts, wheelgap applies uv's `exclude-newer` as uv does: a file uploaded
at or after the cutoff is left out, so a release is checked with only its earlier files, and a release with none of
them is passed over. That holds for ranges, unpinned requirements and exact `==` pins alike, with and without
`--deps`: a wheel added to a pinned release after the cutoff isn't counted, and a pinned release uploaded after it is
unknown, since uv won't install it. `exclude-newer-package` gives each package it names its own cutoff (a
timestamp, a date or a duration), or none with `false`.

A cutoff is an RFC 3339 timestamp with an explicit offset (for example `"2025-01-01T00:00:00Z"`), or a `"YYYY-MM-DD"`
date, which includes that whole day in the local time zone, as uv does. It can also be a duration,
uv's [dependency cooldown](https://docs.astral.sh/uv/concepts/resolution/#dependency-cooldowns): `"3 days"`,
`"1 week"`, `"24 hours"`, `"P3D"`, `"PT24H"`. uv counts a duration back from the current time whenever it resolves,
and wheelgap counts it back from the time of the run, so the answer is what `uv lock` would pick now and can change
from one run to the next; a note on stderr gives the cutoff that was used (`-q` drops it).

Durations are read exactly as uv reads them:

- either jiff's "friendly" format (`3 days`, `1w 2d`, `2 hours, 30 minutes`, `1.5h`, `2d 12:30:00`, with unit names
  from `weeks`/`wks`/`wk`/`w` down to `ns`) or an ISO 8601 duration (`P3D`, `P1W2D`, `PT36H`, `P1DT1.5H`);
- a day is 24 hours and a week 7 days, in UTC, whatever the local time zone does;
- a sign or `ago` changes nothing (`-3 days` and `3 days ago` are `3 days`), and `0 days` is the current time;
- months and years are refused, as by uv, since they have no fixed length;
- a file uploaded at the cutoff itself is left out, compared in whole milliseconds.

`exclude-newer = false` means no cutoff. Any other value is an input error that names the file: a duration uv
refuses (`"1 month"`, `"3 dayz"`, `"1.5 days"`), a date or timestamp written another way (`"20250101"`,
`"2025-01-01T00:00:00"` without an offset), and uv's table form (`{ timestamp = ..., span = ... }`), which wheelgap
doesn't read. uv itself warns about a value it can't read and then ignores the file's settings; wheelgap stops, since
the project meant to set a cutoff.

The settings are read from the script's own block, or for a `pyproject.toml` from its `[tool.uv]` table, or from a
`uv.toml` beside it, which uv reads instead. For a uv workspace member, uv uses the workspace root's settings (its
`uv.toml`, else its `[tool.uv]`) and ignores the member's own, and so does wheelgap; it notes a cutoff the member sets
that uv ignores. Membership is read from the root's `[tool.uv.workspace]` `members` and `exclude`: if they use a
pattern wheelgap doesn't match as uv does (`**` or `..`) and a cutoff is set at the member or the root, the file is
refused rather than checked under a guess. For a project outside a workspace with no `[tool.uv]` or `uv.toml` of its
own, uv uses the settings in the nearest folder above that has them; wheelgap doesn't read those, and for any input it
doesn't read user settings, `UV_EXCLUDE_NEWER` or uv's command-line options. It prints a note when a folder above or
`UV_EXCLUDE_NEWER` sets a cutoff. Lockfiles (`uv.lock`, `script.py.lock`) already record the files uv resolved, so no cutoff is applied to
them. For requirements files, `uv pip` applies the cutoff from `[tool.uv]` or a `uv.toml`, but wheelgap doesn't (and
doesn't read `[tool.uv.pip]` `exclude-newer` either): their packages are checked at the newest matching release.

A file without a valid PyPI `upload_time_iso_8601` makes its package unknown rather than being assumed old enough.
wheelgap reads upload times only from PyPI's JSON API, so under `--index-url` a package with a cutoff is unknown.

## Package names

Without a lockfile, name the packages, with or without a version or range:

```bash
wheelgap psycopg2 uvloop numpy==2.3.4 -t windows,win-arm64
```

Each argument is read as a requirement (PEP 508). A pinned version is checked as given; a bare name or a range uses
the newest matching release. An argument that is also the name of a file or directory here is read as that file or
directory, so pin the package to check it on PyPI instead (`wheelgap numpy==2.3.4`). The same goes for the subcommand
names as the first argument: `wheelgap diff==2023.12.6` checks the PyPI package `diff`, and
`wheelgap mcp==2.3.0` the package `mcp`.

`--deps` also checks what the names install, which is where the gap usually is: `mcp` is pure Python,
but on Windows on ARM `pip install mcp` compiles cryptography (`mcp → PyJWT → cryptography`). wheelgap resolves the
tree for each target from PyPI's metadata, following markers and extras as pip evaluates them there, and takes the
newest release that meets every constraint, supports that target's Python and isn't yanked. It isn't pip's resolver:
when constraints only a backtracking resolver could untangle come up, it stops with an error instead of guessing, and a
release whose dependencies it can't read is `unknown`. A lockfile from `uv lock` or `pip-compile` is exact, so prefer
one when you have it. See [`--deps`](cli.md#deps).
