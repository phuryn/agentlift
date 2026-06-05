# Provider capability matrix

One neutral `.managed-agents/` folder, four managed-agent runtimes. This is the
row-by-row reference for **what maps where** — the detailed companion to the README's
summary. It is generated from the same source of truth the tooling uses:
[`src/agentlift/capabilities.py`](../src/agentlift/capabilities.py), which `agentlift
audit` and `agentlift export` both read.

**Two axes, kept distinct — don't let them collide:**

- The **audit capability matrix** ([`capabilities.py`](../src/agentlift/capabilities.py),
  what `agentlift audit` prints) rates the **target platform**: *can this runtime represent
  the concept?* (`native`/`emulated`/`degraded`/`unsupported`).
- The **maturity / mapping** shown below and in the README's provider table rates
  **agentlift's shipped implementation**: *what does the compiler ship end-to-end today?*

These agree on almost everything. Where they differ it's called out. Bedrock has **two
primitives** behind `--mode`, and **both now deploy live** (the cells below note where they
diverge): the managed **Harness** (config-only single agent) and the custom-container
**Runtime** (multi-agent). The audit rates hosted runtime `native` (AgentCore genuinely hosts
it) and agentlift now **matches** it — `--mode runtime` is a **live hosted multi-agent deploy**
(build the ARM64 image → ECR → `CreateAgentRuntime` → `InvokeAgentRuntime`), gated by
`_RUNTIME_LIVE_VERIFIED` (now True); `--build-only` still emits just the artifact. The
**Harness** is the single-agent path — it deploys a single agent **live** (IAM-only, no
container), and **6/6 single-agent cells are EXERCISED** on a committed Nova receipt (agent +
base-session sandbox + remote MCP + S3-loaded skill + `agentcore_browser`). The split is by
shape: a **single agent** goes to the Harness, a **multi-agent team** (subagents) to the
Runtime. (The AgentCore Harness *feature* is in AWS public preview, and Claude inference is
Gate-A-gated, so the wire-shape receipts are on Nova.) So "AgentCore hosting is native"
(platform) and "both agentlift Bedrock primitives deploy live" (implementation) are now
aligned.

**Legend:** ✅ native / maps 1:1 · 🟡 live, hosted preview · 🔁 translated to a different shape
(export, or model remap) · 🚧 not wired / not enforced yet (surfaced as a diagnostic, never a
silent drop) · ❌ refused / not applicable.

AWS Bedrock AgentCore is split into its **two primitives** as separate columns — they differ on
nearly every axis (managed single-agent vs custom multi-agent container), but **both now deploy
live**:

