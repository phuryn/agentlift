# The agent convention

skylift reads three layouts, auto-detected in this order. You don't pick one — it
finds whichever your project uses.

## 1. `.agents/` (skylift-native)

```
<project>/
└── .agents/
    ├── shared/                              # optional, shared across all agents
    │   ├── skills/<skill-name>/SKILL.md
    │   └── mcp.json
    └── <agent-name>/
        ├── agent.md                         # frontmatter + system prompt
        ├── skills/<skill-name>/SKILL.md     # agent-local skill (+ any bundled files)
        ├── mcp.json                         # agent-local MCP servers
        └── knowledge/*.md                   # reference files (md/txt/json/csv)
```

## 2. `.claude/agents/` (Claude Code embedded-agents layout)

Deploys unchanged — this is the layout from the Claude Agent SDK template.

```
<project>/
└── .claude/agents/<agent-name>/
    ├── CLAUDE.md
    ├── .mcp.json
    ├── .claude/skills/<skill-name>/SKILL.md
    └── knowledge/*.md
```

## 3. A single agent directory

Point skylift straight at a folder that contains `agent.md` or `CLAUDE.md`. It
becomes a one-agent project.

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
- A `shared/<name>` prefix resolves only against `.agents/shared/`.
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

## `mcp.json` / `.mcp.json`

```jsonc
{
  "mcpServers": {
    "docs": {
      "type": "url",                       // url = deployable; stdio = rejected
      "url": "https://example.com/mcp",
      "allowedTools": ["search", "fetch"]  // becomes the per-server tool allowlist
    }
  }
}
```

`stdio` servers (`"command": "npx", ...`) are valid locally but cannot deploy to a
managed agent — skylift errors clearly (or drops them with `--skip-unsupported`).

## Skills

A skill is a directory containing `SKILL.md` (Anthropic skill format: YAML
frontmatter with `name` + `description`, then instructions). Any sibling files are
uploaded with it. skylift content-addresses the whole directory, so identical
skills upload once and are reused across agents and across machines.
