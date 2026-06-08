# The agent convention

agentlift reads two layouts, auto-detected.

## 1. `.managed-agents/` — the deploy folder

Everything inside `.managed-agents/` is a deploy target *by virtue of being there*.
The name is deliberate: it can't be confused with `.claude/agents/`, where Claude's
**local** agents and native subagents live. Those are not deploy targets and are
**never auto-scanned** — keep what you want in the cloud here.

```
<project>/
└── .managed-agents/
    ├── shared/                              # optional, shared across all agents
    │   ├── skills/<skill-name>/SKILL.md
    │   └── mcp.json
    └── <agent-name>/
        ├── agent.md                         # frontmatter + system prompt
        ├── skills/<skill-name>/SKILL.md     # agent-local skill (+ any bundled files)
        ├── mcp.json                         # agent-local MCP servers
        └── knowledge/*.md                   # reference files (md/txt/json/csv)
```

The structure inside each agent folder is the same embedded-agent model you already
use with the Claude Agent SDK — only the parent folder name is new.

`agentlift import` writes **exactly this layout** — it is the inverse of the parser, so a
folder reconstructed from a live runtime is identical in shape to a hand-written one (and
re-deployable as-is). See [import.md](import.md).

## 2. A single agent directory

Point agentlift straight at a folder that contains `agent.md` or `CLAUDE.md` and it
becomes a one-agent project. This is how you deploy **one** existing Claude Code
embedded-agent folder without moving it — point at `.claude/agents/<name>/` and its
`CLAUDE.md`, `.mcp.json`, and `.claude/skills/<skill>/SKILL.md` are all read:

```
.claude/agents/<agent-name>/
├── CLAUDE.md
├── .mcp.json
├── .claude/skills/<skill-name>/SKILL.md
└── knowledge/*.md
```

agentlift never *scans* `.claude/agents/` as a whole — it would sweep in local
subagents (single `.md` files) and other local agents that aren't meant for the
cloud. Pointing at one folder is an explicit, per-agent choice.

## Native single-file subagents

Claude Code subagents are single files — `.claude/agents/<name>.md` with frontmatter
and a prompt, invoked in-process by a parent agent via the Task tool. They are local
delegation helpers, **not** deploy targets, so agentlift does not deploy them. To run
a capability in the managed cloud, give it its own folder under `.managed-agents/`.

---

## `agent.md` / `CLAUDE.md` frontmatter

YAML frontmatter is optional; every key has a sensible default. The body after the
frontmatter is the system prompt.

```markdown
---
name: knowledge-agent          # default: directory name
description: ...               # optional, sent to the API as the agent description
model: claude-haiku-4-5        # default: --model flag (claude-haiku-4-5)
tools: [read, glob, grep]      # built-in tool allowlist; OMIT to enable all builtins
skills: [summarize, shared/pm-basics]   # OMIT to auto-discover local skills/
mcp: [shared/docs]             # OMIT to use all servers in the local mcp.json
subagents: [research-agent]    # presence makes this agent a coordinator
knowledge: inline              # inline (default) | skip
---
System prompt goes here.
```

### Resource references

- A bare name (`summarize`) resolves to a local resource, then a shared one.
- A `shared/<name>` prefix resolves only against `.managed-agents/shared/`.
- Omitting `skills:` / `mcp:` auto-discovers the agent's **local** resources (shared
  resources attach only when referenced).

### Built-in tools

`tools:` lists Claude Code tool names. They map to Managed Agents built-ins:

| You write | Managed built-in |
|---|---|
| `read` | `read` |
| `glob` | `glob` |
| `grep` | `grep` |
| `bash` | `bash` |
| `edit`, `multiedit` | `edit` |
| `write` | `write` |
| `webfetch` / `web_fetch` | `web_fetch` |
| `websearch` / `web_search` | `web_search` |

Tools with no managed equivalent (e.g. `task`, `todowrite`) are dropped with a
warning. MCP tools are configured through `mcp.json`, not `tools:`.

### Per-tool permissions (`:ask` / `:allow`)

Append `:ask` to gate a tool behind caller approval, or `:allow` (the default) to
auto-approve. Works on built-in tools and on specific MCP tools.

```yaml
tools: [read, glob, grep, bash:ask, write:ask]
```

At runtime an `:ask` tool call pauses the session (`requires_action`) until your
app approves or rejects it — the deployable equivalent of a PreToolUse "ask" hook.
See [deploying.md](deploying.md#permissions-the-deployable-hook).

## `mcp.json` / `.mcp.json`

```jsonc
{
  "mcpServers": {
    "docs": {
      "type": "url",                          // url = deployable; stdio = rejected
      "url": "https://example.com/mcp",
      "allowedTools": ["search", "delete:ask"] // specific tools; ':ask' gates one
    }
  }
}
```

`allowedTools` is a **specific-tool** allowlist: only those tools are exposed to
the agent (omit the key to expose all of the server's tools). Each entry may carry
a `:ask` / `:allow` permission suffix, same as built-in tools.

`stdio` servers (`"command": "npx", ...`) are valid locally but cannot deploy to a
managed agent — agentlift errors clearly (or drops them with `--skip-unsupported`).

## Skills

A skill is a directory containing `SKILL.md` (Anthropic skill format: YAML
frontmatter with `name` + `description`, then instructions). Any sibling files are
uploaded with it. agentlift content-addresses the whole directory, so identical
skills upload once and are reused across agents and across machines.
