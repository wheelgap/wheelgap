# Command line

```text
wheelgap [OPTIONS] [INPUT ...]
wheelgap diff [-p VER] [-t PLATFORM] [--glibc X.Y] [--musl X.Y] [--distro NAME] [--macos X.Y] [--json] [--deps] OLD NEW
wheelgap mcp            (or wheelgap-mcp)
```

`INPUT` is a lockfile, a requirements file, a `pyproject.toml`, a script with a `# /// script` block (PEP 723), a
directory, or package specs such as `numpy==2.3.4` (default: the current directory). See [Inputs](inputs.md).

Flags that take a list (`-p`, `-t`, `--extra`, `--group`, `--only-group`, `--ignore`, `--advisory`)
can be repeated; `-p`, `-t`, `--ignore` and `--advisory` also take comma-separated values. Flags win over
[`[tool.wheelgap]`](configuration.md).

`wheelgap --help` lists the flags of the version you run. This page describes `main`; flags marked **after 0.14.0**
aren't in a release yet.

## Targets

### `-p`, `--python` {#python}

`-p VER`: a target Python, such as `3.13`, or `3.14t` for a free-threaded build. Repeatable and comma-separated:
`-p 3.13,3.14t`. Default: the image's Python with `--dockerfile`, then `python` in `[tool.wheelgap]`, then a
`.python-version` file next to the input, then the Python wheelgap runs on.

