<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/wheelgap/wheelgap/main/docs/logo/wheelgap-lockup-dark.png">
    <img alt="wheelgap" src="https://raw.githubusercontent.com/wheelgap/wheelgap/main/docs/logo/wheelgap-lockup.png" height="72">
  </picture>
</h1>

[![CI](https://github.com/wheelgap/wheelgap/actions/workflows/ci.yml/badge.svg)](https://github.com/wheelgap/wheelgap/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wheelgap)](https://pypi.org/project/wheelgap/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wheelgap/wheelgap/blob/main/LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
[![good first issues](https://img.shields.io/github/issues/wheelgap/wheelgap/good%20first%20issue?label=good%20first%20issues&color=7057ff)](https://github.com/wheelgap/wheelgap/labels/good%20first%20issue)

**Find the Python dependencies that have no prebuilt wheel for your target platform, before your Docker build does.**

Point it at your lockfile and the platforms you ship to (ARM servers, Alpine images, Windows on ARM, free-threaded Python 3.14t). It tells you which packages pip or uv will have to compile from source, and which can't be installed at all.

📊 **New: [wheel coverage for the top 5,000 PyPI packages](https://wheelgap.github.io/tracker/)**, by platform and Python version, updated nightly.

![wheelgap demo](https://raw.githubusercontent.com/wheelgap/wheelgap/main/docs/demo.gif)

📖 **Documentation: [wheelgap.github.io/wheelgap](https://wheelgap.github.io/wheelgap/)**, with a reference for every [flag](https://wheelgap.github.io/wheelgap/cli/), [input format](https://wheelgap.github.io/wheelgap/inputs/), [config key](https://wheelgap.github.io/wheelgap/configuration/) and [output format](https://wheelgap.github.io/wheelgap/output/).

## Install and run (under 30 seconds)

With [uv](https://docs.astral.sh/uv/):

```bash
uvx wheelgap
```

Or with pipx:

```bash
pipx run wheelgap
```

Or install it: `pip install wheelgap`.

Run either one in a project directory. It finds `uv.lock`, `pylock.toml`, `poetry.lock`, `pdm.lock`, `Pipfile.lock`, `requirements.txt` or `requirements.lock` on its own (and, after 0.9.0, `pixi.lock`). [Getting started](https://wheelgap.github.io/wheelgap/getting-started/) walks through a first run.

## Usage

```bash
# Will my Alpine ARM image compile anything?
wheelgap -p 3.13 -t alpine-arm64

# Is my stack ready for free-threaded Python 3.14?
wheelgap -p 3.14t -t all

# Several Pythons at once, Linux on both architectures
wheelgap uv.lock -p 3.12,3.13,3.14 -t linux-x86_64,linux-aarch64

# Resolve a version range separately for each Python
wheelgap "numpy>=1.26,<3" -p 3.9,3.13 -t linux

# Check the PyPI packages of a pixi project's `test` environment (after 0.9.0)
wheelgap pixi.lock --environment test -p 3.13 -t linux-x86_64,macos-arm64

# No lockfile? Check packages directly (latest version, or pin one)
wheelgap psycopg2 uvloop numpy==2.3.4 -t windows,win-arm64

# Take the Python and platform from the final stage of a Dockerfile
wheelgap --dockerfile Dockerfile

# Machine-readable, for scripts and CI
wheelgap --json > wheelgap.json

# Why is it a gap? List each rejected wheel and the reason
wheelgap -t alpine-arm64 --explain
```

Write your defaults down once in `pyproject.toml` ([every key](https://wheelgap.github.io/wheelgap/configuration/)); command-line flags still win:

```toml
[tool.wheelgap]
python = ["3.13", "3.14t"]
targets = ["linux-x86_64", "linux-aarch64", "musl-aarch64"]
```

Not sure what to put there? After 0.9.0, `wheelgap --suggest-targets` reads the platforms your CI and Dockerfiles already name and suggests these lines ([how](https://wheelgap.github.io/wheelgap/targets/#suggest-targets)).

Each cell in the table is one of:

| cell | meaning |
| --- | --- |
| `✓` | a compatible wheel exists, so the install is fast |
| `● source` | no wheel, only an sdist, so it compiles at install time (needs a compiler, headers, time) |
| `✗ missing` | no wheel and no sdist, so the install fails |
| `–` | not installed on this target (environment markers exclude it, or the target selects another resolution) |
| `✓ bundled` | no wheel on PyPI, but Pyodide ships its own build (with `--pyodide-lock`) |

**Exit codes:** `0` no gaps, `1` gaps found, `2` bad input. `--fail-on missing` accepts source builds, `--ignore NAME` accepts a known gap, and `--exit-zero` never fails on gaps.

In `--json` output, `results` (and `rejected` and `via` with `--explain`) are keyed by target label: just the platform (`"linux-x86_64"`) when one Python is checked, or Python and platform (`"3.13 linux-x86_64"`) with several. The top-level `targets` list gives each target's `label`, `python` and `platform`, so a script can look the key up instead of building it:

```python
import json

data = json.load(open("wheelgap.json"))
key = next(t["label"] for t in data["targets"] if t["python"] == "3.13" and t["platform"] == "linux-x86_64")
```

Version ranges select the newest matching release whose `Requires-Python` permits each target Python. Different
resolutions appear in separate rows, including in JSON, with `n/a` for targets using another resolution. See
[version ranges](https://wheelgap.github.io/wheelgap/how-it-works/#version-ranges) for details.

After 0.9.0, `--explain` can also show the coverage tracker's notes (`--notes`), what a source build needs (`--build-info`), conda-forge builds that fit (`--conda-forge`) and the nearest newer release with wheels for a gap (`--suggest`). These are opt-in, since they send package names elsewhere ([which names](https://wheelgap.github.io/wheelgap/how-it-works/#pypi-origin)).

The docs cover [inputs and dependency selection](https://wheelgap.github.io/wheelgap/inputs/), [`--explain`](https://wheelgap.github.io/wheelgap/output/#explain), [the `--json` schema](https://wheelgap.github.io/wheelgap/output/#json), [every flag](https://wheelgap.github.io/wheelgap/cli/) and the [recipes](https://wheelgap.github.io/wheelgap/recipes/) for multi-arch images, monorepos and free-threading readiness.

## Targets

`-t all` checks the eight common native platforms (`linux-x86_64`, `linux-aarch64`, `musl-x86_64`, `musl-aarch64`, `macos-arm64`, `macos-x86_64`, `win-amd64`, `win-arm64`), and `-t everything` adds the armv7l, ppc64le, s390x, riscv64 and i686 Linux targets (glibc and musl) and `win32`. Pyodide (`-t pyodide`), iOS and Android (`-t mobile`) are checked when you name them. Aliases (`alpine`, `windows`) and Docker names (`linux/arm64`) work too, and `wheelgap --list-targets` lists them all.

Python versions take a trailing `t` for free-threaded builds (`3.14t`). `--glibc`, `--musl` and `--macos` set the oldest system you deploy to, or `--distro ubuntu-22.04` takes it from a distro. The [targets page](https://wheelgap.github.io/wheelgap/targets/) has the wheel tags each target accepts, the floors, and new-Python readiness.

## In CI (GitHub Action)

```yaml
- uses: actions/checkout@v7
- uses: wheelgap/wheelgap@v0.9.0
  with:
    args: uv.lock -p 3.13 -t linux-x86_64,linux-aarch64
```

The job fails when a dependency would build from source on a target you ship, for example when a dependency bump drops ARM wheels. The gap table also goes to the run's summary page (set `summary: false` to turn that off). After 0.9.0, `compare-to: base` fails only when a pull request makes the lockfile worse, and `comment: true` posts the result on the pull request ([details and permissions](https://wheelgap.github.io/wheelgap/ci/#compare-to)). [CI](https://wheelgap.github.io/wheelgap/ci/) covers the Action, `--markdown`, `--junit` and GitLab.

## With pre-commit

```yaml
repos:
  - repo: https://github.com/wheelgap/wheelgap
    rev: v0.9.0
    hooks:
      - id: wheelgap
```

The hook runs when a lockfile or requirements file changes. Put your targets in `[tool.wheelgap]` in `pyproject.toml` so the hook checks the right platforms.

## From Python

```python
import wheelgap

targets = wheelgap.make_targets(python="3.13", platforms=["linux-aarch64", "musl-aarch64"])

result = wheelgap.check_package("numpy", "2.3.4", targets)  # looks the files up on PyPI
result.ok  # True if every target has a wheel
result.gaps  # the targets that don't

for r in wheelgap.check_lockfile("uv.lock", targets):  # markers and the uv graph respected
    print(r.name, r.version, [t.platform for t in r.gaps])
```

The [Python API](https://wheelgap.github.io/wheelgap/python-api/) page has every function and result attribute.

## With an AI coding agent

wheelgap ships a **`check-wheels` skill** that teaches coding agents to run it, read its JSON and suggest fixes. It follows the open [Agent Skills](https://agentskills.io/) format.

**Claude Code** (install from this repository as a plugin):

```text
/plugin marketplace add wheelgap/wheelgap
/plugin install wheelgap@wheelgap
```

**Codex, Cursor, GitHub Copilot and others:** copy [`integrations/claude-code/skills/check-wheels/`](integrations/claude-code/skills/check-wheels/) into your project's `.agents/skills/` folder. For a Cursor project rule instead, copy [`integrations/cursor/wheelgap.mdc`](integrations/cursor/wheelgap.mdc) to `.cursor/rules/`.

**As an MCP server:** `wheelgap-mcp` lets any agent that speaks the [Model Context Protocol](https://modelcontextprotocol.io/) call wheelgap as a tool, with read-only `check_package`, `check_lockfile`, `check_dockerfile`, `diff_lockfiles` and `list_targets` tools. For Claude Code:

```bash
claude mcp add wheelgap -- uvx --from wheelgap wheelgap-mcp
```

The [MCP setup guide](https://github.com/wheelgap/wheelgap/blob/main/docs/mcp.md) has the tools and a copy-paste snippet for Claude Code, Cursor, VS Code, Codex and Claude Desktop.

<!-- mcp-name: io.github.wheelgap/wheelgap -->

Contributing to wheelgap with an agent? See [AGENTS.md](https://github.com/wheelgap/wheelgap/blob/main/AGENTS.md).

## Comparing two lockfiles

```bash
wheelgap diff old/uv.lock uv.lock -p 3.13 -t linux-aarch64,musl-aarch64
wheelgap diff origin/main:uv.lock uv.lock -p 3.13 -t linux-aarch64   # the base branch's lockfile, no checkout needed
```

This checks both lockfiles and lists the packages that got worse on a target (a wheel that became a source build, say) and the new packages with gaps, so a dependency bump that drops a platform fails CI. It exits 1 if there are any, 0 if not, and 2 on bad input. See [`wheelgap diff`](https://wheelgap.github.io/wheelgap/diff/).

## How it works

- **Lockfiles are read offline.** `uv.lock`, `pylock.toml`, `poetry.lock` and `pdm.lock` already record every file published for each pinned version. wheelgap parses the wheel filenames' tags with [`packaging`](https://packaging.pypa.io/) and checks them against the exact tag set each target accepts. That is the same logic pip and uv use to pick a wheel.
- **`requirements.txt`, `Pipfile.lock`, `pixi.lock` (after 0.9.0), `pyproject.toml` and bare package names** are looked up on the PyPI JSON API (or your `--index-url`). wheelgap checks the requirements you give it and doesn't resolve dependencies.
- **Environment markers are respected.** A `pywin32 ; sys_platform == 'win32'` dependency won't show up as a Linux gap. For `uv.lock` the dependency graph is walked per target, so transitive platform-specific packages are handled too.
- **No guessing.** Where wheelgap can't be sure, it says `unknown`, which counts as a gap, rather than guess.

[How it works](https://wheelgap.github.io/wheelgap/how-it-works/) covers version ranges, `--online` and `--offline`, the lookup cache and private indexes, and the [FAQ](https://wheelgap.github.io/wheelgap/faq/) answers "why does it say source when pip installs a wheel?".

## Why this exists

Missing wheels show up late, in the most expensive place:

- Your `python:3.13-alpine` image on an ARM runner takes 25 minutes because two transitive dependencies don't publish `musllinux_aarch64` wheels, and QEMU is compiling them.
- A build fails with `error: Microsoft Visual C++ 14.0 or greater is required` on a teammate's Snapdragon laptop.
- You try free-threaded Python 3.14t and half your stack quietly falls back to source builds, because `abi3` wheels don't apply there.

Sites like [pyreadiness.org](https://pyreadiness.org) and [free-threaded wheels](https://hugovk.github.io/free-threaded-wheels/) track the top few hundred packages. wheelgap checks your packages, at your pinned versions, for your targets, in about a second, and it can run in CI.

## Roadmap

wheelgap is growing into **caniuse for Python wheels**: an open map of which packages have wheels for which platforms. The CLI stays focused, and around it come a nightly tracker of the most-used PyPI packages, a community knowledge base of workarounds, and campaigns to help upstream projects ship missing wheels. **[Read the roadmap and join the discussion →](https://github.com/wheelgap/wheelgap/discussions/43)**

Next up for the CLI, each with a first step written up and no PR yet:

- "nearest version with a wheel" hints ([#20](https://github.com/wheelgap/wheelgap/issues/20))

The [roadmap](https://github.com/wheelgap/wheelgap/labels/roadmap) and [help wanted](https://github.com/wheelgap/wheelgap/labels/help%20wanted) labels have more.

## Contributing

Contributions are welcome, and pull requests usually get a review within hours.

- **New here?** Issues labelled [good first issue](https://github.com/wheelgap/wheelgap/labels/good%20first%20issue) say which files to change and what to test.
- **Done one or two?** [help wanted](https://github.com/wheelgap/wheelgap/labels/help%20wanted) issues are bigger, and a good next step.

[CONTRIBUTING.md](https://github.com/wheelgap/wheelgap/blob/main/CONTRIBUTING.md) has the 3-command dev setup, or you can start in the browser with no setup:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/wheelgap/wheelgap)

Questions are welcome in [Discussions](https://github.com/wheelgap/wheelgap/discussions).

## Thanks

wheelgap is created and maintained by [@patrickballard](https://github.com/patrickballard), with a lot of help from these people. Thank you, all of you!

[![wheelgap's contributors](https://contrib.rocks/image?repo=wheelgap/wheelgap)](https://github.com/wheelgap/wheelgap/graphs/contributors)

## License

[MIT](https://github.com/wheelgap/wheelgap/blob/main/LICENSE)
