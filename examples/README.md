# Examples

Two runnable projects. Both work with `agentlift plan` offline (no key); deploying
and running needs `ANTHROPIC_API_KEY` with Managed Agents beta access.

## `quickstart/` — one agent, one skill, knowledge

The smallest real agent: a system prompt with an identity sign-off, a tool
allowlist (`read/glob/grep`), one uploaded skill (`receipt-stamp`), and a knowledge
file folded into the prompt.

```bash
agentlift plan   ./quickstart
agentlift deploy ./quickstart -y
agentlift run knowledge-agent --project ./quickstart --task "What is a North Star metric?"
agentlift run knowledge-agent --project ./quickstart --task "What is RICE?" --local   # same folder, locally
agentlift destroy ./quickstart -y
```

The `RECEIPT:` line in the output is the uploaded skill firing inside the runtime.

## `team/` — multi-agent, shared resources, a coordinator

Shows everything agentlift wires:

- **shared skill** `cite-sources` used by two agents → uploaded once
- **shared MCP server** `docs` (remote URL) with a specific-tool allowlist
- a **coordinator** (`lead`) with a `subagents` roster (`bug-finder`, `researcher`)
- a **per-tool permission**: `bug-finder` declares `bash:ask`, so the hosted agent
  pauses for caller approval before each `bash` call

```bash
agentlift plan ./team        # see the dedup, the coordinator ordering, the MCP wiring
agentlift deploy ./team -y
agentlift run lead --project ./team --task "Find the bug in utils.py and explain RICE."
```

> The `docs` MCP server points at `https://example.com/mcp` (a placeholder). Swap in
> a real remote MCP URL before relying on its tools; the agent deploys and runs
> fine without ever calling it.

## `in-a-project/` — `.managed-agents/` embedded in a real codebase

A stand-in for an actual project: a repo-level `CLAUDE.md`, some `src/` code, and a
local `.claude/agents/pr-reviewer.md` subagent — alongside a `.managed-agents/`
folder with an `orchestrator` coordinator over three subagents sharing two skills.

```bash
agentlift plan ./in-a-project    # only the 4 managed agents appear; the repo
                               # CLAUDE.md, app code, and local subagent never do
```

Demonstrates context isolation (nothing outside `.managed-agents/` is read or
uploaded) and shared-skill dedup across a roster.

## `deploy-workflow/ci-deploy.yml` — git-push-to-deploy

A GitHub Actions template. Copy it to your repo's `.github/workflows/`, add an
`ANTHROPIC_API_KEY` secret, and every push that touches `.managed-agents/`
validates, deploys, and commits the updated lockfile. See [docs/deploying.md](../docs/deploying.md).

## `claude-code-skill/deploy-managed-agents/` — deploy from Claude Code

Drop this skill into your repo's `.claude/skills/`. Then in Claude Code you can say
"deploy my managed agents" or "run the researcher with …" and it maps your words to
the right `agentlift` command.
