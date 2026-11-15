# Output

wheelgap prints a table by default, or JSON (`--json`) or Markdown (`--markdown`) instead, and can also write a JUnit
XML file (`--junit PATH`). Notes and errors go to stderr, so the stdout of `--json` and `--markdown` holds only the
report.

## Exit codes

| code | meaning |
| --- | --- |
| `0` | no gaps (or `--exit-zero`) |
| `1` | gaps found, including packages that couldn't be looked up (`unknown`) |
| `2` | bad input: a missing lockfile, an unknown target, a bad `[tool.wheelgap]` value, a usage error |

- `--exit-zero` exits `0` even when gaps are found; bad input still exits `2`.
- `--fail-on missing` (or `fail-on = "missing"`) accepts source builds and only exits `1` when a package can't be
  installed at all, or couldn't be checked.
- `--ignore NAME` and `--ignore NAME:TARGET` take accepted gaps out of the exit status.
- After 0.9.0, gaps on `--advisory` targets never change the exit code.

Add `set -o pipefail` when you pipe the output, so input errors still fail.

## Text

```text
wheelgap 0.9.0 · uv.lock · 5 packages · Python 3.13

package    version  linux-aarch64  musl-aarch64
fastlib    2.0.0    ● source       ● source
nativeext  0.3      ● source       ● source
puresdist  1.0      ● source       ● source

2 packages with wheels for every target hidden (--all to show)
skipped 2 non-index packages: demo (editable), gitdep (git)
linux-aarch64  3 from source
musl-aarch64   3 from source
3 of 5 packages have gaps: 3 will build from source.
Run with --explain to see which wheels were rejected and why.
```

Each cell is one of:

| cell | `--json` status | meaning |
| --- | --- | --- |
| `✓` | `wheel` | a compatible wheel exists, so the install is fast |
| `● source` | `source` | no wheel, only an sdist, so it compiles at install time (needs a compiler, headers, time) |
| `✗ missing` | `missing` | no wheel and no sdist, so the install fails |
| `? unknown` | `unknown` | couldn't be looked up; the package's error says why |
| `–` | `n/a` | not installed on this target (environment markers exclude it, or it selects another resolution) |
| `✓ bundled` | `bundled` | no wheel on PyPI, but Pyodide ships its own build (with `--pyodide-lock`) |

Packages that are fine everywhere are hidden; `--all` shows them. A range that picks a different release for
different `-p` versions is one row per release, and the summary counts each row. With several targets, the summary also counts the
gaps on each target that has any (`musl-aarch64  1 from source, 1 missing`). Packages that weren't checked, such as
editable, path or Git dependencies, are listed on the `skipped` line.

With `-q` / `--quiet` (or `quiet = true`), only the summary and the names of the packages with gaps are printed:

```text
3 of 5 packages have gaps: 3 will build from source.
gaps: fastlib, nativeext, puresdist
```

Clean runs without ignored packages are silent. Quiet text output can't be combined with `--explain`
(`--json --explain` and `--markdown --explain` work).

## --explain {#explain}

`--explain` lists, for each gap, the wheels that were rejected and why, closest misses first (up to 8 per target in
the text output):

```text
armlib 1.4.0 on musl-aarch64: ● source
  armlib-1.4.0-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl  manylinux_2_17_aarch64 is a glibc wheel; musl (Alpine) needs musllinux
```

For `uv.lock` inputs, and `poetry.lock` files older than lock-version 2.1 (which wheelgap reads as a dependency graph),
`--explain` also shows why the package is installed at all: the shortest dependency path from your project (or from a
dependency group, shown as `(dev group)`) to the gap package. With extra/group selection it's a path the selection
installs it through, not always the shortest. Paths are worked out per target, since markers can pull a package in on
one platform only.

```text
uvloop 0.21.0 on win-arm64: ● source
  via: myapp → uvicorn → uvloop
```

Other inputs have no dependency graph, so they show no path. If you think wheelgap got a package wrong, this output
(or `--json --explain`) is all a bug report needs, with no need to share your lockfile.

### Tracker notes {#notes}

!!! note "After 0.9.0"
    `--notes` and `notes = true` are not in 0.9.0.