| Capability | Anthropic Managed Agents | AWS **Harness** (`--mode harness`) | AWS **Runtime** (`--mode runtime`) | Google (`--target google`) | OpenAI |
|---|---|---|---|---|---|
| **Handoff** | ✅ `deploy` (live, full) | ✅ live single-agent deploy, **6/6 EXERCISED** on a committed Nova receipt (AWS feature in preview) | 🟡 live hosted **multi-agent** deploy — build ARM64 → ECR → `CreateAgentRuntime` → `InvokeAgentRuntime` (preview); `--build-only` emits just the artifact | 🟡 `deploy` (live, preview) | 🔁 `export` + self-host |
| **Agents** | ✅ live, per-agent IDs | ✅ one managed agent (live) | ✅ one AgentCore Runtime (live) | ✅ live (one `reasoningEngine`) | 🔁 export |
| **Subagents** | ✅ native, server-side coordinator | ❌ **single-agent** (no sub-agent tool type) — a multi-agent *team* routes to Runtime | ✅ agents-as-tools, one runtime (in-model delegation) — **live-proven**: a Nova receipt's coordinator top-level trace named both specialists (`['bug_finder','researcher']`)¹ | ✅ server-side delegation (ADK `sub_agents`, one engine) | 🔁 `as_tool`, loop runs in your app |
| **Shared skill** | ✅ uploaded once, shared by id | ✅ uploaded to S3, attached via `skills[].s3.uri` (**EXERCISED** live — the bundle loads + applies) | ✅ embedded in source pkg, Strands `Skill.from_file` + `AgentSkills` — root-level use objective; nested-in-a-specialist wired + text-corroborated¹ | ✅ embedded in source pkg, ADK `load_skill_from_dir` | 🔁 Skills-API scaffold (self-host) |
| **Private skill** | ✅ | ✅ same (one agent → all its skills; cross-agent *scoping* needs ≥2 agents → Runtime) | ✅ | ✅ | 🔁 scaffold |
| **Shared MCP (URL)** | ✅ mapped | ✅ `remote_mcp` tool (URL + headers); tools surface as `<server>_<tool>` (**EXERCISED** live). Per-tool `allowedTools` narrowing isn't enforced in preview (diagnosed) | ✅ Strands `MCPClient` (streamable-HTTP) + `tool_filter` — a single-agent smoke got `remote_mcp` **PASS-EXERCISED** (root-level `docs_read_wiki_structure`); nested-in-a-specialist wired + text-corroborated¹ | ✅ ADK `McpToolset` + `tool_filter` | 🔁 `HostedMCPTool` scaffold |
| **Private MCP (URL)** | ✅ | ✅ (same — `remote_mcp` tool, EXERCISED) | ✅ | ✅ | 🔁 scaffold |
| **MCP inline auth** | 🚧 dropped (diagnostic) | ✅ resolved to harness `env_vars` (never inlined) | ✅ resolved to runtime `env_vars` (never inlined) | ✅ resolved to Agent Engine `env_vars` (never inlined) | 🚧 scaffold |
| **stdio MCP** | ❌ refused (host behind HTTPS) | ❌ refused (host behind HTTPS) | ❌ refused (host behind HTTPS) | ❌ refused (host behind HTTPS) | ❌ n/a |
| **Built-in web tools** (`web_search`/`web_fetch`) | ✅ mapped | 🟡 `web_fetch`→`agentcore_browser` (session-based; **EXERCISED** live); `web_search` approximate | 🚧 PLANNED — not yet wired on the Runtime | 🟡 mapped — `web_search`→Google Search grounding, `web_fetch`→URL Context | 🔁 `WebSearchTool` / self-host fetch |
| **Built-in sandbox tools** (`bash/files/glob-grep`) | ✅ mapped | ✅ base-session shell + file_operations (**EXERCISED** live — both fired) | 🚧 PLANNED — real AgentCore Code Interpreter (audit: `emulated`), not yet wired on the Runtime | 🚧 skipped — Vertex sandbox is Python/JS only | 🔁 self-host runner |
| **`:ask` per-tool** | ✅ permission policy | ❌ no interactive approval channel (invoke is request/response) | ❌ no interactive approval channel (`/invocations` is request/response) | 🚧 not enforced on `VertexAiSessionService` | 🔁 client-side (your runner) |
| **Idempotency** | ✅ lockfile + content hashes | ✅ spec hash → `.agentlift-harness.json` | ✅ spec hash → `.agentlift-bedrock.json` (written on a live deploy → create/update/skip) | ✅ `.agentlift-google.json` spec hash | ❌ n/a |
| **Model mapping** | ✅ Claude (native) | ✅ **Claude-native, no remap** — wire shape verified on Nova; Claude-invoke Gate-A-gated | ✅ **Claude-native, no remap** — regional inference profile; receipts on Nova because Claude-invoke is Gate-A-gated (account entitlement, not a code gap). A non-Claude id (`us.amazon.nova-pro-v1:0`) passes through verbatim | 🔁 Gemini (`gemini-2.5-flash`, override with `--google-model`) | 🔁 `gpt-*` |

¹ **The `/invocations` trace boundary.** `InvokeAgentRuntime` returns the container's JSON body,
not a tool-event stream, so the handler surfaces the coordinator's **top-level** `tool_calls`
(from `AgentResult.metrics.tool_metrics`). Delegation and root-level tools are therefore
**objective** (PASS-EXERCISED); **nested** specialist skill/MCP calls are **PASS-WIRED +
text-corroborated** — the direct analogue of the Google `AgentTool`→`stream_query` grounding-
metadata caveat noted below.

## How to read the non-obvious cells

