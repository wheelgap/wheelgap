# Python API

```python
import wheelgap

targets = wheelgap.make_targets(python="3.13", platforms=["linux-aarch64", "musl-aarch64"])

result = wheelgap.check_package("numpy", "2.3.4", targets)  # looks the files up on PyPI
result.ok  # True if every target has a wheel
result.gaps  # the targets that don't
result.to_dict()  # {"name": ..., "results": {"3.13 musl-aarch64": "wheel", ...}}

for r in wheelgap.check_lockfile("uv.lock", targets):  # markers and the uv graph respected
    print(r.name, r.version, [t.platform for t in r.gaps])
```

The package ships a `py.typed` marker (PEP 561), so mypy, pyright and other type checkers use its annotations.

The Python API doesn't read `[tool.wheelgap]`: pass targets and the other options as arguments, and drop ignored
packages yourself.

## Targets

### `make_targets()`

```python
make_targets(python=None, platforms=None, *, glibc="2.36", musl="1.2", macos="14.0", ios="13.0", android_api="24")
```

Targets from the same names the command line takes: `python="3.13,3.14t"` or a list, and
`platforms=["all", "pyodide", "mobile"]` (names, aliases, groups). `python` defaults to the running Python
(`wheelgap.running_python()`), and `platforms` to `linux-x86_64`, `linux-aarch64`, `macos-arm64` and `win-amd64`.
An unknown name raises `wheelgap.TargetError`. After 0.9.0, `python="next"` (or `"next-t"`) is the newest CPython
this wheelgap version knows.

## Checking

### `check_package()`

```python
check_package(name, version=None, targets=None, *, files=None, pyodide_locks=())
```

Check one package: the latest release, or `version`. Pass `files` (distribution filenames you already have) to work
offline, with no index lookup.

### `check_packages()`

```python
check_packages(packages, targets=None, *, workers=16, pyodide_locks=())
```

Check many packages, looking their files up on the index in parallel. Each package is a name (`"numpy"`, the latest
release) or a `(name, version)` pair.

### `check_lockfile()`

```python
check_lockfile(path, targets=None, *, online=False, offline=False, pyodide_locks=(), environment=None)
```

Check every package a lockfile (or a project directory) installs on at least one target. Environment markers and the
`uv.lock` dependency graph are respected, as on the command line. A `uv.lock` whose `requires-python` leaves out a
target's Python may be missing wheels for it, so its PyPI packages are also looked up on the index (keeping the
lock's files if a lookup fails).

- `online=True` re-checks the lockfile's packages on the index, as `--online` does.
- `offline=True` never contacts an index: it raises `wheelgap.sources.SourceError` if an input has no locked file
  list, and skips the re-check for Python versions outside the lock's `requires-python` (so those versions may show
  false gaps).
- The two can't be combined.
- After 0.9.0, `environment=` picks a `pixi.lock` environment (default: `default`); for a project directory it makes
  `pixi.lock` the lockfile read. Naming one for any other lockfile raises `SourceError`.

`check_lockfile()` doesn't read `pyproject.toml` as an input.

### Pyodide

To count packages that Pyodide ships itself, as `--pyodide-lock` does, pass
`pyodide_locks=[wheelgap.pyodide.load_pyodide_lock("pyodide-lock.json")]` (a path or https URL, one lock per Python)
to `check_packages()`, `check_package()` or `check_lockfile()`. They show as `bundled` on pyodide targets for the
lock's Python, and the result's `bundled_version` is the version Pyodide ships. A different version from your pin or
range still counts as `bundled` (`ok` stays `True`, as in the CLI), so check `bundled_version_differs`; `to_dict()`
includes both as `"bundled"` and `"bundled_version_differs"`. A lock that no pyodide target can use raises
`PyodideLockError`, as on the command line.

## Results

Each check returns a `Result` (a list of them for `check_packages()` and `check_lockfile()`):

| attribute | meaning |
| --- | --- |
| `name`, `version` | the package and the version checked |
| `statuses` | a `wheelgap.Status` for each `Target`: `WHEEL`, `SOURCE`, `MISSING`, `UNKNOWN`, `NOT_NEEDED` (`n/a`), `BUNDLED` |
| `error` | why the package is `unknown`, otherwise `None` |
| `files` | the distribution filenames that were checked |
| `ok` | `True` if no target has a gap |
| `gaps` | the targets with a gap (`source`, `missing` or `unknown`) |
| `bundled_version`, `bundled_version_differs` | with `pyodide_locks`: see [Pyodide](#pyodide) |
| `to_dict()` | the package in the shape of a `--json` package entry, keyed by each target's full label (`"3.13 musl-aarch64"`, with the Python even when only one is checked) |

### Explaining a gap

!!! note "After 0.9.0"
    `rejected`, `reasons`, `via` and `to_dict(explain=True)` are not in 0.9.0.

```python
for r in wheelgap.check_lockfile("uv.lock", targets):
    for target, why in r.reasons.items():  # only targets with a gap
        print(target.label(True), " → ".join(r.via.get(target, ())))  # e.g. app → pandas → numpy
        print(why)
```

For each `source` or `missing` target (not `n/a`, `bundled` or `unknown`, where an `unknown` result's `error` says
why), a result explains the gap the way `--explain` does:

- `result.rejected` maps the target to `(filename, reason)` pairs for every wheel it can't use, closest misses first,
  such as `abi3 wheels can't be used on free-threaded Python (only abi3t ones can)`, or `yanked` for a wheel that fits
  but that pip skips because the release has other files it can use.
- `result.reasons` is the same as text: one `filename  reason` per line, with no column alignment and no limit on the
  number of wheels (the command line shows 8), or `no wheels published for this version`, plus a line such as
  `there is an sdist, but pyodide can't build packages from source` on the targets that can't build sdists (Pyodide,
  iOS and Android).
- `result.via` maps the same targets to the dependency path from a root down to the package, as `--explain`'s `via:`
  line shows it: `("app", "pandas", "numpy")`, or `("(dev group)", "pytest")` when a dependency group pulls it in.
  `check_lockfile()` fills it for `uv.lock`, and for `poetry.lock` files older than lock-version 2.1; it's empty for
  other inputs and for `check_package()`.

`rejected` and `reasons` are worked out when first read; `via` is filled in when the result is made. `to_dict()` leaves
all three out, and `to_dict(explain=True)` adds `"rejected"` and `"via"` in the same shapes as
`wheelgap --json --explain`: each keyed by target label, `rejected` as `{"file", "reason"}` objects.