With `--notes` (or `notes = true` in `[tool.wheelgap]`), `--explain` also shows the
[coverage tracker](https://wheelgap.github.io/tracker/)'s
[notes](https://github.com/wheelgap/tracker/blob/main/notes/README.md) for a package with a gap: what the scan can't
know, such as a binary package to use instead, a system library to install first, or an upstream issue to follow. A
note is shown once per package, under its explanation, with the gap platforms it covers and its links:

```text
psycopg2 2.9.10: note from the wheelgap tracker for linux-x86_64 (alternative, checked 2026-11-11)
  - psycopg2 has wheels for Windows x86-64 only. psycopg2-binary has wheels for these platforms (not for free-threaded Python): `pip install psycopg2-binary`. …
    https://www.psycopg.org/docs/install.html#psycopg-vs-psycopg-binary
```

- `--notes` needs `--explain`, and `--no-notes` overrides `notes = true` for one run.
- Only notes for a platform with a gap are shown. A note for `all` platforms covers the nine the tracker scans
  (`linux-x86_64`, `linux-aarch64`, `musl-x86_64`, `musl-aarch64`, `macos-x86_64`, `macos-arm64`, `win-amd64`,
  `win-arm64` and `pyodide`), not other targets. A note that lists Pythons (its `python`, such as `["3.13"]` or
  `["3.14t"]`) is shown only for gaps on those Pythons. The tracker checks the latest release, so a note limited to
  some releases (its `versions`) is shown only when your version is a plain release that matches them.
- With `--json --explain`, each package gets a `notes` list (empty without `--notes` or when there are none), each note
  with the tracker's `platforms`, `kind`, `text`, `links`, `versions` and `updated` (and `python`, when the tracker
  sets it), plus the `targets` (labels, like
  `rejected`'s keys) it covers. `--markdown` puts them in the collapsed section with the rejected wheels, as plain
  text.
- Notes come from a public repository, so treat their text as plain text, not markup. wheelgap shows only notes in the
  tracker's published format, as strictly as the tracker's own checker reads it, and drops any whose text or links
  could pass for a GitHub Actions workflow command (`##[`, or a word starting with `::`).
- **It sends the names of packages with gaps to wheelgap.github.io** (one small file per package), which is why it's
  opt-in: `--explain` on a lockfile otherwise makes no requests. When there's a package it could look up, `--explain`
  says so in one dim line on stderr (not with `-q`). Only names that pass
  [the PyPI-origin rule](how-it-works.md#pypi-origin) are sent, and nothing at all with `--offline`, `--index-url` or a
  `WHEELGAP_INDEX_URL` mirror; `--notes` then says on stderr that notes are off.
- Each request gets one try with a 10-second timeout, all of them together get 15 seconds, and the first connection
  failure stops the rest, so a runner without internet access isn't held up: wheelgap then says on stderr that it
  couldn't reach wheelgap.github.io, or that it didn't answer in time, so notes may be missing (not with `-q`). Lookups
  are cached for 24 hours, a package the tracker doesn't list included, and follow `--no-cache` and `--online`. A note
  that can't be fetched, or a package the tracker doesn't list, adds nothing, and the exit status never depends on
  notes.

### Build requirements {#build-info}

!!! note "After 0.9.0"
    `--build-info` and `build-info = true` are not in 0.9.0.

With `--build-info` (or `build-info = true` in `[tool.wheelgap]`), `--explain` also says what each `source` build
needs, from the `[build-system]` table in the sdist's own `pyproject.toml`, once per package:

```text
rpds-py 0.27.0 on musl-armv7l: ● source
  build needs: at least a Rust toolchain (maturin<2.0,>=1.9)
```

- `maturin` and `setuptools-rust` mean a Rust toolchain; `scikit-build-core`, `scikit-build` and `cmake` mean CMake
  and a C/C++ compiler; `Cython` and `meson-python` mean a C/C++ compiler. Every other requirement is listed by name
  and version range (`at least a C/C++ compiler (cython>=3.0), setuptools, wheel`), since wheelgap can't tell what it
  implies. A requirement with an environment marker counts only if the marker holds on one of the targets that build
  it. At most 50 requirements are shown (then `and N more`), each cut to 200 characters.
- `no compiler` is only said for a `build-backend` that can't run one, named in `requires` from an index:
  `flit_core.buildapi` (from `flit-core` or `flit`), `flit.buildapi` (from `flit`), or `poetry.core.masonry.api`
  (from `poetry-core`, without a `build` script). Without a `build-backend`, pip uses setuptools, which can compile
  anything; an in-tree backend (`backend-path`) is named as such.
- With `--json --explain --build-info`, each package gets `build_requires`: the same requirements, normalized
  (`["maturin<2.0,>=1.9"]`), or `null` when there's no source build or no answer. When build info is off for the
  whole run (see below), the key is left out.
- **It's off by default because it downloads:** lockfiles are otherwise read without contacting the network. Only
  packages that pass [the PyPI-origin rule](how-it-works.md#pypi-origin) are read, `--online` included, so other names
  don't reach pypi.org. Nothing is read in a run with `--index-url`, `WHEELGAP_INDEX_URL` or a `PIP_*`/`UV_*` index
  setting in the environment, which a note on stderr says (not with `-q`), and `--offline` turns it off.
- The whole `.tar.gz` is read in memory, never written to disk, up to 64 MB downloaded, and it gives no answer
  wherever pip could read another `pyproject.toml` from it than wheelgap would, or would fail: several top folders,
  links out of the sdist or written through, hard links to anything but an earlier file of that exact name, links
  alongside non-ASCII names or a `~` (macOS and Windows fold those), a non-ASCII name right next to `pyproject.toml`
  (HFS+ ignores some invisible code points), names Windows reads as others (a trailing dot or space, `:`, a backslash,
  a short name like `PYPROJ~1.TOM`), repeated names, a name differing only in case. `.zip` sdists, releases with more
  than one sdist, and sdists without a `[build-system]` table get no answer either.
- At most 30 seconds go to it in all (name lookups in DNS aside, which only the resolver's own timeouts bound), with
  one 10-second attempt per download, and the first connection failure stops the rest; a server or proxy that answers
  a byte at a time is cut off at the deadline. Either way, a note on stderr says how many packages weren't read (not
  with `-q`). Otherwise a failure prints nothing, and build info never changes the exit code. Answers are cached for
  24 hours with the other lookups, but only from a download that matched its sha256.

### conda-forge hint {#conda-forge}

!!! note "After 0.9.0"
    `--conda-forge` is not in 0.9.0.

`--conda-forge` adds a hint to `--explain` (it needs `--explain`): for a package with a gap on a glibc Linux, macOS
or Windows target, it asks [anaconda.org](https://anaconda.org/conda-forge) whether conda-forge has a package
with exactly the same (canonical) name and a build of the same version that fits that target. For example,
`wheelgap numpy==2.3.4 -p 3.14t -t linux-ppc64le --explain --conda-forge` adds:

```text
numpy: conda-forge has a package named numpy with 2.3.4 builds for linux-ppc64le
```

A target whose only fitting builds are `noarch` is marked `(noarch)` in that line. A build fits when all of these
hold:

- It's of the checked version, on conda-forge's `main` label and not marked broken.
- It's for the target's conda subdir (`linux-64`, `linux-aarch64`, `linux-ppc64le`, `linux-riscv64`, `osx-64`,
  `osx-arm64`, `win-64`, `win-arm64`) or `noarch`.
- Its `__glibc` and `__osx` minimums hold for the target's glibc and macOS versions (`--glibc`, `--macos`);
  conda-forge's riscv64 builds need glibc 2.39, for example. A build that depends on `__win`, `__unix`, `__linux`,
  `__osx` or `__glibc` fits only those OSes. A build that needs another virtual package (such as `__cuda`), or a
  Python that isn't CPython (such as PyPy), never counts.
- It's for the target's Python:
    - a compiled build by its `python_abi` dependency: the same version and ABI, free-threaded or not;
    - an abi3 build (`_python_abi3_support`) for any non-free-threaded CPython its `cpython` constraint admits;
    - a `noarch` build by its `python` constraints, free-threaded Python included;
    - an older compiled build with no `python_abi` by its `python` constraint, and never for free-threaded Python.

    A build whose Python can't be told doesn't count.

musl, Pyodide, iOS and Android targets never match, and neither do `linux-i686`, `linux-armv7l`, `linux-s390x` and
`win32`, where conda-forge builds no current Python. With `--json --explain`, the package gets a `conda_forge` object:
`"conda_forge": {"name": "numpy", "version": "2.3.4", "builds": {"linux-ppc64le": ["linux-ppc64le"]}}`, keyed by target
label like `rejected`, with the subdirs of the builds that fit.

It's only a hint, so it never changes a status, the summary or the exit code: a conda-forge build doesn't make
`pip install` work, and conda names don't always match PyPI's (PyPI's `torch` is `pytorch` there).

- **Which names are sent.** Without the flag nothing is looked up. With it, only names that pass
  [the PyPI-origin rule](how-it-works.md#pypi-origin) are sent to anaconda.org; a note on stderr names the others. A
  run with `--index-url`, `index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag.
- **Time and size.** Each request is tried once (with a 15-second socket timeout), at most three at a time, all of
  them together get 30 seconds, and the first that can't reach anaconda.org stops the rest. Answers over 16 MB
  (pyarrow's is about 60 MB) are refused, so at most about 250 MB is in use. When a lookup fails or is dropped, a note
  on stderr says no hint isn't a "no".
- **Cache.** Answers, including "too large" and "no such package", are cached for 24 hours like pinned lookups, cut
  down to the subdir, Python, ABI and virtual-package needs of each build of the checked version; `--no-cache` skips
  the cache and `--online` asks again. Failures aren't cached.

### Newer releases with wheels {#suggest}

!!! note "After 0.9.0"
    `--suggest` is not in 0.9.0.

`--suggest` adds, after a package's `--explain` blocks (it needs `--explain`), the lowest newer release with a wheel
for each of its gap targets. For example, `wheelgap fastlib==2.0.0 -t musl-aarch64,win-amd64,linux-riscv64 --explain
--suggest` could add:

```text
fastlib 2.0.0 -> 2.1.0 has wheels for musl-aarch64, win-amd64
fastlib 2.0.0: no newer release has a wheel for linux-riscv64
```

- **Which release.** Only a release that keeps a wheel on every target where the checked version has one counts, so a
  suggestion never trades one gap for another. Gap targets that no such release has a wheel for get a "no newer
  release" line, ending `without losing one elsewhere` when some newer release has a wheel for it but drops another.
  The other gap targets get the lowest counted release with a wheel for every one of them, when there is one;
  otherwise each its own nearest, one line per release. A wheel counts when it isn't yanked, the target accepts its
  tags, and its `Requires-Python` admits the target's Python. Pre-releases count only when the checked version is one,
  and versions that don't parse are skipped.
- **Outside your specifier.** The suggestion looks at every newer release, not only those your input allows: a range
  or a lock entry (`foo<2` resolving to 1.9) may be told to move to 2.1, so check it against your constraints.
- **Which packages.** Pinned packages with a gap, a version range's pick included, but not those a requirements
  file's `--no-binary` covers (pip wouldn't use a newer release's wheels either). Each name is looked up once, on
  PyPI's JSON API (`https://pypi.org/pypi/<name>/json`), and only names that pass
  [the PyPI-origin rule](how-it-works.md#pypi-origin); a note on stderr names the others. A run with `--index-url`,
  `index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag.
- **Time and size.** Each request is tried once (with a 10-second socket timeout), at most four at a time, all of them
  together get 30 seconds, and the first that can't reach the index stops the rest. Answers over 32 MB are refused.
  When a lookup fails or is dropped, a note on stderr says no suggestion isn't a "no". Answers aren't cached.

With `--json --explain --suggest`, each package with a gap that was looked up gets a `suggest` object keyed by target
label like `rejected`: `"suggest": {"musl-aarch64": "2.1.0", "win-amd64": "2.1.0"}`. A target with no counted release is
left out (so the object can be empty). The key is left out for a package with no gap, a row with no version, a package a
requirements file's `--no-binary` covers, a package that wasn't looked up (its index isn't known to be PyPI, or the run
uses `--index-url` or `WHEELGAP_INDEX_URL`), and a failed or dropped lookup (a note on stderr says so). Like the other
hints, it never changes a status, the summary or the exit code. Quiet text output can't show it (quiet can't be combined
with `--explain`), and `--markdown --suggest` is an error for now.

## --json {#json}

`--json` prints one object. Its shape is described by
[`wheelgap-output.schema.json`](https://github.com/wheelgap/wheelgap/blob/main/docs/wheelgap-output.schema.json)
(JSON Schema 2020-12), which matches the wheelgap version it ships with; later versions may add keys, so ignore any
you don't know. Keys are never renamed or removed.

`results`, `rejected` and `via` are all keyed by target label: just the platform (`"linux-x86_64"`) when one Python is
checked, or Python and platform (`"3.13 linux-x86_64"`) with several. The top-level `targets` list gives each target's
`label`, `python` and `platform`, so a script can look the key up instead of building it:

```python
import json

data = json.load(open("wheelgap.json"))
key = next(t["label"] for t in data["targets"] if t["python"] == "3.13" and t["platform"] == "linux-x86_64")
gaps = [p["name"] for p in data["packages"] if p["results"].get(key) in ("source", "missing")]
```

With `--json --explain`, each package gets `rejected` (`{"file", "reason"}` objects) and `via` (the dependency path),
both keyed by target label like `results`; `via` is `{}` for inputs with no dependency graph.

If an input can't be read (a missing lockfile, an unknown target, a bad `[tool.wheelgap]` value), `--json` prints an
error object on stdout instead of results, next to the usual `wheelgap: error:` line on stderr and exit code 2:

```json
{"error": {"message": "nonexist.lock: no such file", "type": "SourceError"}}
```

Command-line usage errors from argparse are still plain text on stderr.

### Every key

This table is generated from the schema when the site is built.

<!-- wheelgap-output-schema -->

## --markdown {#markdown}

`--markdown` prints the same report as Markdown, for a job summary (`wheelgap --markdown >> "$GITHUB_STEP_SUMMARY"`)
or a pull request comment. The GitHub Action writes it to the run's summary page. With `--explain`, the rejected
wheels go in a collapsed section.

```markdown
### wheelgap: 3 of 5 packages have gaps

wheelgap 0.9.0 · `uv.lock` · 5 packages · Python 3.13

| package | version | linux-aarch64 | musl-aarch64 |
| --- | --- | --- | --- |
| fastlib | 2.0.0 | ● source | ● source |
| nativeext | 0.3 | ● source | ● source |
| puresdist | 1.0 | ● source | ● source |

2 packages with wheels for every target hidden (`--all` to show).

- linux-aarch64: 3 from source
- musl-aarch64: 3 from source

3 of 5 packages have gaps: 3 will build from source.

Skipped 2 non-index packages: `demo` (editable), `gitdep` (git)
```

`--json` and `--markdown` can't be used together.

## In GitHub Actions logs

!!! note "After 0.9.0"
    This guard is not in 0.9.0.

The Actions runner treats a line that starts with `::`, or contains `##[`, as a workflow command, and a lockfile in a
pull request could put such text in a package name or version. So everything `wheelgap` and `wheelgap diff` print goes
through a guard that writes a line's leading `::` as `\u003a:` and `##[` as `\u0023#[`. In `--json` output those are
JSON escapes, so the decoded values are unchanged; in text and Markdown the escape shows. Normal output never contains
either, so it's never changed, and neither are the `::error`/`::warning` lines `--github-annotations` writes.

Output to a pipe or a file is always UTF-8, even with `PYTHONIOENCODING` set, because the runner reads the log as
UTF-8. Package names in `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock` and the PyPI packages of
`pixi.lock` must be valid PEP 508 names: anything else is an input error (exit 2) naming the file.

## JUnit XML {#junit-xml}

`--junit PATH` also writes a JUnit XML file that GitLab, Jenkins, Azure Pipelines and CircleCI can show as a test
report, alongside whatever output mode you use. It has one testsuite per target and one testcase per package:

```xml
<testsuites>
  <testsuite name="linux-aarch64" tests="5" failures="3" skipped="0">
    <testcase name="devtool" classname="linux-aarch64" />
    <testcase name="fastlib" classname="linux-aarch64"><failure message="source">source</failure></testcase>
    ...
  </testsuite>
</testsuites>
```

- A cell that fails the run is a failure: `missing`, `unknown`, and `source` unless `--fail-on missing`.
- `n/a` cells are skipped, and so are gaps that `--ignore NAME:TARGET` accepts (message `ignored`).
- After 0.9.0, a gap on an `--advisory` target is skipped with the message `advisory: source` (or `missing`,
  `unknown`), so dashboards that mark a build unstable on failed tests don't.
- Other cells (`wheel`, `bundled`, and `source` with `--fail-on missing`) pass.
- A package locked at several versions gets the version in its testcase name; one that several inputs lock at the
  same version is one testcase, which fails if any of them fails.
