# Is my project ready for Python 3.15?

One command answers the question for your own lockfile, at the platforms you
actually ship:

```console
$ uvx wheelgap -p 3.15 -t linux-x86_64,linux-aarch64,macos-arm64,win-amd64 --fix
```

Point it at a `uv.lock`, `requirements.txt`, `pyproject.toml` or another
[supported input](inputs.md). `-p 3.15` sets the Python version, `-t` lists the
platforms, and `--fix` explains how to close each gap. It never edits your
files.

## A real result

Here is the whole output from checking wheelgap's own `uv.lock` with `--fix`
(one stale pin, on four targets):

```console
$ uvx wheelgap uv.lock -p 3.15 -t linux-x86_64,linux-aarch64,macos-arm64,win-amd64 --fix
wheelgap 0.14.0 · uv.lock · 39 packages · Python 3.15

package  version  linux-x86_64  linux-aarch64  macos-arm64  win-amd64
pyyaml   6.0.3    ● source      ● source       ● source     ● source

38 packages with wheels for every target hidden (--all to show)
skipped 1 non-index package: wheelgap (editable)
linux-x86_64   1 from source
linux-aarch64  1 from source
macos-arm64    1 from source
win-amd64      1 from source
1 of 39 packages has gaps: 1 will build from source.
Run with --explain to see which wheels were rejected and why.

--fix: 4 gaps (1 package × its targets): 4 can be fixed by upgrading to an official release; 0 only by an unofficial gap wheel; 0 have no fix yet.

Upgrade: a newer official release has the wheel:
  pyyaml 6.0.3 -> 6.0.4: linux-x86_64, linux-aarch64, macos-arm64, win-amd64 (within your pyproject.toml constraint pyyaml>=6.0.2)
In the project folder, run uv lock --upgrade-package pyyaml, then uv sync: uv moves each to the newest release your constraints allow, so check that it's at least pyyaml==6.0.4, then run wheelgap again.
wheelgap doesn't resolve dependencies: the rest of them must allow these releases.
```

The `● source` cells are the gaps: pip or uv will compile those packages from
source on the target platform. A `✗ missing` cell would mean there's no wheel
and no sdist either, so the install fails. `--all` also shows the packages that
are already fine (`✓ wheel`).

## The common case is a stale pin

This project's only gap is PyYAML 6.0.3. The 6.0.4 release already ships
CPython 3.15 wheels on all four platforms, so `--fix` leads with the upgrade
and prints the exact command for a `uv.lock`:

```console
uv lock --upgrade-package pyyaml
```

Then run `uv sync` and re-run wheelgap. For a `requirements.txt` or a pinned
`pyproject.toml`, it prints the new pin instead. wheelgap does not resolve your
dependency graph, so check that the rest of your constraints allow the
release. This is the normal outcome: most new-Python gaps in real projects are
stale pins whose fix is already released. The
[3.15 readiness report](reports/python-3-15.md) has the measured numbers; this
page links to it rather than repeating them.

## Free-threaded 3.15t needs its own check

Free-threaded Python 3.15t is a different target. Use `-p 3.15t`:

```console
$ uvx wheelgap uv.lock -p 3.15t -t linux-x86_64,linux-aarch64,macos-arm64,win-amd64
```

An `abi3` (stable-ABI) wheel does not count there: free-threaded builds use a
different ABI, so a package that installs from a wheel on regular 3.15 can
still build from source on 3.15t. wheelgap checks the tags for the target you
name and reports the gap. See [Free-threaded Python](free-threading.md) for the
tag rules and the workaround options.

## When there is no fix yet

If `--fix` has no upgrade to offer, it says so and lists the packages covered
by wheelgap's [gap wheel index](https://wheelgap.github.io/wheels/) — an
unofficial index of wheels built from PyPI sdists, clearly not from the
package maintainers. Use it only where you cannot upgrade, and read
[Installing from the gap wheel index](recipes.md#installing-from-the-gap-wheel-index)
before adding it. The package's own issue tracker is the right place to ask
for an official wheel.

## Next

- [Targets](targets.md) for the platform names and wheel tags.
- [Output](output.md) for `--explain` and the JSON schema.
- [CI](ci.md) to make this a pull-request gate.
