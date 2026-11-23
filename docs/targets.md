# Targets

A target is a Python version and a platform, such as Python 3.13 on `musl-aarch64`. wheelgap checks every Python you
pass with `-p` on every platform you pass with `-t`.

```bash
wheelgap -p 3.13,3.14t -t linux-x86_64,linux-aarch64,musl-aarch64
```

Run `wheelgap --list-targets` to see the names, aliases and compatibility limits, or add `--json` for a
machine-readable list.

## Platforms

| name | aliases | wheel tags it accepts |
| --- | --- | --- |
| `linux-x86_64` | `linux`, `linux-amd64`, `linux/amd64` | `manylinux_2_*_x86_64` up to `--glibc` (default 2.36), legacy `manylinux2014`/`2010`/`1` |
| `linux-aarch64` | `linux-arm64`, `linux/arm64`, `linux/arm64/v8` | `manylinux_2_*_aarch64` from 2.17 up to `--glibc`, legacy `manylinux2014` |
| `musl-x86_64` | `alpine`, `musl` | `musllinux_1_*_x86_64` up to `--musl` (default 1.2), plain `linux_x86_64` (after 0.13.0) |
| `musl-aarch64` | `alpine-arm64` | `musllinux_1_*_aarch64`, plain `linux_aarch64` (after 0.13.0) |
| `macos-arm64` | `macos`, `mac` | `macosx_*_arm64` and `universal2` up to `--macos` (default 14.0) |
| `macos-x86_64` | `macos-intel` | `macosx_*_x86_64` and `universal2` |
| `win-amd64` | `windows`, `win` | `win_amd64` |
| `win-arm64` | `windows-arm64` | `win_arm64` |
| `linux-armv7l` | `linux-arm`, `linux-armv7`, `linux/arm/v7` | `manylinux_2_*_armv7l` from 2.17 (32-bit ARM, e.g. Raspberry Pi OS), legacy `manylinux2014`, and plain `linux_armv7l` (which PyPI allows for the Pi) |
| `linux-ppc64le`, `linux-s390x`, `linux-riscv64` | `linux/ppc64le`, `linux/s390x`, `linux/riscv64` | `manylinux_2_*` from 2.17 for that arch (`riscv64` has no legacy `manylinux2014` tag) |
| `linux-i686` | `linux-x86`, `linux-386`, `linux/386` | `manylinux_2_*_i686` up to `--glibc`, legacy `manylinux2014`/`2010`/`1` (32-bit x86) |
| `win32` | `windows-x86`, `win-x86` | `win32` (32-bit Python on Windows) |
| `musl-armv7l`, `musl-ppc64le`, `musl-s390x`, `musl-riscv64`, `musl-i686` | `alpine-armv7`, `alpine-x86` | `musllinux_1_*` for that arch, and plain `linux_<arch>` (after 0.13.0) |
| `pyodide` | `wasm`, `wasm32`, `emscripten` | Pyodide's ABI for that Python: `emscripten_3_1_32` (3.11), `pyodide_2024_0` / `pyemscripten_2024_0` (3.12), `pyodide_2025_0` / `pyemscripten_2025_0` (3.13), `pyemscripten_2026_0` (3.14) |
| `ios-arm64` | `ios`, `iphoneos` | `ios_*_arm64_iphoneos` up to `--ios` (default 13.0) |
| `ios-simulator-arm64`, `ios-simulator-x86_64` | `ios-simulator` | `ios_*_arm64_iphonesimulator` / `ios_*_x86_64_iphonesimulator` |
| `android-arm64`, `android-x86_64` | `android` | `android_*_arm64_v8a` / `android_*_x86_64` up to `--android-api` (default 24) |

`--list-targets` shows a few more aliases than this table, such as `manylinux-x86_64` and `musllinux-aarch64`.

Every Linux target, glibc (`linux-*`) and musl (`musl-*`, after 0.13.0) alike, also accepts a plain `linux_<arch>`
wheel, as pip and uv do. PyPI only allows those for 32-bit ARM, so in practice they matter for `linux-armv7l` and for
wheels from other indexes, such as PyTorch's `torch-1.10.2+cpu-cp39-cp39-linux_x86_64.whl`. Such a wheel is usually
built against glibc: pip and uv install it on Alpine all the same, and whether it then runs there is another question
wheelgap doesn't answer.

