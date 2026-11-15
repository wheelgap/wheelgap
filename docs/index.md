# wheelgap

**Find the Python dependencies that have no prebuilt wheel for your target platform, before your Docker build does.**

Point it at your lockfile and the platforms you ship to (ARM servers, Alpine images, Windows on ARM, free-threaded
Python 3.14t). It tells you which packages pip or uv will have to compile from source, and which can't be installed at
all.

```bash
uvx wheelgap uv.lock -p 3.13 -t linux-aarch64,musl-aarch64
```

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

(That's the test project in `tests/fixtures/uvproj`, which needs no network.)

![wheelgap demo](demo.gif)

## Where to start

- [Getting started](getting-started.md): install, the first run, and reading the table.
- [Inputs](inputs.md): which lockfiles and files wheelgap reads, and what it skips.
- [Targets](targets.md): platform names, aliases, and the glibc, musl, macOS, iOS and Android floors.
- [Configuration](configuration.md): every `[tool.wheelgap]` key in `pyproject.toml`.
- [Output](output.md): the text table, `--json`, `--markdown`, `--junit` and exit codes.
- [CI](ci.md): the GitHub Action, pre-commit and GitLab CI.
- [Command line](cli.md): every flag.
- [Python API](python-api.md), [MCP server](mcp.md) and [AI coding agents](agents.md).
- [FAQ](faq.md), such as "why does it say source when pip installs a wheel?".

## Versions

These pages describe wheelgap on the `main` branch. Anything marked **after 0.9.0** isn't in a release on PyPI yet;
the [changelog](https://github.com/wheelgap/wheelgap/blob/main/CHANGELOG.md) lists it under "Unreleased". Check
`uvx wheelgap@latest --help` for the flags the released version has.

The [coverage tracker](https://wheelgap.github.io/tracker/) shows wheel coverage for the top 5,000 PyPI packages, by
platform and Python version, updated nightly.
