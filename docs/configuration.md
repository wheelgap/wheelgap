# Configuration

Projects usually ship to the same platforms, so you can write the defaults down once in `pyproject.toml`.
Command-line flags still win.

```toml
[tool.wheelgap]
python = ["3.13", "3.14t"]
targets = ["linux-x86_64", "linux-aarch64", "musl-aarch64"]
glibc = "2.31"
```

Not sure what to put there? After 0.9.0, [`wheelgap --suggest-targets`](targets.md#suggest-targets) reads the
platforms and Pythons your CI and Dockerfiles already name and suggests these lines.

wheelgap reads the `pyproject.toml` in the folder of the first file or directory you pass (next to the lockfile),
or in the current directory when you pass none. An unknown key, or a value of the wrong type, is an input error
(exit 2).

The table is read by the command line (`wheelgap`), the MCP server's `check_lockfile` and `check_dockerfile` tools,
and the pre-commit hook. The [Python API](python-api.md) and [`wheelgap diff`](diff.md) don't read it: pass the same
settings as arguments there.

Keys that take a list also accept a single string (`targets = "linux-aarch64"`).

## Pythons and platforms

### `python` {#python}

A list of Python versions, as `-p` takes them: `python = ["3.13", "3.14t"]`. Used when `-p` and `--dockerfile`
aren't given, before a `.python-version` file. See [Python versions](targets.md#python-versions).

After 0.9.0, `next` and `next-t` work here too (`python = ["3.14", "next"]`).

### `targets` {#targets}

A list of platforms, as `-t` takes them (names, aliases, Docker names, `all`, `everything`, `mobile`):
`targets = ["linux-x86_64", "linux-aarch64"]`. Used when `-t` and `--dockerfile` aren't given, before uv's
`required-environments`. See [How targets are chosen](targets.md#how-targets-are-chosen).

### `advisory` {#advisory}

*After 0.9.0.* A list of targets whose gaps are reported but never fail the run, as
[`--advisory`](cli.md#advisory) takes them: `advisory = ["win-arm64"]`. `--advisory` on the command line replaces
the list, and `--advisory ""` clears it for one run. A configured target that a run doesn't check (`-t linux`, a
`--dockerfile` image) gets a note on stderr rather than an error.

### `glibc` {#glibc}

The glibc version for `linux-*` targets, as a string: `glibc = "2.31"`. Default 2.36. Same as
[`--glibc`](cli.md#glibc).

### `musl` {#musl}

The musl version for `musl-*` targets, as a string: `musl = "1.2"`. Default 1.2. Same as [`--musl`](cli.md#musl).

### `distro` {#distro}

A distro whose libc version to use, such as `distro = "ubuntu-22.04"`. It supplies a glibc or musl version only, and
`glibc` or `musl` in the same table win over it. See [Distros](targets.md#distros).

### `macos` {#macos}

The minimum macOS for `macos-*` targets, as a string: `macos = "13.0"`. Default 14.0. Same as
[`--macos`](cli.md#macos).

### `ios` {#ios}

The minimum iOS for `ios-*` targets, as a string: `ios = "15.0"`. Default 13.0. Same as [`--ios`](cli.md#ios).

### `android-api` {#android-api}

The minimum Android API level for `android-*` targets, as an integer or a string: `android-api = 28`. Default 24.
Same as [`--android-api`](cli.md#android-api).

### `pyodide-lock` {#pyodide-lock}

A Pyodide release's `pyodide-lock.json`, as a path or `https://` URL: packages Pyodide ships itself count as
`bundled` on `pyodide` targets. A relative path is relative to the `pyproject.toml`. The lock is only read (or
downloaded) on runs that check a `pyodide` target, so it doesn't affect `wheelgap -t linux` in the same project. Same
as [`--pyodide-lock`](cli.md#pyodide-lock); see [Pyodide](targets.md#pyodide).

## Dependencies

### `extras` {#extras}

A list of project extras to include: `extras = ["api"]`. Applies to `uv.lock` inputs, and after 0.9.0 to Poetry 2.x
`poetry.lock` inputs too. `--extra` replaces the list; an empty list selects no extras. See
[dependency selection](inputs.md#uvlock).

### `groups` {#groups}

A list of dependency groups to include: `groups = ["docs"]`. Applies to `uv.lock`, Poetry 2.x `poetry.lock` and
`pdm.lock` inputs; other inputs are checked as usual. `--group` replaces the list, `--only-group` suppresses both
`extras` and `groups`, and `--no-dev` removes a configured `dev` group. An empty list selects no additional groups.

### `ignore` {#ignore}

A list of accepted gaps, as [`--ignore`](cli.md#ignore) takes them: `ignore = ["psycopg2"]` on every target, or
`ignore = ["psycopg2:musl-x86_64"]` on one. Any `--ignore` flags replace the configured list rather than adding to
it, and `--ignore ""` clears it for one run.

## Results and output

### `fail-on` {#fail-on}

`"source"` (the default) or `"missing"`. With `fail-on = "missing"`, source builds are accepted and wheelgap only
exits 1 when a package can't be installed at all (or couldn't be checked). Same as [`--fail-on`](cli.md#fail-on).

### `quiet` {#quiet}

`true` or `false`. `quiet = true` shows only the gap summary and package names, as [`-q`](cli.md#quiet) does;
`--no-quiet` turns it off for one run.

### `notes` {#notes}

*After 0.9.0.* `true` or `false`. `notes = true` makes `--explain` also show the coverage tracker's notes, as
[`--notes`](cli.md#notes) does, which sends the names of packages with gaps to wheelgap.github.io; `--no-notes` turns it
off for one run. See [Tracker notes](output.md#notes).

### `build-info` {#build-info}

*After 0.9.0.* `true` or `false`. `build-info = true` makes `--explain` also say what each `source` build needs, as
[`--build-info`](cli.md#build-info) does, which downloads the sdists of packages known to be PyPI's. See
[Build requirements](output.md#build-info).

## Index access

### `offline` {#offline}

`true` or `false`. `offline = true` makes [`--offline`](cli.md#offline) the project default: wheelgap never contacts an
index, and inputs that would need a lookup are errors. `--online` on the command line overrides it for one run.

### `index-url` {#index-url}

A simple index (PEP 691 JSON) to look packages up on instead of PyPI, as [`--index-url`](cli.md#index-url) takes it.
Credentials are refused here, since `pyproject.toml` is committed: pass a URL with credentials on the command line
(`--index-url "$PRIVATE_INDEX"`) instead.