- **Handoff (Bedrock) — two primitives behind `--mode`.** `auto` (the default) picks the
  least-powerful primitive that preserves the folder's semantics, **never a silent downgrade**:
  a **single agent** (with its skills, MCP, and tools) goes to the managed **Harness**; a
  **multi-agent team** (subagents) routes to the **Runtime**.
  - **`--mode harness`** deploys a config-only managed single agent **live** — IAM + an execution
    role, no container, minutes. Its `CreateHarness`/`InvokeHarness` wire shape is **live-verified**
    (committed Nova receipt, 6/6 single-agent cells); skills upload to S3
    (`$AGENTLIFT_BEDROCK_S3_BUCKET`) and attach via `skills[].s3.uri`. The AgentCore Harness feature
    is in AWS public preview.
  - **`--mode runtime`** is now a **live hosted multi-agent deploy**: it compiles the Strands
    package, builds the ARM64 AgentCore Runtime image, pushes it to ECR, calls `CreateAgentRuntime`
    (or update/skip via the `.agentlift-bedrock.json` spec hash), and invokes it with
    `InvokeAgentRuntime`. The hosted create wire shape is live-verified (`_RUNTIME_LIVE_VERIFIED`),
    so the bare deploy no longer refuses. `--mode runtime --build-only` still emits just the
    artifact — the complete ARM64 container build context (image + Dockerfile + a `NOTES.txt`
    runbook) — without touching AWS.

  See [deploy-bedrock.md](deploy-bedrock.md).
