# How it works

agentlift is four stages: **parse → plan → apply → run**. The first two are pure
(no network); the second two talk to the API.

## parse  (`agentlift.parser`)

Reads the project folder into an in-memory `Project` of `AgentSpec`s. Pure file IO:
frontmatter is split, skills are discovered and content-hashed, MCP servers are
classified `url`/`stdio`, knowledge files are collected. No validation of API
limits yet — just "what's on disk."

## plan  (`agentlift.planner`)

Turns the `Project` into a `DeployPlan`: an ordered list of API operations plus
diagnostics. It is a **pure function of the folder** — same input, same plan. That
property is what makes `agentlift plan` a safe dry-run and makes the whole
translation unit-testable.

The plan carries *symbolic* references so it can be built and asserted on without
ever contacting the API:

- skills are referenced as `@skill:<hash8>` — identical skills collapse to one upload
- roster agents are referenced as `@agent:<name>`

The planner also (for the **Anthropic Managed Agents** target — `agent_toolset_20260401`,
`mcp_toolset`, and `permission_policy` are Anthropic's API wire-shape names, not generic/AWS;
the Bedrock and Google targets emit their own shapes):

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

## apply  (`agentlift.anthropic_target.Deployer`)

The only networked module. It resolves the symbolic refs to real IDs:

1. **Upload skills.** For each upload, check the lockfile by content hash; else look
   for an already-existing skill on the account (titles are content-addressed:
   `<name>-<hash8>`); else `beta.skills.create(files=...)`. Identical skills are
   uploaded exactly once.
2. **Create agents** in dependency order (roster before coordinators). Each request
   has its `@skill:`/`@agent:` refs replaced with real IDs, then a canonical hash is
   computed. If the lockfile already has that agent at that exact spec hash, it is
   reused — no API call. Otherwise `beta.agents.create(...)`.
3. **Write `.agentlift-lock.json`** mapping content hashes → skill IDs and agent names
   → `{agent_id, version, spec_hash, skill_ids}`.

### Idempotency

Re-running `deploy` on an unchanged folder uploads nothing and creates nothing.
This is pinned by `tests/test_idempotency.py`, which applies the plan twice against
a fake client and asserts zero work on the second pass — no network required.

Commit the lockfile so a teammate's deploy reuses the same remote objects. Even
without it, content-addressed skill titles mean skills are found and reused rather
than duplicated.

## run  (`agentlift.runtime`)

- `run_managed` — create an environment, open a session against the agent ID,
  stream events to collect the answer, read usage, estimate cost.
- `run_local` — run the *same folder* on your machine via the Messages API plus
  local tool execution (`read_file` / `list_files` / `run_bash`), with each
  `SKILL.md` inlined into the system prompt. This is the portability check: one
  definition, two runtimes.

## The reverse pipeline: import  (`agentlift.importer`)

`import` is `deploy` run backwards — it reads a **live** managed agent back into the
neutral `.managed-agents/` folder, so you can migrate a runtime you only have in the
cloud. It mirrors **parse → plan → apply** in reverse:

```
live runtime ──fetch──▶ raw resources ──import──▶ ImportedProject ──folder_writer──▶ folder
              (network)                  (pure)                       (pure)
```

- **fetch** (`anthropic_source` / `harness_source`) — the only networked edge: list +
  retrieve the agents, download skill versions, read the harness config + its S3 skills.
- **import** (`agentlift.importer`) — the inverse of `planner`: provider wire-shape →
  `AgentSpec`s. It reverses the planner's dedup (skills/MCP used identically by more than
  one agent are *hoisted* to `shared/` — skills keyed by content hash, MCP by full
  identity), reverse-maps the model id (Bedrock's regional inference profile → the folder
  `claude-*` id), and pulls a coordinator's subagent closure in by roster id → name.
- **folder_writer** (`agentlift.folder_writer`) — the inverse of `parser`: writes
  `agent.md` frontmatter + system prompt, `skills/<name>/SKILL.md`, `mcp.json`, exactly
  the [folder convention](convention.md) a hand-written project uses.

Same discipline as deploy — **pure core, thin network edge, the mapping is the contract**:
`importer` and `folder_writer` are pure and offline-tested; only the two `*_source.py`
modules touch the network. After writing the folder, `import` **self-verifies** by
re-running the real `parse` + `plan` over what it just wrote and printing `Round-trip OK`.
`import --dry-run` is the import analogue of `plan`: it prints the imported project +
diagnostics and writes nothing. Whatever does not survive the round-trip becomes a
`Diagnostic` (knowledge inlining is one-way; MCP auth values are provider-side; etc.) —
never a silent loss. Full details and the per-provider coverage in [import.md](import.md).

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
