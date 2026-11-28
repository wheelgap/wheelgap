# Output

wheelgap prints a table by default, or JSON (`--json`) or Markdown (`--markdown`) instead, and can also write a JUnit
XML file (`--junit PATH`), a SARIF file (`--sarif PATH`) or a GitLab Code Quality report
(`--gitlab-code-quality PATH`). Notes and errors go to stderr, so the stdout of `--json`
and `--markdown` holds only the report.

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
- Gaps on `--advisory` targets never change the exit code.

Add `set -o pipefail` when you pipe the output, so input errors still fail.

## Text

<!-- sample: uvproj -p 3.13 -t linux-aarch64,musl-aarch64 -->
```text
wheelgap 0.14.0 · uv.lock · 5 packages · Python 3.13

package                  version  linux-aarch64  musl-aarch64
fastlib                  2.0.0    ● source       ● source
nativeext (via fastlib)  0.3      ● source       ● source
puresdist                1.0      ● source       ● source

2 packages with wheels for every target hidden (--all to show)
skipped 2 non-index packages: demo (editable), gitdep (git)
linux-aarch64  3 from source
musl-aarch64   3 from source
3 of 5 packages have gaps: 3 will build from source.
Run with --explain to see which wheels were rejected and why.
Run with --fix for a way out: an upgrade where a newer release has the wheel, else wheelgap's gap wheel index.
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

A run without [`--fix`](cli.md#fix) ends with a line about it when a gap could use a gap wheel; printing
it never makes a request. With no gap wheel list in the [cache](how-it-works.md#cache), or with `--no-cache` or
`--online` (which don't read the cache), it is
`Run with --fix for a way out: an upgrade where a newer release has the wheel, else wheelgap's gap wheel index.` When an earlier `--fix` run left the
list in the cache (kept for an hour), it is
`Run with --fix for a way out: an upgrade where a newer release has the wheel, else wheelgap's gap wheel index, which has a wheel for N of these gaps.`, where N counts the gap cells of the table
that the cached list fills, and when that list fills none of them there is no line. No line isn't a statement that
nothing fits: a wheel published since the list was cached isn't in it, and `--fix` itself reads the list again before it
says so. `--offline`, `--index-url` and a `WHEELGAP_INDEX_URL` mirror print neither.

Packages that are fine everywhere are hidden; `--all` shows them. A range that picks a different release for
different `-p` versions is one row per release, and the summary still counts the package once. With several targets, the summary also counts the
gaps on each target that has any (`musl-aarch64  1 from source, 1 missing`). Packages that weren't checked, such as
editable, path or Git dependencies, are listed on the `skipped` line.

