# Comparing two lockfiles

`wheelgap diff` checks two lockfiles and lists the packages that got worse on a target (a wheel that became a source
build, say) and the new packages with gaps, so a dependency bump that drops a platform fails CI.

```bash
wheelgap diff old/uv.lock uv.lock -p 3.13 -t linux-aarch64,musl-aarch64
wheelgap diff origin/main:uv.lock uv.lock -p 3.13 -t linux-aarch64   # the base branch's lockfile, no checkout needed
```

It exits `1` if there are any, `0` if not, and `2` on bad input. `python -m wheelgap.diff OLD NEW` works the same way.

## Options

| option | meaning |
| --- | --- |
| `OLD` | the lockfile before the change: a file, a directory, or git's `REF:PATH` |
| `NEW` | the lockfile after the change, in the same forms |
| `-p`, `--python VER` | target Python, repeatable (default: the running Python); after 0.9.0, `next` and `next-t` work too |
| `-t`, `--target PLATFORM` | target platform, repeatable |
| `--glibc X.Y` | after 0.9.0: glibc version for `linux-*` targets (default: 2.36), e.g. `--glibc 2.28` for RHEL 8 |
| `--musl X.Y` | after 0.9.0: musl version for `musl-*` targets (default: 1.2) |
| `--distro NAME` | after 0.9.0: default glibc/musl version from a distro, e.g. `--distro ubuntu-22.04`; doesn't select `-t`, and an explicit `--glibc` or `--musl` wins over it |
| `--macos X.Y` | after 0.9.0: minimum macOS for `macos-*` targets (default: 14.0) |
| `--json` | print one JSON object instead of the text report |

The same `-p` and `-t` apply to both lockfiles. `[tool.wheelgap]` and `.python-version` aren't read yet, so pass them
explicitly. `--advisory` (after 0.9.0) doesn't work with `wheelgap diff` yet.

## What counts as worse

Both lockfiles are checked and packages are matched by name. Per target:

- `wheel`, `bundled` and `n/a` are all fine and rank the same; `source` is worse than them, and `missing` is worse
  than `source`. So `✓ → ● source` and `● source → ✗ missing` are worse.
- `unknown` (the package couldn't be checked) isn't on that scale, because it says nothing about what pip would do.
  Anything → `unknown` is worse, and `unknown` → `source` or `missing` is worse too; only `unknown` → `wheel`,
  `bundled` or `n/a` is not. `unknown` on both sides counts as worse if the version changed; at the same version it's
  unchanged, and a note on stderr names those packages.
- A package installed from somewhere wheelgap doesn't check (git, a path, a URL, an editable install or a private
  index) is "skipped" on that target and treated like `unknown`. Skipped on both sides is unchanged. A new skipped
  package counts as a new gap, except a uv `virtual` project, which installs nothing.
- A package listed more than once (a `uv.lock` fork) takes its worst status on each target.

A requirements-file path with no name (`./pkg`, or `-e ./pkg` without `#egg=`) is matched by its path, so it shows up
as a new package rather than as the one it replaced.

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
- `unchecked`: the names of packages that were `unknown` on both sides of a target at the same version, so neither
  side could be checked there;
- `summary`: the `worse` and `new` counts.

A skipped cell has the status `skipped` and a reason: `skip_reason` in `new`, and `old_skip_reason` or
`new_skip_reason` in `worse` (`git`, `url`, `private index`, ...). Versions are lists: empty when the input gives no
version, and several for a `uv.lock` fork.

## The `diff` name

`diff` is a subcommand only as the first argument when there's no file or folder named `diff` in the current directory
(then it's read as an input, with a note; `python -m wheelgap.diff` always compares), and a pinned `diff==2023.12.6`
checks the PyPI package of that name.

The [MCP server](mcp.md)'s `diff_lockfiles` tool gives the same answer, for agents that bump dependencies.
