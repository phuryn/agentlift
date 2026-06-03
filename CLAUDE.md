# CLAUDE.md

Guidance for Claude Code (and any AI agent) working in this repository.

## What agentlift is

A compiler with a CLI. You define an agent **once** as a neutral folder
(`.managed-agents/` — system prompt + skills + MCP servers + tool allowlist +
subagent roster). agentlift then treats each managed-agent runtime as a back-end:

- `audit` — report, per provider, what is `native` / `emulated` / `degraded` / `unsupported` (offline).
- `export` — compile the folder to a provider-native artifact: `anthropic-yaml` (for the `ant` CLI), `google-adk`, `openai-agents` (offline).
- `deploy` — push to a live managed runtime via API: **Anthropic** (full) and **Google `--target google`** (preview).

Tagline: *Own the definition. Rent the runtime.*

## The pipeline: `parse → plan → apply → run`

```
folder ──parse──▶ Project ──plan──▶ DeployPlan ──apply──▶ live IDs ──▶ lockfile
         (pure)            (pure)              (network)
```

- **parse** ([parser.py](src/agentlift/parser.py)) — read the folder into a `Project` of `AgentSpec`s. Pure file IO.
- **plan** ([planner.py](src/agentlift/planner.py)) — `Project → DeployPlan`: a deterministic list of API ops with **symbolic refs** (`@skill:<hash8>`, `@agent:<name>`), skill dedup, validation, diagnostics. **No network.** This is what `agentlift plan` prints and what offline tests assert against — *the plan is the contract.*
- **apply** ([anthropic_target.py](src/agentlift/anthropic_target.py)) — the only Anthropic networking. Resolves symbolic refs to real IDs, uploads skills (deduped via lockfile), creates agents in dependency order, writes `.agentlift-lock.json` for idempotent re-deploys.
- **run** ([runtime.py](src/agentlift/runtime.py)) — invoke a deployed agent by ID, or run the same folder locally (`--local`).

## Module map (`src/agentlift/`)

| File | Role | Pure? |
|---|---|---|
| [model.py](src/agentlift/model.py) | dataclasses: `Project`, `AgentSpec`, `SkillSpec`, `McpServerSpec`; `BUILTIN_TOOL_MAP` | ✅ |
| [parser.py](src/agentlift/parser.py) | folder → `Project` (frontmatter, skills, MCP, knowledge, shared/local refs) | ✅ |
| [planner.py](src/agentlift/planner.py) | `Project` → `DeployPlan` (Anthropic wire shape, symbolic refs) | ✅ |
| [capabilities.py](src/agentlift/capabilities.py) | the provider capability map (`anthropic`/`google`/`openai` × feature → tier) — **single source of truth** for `audit` and `export` annotations | ✅ |
| [audit.py](src/agentlift/audit.py) | cross-reference folder features against `capabilities` | ✅ |
| [export.py](src/agentlift/export.py) | `Project`/`DeployPlan` → text artifact (anthropic-yaml, google-adk, openai-agents) | ✅ |
| [anthropic_target.py](src/agentlift/anthropic_target.py) | `DeployPlan` → Anthropic API (skills + agents + multiagent) | ❌ network |
| [google_plan.py](src/agentlift/google_plan.py) | `Project` → `GoogleDeployPlan` (ADK recipe: agents, skills, URL MCP, env-var names, model map, spec hash, diagnostics) | ✅ |
| [google_codegen.py](src/agentlift/google_codegen.py) | `GoogleDeployPlan` → source package (`agentlift_engine/agent.py` + `requirements` + embedded skill bundles) | ✅ |
| [google_lock.py](src/agentlift/google_lock.py) | `.agentlift-google.json` spec-hash state + pure `decide_action` → create/update/skip | ✅ |
| [google_target.py](src/agentlift/google_target.py) | `GoogleDeployPlan` → built source package → live `reasoningEngine` via `agent_engines.create/update()` (source-deploy as a relative `ModuleAgent`; resolves MCP auth env vars) | ❌ network |
| [lockfile.py](src/agentlift/lockfile.py) | `.agentlift-lock.json` idempotency state (Anthropic) | ✅ |
| [diff.py](src/agentlift/diff.py) | plan vs lockfile (and optional `--remote`) | mostly |
| [runtime.py](src/agentlift/runtime.py) | run managed / run local | ❌ network |
| [cost.py](src/agentlift/cost.py), [graders.py](src/agentlift/graders.py) | token→USD estimate; substring + LLM graders | mixed |
| [cli.py](src/agentlift/cli.py) | argparse entry point (`python -m agentlift.cli`) | — |

## The folder convention (the input)

```
.managed-agents/
  shared/
    skills/<name>/SKILL.md     # skill shared across agents (uploaded once on Anthropic)
    mcp.json                   # MCP servers shared across agents
  <agent>/
    agent.md                   # YAML frontmatter + system prompt (CLAUDE.md also accepted)
    skills/<name>/SKILL.md      # private skill (this agent only)
    mcp.json / .mcp.json        # private MCP servers
    knowledge/*.md              # folded into the system prompt
```

Also accepted: a **single agent dir** passed directly (must contain `agent.md` or `CLAUDE.md`),
including an existing `.claude/agents/<name>/` embedded folder. `.claude/agents/` is **never
auto-scanned** — those are local subagents, not deploy targets.

`agent.md` frontmatter: `name`, `model`, `description`, `tools: [read, glob, bash:ask, ...]`
(built-in allowlist; `:ask`/`:allow` permission suffix), `skills: [name, shared/name]`,
`mcp: [name, shared/name]`, `subagents: [a, b]` (makes it a coordinator), `knowledge: skip`.
A bare ref resolves to the agent's **own** resource first, then `shared/`.

