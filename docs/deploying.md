# Deploying: commands, workflows, and where the IDs live

skylift is declarative: your `.managed-agents/` folder is the desired state, and
`deploy` makes the cloud match it. *How* you trigger that deploy is up to you —
three paths, all reusing things you already know.

## 1. A command (individuals)

```bash
skylift plan .            # dry run — see exactly what will happen
skylift deploy . --yes    # upload skills + create agents, write the lockfile
skylift run lead --project . --task "..."
```

Best for solo work and first runs. `plan` is a pure dry-run (no network), so you
always see the diff before anything ships.

## 2. Git push (teams) — recommended

Treat agents like any other code: edit the folder, open a PR, merge, and a CI job
deploys. Copy [`examples/deploy-workflow/ci-deploy.yml`](../examples/deploy-workflow/ci-deploy.yml)
to `.github/workflows/`, add an `ANTHROPIC_API_KEY` secret, and commit
`.managed-agents/`. On every push that touches it:

1. `skylift validate .` fails the build on any error (e.g. a stdio MCP server).
2. `skylift deploy . --yes --prune` applies the change (idempotent — unchanged
   skills/agents are skipped).
3. The updated `.skylift-lock.json` is committed back.

Nothing new to learn: the workflow is `git push`. Review happens in PRs. Rollback
is `git revert` + redeploy.

## 3. From Claude Code (if you live there)

Drop [`examples/claude-code-skill/deploy-managed-agents/`](../examples/claude-code-skill/)
into your repo's `.claude/skills/`. Then in Claude Code:

> "deploy my managed agents"
> "run the researcher with: summarize the Q3 launch"

The skill maps your words to the right `skylift` command and shows you the plan
first. Nothing new to learn: you just ask.

---

## Where the IDs live: the lockfile

When you deploy, skylift writes **`.skylift-lock.json`** next to the path you
deployed. It maps your local definitions to the remote objects they became:

```jsonc
{
  "version": 1,
  "skills": { "<content-hash>": { "skill_id": "skill_01…", "display_title": "receipt-stamp" } },
  "agents": { "lead": { "agent_id": "agent_01…", "version": 1, "spec_hash": "…", "skill_ids": ["skill_01…"] } }
}
```

**Yes — store the IDs. Commit the lockfile.** It is the source of truth for:

- **Idempotent re-deploys.** An unchanged skill is not re-uploaded; an unchanged
  agent (same resolved spec hash) is not re-created. Without the lockfile, every
  deploy would make new objects.
- **`run` / `list` / `destroy` by name.** `skylift run lead …` resolves `lead` to
  its `agent_id` from the lockfile.
- **Team + CI reuse.** A teammate or the CI job deploying the same repo reuses the
  same cloud objects instead of duplicating them.

It is safe to commit: it holds only IDs, hashes, and titles — **no secrets**.

Notes:
- The lockfile is **per Anthropic account/org**. Commit it when your team shares an
  account (the common case). If two people deploy to different accounts, each gets
  their own state — skylift self-heals skills by content hash + remote lookup, and
  re-creates agents your account doesn't have.
- Deploy and run with the **same path** so they read the same lockfile
  (`skylift deploy .` then `skylift run … --project .`).

---

## Isolation: each agent gets only its folder

A deployed agent's context is exactly: its own system prompt, its own (and
`shared/`) skills, its own (and `shared/`) MCP servers, and its inlined knowledge.
Nothing else.

This is the isolation contract the local Agent SDK has to *work* for — there, the
CLI walks up the directory tree and leaks the repo-root `CLAUDE.md`, repo-root
skills, and user-level MCP servers into every agent unless you pass an explicit
skills allowlist and `strictMcpConfig: true` (see the embedded-agents playbook
§3/§3.5). In the managed cloud there is no directory to walk: the agent only ever
receives what skylift uploads, and skylift scopes uploads to the agent folder. So
you get isolation **by construction** — the repo's `CLAUDE.md`, a sibling agent's
skills, and your machine's MCP servers can't leak in. Pinned by
[`tests/test_isolation.py`](../tests/test_isolation.py).

---

## Permissions: the deployable "hook"

Claude Code hooks are local scripts (PreToolUse/PostToolUse) — they can't run in a
cloud sandbox with no local process. But their main job, **gating a tool behind
approval**, deploys as a per-tool permission policy.

Append `:ask` to any built-in tool or specific MCP tool to require caller approval
before each call (`:allow` is the default):

```yaml
# agent.md
tools: [read, glob, grep, bash:ask]      # bash pauses for approval
```
```jsonc
// mcp.json
{ "mcpServers": { "github": {
    "type": "url", "url": "https://…/mcp",
    "allowedTools": ["search_issues", "create_issue:ask"]   // writes gated
} } }
```

At runtime an `:ask` tool call pauses the session (`requires_action`); your app
approves or rejects it via a session event. That covers the PreToolUse "ask" hook.

What does **not** map: arbitrary hook *code* (custom block logic, PostToolUse
metadata capture). For those:
- Replace "block path X" guardrails by simply not enabling the tool, or by gating
  it with `:ask` and rejecting in your app.
- Do PostToolUse capture in your app — it already sees every `tool_use` event on
  the session stream.