- **Model mapping (Bedrock) — the headline.** Bedrock exposes Claude as a native model family,
  so unlike Google agentlift emits the Bedrock Claude inference-profile ID directly rather than
  remapping to another model: a folder's `claude-haiku-4-5` maps to its regional Bedrock
  inference profile (`eu.anthropic.claude-haiku-4-5-20251001-v1:0` in `eu-north-1`; the region
  prefix follows the deploy region). This is a **mapping fact** — the compiler does no
  Gemini-style substitution — not a proven-inference claim. End-to-end Claude composition on
  Bedrock is **pending stable Gate A entitlement** (the per-account **Anthropic use-case form**,
  eventually consistent) and per-region availability; that's an *account entitlement, not a code
  gap*, so the live receipts are on Amazon Nova (model-agnostic). A non-Claude id like
  `us.amazon.nova-pro-v1:0` passes through **verbatim** — no remap either way. The Strands
  composition is itself live-proven on Nova (see
  [tested-platforms.md](tested-platforms.md#amazon-bedrock-agentcore-runtime--harness)).
- **MCP inline auth.** Anthropic's managed URL-MCP shape carries no credentials, so an
  inline auth header is **dropped with a warning** — the server must be public or
  self-authenticating. Both Bedrock and Google **carry it**: the header *value* resolves
  from the deployer's local environment at deploy time into a runtime `env_var`; only the
  env-var *name* is ever written into the plan, source, or lockfile.
- **MCP per-tool filtering — where it's enforced.** The caveat that per-tool MCP filtering
  isn't enforced applies **only to the direct `remote_mcp` attachment** path (the Harness's
  preview `allowedTools`, and a Runtime `tool_filter` that scopes client-side). For an AgentCore
  **Gateway**-fronted MCP server, tool scoping is enforced at the Gateway/Policy layer
  server-side — that's **AWS-documented Gateway behavior**, not something agentlift has
  live-verified, so treat it as a platform property of Gateway, not an agentlift-proven claim.
- **stdio MCP.** A hosted engine can't spawn a local subprocess, so a `command:`/`npx`
  server is refused on all three deploy targets. Host it behind an HTTPS URL first.
- **Built-in sandbox tools — Bedrock vs Google differ, and the two Bedrock primitives differ.**
  This is the one row where Bedrock is *more* capable than Google: AgentCore offers a **real**
  sandbox (the Code Interpreter with shell + filesystem, plus a Browser tool), so the audit rates
  it `emulated` (platform-capable), not `degraded`. The **Harness** base session ships shell +
  `file_operations` natively, so a harness deploy maps the sandbox built-ins directly — **live-
  confirmed**: in the committed Nova receipt the agent invoked both `shell` and `file_operations`.
  The **Runtime** deploys live, but does not wire the Code Interpreter **yet**
  (a `PLANNED` diagnostic, never a silent drop). Google's hosted sandbox is genuinely Python/JS
  only — no shell, no workspace — so there it is `degraded` and skipped; on Google the workaround
  is a URL MCP server.
- **Built-in web tools (Google).** `web_search` and `web_fetch` *do* map on Google: deploy
  lowers each as a dedicated single-tool ADK sub-agent — `web_search`→`GoogleSearchTool()`
  (Gemini's Google Search grounding), `web_fetch`→`url_context` (URL Context) — wrapped in
  an `AgentTool` with `propagate_grounding_metadata=True`. On the Bedrock **Runtime** they are
  `PLANNED` — the Runtime deploys live, but web/sandbox built-ins aren't wired there yet (the
  Harness already maps `web_fetch`→`agentcore_browser`). On Anthropic both are native built-ins.
- **`:ask`.** Native on Anthropic. On Google it's not enforced under `VertexAiSessionService`
  (a diagnostic). On Bedrock **neither primitive** has an interactive approval channel — the
  Runtime's hosted `/invocations` and the Harness invoke are both request/response — so it's
  `unsupported`; enforce approval **client-side**, or keep `:ask` agents on Anthropic.
- **Subagents (per-agent IDs).** Anthropic gives each agent its own addressable id; Bedrock
  and Google both deploy the whole roster as **one** runtime with in-runtime delegation
  (Strands agents-as-tools / ADK server-side `transfer_to_agent`), so the roster is not
  individually addressable. (Deploy specialists as separate runtimes — or A2A across Google
  deployments — for per-agent ids.) On the Bedrock **Runtime** this delegation is now
  **live-proven**: a Nova receipt's coordinator top-level trace named both specialists
  (`['bug_finder','researcher']`). The delegation event and root-level tools are objective; a
  specialist's *nested* skill/MCP calls are wired + text-corroborated, because `InvokeAgentRuntime`
  returns the container's JSON body (top-level `tool_calls` only), not a full tool-event stream —
  see footnote ¹.
- **OpenAI.** There is no code-define + OpenAI-host path, so OpenAI is an `export` target,
  never a `deploy` target. Agents + subagents are real (`as_tool` composition,
  trace-verified); skills and MCP compile to guided self-host scaffolding.

## What's been exercised live

This matrix is the *capability* reference — what the compiler maps. For *receipt evidence*
of what actually ran, see [`tested-platforms.md`](tested-platforms.md): all six portability
dimensions on a deployed engine for **both Anthropic and Google** (classified by objective
runtime events). The **Bedrock** story now has **two live paths**:

- The **Harness** single-agent live deploy is **verified end-to-end by a committed Nova receipt**
  ([`20260605-121525-harness-bedrock`](../tests/live/receipts/)) — **6/6 single-agent cells
  EXERCISED** server-side: agent + base-session sandbox (`shell`) + remote MCP
  (`docs_read_wiki_structure`, surfaced as `<server>_<tool>`) + S3-loaded skill + `agentcore_browser`.
- The **Runtime** is now a **live hosted multi-agent deploy** (`CreateAgentRuntime` →
  `InvokeAgentRuntime`), not build-only. Its multi-agent **delegation is live-proven on Nova**: the
  coordinator's top-level trace named both specialists (`['bug_finder','researcher']`), and a
  single-agent smoke got `remote_mcp` PASS-EXERCISED at the root level
  (`docs_read_wiki_structure`). The honest boundary: `InvokeAgentRuntime` returns the container's
  JSON body, so only the coordinator's **top-level** `tool_calls` are objective — **nested**
  specialist skill/MCP calls are PASS-WIRED + text-corroborated (footnote ¹). Web/sandbox built-ins
  on the Runtime remain `PLANNED` (not yet wired).

Claude inference runs on both primitives but is Gate-A-gated (an account entitlement, not a code
gap), so the wire-shape receipts are on Nova (model-agnostic). For the honest constraints and
non-goals, see
[`limitations.md`](limitations.md). The exact Anthropic field-level mapping is in
[`anthropic-mapping.md`](anthropic-mapping.md).
