# AI coding agents

There are three ways to give a coding agent wheelgap: the `check-wheels` skill, a Cursor rule, or the
[MCP server](mcp.md). For an agent or script that runs the command line itself, the
[agent recipe](recipes.md#using-wheelgap-from-an-agent-or-script) covers the exit codes and reading `--json`.

## The check-wheels skill

wheelgap ships a **`check-wheels` skill** that teaches coding agents to run it, read its JSON and suggest fixes. It
follows the open [Agent Skills](https://agentskills.io/) format.

**Claude Code** (install from the wheelgap repository as a plugin):

```text
/plugin marketplace add wheelgap/wheelgap
/plugin install wheelgap@wheelgap
```

On Claude Code 2.1.275 or later, `/plugin install wheelgap --marketplace wheelgap/wheelgap` does both in one step.

**Codex, Cursor, GitHub Copilot and others:** copy
[`integrations/claude-code/skills/check-wheels/`](https://github.com/wheelgap/wheelgap/tree/main/integrations/claude-code/skills/check-wheels)
into your project's `.agents/skills/` folder.

## A Cursor rule

For a Cursor project rule instead, copy
[`integrations/cursor/wheelgap.mdc`](https://github.com/wheelgap/wheelgap/blob/main/integrations/cursor/wheelgap.mdc)
to `.cursor/rules/`.

## The MCP server

`wheelgap-mcp` lets any agent that speaks the [Model Context Protocol](https://modelcontextprotocol.io/) call
wheelgap as a tool, with no shell access needed:

```bash
claude mcp add wheelgap -- uvx --from wheelgap wheelgap-mcp
```

[MCP server](mcp.md) has its tools and the setup for Claude Code, Cursor, VS Code, Codex and Claude Desktop.

## Contributing with an agent

Contributing to wheelgap with an agent? See
[AGENTS.md](https://github.com/wheelgap/wheelgap/blob/main/AGENTS.md).