## Provider status (keep honest — see [IMPLEMENTATION-STATUS], external)

| | Anthropic | Google (`--target google`) | OpenAI |
|---|---|---|---|
| Handoff | `deploy` (live, **full**) | `deploy` (live, **preview**) | `export` + self-host only |
| Subagents | native, per-agent IDs | emulated (one `reasoningEngine`, server-side delegation) | `as_tool`, loop in your app |
| Skills | uploaded, shared by id (skill-bearing agents auto-get `read` — Managed Agents needs it to open `SKILL.md`) | ✅ embedded in source package, loaded via ADK `load_skill_from_dir` (update = redeploy) | export comment only |
| Remote MCP | mapped | ✅ URL → ADK `McpToolset` + `tool_filter`; inline auth → Agent Engine `env_vars` (resolved at deploy, never inlined) | export comment only |
| Built-in tools | mapped | 🚧 skipped (sandbox is Python/JS only) | self-host runner |
| `:ask` | permission policy | 🚧 unsupported on `VertexAiSessionService` | client-side |
| Idempotency | lockfile + content hashes | ✅ `.agentlift-google.json` spec hash → create/update/skip | n/a |
| Model | Claude (native) | 🔁 mapped to Gemini (`gemini-2.5-flash`) | 🔁 mapped to `gpt-*` |

**Live-verified (6/6 both):** one neutral fixture (`tests/live/fixtures/coverage-matrix`) was deployed
+ queried on **both** Anthropic and Google; all six portability dimensions (agents · subagents ·
shared MCP · individual MCP · shared skill · individual skill) were **EXERCISED server-side** —
objective runtime events, not answer text. Anthropic's subagents cell keys on the native delegation
event (`session.thread_created` + `agent.thread_message_sent`) since coordinator delegation is async.
Committed receipts: `tests/live/receipts/20260604-012428-anthropic` + `20260604-004318-google`. The
WIRED layer is pinned offline in `tests/test_coverage_matrix_plan.py` (CI); the live harness is
`tests/live/coverage_matrix.py` (gated pytest wrapper: `tests/live/test_coverage_matrix.py`). See
[docs/tested-platforms.md](docs/tested-platforms.md). OpenAI stays `export`-only (no hosted engine).

**The Google divergence to remember:** `audit` reports each *platform's* capability;
`deploy --target google` reports *agentlift's current implementation*. These now agree on
skills + URL MCP (both mapped). They still diverge on the built-in sandbox and `:ask`
(`audit` rates them `degraded`/`unsupported` for Google; `deploy` refuses or skips a
stdio MCP server / built-in-tool-only folder). Pipeline for Google mirrors Anthropic's
*plan-is-the-contract* discipline: `google_plan.py` is pure and offline-tested, only
`google_target.py` touches the network.

## Commands

```bash
agentlift validate <path>              # parse + plan, report problems (exit 1 on errors)
agentlift plan     <path> [--json] [--target anthropic|google] [--google-model M]  # deterministic deploy plan, no network
agentlift audit    <path> --targets anthropic,google,openai
agentlift export   <target> <path> [--out DIR]   # anthropic-yaml | google-adk | openai-agents
agentlift diff     <path> [--remote]
agentlift deploy   <path> [--target anthropic|google] [--build-only] [--prune] [-y]
agentlift run <agent> --project <path> --task "..." [--local]
agentlift list/destroy/bench ...
```

Not on PATH? `python -m agentlift.cli <cmd>` always works.

## Dev workflow & ground rules

```bash
python -m pip install -e ".[dev]"
pytest -m "not live"                    # fast, deterministic, no API key — what CI runs
ANTHROPIC_API_KEY=... pytest -m live    # hits the real API, costs cents
```

- **Keep `parser.py` and `planner.py` pure.** No network, no clock, no randomness. If a behavior can be tested offline, it lives there and gets an offline test in `tests/`.
- **Every translation rule needs an offline test asserting the plan** ([tests/test_planner.py](tests/test_planner.py)). The plan is the contract.
- **New API behavior gets confirmed live first, then encoded.** Don't guess wire format from docs alone — the betas move. Anthropic wire format notes live in [anthropic_target.py](src/agentlift/anthropic_target.py) docstring + [docs/anthropic-mapping.md](docs/anthropic-mapping.md).
- **Surface, don't swallow.** Anything agentlift can't translate becomes a `Diagnostic` (error/warning), visible in `agentlift plan` — never a silent drop.
- **`capabilities.py` is the single source of truth** for what each provider supports. `audit` and `export` annotations both read it; update it (not ad-hoc strings) when provider support changes.
- **Adding a provider target:** implement the same `apply(plan)` contract as `anthropic_target.Deployer`; the planner already emits provider-agnostic ops. Keep the convention identical so one folder deploys anywhere.
- Windows shell is PowerShell; Bash tool is available for POSIX scripts. The repo ships both `demo/*.ps1` and `demo/*.sh`.

## Key docs

- [docs/convention.md](docs/convention.md) — the `.managed-agents/` spec
- [docs/anthropic-mapping.md](docs/anthropic-mapping.md) — exact local → Managed Agents field mapping
- [docs/deploy-google.md](docs/deploy-google.md) — Google ADC/credentials/setup
- [docs/tested-platforms.md](docs/tested-platforms.md) — per-platform live test receipts
- [docs/how-it-works.md](docs/how-it-works.md), [docs/deploying.md](docs/deploying.md), [docs/limitations.md](docs/limitations.md)
- **External single source of truth for "real vs roadmap":** the author's `IMPLEMENTATION-STATUS.md` (kept in sync with README/article). Version is **0.5.0**.
