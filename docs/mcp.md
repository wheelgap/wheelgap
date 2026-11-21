# Set up wheelgap-mcp in your agent

`wheelgap-mcp` is wheelgap's [MCP](https://modelcontextprotocol.io/) server. It
runs over stdio and gives an agent eight read-only tools: `check_package`,
`check_lockfile`, `check_dockerfile`, `check_dist`, `diff_lockfiles`, `explain_install_failure`,
`suggest_targets` and `list_targets`. Every
client below starts it with the same command:

```bash
uvx --from wheelgap wheelgap-mcp
```

That needs [uv](https://docs.astral.sh/uv/getting-started/installation/). If
you ran wheelgap with `uvx` before 0.9.0 and get "An executable named
`wheelgap-mcp` is not provided", uvx is reusing its cached copy: run
`uvx --refresh --from wheelgap wheelgap-mcp --version` once. Pick your client,
paste the snippet, restart the client (or reload its MCP servers) and try the
prompt at the end. From 0.10.0 on, `uvx wheelgap mcp` starts the same server
too.

## Tools

All eight are read-only. Package checks look packages up on PyPI, or on the index in
`WHEELGAP_INDEX_URL`; `list_targets` and `suggest_targets` contact nothing. Like wheelgap the server needs nothing beyond
`packaging`.

- `check_package`: one package, the latest release or a given version, on each
  target. `explain: true` adds why each target with a gap can't use its
  wheels, and `dependencies: true` checks what it installs too. The response's
  `assumes` includes the platform version floors used for the check.
- `check_lockfile`: the same answer as `wheelgap PATH --json`, `[tool.wheelgap]`
  settings included: the packages with gaps, the ones it skipped (git, path and
  private-index packages, which aren't checked) and any notes. It also returns
  `blocking_newer_python` when two Pythons, one newer, are checked. With
  `dependencies: true`, requirements files, a conda `environment.yml`,
  `pyproject.toml` and a script's `# /// script` block are resolved as `--deps` would, including transitive gaps, each with `via`
  (the path to it on each gap target) and `direct` (the direct dependencies
  behind it). It's refused where `--deps` is: lockfiles, offline, and another
  index. With `fix: true` it also returns the [gap wheels](#gap-wheels-fix) that fill
  its gaps, as `--fix` does (`check_dockerfile` takes it too).
- `check_dockerfile`: the answer of
  `wheelgap PATH --dockerfile DOCKERFILE --json` (`PATH` defaults to the
  Dockerfile's folder), plus the `image` it read (`python`, `platform`,
  `glibc`, `musl`, `build_args`), so an agent can ask before switching `python:3.13-slim` to
  `-alpine` or adding `--platform=linux/arm64`. `build_args` maps global ARG names
  used by the image's `FROM` to their values (or `{}` if none were used). If a
  Docker build overrides them with `--build-arg`, pass the same values as the
  `build_args` argument, as `--build-arg` does on the
  command line. A Dockerfile wheelgap can't read is a tool error. With `inputs: true` it checks the
  requirements files the Dockerfile installs with `RUN pip install -r FILE` instead, as
  `--dockerfile-inputs` does (`context` is `--dockerfile-context`).
- `check_dist`: the files a release is about to upload — the
  `dist/` folder `uv build` or `python -m build` leaves — as
  `wheelgap --dist DIR --json` reports them: per target, whether a wheel
  installs or only the sdist is there to build from, and `dropped` naming each target the previous
  release on PyPI had a wheel for that this one doesn't. `ok` is false when
  anything was dropped — and when the comparison couldn't run, rather than
  guess, and when any target has no wheel (a source build may need a
  compiler), as in the other tools. `[tool.wheelgap]` settings and
  `.python-version` come from the nearest folder above it with a
  `pyproject.toml` (else the folder holding it), not the server's working
  directory. An agent that cuts releases can gate the upload on it, the way the
  [Action's `dist` input](ci.md#before-a-release) does in a workflow.
- `diff_lockfiles`: takes an `old` and a `new` lockfile (plus `python` and
  `targets` and optional platform version floors) and answers as
  `wheelgap diff OLD NEW --json` does, with `ok`
  added: the packages that got worse on a target and the new ones with gaps. An
  agent that bumps dependencies can save the old lockfile under its own name
  (`mkdir -p /tmp/old && git show HEAD:uv.lock > /tmp/old/uv.lock`) and ask
  whether the upgrade lost a wheel. Pass any floors that differ from the
  defaults explicitly. This tool does not read `[tool.wheelgap]` settings.
- `explain_install_failure`: takes the text of a failed install log (up to 1 MB)
  and reads it as [`--from-log`](cli.md#from-log) does: pip's `Failed to build` summary
  (its versions from the `Collecting`, `Downloading` and `Using cached` lines),
  uv's ``Failed to build `NAME==VERSION` ``, Poetry's build-backend note (normal
  or `-v` output) and `pdm install -v` output. PDM's normal output is refused,
  since it doesn't say which index a package came from; colour codes and GitHub
  Actions timestamps are fine. Each package that failed to build (at most 50)
  is checked at the version the log names, as `check_package` does with
  `explain: true`. Pass `python` and `targets` for the machine the install ran
  on: the log doesn't say, so without them the server's Python and the default
  platforms are checked, and `notes` says so. The reply is always
  `{"targets": [...], "ok": ..., "packages": [...], "assumes": {...}, "notes": [...]}`:
  `packages` has one entry per failed package, in the order the log names them,
  in `check_package`'s shape (`name`, `version`, `results`, `gaps`, `ok`,
  `rejected`, `rejected_omitted`, `via`, `direct`, `issues_url`, and `suggest`
  and `last_with_wheel` when they were looked up, in one lookup for all of
  them); `ok` is true only if every one installs without building on every
  target. A failed package with a wheel for a checked target gets a note that
  the log likely comes from another platform or Python. A log it can't read
  with certainty is a tool error, never a guess: one that names no failed build,
  one where which version failed isn't clear, or one that installed from another
  index than PyPI (pip's `Looking in indexes:`, PDM's build link) or with
  `--find-links`, unless the server's `WHEELGAP_INDEX_URL` is set (then the
  failures are checked on that index). uv's log doesn't name its index, so a uv failure is checked on PyPI (or
  `WHEELGAP_INDEX_URL`). The log is untrusted text: the reply and errors repeat
  no more of it than package names and versions.
- `suggest_targets`: read the project's GitHub Actions workflows, `.gitlab-ci.yml` and Dockerfiles,
  returning `targets`, `python`, `unreadable` and suggested `config`, as
  `wheelgap --suggest-targets --json` does. Each finding names its file and line.
  Nothing is changed or checked; confirm the suggested platforms with the user
  before checking their project. A missing folder, the filesystem root and the
  home folder are tool errors, and files that are symlinks out of the project
  are listed as unreadable, not read.
- `list_targets`: the platform names the other tools accept and the built-in
  glibc, musl, macOS, iOS and Android API floors in `defaults`.

For example, to check everything `requirements.txt` installs, call
`check_lockfile` with `{"path": "requirements.txt", "python": "3.13",
"targets": ["win-arm64"], "dependencies": true}`. The `dependencies` argument
is a boolean, defaults to `false`, and follows the command line's `--deps`
validation and error rules.

On `pyodide` targets, `check_package` and `check_lockfile` take an optional
`pyodide_lock` (a path or https URL of a `pyodide-lock.json`, as
`--pyodide-lock` does), so packages Pyodide ships itself show as `bundled`.

`check_lockfile` also takes an optional `environment` string for
`pixi.lock` inputs, like the CLI's `--environment`. For example, pass
`"environment": "test"` to check the `test` environment instead of `default`.
An unknown environment is a tool error listing the lockfile's environment names.

Pass `check_lockfile`, `check_dockerfile`, `check_dist`, `diff_lockfiles` and `suggest_targets` an absolute path
or a `file://` URI, since the server's working directory may not be your
project. If the client shares its
[roots](https://modelcontextprotocol.io/specification/2025-06-18/client/roots),
a relative path such as `uv.lock` is resolved against the first root that
contains it.

Tool calls send progress notifications when the client asks for them and don't
stop the server answering `ping`. A cancelled call sends no more progress and no
result, but its index lookups still finish before the next call starts.

### Tool arguments

Each tool's arguments, taken from the `description`s in `TOOLS` in `mcp.py`
(`inputSchema.properties`). Types are JSON types; defaults are shown where the
tool has one. `check_lockfile` and `check_dist` read the project's `pyproject.toml`
(and `.python-version`) as the command line does, so a setting there comes
before the built-in default: the lockfile's folder for `check_lockfile`, and
for `check_dist` the nearest folder above the dist folder with a
`pyproject.toml`.

#### `check_package`

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | The package name alone, e.g. 'numpy'. |
| `version` | string | latest release | An exact version, e.g. '2.3.4'. Default: the latest release, which may not support an older Python (it then shows gaps an older release wouldn't have). |
| `python` | string | server's | Target Python version(s), comma-separated, e.g. '3.13' or '3.12,3.14t' (t = free-threaded); 'next' and 'next-t' mean the newest CPython wheelgap knows. Default: the server's. |
| `targets` | array of strings | `linux-x86_64`, `linux-aarch64`, `macos-arm64`, `win-amd64` | Platforms, e.g. ['linux-aarch64', 'musl-x86_64', 'win-arm64', 'pyodide'], Docker platforms such as 'linux/arm64', or the groups 'all', 'everything' and 'mobile'. `list_targets` gives every name. |
| `glibc` | string | `2.36` | glibc version for `linux-*` targets, e.g. '2.28' for a RHEL 8 image. |
| `musl` | string | `1.2` | musl version for `musl-*` (Alpine) targets. |
| `macos` | string | `14.0` | Oldest macOS version to support. |
| `ios` | string | `13.0` | Oldest iOS version to support, e.g. '15.0'. |
| `android_api` | string | `24` | Minimum Android API level to support, e.g. '26'. |
| `pyodide_lock` | string | none | Absolute path or https URL of a Pyodide release's `pyodide-lock.json`, so packages that release ships itself count as 'bundled' on pyodide targets for its Python. |
| `explain` | boolean | `false` | Also return `rejected` (and `via`, always empty for one package) and `issues_url` (the package's issue tracker from PyPI metadata): why each source or missing target can't use the published wheels. |
| `dependencies` | boolean | `false` | Also check everything the package installs (its dependency tree, resolved from PyPI for each target, as `--deps` does) and return `dependency_gaps`. |

#### `check_lockfile`

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | string | required | Absolute path or `file://` URI of the lockfile or project directory (a relative path is looked for in the client's roots if it shares them, else in the server's working directory). |
| `environment` | string | `default` | The pixi.lock environment to check. Only for pixi.lock inputs. |
| `python` | string | `[tool.wheelgap] python`, then `.python-version`, else the server's | Target Python version(s), comma-separated, e.g. '3.13' or '3.12,3.14t' (t = free-threaded); 'next' and 'next-t' mean the newest CPython wheelgap knows. |
| `targets` | array of strings | `[tool.wheelgap] targets`, then `[tool.uv] required-environments`, else the four default platforms | Platforms, e.g. ['linux-aarch64', 'musl-x86_64', 'win-arm64', 'pyodide'], Docker platforms such as 'linux/arm64', or the groups 'all', 'everything' and 'mobile'. `list_targets` gives every name. |
| `glibc` | string | `[tool.wheelgap] glibc` or `distro`, else `2.36` | glibc version for `linux-*` targets, e.g. '2.28' for a RHEL 8 image. |
| `musl` | string | `[tool.wheelgap] musl` or `distro`, else `1.2` | musl version for `musl-*` (Alpine) targets. |
| `macos` | string | `[tool.wheelgap] macos`, else `14.0` | Oldest macOS version to support. |
| `ios` | string | `[tool.wheelgap] ios`, else `13.0` | Oldest iOS version to support, e.g. '15.0'. |
| `android_api` | string | `[tool.wheelgap] android-api`, else `24` | Minimum Android API level to support, e.g. '26'. |
| `pyodide_lock` | string | `[tool.wheelgap] pyodide-lock` (when a pyodide target is checked) | Absolute path or https URL of a Pyodide release's `pyodide-lock.json`. |
| `offline` | boolean | `[tool.wheelgap] offline`, else `false` | Use only the files recorded in the lockfile; never contact the index. `true` turns it on; `offline = true` in the project can't be turned off from here. |
| `dependencies` | boolean | `false` | Resolve transitive dependencies of requirements files, a conda `environment.yml`, `pyproject.toml` and a script's `# /// script` block with `--deps`; each gap gets `via` and `direct`. Refused for lockfiles, offline and another index. |
| `extras` | array of strings | `[tool.wheelgap] extras`, else every extra of a `uv.lock` or `poetry.lock`, none of a `pyproject.toml` | Install these extras, as `--extra NAME`: a uv or Poetry project extra, or a `pyproject.toml` `[project] optional-dependencies` group. Not for `pdm.lock`. |
| `all_extras` | boolean | `false` | Install every extra, as `--all-extras` (`uv.lock`, `poetry.lock`, `pyproject.toml`). |
| `groups` | array of strings | `[tool.wheelgap] groups`, else every group of a lockfile, none of a `pyproject.toml` | Include these dependency groups, as `--group NAME` (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`, or `pyproject.toml` `[dependency-groups]`). |
| `only_groups` | array of strings | none | Check only these dependency groups, as `--only-group NAME`: the project's own dependencies are left out unless a group names them. |
| `no_dev` | boolean | `false` | Leave out the `dev` group, as `--no-dev` (`uv.lock`, Poetry 2.x `poetry.lock`, `pdm.lock`; refused for `pyproject.toml`, where no group is checked unless selected). |
| `fix` | boolean | `false` | Also return `gap_wheels`: the gaps a wheel on wheelgap's [gap wheel index](https://wheelgap.github.io/wheels/) fills, as `--fix` does. One request to wheelgap.github.io that names no package. Refused with `offline`. See [Gap wheels](#gap-wheels-fix). |

With none of these and no `[tool.wheelgap] extras` or `groups`, a `uv.lock`, `poetry.lock` or `pdm.lock` is checked
for every extra and group, which may report gaps the deployment never installs. Match the deployment explicitly, for
example `{"no_dev": true, "extras": ["api"]}` for a production install with the `api` extra, or
`{"only_groups": ["test"]}` to check only a `pyproject.toml`'s PEP 735 `test` group. An empty array is the same as
leaving the argument out. Other inputs (requirements files, `Pipfile.lock`, `pylock.toml`, `pixi.lock`, a script's
`# /// script` block) refuse these arguments, and unknown names and incompatible combinations (`only_groups` with
`extras` on a `uv.lock` or `pyproject.toml`, `no_dev` with `groups: ["dev"]`) return the command line's error.

#### `check_dockerfile`

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `dockerfile` | string | required | Absolute path or `file://` URI of the Dockerfile (a relative path is looked for in the client's roots if it shares them, else in the server's working directory). |
| `path` | string | the Dockerfile's folder | Absolute path or `file://` URI of the lockfile or project directory. Not with `inputs: true`. |
| `inputs` | boolean | `false` | Check the requirements files the Dockerfile's final stage installs with `RUN pip install -r FILE`, instead of a lockfile, as `--dockerfile-inputs` does. |
| `context` | string | the Dockerfile's folder | With `inputs: true`, absolute path or `file://` URI of the folder the build context starts from, as `--dockerfile-context DIR` (for `docker build -f docker/Dockerfile .`, the folder that `.` is). |
| `build_args` | object (string values) | `{}` | The build's `--build-arg` values, e.g. {'PYTHON_VERSION': '3.13'}: each replaces the default of the global ARG it names, as in docker build. |
| `targets` | array of strings | the image's platform | Platforms to check instead of the image's, e.g. ['linux/arm64'] (with an Alpine image, Docker platforms mean musl). |
| `fix` | boolean | `false` | Also return `gap_wheels`: the gaps a wheel on wheelgap's [gap wheel index](https://wheelgap.github.io/wheels/) fills, as `--fix` does. One request to wheelgap.github.io that names no package. See [Gap wheels](#gap-wheels-fix). |

With `inputs: true` the tool answers "will this image build without compiling anything?" in one call, as
`wheelgap --dockerfile DOCKERFILE --dockerfile-inputs --json` does: it checks the requirements files the final
stage installs, on the image's Python and platform. `path` in the result is then the build context, and `notes`
names each file read and its line (`Dockerfile:4: RUN pip install -r reads requirements.txt`). Only a
[strict subset of Dockerfiles](cli.md#dockerfile-inputs) is read this way. A line that may install anything else
(`RUN pip install .`, say) is a tool error naming the line, never a guess. `inputs` with `path`, and `context`
without `inputs`, are tool errors.

#### `check_dist`

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | string | required | Absolute path or `file://` URI of the folder of built files (a relative path is looked for in the client's roots if it shares them, else in the server's working directory). |
| `python` | string | `[tool.wheelgap] python`, then `.python-version`, else the server's | Target Python version(s), comma-separated, e.g. '3.13' or '3.12,3.14t' (t = free-threaded); 'next' and 'next-t' mean the newest CPython wheelgap knows. |
| `targets` | array of strings | `[tool.wheelgap] targets`, then `[tool.uv] required-environments`, else the four default platforms | Platforms, e.g. ['linux-aarch64', 'musl-x86_64', 'win-arm64', 'pyodide'], Docker platforms such as 'linux/arm64', or the groups 'all', 'everything' and 'mobile'. `list_targets` gives every name. |
| `glibc` | string | `[tool.wheelgap] glibc` or `distro`, else `2.36` | glibc version for `linux-*` targets, e.g. '2.28' for a RHEL 8 image. |
| `musl` | string | `[tool.wheelgap] musl` or `distro`, else `1.2` | musl version for `musl-*` (Alpine) targets. |
| `macos` | string | `[tool.wheelgap] macos`, else `14.0` | Oldest macOS version to support. |
| `ios` | string | `[tool.wheelgap] ios`, else `13.0` | Oldest iOS version to support, e.g. '15.0'. |
| `android_api` | string | `[tool.wheelgap] android-api`, else `24` | Minimum Android API level to support, e.g. '26'. |

#### `diff_lockfiles`

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `old` | string | required | Absolute path to the lockfile (or project directory) before the change. |
| `new` | string | required | Absolute path to the lockfile (or project directory) after the change. |
| `python` | string | the server's | Target Python version(s), comma-separated, e.g. '3.13' or '3.12,3.14t' (t = free-threaded); 'next' and 'next-t' mean the newest CPython wheelgap knows. |
| `targets` | array of strings | the four default platforms | Platforms, e.g. ['linux-aarch64', 'musl-x86_64', 'win-arm64', 'pyodide'], Docker platforms such as 'linux/arm64', or the groups 'all', 'everything' and 'mobile'. `list_targets` gives every name. |
| `glibc` | string | `2.36` | glibc version for `linux-*` targets, e.g. '2.28' for a RHEL 8 image. |
| `musl` | string | `1.2` | musl version for `musl-*` (Alpine) targets. |
| `macos` | string | `14.0` | Oldest macOS version to support. |
| `ios` | string | `13.0` | Oldest iOS version to support, e.g. '15.0'. |
| `android_api` | string | `24` | Minimum Android API level to support, e.g. '26'. |

#### `explain_install_failure`

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `log` | string | required | The text of the failed pip, uv, Poetry or PDM (`pdm install -v`) install log, up to 1 MB in UTF-8. |
| `python` | string | the server's | Target Python version(s), comma-separated, e.g. '3.13' or '3.12,3.14t' (t = free-threaded); 'next' and 'next-t' mean the newest CPython wheelgap knows. Pass the one the install ran with: the log doesn't say. |
| `targets` | array of strings | the four default platforms | Platforms, e.g. ['linux-aarch64', 'musl-x86_64', 'win-arm64', 'pyodide'], Docker platforms such as 'linux/arm64', or the groups 'all', 'everything' and 'mobile'. Pass the platform the install ran on: the log doesn't say. `list_targets` gives every name. |
| `glibc` | string | `2.36` | glibc version for `linux-*` targets, e.g. '2.28' for a RHEL 8 image. |
| `musl` | string | `1.2` | musl version for `musl-*` (Alpine) targets. |
| `macos` | string | `14.0` | Oldest macOS version to support. |

#### `suggest_targets`

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `path` | string | required | Absolute path or `file://` URI of the project folder (a relative path is looked for in the client's roots if it shares them, else in the server's working directory). |

#### `list_targets`

No arguments.

### Gap wheels (`fix`)

`check_lockfile` and `check_dockerfile` take `fix: true` to also say which gaps a wheel on wheelgap's
[gap wheel index](https://wheelgap.github.io/wheels/) fills, as `--json --fix` does. The result then has:

- `gap_wheels`: one entry per filled gap cell, with `name`, `version`, `target` (labelled like `results`, e.g.
  `"3.14 win-arm64"`), `wheel`, `url` and `sha256`. It's `[]` when no gap wheel fits, and `notes` says why;
- `gap_wheel_index`: the index to install from (`https://wheelgap.github.io/wheels/simple/`);
- `gap_wheel_about`: the page on how the wheels are built and checked.

Without `fix`, none of these keys are returned and nothing is requested. With it, the server makes one request to
wheelgap.github.io that names no package. The wheels are unofficial: wheelgap builds them from the PyPI sdist, the
package's maintainers don't. The agent should tell the user that, and ask before adding the index to project files
(the tools never write files themselves). `fix` never changes `gaps`, `ok` or `passes`: they still report what PyPI
has. It's off with another index (`index-url` in `[tool.wheelgap]` or a `WHEELGAP_INDEX_URL` mirror), with a note,
and packages wheelgap can't tell come from PyPI are never looked for. `fix` with `offline` is a tool error.

## Claude Code

Add it for yourself, in the current project:

```bash
claude mcp add wheelgap -- uvx --from wheelgap wheelgap-mcp
```

To share it with everyone working on the project, add `--scope project`
(`claude mcp add --scope project wheelgap -- ...`), or commit this `.mcp.json`
at the project root. Claude Code asks each person to approve a project server
before it uses it.

```json
{
  "mcpServers": {
    "wheelgap": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "wheelgap", "wheelgap-mcp"]
    }
  }
}
```

Check it with `claude mcp get wheelgap`. Docs: [Claude Code MCP](https://code.claude.com/docs/en/mcp).

## Cursor

Put this in `.cursor/mcp.json` in your project, or in `~/.cursor/mcp.json` to
have it in every project:

```json
{
  "mcpServers": {
    "wheelgap": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "wheelgap", "wheelgap-mcp"]
    }
  }
}
```

Docs: [Cursor MCP](https://cursor.com/docs/mcp).

## VS Code (GitHub Copilot)

Copilot reads the same `mcpServers` format as Claude Code. Put the snippet
below in `.mcp.json` at the workspace root to share it with your team, or in
`~/.copilot/mcp-config.json` (`$COPILOT_HOME/mcp-config.json` if you set
`COPILOT_HOME`) to have it in every workspace. **Chat: Open Customizations** >
**MCP Servers** > **Add Server** writes the same entry for you.

```json
{
  "mcpServers": {
    "wheelgap": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "wheelgap", "wheelgap-mcp"]
    }
  }
}
```

These files are read by the **Copilot** harness: pick it in the chat's harness
picker if it isn't already selected. The workspace-root `.mcp.json` is the same
file Claude Code reads, so one committed file serves both. The older Local
harness reads `.vscode/mcp.json` instead, with a top-level `servers` key in
place of `mcpServers`; VS Code's docs call that format deprecated (to be removed
in February 2027), so use it only if you're still on the Local harness. Docs:
[VS Code MCP configuration](https://code.visualstudio.com/docs/agents/reference/mcp-configuration).

## OpenAI Codex CLI

```bash
codex mcp add wheelgap -- uvx --from wheelgap wheelgap-mcp
```

Or add it to `~/.codex/config.toml` (or a project's `.codex/config.toml`, which
Codex reads only in trusted projects):

```toml
[mcp_servers.wheelgap]
command = "uvx"
args = ["--from", "wheelgap", "wheelgap-mcp"]
```

Docs: [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp).

## Claude Desktop

Open **Settings** > **Developer** > **Edit Config**, or edit
`claude_desktop_config.json` directly:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "wheelgap": {
      "command": "uvx",
      "args": ["--from", "wheelgap", "wheelgap-mcp"]
    }
  }
}
```

Quit Claude Desktop completely and reopen it. It doesn't start from your shell,
so if it can't find `uvx`, put the full path from `which uvx` (`where uvx` on
Windows) in `command`: usually `~/.local/bin/uvx` (uv's installer, written out in
full, e.g. `/Users/you/.local/bin/uvx`) or `/opt/homebrew/bin/uvx` (Homebrew). On
Windows, double the backslashes in JSON: `"C:\\Users\\you\\.local\\bin\\uvx.exe"`. The server's stderr goes to `mcp-server-wheelgap.log` in
`~/Library/Logs/Claude` (macOS), `%APPDATA%\Claude\logs` (Windows) or
`~/.config/Claude/logs` (Linux). Docs:
[Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## Try it

Open a project that has a `uv.lock` and ask:

> Can this project's uv.lock install on Alpine ARM64 with Python 3.13?

The agent should call `check_lockfile` with the lockfile's path, `python`
`3.13` and the `musl-aarch64` target, and list any packages with no wheel for
it. The server's working directory may not be your project, so the agent should
pass an absolute path or a `file://` URI. A relative path such as `uv.lock`
works only when the client shares its
[roots](https://modelcontextprotocol.io/specification/2025-06-18/client/roots).

`check_lockfile` and `check_dockerfile` apply the project's `[tool.wheelgap]`
settings. Their `ok` is true only when no package has a gap on any target.
`passes` is the command line's verdict (exit code 0), which also accepts gaps on
the project's advisory targets (`advisory = [...]`) and, with
`fail-on = "missing"`, source builds. The result also lists the advisory targets
under `advisory`, and each entry in `gaps` has `advisory_gaps`: which of its
`gaps` are on advisory targets.

You can also ask why a package has no wheel for a target:

> Why doesn't numpy 2.3.4 have a wheel for Python 3.14t on Alpine ARM64?

The agent should call `check_package` with `explain` set to `true`. The
result then adds `rejected`: for each target with a gap, up to 8 of the
package's wheels that target can't use, closest misses first, each with the
reason (such as `abi3 wheels can't be used on free-threaded Python (only
abi3t ones can)`), and `rejected_omitted` counting any it left out. Without
`explain`, the result has only the statuses, which keeps it small.

When a package has gaps, `check_package` also returns `suggest` and
`last_with_wheel`, as `wheelgap --explain --suggest --json` does: for each gap
target some newer release has a wheel for, `suggest` names that release, such
as `{"3.13 win-arm64": "46.0.4"}`, so an agent can answer "move to
cryptography 46.0.4"; and for each gap target no newer release has a wheel
for, `last_with_wheel` names the newest older release that has one (of the 50
newest before the one checked), such as `{"3.13 win-arm64": "46.0.3"}`, so it
can answer "pin cryptography to 46.0.3 or earlier". It reads the
project's release list from PyPI, which is one more request, and is left out
when there's no gap, when `WHEELGAP_INDEX_URL` points at another index (which
may hold private names), or when that lookup fails. (`suggest` is new in
0.13.0; 0.12.0 gives only `last_with_wheel`.)

To check what a package installs as well as the package itself, as [`--deps`](cli.md#deps) does, pass
`dependencies: true`:

> Can I install mcp 2.3.0 on Windows on ARM?

The tree is resolved for each target from PyPI's metadata, and the result adds
`dependencies_checked` (how many other packages were checked) and
`dependency_gaps`: each package in the tree with a gap, with its `results`,
`gaps` and `via`, the path to it on each gap target, such as
`{"3.13 win-arm64": ["mcp", "PyJWT", "cryptography"]}`, and its
own `suggest` and `last_with_wheel`, looked up for the whole tree together.
Those aren't checked against what the rest of the tree requires, so a parent
may pin the dependency elsewhere: `via` shows who pulls it in. With several
Pythons, the package's own `suggest` and `last_with_wheel` cover every release
it resolved to. The other fields stay
those of the package asked about, except `ok`, which is then true only if
nothing in the tree has a gap. A tree that only a backtracking resolver could
settle is an error, as it is on the command line.
When several Pythons pick different releases of the package itself, its
`results` and `gaps` cover all of them and `versions` lists them.

## Protocol versions

The server speaks every MCP revision from 2024-11-05 to 2026-07-28, so it works
with older and newer clients alike (0.9.0 speaks the `initialize` handshake of
2024-11-05 to 2025-11-25 only):

- **2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25** start with the
  `initialize` handshake. Results include `structuredContent` from 2025-06-18 on.
- **2026-07-28** has no handshake: each request carries its protocol version and
  the client's capabilities in `_meta`, and a client may first ask
  `server/discover` which versions the server speaks. A client that supports both
  probes with `server/discover` and gets an answer, so it stays on 2026-07-28. A
  request for a version the server doesn't speak gets error `-32022` listing the
  ones it does. These requests are stateless, and the server can't send requests
  of its own. So when a client that shares roots passes a relative path, the server
  answers with `resultType: "input_required"` asking for `roots/list`, and the
  client retries the call with its roots in `inputResponses`. The official
  Python SDK's client does this for you. The path must then be inside one of
  those roots (`..` can't leave it): if it isn't, the call is an error saying to
  pass an absolute path, rather than a check of a same-named file in the server's
  working directory. A relative `pyodide_lock` is looked for the same way. A
  client that answers with no roots gets the working directory.

The server picks the version for each request, so clients of both kinds can
share one server process.
