# Comparing two lockfiles

`wheelgap diff` checks two lockfiles and lists the packages that got worse on a target (a wheel that became a source
build, say) and the new packages with gaps, so a dependency bump that drops a platform fails CI.

```bash
wheelgap diff old/uv.lock uv.lock -p 3.13 -t linux-aarch64,musl-aarch64
wheelgap diff origin/main:uv.lock uv.lock -p 3.13 -t linux-aarch64   # the base branch's lockfile, no checkout needed
```

It exits `1` if there are any, `0` if not, and `2` on bad input. `python -m wheelgap.diff OLD NEW` works the same way.

Packages are looked up on PyPI, or on the JSON API mirror in `WHEELGAP_INDEX_URL`. As in the main command, a
`WHEELGAP_INDEX_URL` ending in `/simple` is refused before any lookup (exit 2), since it isn't a JSON API (0.13.0 looked
every package up there, so all were `unknown`). `wheelgap diff` has no `--index-url` for a simple index yet; check
each lockfile with `wheelgap --index-url` instead. For the same reason, `index-url` in `[tool.wheelgap]` is refused
(exit 2) when it's set in the `pyproject.toml` beside either input (for a `REF:PATH`, the one at that ref), or in the
current directory's with `--deps`: the main command would check those packages on that index, and 0.13.0's diff
checked PyPI instead without a word. Check each lockfile with `wheelgap` there.

## Comparing two package versions

To compare a package's whole dependency tree between two releases without a lockfile, give two pinned specs and
`--deps`:

```bash
wheelgap diff mcp==2.2.0 mcp==2.3.0 --deps -p 3.14 -t win-arm64
```

