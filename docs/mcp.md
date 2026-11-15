# Set up wheelgap-mcp in your agent

`wheelgap-mcp` is wheelgap's [MCP](https://modelcontextprotocol.io/) server. It
runs over stdio and gives an agent five read-only tools: `check_package`,
`check_lockfile`, `check_dockerfile`, `diff_lockfiles` and `list_targets`. Every
client below starts it with the same command:

```bash
uvx --from wheelgap wheelgap-mcp
```

That needs [uv](https://docs.astral.sh/uv/getting-started/installation/). If
you ran wheelgap with `uvx` before 0.9.0 and get "An executable named
`wheelgap-mcp` is not provided", uvx is reusing its cached copy: run
`uvx --refresh --from wheelgap wheelgap-mcp --version` once. Pick your client,
paste the snippet, restart the client (or reload its MCP servers) and try the
prompt at the end. In releases after 0.9.0, `uvx wheelgap mcp` starts the same
server too.

## Tools

All five are read-only. They look packages up on PyPI, or on the index in
`WHEELGAP_INDEX_URL`, and like wheelgap the server needs nothing beyond
`packaging`.

- `check_package`: one package, the latest release or a given version, on each
  target. In releases after 0.9.0, `explain: true` adds why each target with a
  gap can't use its wheels.
- `check_lockfile`: the same answer as `wheelgap PATH --json`, `[tool.wheelgap]`
  settings included: the packages with gaps, the ones it skipped (git, path and
  private-index packages, which aren't checked) and any notes. In releases after
  0.9.0 it also returns `blocking_newer_python` when two Pythons, one newer, are
  checked.
- `check_dockerfile`: the answer of
  `wheelgap PATH --dockerfile DOCKERFILE --json` (`PATH` defaults to the
  Dockerfile's folder), plus the `image` it read (`python`, `platform`,
  `glibc`, `musl`), so an agent can ask before switching `python:3.13-slim` to
  `-alpine` or adding `--platform=linux/arm64`. A Dockerfile wheelgap can't read
  is a tool error.
- `diff_lockfiles`: takes an `old` and a `new` lockfile (plus `python` and
  `targets`) and answers as `wheelgap diff OLD NEW --json` does, with `ok`
  added: the packages that got worse on a target and the new ones with gaps. An
  agent that bumps dependencies can save the old lockfile under its own name
  (`mkdir -p /tmp/old && git show HEAD:uv.lock > /tmp/old/uv.lock`) and ask
  whether the upgrade lost a wheel.
- `list_targets`: the platform names the other tools accept.

On `pyodide` targets, `check_package` and `check_lockfile` take an optional
`pyodide_lock` (a path or https URL of a `pyodide-lock.json`, as
`--pyodide-lock` does), so packages Pyodide ships itself show as `bundled`.

Pass `check_lockfile`, `check_dockerfile` and `diff_lockfiles` an absolute path
or a `file://` URI, since the server's working directory may not be your
project. If the client shares its
[roots](https://modelcontextprotocol.io/specification/2025-06-18/client/roots),
a relative path such as `uv.lock` is resolved against the first root that
contains it.

Tool calls send progress notifications when the client asks for them and don't
stop the server answering `ping`. A cancelled call sends no more progress and no
result, but its index lookups still finish before the next call starts.

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
settings. Their `ok` is true only when no package has a gap on any target. In
releases after 0.9.0, `passes` is the command line's verdict (exit code 0),
which also accepts gaps on the project's advisory targets (`advisory = [...]`)
and, with `fail-on = "missing"`, source builds. In releases after 0.9.0, the
result also lists the advisory targets under `advisory`, and each entry in
`gaps` has `advisory_gaps`: which of its `gaps` are on advisory targets.

In releases after 0.9.0, you can also ask why a package has no wheel for a
target:

> Why doesn't numpy 2.3.4 have a wheel for Python 3.14t on Alpine ARM64?

The agent should call `check_package` with `explain` set to `true`. The
result then adds `rejected`: for each target with a gap, up to 8 of the
package's wheels that target can't use, closest misses first, each with the
reason (such as `abi3 wheels can't be used on free-threaded Python (only
abi3t ones can)`), and `rejected_omitted` counting any it left out. Without
`explain`, the result has only the statuses, which keeps it small.

## Protocol versions

In releases after 0.9.0, the server speaks every MCP revision from 2024-11-05 to
2026-07-28, so it works with older and newer clients alike (0.9.0 speaks the
`initialize` handshake of 2024-11-05 to 2025-11-25 only):

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
