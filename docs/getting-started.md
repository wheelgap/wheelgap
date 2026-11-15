# Getting started

## Install and run

With [uv](https://docs.astral.sh/uv/), nothing needs installing:

```bash
uvx wheelgap
```

Or with pipx:

```bash
pipx run wheelgap
```

Or install it: `pip install wheelgap`. wheelgap needs Python 3.11 or later, and depends only on
[`packaging`](https://packaging.pypa.io/).

Run it in a project directory. It finds `uv.lock`, `pylock.toml`, `poetry.lock`, `pdm.lock`, `Pipfile.lock`,
`requirements.txt` or `requirements.lock` (and, after 0.9.0, `pixi.lock`) on its own, and reads the first of those it
finds. You can also pass a file,
a directory, or package names. [Inputs](inputs.md) has the details.

`uvx wheelgap` can reuse an older copy that uv installed or cached. Run `uvx wheelgap@latest` for the newest release,
or pin one (`uvx wheelgap@0.9.0`) when the result must not change between runs.

## Pick your Python and platforms

With no flags and no configuration, wheelgap checks the Python it runs on (or the one in a `.python-version` file next
to the lockfile) on `linux-x86_64`, `linux-aarch64`, `macos-arm64` and `win-amd64`. Say what you ship to with `-p` and
`-t`:

```bash
# Will my Alpine ARM image compile anything?
wheelgap -p 3.13 -t alpine-arm64

# Is my stack ready for free-threaded Python 3.14?
wheelgap -p 3.14t -t all

# Several Pythons at once, Linux on both architectures
wheelgap uv.lock -p 3.12,3.13,3.14 -t linux-x86_64,linux-aarch64

# No lockfile? Check packages directly (latest version, or pin one)
wheelgap psycopg2 uvloop numpy==2.3.4 -t windows,win-arm64

# Take the Python and platform from the final stage of a Dockerfile
wheelgap --dockerfile Dockerfile
```

`wheelgap --list-targets` lists every platform name. [Targets](targets.md) explains them, and
[Configuration](configuration.md) shows how to write your Pythons and platforms down once in `pyproject.toml`.

## Read the table

```text
package    version  linux-aarch64  musl-aarch64
fastlib    2.0.0    ● source       ● source
```

Each cell is one of:

| cell | meaning |
| --- | --- |
| `✓` | a compatible wheel exists, so the install is fast |
| `● source` | no wheel, only an sdist, so it compiles at install time (needs a compiler, headers, time) |
| `✗ missing` | no wheel and no sdist, so the install fails |
| `–` | not installed on this target (environment markers exclude it) |
| `✓ bundled` | no wheel on PyPI, but Pyodide ships its own build (with `--pyodide-lock`) |
| `? unknown` | wheelgap couldn't check it, for example because the index lookup failed |

Packages that are fine everywhere are hidden; `--all` shows them.

wheelgap exits `0` when there are no gaps, `1` when it finds some, and `2` on bad input, so it can fail a CI job.
[Output](output.md) covers the exit codes and the other output formats.

## Why is it a gap?

`--explain` lists, for each gap, the wheels that were rejected and why, closest misses first:

```bash
wheelgap -t alpine-arm64 --explain
```

```text
armlib 1.4.0 on musl-aarch64: ● source
  armlib-1.4.0-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl  manylinux_2_17_aarch64 is a glibc wheel; musl (Alpine) needs musllinux
```

For `uv.lock` inputs, and `poetry.lock` files older than lock-version 2.1, it also shows the dependency path that
installs the package (`via: myapp → uvicorn → uvloop`). If you think wheelgap got a package wrong, that output (or
`--json --explain`) is all a bug report needs, with no need to share your lockfile.

## Next steps

- Run it in CI: [the GitHub Action, pre-commit and GitLab](ci.md).
- Accept known gaps with [`--ignore`](cli.md#ignore), or, after 0.9.0, watch a platform without failing on it with
  [`--advisory`](cli.md#advisory).
- Not sure which targets you ship to? After 0.9.0,
  [`wheelgap --suggest-targets`](targets.md#suggest-targets) reads them from your CI and Dockerfiles.
- Compare the lockfile before and after a dependency bump with [`wheelgap diff`](diff.md).
- See the [recipes](recipes.md) for multi-arch images, monorepos and free-threading readiness.