Each side is resolved from PyPI per target, as [`--deps`](cli.md#deps) does, and the two trees are compared as two
lockfiles are. Each spec is an exact `name==version`, with extras if you like (`mcp[cli]==2.3.0`); a path, URL,
range or marker is refused (exit 2), and so is a pip or uv index variable such as `PIP_INDEX_URL`, since this mode
reads PyPI only. If the resolver can't settle on releases, it exits 2 with its message; a package whose dependencies
it couldn't read is `unknown`, as in a lockfile.

## Options

| option | meaning |
| --- | --- |
| `OLD` | the lockfile before the change: a file, a directory, or git's `REF:PATH`; with `--deps`, a pinned `name==1.2` |
| `NEW` | the lockfile after the change, in the same forms |
| `-p`, `--python VER` | target Python, repeatable (default: `python` in `[tool.wheelgap]`, else `.python-version`, else the running Python); `next` and `next-t` work too |
| `-t`, `--target PLATFORM` | target platform, repeatable |
| `--glibc X.Y` | glibc version for `linux-*` targets (default: 2.36), e.g. `--glibc 2.28` for RHEL 8 |
| `--musl X.Y` | musl version for `musl-*` targets (default: 1.2) |
| `--distro NAME` | default glibc/musl version from a distro, e.g. `--distro ubuntu-22.04`; doesn't select `-t`, and an explicit `--glibc` or `--musl` wins over it |
| `--macos X.Y` | minimum macOS for `macos-*` targets (default: 14.0) |
| `--ios X.Y` | minimum iOS for `ios-*` targets (default: 13.0) |
| `--android-api N` | minimum Android API level for `android-*` targets (default: 24) |
| `--json` | print one JSON object instead of the text report |
| `--deps` | `OLD` and `NEW` are pinned releases: resolve and compare their whole trees ([above](#comparing-two-package-versions)) |
| `--fix` | **after 0.14.0**: also say how to fix each listed gap, as the main command's [`--fix`](cli.md#fix) does ([below](#fix)); not with `--deps` |

The same `-p` and `-t` apply to both lockfiles. `wheelgap diff` reads `[tool.wheelgap]` (`python`, `targets`, `glibc`,
`musl`, `distro`, `macos`, `ios`, `android-api`), `[tool.uv] required-environments` and `.python-version` from the
`NEW` input's project directory only, never `OLD`'s, with command-line flags winning over them just as in the main
command. For a `REF:PATH` input that's the folder of `PATH` in the current directory's working tree, not the copy at
`REF`, so uncommitted settings apply; with `--deps`, it's the current directory. Keys `diff` doesn't use yet (`ignore`,
`advisory`, `groups`, ...) are accepted and ignored. `--advisory` doesn't work with `wheelgap diff` yet.

## What counts as worse

Both lockfiles are checked and packages are matched by name. Per target:

- `wheel`, `bundled` and `n/a` are all fine and rank the same; `source` is worse than them, and `missing` is worse
  than `source`. So `✓ → ● source` and `● source → ✗ missing` are worse.
- `unknown` (the package couldn't be checked) isn't on that scale, because it says nothing about what pip would do.
  Anything → `unknown` is worse, and `unknown` → `source` or `missing` is worse too; only `unknown` → `wheel`,
  `bundled` or `n/a` is not. `unknown` on both sides counts as worse if the version changed, since the new version
  was never checked; at the same version it's unchanged, and a note on stderr names those packages.
- A package installed from somewhere wheelgap doesn't check (git, a path, a URL, an editable install or a private
  index) is "skipped" on that target and treated like `unknown`. Skipped on both sides is unchanged. A new skipped
  package counts as a new gap, except a uv `virtual` project, which installs nothing.
- A package listed more than once (a `uv.lock` fork) takes its worst status on each target.
- **After 0.14.0**, a target whose Python either lockfile's `requires-python` leaves out (`-p 3.13` for a lock
  made for `>=3.14`) isn't compared, and a note on stderr says so. A lock isn't made for a Python outside its range:
  uv keeps only the wheels that range can use, and the main command
  [looks its PyPI packages up again](how-it-works.md#index-lookups) for that Python but can't do that for packages
  from another index, so a lock that moved its packages to a mirror would look as if they all lost their wheels
  there. Nothing on such a target counts as worse, and the last line says "Nothing got worse on the compared
  targets." (or "Nothing was compared." when every target is left out) rather than "Nothing got worse."
- When only the NEW lockfile leaves a Python out, the change dropped it: `uv sync` there now fails. The note says so
  ("this change drops Python 3.13 support: the new lockfile no longer supports Python 3.13 (old: >=3.12, new:
  >=3.14)"), and the Action's [pull request gate](ci.md#pr-gate) adds a warning to the log. It still doesn't make the
  exit code 1, which only `worse` and `new` do.
- When only the OLD lockfile leaves a Python out (the range widened), the new lockfile's gaps on that Python aren't
  shown. Check it with `wheelgap NEW -p 3.13` to see them.
- A `pdm.lock` whose [`[[metadata.targets]]`](inputs.md#pdmlock) take no patch release of a target's Python is
  treated the same way (`pdm install` refuses it there), with the targets' `requires_python` in the reason, though the
  main command refuses such a target. Their platforms narrow the default targets as a `uv.lock`'s
  `[tool.uv] environments` do: a platform both locks leave out goes, one only one side leaves out is refused.

With a dependency graph (`uv.lock`, Poetry 1.x, or `--deps`), a new or worse transitive package that builds from source, is
missing or couldn't be looked up names the direct dependency that brings it in, as the main command's table does: `cryptography (via mcp)`,
or `(via mcp +1)` when more than one does. The Action's PR comment shows the same.

A requirements-file path with no name (`./pkg`, or `-e ./pkg` without `#egg=`) is matched by its path, so it shows up
as a new package rather than as the one it replaced.

## Fixes {#fix}

**After 0.14.0**, `--fix` adds each listed gap's way out: what [`wheelgap NEW --fix`](cli.md#fix) says for that
package and target, an upgrade to the nearest newer official release with the wheel first, else an unofficial gap
wheel, else none and why. Only the packages the report lists are looked up, on PyPI, so an unchanged gap costs
nothing, and names are judged as `wheelgap NEW --fix` judges them, by the NEW input
([which names leave the run](how-it-works.md#pypi-origin)); a NEW given as `REF:PATH` sends a name only when its lock
records the PyPI index (a `uv.lock`, say), so a requirements file read that way is never looked up. The text
report ends with `--fix`'s section for those gaps, and `--json` adds a `fix` to each `source` or `missing` cell in
`worse` and `new`, and a `fix_summary`, both as the main command's `--json --fix` has them. Like the main command's,
it never changes the exit code. The GitHub Action's [pull request gate](ci.md#pr-gate) puts these fixes in its comment.

```bash
wheelgap diff origin/main:uv.lock uv.lock -p 3.15 -t linux-x86_64,win-arm64 --fix
```

## Git refs

Either input can be a git `REF:PATH` (`origin/main:uv.lock`, `HEAD~1:poetry.lock`) when no file or folder of that name
exists: wheelgap reads it with git into a temporary folder under the same file name (with the `pyproject.toml` beside
it at that ref, if any), so its format is detected and read as usual. `PATH` is relative to the repository root: use
`REF:./uv.lock` from a subfolder. In a shallow CI checkout, fetch the base ref first. A ref or path git can't find exits
2 with git's message, and so does a missing `git`.

## JSON

`--json` prints one object with:

- `version`: the wheelgap version;
- `targets`: each target's `label`, `python` and `platform`, as in the main command's `--json`; the per-target keys
  below are these labels;
- `worse`: each package's `name`, its `old_versions` and `new_versions` lists, and `targets`, with `old_status` and
  `new_status` for each target where it got worse;
- `new`: each new package's `name`, its `versions` list, and `targets`, with the `status` for each target where it
  has a gap;
- `direct` (on `worse` and `new` entries, only when known): per target label, the direct dependencies that bring the
  package in; absent for a direct dependency or an input without a dependency graph;
- `unchecked`: the names of packages that were `unknown` on both sides of a target at the same version, so neither
  side could be checked there;
- `not_compared` (**after 0.14.0**): the targets left out because either lockfile's `requires-python` (or a
  `pdm.lock`'s targets) leaves out their Python ([above](#what-counts-as-worse)), each with its `target` label, its `python`, the `sides` that leave
  it out (`old`, `new` or both; `["new"]` alone means the change dropped that Python) and a `reason`;
- `summary`: the `worse` and `new` counts;
- `fix` (on each target of `worse` and `new`, with `--fix`, **after 0.14.0**): that gap's way out, as `fix` in the
  main command's [`--json`](output.md#json) has it (`kind` `upgrade`, `gap_wheel` or `none`); absent for an `unknown`
  or skipped cell, which `--fix` has no answer for;
- `fix_summary` (with `--fix`): the counts of the listed gaps, as the main command's.

A skipped cell has the status `skipped` and a reason: `skip_reason` in `new`, and `old_skip_reason` or
`new_skip_reason` in `worse` (`git`, `url`, `private index`, ...). Versions are lists: empty when the input gives no
version, and several for a `uv.lock` fork.

## The `diff` name

`diff` is a subcommand only as the first argument when there's no file or folder named `diff` in the current directory
(then it's read as an input, with a note; `python -m wheelgap.diff` always compares), and a pinned `diff==2023.12.6`
checks the PyPI package of that name.

The [MCP server](mcp.md)'s `diff_lockfiles` tool gives the same answer, for agents that bump dependencies.
