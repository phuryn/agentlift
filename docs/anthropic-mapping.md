# Local definition → Anthropic Managed Agents

The exact field-by-field translation skylift performs, and the API constraints
behind each rule. Confirmed against `anthropic` Python SDK 0.105.x and live API
calls on 2026-06-02.

## Betas

- `managed-agents-2026-04-01` — agents, sessions, environments
- `skills-2025-10-02` — skill upload, and required when an agent references a custom skill

## System prompt

`agent.md` / `CLAUDE.md` body → `agents.create(system=...)`. Limit: 100,000 chars.
`knowledge/*.md` files are appended under a `# Reference material` section
(size-guarded; overflow warns and stops).

## Model

frontmatter `model:` → `agents.create(model=...)`, default `claude-haiku-4-5`
(override with `--model`).

## Built-in tools

frontmatter `tools:` → one `agent_toolset_20260401` entry.

- omitted → `default_config: {enabled: true}` (all builtins on)
- listed → `default_config: {enabled: false}` plus one `configs` entry per allowed tool

Valid managed built-in names: `bash`, `edit`, `read`, `write`, `glob`, `grep`,
`web_fetch`, `web_search`. Local names map case-insensitively (`multiedit`→`edit`,
`webfetch`→`web_fetch`, …). Anything else is dropped with a `tools.unmapped`
warning.

## Skills

A skill directory → `beta.skills.create(display_title, files=[...])` → `skill_id`,
referenced as `{"type": "custom", "skill_id": ...}`.

- **Custom skills are not inline.** They are uploaded as a multipart bundle (every
  file under a `<name>/` top-level directory, `SKILL.md` at its root) and referenced
  by ID. skylift handles the upload + reference for you.
- **`display_title` must be globally unique per account** (the API 400s on reuse).
  skylift suffixes the content hash (`<name>-<hash8>`) so the title is stable and
  collision-free, and identical skills resolve to the same title.
- **Dedup / sharing.** Skills are content-addressed; an identical skill used by N
  agents is uploaded once and all N reference the same `skill_id`.
- Limit: 20 skills per agent.

## MCP servers

frontmatter `mcp:` / discovered `mcp.json` → `agents.create(mcp_servers=[...])` plus
one `mcp_toolset` per server in `tools`.

- Only **URL** servers deploy: `{"type": "url", "name", "url"}`. The managed URL MCP
  shape has no `headers`/`env` field — **no inline auth rides along**.
- **stdio** servers (`command`/`args`) are rejected with `mcp.stdio_unsupported`
  (or dropped with `--skip-unsupported`).
- A server's `allowedTools` becomes its `mcp_toolset` allowlist
  (`default_config.enabled=false` + a `configs` entry per allowed tool). No
  `allowedTools` → `default_config.enabled=true` (all tools from the server).
- Limit: 20 servers per agent.

## Subagents → multiagent

frontmatter `subagents: [a, b]` → `agents.create(multiagent={"type":"coordinator",
"agents":[<ids>]})`.

- Roster agents are created first; their IDs are substituted into the coordinator.
- **Depth limit 1:** a roster agent may not itself be a coordinator. skylift errors
  with `subagent.depth` if you nest.
- Roster: 1–20 entries.

## Tool count

Across the built-in toolset + every `mcp_toolset`, the API allows ≤128 tool
configurations. skylift errors with `tools.too_many` past that.

## What is NOT yet mapped

- **Vaults / secrets** — authenticated remote MCP. The agent-create shape in this
  SDK version has no `vault_ids`; on the roadmap.
- **Environments / files / memory stores** — skylift creates a default cloud
  environment at run time; richer environment config is not yet exposed.
