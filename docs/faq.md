# FAQ

## Why does it say source when pip installs a wheel?

Run the same command with `--explain`: it lists every wheel the target rejected and why. The usual reasons:

- **You're checking a different target from the machine pip ran on.** wheelgap checks the targets you name, not the
  machine it runs on. A wheel for your x86_64 laptop doesn't help an ARM server, and a `manylinux` (glibc) wheel
  doesn't install on Alpine (musl).
- **The target's floor is older than your machine.** wheelgap assumes glibc 2.36, musl 1.2, macOS 14.0, iOS 13.0 and
  Android API 24 unless you say otherwise. pip on a machine with glibc 2.39 can install a `manylinux_2_39` wheel that
  a glibc 2.36 target can't. Pass the version of the system you deploy to: `--glibc 2.39`, `--distro ubuntu-24.04`,
  `--macos 15.0`, or `--dockerfile Dockerfile`.
- **A different Python.** Wheels are built per Python version, and free-threaded Python (`3.14t`) can't use `abi3`
  wheels, so a package with only `abi3` wheels builds from source there.
- **Your lockfile is older than the wheels.** Lockfiles record the files that existed when you locked. If the project
  uploaded wheels for that version later, `--online` re-checks the lockfile against PyPI. A `uv.lock` made with
  `environments` restrictions also leaves out wheels for other platforms; `--online` helps there too.
- **A different version.** For a version range or a bare name, wheelgap picks the newest matching release (see
  [Version ranges](how-it-works.md#version-ranges)), which may have no wheel where an older one does. Pin the version
  you install.
- **A yanked wheel.** pip skips a yanked wheel when the release has other files it can use, so it builds the sdist;
  after 0.9.0, `--explain` names such a wheel with the reason `yanked`.

If none of these explain it, wheelgap may be wrong, and that's the most useful bug report there is. Check the cell with
`pip download --only-binary=:all:` for the target's tags, as
[AGENTS.md](https://github.com/wheelgap/wheelgap/blob/main/AGENTS.md#no-issue-fits-find-a-real-bug) describes, and
open a [bug report](https://github.com/wheelgap/wheelgap/issues/new?template=bug_report.yml) with both outputs.

## Why does it say missing when the package has an sdist?

On `pyodide`, iOS and Android targets, packages are installed from wheels only: there's no compiler, so an sdist can't
be used and a package without a matching wheel shows as `✗ missing`. `--explain` says so
(`there is an sdist, but pyodide can't build packages from source`).

## What does unknown mean?

wheelgap couldn't check the package, so it doesn't guess. The package's `error` in `--json` says why: a lookup that
failed, a version that isn't on the index, or a constraint it can't apply. `unknown` counts as a gap and exits 1, even
with `--fail-on missing`.

## Why is a package skipped?

Packages that don't come from an index, such as Git, path, URL and editable dependencies, have no published wheels to
check, so they're listed on the `skipped` line with the reason instead. Packages pinned to a private index that the
lockfile has no files for are skipped as `private index`, so their names aren't sent to PyPI.

## Does it resolve dependencies?

No. Lockfiles already list every package; wheelgap reads them and respects their environment markers. A requirements
file or package names are checked as given, without their dependencies, so use a lockfile or a fully pinned
requirements file (from `uv pip compile`, `pip-compile` or `uv export`) for the full picture.

## Does it need network access?

Not for lockfiles that record their files (`uv.lock`, `pylock.toml`, and most `poetry.lock` and `pdm.lock` files), as
long as their `requires-python` includes the Pythons you check. Requirements files, `Pipfile.lock`, `pyproject.toml`,
package names and (after 0.9.0) `pixi.lock` are looked up on PyPI or the index you choose. `--offline` guarantees no
index is contacted. See [Index lookups](how-it-works.md#index-lookups). After 0.9.0, the opt-in `--notes`,
`--build-info`, `--conda-forge` and `--suggest` make requests of their own, for [some package names](how-it-works.md#pypi-origin).

## Why does a Windows-only package show `–` on Linux?

Its environment marker (`sys_platform == 'win32'`) excludes it there, so it isn't installed on that target and isn't a
gap.

## How do I accept a gap I can't fix?

`--ignore NAME`, or `--ignore NAME:TARGET` for one target only, or `ignore = [...]` in `[tool.wheelgap]`. See
[`--ignore`](cli.md#ignore). To accept source builds but still fail on packages that can't install at all, use
`--fail-on missing`.

## Which version of the docs is this?

These pages follow the `main` branch. Anything marked **after 0.9.0** isn't released yet; the
[changelog](https://github.com/wheelgap/wheelgap/blob/main/CHANGELOG.md) lists it under "Unreleased".
