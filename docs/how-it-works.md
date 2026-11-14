# How it works

For each package and each target, wheelgap takes the list of files published for the package's version and asks the
same question pip and uv ask: is there a wheel whose tags this target accepts? It parses each wheel filename's tags
with [`packaging`](https://packaging.pypa.io/) and checks them against the exact tag set the target accepts, the
same logic pip and uv use to pick a wheel. If there's no such wheel, an sdist means a source build (`source`), and no
sdist means the install fails (`missing`). Where wheelgap can't be sure, it says `unknown` rather than guess.

## Where the file lists come from

- **Lockfiles are read offline.** `uv.lock`, `pylock.toml`, `poetry.lock` and `pdm.lock` already record every file
  published for each pinned version (except a `uv.lock` or `pylock.toml` whose `requires-python` leaves out the Python
  you check; see [below](#index-lookups)).
- **`requirements.txt`, `requirements.lock`, `Pipfile.lock`, `pyproject.toml`, package names and (after 0.9.0)
  `pixi.lock`** are looked up on the PyPI JSON API. wheelgap checks the requirements you give it and doesn't resolve
  dependencies, so feed it a fully pinned file. See [Inputs](inputs.md).

## Version ranges

Version ranges such as `numpy>=1.26,<2` use the newest matching release with a non-yanked file whose
`Requires-Python` permits the selected Python. With several `-p` versions, each Python major/minor selects its own
release from one index response. Regular and free-threaded builds of the same Python share release selection, but
each still checks its own wheel tags. Missing or invalid `Requires-Python` metadata is treated as unrestricted.
Exact pins and bare package names keep their existing behavior. This applies to both the PyPI JSON API and
`--index-url` simple indexes.

Distinct resolutions appear as separate rows in text, Markdown and JSON, each with its selected version and only
that Python's eligible files. Targets using another resolution show `–` (`n/a` in JSON). Identical resolutions share
a row; the same version can appear twice if its eligible files differ by Python. A Python with no matching release
gets `unknown` with its own error, while other Pythons still show their results. The Python `check_lockfile()` API
uses the same representation with separate `Result` objects.

## Environment markers

Environment markers are respected. A `pywin32 ; sys_platform == 'win32'` dependency won't show up as a Linux gap; it
shows as `–` (`n/a`) there. For `uv.lock` the dependency graph is walked per target, so transitive platform-specific
packages are handled too. A marker wheelgap can't evaluate for a target is treated conservatively: the package is
still checked, so a possible gap is reported rather than hidden. See
[Targets](targets.md#environment-markers-on-unusual-platforms) for `platform_machine` on 32-bit targets.

## Index lookups

- `--online` re-checks lockfile packages against PyPI. Packages the lock says came from another index keep their
  locked files, so their names are never sent to PyPI. Use it when a project has uploaded new wheels for an existing
  version since you locked, or if your `uv.lock` was generated with `environments` restrictions.
- A `uv.lock` or `pylock.toml` whose `requires-python` leaves out a target's Python (say `==3.13.*`, checked with
  `-p 3.14t`) may be missing wheels for it, because uv only keeps the wheels that can serve `requires-python`. Without
  `--online`, wheelgap then also looks up its PyPI packages on the index, keeps the lock's own files if a lookup fails,
  and prints a note (with `-q`, only when some packages couldn't be re-checked).
- `--offline` never contacts an index, for CI and coding agents without network. Inputs that need a lookup make it
  exit 2 with an error naming the packages. The `requires-python` re-check above is skipped, with a note that those
  Pythons may show false gaps. `offline = true` in `[tool.wheelgap]` makes it the project default.
- Lookups retry on temporary failures and follow only https redirects. A package whose lookup still fails is
  `unknown`, with the reason in its `error`, and counts as a gap.

## Cache

Lookups of a pinned version (`name==1.2.3`) are saved for 24 hours, so a repeat run needn't ask PyPI again. They're
kept in `wheelgap/pinned-v1` inside `~/.cache` on Linux (or `$XDG_CACHE_HOME`), `~/Library/Caches` on macOS and
`%LOCALAPPDATA%` on Windows. Each entry holds only the release's file names and yanked flags, named by a hash of the
index URL (without any credentials in it), the package and the version; on Linux and macOS, wheelgap creates the
folder and its entries readable by you only.

`--online` always asks the index again and saves the fresh answer, so later runs use it; `--no-cache` neither reads nor
saves the cache for one run. Entries older than 24 hours are deleted, at most once a day, and `wheelgap --clear-cache`
deletes all of them (add inputs to check them right after). Both touch only wheelgap's own entry files, and never follow
a link. Unpinned names, version ranges and `--index-url` lookups aren't cached, and neither are lookups from the
Python API.

After 0.9.0, the same folder also keeps, for 24 hours each:

- `--conda-forge` answers, cut down to the subdir, Python, ABI and virtual-package needs of each build of the checked
  version;
- `--notes` lookups, a package the tracker doesn't list included;
- `--build-info` answers (the sdist's `[build-system]` table), but only from a download that matched its sha256.

They follow `--no-cache` and `--online` like the other lookups.

## Private indexes

`--index-url URL` (or `index-url` in `[tool.wheelgap]`) sends the PyPI lookups described above to a simple index that
serves the JSON form (PEP 691) instead. The URL must be https (plain http only for localhost). Credentials can go in
the URL passed to `--index-url` (`https://user:token@host/simple`) and never appear in output; in CI,
`--index-url "$PRIVATE_INDEX"` keeps them out of your repo. They're refused in `[tool.wheelgap]`, since
`pyproject.toml` is committed. An index that only serves HTML (PEP 503) gets a clear error.

Packages a lockfile pins to another index are never looked up on PyPI by name: a namesake on PyPI would be another
package. They keep their locked files, or are skipped as `private index` when the lock has none. `WHEELGAP_INDEX_URL`
points the JSON API lookups at a mirror; see [Pipfile.lock](inputs.md#pipfilelock).

## Which package names leave the run {#pypi-origin}

!!! note "After 0.9.0"
    `--conda-forge`, `--notes` and `--build-info` are not in 0.9.0.

Three opt-in flags send the names of packages with gaps somewhere other than the index: `--conda-forge` to
anaconda.org, `--notes` to wheelgap.github.io, and `--build-info` to PyPI's file host, for the sdist. A name from a
private index must never leave the run, so all three send a package's name only when wheelgap knows the package is
PyPI's. That's one rule, shared by the three, and it holds when all of these do:

- **Lookups go to pypi.org:** no `--index-url` (or `index-url` in `[tool.wheelgap]`) and no `WHEELGAP_INDEX_URL`
  pointing at another index. With either, none of the three sends anything, and a note on stderr says so.
- **The package comes from an index:** not a Git, path, URL, editable or conda package, and not one already skipped
  as `private index`.
- **No other input of the run says otherwise:** when a run has several inputs and any of them places a name
  somewhere other than PyPI, or can't place it (a `pdm.lock` entry, say, or a `poetry.lock` package that `--no-dev` left
  out), that name isn't sent for the other inputs either (nor for another entry of the same input, such as a
  package a `uv.lock` forks between pypi.org and another index). An `acme-secret`
  on a private index in `a/uv.lock` keeps the `acme-secret==1.0` in `b/requirements.txt` in, too.
- **And either** its lock records its index as exactly `https://pypi.org/simple` (a `uv.lock` registry or a
  `pylock.toml` index; a `poetry.lock` legacy source or a `pixi.lock` index with that exact URL counts too, except for
  `--notes`), **or** this run looked it up and found it on pypi.org, and nothing points pip or uv at another index:
    - **The input names no other index.** That's package names, a requirements file with no `-i`/`--index-url`,
      `--extra-index-url`, `-f`/`--find-links` or `--no-index` option (in any form pip reads, in it or in the files it
      pulls in with `-r` and `-c`), a `pyproject.toml` with no index or sources in `[tool.uv]`, `[tool.uv.pip]`,
      `[tool.poetry]`, `[tool.pdm]` or `[tool.pixi.pypi-options]`, or a `Pipfile.lock` whose sources are all PyPI. A
      `-c` file that isn't read (a URL or `${VAR}`) or an abbreviated `--requirement`/`--constraint` option counts as
      naming one.
    - **No `uv.toml`, `pyproject.toml` or `pdm.toml` sets an index** in the input's folder or a folder above it, up to
      the repository root (the first with a `.git`). One that can't be read counts as setting one.
    - **No environment variable sets one:** `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_FIND_LINKS`,
      `PIP_NO_INDEX`, `PIP_CONFIG_FILE` (any case after `PIP_`), `UV_INDEX_URL`, `UV_DEFAULT_INDEX`, `UV_INDEX`,
      `UV_EXTRA_INDEX_URL`, `UV_FIND_LINKS`, `UV_NO_INDEX`, `UV_CONFIG_FILE` or any `UV_INDEX_*`.

So `pdm.lock` entries, `poetry.lock` and `pixi.lock` entries that record no index, `pylock.toml` entries without an
`index`, and packages from another index are never sent. User and system settings aren't read: a `pip.conf` or
`pip.ini`, `~/.config/uv/uv.toml`, `/etc/uv/uv.toml` and `.netrc`. If those point pip or uv at a private index, don't
use these flags (or set one of the variables above).

Each flag then adds its own limits:

- **`--conda-forge`** follows the rule as it is, and a note on stderr names the packages it didn't look up.
- **`--notes`** takes the recorded-index route only from `uv.lock` and `pylock.toml`, and is off with `--offline`
  (or `offline = true`).
- **`--build-info`** is off for the whole run when an environment variable above is set, as well as with
  `--offline`. When the lock records an sdist URL (`uv.lock`, `pylock.toml`), it downloads from that URL only if it's
  on `files.pythonhosted.org` and names that release's sdist, checked against the lock's sha256; otherwise there's no
  answer. When the lock records no URL, it asks PyPI's JSON API for it.

## Scanning the top PyPI packages

```bash
python -m wheelgap.scan --top 5000 --out coverage.json
```

This checks the latest release of the most-downloaded packages (from
[top-pypi-packages](https://hugovk.dev/top-pypi-packages/)) against the eight common platforms (and Pyodide), for
Python 3.13, 3.14, 3.14t, 3.15 and 3.15t by default, and writes one JSON file with per-platform totals. It's the data
behind the [coverage tracker](https://wheelgap.github.io/tracker/).

Add `--pyodide-lock PATH_OR_URL` once per Pyodide release (each is for one Python) to count the packages Pyodide ships
itself as `bundled` on that Python's `pyodide` target; the per-target `summary` counts them too, and `pyodide_locks`
in the JSON lists the locks used. A package with a `bundled` cell also gets `bundled_versions` (the version each of
those locks ships, by Python), `bundled_version` (that version when they all agree, otherwise `null`) and
`bundled_version_differs` (whether any differs from the release the scan checked). Pyodide targets with no lock get a
note on stderr.
