# Recipes

Practical ways to check your project's dependencies with wheelgap.

## Multi-arch images

Check both architectures before building an image for AMD64 and ARM64. This
example uses `uv.lock` and a `Dockerfile` whose final stage is based on an
official `python` image or a Python-tagged `ghcr.io/astral-sh/uv` image, such as
`FROM python:3.13-slim-bookworm`, without pinning it to one architecture. Run it from the build context directory:

```bash
wheelgap uv.lock --dockerfile Dockerfile -t linux/amd64,linux/arm64 &&
  docker buildx build --platform linux/amd64,linux/arm64 \
    --output type=oci,dest=app.tar .
```

- The same Docker platform names select both architectures in wheelgap and
  Buildx. `--dockerfile` supplies the Python version and libc settings from
  the final image; for a supported Alpine image it checks musl wheels instead.
- `&&` starts the build only if the check succeeds. With the default failure
  policy (unless `fail-on` is set in `pyproject.toml`), a source build, missing
  distribution or unknown result on either architecture stops it. Input errors
  stop it too.
- In a CI script that runs under `set -e`, keep this as the step's last
  command, or put `wheelgap` and `docker buildx build` on separate lines: a
  failed check on the left of `&&` doesn't end the script by itself.
- Use a Buildx builder configured for both platforms and OCI export, such as
  the `docker-container` builder in Docker's
  [multi-platform guide](https://docs.docker.com/build/building/multi-platform/).
  The [OCI output](https://docs.docker.com/reference/cli/docker/buildx/build/#oci)
  writes both image variants to `app.tar`.

The check reports wheel availability for the lockfile, not whether the image
build or application will succeed. Match any dependency-selection flags to
what your Dockerfile installs.

## Free-threading readiness

List packages that need a source build, have no usable distribution, or couldn't
be looked up on at least one common native platform for free-threaded Python 3.14. With `wheelgap`
and `jq` installed, run this in your project directory:

```bash
set -o pipefail
wheelgap -p 3.14t -t all --json --exit-zero \
  | jq -r '.packages[] | select([.results[]] | any(. == "source" or . == "missing" or . == "unknown")) | .name'
```

- `-p 3.14t` checks free-threaded Python; you do not need to run wheelgap on that
  interpreter.
- `-t all` checks the eight common native platforms. Replace it with the targets
  you deploy to if you only need a subset.
- `--exit-zero` keeps gaps from failing this informational command.
  Bash's `pipefail` still exposes input errors from wheelgap.
- The filter reads the **values** of `results`, so it keeps working if you add
  more Pythons (for example `-p 3.13,3.14t`), though the list then includes gaps
  on those Pythons too.

Sample output (from wheelgap's own `tests/fixtures/uvproj` test project):

```text
fastlib
nativeext
puresdist
winonly
```

This reports wheel availability, not whether a package is safe to use without
the GIL. A package listed because its result is `unknown` couldn't be looked up;
its `error` field in the JSON says why.

## Monorepos with separate lockfiles

When projects `a/` and `b/` each have a `uv.lock`, check both from the
repository root with the same Python and deployment platform:

```bash
wheelgap a/uv.lock b/uv.lock -p 3.13 -t linux-aarch64
```

This produces one report for both inputs. The report doesn't say which
lockfile a row comes from, and a package locked in both projects is listed once
for each; use the loop below to see gaps per project. It exits `1` when a gap
fails the check (see `--fail-on`) and `2` for bad input, so a CI job can fail
when either project needs attention.

Only `a/pyproject.toml`, next to the **first input**, supplies
`[tool.wheelgap]` defaults for this combined run; `b/pyproject.toml` does not.
The explicit `-p` and `-t` flags override those two defaults for both projects.
Other settings from the first project, such as `glibc`, `ignore`, `fail-on`,
`extras` and `groups`, still apply to both. Use the combined command when those settings
are intended to be shared.

To respect each project's own configuration instead, run a separate check
for each lockfile in a shell script or CI step:

```bash
for lockfile in a/uv.lock b/uv.lock; do
  wheelgap "$lockfile" || exit "$?"
done
```

Each invocation reads `[tool.wheelgap]` next to its own lockfile. Configure
the intended Python and targets there, or add `-p` and `-t` to the command.
The loop stops at the first failing check and exits with its code, so later
lockfiles aren't checked and a later successful check can't hide an earlier gap
or input error.

## Using wheelgap from an agent or script

A coding agent or a script should read wheelgap's exit code and its `--json`
output, not the table. Nothing needs installing:

```bash
uvx wheelgap@latest uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --json > wheelgap.json
```

- `uvx wheelgap@latest` runs the newest release from PyPI. Plain
  `uvx wheelgap` can run an older one: uv uses a version installed with
  `uv tool install` unless a version is requested, and may reuse one from its
  cache. Pin a release, such as `uvx wheelgap@0.8.0`, when the result must not
  change between runs, and check `--help` on the same command for the flags
  that release has. `pipx run wheelgap` works too.
- Always pass `-p` and `-t`. Without them, the Python comes from
  `[tool.wheelgap]`, a `.python-version` file or the interpreter wheelgap runs
  on, in that order, and the targets
  from `[tool.wheelgap]` or the built-in defaults, so the same command can check
  different things on different machines. `uvx wheelgap --list-targets` lists
  the target names.
- Lockfiles (`uv.lock`, `pylock.toml`, `poetry.lock`, `pdm.lock`) are read
  without network access, as long as their `requires-python` includes the Python
  you check. Requirements files, `Pipfile.lock`, package names and lockfile
  entries that record no files (as in an old-format `poetry.lock`) are looked
  up on PyPI.

**Exit codes:** `0` no gaps, `1` gaps found, `2` bad input. A package that
couldn't be looked up (`unknown`) counts as a gap and exits `1`, even with
`--fail-on missing`, so check its `error` before reporting it as a real gap.
With `--json`, a bad
input (a missing lockfile, an unknown target, a bad `[tool.wheelgap]` value)
prints `{"error": {"message": "...", "type": "..."}}` on stdout as well as the
`wheelgap: error:` line on stderr. A command-line usage error, such as an
unknown flag, is plain text on stderr only. `--exit-zero` exits `0` even when
gaps are found, for informational runs; bad input still exits `2`. Add
`set -o pipefail` when you pipe the output, so input errors still fail.

**Reading `--json`:** notes go to stderr, so stdout holds only the JSON.
`packages` has one entry per package with its `name`, `version`, `error` and
`results`; a range that picks a different release for different `-p` versions
has one entry per release, with `n/a` for the targets that use the other.
`results` is keyed by target label: just the platform
(`"linux-aarch64"`) when one Python is checked, or Python and platform
(`"3.13 linux-aarch64"`) with several. Rather than building the key, look it up
in the top-level `targets` list, which gives each target's `label`, `python` and
`platform` (the [README](https://github.com/wheelgap/wheelgap#usage) has a
Python example). Each result is one of:

| value | meaning |
| --- | --- |
| `wheel` | a compatible wheel exists |
| `source` | only an sdist, so pip or uv compiles it at install time |
| `missing` | no wheel and no sdist, so the install fails |
| `unknown` | couldn't be looked up; the package's `error` says why |
| `n/a` | not installed on this target (environment markers exclude it), or this entry is another release's (see above) |
| `bundled` | Pyodide ships its own build (with `--pyodide-lock`) |

`skipped` lists packages that weren't checked (such as editable, path or Git
dependencies), `ignored` the names matched by `--ignore`, and `summary` the
counts. Later versions may add keys, so ignore any you don't know;
[AGENTS.md](https://github.com/wheelgap/wheelgap/blob/main/AGENTS.md) tells
contributors not to rename or remove them.

**Why is it a gap?** `--explain` lists each rejected wheel and the reason. With
`--json --explain`, each package also gets `rejected` and, for `uv.lock`, `via`
(the dependency path that installs it), both keyed by target label like
`results`.

**Accepted gaps:** `--fail-on missing` (or `fail-on = "missing"` in
`[tool.wheelgap]`) allows source builds and only exits `1` when a package can't
be installed at all or couldn't be looked up. `--ignore psycopg2` (repeatable or comma-separated) takes a
package out of the table, the counts and the exit code, and lists it as ignored.
`ignore = ["psycopg2"]` in `[tool.wheelgap]` makes that the default; `--ignore`
flags replace the configured list rather than adding to it.

### Worked example

Run from a clone of this repository, on its `tests/fixtures/uvproj` test
project, which needs no network access. The commands pin wheelgap 0.8.0 so the
output below stays reproducible.

```bash
uvx wheelgap@0.8.0 tests/fixtures/uvproj/uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --json > wheelgap.json
echo $?
```

```text
1
```

List every gap with its target label:

```bash
jq -r '.packages[] | .name as $name | .results | to_entries[]
  | select(.value == "source" or .value == "missing" or .value == "unknown")
  | "\($name) \(.key) \(.value)"' wheelgap.json
```

```text
fastlib linux-aarch64 source
nativeext linux-x86_64 source
nativeext linux-aarch64 source
puresdist linux-x86_64 source
puresdist linux-aarch64 source
```

Ask why `fastlib` has no ARM wheel (this exits `1` too):

```bash
uvx wheelgap@0.8.0 tests/fixtures/uvproj/uv.lock -p 3.13 -t linux-aarch64 --explain
```

```text
wheelgap 0.8.0 · uv.lock · 5 packages · Python 3.13

package    version  linux-aarch64
fastlib    2.0.0    ● source
nativeext  0.3      ● source
puresdist  1.0      ● source

2 packages with wheels for every target hidden (--all to show)

fastlib 2.0.0 on linux-aarch64: ● source
  via: demo → fastlib
  fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl  manylinux_2_17_x86_64 doesn't match linux-aarch64
  fastlib-2.0.0-cp313-cp313-macosx_11_0_arm64.whl                           macosx_11_0_arm64 doesn't match linux-aarch64
  fastlib-2.0.0-cp313-cp313-win_amd64.whl                                   win_amd64 doesn't match linux-aarch64

nativeext 0.3 on linux-aarch64: ● source
  via: demo → fastlib → nativeext
  no wheels published for this version

puresdist 1.0 on linux-aarch64: ● source
  via: demo → puresdist
  no wheels published for this version

skipped 2 non-index packages: demo (editable), gitdep (git)
3 of 5 packages have gaps: 3 will build from source.
```

Source builds are acceptable here, so check that nothing fails to install:

```bash
uvx wheelgap@0.8.0 tests/fixtures/uvproj/uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --fail-on missing -q
echo $?
```

```text
3 of 5 packages have gaps: 3 will build from source.
gaps: fastlib, nativeext, puresdist
0
```