Docker platform names also work with `-t`, for example `-t linux/amd64,linux/arm64` (also `linux/arm64/v8`,
`linux/arm/v7`, `linux/ppc64le`, `linux/s390x`, `linux/riscv64` and `linux/386`, also spelled `linux/i386`).

### Groups

- `-t all` checks the eight common native platforms: `linux-x86_64`, `linux-aarch64`, `musl-x86_64`, `musl-aarch64`,
  `macos-arm64`, `macos-x86_64`, `win-amd64` and `win-arm64`.
- `-t everything` adds the armv7l, ppc64le, s390x, riscv64 and i686 Linux targets (glibc and musl) and `win32`.
- `-t mobile` is the iOS and Android targets.

Pyodide, iOS and Android are only checked when you name them.

## Floors: glibc, musl, macOS, iOS and Android

A wheel built for a newer system than your target's can't be used there, so each family of platforms has a version:

| flag | config key | default | applies to |
| --- | --- | --- | --- |
| `--glibc X.Y` | `glibc` | 2.36 | `linux-*` targets |
| `--musl X.Y` | `musl` | 1.2 | `musl-*` targets |
| `--macos X.Y` | `macos` | 14.0 | `macos-*` targets |
| `--ios X.Y` | `ios` | 13.0 | `ios-*` targets |
| `--android-api N` | `android-api` | 24 | `android-*` targets |

Only x86 Linux takes `manylinux` wheels older than glibc 2.17: every other architecture's first manylinux was
`manylinux2014` (glibc 2.17), so pip accepts nothing older there, and neither does wheelgap.

### Distros

If you know the deployment distro rather than its libc version, use `--distro`:

```bash
wheelgap uv.lock -p 3.13 -t linux-aarch64 --distro ubuntu-22.04
wheelgap uv.lock -p 3.13 -t musl-aarch64 --distro alpine
```

It supplies a glibc or musl version only: it does **not** select Python versions or platforms. Keep using `-t` for
those platforms, including `musl-*` for Alpine. You can also set `distro = "ubuntu-22.04"` in `[tool.wheelgap]`.