A package that's only there because something else needs it names the direct dependency that pulls it in, from
`uv.lock`, Poetry 1.x lockfiles or [`--deps`](cli.md#deps): `cryptography (via mcp)`, or `cryptography (via mcp +2)`
when three of your direct dependencies lead to it. It names a dependency only when the lockfile shows that one really
leads there on that target, so a package reached through another package's optional extra may have no hint.
`--explain` gives the full path (`via: mcp → PyJWT → cryptography`); `--json` and quiet output keep the bare name.

With [`--dockerfile-inputs`](cli.md#dockerfile-inputs), the first line names the Dockerfile line that installs each
file: `requirements.txt (Dockerfile:4)`, and with several files each gets its own,
`requirements.txt (Dockerfile:4) + second.txt (Dockerfile:5)`. The line is the one the `RUN` starts on, as Docker
numbers it, when the instruction continues over several lines. `--markdown` has the same header, and a
[`--github-annotations`](cli.md#github-annotations) message, still placed on the requirements file's line, ends
`(installed by Dockerfile:4)`.

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

For a `uv.lock` checked with `--online`, `--explain` also names each package that uv installs and pip
would refuse (uv checks the `Requires-Python` lower bound; see [uv.lock](inputs.md#uvlock)), whether or not it has a
gap:

```text
agent-client-protocol 0.11.1 on linux-x86_64: checked as uv installs it (uv checks the Requires-Python lower bound); pip refuses it: requires Python <3.15,>=3.10
```

When the package comes from PyPI and publishes an issue tracker in its metadata (or a GitHub repository fallback),
each gap line also links to it so you can ask the maintainers for a wheel. Its `project_urls` labels are read the way
PyPI reads its well-known labels ([PEP 753](https://peps.python.org/pep-0753/)), so `Issues`, `Issue-Tracker`,
`bug_tracker` and `GitHub: issues` all count:

```text
armlib 1.4.0 on musl-aarch64: ● source
  armlib-1.4.0-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl  manylinux_2_17_aarch64 is a glibc wheel; musl (Alpine) needs musllinux
  ask for a wheel: https://github.com/example/armlib/issues
```

With [`--fix`](#fix) too, a package a wheel on wheelgap's gap wheel index fills gets a `gap wheel` line under its first
filled target.

### Tracker notes {#notes}

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

### Build lab results {#build-lab}

The tracker's weekly [build lab](https://wheelgap.github.io/tracker/) builds popular packages that
have no wheel for a platform from their PyPI sdist on that platform, on GitHub's runners, then installs and imports the
result, and on free-threaded Python checks whether importing it turned the GIL back on. With `--explain --notes`, a
`source` cell gets one line after the package's notes when the lab built **exactly the checked version** for
**exactly that target**:

```text
pkg 1.0 on win-arm64: build lab (2026-11-15, windows-11-arm, Python 3.14.2): builds from source in 87 s and imports
```

- The verdict is one of: `builds from source in 87 s and imports` (with `, GIL stays off` on free-threaded Python),
  `builds in 87 s, but importing it turns the GIL back on`, `builds in 87 s as pure Python, with no compiled
  extension module (if it has an optional one, that was skipped)`, `builds in 87 s, but with no compiled extension module`, `builds in 87 s, but doesn't import`,
  `builds in 87 s, but its dependencies don't install`, `fails to build: needs a Rust toolchain` (or another
  reason) and `didn't finish building in 40 min`. A wheel with no extension module is never called a working build.
- These are the lab's dated observations on GitHub's runners, with the runner and Python they used, not a promise
  about your machine. The runners have compilers and many system libraries preinstalled, so a build that works there
  can still need setup on your machine: a package that needs a C library's headers, such as libpq's, may find them
  already there. Nothing the lab built is downloaded: the results come in the same small file as the notes, so they
  add no request, and follow the same rules (opt-in, names known to be PyPI's only, the cache, the timeouts).
- A result for another release, or for another target (the free-threaded build says nothing about the regular one),
  shows nothing. The tracker builds its latest release, which may not be yours.
- With `--json --explain`, each package gets a `build_lab` object, keyed by target label like `rejected`, with the
  lab's fields (`target`, `version`, `outcome`, `seconds`, `wheel`, `extensions`, `import`, `gil`, `reason`,
  `excerpt`, `toolchains`, `runner`, `python`, `date`) and the `verdict` above; it's empty without `--notes` or when
  there's no result. `--markdown` puts the lines with the notes.
- The results come from a public repository too, so wheelgap reads them as strictly as the tracker's own checker and
  drops any result that isn't exactly in its format, without hiding the notes (or the other way round). Treat
  `excerpt` as untrusted plain text. The exit status never depends on them.

### Build requirements {#build-info}

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
- With `--fix`, a gap with no fix on a Linux target that builds from source also says which distro packages provide
  those needs (only a Rust toolchain, CMake and a C/C++ compiler are mapped; `--explain` isn't needed for this):

  ```text
  No fix yet:
    numpy 1.26.4: no newer release has a wheel for musl-armv7l; ask for a wheel: https://github.com/numpy/numpy/issues
      to build it from source on musl-armv7l, it needs at least a C/C++ compiler: apk add --no-cache build-base on Alpine (and python3-dev with the distro's own Python); these are build tools only: libraries the package links against (e.g. libxml2 and libxslt for lxml) aren't named
  ```

  `musl-*` targets get Alpine's `apk add --no-cache` (`build-base`, `cmake`, `cargo`) and `linux-*` targets Debian's
  and Ubuntu's `apt-get update && apt-get install -y` (`build-essential`, `cmake`, `cargo`), so the command runs as
  it stands in a fresh image such as `python:3.13-slim`, which has no apt package lists. The `python:*` images
  already have the Python headers; the distro's own Python needs `python3-dev`, which is named for a Rust build too,
  since one can compile C through cffi as well (cryptography does). A distro's Rust can be older than a crate needs,
  so a Rust build also names [rustup](https://rustup.rs). It's "at least": these are build tools only, and a library
  the build links to (libxml2 and libxslt for lxml, BLAS, OpenSSL, libffi) isn't in `[build-system]` and isn't
  named. Nor is the distro's own build of the package (`apk add py3-numpy`) suggested, since it installs for the
  distro's Python only. Fedora and RHEL (`dnf`) aren't covered yet. `--json --fix` gives the same as
  `build_packages` in the cell's [`fix`](#fix).
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

### piwheels hint {#piwheels}

`--piwheels` adds a hint to `--explain` (it needs `--explain`): for a package with a gap on `linux-armv7l` (32-bit
Raspberry Pi OS), it asks [piwheels.org](https://www.piwheels.org/) whether piwheels has a wheel of the same version
whose tags the target accepts. piwheels has no aarch64 wheels, so no other target is looked up. For example, PyPI has
no armv7l wheel of numpy 2.5.4, and `wheelgap numpy==2.5.4 -p 3.13 -t linux-armv7l --explain --piwheels` adds:

```text
piwheels has numpy 2.5.4 for linux-armv7l: --extra-index-url https://www.piwheels.org/simple
```

A wheel fits when all of these hold:
- It's of the checked version, in a release that isn't yanked.
- Its filename is a valid wheel filename of this package and version.
- Its wheel tags are accepted by the target (`target.accepts()`).

With `--json --explain`, the package gets a `piwheels` object, with the apt packages piwheels lists for those wheels,
each once:
`"piwheels": {"name": "numpy", "version": "2.5.4", "wheels": {"linux-armv7l": ["numpy-2.5.4-cp313-cp313-linux_armv7l.whl"]}, "apt": ["libc6", "libgcc-s1", "libgfortran5", "libopenblas0-pthread", "libstdc++6"]}`.

It's only a hint, so it never changes a status, the summary or the exit code.

- **Not from PyPI.** piwheels builds its wheels on Raspberry Pi OS from the PyPI sdist; they aren't from the
  package's maintainers. A wheel that links to system libraries needs the apt packages piwheels lists for it
  (`sudo apt install ...`; the `apt` list in `--json`, or the package's page on piwheels.org), and may not work on
  another armv7l distribution.
- **With a uv lockfile.** `--extra-index-url` is for pip. For `uv.lock`, add piwheels as an explicit index and send
  only the package, and only on armv7l, to it (as with the gap wheel index, `[tool.uv.sources]` applies to direct
  dependencies only, so list the package in `dependencies` too):

  ```toml
  [[tool.uv.index]]
  name = "piwheels"
  url = "https://www.piwheels.org/simple"
  explicit = true

  [tool.uv.sources]
  numpy = { index = "piwheels", marker = "platform_machine == 'armv7l'" }
  ```

- **Which names are sent.** Without the flag nothing is looked up. With it, only names that pass
  [the PyPI-origin rule](how-it-works.md#pypi-origin) are sent to piwheels.org; a note on stderr names the others. A
  run with `--index-url`, `index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag.
- **Time and size.** Each request is tried once (with a 15-second socket timeout), at most three at a time, all of
  them together get 30 seconds, and the first that can't reach piwheels.org stops the rest. Answers over 4 MB
  are refused. When a lookup fails or is dropped, a note on stderr says no hint isn't a "no".
- **Cache.** Answers, including "no such package", are cached for 24 hours; `--no-cache` skips the cache and
  `--online` asks again. Failures aren't cached.

### Newer releases with wheels {#suggest}

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
  When no newer release has a wheel for it at all, the line names the newest older release that did, if one of the 50
  releases before the checked version has one (counted the same way): `cryptography 50.0.2: no newer release has a
  wheel for win-arm64; the last one that did is 46.0.3`. Pinning it avoids the build on that target; check that
  it still has wheels for your other targets.
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
left out (so the object can be empty). Next to it, `last_with_wheel` names, for each gap target no newer release has a
wheel for, the newest older release that has one: `"last_with_wheel": {"win-arm64": "46.0.3"}`. Both keys are left out
for a package with no gap, a row with no version, a package a requirements file's `--no-binary` covers, a package that
wasn't looked up (its index isn't known to be PyPI, or the run uses `--index-url` or `WHEELGAP_INDEX_URL`), and a failed
or dropped lookup (a note on stderr says so). Like the other hints, it never changes a status, the summary or the exit
code. Quiet text output can't show it (quiet can't be combined with `--explain`). With `--markdown --explain --suggest`,
suggestions render as a list under the collapsed explanation details.

## Fixes and gap wheels (--fix) {#fix}

`--fix` starts its section with a summary of the gap cells (a package on a target) and their way out:
an upgrade to an official release first, an unofficial gap wheel second, else none. Only upgrades count as fixed. For a
requirements file with `pyyaml==6.0.3`, `markupsafe==3.0.2` and `zstandard==0.25.0`, on `-p 3.15 -t
linux-x86_64,win-amd64`:

```text
--fix: 6 gaps (3 packages × their targets): 4 can be fixed by upgrading to an official release; 2 only by an unofficial gap wheel; 0 have no fix yet.

Upgrade: a newer official release has the wheel:
  markupsafe 3.0.2 -> 3.0.4: linux-x86_64, win-amd64
  pyyaml 6.0.3 -> 6.0.4: linux-x86_64, win-amd64
In requirements.txt, change the pins to markupsafe==3.0.4, pyyaml==6.0.4.
wheelgap doesn't resolve dependencies: the rest of them must allow these releases.

As a fallback, wheelgap's gap wheel index has unofficial wheels PyPI lacks (built by wheelgap from PyPI's sdist, not by the packages' maintainers) for:
  pyyaml 6.0.3: linux-x86_64 (if you can't upgrade pyyaml)
  zstandard 0.25.0: linux-x86_64, win-amd64
```

followed by how to install the gap wheels, as below. Each upgrade line names the release and the targets it has wheels
for, then a line says what to run or change for the input ([`--fix`](cli.md#fix) lists them per input); where wheelgap
reads your own requirement for the package, the line ends with `(within your pyproject.toml constraint pyyaml>=6.0)` or
`(needs requirements.txt's pyyaml>=6,<6.0.4 relaxed)`, as the file writes it (or `needs pyyaml==6.0.3 in the -c
constraints of requirements.txt relaxed`). A package whose targets need different releases gets no pin; a line names
each target's release. In Markdown, the what-to-run line and the note that wheelgap doesn't resolve are paragraphs of
their own. A gap with neither goes under `No fix yet:`, one line per package with the reason per target (for a release
that drops a wheel elsewhere, what the nearest newer one has none for) and, where no newer release has a wheel,
`ask for a wheel: URL` when wheelgap knows the package's issue tracker; a uv.lock's gap that uv may fix by locking a
release per Python goes under `Maybe, with uv:` instead, with `uv lock --upgrade-package NAME` to try (`uv lock
--script SCRIPT --upgrade-package NAME` for a script's lock), unless your own requirement leaves that release out
(`(pyproject.toml's numpy<2.5 leaves out 2.5.2)` on the `No fix yet:` line); on a
free-threaded target whose release has only abi3 wheels, the line says so (`only abi3 wheels for linux-x86_64, which
free-threaded Python can't use: ask NAME for a cp315t wheel at URL`). Packages whose releases weren't looked up get one
line saying why: wheelgap can't tell they come from PyPI (their names stay private), the run uses another index, or the
lookup failed. They're counted apart in the summary (`; 2 weren't checked for an upgrade`), not as "no fix yet". The
gap wheel section opens with `As a fallback, ` only when `--fix` names an upgrade.

The gap wheel part lists one line per package with the targets it fills (as
the table labels them), then how to install from the index. For a uv.lock input that's a snippet for
`pyproject.toml`:

```text
As a fallback, wheelgap's gap wheel index has unofficial wheels PyPI lacks (built by wheelgap from PyPI's sdist, not by the packages' maintainers) for:
  tokenizers 0.23.3: win-arm64

To install them with uv, add this to pyproject.toml, then run uv lock:

[[tool.uv.index]]
name = "wheelgap"
url = "https://wheelgap.github.io/wheels/simple/"
explicit = true

[tool.uv.sources]
tokenizers = { index = "wheelgap" }

tokenizers comes in through transformers; uv only applies [tool.uv.sources] to direct dependencies, so also list "tokenizers" where transformers is (the same dependencies, dependency group or extra).
If pyproject.toml already has a [tool.uv.sources] table, add these lines to it instead of a second one.

How these wheels are built and checked: https://wheelgap.github.io/wheels/
```

A requirements file, a `pyproject.toml`, a pip report or package names get the
`--extra-index-url https://wheelgap.github.io/wheels/simple/` line for a requirements file or `pip install`, for
requirements that pin hashes each package's gap wheel `--hash` options under an `add to the NAME==VERSION line:` label,
to add to that package's existing line (keeping the hashes it has: on a second line for the same package, pip won't
use them), and a reminder that pip treats every index alike. Other lockfiles (`pylock.toml`, `poetry.lock`,
`pdm.lock`, `Pipfile.lock`, `pixi.lock`) pin each file, so they get a line saying to add the index where the lockfile
is made and lock again. A script gets the `[[tool.uv.index]]` and `[tool.uv.sources]`
lines to add inside its `# /// script` block for `uv run`, commented as the block's lines are, with a note for each
package that isn't one of the block's own `dependencies` (uv applies sources only to those). Packages wheelgap can't place on PyPI aren't checked, and a note
on stderr names them. `--markdown` renders the same section with fenced code blocks, and `-q` keeps it. With nothing to fill, a
note on stderr says so instead (not with `-q`).

With `--explain` too, a target a newer official release fixes gets an `upgrade: NAME VERSION has
the wheel (an official release; see the --fix section)` line first, and each package a gap wheel fills gets a
`gap wheel` line under its first filled target in the `--explain` output, naming the targets it fills (starting
`if you can't upgrade, ` when an upgrade fixes one of them). The wheel is unofficial: built by wheelgap from
the PyPI sdist, not from the package's maintainers. For an input pip installs from as it is, the line gives the pip
command; for a lockfile it points to the section above, since the lockfile's own tool has to pick up the index:

```text
nowheels 0.9 on win-arm64: ● source
  no wheels published for this version
  gap wheel for win-arm64: unofficial, built by wheelgap from the PyPI sdist; not from the package's maintainers
    pip install --extra-index-url https://wheelgap.github.io/wheels/simple/ nowheels==0.9
```

The gap wheel list is read once and kept in the [cache](how-it-works.md#cache) for an hour (only a list with at least
one usable entry), with or without `--explain`; `--no-cache` neither reads nor saves it. A cached list that fills none
of the run's gaps is read again before wheelgap says no wheel fits.

With `--json --fix`, the top-level `gap_wheels` list has one object per filled cell: `name`, `version`, `target` (the
target label), `wheel` (its filename), `url` (where to download it) and `sha256`. It's `[]` when nothing fits, when
the run uses another index, or when the list couldn't be read, and absent without `--fix`. Like the other hints, it
never changes a status, the summary or the exit code.

With `--json --fix`, each package also gets `fix`: an object keyed by its gap target labels whose
values have `kind` and that kind's keys:

- `"upgrade"`: `version` (the release), `prerelease` (whether it's a pre-release, named only for a pre-release pin
  when no one final release fits every target), `exclude_newer` (`null`, or `setting`, `cutoff`, `span` and `move_past` when the
  release was uploaded after uv's exclude-newer, so the cutoff must move past `move_past` first; for a duration such
  as `850 days`, `span` is the duration and `cutoff` is `null`, since the time it gives moves every run), `how` (what to run or change), `constraint` (`null`, or `file`,
  `requirements`, `allows` and `via`: your own requirement, or `-c` constraint, and whether the release fits it) and
  `gap_wheel` (the fallback:
  `null`, or `wheel`, `url`, `sha256` and `unofficial: true`);
- `"gap_wheel"`: `wheel`, `url`, `sha256` and `unofficial: true`;
- `"none"`: `reason` (`no_newer_release`, `loses_wheel`, `uv_may_fork`, `not_looked_up`, `no_binary` or, with
  `--deps`, `held_back`: another package in the resolved tree requires a range that rules out the newer release
  with the wheel), `abi3_only` and `build_packages` (`null`, or with [`--build-info`](#build-info) the distro
  packages to build it from source: `distro`, `command`, `packages`, `needs` and `distro_python`).

The top-level `fix_summary` counts them: `{"gaps": 6, "upgrade": 4, "gap_wheel": 2, "none": 0, "not_looked_up": 0,
"uv_may_fork": 0}`, where `none` counts only the gaps with no known fix and `gaps` is the sum of the other five. `gap_wheels` still
lists every cell a gap wheel fills, the fallback ones too.

With `--json --explain --fix`, each package also gets `gap_wheel`: an object keyed by target label (only the targets a
gap wheel fills, so `{}` when none does) whose values have `wheel`, `url` and `sha256`, and `install` (the pip command)
for inputs pip installs from as they are; a lockfile's entries have no `install`.

## --json {#json}

`--json` prints one object. Its shape is described by
[`wheelgap-output.schema.json`](https://github.com/wheelgap/wheelgap/blob/main/docs/wheelgap-output.schema.json)
(JSON Schema 2020-12), which matches the wheelgap version it ships with; later versions may add keys, so ignore any
you don't know. Keys are never renamed or removed.

With [`--dockerfile-inputs`](cli.md#dockerfile-inputs), the top-level `dockerfile_inputs` list has one
entry per requirements file, in the order of `sources`: the `dockerfile` path as it was given, the `line` its `RUN`
starts on (counted from 1; the first `RUN` that installs the file, if several do), and the `file` path relative to
the build context, with `/` on every OS (`reqs/base.txt`), as the Dockerfile's `COPY` names it; `sources` gives its
name only. `constraints` lists the `-c` files of that `RUN` line and `includes` the files `file` includes with `-r` or `-c`, at
any depth, both as `file` is written and empty when there are none; what a constraints file itself
includes is in the note only. The key is absent without the flag.

When an input is a directory, the top-level `directory_inputs` list says which of its files was read,
one entry per directory: the `directory` as it was given, the `file` relative to it with `/` on every OS
(`backend/requirements.txt`), the `reason` (`lockfile`: a lockfile or `requirements.txt` at the root, found by its
name; `pyproject`: the root's `pyproject.toml`; `requirements`: the root's only requirements file under another name;
`subfolder`: the only input below the root, which `sources` then names by the same relative path, as in
`scripts/requirements.txt`, where every other input has its file name only) and `empty`, the files that come first in the search but had nothing to
check, each with its `file` and `why` (`[]` in the usual case), and `other_requirements`: with the reason
`pyproject`, the root's requirements files under other names, which weren't read, each with its `file` and
`compiled`, whether it looks like `pip-compile`, `uv pip compile` or `uv export` output that pins everything (a hint
for which to check next, never used to choose; those come first), and `[]` otherwise. The key is absent when no input was a directory. See
[Which file is read](inputs.md#which-file-is-read).
`from_log_notes` (**after 0.14.0**; with [`--from-log`](cli.md#from-log) only, `[]` in the usual case) lists each
failed package checked at its latest release because the log doesn't name its version, one object each: `name`,
`version` (the release checked), `kind` (`latest`) and the `message` the note on stderr has. `--markdown` adds the same
message as a `> **Note:**` line.
`dockerfile_notes` lists what a `--dockerfile` answer leaves out, one object each: the `dockerfile`
path as given, the `line` it is about (or `null`), a `kind` to match on and the `message` the note on stderr has.
The kinds: `image-not-read` (`-p` and `-t` stood in for an [image wheelgap can't read](cli.md#dockerfile));
`image-libc` (that image's tag says Alpine or musl, and glibc targets were checked);
`passed-over:CLASS`, one per class of `RUN` line [`--dockerfile-inputs`](cli.md#dockerfile-inputs) didn't read
because it installs no Python packages with pip or uv (`system-packages`, `shell`, `programs`, `venv`,
`cache-mounts`, `copy-from`, `installer-tools`), with `line` `null` and the line numbers in `lines`; `arg-unset` (an
`ARG PIP_...` with no default, read as unset); `system-python-packages` (system package commands on `lines`
install Python packages, which pip may keep instead of fetching a requirement); and, after 0.14.0, `kept-version`
(an unpinned requirement of the install on `line` that an earlier install line already put in the image, which pip
keeps if it satisfies the requirement). The key is absent when there is
nothing to say.

`dockerfile_packages` has one entry per `RUN` line whose `pip install` names requirements on the
line itself: `dockerfile`, `line`, the `installer` (`pip` or `uv pip`) and the `requirements` as written, variables
replaced. Two installs on one line that read different `-r` files (`pip install -r a.txt x && pip install y`) get an
entry each, since each file's options count for its own install's names only. Each entry is also a source of its own
in `sources`, labelled with the line (`Dockerfile:5`). `kind` `arg-value` in
`dockerfile_notes` says an install used an `ARG`'s default or a `--build-arg` value.

`dockerfile_warnings` (always there with `--dockerfile-inputs`) lists the `RUN` lines wheelgap
couldn't see into, which may install Python packages the answer doesn't have: `dockerfile`, `line`, `kind`
(`may-install`, or `may-bring` for a `COPY --from` that may carry an environment from another stage) and `message`. An empty list means every line was read or can't install any; a script should treat a
non-empty one as "this answer is not the whole image". Each such line comes after the last install wheelgap read: one
before it is an error instead. With `--dockerfile-inputs`, `summary.complete` says the same as a boolean: `false`
when `dockerfile_warnings` lists a line or `-p` and `-t` stood in for an image wheelgap couldn't read (the headline then says so too), `true` otherwise (absent without the flag). The exit code doesn't
change for a warning.

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
both keyed by target label like `results`; `via` is `{}` for inputs with no dependency graph, and also
covers targets where the package couldn't be looked up. It also
gets `issues_url`: the package's issue tracker URL from its PyPI metadata, or `null`, and `direct`:
for each gap target, and each target where the package couldn't be looked up (`unknown`), the direct
dependencies that lead to the package, as the table's `cryptography (via mcp)` names
them (`{"win-arm64": ["mcp", "httpx"]}`). `direct` has entries only for uv.lock, Poetry 1.x and `--deps` inputs. It
leaves out a target where the package is itself a direct dependency, and may leave one out when the lockfile doesn't
record which direct dependency asked for an extra that leads to the package (Poetry 1.x).

For a `uv.lock` checked with `--online`, a package with files that uv installs and pip would refuse (uv checks only
the `Requires-Python` lower bound) also gets `pip_refuses`, with or without `--explain`: those files, each
mapped to the reason, as in `{"agent_client_protocol-0.11.1-py3-none-any.whl": "requires Python <3.15,>=3.10"}`. Its
`results` are still uv's. See [uv.lock](inputs.md#uvlock).

A package that is `unknown` on a target for a reason other than a failed lookup gets `unknown_reasons`, with or
without `--explain`: each such target mapped to why. For now that's a Poetry 1.x `poetry.lock` checked without a
`pyproject.toml` that declares the project's dependencies, or one whose project requires the package there only under a
marker or Python restriction wheelgap can't decide; see [poetry.lock](inputs.md#poetrylock).

If an input can't be read (a missing lockfile, an unknown target, a bad `[tool.wheelgap]` value), `--json` prints an
error object on stdout instead of results, next to the usual `wheelgap: error:` line on stderr and exit code 2:

```json
{"error": {"message": "nonexist.lock: no such file", "type": "SourceError"}}
```

When a directory has several files to choose from (see
[Which file is read](inputs.md#which-file-is-read)), the error's `type` is `DirectoryChoiceError` and it also has
`candidates`: the paths of all of them, each one an input to pass instead of the directory. The message lists the
first 10 as commands, one per line.

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

wheelgap 0.14.0 · `uv.lock` · 5 packages · Python 3.13

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

With `--dockerfile-inputs`, each line wheelgap couldn't see into ([`dockerfile_warnings`](#json)) is
also a `<property name="wheelgap.dockerfile-warning" value="Dockerfile:8: …"/>` and a `system-out` line in every
testsuite: it fails nothing, but the dashboard shows it.

- A cell that fails the run is a failure: `missing`, `unknown`, and `source` unless `--fail-on missing`.
- A transitive gap's failure or advisory-skip message names the direct dependency that pulls it
  in on that target, as the tables do: `source (via mcp)`, or `source (via mcp +2)`, and the element's text lists
  them all. Only inputs with a dependency graph (uv.lock, Poetry 1.x, `--deps`) have one. Testcase names don't
  change, so dashboard history is kept.
- `n/a` cells are skipped, and so are gaps that `--ignore NAME:TARGET` accepts (message `ignored`).
- A gap on an `--advisory` target is skipped with the message `advisory: source` (or `missing`,
  `unknown`), so dashboards that mark a build unstable on failed tests don't.
- Other cells (`wheel`, `bundled`, and `source` with `--fail-on missing`) pass.
- A package locked at several versions gets the version in its testcase name; one that several inputs lock at the
  same version is one testcase, which fails if any of them fails.

## SARIF {#sarif}

`--sarif PATH` also writes a
[SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) file for GitHub code scanning (or
another SARIF viewer), alongside whatever output mode you use. It has two rules, `wheelgap/source` and
`wheelgap/missing`, and one result per package and target with that status:

- The level is `error` for a gap that fails the run, `warning` for `source` under `--fail-on missing`, and `note` on
  an `--advisory` target.
- Gaps that `--ignore NAME:TARGET` accepts have no result, and neither do `unknown` cells (a lookup that failed isn't
  a finding about the package); the exit code still fails on them.
- The message says what happens on the target, with up to three of the `--explain` lines saying why:
  `newglibc 2.0 can't install on linux-aarch64. newglibc-2.0-cp313-cp313-manylinux_2_39_aarch64.whl
  manylinux_2_39_aarch64 needs glibc 2.39, newer than ...`.
- A transitive gap's message names the direct dependency that pulls it in on that target, as the tables do:
  `cryptography 50.0.2 (via mcp) builds from source on win-arm64.`, or `(via mcp +2)`. Only inputs with a dependency
  graph (uv.lock, Poetry 1.x, `--deps`) have one. Fingerprints don't change, so existing alerts stay open.
- The location is the package's line in its lockfile or requirements file, relative to `GITHUB_WORKSPACE` (else the
  current directory), as for [`--github-annotations`](cli.md#github-annotations), or the file's first line when
  wheelgap can't tell the line (`pyproject.toml`, say). A package named on the command line, or read from a file
  outside the repository, has no location: its gap still fails the job, but code scanning doesn't show it.
- With `--dockerfile`, a note that the final image wasn't read ([`dockerfile_notes`](#json) of kind
  `image-not-read` or `image-libc`) is a `note` result of a third rule, `wheelgap/dockerfile-note`, at the `FROM`
  line (for a heredoc an old `# syntax=` line may read differently, at that instruction's line). The rule is listed only in a run that has such a result. A line `--dockerfile-inputs` couldn't see into
  ([`dockerfile_warnings`](#json)) is a `warning` result of `wheelgap/dockerfile-unseen` at that line.
- `partialFingerprints` key a result by file, package and target, not version, so code scanning keeps one alert open
  across lockfile bumps until the gap goes away. A package that one file locks at several versions gets an alert per
  version.

## GitLab Code Quality {#gitlab-code-quality}

`--gitlab-code-quality PATH` writes a [GitLab Code Quality report](https://docs.gitlab.com/ci/testing/code_quality/#code-quality-report-format)
as a JSON array, alongside the usual output and even when the check fails. Each finding names one package and gap
target, with check name `wheelgap/source` or `wheelgap/missing` and a SHA-256 fingerprint of the file, canonical
package name and target (always including the Python), not the version, so GitLab keeps one finding across lockfile
bumps until the gap goes away. Changing report order or adding another Python doesn't change an existing finding's
fingerprint, and the same package in two files is two findings. Each fingerprint appears once per report: a package
that one file locks at several versions gets one finding, for the first.

Severity is `critical` for `missing`, `major` for `source`, `minor` for a source build `--fail-on missing` accepts,
and `info` on an `--advisory` target. Ignored gaps and `unknown` cells have no finding; failed lookups still fail the
job. Locations use the same package lines as SARIF, relative to `CI_PROJECT_DIR` (else the current directory), with
forward slashes. If the reader knows the file but not the package's line, the finding uses line 1. GitLab requires
a file and line: packages named on the command line and files outside the repository are omitted from this report,
while their gaps still appear in normal output and affect the exit code. No gaps means an empty array (`[]`).
