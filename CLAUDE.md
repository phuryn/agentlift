# CLAUDE.md

Guidance for Claude Code (and any AI agent) working in this repository.

## What agentlift is

A compiler with a CLI. You define an agent **once** as a neutral folder
(`.managed-agents/` — system prompt + skills + MCP servers + tool allowlist +
subagent roster). agentlift then treats each managed-agent runtime as a back-end:

- `audit` — report, per provider, what is `native` / `emulated` / `degraded` / `unsupported` (offline).
- `export` — compile the folder to a provider-native artifact: `anthropic-yaml` (for the `ant` CLI), `bedrock-strands`, `google-adk`, `openai-agents` (offline).
- `import` — the **inverse of `deploy`**: read a *live* managed agent back into a neutral folder (read-only, no writes to the account). **Anthropic** (full) and **AWS Bedrock `--mode harness`** today; a Bedrock Runtime is an opaque container → not importable. Because the folder is the neutral pivot, import + deploy = **migration between runtimes**. See [docs/import.md](docs/import.md).
- `deploy` — push to a live managed runtime via API: **Anthropic** (full); **Google `--target google`** (preview); **AWS Bedrock `--target bedrock`** with two primitives behind `--mode` (`auto` picks the least-powerful that preserves semantics, never a silent downgrade) — **both now live-verified**: **`--mode harness`** is a **✅ live single-agent deploy** via `CreateHarness` (IAM-only, no container; **6/6 cells receipt-verified on Nova** — agent + base-session sandbox + remote MCP + S3-loaded skill + `agentcore_browser`; AWS Harness feature in public preview), and **`--mode runtime`** is a **✅ live multi-agent hosted deploy** via `CreateAgentRuntime` (custom ARM64 container → ECR → runtime; **subagent delegation receipt-verified on Nova** — the coordinator's top-level trace named both specialists; `--build-only` still emits just the artifact). Both are **Claude-native, no model remap**; receipts are on **Nova** because Claude inference is Gate-A-gated (an account entitlement, not a code gap). Honest Runtime boundary: `InvokeAgentRuntime` returns the container's JSON body, not an event stream, so coordinator/root tool calls are objective (PASS-EXERCISED) while **nested** specialist skill/MCP calls are PASS-WIRED + text-corroborated.

Tagline: *Own the definition. Rent the runtime.*

> 🔒 **Before any commit:** anonymize real cloud identifiers — Google (project id/number,
> `reasoningEngine` id, bucket) **and** AWS (account id, ECR registry, AgentCore runtime
> **and harness** ARN/id) — to `****`, and never commit the Bedrock bearer token. See
> [Anonymize Google identifiers](#-anonymize-google-identifiers-before-every-commit-mandatory).

## The pipeline: `parse → plan → apply → run`

```
folder ──parse──▶ Project ──plan──▶ DeployPlan ──apply──▶ live IDs ──▶ lockfile
         (pure)            (pure)              (network)
```

- **parse** ([parser.py](src/agentlift/parser.py)) — read the folder into a `Project` of `AgentSpec`s. Pure file IO.
- **plan** ([planner.py](src/agentlift/planner.py)) — `Project → DeployPlan`: a deterministic list of API ops with **symbolic refs** (`@skill:<hash8>`, `@agent:<name>`), skill dedup, validation, diagnostics. **No network.** This is what `agentlift plan` prints and what offline tests assert against — *the plan is the contract.*
- **apply** ([anthropic_target.py](src/agentlift/anthropic_target.py)) — the only Anthropic networking. Resolves symbolic refs to real IDs, uploads skills (deduped via lockfile), creates agents in dependency order, writes `.agentlift-lock.json` for idempotent re-deploys.
- **run** ([runtime.py](src/agentlift/runtime.py)) — invoke a deployed agent by ID, or run the same folder locally (`--local`).

### The reverse pipeline: `fetch → import → write` (`import`)

```
live runtime ──fetch──▶ raw dicts ──import──▶ ImportedProject ──write──▶ folder
            (network)             (pure)                    (pure)
```

`import` mirrors `deploy` in reverse and keeps the **same discipline** — a pure core with a
thin network edge, the mapping is the contract, asserted offline. **fetch**
([anthropic_source.py](src/agentlift/anthropic_source.py) / [harness_source.py](src/agentlift/harness_source.py))
is the only networking; **import** ([importer.py](src/agentlift/importer.py)) is the *inverse of
the planner* (decode toolsets → `tools:` + `:ask`/`:allow`, regional inference profile →
folder Claude id, roster ids → names, hoist shared resources by content-hash/identity); **write**
([folder_writer.py](src/agentlift/folder_writer.py)) is the *inverse of the parser*. After writing,
the `import` command re-runs the real parse + plan and prints `Round-trip OK` only if the result
re-deploys — so an import is a verified, deployable folder, never a lossy copy.

## Module map (`src/agentlift/`)

| File | Role | Pure? |
|---|---|---|
| [model.py](src/agentlift/model.py) | dataclasses: `Project`, `AgentSpec`, `SkillSpec`, `McpServerSpec`; `BUILTIN_TOOL_MAP` | ✅ |
| [parser.py](src/agentlift/parser.py) | folder → `Project` (frontmatter, skills, MCP, knowledge, shared/local refs) | ✅ |
| [planner.py](src/agentlift/planner.py) | `Project` → `DeployPlan` (Anthropic wire shape, symbolic refs) | ✅ |
| [import_model.py](src/agentlift/import_model.py) | dataclasses for the reverse pipeline: `ImportedProject`/`ImportedAgent`/`ImportedSkill`/`ImportedMcp` (skills held as in-memory bytes) | ✅ |
| [importer.py](src/agentlift/importer.py) | raw provider dicts → `ImportedProject` (**inverse of `planner`**): tool decode, Bedrock model reverse-map, roster id→name, shared hoisting. Entry points `import_anthropic_agents` / `import_bedrock_harness` | ✅ |
| [folder_writer.py](src/agentlift/folder_writer.py) | `ImportedProject` → `.managed-agents/` files (**inverse of `parser`**): `agent.md`, `mcp.json`, `skills/*`, `shared/*` | ✅ |
| [anthropic_source.py](src/agentlift/anthropic_source.py) | live Anthropic → raw dicts (`agents.list/retrieve`, `skills.versions.download`; roster closure; zip unpack) | ❌ network |
| [harness_source.py](src/agentlift/harness_source.py) | live Bedrock harness → raw dict (`get_harness` + S3 skill bundles) | ❌ network |
| [capabilities.py](src/agentlift/capabilities.py) | the provider capability map (`anthropic`/`bedrock`/`google`/`openai` × feature → tier) — **single source of truth** for `audit` and `export` annotations | ✅ |
| [audit.py](src/agentlift/audit.py) | cross-reference folder features against `capabilities` | ✅ |
| [export.py](src/agentlift/export.py) | `Project`/`DeployPlan` → text artifact (anthropic-yaml, google-adk, openai-agents) | ✅ |
| [anthropic_target.py](src/agentlift/anthropic_target.py) | `DeployPlan` → Anthropic API (skills + agents + multiagent) | ❌ network |
| [google_plan.py](src/agentlift/google_plan.py) | `Project` → `GoogleDeployPlan` (ADK recipe: agents, skills, URL MCP, env-var names, model map, spec hash, diagnostics) | ✅ |
| [google_codegen.py](src/agentlift/google_codegen.py) | `GoogleDeployPlan` → source package (`agentlift_engine/agent.py` + `requirements` + embedded skill bundles) | ✅ |
| [google_lock.py](src/agentlift/google_lock.py) | `.agentlift-google.json` spec-hash state + pure `decide_action` → create/update/skip | ✅ |
| [google_target.py](src/agentlift/google_target.py) | `GoogleDeployPlan` → built source package → live `reasoningEngine` via `agent_engines.create/update()` (source-deploy as a relative `ModuleAgent`; resolves MCP auth env vars) | ❌ network |
| [bedrock_plan.py](src/agentlift/bedrock_plan.py) | `Project` → `BedrockDeployPlan` (Strands recipe: agents-as-tools, skills, URL MCP, env-var names, **native Claude** model map via regional inference profile, spec hash, diagnostics) | ✅ |
| [bedrock_codegen.py](src/agentlift/bedrock_codegen.py) | `BedrockDeployPlan` → source package (`agentlift_runtime/agent.py` + requirements + Dockerfile + embedded skill bundles) — the ARM64 AgentCore Runtime build context | ✅ |
| [bedrock_lock.py](src/agentlift/bedrock_lock.py) | `.agentlift-bedrock.json` spec-hash state + pure `decide_action` → create/update/skip | ✅ |
| [bedrock_target.py](src/agentlift/bedrock_target.py) | `BedrockDeployPlan` → built container build context (`--build-only`) **and** the live hosted create (ECR repo + login + buildx ARM64 push + `CreateAgentRuntime` + poll READY + `.agentlift-bedrock.json` lock + `InvokeAgentRuntime`), gated by `_RUNTIME_LIVE_VERIFIED` (now True — receipt-verified) | ❌ network |
| [harness_plan.py](src/agentlift/harness_plan.py) | `Project` → `HarnessDeployPlan` (managed single-agent recipe: native Claude model, `remote_mcp` tools, `agentcore_browser` web, base-session sandbox, env-var names, spec hash, diagnostics); `select_bedrock_mode` (harness vs runtime routing) + `harness_auto_deploy_allowed` (`_HARNESS_LIVE_VERIFIED` — now True, receipt-verified — gates bare auto-deploy) | ✅ |
| [harness_lock.py](src/agentlift/harness_lock.py) | `.agentlift-harness.json` spec-hash state + pure `decide_action` → create/update/skip (carries `live_verified: false` — operational state, not proof) | ✅ |
| [harness_target.py](src/agentlift/harness_target.py) | `HarnessDeployPlan` → live `CreateHarness`/`UpdateHarness`/`InvokeHarness` via boto3 (SigV4/IAM, `EXECUTION_ROLE_ENV`; resolves MCP auth env vars; clientToken-deleted retry) — wire shape **receipt-verified** (Nova) | ❌ network |
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

Two axes, kept distinct: `audit`/`capabilities.py` rate the **platform** (what the runtime
*could* do); this table + the README maturity table rate **agentlift's shipped implementation**.
Bedrock has **two primitives** behind `--mode`, **both now live-verified**: the managed
**Harness** (config-only single agent, ✅ live deploy) and the custom-container **Runtime**
(multi-agent, ✅ live hosted deploy). The **Runtime's hosted create** used to be the sharpest
gap (build-only, hosted create refused) — it is now a **live `CreateAgentRuntime`** path
(ARM64 image → ECR → runtime → `InvokeAgentRuntime`), with **subagent delegation
receipt-verified on Nova** (the coordinator's top-level trace named both specialists). The
**Harness** is a complete **single-agent** live deploy — **6/6 cells verified by a committed
Nova receipt** (agent + base-session sandbox + remote MCP + S3-loaded skill +
`agentcore_browser`); it just can't represent *subagents* (a team routes to the Runtime).
Both map Claude natively; **receipts are on Nova** because Claude inference is Gate-A-gated (an
account entitlement, not a code gap). The AgentCore Harness feature is in AWS public preview.
Honest Runtime boundary: `InvokeAgentRuntime` returns the container's JSON body (not an event
stream), so coordinator/root tool calls are objective (PASS-EXERCISED) while **nested**
specialist skill/MCP calls are PASS-WIRED + text-corroborated. All true, not contradictory.

| | Anthropic | AWS Bedrock (`--target bedrock`) | Google (`--target google`) | OpenAI |
|---|---|---|---|---|
| Handoff | `deploy` (live, **full**) | `--mode harness` (**✅ live single-agent deploy**, 6/6 EXERCISED on a Nova receipt) · `--mode runtime` (**✅ live multi-agent hosted deploy** via `CreateAgentRuntime`; delegation EXERCISED on a Nova receipt; `--build-only` emits the ARM64 artifact) — AWS AgentCore in preview | `deploy` (live, **preview**) | `export` + self-host only |
| Subagents | native, per-agent IDs | emulated — runtime: one AgentCore runtime, Strands agents-as-tools in-model delegation (**live-proven**: coordinator's top-level trace named both specialists on a Nova receipt) · **harness is single-agent → multi-agent folders route to runtime** | emulated (one `reasoningEngine`, server-side delegation) | `as_tool`, loop in your app |
| Skills | uploaded, shared by id (skill-bearing agents auto-get `read` — Managed Agents needs it to open `SKILL.md`) | ✅ both primitives — runtime: embedded in source package (`Skill.from_file` + `AgentSkills`) · **harness: uploaded to `$AGENTLIFT_BEDROCK_S3_BUCKET`, attached via `skills[].s3.uri` (live-verified; exec role needs s3:ListBucket+GetObject)** | ✅ embedded in source package, loaded via ADK `load_skill_from_dir` (update = redeploy) | export comment only |
| Remote MCP | mapped | ✅ both primitives: URL → harness `remote_mcp` tool / runtime Strands `MCPClient` (streamable-HTTP) + `tool_filter`; inline auth → the deployed resource's `env_vars` (resolved at deploy, never inlined) | ✅ URL → ADK `McpToolset` + `tool_filter`; inline auth → Agent Engine `env_vars` (resolved at deploy, never inlined) | export comment only |
| Built-in web tools (`web_search`/`web_fetch`) | mapped | harness: ✅(preview) → `agentcore_browser` (`web_fetch` clean, `web_search` approximate → audit `degraded`) · runtime: 🚧 PLANNED | ✅ `web_search`→Google Search grounding, `web_fetch`→URL Context, each a wrapped single-tool ADK sub-agent (`AgentTool`, `propagate_grounding_metadata=True`); always-wrap so they coexist with `transfer_to_agent`; pins `google-adk>=1.34.3` | `WebSearchTool` / self-host fetch |
| Built-in sandbox tools (`bash/files/glob-grep`) | mapped | harness: ✅(preview) native base-session shell + file_operations · runtime: 🚧 PLANNED — a *real* AgentCore Code Interpreter (shell + FS) + Browser exist (audit: `emulated`, **not** a non-goal); not yet wired, expose via URL MCP meanwhile | 🚧 skipped (sandbox is Python/JS only — in-engine emulation is a **non-goal**; expose equivalents via a URL MCP server) | self-host runner |
| `:ask` | permission policy | ❌ unsupported on **both** primitives (Runtime `/invocations` + Harness invoke are both non-interactive; gate client-side) | 🚧 unsupported on `VertexAiSessionService` (gate client-side, or keep on Anthropic) | client-side |
| Idempotency | lockfile + content hashes | ✅ spec hash → create/update/skip: `.agentlift-harness.json` (harness) · `.agentlift-bedrock.json` (runtime) | ✅ `.agentlift-google.json` spec hash → create/update/skip | n/a |
| Model mapping | Claude (native) | ✅ **Claude-native mapping — no remap** (both primitives); `claude-*` → regional Bedrock inference profile (`eu.anthropic.claude-haiku-4-5-…` in `eu-north-1`, `us.anthropic.…` in `us-*`). The hosted Runtime composition + harness are **live-proven on Nova** (control plane, container, invoke, delegation); a same-Claude-brain inference receipt is **pending Gate A** (an account entitlement, not a code gap) | 🔁 mapped to Gemini (`gemini-2.5-flash`); Claude-on-Vertex is an offline-verified **spike, not shipped** (`experiments/claude-on-vertex/`) — a Claude `--google-model` is refused (`google.deploy_model.claude_unsupported`) | 🔁 mapped to `gpt-*` |

**Live-verified (6/6 both):** one neutral fixture (`tests/live/fixtures/coverage-matrix`) was deployed
+ queried on **both** Anthropic and Google; all six portability dimensions (agents · subagents ·
shared MCP · individual MCP · shared skill · individual skill) were **EXERCISED server-side** —
objective runtime events, not answer text. Anthropic's subagents cell keys on the native delegation
event (`session.thread_created` + `agent.thread_message_sent`) since coordinator delegation is async.
Committed receipts: `tests/live/receipts/20260604-012428-anthropic` + `20260604-004318-google`. The
WIRED layer is pinned offline in `tests/test_coverage_matrix_plan.py` (CI); the live harness is
`tests/live/coverage_matrix.py` (gated pytest wrapper: `tests/live/test_coverage_matrix.py`). See
[docs/tested-platforms.md](docs/tested-platforms.md). OpenAI stays `export`-only (no hosted engine).

**Built-in web tools — live-verified (Google).** A separate fixture (`tests/live/fixtures/web-tools`)
was deployed to its own `reasoningEngine`: both `web_search` (Google Search grounding) and `web_fetch`
(URL Context) **fired server-side**, proven by the wrapped-agent `function_call` + `function_response`
(the fetch returns a unique URL-served nonce verbatim — unforgeable from memory). One honest caveat
encoded in `tests/live/web_tools.py`: the inner grounding/url_context **metadata does not cross the
`AgentTool` → Agent-Engine `stream_query` boundary** (even with `propagate_grounding_metadata=True`),
so the objective signal is the tool-call + its response content, not citation chunks. Receipt:
`tests/live/receipts/20260604-115352-web-google`. Pinned offline in `tests/test_google_plan.py` +
`tests/test_google_codegen.py`.

**The Google divergence to remember:** `audit` reports each *platform's* capability;
`deploy --target google` reports *agentlift's current implementation*. These now agree on
skills, URL MCP, and the built-in **web** tools (all mapped). They still diverge on the
built-in **sandbox** tools and `:ask` (`audit` rates them `degraded`/`unsupported` for
Google; `deploy` skips a stdio MCP server / sandbox-tool-only folder). Those two are framed
as **non-goals with workarounds**, not parity TODOs (sandbox → expose via a URL MCP server;
`:ask` → gate client-side or keep on Anthropic — see
[docs/deploy-google.md](docs/deploy-google.md)). Pipeline for Google mirrors Anthropic's
*plan-is-the-contract* discipline: `google_plan.py` is pure and offline-tested, only
`google_target.py` touches the network.

**Claude-on-Vertex (spike, not shipped):** ADK 1.34.3 resolves Claude on Vertex and the
mixed-model shape composes (web sub-agents must stay Gemini — Search/URL-Context are Gemini
built-ins, encoded by `web_model()` in `google_codegen.py`). Offline-verified in
`experiments/claude-on-vertex/`; no live receipt yet, so `build_google_plan` **refuses** a
Claude `--google-model` (`google.deploy_model.claude_unsupported`) rather than silently
shipping it (the *confirm-live-before-encoding* rule).

**Bedrock status (two primitives behind `--mode`):** all four pure planners
(`build_bedrock_plan` for runtime, `build_harness_plan` for harness) are pure + offline-tested;
only `bedrock_target.py` / `harness_target.py` touch the network. `--mode auto` (default) routes
the least-powerful primitive that preserves semantics — a **single agent** (with its skills, MCP,
tools) → **harness**, a **multi-agent team** (subagents / >1 agent) → **runtime** — *never a
silent downgrade* (`select_bedrock_mode`). Skills no longer force the runtime (the harness uploads
them to S3).

- **Harness (✅ live single-agent deploy):** `--mode harness` *runs* `CreateHarness`/`UpdateHarness`/
  `InvokeHarness` over boto3 + IAM — config-only, no container, minutes. **6/6 single-agent cells
  live-verified** by a committed Nova receipt (`tests/live/receipts/20260605-121525-harness-bedrock`):
  agent + base-session sandbox (`shell`) + remote MCP (`docs_read_wiki_structure` — the harness
  surfaces MCP tools as `<server>_<tool>`) + an **S3-loaded skill** + `agentcore_browser`, all
  EXERCISED server-side. `_HARNESS_LIVE_VERIFIED = True`. **Skills**: `harness_target` uploads each
  bundle to `$AGENTLIFT_BEDROCK_S3_BUCKET` (SKILL.md directly under the prefix) and references it
  via `skills[].s3.uri`; the exec role needs `s3:ListBucket`+`s3:GetObject`. **MCP allowlist**: a
  *restrictive* `allowedTools` suppresses MCP surfacing in preview, so `_build_allowed_tools`
  returns `[]` and per-tool narrowing is surfaced as `bedrock.mcp.tool_filter_unenforced`. Honest
  notes (standing `bedrock.harness.preview` diagnostic): the AgentCore Harness *feature* is in AWS
  public preview, and Claude inference *runs* but is **Gate-A-gated** (eventually-consistent — it
  answered, then flapped to `ResourceNotFoundException`), so the model-agnostic receipt is on Nova.
  A `clientToken` quirk is handled: AWS rejects reusing a deterministic token whose resource was
  deleted, so the create retries once **without** the token.
- **Runtime (✅ live multi-agent hosted deploy):** `--mode runtime` *runs* the full hosted path —
  build the ARM64 container context, create the ECR repo + login + `docker buildx --platform
  linux/arm64 --push`, `CreateAgentRuntime` (PUBLIC network, HTTP `serverProtocol`, IAM-only — no
  JWT authorizer), poll READY, write `.agentlift-bedrock.json`, and `InvokeAgentRuntime`. Gated by
  `_RUNTIME_LIVE_VERIFIED` (**now True**) — a bare hosted create refused until a committed receipt.
  **Live-verified on Nova** (us-east-1): a multi-agent team (coordinator + 2 specialists) deployed +
  invoked — **create + agent + subagent DELEGATION all PASS-EXERCISED** (the coordinator's top-level
  trace was `['bug_finder', 'researcher']`), and a single-agent smoke got **remote_mcp PASS-EXERCISED**
  (an objective root-level `docs_read_wiki_structure` call). Receipts:
  `tests/live/receipts/20260605-134012-runtime-bedrock` (team) + `20260605-133821-runtime-bedrock`
  (smoke). **Honest boundary**: `InvokeAgentRuntime` returns the container's JSON body, not an event
  stream — the generated handler returns `{result, tool_calls?}` where `tool_calls` is the
  coordinator's *top-level* trace (`AgentResult.metrics.tool_metrics`, fail-open). So delegation +
  root-level skill/MCP are objective; **nested** specialist skill/MCP calls don't cross the
  `/invocations` boundary → PASS-WIRED + text-corroborated (the runtime analogue of the Google
  `AgentTool`→`stream_query` grounding-metadata caveat). The runtime execution role needs
  bedrock-agentcore trust (`aws:SourceAccount`), ECR pull, `bedrock:InvokeModel`, and CloudWatch Logs.
  `--build-only` still materializes just the ARM64 build context under `<path>/.agentlift-build/bedrock/`.

**Two one-time gates outside the code path:** Gate A = the Anthropic use-case form (Bedrock
console → Model access → Anthropic) — a per-account entitlement for Claude inference that is
**eventually consistent** (it cleared once in testing, then flapped back to
`ResourceNotFoundException`); applies only to Claude, **Nova sidesteps it** (cheapest first
harness receipt). Gate B = AWS IAM creds + execution role (+ ECR **for the runtime only**) for a
hosted create. The Strands **composition is proven live** on Amazon Nova Pro — both *locally*
(objective tool-call trace, bearer-token inference — `experiments/bedrock-composition/`) **and now
hosted** (the live Runtime receipt: delegation EXERCISED inside a real AgentCore Runtime). The
remaining pending item is the **same-Claude-brain inference receipt** (Gate A), not a code gap. The
model map is **native** for both primitives (no Gemini-style remap): folder `claude-*` →
`<region_prefix>.anthropic.<slug>` (eu-*→eu, us-*→us, ap-*→apac, else→global). A non-Claude folder
id (e.g. `us.amazon.nova-pro-v1:0`, used for the Nova receipts) passes through verbatim. Region
defaults differ (harness `us-west-2` preview, runtime `eu-north-1`; the receipts above used
`us-east-1`); region flows into the inference-profile prefix, so a region change forces a fresh
create. See [docs/deploy-bedrock.md](docs/deploy-bedrock.md).

## Commands

```bash
agentlift validate <path>              # parse + plan, report problems (exit 1 on errors)
agentlift plan     <path> [--json] [--target anthropic|bedrock|google] [--mode auto|harness|runtime] [--google-model M]  # deterministic deploy plan, no network
agentlift audit    <path> --targets anthropic,bedrock,google,openai
agentlift export   <target> <path> [--out DIR]   # anthropic-yaml | bedrock-strands | google-adk | openai-agents
agentlift diff     <path> [--remote]
agentlift deploy   <path> [--target anthropic|bedrock|google] [--mode auto|harness|runtime] [--build-only] [--bedrock-region R] [--prune] [-y]   # bedrock: both --mode harness AND --mode runtime deploy live (AgentCore preview); --build-only just emits the runtime artifact
agentlift import   <anthropic|bedrock> <out> [--agent N ...] [--mode harness] [--harness-id|--harness-name X] [--bedrock-region R] [--dry-run]   # read-only inverse of deploy; self-verifies (Round-trip OK). bedrock --mode runtime refuses (opaque container)
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

## 🔒 Anonymize Google identifiers before every commit (MANDATORY)

**Real Google Cloud identifiers must never be committed. Replace them with `****` (or
`********`) in every tracked file before staging.** This applies to the project ID
(`gen-lang-client-…`), the project number (the long numeric id in a resource path), the
`reasoningEngine` numeric id, and the staging bucket name. It is the one rule that gates a
commit — live testing writes real values into state/receipt files, so re-anonymize as the
last step before `git add`.

- **Where they leak:** `tests/live/receipts/_state-*.json`, `tests/live/receipts/<ts>-*/receipt.json`
  (`resource_name`, `project`, the project-number in `projects/<num>/…`), any console paste in a
  `.md`. Anthropic ids (`agent_…`, `skill_…`) are not secret, but Google project ids/numbers are.
- **How:** redact to `projects/********/locations/<loc>/reasoningEngines/********`, `project: "****"`,
  bucket `gs://****`. Keep the *location* (`us-central1`) and the spec-hash — they are not identifying.
- **Secrets stay out entirely:** MCP auth header *values* resolve from the deployer's local env at
  deploy time into runtime `env_vars` (Agent Engine / AgentCore Runtime); only the env-var *name* is
  ever written to source, plan, or lockfile. Never inline a secret. `.env` is gitignored.
- **AWS Bedrock identifiers, same rule:** the **Bedrock bearer token** (`AWS_BEARER_TOKEN_BEDROCK`,
  starts `ABSK…`) is a secret — never commit or echo it (`.env` only). Redact any **AWS account id**
  (12-digit), **ECR registry** (`<acct>.dkr.ecr.<region>.amazonaws.com`), and **AgentCore runtime
  *and harness* ARN/id** (`arn:aws:bedrock-agentcore:<region>:<acct>:harness/…`, the `harnessId`) to
  `****` in tracked files. The build-only artifact uses `<acct>` placeholders by design; the model id
  / region / spec-hash are not identifying and may stay. Live state can leak real values into
  `.agentlift-build/`, **`.agentlift-harness.json`** (the harness lock — `harness_arn`/`harness_id`),
  and any console paste — re-anonymize before staging.
- **Sanity check before committing:** `git grep -nE "gen-lang-client-|reasoningEngines/[0-9]|projects/[0-9]{6,}|[0-9]{12}\.dkr\.ecr|[0-9]{12}:harness/|bedrock-agentcore:[a-z0-9-]+:[0-9]{12}|AWS_BEARER_TOKEN_BEDROCK=.+|ABSK[A-Za-z0-9]"`
  must return nothing real in tracked files.

## Key docs

- [docs/convention.md](docs/convention.md) — the `.managed-agents/` spec
- [docs/import.md](docs/import.md) — `agentlift import` (the reverse pipeline): provider → folder, the round-trip + migration story, what can't round-trip (one-way losses as diagnostics)
- [docs/anthropic-mapping.md](docs/anthropic-mapping.md) — exact local → Managed Agents field mapping
- [docs/deploy-bedrock.md](docs/deploy-bedrock.md) — AWS Bedrock AgentCore: the two primitives (`--mode harness` live · `--mode runtime` live hosted deploy / `--build-only`), bearer-token vs IAM, the two gates, the `/invocations` trace boundary, MCP-auth env_vars
- [docs/deploy-google.md](docs/deploy-google.md) — Google ADC/credentials/setup
- [docs/provider-matrix.md](docs/provider-matrix.md) — row-by-row capability matrix across all four runtimes
- [docs/tested-platforms.md](docs/tested-platforms.md) — per-platform live test receipts
- [docs/how-it-works.md](docs/how-it-works.md), [docs/deploying.md](docs/deploying.md), [docs/limitations.md](docs/limitations.md)
- **External single source of truth for "real vs roadmap":** the author's `IMPLEMENTATION-STATUS.md` (kept in sync with README/article). Version is **0.6.0**.
