# Local definition → Anthropic Managed Agents

The exact field-by-field translation agentlift performs, and the API constraints
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

### Per-tool permission policy

A `:ask` / `:allow` suffix on a built-in or MCP tool name maps to that tool's
`permission_policy`:

- `name:ask`   → `permission_policy: {"type": "always_ask"}` (session pauses for caller approval)
- `name:allow` → `permission_policy: {"type": "always_allow"}` (default; omitted from the request)

Confirmed accepted on both `agent_toolset_20260401` configs and `mcp_toolset`
configs. This is the deployable form of a PreToolUse "ask" hook; arbitrary hook
code does not run in the managed sandbox.

## Skills

A skill directory → `beta.skills.create(display_title, files=[...])` → `skill_id`,
referenced as `{"type": "custom", "skill_id": ...}`.

- **Custom skills are not inline.** They are uploaded as a multipart bundle (every
  file under a `<name>/` top-level directory, `SKILL.md` at its root) and referenced
  by ID. agentlift handles the upload + reference for you.
- **`display_title` must be globally unique per account** (the API 400s on reuse).
  agentlift suffixes the content hash (`<name>-<hash8>`) so the title is stable and
  collision-free, and identical skills resolve to the same title.
- **Dedup / sharing.** Skills are content-addressed; an identical skill used by N
  agents is uploaded once and all N reference the same `skill_id`.
- **No XML tags in the description.** The API rejects angle-bracket tags in a
  `SKILL.md` frontmatter `description`; agentlift flags this at plan time
  (`skill.xml_in_description`). The body is unrestricted.
- Limit: 20 skills per agent.

## MCP servers

frontmatter `mcp:` / discovered `mcp.json` → `agents.create(mcp_servers=[...])` plus
one `mcp_toolset` per server in `tools`.

- Only **URL** servers deploy: `{"type": "url", "name", "url"}`. The managed URL MCP
  shape has no `headers`/`env` field — **no inline auth rides along**.
- **stdio** servers (`command`/`args`) are rejected with `mcp.stdio_unsupported`
  (or dropped with `--skip-unsupported`).
- A server's `allowedTools` becomes its `mcp_toolset` **specific-tool** allowlist
  (`default_config.enabled=false` + a `configs` entry per allowed tool). No
  `allowedTools` → `default_config.enabled=true` (all tools from the server).
- Each `allowedTools` entry may carry a `:ask` / `:allow` permission suffix, mapped
  to the tool config's `permission_policy` (see above).
- Limit: 20 servers per agent.

## Subagents → multiagent

frontmatter `subagents: [a, b]` → `agents.create(multiagent={"type":"coordinator",
"agents":[<ids>]})`.

- Roster agents are created first; their IDs are substituted into the coordinator.
- **Depth limit 1:** a roster agent may not itself be a coordinator. agentlift errors
  with `subagent.depth` if you nest.
- Roster: 1–20 entries.

## Tool count

Across the built-in toolset + every `mcp_toolset`, the API allows ≤128 tool
configurations. agentlift errors with `tools.too_many` past that.

## Reverse mapping (import)

`agentlift import anthropic <out-dir>` inverts every field rule above, reading a live agent
back into a neutral `.managed-agents/` folder. It is **read-only** — `agents.list`/
`agents.retrieve` plus `skills.versions.download` for custom-skill content; it never creates,
updates, or archives anything, and self-verifies by re-running the real parse+plan and printing
**"Round-trip OK"**.

It recovers system prompt / description / model, built-in tools **with their `:ask`/`:allow`
permission policies**, URL MCP servers **with per-server tool filters**, custom skills (content,
re-keyed to a `<name>/` bundle), and coordinator `subagents` (roster ids resolved back to names;
selecting a coordinator pulls its subagents into the import closure automatically). Skills and
MCP servers used identically by more than one agent are hoisted to `shared/` (skills keyed by
content hash, MCP by full identity) — the inverse of the dedup/sharing rule above.

Four mappings are one-way and each surfaces as a diagnostic: **knowledge inlining** (the
`# Reference material` fold stays in the prompt body — import can't re-split it into
`knowledge/`), **first-party skills** (`type: anthropic` is reference-only, no downloadable
content), **inline MCP auth** (only the header/env-var *name* is recoverable, never the secret),
and any **custom tool** with no neutral-folder form (dropped). See [import.md](import.md).

## What is NOT yet mapped

- **Vaults / secrets** — authenticated remote MCP. The agent-create shape in this
  SDK version has no `vault_ids`; on the roadmap.
- **Environments / files / memory stores** — agentlift creates a default cloud
  environment at run time; richer environment config is not yet exposed.
