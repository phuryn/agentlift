# How it works

skylift is four stages: **parse → plan → apply → run**. The first two are pure
(no network); the second two talk to the API.

## parse  (`skylift.parser`)

Reads the project folder into an in-memory `Project` of `AgentSpec`s. Pure file IO:
frontmatter is split, skills are discovered and content-hashed, MCP servers are
classified `url`/`stdio`, knowledge files are collected. No validation of API
limits yet — just "what's on disk."

## plan  (`skylift.planner`)

Turns the `Project` into a `DeployPlan`: an ordered list of API operations plus
diagnostics. It is a **pure function of the folder** — same input, same plan. That
property is what makes `skylift plan` a safe dry-run and makes the whole
translation unit-testable.

The plan carries *symbolic* references so it can be built and asserted on without
ever contacting the API:

- skills are referenced as `@skill:<hash8>` — identical skills collapse to one upload
- roster agents are referenced as `@agent:<name>`

The planner also:

- maps the built-in tool allowlist to `agent_toolset_20260401` configs
- maps `:ask` / `:allow` tool suffixes to each config's `permission_policy`
- builds an `mcp_toolset` per remote server carrying its specific-tool allowlist
- rejects `stdio` MCP servers (or drops them with `--skip-unsupported`)
- scopes every agent to its own folder + `shared/` only — the repo-root
  `CLAUDE.md`, sibling skills, and user-level MCP servers never enter the request
- folds `knowledge/*.md` into the system prompt (size-guarded)
- wires `subagents` into a `multiagent` coordinator and orders roster agents first
- validates limits (≤20 skills, ≤20 MCP servers, ≤128 tools, 100k-char system)

A plan with any `error` diagnostic is not deployable.

## apply  (`skylift.anthropic_target.Deployer`)

The only networked module. It resolves the symbolic refs to real IDs:

1. **Upload skills.** For each upload, check the lockfile by content hash; else look
   for an already-existing skill on the account (titles are content-addressed:
   `<name>-<hash8>`); else `beta.skills.create(files=...)`. Identical skills are
   uploaded exactly once.
2. **Create agents** in dependency order (roster before coordinators). Each request
   has its `@skill:`/`@agent:` refs replaced with real IDs, then a canonical hash is
   computed. If the lockfile already has that agent at that exact spec hash, it is
   reused — no API call. Otherwise `beta.agents.create(...)`.
3. **Write `.skylift-lock.json`** mapping content hashes → skill IDs and agent names
   → `{agent_id, version, spec_hash, skill_ids}`.

### Idempotency

Re-running `deploy` on an unchanged folder uploads nothing and creates nothing.
This is pinned by `tests/test_idempotency.py`, which applies the plan twice against
a fake client and asserts zero work on the second pass — no network required.

Commit the lockfile so a teammate's deploy reuses the same remote objects. Even
without it, content-addressed skill titles mean skills are found and reused rather
than duplicated.

## run  (`skylift.runtime`)

- `run_managed` — create an environment, open a session against the agent ID,
  stream events to collect the answer, read usage, estimate cost.
- `run_local` — run the *same folder* on your machine via the Messages API plus
  local tool execution (`read_file` / `list_files` / `run_bash`), with each
  `SKILL.md` inlined into the system prompt. This is the portability check: one
  definition, two runtimes.

## Confirmed wire format

Everything above targets the shape confirmed live against the API (2026-06-02):

```python
# skill upload (multipart; SKILL.md at the root of a named directory)
client.beta.skills.create(
    display_title="receipt-stamp-035823c8",
    files=[("receipt-stamp/SKILL.md", b"---\nname: ...\n---\n...", "text/markdown")],
    betas=["managed-agents-2026-04-01", "skills-2025-10-02"],
)  # -> .id  == "skill_..."

# agent create
client.beta.agents.create(
    name="knowledge-agent", model="claude-haiku-4-5", system="...",
    tools=[{"type": "agent_toolset_20260401",
            "default_config": {"enabled": False},
            "configs": [{"name": "read", "enabled": True}, ...]}],
    skills=[{"type": "custom", "skill_id": "skill_..."}],
    mcp_servers=[{"type": "url", "name": "docs", "url": "https://..."}],
    multiagent={"type": "coordinator", "agents": ["agent_..."]},
    betas=["managed-agents-2026-04-01", "skills-2025-10-02"],
)  # -> .id, .version
```