`next` and `next-t` are the newest CPython this wheelgap knows and its free-threaded build. See
[Python versions](targets.md#python-versions).

### `-t`, `--target` {#target}

`-t PLATFORM`: a target platform, by name, alias or Docker platform name (`linux/arm64`). Repeatable and
comma-separated. `all` is the eight common native platforms, `everything` adds armv7l, ppc64le, s390x, riscv64 and
i686 Linux and `win32`, and `mobile` is iOS and Android. Default: `linux-x86_64,linux-aarch64,macos-arm64,win-amd64`,
unless `--dockerfile`, `[tool.wheelgap] targets` or uv's `required-environments` say otherwise. See
[Targets](targets.md).

### `--glibc` {#glibc}

`--glibc X.Y`: the glibc version of `linux-*` targets (default 2.36). Wheels for a newer glibc are rejected.

### `--musl` {#musl}

`--musl X.Y`: the musl version of `musl-*` targets (default 1.2). For Alpine older than 3.13, pass `--musl 1.1`.

### `--distro` {#distro}

`--distro NAME`: take the glibc or musl version from a distro, such as `ubuntu-22.04`, `debian-12`, `rhel-9`,
`amazonlinux-2023` or `alpine`. It doesn't select targets: keep using `-t`. See [Distros](targets.md#distros).

### `--macos` {#macos}

`--macos X.Y`: the minimum macOS of `macos-*` targets (default 14.0).

### `--ios` {#ios}

`--ios X.Y`: the minimum iOS of `ios-*` targets (default 13.0).

### `--android-api` {#android-api}

`--android-api N`: the minimum Android API level of `android-*` targets (default 24).

### `--dockerfile` {#dockerfile}

`--dockerfile PATH`: take the Python, the platform and the glibc or musl version from the image of the Dockerfile's
final stage, such as `FROM python:3.13-alpine`. `-p`, `-t`, `--glibc` and `--musl` still win. See
[From a Dockerfile](targets.md#from-a-dockerfile).

`-p` and `-t` together also stand in for an image wheelgap can't read: `FROM ubuntu:24.04`,
`nvidia/cuda`, `node`, a python tag that names no version, a `FROM` whose build argument has no value, or a heredoc
that an old `# syntax=` line may read differently (not with `--dockerfile-inputs`, which needs to know which lines
are instructions: there, drop the `# syntax=` line or set it to `docker/dockerfile:1`). In 0.13.0
that is an error even with both flags. Nothing is taken from such an image, and a note that `--quiet` doesn't hide
(also in `--json`, as [`dockerfile_notes`](output.md#json)) says what the Dockerfile then leaves unknown: which
Python the image's `pip` installs for and which pip version it has, the image's glibc or musl version (pass
`--glibc` or `--musl`; otherwise wheelgap's default is checked), the packages the image already has, the image's own
pip settings (`ENV PIP_…`, a `pip.conf`, a constraints file) and any `ONBUILD` instructions it runs in your build
(`python:3-onbuild` runs its own `pip install -r requirements.txt`). The note is also a quoted line under
`--markdown`, a `::notice` under [`--github-annotations`](#github-annotations) and a `note` result in
[`--sarif`](output.md#sarif). When the tag says Alpine or musl (`python:3-alpine`, `node:22-alpine`) and glibc
targets (`-t linux-…`) are checked, a second note (kind `image-libc`) says so: pass `-t musl-…` if the image uses
musl. Without both flags the image is still an error, and with `--dockerfile-inputs` it is the first error reported.
A `FROM` that Docker itself rejects stays an error, and so does `FROM scratch` (an empty image has no Python but one
a `COPY` brings), and, with `--dockerfile-inputs`, a `FROM ${NAME}` wheelgap can't expand in a Dockerfile with named
stages before it, since it may name one of them: pass its value with [`--build-arg`](#build-arg).

### `--dockerfile-inputs` {#dockerfile-inputs}

`--dockerfile-inputs` (new in 0.14.0; with `--dockerfile`, and no `INPUT`): check the requirements files the
Dockerfile's final stage installs with `RUN pip install -r FILE`, on the image's Python and platform, so "will this image build without
compiling anything?" is one command (add `--deps` for what they pull in).

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
```

With one pinned package (`packaging==25.0`) in `requirements.txt`:

```console
$ wheelgap --dockerfile Dockerfile --dockerfile-inputs -p 3.13 -t linux-x86_64
wheelgap: note: Dockerfile:4: RUN pip install -r reads requirements.txt
wheelgap 0.14.0 · requirements.txt (Dockerfile:4) · 1 package · Python 3.13

✓ The 1 package has a wheel for 1 target.
```

`FILE` is followed back through `WORKDIR` and `COPY` to a file in the build context, taken to be the Dockerfile's
folder unless [`--dockerfile-context DIR`](#dockerfile-context) names another, and each one gets a note on stderr
(`Dockerfile:4: RUN pip install -r reads requirements.txt`). The report says it too: in the header
(`requirements.txt (Dockerfile:4)`), in `--json`'s [`dockerfile_inputs`](output.md#json) and in each
`--github-annotations` message. `[tool.wheelgap]` and `.python-version` are read from that folder too. Without this
flag, `--dockerfile` reads only the image.

**What the answer is.** The Python packages the final image gets from the `pip install`, `uv pip install` and
(**after 0.14.0**) `uv sync` lines wheelgap can read. It is not "everything in the image": system packages, what the
base image ships and anything a script installs are outside it. Every line of the final stage falls into one of three groups, and the answer says
which lines it did not look at:

| | What wheelgap does |
| --- | --- |
| **Read** | Checks the packages, and names the file and line. The forms below. |
| **Passed over, with a note** | One note per class on stderr with the line numbers (`not read, since they install no Python packages with pip or uv: system package and account commands, lines 3, 9`), and the same list in `--json`'s [`dockerfile_notes`](output.md#json) |
| **Passed over, with a warning** | The answer is still given, and the exit code is unchanged. Each line gets `wheelgap: warning: Dockerfile:8: this line runs … If it installs Python packages, or another Python, that is not in this answer` on stderr (also under `--quiet`). The text and `--markdown` headline says `The 3 packages wheelgap read have a wheel for 2 targets; line 8 wasn't seen into` instead of a green ✓, and the report's last line repeats the line numbers (text, `--quiet` and `--markdown`). `--json` lists them in [`dockerfile_warnings`](output.md#json) and sets `summary.complete` to `false`; [`--github-annotations`](#github-annotations) writes a `::warning` at the line, [`--sarif`](output.md#sarif) a `warning` result, and `--junit` a `wheelgap.dockerfile-warning` property and a `system-out` line in each suite |
| **Stops, naming the line** | Exit 2 with `path:line` and the reason; nothing is looked up |

When no install line is left to read, that is an error too, and it names the lines it couldn't see into.

#### What is read

**The install lines.** An install counts alone, after `;`, or in a chain joined by `&&` that is the `RUN`'s last
(an `&&` chain a `;` or a new line ends stops, below).

| Form | Read as |
| --- | --- |
| `RUN pip install -r FILE` — also `pip3`, `python -m pip`, `python3 -m pip`, `/usr/local/bin/pip`; `pythonX.Y -m pip` and `pipX.Y` when X.Y is every Python checked; a venv's `pip` when an earlier `python -m venv` made it, also through `ENV PATH=/venv/bin:$PATH` (with `$NAME` from an earlier `ENV` or `ARG` expanded, as BuildKit does: `ENV PATH="$VIRTUAL_ENV/bin:$PATH"`) or `. /venv/bin/activate` | The file's requirements, with `-c FILE` and the flags below |
| `RUN pip install NAME`, `NAME==VERSION`, `"NAME>=1,<2"`, `NAME[extra]`, alone or beside `-r FILE` — `RUN pip install numpy==2.3.4`, `pip install "numpy<3" pandas`, `pip install 'uvicorn[standard]'` | Checked the way requirements given on wheelgap's own command line are (`wheelgap numpy==2.3.4 pandas`): a pin is that version, and anything else is the newest release pip would pick today, with a note (`Dockerfile:5: RUN pip install names 1 unpinned package (pandas), so the newest matching release was checked`). Each such line is its own source in the report, labelled `Dockerfile:5`, and `--json` lists the lines in [`dockerfile_packages`](output.md#json). What the names pull in is checked with `--deps`, as for any input, extras included. A name written with a variable is read when the variable has a literal value from an earlier `ENV` or `ARG` (`ARG TORCH=2.4.1` then `pip install torch==$TORCH`), with a note that the `ARG`'s default was used (pass `--build-arg` if your build sets another). Only a line that names nothing but `pip`, `setuptools` and `wheel` is passed over; `pip install uv` or `poetry` is checked like any other package |
| `RUN uv pip install --system -r FILE` | The file's requirements as uv installs them (below), with `-c FILE` and uv's flags. The `COPY --from=ghcr.io/astral-sh/uv[:TAG] /uv /uvx DEST/` that puts uv in the image is read (`/uvx` is optional) |
| `RUN uv sync --locked` or `--frozen` (**after 0.14.0**) | The `uv.lock` and `pyproject.toml` in the folder the line runs in, followed back to the build context as a `-r` file is — they must come from beside each other there — and the lock is checked as `wheelgap uv.lock` is, with `uv sync`'s own selection: the project's dependencies and its default groups, `--no-dev` leaving the dev group out as uv does. The project itself is listed as skipped, as an editable is for `wheelgap uv.lock`; when it has a `[build-system]`, uv builds it in the image, and the build requirements it fetches for that, which the lock doesn't pin, aren't checked. The sync's flags and files, and what it refuses, are below |
| `python --version`, `pip --version`, `pip list`/`freeze`/`show`/`check` | Install nothing, whatever the `PATH` |

**The supporting instructions.**

| Instruction | What it does for the read |
| --- | --- |
| `WORKDIR DIR` | A literal path; with none, the official python image's working folder is `/`, as BuildKit starts in `/` when the image sets no `WorkingDir` |
| `COPY SRC... DEST` from the build context | Plain copies (`--chown`, `--chmod`, `--link`): the files installs may read |
| `cd DIR` before the install on the same `RUN` | Where the install looks for its files |
| `ENV`/`ARG` values used in the install (`-r $DIR/requirements.txt`) | `--build-arg` replaces an `ARG`'s default |
| `python -m venv` | Makes the venv whose `pip` is then read |
| `ENV`/`ARG` settings that don't change which files are picked | Passed over; the fine list is below |

**`pip install` flags.** Any other flag is refused (below).

| Flag | Read |
| --- | --- |
| `-r FILE` / `--requirement FILE` | The file, whatever its name (`requirements.pip`, `deps.py`, no extension), as pip reads it — except a `pylock.toml` or `pylock.NAME.toml`, which is read as a lock, as pip 26.1 and later install it |
| `-c FILE` / `--constraint FILE` | Followed back through `WORKDIR` and `COPY` like `-r FILE`, with the same refusals, and applied to the line's requirements files as a `-c FILE` line at the top of each would be (the note on stderr names it too) |
| Several `-r FILE`s on one line (`pip install -r a.txt -r b.txt`) | One install, with one set of options, as pip reads them: an option line in one file (`--no-binary :all:`, `--only-binary`, `--pre`, an index option) and a `-c` line inside it count for the requirements of every file of the line, and for the requirements named on it (`pip install -r a.txt fastlib==2.0.0`), whichever comes first, as they do for pip. Each file is still its own input in the report, labelled with the line, and an index option another file of the line sets gets a note naming that file |
| `--no-cache-dir`, `--disable-pip-version-check`, `--no-input`, `-q` / `--quiet`, `--no-compile`, `--compile`, `--no-warn-script-location`, `--no-color`, `--break-system-packages` | Taken with no value |
| `--upgrade` / `-U` | Taken. **After 0.14.0**: an install with it gets no `kept-version` note (below) |
| `--root-user-action ignore` or `warn` (or `=ignore`) | pip's only values |
| `--progress-bar auto` / `on` / `off` / `raw` (or `=off`) | pip's only values |
| `--retries N`, `--timeout SECONDS`, `--default-timeout SECONDS` (or `=N`) | A whole number of retries, a number of seconds; any other value is refused |
| `--no-deps` | Taken; with `--deps` it is an error, since the line installs no dependencies |
| `--prefer-binary` | Read only when every requirement the line installs is pinned with `==` (in `FILE`, in the files it includes, and in a `-r` inside a `-c` file; a requirement that a constraint pins with `==` counts) and `--deps` isn't used: otherwise pip may take an older release that has a wheel over a newer one that has none, while wheelgap checks the newest |

**PyTorch's index (after 0.14.0).** A plain `RUN pip install NAME...` with one `--index-url` or
`--extra-index-url` on the exact HTTPS host `download.pytorch.org` no longer stops the rest of the check. For
example, `RUN pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu` lists `torch` as
`unknown`, with its Dockerfile line. Wheelgap sends none of the names on that line to either index: even with
`--extra-index-url`, pip might choose a file from that host. A later requirement for the same name in the same
Python environment is also `unknown` because pip may keep the installed version. A requirements file used both
before and after that install is only reported once, with that name marked `unknown` and the reason pointing to
the index line. Ordinary names on other lines are still checked. This is not a complete build check: the text report
warns, `--json` sets
`summary.complete` to `false`, and `--deps` refuses because the uninspected packages can change the dependency
set. Index URLs are not printed. Other index choices, mirrors, URLs with credentials, ports, queries or fragments,
compound commands, and index options on a `-r FILE` or `uv pip install` line are still refused.

**`uv pip install` flags.** `--system` (or an earlier `ENV UV_SYSTEM_PYTHON=1` or
`=true`) is required: without it uv installs into a virtual environment. Any other flag is an error, pip's own among
them (`--no-input`, `--disable-pip-version-check`, `--root-user-action`, `--prefer-binary`, `--no-compile`,
`--progress-bar`, `--no-warn-script-location`, `--no-color`, `--retries`, `--timeout`).

| Flag | Read |
| --- | --- |
| `-r FILE` / `--requirement FILE` and `-c FILE` / `--constraint FILE` (uv also spells them `--requirements`, `--constraints`) | Only `*.txt` or `*.in` files: uv reads other names as locks, scripts or projects |
| `--no-cache`, `--no-cache-dir`, `--upgrade` / `-U`, `-q` / `--quiet` (also `-qq` and further repetitions), `--compile-bytecode` (and its alias `--compile`), `--no-progress`, `--reinstall` (the same requirements are installed again), `--refresh` (cached data is only revalidated), `--break-system-packages`, `--strict` (it only warns after the install), `--no-python-downloads`, `--system-certs`, `--no-deps` | Taken with no value |
| `-v` / `--verbose` (also `-vv` and further repetitions) | Output alone; repeatable, but uv exits when quiet and verbose are together before `install` or together after it |
| `--link-mode clone` / `copy` / `hardlink` / `symlink` (or `=copy`) | uv's only values |
| `--output-format text` / `json` (or `=json`) | uv's only values: output alone |
| `--color auto` / `always` / `never` (or `=never`) | uv's only values: output alone |
| `--keyring-provider disabled` / `subprocess` (or `=disabled`) | uv's only values: authentication alone, not which requirements are selected |
| `--no-break-system-packages` | Taken with no value: uv's default, so it doesn't change what is installed; it may be combined with `--break-system-packages`, as uv allows |
| `--no-editable` | Taken with no value: editable lines are refused here, so this is a no-op for the pinned requirements that are read |
| `--no-editable-package NAME` (or `=NAME`) | uv's package names: editable lines are refused here, so this is a no-op for the pinned requirements that are read |
| `--refresh-package NAME` (or `=NAME`) | uv's package names: only that package's cached data is revalidated |
| `--reinstall-package NAME` (or `=NAME`) | uv's package names: only that package is installed again |

**`uv sync` flags** (**after 0.14.0**). Exactly one of `--locked` and `--frozen` is required: without one uv may
relock, and the copied lock may then not be what the image gets. `--no-dev`, `--no-install-project`,
`--no-install-workspace`, `--no-install-local`, `--no-editable`, `--compile-bytecode` (or its alias `--compile`),
`--reinstall`, `--refresh`, `--inexact`, `--link-mode`, `--output-format`, `--color`, `--keyring-provider`,
`--no-editable-package`, `--refresh-package` and `--reinstall-package` with uv's values, and the harmless global
flags may join it after `sync`; a global flag before the subcommand (`uv --quiet sync --locked`) is an error, and
so is every other flag, since it may change the installed set.

| Flag | Read |
| --- | --- |
| `--locked` or `--frozen` | Exactly one, required: uv doesn't relock |
| `--no-dev` | The dev group is left out of the check, as uv leaves it out of the image |
| `--no-install-project`, `--no-install-workspace`, `--no-install-local`, `--no-editable` | Taken with no value: they don't change which locked dependencies are installed |
| `--compile-bytecode` / `--compile`, `--reinstall`, `--refresh`, `--inexact` | Taken with no value: they don't change which locked dependencies are installed (`--inexact` only leaves extraneous packages in place) |
| `--link-mode clone` / `copy` / `hardlink` / `symlink` (or `=copy`) | uv's only values: how cache files are linked doesn't change which locked dependencies are installed |
| `--output-format text` / `json` (or `=json`) | uv's only values: output alone doesn't change which locked dependencies are installed |
| `--color auto` / `always` / `never` (or `=never`) | uv's only values: output alone doesn't change which locked dependencies are installed |
| `--keyring-provider disabled` / `subprocess` (or `=disabled`) | uv's only values: authentication alone doesn't change which locked dependencies are installed |
| `--no-editable-package NAME` (or `=NAME`) | uv's package names: it only decides whether a named first-party editable package is installed non-editably, not which locked dependencies are installed |
| `--refresh-package NAME` (or `=NAME`) | uv's package names: only that package's cached data is revalidated, which doesn't change which locked dependencies are installed |
| `--reinstall-package NAME` (or `=NAME`) | uv's package names: only that locked package is installed again, which doesn't change which locked dependencies are installed |
| `-q` / `--quiet` (also `-qq` and further repetitions), `--no-cache` / `--no-cache-dir`, `--no-progress`, `--no-python-downloads`, `--system-certs` | Taken with no value |
| `-v` / `--verbose` (also `-vv` and further repetitions) | Output alone; repeatable, but uv exits when quiet and verbose are combined |

The line is the selection: `--extra`, `--all-extras`, `--group`, `--only-group` and `--no-dev` on wheelgap's command
line are an error with it (they would check another set than the image gets), and `[tool.wheelgap]` `extras` and
`groups` don't change it. With none of the refusals below, uv 0.13.9 creates `.venv` from the image's own Python (it
downloads one only when no Python it finds satisfies `requires-python`), so the image's Python and platform are the
target — unless the project pins one:

**A `.python-version` a `COPY` puts in the project's folder is read**: its pin is the Python `uv sync` installs
for, so that Python is checked — it must be among the `-p` Pythons when `-p` is given, and a pin that isn't the
image's Python is an error when another install in the stage installs the image's (the answer can't split Pythons).
A patch pin is checked at its `X.Y`'s newest patch release, as an image's is. A pin that isn't a CPython `X.Y`,
`X.Y.Z` or `X.Yt` version is an error.

**`ENV` and `ARG` settings.** These don't change
which files are picked, and are fine: `PIP_NO_CACHE_DIR`, `PIP_CACHE_DIR`, `PIP_DISABLE_PIP_VERSION_CHECK`,
`PIP_ROOT_USER_ACTION`, `PIP_NO_INPUT`, `PIP_PROGRESS_BAR`, `PIP_NO_COLOR`, `PIP_DEFAULT_TIMEOUT`, `PIP_TIMEOUT`,
`PIP_RETRIES`, `PIP_QUIET`, `PIP_VERBOSE`, `PIP_NO_WARN_SCRIPT_LOCATION`, `PIP_NO_COMPILE`, `PIP_COMPILE`,
`PIP_BREAK_SYSTEM_PACKAGES`, `PIP_NO_PYTHON_VERSION_WARNING`; `UV_NO_CACHE`, `UV_CACHE_DIR`, `UV_COMPILE_BYTECODE`,
`UV_LINK_MODE`, `UV_NO_PROGRESS`, `UV_HTTP_TIMEOUT`, `UV_HTTP_RETRIES`, `UV_CONCURRENT_DOWNLOADS`,
`UV_CONCURRENT_BUILDS`, `UV_CONCURRENT_INSTALLS`, `UV_PYTHON_DOWNLOADS`, `UV_PYTHON_INSTALL_DIR`, `UV_TOOL_BIN_DIR`,
`UV_TOOL_DIR`, `UV_INSTALL_DIR`, `UV_BREAK_SYSTEM_PACKAGES`, `UV_NO_INSTALL_PROJECT`, `UV_NO_INSTALL_WORKSPACE`,
`UV_NO_INSTALL_LOCAL`, `UV_NO_EDITABLE`, `UV_PROJECT_ENVIRONMENT`, `UV_SYSTEM_CERTS`, and
`UV_VERSION` (not a uv setting; Dockerfiles use the name for the uv they install). `UV_SYSTEM_PYTHON=1` (or `true`)
counts as `--system` on later `uv pip install` lines. Any other name that starts with `PIP_` or `UV_`
(`PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_NO_BINARY`, `PIP_ONLY_BINARY`, `PIP_CONSTRAINT`, `PIP_PRE`,
`UV_INDEX…`, `UV_NO_BINARY…`, `UV_PRERELEASE`, `UV_RESOLUTION`, `UV_EXCLUDE_NEWER`, a name wheelgap has never heard
of) is an error for the install lines after it, naming the variable and never its value. An `ARG` with no default
(`ARG PIP_INDEX_URL`) isn't in the build's environment unless `--build-arg` sets it, so it is read as unset, with a
note; pass the same `--build-arg` to wheelgap if your build does, and it is then an error like any other value. A
value that only mentions `PIP_` no longer counts.

**Includes: a `-r` or `-c` line inside `FILE`** (and inside the file it names, and so on).
It is followed when the included file is in the image where pip looks for it, and the same holds inside a
constraints file named with `-c FILE` on the `RUN` line, where a `-r` adds requirements (their packages are
checked) and a `-c` adds constraints, as pip reads them. pip reads an include beside the including file *in the
image*, so wheelgap finds the `COPY` that puts a file at that path and reads it only if it is the same file as the
one beside the including file in the build context; the note on stderr then names every file read
(`Dockerfile:4: RUN pip install -r reads requirements.txt, base.txt, -c constraints.txt (which includes
pins.txt)`).

**`.dockerignore`**, read as BuildKit reads it: `<Dockerfile
name>.dockerignore` beside the Dockerfile if there is one, otherwise `.dockerignore` at the root of the build
context. A requirements or constraints file that the rules leave out of the context, or a file one of them includes
with `-r` or `-c`, is an error naming its `COPY` line and the rule: the image doesn't have it, so the build would
fail there, and wheelgap doesn't read it from disk. That holds for a file a folder `COPY` (`COPY . .`) would have
brought, for a file or wholly ignored folder a `COPY` names itself (which Docker then doesn't find), and for config
files (`pip.conf`, `pyproject.toml`, `uv.toml`): an ignored config file never enters the image and doesn't refuse
the project. When the ignore file has rules, a requirements file that is a symlink, or is reached through a
symlinked folder, isn't read either: the rules may leave out what the link points to, and the link then dangles in
the image. A pattern wheelgap can't read exactly as Docker does is an error naming the ignore file's line: one
Docker rejects, a `\` escape before a letter or digit, any `\` on Windows (where Docker reads it as a path
separator), an exception that climbs out of its own path (`!../../requirements.txt`, which Docker reads as a rule
that excludes), or an empty `<Dockerfile name>.dockerignore` next to a `.dockerignore` with rules (which of the two
Docker then uses depends on the Dockerfile frontend).

#### Passed over, with a note

A `RUN` command that can't install Python packages with pip or uv:

- system package and account commands (`apt-get`, `apk`, `dnf`, `useradd`, with `DEBIAN_FRONTEND=noninteractive` in
  front or exported);
- shell and file commands (`mkdir`, `chmod`, `rm`, `cp`, `ln`, `mv`, `tee`, `sed`, `echo`, `cd`, `set -eux`,
  `declare`, `if`/`for` around them, and redirects), as long as what they write isn't the image's Python or pip or
  a config folder;
- other programs (`curl`, `wget`, `git`, `go build`, `cargo fetch`, `cmake -E`, compilers), and the JavaScript
  package managers with their scripts turned off (`npm ci --ignore-scripts`, `yarn install --mode=skip-build`),
  installing named packages globally (`npm install -g pnpm@9`, `yarn global add serve`) or, for npm, locally too
  (`npm install left-pad` — npm runs the root package.json's scripts only when no package is named — but only
  while no earlier COPY or ADD can have put a package.json in its working folder or, without an explicit
  `--prefix`, a folder above it), which runs their scripts and not the project's, or only asked for their version
  or settings, as long as their arguments don't name an installer;
- `python -m venv`, `pip --version`/`list`/`check`, `RUN --mount=type=cache` or `tmpfs` over a folder that holds
  neither the requirements file nor a pip config file, `--network`, `--security`;
- `pip install` (with no pin or upper bound on `pip`) of only `pip`, `setuptools` and `wheel`;
- an `ARG PIP_...` with no default;
- `COPY --from=STAGE-OR-IMAGE` of a single path that doesn't look like Python and is a file or lands in a bin
  folder (`/app/dist/main.js`, `COPY --from=node /usr/local/bin/node /usr/local/bin/`), listed as not followed;
- `SHELL` set to sh, dash, ash or bash with `-c`.

#### Passed over, with a warning

A line wheelgap can't see into, **after the last install it reads**. After the last install read, such a line can
add packages, but can't change what the read installs fetched. A `COPY --from` before an install can only bring
packages pip keeps instead of fetching, so the answer checks more, not less (with `--deps` it stops: a kept older
copy changes the dependency set):

- a script (`./install.sh`, `sh x.sh`, `bash -c "$CMD"`, `bash -lc`, `su -c`, `curl … | sh`), `make`, `xargs`,
  `find -exec`, `awk` with `system()`, `eval`, `.`/`source` of a script, `python script.py` (also from a redirect
  or a pipe: `python < install.py`, `cat install.py | python`), `python -c`, `python -m MODULE` other than pip and
  venv, a heredoc with no installer named in it, a `case` statement or a shell function with no installer named in
  it, `timeout`, `sudo -H` and programs wheelgap doesn't know, a package file (`dpkg -i x.deb`,
  `apt-get install ./x.deb`, `rpm -i`);
- a program that runs the project's own code, **also before an install**: a package.json script, named (`npm
  run`/`run-script`/`start`/`test`/`exec`/`explore`/`init`, `yarn`, `pnpm` and `bun` with a script name or
  `run`/`exec`/`dlx`, `deno run`/`task`) or a lifecycle one such as `postinstall` (`npm
  ci`/`pack`/`version`/`rebuild`, an `npm install` with no package named or with a path, tarball or `file:`
  alias, a local `npm install` of named packages once a COPY or ADD may have put a package.json in its working
  folder or one above it (npm's project is the nearest such folder, and its workspaces and `file:` dependencies
  are the project's own code), a bare `yarn`,
  `yarn`/`pnpm`/`bun install`, unless `--ignore-scripts` or yarn's `--mode=skip-build`),
  `npx`, a build file (`cargo build`/`check`/`install`/`run`/`test`, which run build.rs, `cmake` other than `-E`
  and `--version`, `ninja`, `mvn`, `gradle`, `dotnet`, `bundle`, `composer`) or a
  script file (`node FILE`, `go run`, `cmake -P`, `ruby FILE`, `php FILE`, `java -jar`, and `node`, `ruby` or `php`
  reading a script from a redirect or a pipe, `node < setup.js`);
- a program whose arguments name an installer (`node -e "…execSync('pip install x')"`, `ruby -e`,
  `sed 'e pip install x'`, `git -c alias.x='!pip install x' x`, `npm exec -- pip install x`);
- a write over the image's Python or pip (`ln -sf /usr/bin/python3 /usr/local/bin/python`,
  `update-alternatives --install /usr/bin/python3 …`, `cp`/`tee`/`>` onto `/usr/local/bin/pip`, `sed -i` in a
  Python's `lib/python3.X`, a `.pth` file, anything in a venv);
- `pip install pip==X` (or another pin or upper bound on pip);
- every other `COPY --from=STAGE-OR-IMAGE` (a folder may hold installed packages: a venv, `site-packages`,
  `--target` output, wheels), also before the install, as long as it doesn't land on the image's Python (a bin
  folder, `lib/python3.X`, `libpython`, `/root/.local`; another entry of a lib folder, `/usr/local/lib/node_modules`,
  is fine);
- an `ENV PATH`, `PYTHONPATH`, `VIRTUAL_ENV`, `PYTHONHOME` or `PYTHONUSERBASE` that names a copied folder, after
  the last install.

#### What is refused, and why

Each item is exit 2, `path:line` with the reason; nothing is looked up. "Before an install" means before one
wheelgap reads, in an earlier `RUN` or earlier on the same one.

| Refused | Why | What to do instead |
| --- | --- | --- |
| Any line from the warning list above that comes **before** an install wheelgap reads | It may rewrite the requirements file, write a pip config file, set `PIP_` values or change the pip and Python the install runs — answering could be wrong. Exception: a program that runs the project's own code is a warning there too (``this line runs `npm ci`, which runs the project's own code before the install on line 5, and may change what it installs``), so a full-stack Dockerfile still gets an answer, marked incomplete, and it stops only with `--deps`, as a `COPY --from` does | Move the line after the install, or to a stage the final one isn't built `FROM` |
| A heredoc that names pip, python, uv or another installer, anywhere | A heredoc's body isn't split into commands | Put the commands on `RUN` lines |
| An install in a shell position that may not run, or may fail unnoticed: after `\|\|`, after `&&` in a chain that a `;`, `&` or new line ends (`[ "$MODE" = dev ] && pip install -r dev.txt; …`), after an `exit` or `return` on the same `RUN`, negated with `!`, in a pipeline, in the background (also `cmd &> file` under sh, where `&>` is `&` then a redirect; under `SHELL ["/bin/bash", …]` it is a redirect), inside `if`/`for`/`while`/`{ }`/`( )`/`$( )`/`sh -c`/`case`/a function | The packages it installs may never reach the image | Put the install on a plain `RUN` line of its own |
| `pip install .`, `./pkg`, `-e .`, a wheel or sdist file | The project itself isn't read yet (reading its `pyproject.toml` is planned) | Install from a requirements file |
| A URL or `NAME @ URL` | A file from a URL isn't PyPI's, and only PyPI is checked. The text isn't echoed, since a URL may hold credentials | If the package is on PyPI, pin it by name |
| `-i` / `--index-url`, `--extra-index-url`, `-f` / `--find-links`, `--no-index`, `--trusted-host` (and uv's `--index`, `--default-index`) | The files on another index can differ from PyPI's, and only PyPI is checked. A requirements *file* with such a line is still read against PyPI alone today, a known gap that isn't copied here | Check the other index's files on the command line instead |
| Any other `pip install` flag — `--require-hashes`, `--pre`, `--only-binary`, `--user`, `--target`, `--ignore-installed`, `-e`, combined short flags such as `-Uq`, `--requirement=FILE` — or a value pip doesn't take (`--progress-bar pretty`); on a `uv pip install` line, any flag not in the uv table above, pip's own among them | It may change what the installer picks, or make it exit | Drop the flag if the build doesn't need it; write `--requirement=FILE` as `-r FILE` and a combined flag such as `-Uq` as `-U -q` |
| A `uv pip install` without `--system` (or an earlier `ENV UV_SYSTEM_PYTHON=1`) | uv then installs into a virtual environment, not the image's Python | Add `--system` |
| `pip uninstall`/`download`/`config`, `uv sync` in another form than `--locked` or `--frozen` (optionally `--no-dev`, `--no-install-project`, `--no-install-workspace`, `--no-install-local`, `--no-editable`, `--compile-bytecode`/`--compile`, `--reinstall`, `--refresh`, `--inexact`, `--link-mode`, `--output-format`, `--color`, `--keyring-provider`, `--no-editable-package`, `--refresh-package` or `--reinstall-package`, **after 0.14.0**) and every other `uv` command (`uv add`, `uv pip sync`), `poetry`, `pdm`, `pipenv`, `conda`, `mamba`, `pipx`, `easy_install`, `hatch`, `pixi`, `python setup.py` | Python packages installed a way that isn't read | Install with `pip install -r` or `uv pip install --system -r`, or sync with `uv sync --locked` |
| A `uv.lock` or `pyproject.toml` no `COPY` brings before a `uv sync` line, or any `COPY --from` before it other than uv's own `/uv /uvx` (whose files wheelgap can't read), the two from different folders of the build context, or a cache or tmpfs mount over them | The image's pair may not be one project, or the build may read another file than the one on disk | `COPY` the lock and `pyproject.toml` in together, from beside each other in the build context |
| For a `uv sync` line, a `uv.toml` or `[tool.uv]` table uv would read settings from, or a `uv.toml` with settings beside the lock in the build context that the image doesn't get (not copied, or left out by `.dockerignore`) | uv resolves with those settings, so the check would follow settings the image doesn't use — or miss the ones it does | Copy the requirements the lock pins into a `-r` file instead, or keep uv's settings out of the build |
| For a `uv sync` line, a `.python-version` an earlier `RUN` writes, one a `COPY` puts above the project's folder (uv 0.13.9 reads it only when that folder is the workspace root, which isn't read) or in uv's config folder (`~/.config/uv`, where `uv python pin --global` writes), a `.python-versions` (uv 0.13.9 reads its first version), a `.venv` in the project's folder, or `UV_PROJECT_ENVIRONMENT` (in an `ENV`, an `ARG` with a value, or on the `RUN`) | Each may give the sync a Python or environment the check doesn't know | `COPY` a `.python-version` into the project's folder instead — it is read, above — or drop the file or setting |
| A `pyproject.toml` `requires-python` the `uv sync` line's Python doesn't satisfy | uv sync fails there, so the image gets nothing | Match the image's Python, or the `.python-version` pin, to the project |
| A pip or python at a path other than `/usr/local/bin` or a venv an earlier `python -m venv` made (`/usr/bin/python3` is Debian's, not the official image's — when `-p`/`-t` stand in for an image wheelgap can't read, `/usr/bin` and the other bin folders are read too) | Another Python than the one checked | Install with the image's own `pip` (`/usr/local/bin`), or a venv an earlier `python -m venv` made |
| `pythonX.Y`, `pipX.Y` or a venv made by one, when X.Y isn't every Python checked | Another Python than the one checked | Check only that Python: `-p X.Y` |
| A bare `pip`, `python` or `uv` after `ENV PATH`, `PATH=…`, `export PATH=…` or `. …/bin/activate` puts a folder first that isn't the image's or such a venv's, or after bash's `hash -p` names the file a command runs | Which program runs is unknown | Name `/usr/local/bin/pip`, or keep the image's `PATH` |
| An install whose `-r` or `-c` value, or a requirement named on the line, has a variable with no known value, a command substitution or a glob, or follows a `cd` to an unknown folder, or uses a name an `ENV` and then an `ARG` with a value both set (BuildKit gives the `ARG`'s value, the classic builder the `ENV`'s) | Which file or requirement is read is unknown | Write the value literally, or give the variable a value with an `ENV` or an `ARG` before the install |
| An install under `RUN --mount=type=bind`, `secret` or `ssh`, or under a cache or tmpfs mount over the requirements file's folder, a pip config folder, or a folder wheelgap can't resolve | Which file is read may differ per build | Unmount the folder, or copy the file in |
| A requirements file that an earlier unread command names, or names a folder or glob that covers it (`sed -i … requirements.txt`, `curl -o`, `git clone … .`), or that `tar`, `unzip` or `patch` in its folder, or `git checkout`/`pull`/`reset`/`stash`/`apply`/… anywhere, may have changed | What the file says at build time isn't what is on disk | `COPY` the file in, or move the command after the install |
| A command that names a pip or uv config file (`pip.conf`, `uv.toml`, `.netrc`, `/etc/xdg/pip`) or writes a whole folder pip reads one from (`cp -r cfg/. /etc/`, `mv cfg /root/.config`, a home folder, `/usr/local`) | The config may change what the install fetches | Move the write after the install |
| `PIP_…`/`UV_…` set on the `RUN` (by an assignment, `export`, `declare -x` or `typeset -x`, or an `export` whose names aren't known, such as `export $(cat .env \| xargs)`) or by `ENV`/`ARG`, unless in the fine list above | The setting may change which files are picked | Drop the setting, or use a form the list above allows |
| `ADD`, globs in `COPY` | Which files land is unknown | Use `COPY` with literal paths |
| A `COPY --from` that may put a requirements file where an install reads it, bring a pip or uv config file (also into a folder an `ENV HOME`, `XDG_CONFIG_HOME` or `XDG_CONFIG_DIRS` points pip at), or land on the image's Python or pip before an install (`/usr/local`, `/usr/local/lib`, the bin folders, `/root/.local`, a home folder, a venv); a `COPY --from` into the working folder (or of a standard-library or pip module's name into it) before `python -m pip`, which imports from there first; with `--deps`, any `COPY --from` that may bring packages before an install | What it brings isn't followed — it may hold packages, a config file or another pip | Copy from the build context, or move the `COPY --from` after the install |
| An `ENV PATH`, `PYTHONPATH`, `PYTHONUSERBASE`, `PYTHONHOME` or `VIRTUAL_ENV` that names a folder a `COPY --from` brings, before an install, whether the `ENV` comes before the `COPY --from` or after it | The install may run another pip or Python, or see packages wheelgap doesn't follow | Move the `ENV` after the last install |
| An `ENV` entry with a variable wheelgap can't expand (such as `$BASE/deps` with `BASE` an `ARG` with no value), which may be any folder a `COPY --from` fills | Which folder it names is unknown | Give the variable a literal value |
| On a Windows image, any `RUN` that isn't one plain command | It runs in `cmd /S /C`, where `set X=…&& pip install` changes pip's settings | One command per `RUN` |
| Under a bash `SHELL`, any `RUN` after an `ENV` or `ARG` that sets `BASH_ENV` | bash runs that file first | Don't set `BASH_ENV`, or use the default `SHELL` |
| `ONBUILD` in a stage the final stage builds on | A trigger of a stage the final stage builds on runs in the stage built `FROM` it | Keep `ONBUILD` out of base stages |
| A `SHELL` other than sh, dash, ash or bash with `-c`; a `# syntax=` frontend other than `docker/dockerfile` | The `RUN` text is split by rules wheelgap doesn't know | Use the stock shell and frontend |
| A requirements file installed twice with different `-c` files | The constraint set differs per install | Give every line that reads the file the same `-c` files |
| A requirements file installed again with other `-r` files than the first line, whose option lines change what pip picks for it | The other `-r` files' options change what pip picks for this file's requirements, and wheelgap checks each file once | Repeat the same `-r` set, or give the file a line of its own |
| A `pylock.toml` beside a requirements file on one `pip install` line | pip picks the lock's files with the requirements files' options, in either order, which the pylock.toml reader doesn't follow | Install the lock on a line of its own |
| A file installed by pip on one line and by uv on another | The two resolve differently | Pick one installer for the file |
| `-c FILE` together with requirements named on the line | Not read yet | Put the names in a requirements file and install it with `-r` beside the `-c` |
| `--prefer-binary` with a requirement that isn't pinned with `==`, or with `--deps` | pip may take an older release that has a wheel, while wheelgap checks the newest | Pin every requirement with `==`, or drop `--prefer-binary` |
| An include that no `COPY` puts where pip looks (pip would stop the build), one where a `COPY` puts a different file, a URL, a `${VAR}`, an absolute path, an abbreviated option (`--req`), an include on a line with an editable, or a symlink anywhere in the chain | Which file pip reads in the image isn't known | Write the include as a plain `-r FILE` / `-c FILE` with a path relative to the including file, and `COPY` that file (not a symlink) to the same spot beside it in the image |
| An unquoted extras requirement (`uvicorn[standard]`) when a file may match it as a glob | The shell leaves it as written unless the working folder has a file named like the requirement with one letter of the extras in place of the brackets (`uvicorns`), and then pip installs that file's name. wheelgap reads it as the requirement it spells only when it can tell that no such name is there: the folder is one the Dockerfile made, or `/` of an official python image (whose root folders, `usr`, `lib`, `etc` and the rest, are known: `pip install us[r]` there stops), not `/usr/…` or another folder of the base image; the image is one wheelgap read, and no `COPY` from the build context, `WORKDIR`, or unread command before the install may put a matching name in it. A path with brackets (`./pkg[dev]`, `-e ./pkg[dev]`) stops as a project folder or an editable install | Quote it (`'uvicorn[standard]'`) and it is read whatever the folder holds |
| Under `uv pip install --system`, a requirement (in `FILE`, in a file it includes, or named on the line) that isn't pinned with `==`, by itself or by a constraints file; or `--deps` | For a range uv and pip can pick different versions, and wheelgap picks as pip does: an unpinned requirement is an error naming the `RUN` line, the requirement and its file | Pin every requirement |
| Under `uv pip install`, a `-r` or `-c` file not named `*.txt` or `*.in` | uv reads other names as locks, scripts or projects | Name a requirements file `*.txt` or `*.in`, or install it with pip |
| Under `uv pip install`, an option line other than a plain `-r FILE` or `-c FILE` in any file the line reads (`--extra-index-url`, `--index-url`, `--no-binary`, `--only-binary`, `--pre`, `-e`) | uv reads these by its own rules, and with a second index it takes the first one that has the package, where pip takes the best file of them all. A requirement with its own `--hash` is fine | Keep the file to plain requirements and `-r`/`-c` lines |
| Under `uv pip install`, a `-r` inside a constraints file | pip installs what it names, uv only constrains with it | Move the `-r` to a requirements file |
| Before a `uv pip install` line, an earlier `RUN` command wheelgap can't see into that names a `uv.toml`, and a `COPY` that brings a `uv.toml` or `pyproject.toml`. **After 0.14.0**: only a non-empty `uv.toml` or a `pyproject.toml` with a non-empty `[tool.uv]` table, anywhere in what the `COPY` brings, and an unread command that names a `pyproject.toml` as well | The image's `uv pip install` would read settings from them. **After 0.14.0**, a plain `pyproject.toml` — `COPY . .` brings one for nearly every project — is fine, as is a `uv.toml` that sets nothing; a file that isn't valid TOML or isn't UTF-8, or that is a symlink inside a copied folder, is refused, naming its path in the build context | Copy the requirements file alone and run `uv pip install` before the `COPY` that brings it |

**Multi-stage builds.** The final image
is the final stage on top of the stage it is built `FROM`, and so on back to an image, so wheelgap reads that chain
of stages in order, as one: an install in `FROM python:3.13 AS base` counts when the last stage is `FROM base`, and
the parent's `WORKDIR`, copied files, `ENV`, `SHELL` and anything that stops (an `ENV PIP_INDEX_URL`, a
`poetry install`) count too, each with its own line. Stages that aren't on the chain (a `builder` the final stage
only copies from) aren't read at all. Two things are deliberately not guessed:

- `COPY --from=builder …` brings files wheelgap doesn't follow. Where they land is known, so an install that reads
  a requirements file from under that folder is an error, and so is a copy that could carry a pip or uv config
  file (into `/`, `/etc`, `/root`, `/usr/local`, a home folder, a folder `ENV HOME` or `XDG_CONFIG_*` points pip
  at; or any such copy before a `uv pip install`), and one that lands on the image's Python or pip before an
  install. Otherwise the copy is passed over with a warning, since a folder from a builder stage is how its
  environment usually reaches the final image (`pip install --target=/packages` there, `COPY --from=build
  /packages /packages` and `ENV PYTHONPATH=/packages` here). Only a single file that doesn't look like Python, or
  one into a bin folder, is a note that says it wasn't followed.
- An `ARG` declared in a parent stage: newer BuildKit keeps its value in the stage built on it and older ones
  don't, so an install in a later stage that needs the value is an error. Declare the `ARG` again in that stage.

A `FROM ${NAME}` on the chain that wheelgap can't expand is an error when a named stage comes before it, since it
may name that stage; `--target` builds (stopping at an earlier stage) aren't read.

**How a `RUN` is split.** The text after `RUN` (and its `--mount`/`--network`/`--security` flags) is what Docker
hands to `/bin/sh -c`, so wheelgap splits it into commands by the POSIX shell's rules: quotes and backslashes,
`&&`, `||`, `;`, `|`, `&`, redirects, comments, `$NAME` and `${NAME}` (replaced when an earlier `ENV`, an `ARG`
with a value or an assignment on the same line gives a literal value; anything else makes the word unknown),
command substitutions (their commands are classified too). A glob, a leading `~` and a bash brace list make a word
unknown rather than being expanded. `&>` and `&>>` are read as dash and ash run them, `&` and then a redirect,
unless the `SHELL` is bash. A command is classified by its name, after `sudo`, `exec`, `env NAME=VALUE` and
`command`. Limits to know: a program that runs the project's own code before an install (`npm ci && npm run
build`) is a warning, not a stop, so the answer is given though that code may change what the install fetches; and
a command that changes a requirements file without naming it, its folder or a matching glob (through a variable,
say) isn't noticed.

**Each install is checked as a fresh one.** wheelgap answers for the newest release each requirement allows,
as pip or uv would install into an empty environment. Without `--upgrade`, the installer keeps a version that is
already in the image and satisfies the requirement — from the base image, a system package, or an earlier `RUN` —
so a build may keep an older release than the one checked. For pinned requirements this makes no difference: the
pin is checked as written. **After 0.14.0**, a `wheelgap: note:` (kind `kept-version` in `--json`'s
[`dockerfile_notes`](output.md#json)) says so when a later install requires, unpinned and without `--upgrade`, a
package an earlier install line put in the same environment (the image's Python, or the same venv) from its `-r`
files or by name (`Dockerfile:5: numpy is already installed by line 4 (base.txt); pip keeps that version if it
satisfies app.txt, and wheelgap checked the newest release`). The later requirement counts when it is in a `-r`
file, or named on a line with no `-r` file (a `-c` line inside one may pin the names beside it). The note names the
line that put the version there last, and there is none when the earlier line's pin doesn't satisfy the later range
(pip then installs the newest match, which is what was checked) or when the requirement has an environment marker. It
can't see an upgrade made as a dependency of another package, by a project script or by a `COPY` in between, so it
says "if it satisfies".

**How the answer differs for uv.** The answer follows uv, not pip: uv checks only the lower bound of a release's
`Requires-Python`, so a pin that pip refuses for an upper bound is checked as uv installs it, and the report says
which packages that changes (`pip_refuses` in `--json`). For a range the two can pick different versions, and
wheelgap picks as pip does — that is why under uv every requirement must be pinned with `==` and `--deps` can't be
used (in the refusal list above).

**What stays unknown, and is said in the notes:** the pip version the install lines run (a
`pip install --upgrade pip` line is passed over; wheelgap answers for a current pip), and pip, setuptools and wheel
themselves, which aren't checked. pip keeps a package the image already has when it satisfies the requirement, so
when a system package command installs Python packages (`apk add py3-yaml`, `apt-get install python3-numpy`), a
note names them: the build may not fetch every package the answer checks.

### `--dockerfile-context` {#dockerfile-context}

`--dockerfile-context DIR` (with `--dockerfile-inputs`): the build context, for a Dockerfile that
isn't built from its own folder. For `docker build -f docker/Dockerfile .`, run
`wheelgap --dockerfile docker/Dockerfile --dockerfile-inputs --dockerfile-context .`. `COPY` sources are looked for in
`DIR`, a path or symlink that leaves `DIR` is refused, and `[tool.wheelgap]` and `.python-version` are read from `DIR`.
The ignore file is looked for where Docker reads it: `<Dockerfile name>.dockerignore` beside the Dockerfile if there
is one, otherwise `.dockerignore` at the root of `DIR`. A `DIR` that isn't a folder exits 2. This isn't
`docker buildx build --build-context`, which adds named contexts.

### `--build-arg` {#build-arg}

`--build-arg NAME=VALUE` (repeatable, with `--dockerfile`): the value `docker build --build-arg`
gives a global ARG, so `FROM python:${PYTHON_VERSION}-slim` is read with the Python the build picks. It replaces the
ARG's default, or gives a value to an ARG with none; values follow the same rules as defaults (no quotes, escapes or
`$`). A name no ARG before the first `FROM` declares is ignored, as Docker ignores it there, with a note. See
[From a Dockerfile](targets.md#from-a-dockerfile).

### `--pyodide-lock` {#pyodide-lock}

`--pyodide-lock PATH_OR_HTTPS_URL`: a Pyodide release's `pyodide-lock.json`. Packages Pyodide ships itself count as
`✓ bundled` on `-t pyodide`. See [Pyodide](targets.md#pyodide).

### `--advisory` {#advisory}

`--advisory TARGET`: report gaps on `TARGET` without letting them fail the run.

Its gaps are reported as usual but never fail the run: the column is headed `win-arm64 (advisory)` in the table and the
Markdown report, the summary includes a line like
`3 gaps in 2 packages on advisory targets linux-aarch64, win-arm64 don't fail the run.` (it counts each package once
per advisory target, while the line above it counts packages), and `--json` lists the advisory target labels under
`advisory`, while `packages`, `summary.with_gaps` and `summary.per_target` still count those gaps.

- Advisory works per platform, so it marks every Python checked on that platform (`"3.13 win-arm64"` and
  `"3.14t win-arm64"`): a free-threaded `3.14t` can't be made advisory on its own.
- It takes anything `-t` does (aliases like `windows-arm64`, Docker names like `linux/arm64`), repeated or
  comma-separated, and replaces the configured `advisory` list (`--advisory ""` clears it for one run).
- It only marks targets, so a target must also be checked: an `--advisory` target that isn't is an error (exit 2),
  while a configured one that a run doesn't check gets a note on stderr.
- A group (`all`, `everything`, `mobile`) means whichever of its targets the run checks, so `--advisory all` makes
  every checked target advisory, and it's an error only when the run checks none of them. When every checked target
  is advisory, the run can't fail, not even when a lookup fails during an index outage, so a note on stderr says so
  (`-q` hides it).
- With `--fail-on missing`, a source build on a required target still passes and a missing package on one still
  fails, whatever the advisory targets show. An `--ignore NAME:TARGET` gap on an advisory target stays ignored and
  isn't counted as an advisory gap.
- In `--junit` reports an advisory gap is a skipped testcase with the message `advisory: source` (or `missing`,
  `unknown`), not a failure.
- `--advisory` doesn't work with `wheelgap diff` yet.

## Dependency selection

These select which dependencies of a `uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock` or `pyproject.toml` count. See
[Inputs](inputs.md#uvlock) for the full rules per format.

### `--extra` {#extra}

`--extra NAME`: include a uv project extra, or a `pyproject.toml` `[project.optional-dependencies]` group, or a
Poetry 2.x `poetry.lock` extra (like `poetry install --extras NAME`). Repeatable. Not supported for
`pdm.lock`.

### `--all-extras` {#all-extras}

Include every uv project extra (or every `pyproject.toml` extra, or every Poetry 2.x `poetry.lock` extra).

### `--group` {#group}

`--group NAME`: include a dependency group (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`, or a
`pyproject.toml` `[dependency-groups]` group). Repeatable. With `pyproject.toml`, no group is included by default.

### `--no-dev` {#no-dev}

Leave out the `dev` group (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`). An error for `pyproject.toml`,
which includes no group by default.

### `--only-group` {#only-group}

`--only-group NAME`: check only the named dependency groups, without the project's own dependencies or default
groups (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`, or `pyproject.toml` `[dependency-groups]`).
Repeatable; combine it with `--group` to add more groups, but not with `--extra` or `--all-extras`.

### `--environment` {#environment}

`--environment NAME`: the `pixi.lock` environment to check (default: `default`). It also makes a
directory's `pixi.lock` the lockfile read. Naming one for any other input is an error. See
[pixi.lock](inputs.md#pixilock).

## Index access

### `--dist` {#dist}

Check the files you're about to upload — the `dist/` folder `python -m build` or `uv build` leaves —
as the release they make: for each target, whether one of the wheels installs there or the build falls back to the
sdist, with the same statuses and `--explain` reasons as a lockfile row. It takes no other inputs; the project's name
and version come from the wheel and sdist filenames, and a folder that mixes projects or versions is refused.

The baseline is the newest release on PyPI older than this one (so a 1.9.3 maintenance release is compared with 1.9.2,
not with 2.0), looked up by the project's name (a final release ignores earlier pre-releases, so a 1.0 whose only
predecessor is 1.0rc1 counts as a first release): a target it had a wheel for that these files don't, on a Python that
wheel's Requires-Python allowed, is reported as dropped (`win-arm64: 46.0.3 had a wheel, this release has none`) and
fails the run, on top of the usual gap exit. `--json` has them as `dropped`, a map of target label to the last release's
version (`{}` when nothing was dropped or the project has no release to compare — a first release — and absent when the
lookup failed). The comparison needs PyPI's JSON API, so `--dist` is an error with `--offline` or `--index-url` (and
with `--online`, which would check PyPI's files instead of these). An `--ignore`d or `--advisory` target is left out, as
for gaps. The comparison is skipped with a note when a pip or uv index variable (`PIP_INDEX_URL` and the like) is set,
since PyPI's project of that name may not be yours. If the previous release couldn't be looked up, a note says the
comparison didn't run; both notes show even with `-q`, so a gate that didn't compare never passes silently.
### `--deps` {#deps}

For package names on the command line, requirements files, the `pip:` list of a conda `environment.yml`, and
dependencies declared in `pyproject.toml` or a script's `# /// script` block, also check what they install.
For a project file, this includes any extras you select with `--extra` or `--all-extras`. wheelgap resolves the
tree for each target from PyPI's metadata: for each requirement, the newest release that meets every constraint on it,
supports the target's Python and isn't yanked (a yanked release only for an `==` pin), following markers and extras as
pip evaluates them on that target. `--explain` shows the path to each gap (`via: mcp → PyJWT → cryptography`), and
`--json --explain` has it as `via`. `--explain` with `--deps` also turns on [`--suggest`](#suggest),
so each gap ends with the release to move to (`--no-suggest` turns it off). A release picked differently for two Pythons is a row for each.

With a requirements file, each requirement line is a root — pinned or not, with its markers and extras — and `-r`
includes are read too. A `-c` constraint file's constraints apply to the resolved tree, to dependencies as well as
named packages, and one wheelgap can't apply marks the package `unknown`, as without `--deps`. `--no-binary` and
`--only-binary` lines apply to the resolved packages as they do to the file's own. Editable, path and URL lines stay
skipped. A file that already lists what it installs — every requirement pinned, with `pip-compile`'s `--hash` lines or
`# via` comments — needs no resolving: wheelgap says so and checks it as written.

It isn't pip's resolver. When a later constraint rules out a release it already picked, it starts again with that
constraint known; if the constraints on a package can't all be met, it stops with an error rather than guess. A release
whose dependencies it can't read (an sdist-only release with no `Requires-Dist` on PyPI, say) is `unknown`. Where pip
would backtrack to an older release of a package to satisfy an upper bound (`"numpy<2" pandas`), it stops with that
error too. A package with no release that installs on a target (no matching wheel or sdist) is shown at its newest
matching release, as `missing`, rather than walking back to an old release of whatever depends on it. Dependencies are
read from a wheel's own metadata file, since PyPI's JSON page can leave them out. A marker it can't evaluate for a
target (`platform_release`) counts as true there. It looks
up each package in the tree on the PyPI JSON API, so it doesn't work with `--offline`, `--index-url` or a lockfile
input. It also refuses a project that configures another installer index or source in its own files, or
when a pip or uv index variable (`PIP_INDEX_URL`, `UV_INDEX_URL` and the like) is set; it can't see a `pip.conf` or
your user-level uv config, so with a private index there, use a lockfile. A lockfile from `uv lock` or
`pip-compile` is exact, so prefer one when you have it.

### `--online` {#online}

Re-check lockfile packages against PyPI instead of trusting the files the lockfile lists, for when a project has
uploaded new wheels for an existing version since you locked. Packages the lock says came from another index keep
their locked files, so their names are never sent to PyPI. Can't be combined with `--offline`. It doesn't make a
`uv.lock` limited by `[tool.uv] environments` checkable on other platforms: [such a lock](inputs.md#uvlock) leaves
out whole packages, not only wheels.

### `--offline` {#offline}

Never contact an index, for CI and coding agents without network. Inputs that need a lookup (requirements files,
`pyproject.toml`, `Pipfile.lock`, package names, lockfile entries with no file list, `pixi.lock` and `environment.yml`)
make it exit 2 with an error
naming the packages, so use a lockfile instead. A `--pyodide-lock` URL is refused too: pass a downloaded copy. The
[`requires-python` re-check](inputs.md#uvlock) is skipped, with a note that those Pythons may show false gaps.
`offline = true` in `[tool.wheelgap]` makes it the project default; `--online` overrides that for one run.

### `--no-cache` {#no-cache}

Don't read or save the 24-hour [cache](how-it-works.md#cache) of pinned package lookups for this run.

### `--clear-cache` {#clear-cache}

Delete the cache of pinned package lookups first. With no `INPUT`, do only that. It touches only wheelgap's own entry
files, and never follows a link.

### `--index-url` {#index-url}

`--index-url URL`: look packages up in this simple index (PEP 691 JSON) instead of PyPI's JSON API. Lockfiles that list
their files are still read offline unless `--online`. The URL must be https (plain http only for localhost).
Credentials can go in the URL (`https://user:token@host/simple`) and never appear in output; in CI,
`--index-url "$PRIVATE_INDEX"` keeps them out of your repo. An index that only serves HTML (PEP 503) gets a clear
error. See [Private indexes](how-it-works.md#private-indexes).

## Output

### `--all` {#all}

Show every package in the table, not just the ones with gaps.

### `--explain` {#explain}

For each gap, list the wheels that were rejected and why, closest misses first, and the dependency path that installs
the package where the input has one. With `--json`, adds `rejected` and `via` to each package. Text output can't
combine it with quiet mode. See [Output](output.md#explain). With [`--fix`](#fix), a gap that a
gap wheel fills also gets a `gap wheel` line.

A wheel that would fit but whose release was yanked is named with the reason `yanked`.

### `--from-log` {#from-log}

`--from-log FILE` reads a failed pip, uv, Poetry or PDM install log (`-` reads stdin), finds the
packages that failed to build, and checks each one as `--explain --suggest` does: why no wheel fits, and the release
that has one. The log doesn't say where the install ran, so pass `-p` and `-t` for that machine (or `--dockerfile` for
a docker build); without them it's an error.

```bash
wheelgap --from-log install.log -p 3.13 -t win-arm64
pip install -r requirements.txt 2>&1 | wheelgap --from-log - -p 3.13 -t win-arm64
```

It reads pip's summary at the end (`Failed to build NAME`; pip 24.1 to 25.1's `Failed to build installable wheels for
some pyproject.toml based projects (NAME)`; and pip 25.2 and later's `× Failed to build installable wheels for some
pyproject.toml based projects` with the names on the `╰─> NAME` line after it), counting only names pip also printed
`Building wheel for NAME` for, and uv's ``Failed to build `NAME==VERSION` `` on uv's own error lines (after `×`, `├─▶`,
`╰─▶`, `error:` or `Caused by:`). pip leaves the version out, so it comes from the `Collecting NAME==VERSION`,
`Downloading` and `Using cached` lines since the package's last `Collecting` line at column 0 (under `pip install -v`,
the indented `Collecting` lines of the pip that installs build dependencies don't count), in the same pip run: a
column-0 `Successfully installed` ends a run, so a later run's failure without its own `Collecting` line is an error.
pip doesn't mark where a run starts, so give `--from-log` the log of one pip run. uv's log doesn't name the index it
used, so a uv failure is checked on PyPI (or the index wheelgap is given).

**After 0.14.0:** pip's failures before it builds a wheel are read too: `× Getting requirements to build wheel did not
run successfully`, `× Preparing metadata (pyproject.toml) did not run successfully` and `error:
metadata-generation-failed` name no package, so the failed one is the sdist (`NAME-VERSION.tar.gz` or `.zip`) that the
`Downloading` or `Using cached` lines after the last column-0 `Collecting` line name for that package (PyYAML 5.4's
Cython 3 break is one); without such an sdist it's an error. When pip's lines don't name the version (`pip install -q`
hides `Collecting`, or the log's start was cut off), an exact pin on a `pip install`, `uv pip install` or `RUN pip
install` line in the log gives it (`pip install 'grpcio ==1.73.1'` at a shell prompt, a Dockerfile's `RUN pip install
tokenizers==0.20.0`); with no version anywhere, the latest release is checked, with a note (`the log doesn't name the
version of NAME that failed to build; checked NAME X, the latest release`; in `--json` as
[`from_log_notes`](output.md#json), and in `--markdown`). It's still an error when pip's `Processing` or `Obtaining`
line, or a path or `-e` on the log's install command (`pip install -q .`), says the package may be a local project,
whose name PyPI may give another project; when the install commands also name the package unpinned (`pip install -U
NAME` after `NAME==1.0`); and when Docker BuildKit steps' output interleaves (`#7`, `#8`, then `#7` again), since pip's
lines may then be another step's. Check `NAME==VERSION` directly then. Install commands on a comment or `echo` line, or
in a build's output (indented 6 or more spaces), don't count. Docker BuildKit's output works too: its `#12 28.56 ` prefix
(`--progress=plain`) and the `28.56 ` prefix of the failed step's excerpt are skipped, as GitHub Actions timestamps
are. A failure of the pip that installs a package's build or backend dependencies (`× pip subprocess to install build
dependencies did not run successfully`, or `backend dependencies`; `× installing build dependencies for NAME` since pip 25.3) is the package's own failure: its build never ran, so the answer is the
sdist's, whatever failed inside the subprocess (whose quoted output — the inner pip's lines, indented further —
isn't this run's).

It's an error rather than a guess when the log's install commands pin more than one version, when it names more than
one or pip backtracked (`INFO: pip is looking at multiple
versions of ...`), when it collects the package again without a version after naming one, when the package came from a
URL or a path, when pip's `Looking in indexes:` names another index than https://pypi.org/simple or the log has `Looking
in links:` (`--find-links`, with or without `--no-index`) and wheelgap isn't given an index too (`--index-url`,
`index-url` in `[tool.wheelgap]`, or `WHEELGAP_INDEX_URL`), when a failure line can't be read, or when more than 50
packages failed. Colour codes and GitHub Actions timestamps are skipped, a UTF-16 log (PowerShell's `>`) is read too,
and of a longer log only the last 8 MiB is read.

Poetry 2.x logs (captured with 2.5.1; normal or `-v`) are read from Poetry's final build-backend note, `Note: This
error originates from the build backend, ... issues with NAME (VERSION)`, after its `PEP517 build of a dependency
failed` header. The backend's own output, which Poetry indents under `|` but can also print at column 0 (what the
backend writes to stderr), can't be told apart from Poetry's lines, so the note counts only when an earlier Poetry
operation line names the same package and version, `- Installing NAME (VERSION)` or `- Updating NAME (OLD -> NEW)` (and
`Downgrading`) for NEW. Poetry's note doesn't name the index, so, as with uv, pass the index the install used with
`--index-url` when it wasn't PyPI.

PDM logs (captured with 2.29.7) need `pdm install -v`: normal output prints the same name and version for an index,
URL or local-path dependency, so it's refused with a request to rerun with `-v`. In verbose output the failure counts
only with all three: PDM's `✖ Install NAME VERSION failed` line, its `Error occurs adding NAME:` with a
`pdm.exceptions.BuildError` traceback, and the `Running PEP 517 backend to build a wheel for <Link ... (from URL)>`
line that says which index the sdist came from. A download or other install error is refused rather than reported as a
build failure, and a package built from another index than PyPI is an error that names the index, unless wheelgap is
given that index with `--index-url`.

Not read, so they give an error rather than a guess: Poetry 1.x logs, whose `ChefBuildError` diagnostics differ (`found
no package that failed to build`); a PDM sync that updates a package, whose failure line is `✖ Update NAME OLD -> NEW
failed`; and a PDM log from a console that can't print Unicode, where PDM writes `x` instead of `✖`. Other Poetry and
PDM versions whose diagnostics differ are refused too.

A note lists the packages the log says failed, and another says when one has a wheel for a checked target after all:
then the log likely comes from another platform or Python than `-p` and `-t`. A log whose build ran on PyPy (a PyPy
interpreter in its paths, or a build error only a PyPy build prints) gets a different note instead: wheelgap checks
CPython wheels only, so the answers don't say why the build failed there. `--no-suggest` turns `--suggest` off.
`quiet = true` in `[tool.wheelgap]` doesn't apply to it, as `--no-quiet` overrides it; `--quiet` is an error, as with
`--explain`, unless the output is `--json` or `--markdown`. It takes no other inputs, and can't be combined with
`--dist`, `--deps`, `--environment`, `--suggest-targets`, `--list-targets` or `--clear-cache`.

### `--conda-forge` {#conda-forge}

With `--explain`, also say when conda-forge has a package with exactly the same name and a build of
the same version that fits a gap's target. Only glibc Linux, macOS and Windows targets with a current conda-forge
Python can match, and it never changes a status, the summary or the exit code. Only names known to be on PyPI are sent
to anaconda.org; a run with `--index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag.
See [conda-forge hint](output.md#conda-forge) and [the PyPI-origin rule](how-it-works.md#pypi-origin).

### `--piwheels` {#piwheels}

With `--explain`, also say when [piwheels](https://www.piwheels.org/) has a wheel of the same version for a
`linux-armv7l` gap (32-bit Raspberry Pi OS). piwheels has no aarch64 wheels, so other targets are never looked up. It
never changes a status, the summary or the exit code. Only names known to be on PyPI are sent to piwheels.org; a run with `--index-url`
or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag.
See [piwheels hint](output.md#piwheels) and [the PyPI-origin rule](how-it-works.md#pypi-origin).

### `--notes`, `--no-notes` {#notes}

With `--explain`, also show the [coverage tracker](https://wheelgap.github.io/tracker/)'s notes for a
package with a gap, such as a binary package to use instead. It sends the names of packages with gaps to
wheelgap.github.io, so it's opt-in, and only for names that pass [the PyPI-origin rule](how-it-works.md#pypi-origin);
never with `--offline`, `--index-url` or a `WHEELGAP_INDEX_URL` mirror. `notes = true` in `[tool.wheelgap]` turns it
on by default, and `--no-notes` turns it off for one run. See [Tracker notes](output.md#notes).
It also shows the tracker's [build lab results](output.md#build-lab) for a `source` cell, from the
same file: whether exactly that release built from its sdist and imported on GitHub's runners for that target. They're
the lab's dated observations on GitHub's runners, which have compilers and many system libraries preinstalled, so a
build that works there can still need setup on your machine. Nothing the lab built is downloaded.

### `--build-info`, `--no-build-info` {#build-info}

With `--explain`, also say what each `source` build needs (`build needs: at least a Rust toolchain
(maturin<2.0,>=1.9)`), from the `[build-system]` table of the sdist's `pyproject.toml`. With `--fix`, also say which
distro packages to install to build a gap with no fix on a Linux target (`apk add --no-cache build-base` on Alpine,
`apt-get update && apt-get install -y build-essential` on Debian or Ubuntu); only those gaps' sdists are read then.
It downloads sdists, so it's
opt-in, and only for packages that pass [the PyPI-origin rule](how-it-works.md#pypi-origin); it's off with `--offline`,
`--index-url`, `WHEELGAP_INDEX_URL` or a pip or uv index setting in the environment. `build-info = true` in
`[tool.wheelgap]` turns it on by default; `--no-build-info` turns it off. See [Build requirements](output.md#build-info).

### `--suggest`, `--no-suggest` {#suggest}

With `--explain`, also name the lowest newer release with a wheel for a pinned package's gap targets
(`fastlib 2.0.0 -> 2.1.0 has wheels for musl-aarch64, win-amd64`) that keeps the wheels the package has on its other
targets, or each target's nearest when no one release has them all. For a pre-release pin, final releases come
first: one final release for every target, else one pre-release for every target, else a final release where one fits and a pre-release for the rest; a pre-release is labelled `(a pre-release)`. It
looks each package with a gap up on pypi.org
once, only for names that pass [the PyPI-origin rule](how-it-works.md#pypi-origin), and never changes a status, the
summary or the exit code. A run with `--index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the
flag. With `--markdown`, it renders suggestions as a Markdown list under the explanation. With `--json`, adds `suggest` to each package with a gap. See
[Newer releases](output.md#suggest).

With `--deps --explain` it's on by default, since `--deps` already looks every package in the tree
up on PyPI: each gap ends with the way out, the nearest newer release with the wheel or the last older one that had
it. That's only when `--deps` resolved every input; with a lockfile or an already-pinned requirements file among them,
it stays opt-in, since those packages' names weren't sent to PyPI. `--no-suggest` turns it off.

### `--write-constraints` {#write-constraints}

`--write-constraints FILE` (with `--explain --suggest`, or `--deps --explain`, which turns `--suggest` on): also write what `--suggest` found to a
pip/uv constraints file, one pin per package:

```text
cryptography==46.0.3  # wheels for win-arm64; 50.0.2 has none
```

A package gets a pin only when a single release has wheels for every gap target `--suggest` named a release for, and
it keeps every wheel the checked release has: a newer one as `--suggest` names it, or the last older one that had a
wheel. A package whose gaps need different releases, or whose only release with a wheel for a target drops one
elsewhere, is left out with a comment saying why, and so is a version range that resolved to different releases on
different Pythons. The file isn't checked against the rest of the dependency tree, so check that it still resolves:
`pip install -c FILE -r requirements.txt` or `uv pip compile -c FILE requirements.in`. Gaps on `--advisory` targets count
too, so a pin can move a package for a target that wouldn't fail the run. It's written even when the check fails; an
empty `FILE` is an error, as for `--junit`.

### `--fix` {#fix}

Following the usual output, say how to fix each gap cell (a package on a target, `source` or
`missing`, not `--ignore`d), in order of preference:

1. **Upgrade** to an official release: the nearest newer release with a wheel for that target, as
   [`--suggest`](#suggest) names it (not yanked, `Requires-Python` admitting the target's Python, no pre-release unless
   the pin is one, and then only when no one final release fits every target, in which case the line says it's a
   pre-release, and keeping a wheel on every target the package has one on now), with what to run or change for the
   input: `uv lock --upgrade-package NAME` for a uv.lock (`uv lock --script SCRIPT --upgrade-package NAME` for a
   script's lock; a bare name, since uv applies `NAME==VERSION` to every Python the lock covers, then check it reached
   VERSION), the new pins for a requirements file, in the file whose line pins the package (a `-r` include's own file;
   for one that pins hashes, `pip-compile --upgrade-package NAME==VERSION` or `uv pip compile --upgrade-package
   NAME==VERSION` to regenerate it), `poetry update NAME` or
   `pdm update NAME` for those locks (they stay within `pyproject.toml`'s constraints, so check the release they pick),
   and "upgrade NAME to VERSION" for the other lockfiles. Where wheelgap reads your own requirement for the package (a
   uv.lock project's dependencies, dependency groups and `[manifest]` constraints whose markers hold on the target,
   with any `extra == ...` counted as holding since uv locks every extra, or a ranged requirement in a
   requirements file, `pyproject.toml`, script or the command line, as written, or a requirements file's `-c`
   constraints), it says whether the release is within it or the requirement needs relaxing; otherwise it says
   nothing. With uv's `exclude-newer` (a uv.lock's `[options]`, or a `pyproject.toml`'s or script's own setting), only
   files uploaded before the cutoff count, as uv sees them; when only a release uploaded after it has the wheel, the
   line says `needs uv's exclude-newer moved past DATE` (when that release's last file was uploaded), or for a
   duration `needs uv's exclude-newer duration (P850D) shortened, or set to a time past DATE`, and the what-to-run
   lines start with that. A cutoff uv recorded in a uv.lock that wheelgap can't read means the package isn't looked
   up, and a line says so. A package whose targets need different releases (no one release has wheels for all of them) gets no pin:
   a line names each target's release instead. wheelgap doesn't resolve, so the rest of the dependencies must allow
   the release.
2. **Gap wheel**: when no newer release has the wheel, an unofficial wheel of the pinned version from wheelgap's gap
   wheel index, as below. When both exist, the upgrade comes first and the gap wheel is listed as the fallback
   ("if you can't upgrade NAME"); the gap wheel section opens with "As a fallback" when `--fix` names an upgrade.
3. **No fix yet**, with the reason (no newer release has a wheel; every one that has drops a wheel elsewhere, naming
   what the nearest one drops; `--no-binary`; or, with `--deps`, a package whose newer release with the wheel another
   package in the resolved tree rules out, naming that package and its requirement to upgrade or relax instead; a
   dependency that only a `-c` constraint or `exclude-newer` holds back gets the upgrade, with that to change, and
   no line of its own to edit), the package's issue tracker where no newer release has a wheel, and,
   on a free-threaded target whose release has only abi3 wheels, a line saying so. For a uv.lock, when what the newer
   releases drop is only an older Python's wheel because their `Requires-Python` leaves that Python out, uv may lock a
   release per Python: such a gap goes under "Maybe, with uv:", with `uv lock --upgrade-package NAME` to try (and the
   folder to run it in), or `uv lock --script SCRIPT --upgrade-package NAME` for a script's lock; not when your own
   requirement leaves that release out, which the "No fix yet" line then names.

A summary at the top counts them (`--fix: 6 gaps (3 packages × their targets): 4 can be fixed by upgrading to an
official release; 2 only by an unofficial gap wheel; 0 have no fix yet.`); only upgrades count as fixed, and gaps
whose releases weren't looked up are counted apart (`N weren't checked for an upgrade`), since they aren't known to
have no fix. The releases are looked up on pypi.org, one request per package, as for `--suggest` (and once for both
when you pass both), only for packages that pass [the PyPI-origin rule](how-it-works.md#pypi-origin); for the others,
and with another index, a line says they weren't looked up and why.

For the gap wheels, it says which gaps a wheel on wheelgap's [gap wheel index](https://wheelgap.github.io/wheels/)
fills, and how to install from it: a `[tool.uv.index]` and `[tool.uv.sources]` snippet for `pyproject.toml` with a
uv.lock input, the same entries for the `# /// script` block of a script (or of the script beside a script's lock,
`example.py.lock`, then `uv lock --script example.py`), and an `--extra-index-url` line for pip otherwise. A cell counts only when it's `source` or `missing`
(and not `--ignore`d), the package passes [the PyPI-origin rule](how-it-works.md#pypi-origin), and the index has a
wheel of exactly that version whose tags the target accepts. It reads the index's list from wheelgap.github.io in one
request that names no package, and only when a gap could use it. A list it could read is kept in the 1-hour
[cache](how-it-works.md#cache) (`--no-cache` neither reads nor saves it, and `--online` reads it again). When a cached
list fills none of the run's gaps, wheelgap answers from it if it was fetched less than 5 minutes ago and refreshes it
once it is older; if that refresh fails, it answers from the cached list and notes that it couldn't be refreshed. The
empty-fill note always names the UTC time the list was fetched. It never
changes a status, the summary or the exit code: wheelgap still reports what PyPI has. `--offline` refuses the flag
with an error; with `--index-url`, `index-url` in `[tool.wheelgap]` or a `WHEELGAP_INDEX_URL` mirror it's off and
reads nothing. If the list can't be read, a note on stderr says so. With `--json` it adds
[`gap_wheels`](output.md#fix), and each package's `fix` and the top-level `fix_summary`. There's no
config key for it.
See [Installing from the gap wheel index](recipes.md#installing-from-the-gap-wheel-index).

With `--explain` too, a target a newer official release fixes gets an `upgrade:` line first, and
each package a gap wheel fills gets a `gap wheel` line under its first filled
target (starting `if you can't upgrade, ` when an upgrade fixes one of them), naming the targets it fills and saying the wheel is unofficial: built by wheelgap from the PyPI sdist, not from
the package's maintainers. For inputs pip installs from as they are (a requirements file, a `pyproject.toml`, a pip
report, an `environment.yml`'s `pip:` list or package names) it gives the `pip install --extra-index-url` command;
for a lockfile it points to the `--fix` section, since that lockfile's own tool has to pick the index up, and for a
script likewise, since `uv run` reads the index from its `# /// script` block. With `--json`, each package gets
[`gap_wheel`](output.md#fix).

### `--print-gap-wheel-snippet` {#print-gap-wheel-snippet}

With [`--fix`](#fix), print only the configuration that installs the gap wheels, instead of the
usual output, for you to copy into your project. It never writes to your project. Comment lines say the wheels are unofficial
(built by wheelgap from the PyPI sdist, not by the package's maintainers), link to how they're built, and name each
package and the targets it fills. The exit code is the check's own, so a run with gaps still exits 1 after printing a
snippet; when no gap wheel fits, stdout is empty.

- For one requirements file or package names on the command line: the `--extra-index-url` line, with pip's
  dependency-confusion caveat, and for each package the `--hash=sha256:` options of its gap wheels (one per wheel)
  to add to its existing line if the requirements pin hashes, keeping the hashes already there. On a line of their
  own pip won't use them: it allows only the hashes all of a package's lines share.
- For one uv.lock: the `[[tool.uv.index]]` (with `explicit = true`) and `[tool.uv.sources]` entries for
  `pyproject.toml`, with a note for each package uv won't take from the index until it's listed as a direct
  dependency. Add them to an existing `[tool.uv.sources]` table rather than a second one, then run `uv lock`.

It refuses, with exit code 2 and before any lookup, more than one input and any other input: `pyproject.toml`, a
script or a script's lock (`example.py.lock`), `pylock.toml`, `poetry.lock`, `pdm.lock`, `Pipfile.lock`, `pixi.lock`, a pip report and an `environment.yml`.
Use `--fix` alone for those. It can't be used with `--explain` (and its add-ons), `--all`, `--from-log`, `--dist`, `--json`, `--markdown`, `--junit`, `--sarif`, `--gitlab-code-quality`, `--badge`,
`--write-constraints` or `--github-annotations`.

```console
wheelgap requirements.txt -p 3.14t -t win-arm64 --fix --print-gap-wheel-snippet
wheelgap uv.lock -p 3.14t -t win-arm64 --fix --print-gap-wheel-snippet
```

### `-q`, `--quiet`, `--no-quiet` {#quiet}

Print only the gap summary and the affected package names. Clean runs without ignored packages are silent, and JSON
and Markdown output are unchanged. `--no-quiet` overrides `quiet = true` in `[tool.wheelgap]`.

### `--json` {#json}

Print JSON instead of the table. See [Output](output.md#json) for the shape. Can't be combined with `--markdown`.

### `--markdown` {#markdown}

Print a Markdown report instead of the table, for a job summary (`wheelgap --markdown >> "$GITHUB_STEP_SUMMARY"`) or
a pull request comment. See [Output](output.md#markdown).

### `--junit` {#junit}

`--junit PATH`: also write a JUnit XML report to `PATH`, alongside the usual output. See
[Output](output.md#junit-xml).

### `--sarif` {#sarif}

`--sarif PATH`: also write a
[SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) report to `PATH`, alongside the usual
output, for GitHub code scanning. It's written even when the check fails, and an empty `PATH` is an error, as for
`--junit`. See [Output](output.md#sarif) and [CI](ci.md#code-scanning).

### `--gitlab-code-quality` {#gitlab-code-quality}

`--gitlab-code-quality PATH`: also write a GitLab Code Quality JSON report to `PATH`, alongside the usual output,
even when the check fails. An empty `PATH` is an error. See [Output](output.md#gitlab-code-quality) and
[GitLab CI](ci.md#gitlab-ci).

### `--badge` {#badge}

`--badge PATH`: also write a [shields.io endpoint](https://shields.io/badges/endpoint-badge) file
to `PATH`, alongside the usual output, with how many of the checked targets have wheels for everything:
`{"schemaVersion": 1, "label": "wheels", "message": "7/8 targets", "color": "yellow"}`. A target counts when nothing on
it is `source` or `missing` (a gap `--ignore` accepts there doesn't count against it; an `--advisory` target's gaps
do). The colour is `brightgreen` when every target counts, `red` when none does and `yellow` in between. If any result
is `unknown`, the message is `unknown` (`lightgrey`) instead of a count. The file is written even when the check fails,
so CI can publish it; an empty `PATH` is an error, as for `--junit`. See [Recipes](recipes.md#readme-badge).

### `--github-annotations` {#github-annotations}

Also mark each package with a gap on its own line of the lockfile in GitHub Actions, as an
annotation. The GitHub Action passes it; see [CI](ci.md#annotations).

With `--deps` on a requirements file, a direct requirement's
gap is on its own line, as without `--deps`. A transitive gap goes on the line
of the requirement that pulls it in when exactly one does on every target where
it has a gap, and its message says `(via …)`. When more than one does, or a
line isn't certain, the annotation names the file without a line, and the
message names them all.

With [`--fix`](#fix) too, the annotation of a package that a newer official release fixes starts
its fix with that (`Upgrade: NAME VERSION has the wheel for TARGETS (an official release); the --fix section of the log
says how.`), and the annotation of a package that a gap wheel fills gets one more sentence (after the upgrade, it
starts `If you can't upgrade:`): the targets the wheel fills, that it's unofficial (built by wheelgap from the PyPI sdist), and the
`pip install --extra-index-url` command for inputs pip installs from as they are, or a pointer to the log's gap wheel
section for a lockfile or a script. The level, title, line and exit code stay the same; see
[CI](ci.md#fix).

With [`--dockerfile`](#dockerfile), a note that the final image wasn't read is a `::notice` at its
`FROM` line, and each line [`--dockerfile-inputs`](#dockerfile-inputs) couldn't see into is a `::warning` at that
line, so a pull request shows what the answer doesn't cover.

### `--color` {#color}

`--color {auto,always,never}`: color the table. `auto` (the default) colors a terminal, and respects the `NO_COLOR` and
`FORCE_COLOR` environment variables.

## Exit status

### `--ignore` {#ignore}

`--ignore NAME[:TARGET]`: accept a known gap. Matched packages are excluded from the table, counts and exit status,
and listed as ignored in text (including quiet mode), JSON and Markdown. Repeatable or comma-separated, with
case-insensitive package-name matching and equivalent hyphens, underscores and dots. Any `--ignore` flags replace the
configured `ignore` list rather than adding to it, and `--ignore ""` clears it for one run.

To accept a gap on one target only, add the target after a colon: `--ignore psycopg2:musl-x86_64` still checks
psycopg2 everywhere else. The target takes anything `-t` does (`alpine`, `linux/amd64`, `all`), one per entry
(`--ignore psycopg2:alpine,psycopg2:win-arm64`). An unknown target is an error (exit 2), and so is a second target
after a comma (`psycopg2:alpine,win-arm64`), which would otherwise read as a package name. The package keeps its real
status in the report (`● source (ignored)` in the table), but a gap on that target doesn't count towards the summary
or the exit status, isn't explained by `--explain`, and is a skipped testcase in `--junit`. Reports list it as
`ignored: psycopg2 (musl-x86_64)`, and `--json` puts it in `ignored_targets` (`ignored` keeps only packages ignored
everywhere). A target that isn't checked in this run gets a note on stderr.

### `--exit-zero` {#exit-zero}

Exit 0 even when gaps are found, for informational runs. Bad input still exits 2.

### `--fail-on` {#fail-on}

`--fail-on {source,missing}`: exit 1 on any gap (`source`, the default), or only when a package can't be installed
(`missing`). A package that couldn't be checked (`unknown`) exits 1 either way.

## Other

### `--list-targets` {#list-targets}

List the platforms, their aliases and compatibility limits (and the `--distro` names), then exit. With `--json`, print
the platforms as JSON. Can't be combined with `--markdown`.

### `--suggest-targets` {#suggest-targets}

`--suggest-targets [DIR]`: read the platforms and Pythons that `DIR` (default: the current directory)
already names in its GitHub Actions workflows, `.gitlab-ci.yml` and Dockerfiles, print each with the file and line it
came from, then a `[tool.wheelgap]` snippet to paste into `pyproject.toml`. It only suggests: nothing is changed or
checked, and it exits 0. With `--json`, it prints the same as an object. It can't be combined with `-p`, `-t`,
`--dockerfile`, `--build-arg`, `--list-targets`, `--markdown` or input files, and a `DIR` that isn't a folder exits 2.
What it can't read for sure is listed with its file and line rather than guessed. See
[Suggest targets from your CI and Dockerfiles](targets.md#suggest-targets).

### `-V`, `--version` {#version}

Print wheelgap's version, with the versions of `packaging` and of the Python it runs on, for bug reports.

### `-h`, `--help` {#help}

Show the flags and some examples, then exit.

## Subcommands

### `wheelgap diff` {#diff}

Compare two lockfiles, or two pinned PyPI package versions with `--deps`: which packages got worse on a target,
and which new packages have gaps. It takes `-p`, `-t`, `--json`, `--glibc`, `--musl`, `--distro`, `--macos` and `--deps`.
See [Comparing two lockfiles](diff.md).

### `wheelgap mcp` {#mcp}

Start the MCP server, as `wheelgap-mcp` does. See [MCP server](mcp.md).

## Other commands

### `python -m wheelgap.scan` {#scan}

Not a `wheelgap` subcommand: a module that checks the latest release of the most-downloaded PyPI packages, the data
behind the [coverage tracker](https://wheelgap.github.io/tracker/). See
[How it works](how-it-works.md#scanning-the-top-pypi-packages).

## Environment variables

| variable | effect |
| --- | --- |
| `WHEELGAP_INDEX_URL` | the PyPI JSON API to use instead of `https://pypi.org/pypi`, such as a mirror's; see [Pipfile.lock](inputs.md#pipfilelock) |
| `NO_COLOR` | no color with `--color auto` |
| `FORCE_COLOR` | color with `--color auto`, even when stdout isn't a terminal |
| `GITHUB_WORKSPACE` | with `--github-annotations`, annotation paths are relative to it |