| distro names | libc version | package sources |
| --- | --- | --- |
| `ubuntu-20.04`, `ubuntu-22.04`, `ubuntu-24.04` | glibc 2.31, 2.35, 2.39 | [Focal](https://launchpad.net/ubuntu/focal/+source/glibc), [Jammy](https://packages.ubuntu.com/jammy/libc6), [Noble](https://packages.ubuntu.com/noble/libc6) |
| `debian-11` / `bullseye`, `debian-12` / `bookworm`, `debian-13` / `trixie` | glibc 2.31, 2.36, 2.41 | [Bullseye](https://packages.debian.org/bullseye/libc6), [Bookworm](https://packages.debian.org/bookworm/libc6), [Trixie](https://packages.debian.org/trixie/libc6) |
| `rhel-8` / `rocky-8` / `alma-8`, `rhel-9` / `rocky-9` / `alma-9` | glibc 2.28, 2.34 | [RHEL 8](https://access.redhat.com/errata/RHSA-2021:1585), [RHEL 9](https://access.redhat.com/errata/RHBA-2022:3917) |
| `amazonlinux-2`, `amazonlinux-2023` | glibc 2.26, 2.34 | [Amazon Linux toolchain packages](https://docs.aws.amazon.com/linux/al2023/ug/glibc-gcc-and-binutils.html) |
| `alpine` | musl 1.2 (Alpine 3.13 and later) | [Alpine musl](https://pkgs.alpinelinux.org/package/v3.22/main/x86_64/musl) |

For each libc, an explicit `--glibc` / `--musl` wins, then `--distro`, then a version from `--dockerfile`, then a
`glibc` / `musl` config value, then the config `distro`, then the built-in default. A distro only supplies its own
libc; the other libc keeps its existing default. Names are case-insensitive; unknown names are errors. For older
Alpine, pass `--musl 1.1` explicitly. The text `--list-targets` output also lists distro names; its JSON output
remains a list of platforms.

## Python versions

`-p` takes versions such as `3.13`, repeated or comma-separated. A trailing `t` means a free-threaded build
(`3.14t`), which correctly rejects `abi3` wheels. A `t` on a version before 3.13 (`3.12t`) is an input
error (exit 2), because free-threading starts at Python 3.13.

Without `-p`, the Python comes from `--dockerfile`, then `python` in `[tool.wheelgap]`, then a `.python-version` file
next to the lockfile (or `pyproject.toml`), and otherwise the Python wheelgap is running on.

### Free-threading and new Python readiness

A new CPython comes out each October, and for months many packages have no wheels for it yet. To see what would
build from source if you upgraded, check the Python you use now next to the new one. `-p next` is the newest CPython
this wheelgap release knows (3.15), and `-p next-t` is its free-threaded build, so you don't have to look the version
up; a note on stderr says which version it is (`-q` drops it). Upgrade wheelgap to move `next` on. `next` works
anywhere a Python version does: `-p`, `python = ["3.14", "next"]` in `[tool.wheelgap]`, `wheelgap diff -p`,
`make_targets(python="next")` and the MCP tools' `python` argument, but not in `.python-version`, which uv and pyenv
read too.

```bash
wheelgap uv.lock -p 3.14 -p next -t linux-x86_64,linux-aarch64
wheelgap uv.lock -p 3.14 -p next-t -t all   # the upgrade and free-threading together
```

When exactly two Python versions are checked and one is newer, the summary names the packages that block the
upgrade: those with a gap (`● source` or `✗ missing`) on the newer Python on a platform where the older one has a
wheel (or doesn't install the package). Gaps both Pythons share, and targets ignored with `--ignore NAME:TARGET`,
don't count. Packages that couldn't be checked aren't listed, only counted
(`blocking 3.15: none known (1 package couldn't be checked)`), so an empty list isn't a pass on its own: the summary
and the exit code still decide.

```text
3 of 41 packages have gaps: 3 will build from source.
blocking 3.15: numpy, pydantic-core
```

`--markdown` and `-q` print the same line, and `--json` has the list as `blocking_newer_python`. Two builds of the
same version, such as `-p 3.14 -p 3.14t`, have no newer one, so they get no such line; the
[free-threading recipe](recipes.md#free-threading-readiness) lists a free-threaded build's gaps for scripts.

## How targets are chosen

Target platforms come from the first of these that gives any:

1. `-t` on the command line;
2. the final stage of the Dockerfile given with `--dockerfile`;
3. `targets` in `[tool.wheelgap]`;
4. uv's `[tool.uv] required-environments` in the project's `pyproject.toml`;
5. the built-in defaults: `linux-x86_64`, `linux-aarch64`, `macos-arm64` and `win-amd64`.

Not sure what to put in `-t` or `[tool.wheelgap]`?
[`--suggest-targets`](#suggest-targets) reads the platforms your CI and Dockerfiles already name and suggests them.

### uv's required-environments

For uv projects, wheelgap reads `[tool.uv] required-environments` as default targets:

```toml
[tool.uv]
required-environments = ["sys_platform == 'linux' and platform_machine == 'aarch64'"]
```

Each marker is evaluated against the eight common native platforms using the selected Python versions. A platform is
included if any marker matches for any selected Python, and the resulting platforms are checked for all selected
Pythons. Linux markers select both glibc and musl targets, since markers cannot distinguish the libc; the example
above selects `linux-aarch64` and `musl-aarch64`. Use `-t` to choose one explicitly. An inferred selection is
explained on stderr, including with `--json` or `--markdown`.

The inferred targets replace the built-in defaults, so a project that only requires Intel macOS wheels is then
checked for Intel macOS only: add `[tool.wheelgap] targets` or `-t` to check more. An absent or empty setting keeps
the built-in defaults, and so does one matching none of the targets above (with a note). An invalid marker is an
error. Only `required-environments` is used here: `environments`, extra Linux architectures, mobile and Pyodide
targets are not inferred.

`[tool.uv] environments` doesn't pick targets, but it limits them (after 0.13.0): a `uv.lock` made with it covers
those environments only, so the built-in defaults are narrowed to the ones it covers and a target you ask for outside
them is an error. See [uv.lock](inputs.md#uvlock).

### From a Dockerfile

`--dockerfile PATH` takes the Python, the platform and the glibc or musl version from the image of the Dockerfile's
final stage, such as `python:3.13-alpine` or `ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim`, following named
stages like `FROM base AS runtime` back to the image they start from. `-p`, `-t`, `--glibc` and `--musl` on the
command line still win, and the image wins over `[tool.wheelgap]`. After 0.13.0, `-p` and `-t` together are also
enough for an image wheelgap can't read (`FROM ubuntu:24.04`, `nvidia/cuda`, a build argument with no value): the
run checks the targets given, with a note on [what that leaves unknown](cli.md#dockerfile).

A python X.Y tag without a Debian release (`python:3.13-slim`) is read as the release Docker Hub currently builds it
on (trixie, glibc 2.41, for 3.9 and later); patch tags like `3.12.7-slim` keep the `--glibc` default. A pre-release
tag like `3.11-rc-slim` stopped being rebuilt when its Python was released, so it's read as the release it was last
built on (bullseye for 3.10 and 3.11, bookworm for 3.12 and 3.13, trixie for 3.14 and 3.15); one still being built
(`3.16-rc-slim`) keeps the `--glibc` default, and one older than 3.10 is an error. Pin `-bookworm` or `-trixie` for an
answer that doesn't move. A Dockerfile wheelgap can't read the way Docker does is an error, not a guess.

A tag that names a patch release (`python:3.9.1-slim`, `python:3.12.7`) is checked as its minor version, at that
version's newest patch release, with (after 0.13.0) a note that says so: see
[which patch release is checked](how-it-works.md#patch-release).

A global `ARG` used in the final stage's `FROM` (`ARG PYTHON_VERSION=3.12` then `FROM python:${PYTHON_VERSION}-slim`)
is read from its default, with a note. When the build picks the value
(`docker build --build-arg PYTHON_VERSION=3.13 .`), pass the same [`--build-arg`](cli.md#build-arg): it replaces the
default, and gives a value to an ARG with none, so the platform needn't be spelled out again with `-t`. The note says
which values came from `--build-arg` and which were defaults.

## Suggest targets from your CI and Dockerfiles {#suggest-targets}

`wheelgap --suggest-targets [DIR]` (default: the current directory) reads the platforms and Pythons the project already
names, and prints each suggestion with the file and line it came from, then a `[tool.wheelgap]` snippet to paste into
`pyproject.toml`. It only suggests: nothing is changed or checked, and it exits 0. It can't be combined with `-p`, `-t`,
`--dockerfile` or `--list-targets`, and a `DIR` that isn't a folder exits 2.

```text
$ wheelgap --suggest-targets
Targets:
  linux-x86_64
    .github/workflows/wheels.yml:5  runs-on: ubuntu-24.04
  linux-aarch64
    .github/workflows/test.yml:9  runs-on: ubuntu-24.04-arm (matrix.os, line 5)
  musl-x86_64
    Dockerfile:4  FROM python:3.13-alpine (no --platform: read as linux/amd64, musl 1.2)
  win-amd64
    .github/workflows/test.yml:9  runs-on: windows-latest (matrix.os, line 5)
Pythons:
  3.13
    .github/workflows/test.yml:8  python-version: 3.13 (matrix.python-version, line 14)
    Dockerfile:4                  FROM python:3.13-alpine
  3.14
    .github/workflows/test.yml:8  python-version: 3.14 (matrix.python-version, line 14)
Couldn't read (not guessed):
  .github/workflows/wheels.yml:12  job build: the matrix is computed (${{ fromJSON(needs.plan.outputs.matrix) }})

Suggested settings for pyproject.toml (nothing was changed or checked). They combine every job, lint,
release and bot jobs too, so keep only the platforms you ship to:

[tool.wheelgap]
python = ["3.13", "3.14"]
targets = ["linux-x86_64", "linux-aarch64", "musl-x86_64", "win-amd64"]
glibc = "2.39"
musl = "1.2"
```

The snippet combines every job in every workflow, including lint, release and bot jobs (the `plan` job above adds
`linux-x86_64`), so check it before pasting and keep only the platforms you ship to.

- **GitHub Actions** (`.github/workflows/*.yml` and `*.yaml`):
    - Each job's `runs-on`, as a single label or `${{ matrix.KEY }}` with a literal matrix. The matrix can use lists and
      `include` and `exclude` entries, which are combined as GitHub does.
    - The `python-version` given to `actions/setup-python` or `astral-sh/setup-uv`, as a literal (`"3.13"`, `3.13.1`,
      `3.14t`) or from the matrix.
    - setup-python's `freethreaded: true`, which adds the `t` (free-threading starts at 3.13, so `3.12t` is refused).
    - setup-python's `architecture`, where it's certain: `x86` on x64 Windows is `win32`, and `x64` on Apple silicon is
      `macos-x86_64` (Rosetta). It can come from a literal matrix, which pairs each runner with its own architecture.
    - A job's `container:`, as a literal image string or a mapping (block or `{ image: ... }`) with only an `image:`
      key. The image is read exactly as `--dockerfile` reads a `FROM`, so `container: python:3.13-alpine` on
      `ubuntu-latest` suggests `musl-x86_64` and Python 3.13 (and `musl = "1.2"`) rather than the runner's target.
      Actions pulls the image for the runner's CPU without forcing a platform, so the same image on
      `ubuntu-24.04-arm` suggests `musl-aarch64` instead; a runner label that can't be resolved (a matrix, an
      expression or an unknown label), a non-Linux runner (job containers only run on Linux), or a Windows container
      image isn't guessed. A step that picks the job's Python (`setup-python`, or `setup-uv` with a
      `python-version`) installs it into the image, so that Python is suggested instead of the image's own and the
      image's libc still applies. `setup-python` has no musl builds, so in an Alpine image it's refused, and so is
      `ubuntu-slim`, which runs in a container itself. Expressions, an `options:` that may
      change the platform, `services:` (at the job level beside `container:`), and any other container shape are
      listed under "Couldn't read".
    - Input names are case-insensitive, as Actions reads them (`Architecture: x86`). Step keys such as `uses` and `with`
      aren't.
- **GitLab CI** (`.gitlab-ci.yml` at the project root):
    - Each job's literal `image:` (a string, or a mapping with only `name:`), or else `default: image:` or the
      top-level `image:`. The image is read as a Dockerfile's `FROM` is, for the CPU of the job's runner: a known
      [GitLab.com hosted runner](https://docs.gitlab.com/ci/runners/hosted_runners/) tag in the job's `tags:` (or
      `default: tags:`), so `python:3.13-alpine` with `tags: [saas-linux-small-arm64]` suggests `musl-aarch64` and
      Python 3.13.
    - A job without tags is read as running on GitLab.com's default runner, `saas-linux-small-amd64`, and its source
      line says so. On a self-managed GitLab instance an untagged job can run on any runner, so check those.
    - The Linux small/medium/large amd64 and arm64 tags, xlarge and 2xlarge amd64, `saas-windows-medium-amd64`,
      `saas-macos-medium-m1` and `saas-macos-large-m2pro` are recognized. A recognized tag with no image names only
      a target, not a Python.
    - On the macOS runners `image:` names the macOS VM rather than a container: `macos-15-xcode-16` suggests
      `macos-arm64` and `macos = "15.0"`. Any other image there (including one from `default:`) is refused, and so is
      any `image:` on the Windows runner, which doesn't use one.
    - A job with an unknown or self-hosted tag, more than one tag, an image with a variable, an image mapping with
      other keys, `extends:`, `inherit:`, `trigger:` or `parallel: matrix:` is listed under "Couldn't read"; the
      other jobs are still read. `include:`, YAML anchors and aliases, `!reference` and multi-document files make
      the whole file unreadable.
- **Runner labels.** [GitHub-hosted runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
  labels map to targets as follows. Other Ubuntu versions (`ubuntu-22.04`, `ubuntu-22.04-arm`) and Windows images
  (`windows-2022`, `windows-2025-vs2026`) map the same way.
    - `ubuntu-latest`, `ubuntu-slim` and `ubuntu-24.04` → `linux-x86_64`.
    - `ubuntu-24.04-arm` → `linux-aarch64`.
    - `windows-latest` and `windows-2025` → `win-amd64`.
    - `windows-11-arm` → `win-arm64`.
    - `macos-latest`, `macos-14` and later, `xcode-*` and the `-xlarge` runners → `macos-arm64`.
    - `macos-13`, `macos-15-intel` and the `-large` runners → `macos-x86_64`.
- **Container images**. A job's literal `container:` image names its own libc and Python, read as a Dockerfile's
  `FROM` is, so the runner's target isn't read for that job; its CPU is, since Actions pulls the image's build for
  that CPU (an ARM runner gets `musl-aarch64` from `python:3.13-alpine`). A setup step that picks a Python installs it
  into the image, so the image's libc applies to that Python, which is the job's instead of the image's own.
- **Runner versions.** A pinned label also names its glibc (`ubuntu-22.04` has 2.35, `ubuntu-24.04` 2.39) or its macOS
  version (`macos-14` is 14.0). A `-latest` label moves, so it names neither. `ubuntu-26.04` names none yet: GitHub's
  runner-images readme doesn't give its glibc.
- **Dockerfiles.** Every `Dockerfile` and `Containerfile`, `Dockerfile.x`, `Dockerfile-x`, `Dockerfile_x` (also
  `dockerfile.dev`), the same for `Containerfile`, and `*.Dockerfile` and `*.Containerfile` is read (names are matched
  without regard to case, and ones ending in `.md`, `.py`, `.yml` and the like aren't Dockerfiles). Hidden folders,
  virtual environments, `node_modules`, `__pycache__` and `site-packages`, and `tests`, `test`, `fixtures`, `examples`,
  `example`, `docs`, `build`, `dist` and `target` folders are skipped. The final stage's image is read exactly as `--dockerfile`
  reads it, for the target, the Python and the glibc or musl version. Without `--platform` it's read as `linux/amd64`,
  and the output says so.

The snippet sets `glibc`, `musl` or `macos` only when every source of those targets names the same version. When
sources name different versions, or only some name one, a `#` line says why it's left out. It also leaves out `python` when no Python was found. `--json` prints the same as an
object with `targets`, `python` (each with `file`, `line` and `from`), `unreadable` and `config`.

There's no YAML parser in the standard library, so a workflow is read only when every line is in a small subset of YAML
whose meaning is certain. Otherwise the whole file is listed under "Couldn't read: uses YAML this scanner doesn't read",
with the first line that isn't. The subset is:

- **Lines.** Blank lines and comments, `key:` or `key: VALUE` (letters, digits, `_`, `.` and `-`, or a whole quoted key
  without escapes), and list items (`- VALUE`, `- key: VALUE`). A leading `---` (after an optional `%YAML 1.2`) is
  allowed too.
- **Values.** A VALUE is one of:
    - a plain scalar on one line;
    - a whole single- or double-quoted scalar on one line;
    - a one-line flow list or mapping of such scalars, with a space after each `:`;
    - a block scalar header (`|`, `>-`...);
    - a flow list over several lines, as Prettier writes it, with `[` ending its line (or alone on the next), one item
      per line, and `]` on its own line;
    - a flat flow mapping over several lines, with `{` ending the opening line, one `key: scalar` per line,
      commas between entries, and `}` on its own line. Such a mapping can also be an item of a multi-line flow list,
      closed by `}` or `},`.
- **Anchors.** An anchor on a block (`with: &w`) is accepted and read as usual.
- **Accepted but never read.** Aliases (`*w`), double-quoted values with escapes (outside flow collections), single-
  or double-quoted values continued on more-indented lines, and plain values continued on more-indented lines (long
  `if:` and `run:` lines) are accepted where they aren't needed, and refused where wheelgap would read them.

Everything else isn't read: control characters (except tabs) in quoted scalars, invalid backslash escapes in
double-quoted ones, `''` in single-quoted ones, escapes inside flow collections, anchors or tags inside a value, an
alias with no anchor before it, a key or anchor used twice, lines indented under a value (including `key: {...}`), `? `
keys, `<<` merge keys, a second document, flow collections in other layouts, and line breaks other than `\n` and
`\r\n`. Non-ASCII spaces such as NBSP are ordinary characters, as they are to YAML.

In a file that is read, anything wheelgap can't be sure of is still listed under "Couldn't read" with its file and line,
and never guessed:

- expressions and computed matrices, and a `freethreaded` taken from the matrix;
- a matrix that might have no combinations (an empty list, or an `exclude` that can't be worked out): GitHub would run
  nothing, so the job isn't read;
- a job with a setup step whose `uses` can't be read, which sets an input twice, or which picks the Python under an
  `if:` (the step may not run);
- self-hosted and other unknown runner labels;
- a `container:` that isn't a single literal image (an expression, an `options:` that may change the platform, or a
  mapping with any other key), or a job-level `services:` beside it;
- an unquoted `3.10` (YAML reads it as the number 3.1) or `0x10`;
- `python-version-file`;
- Dockerfile templates (`Dockerfile.j2`, `Dockerfile.template`), files or folders it can't open, and Dockerfiles
  `--dockerfile` would reject.

Setup steps inside your own composite actions aren't read.

## Advisory targets

To watch a platform before you support it, such as Windows on ARM, make it advisory:
`-t linux-x86_64,win-arm64 --advisory win-arm64` (or `advisory = ["win-arm64"]` in `[tool.wheelgap]`). Its gaps are
reported as usual but never fail the run. [`--advisory`](cli.md#advisory) has the details.

## Pyodide

Pyodide (Python in the browser) only installs wheels: there's no compiler, so a package with only an sdist shows as
`✗ missing`, not `● source`. Pyodide wheels are tied to one ABI per Python version and don't work across them.

Pyodide also ships its own builds of many popular packages (numpy, pydantic-core and others) that aren't on PyPI.
Point `--pyodide-lock` at the release's `pyodide-lock.json` (a path or `https://` URL, also `pyodide-lock = "..."` in
`[tool.wheelgap]`) and those count as `✓ bundled`, with a note when Pyodide's version differs from the one you pinned
(shown with `-q` too, and `"bundled_version_differs": true` in `--json`). That still passes: `pyodide.loadPackage`
gives you Pyodide's version, though `micropip.install` of your exact pin would fail.

```bash
wheelgap -p 3.12 -t pyodide --pyodide-lock https://cdn.jsdelivr.net/pyodide/v0.27.7/full/pyodide-lock.json
```

## iOS and Android

iOS and Android (PEP 730 and PEP 738, Python 3.13+) are checked with `-t mobile` or by name, for apps built with tools
like BeeWare's Briefcase. Mobile dependencies are installed by a cross-platform pip run that can only use wheels, so a
package with only an sdist shows as `✗ missing`. `--ios` and `--android-api` (or `ios = "15.0"` and
`android-api = 28` in `[tool.wheelgap]`) set your app's minimum iOS version and Android API level.

wheelgap checks PyPI only: Briefcase and Chaquopy also install from their own package repositories (BeeWare's on
anaconda.org, and chaquo.com), which have mobile builds of packages like numpy, so a `✗ missing` there may still
install.

## Environment markers on unusual platforms

On `win32`, `linux-i686`, `musl-i686`, `linux-armv7l` and `musl-armv7l` a `platform_machine` comparison counts as
unknown, so the package is still checked. `platform_machine` reports the machine, not the Python: 32-bit Python on
64-bit Windows says `AMD64`, a `linux/386` container on an x86_64 host says `x86_64`, and 32-bit Raspberry Pi OS on a
Pi 4 or 5 (which boots a 64-bit kernel) or a `linux/arm/v7` container on an arm64 host can say `aarch64`.
