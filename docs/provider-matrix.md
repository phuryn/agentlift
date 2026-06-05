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
primitives** behind `--mode` (the cells below note both where they diverge): the managed
**Harness** (config-only single agent) and the custom-container **Runtime** (multi-agent). The
sharpest case is the **Runtime's hosted create**: the audit rates hosted runtime `native`
(AgentCore genuinely hosts it), but agentlift ships only the **build-only** artifact for it and
*refuses* the unverified hosted-create call. The **Harness** narrows that gap for a single
agent — it deploys a single agent **live** (IAM-only, no container), and **6/6 single-agent cells
are EXERCISED** on a committed Nova receipt (agent + base-session sandbox + remote MCP + S3-loaded
skill + `agentcore_browser`). The only thing it can't represent is a **multi-agent team**
(subagents) — that goes to the Runtime. (The AgentCore Harness *feature* is in AWS public preview,
and Claude inference is Gate-A-gated, so the wire-shape receipt is on Nova.) So "AgentCore hosting
is native" (platform), "agentlift's Bedrock *runtime* deploy is build-only" and "the Harness
deploys a single agent live" (implementation) are all true and not contradictory.

**Legend:** ✅ native / maps 1:1 · 🟡 live, hosted preview · 🟠 build-only preview
(compiles + builds a deployable artifact; hosted create is manual until live-verified) ·
🔁 translated to a different shape (export, or model remap) · 🚧 not wired / not enforced yet
(surfaced as a diagnostic, never a silent drop) · ❌ refused / not applicable.

AWS Bedrock AgentCore is split into its **two primitives** as separate columns — they differ on
nearly every axis (managed single-agent vs custom multi-agent container; live vs build-only):

| Capability | Anthropic Managed Agents | AWS **Harness** (`--mode harness`) | AWS **Runtime** (`--mode runtime`) | Google (`--target google`) | OpenAI |
|---|---|---|---|---|---|
| **Handoff** | ✅ `deploy` (live, full) | ✅ live single-agent deploy, **6/6 EXERCISED** on a committed Nova receipt (AWS feature in preview) | 🟠 `--build-only` container; hosted create manual (Gate B) | 🟡 `deploy` (live, preview) | 🔁 `export` + self-host |
| **Agents** | ✅ live, per-agent IDs | ✅ one managed agent (live) | ✅ compiled to one AgentCore Runtime (build-only) | ✅ live (one `reasoningEngine`) | 🔁 export |
| **Subagents** | ✅ native, server-side coordinator | ❌ **single-agent** (no sub-agent tool type) — a multi-agent *team* routes to Runtime | ✅ agents-as-tools, one runtime (in-model delegation) | ✅ server-side delegation (ADK `sub_agents`, one engine) | 🔁 `as_tool`, loop runs in your app |
| **Shared skill** | ✅ uploaded once, shared by id | ✅ uploaded to S3, attached via `skills[].s3.uri` (**EXERCISED** live — the bundle loads + applies) | ✅ embedded in source pkg, Strands `Skill.from_file` + `AgentSkills` | ✅ embedded in source pkg, ADK `load_skill_from_dir` | 🔁 Skills-API scaffold (self-host) |
| **Private skill** | ✅ | ✅ same (one agent → all its skills; cross-agent *scoping* needs ≥2 agents → Runtime) | ✅ | ✅ | 🔁 scaffold |
| **Shared MCP (URL)** | ✅ mapped | ✅ `remote_mcp` tool (URL + headers); tools surface as `<server>_<tool>` (**EXERCISED** live). Per-tool `allowedTools` narrowing isn't enforced in preview (diagnosed) | ✅ Strands `MCPClient` (streamable-HTTP) + `tool_filter` | ✅ ADK `McpToolset` + `tool_filter` | 🔁 `HostedMCPTool` scaffold |
| **Private MCP (URL)** | ✅ | ✅ (same — `remote_mcp` tool, EXERCISED) | ✅ | ✅ | 🔁 scaffold |
| **MCP inline auth** | 🚧 dropped (diagnostic) | ✅ resolved to harness `env_vars` (never inlined) | ✅ resolved to runtime `env_vars` (never inlined) | ✅ resolved to Agent Engine `env_vars` (never inlined) | 🚧 scaffold |
| **stdio MCP** | ❌ refused (host behind HTTPS) | ❌ refused (host behind HTTPS) | ❌ refused (host behind HTTPS) | ❌ refused (host behind HTTPS) | ❌ n/a |
| **Built-in web tools** (`web_search`/`web_fetch`) | ✅ mapped | 🟡 `web_fetch`→`agentcore_browser` (session-based; **EXERCISED** live); `web_search` approximate | 🚧 PLANNED | 🟡 mapped — `web_search`→Google Search grounding, `web_fetch`→URL Context | 🔁 `WebSearchTool` / self-host fetch |
| **Built-in sandbox tools** (`bash/files/glob-grep`) | ✅ mapped | ✅ base-session shell + file_operations (**EXERCISED** live — both fired) | 🚧 PLANNED — real AgentCore Code Interpreter (audit: `emulated`), not yet wired | 🚧 skipped — Vertex sandbox is Python/JS only | 🔁 self-host runner |
| **`:ask` per-tool** | ✅ permission policy | ❌ no interactive approval channel (invoke is request/response) | ❌ no interactive approval channel (`/invocations` is request/response) | 🚧 not enforced on `VertexAiSessionService` | 🔁 client-side (your runner) |
| **Idempotency** | ✅ lockfile + content hashes | ✅ spec hash → `.agentlift-harness.json` | ✅ spec hash → `.agentlift-bedrock.json` | ✅ `.agentlift-google.json` spec hash | ❌ n/a |
| **Model mapping** | ✅ Claude (native) | ✅ **Claude-native, no remap** — wire shape verified on Nova; Claude-invoke Gate-A-gated | ✅ **Claude-native, no remap** — regional inference profile | 🔁 Gemini (`gemini-2.5-flash`, override with `--google-model`) | 🔁 `gpt-*` |

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
  - **`--mode runtime --build-only`** compiles a Strands package and builds a complete ARM64
    AgentCore Runtime container artifact (image + Dockerfile + a `NOTES.txt` runbook). A *bare*
    `--mode runtime` hosted deploy **refuses** — it raises before any AWS call and writes nothing —
    because the AgentCore Runtime control-plane create wire shape is not live-verified here (the same
    *confirm-live-before-encoding* rule that keeps Claude-on-Vertex an offline spike).

  See [deploy-bedrock.md](deploy-bedrock.md).
- **Model mapping (Bedrock) — the headline.** Bedrock exposes Claude as a native model family,
  so unlike Google agentlift emits the Bedrock Claude inference-profile ID directly rather than
  remapping to another model: a folder's `claude-haiku-4-5` maps to its regional Bedrock
  inference profile (`eu.anthropic.claude-haiku-4-5-20251001-v1:0` in `eu-north-1`; the region
  prefix follows the deploy region). This is a **mapping fact** — the compiler does no
  Gemini-style substitution — not a proven-inference claim. End-to-end Claude composition on
  Bedrock is **pending stable Gate A entitlement** (the per-account **Anthropic use-case form**,
  eventually consistent) and per-region availability; the Strands composition is itself
  live-proven on Amazon Nova (see
  [tested-platforms.md](tested-platforms.md#amazon-bedrock-agentcore-runtime--harness)).
- **MCP inline auth.** Anthropic's managed URL-MCP shape carries no credentials, so an
  inline auth header is **dropped with a warning** — the server must be public or
  self-authenticating. Both Bedrock and Google **carry it**: the header *value* resolves
  from the deployer's local environment at deploy time into a runtime `env_var`; only the
  env-var *name* is ever written into the plan, source, or lockfile.
- **stdio MCP.** A hosted engine can't spawn a local subprocess, so a `command:`/`npx`
  server is refused on all three deploy targets. Host it behind an HTTPS URL first.
- **Built-in sandbox tools — Bedrock vs Google differ, and the two Bedrock primitives differ.**
  This is the one row where Bedrock is *more* capable than Google: AgentCore offers a **real**
  sandbox (the Code Interpreter with shell + filesystem, plus a Browser tool), so the audit rates
  it `emulated` (platform-capable), not `degraded`. The **Harness** base session ships shell +
  `file_operations` natively, so a harness deploy maps the sandbox built-ins directly — **live-
  confirmed**: in the committed Nova receipt the agent invoked both `shell` and `file_operations`.
  The **Runtime** path does not wire the Code Interpreter **yet**
  (a `PLANNED` diagnostic, never a silent drop). Google's hosted sandbox is genuinely Python/JS
  only — no shell, no workspace — so there it is `degraded` and skipped; on Google the workaround
  is a URL MCP server.
- **Built-in web tools (Google).** `web_search` and `web_fetch` *do* map on Google: deploy
  lowers each as a dedicated single-tool ADK sub-agent — `web_search`→`GoogleSearchTool()`
  (Gemini's Google Search grounding), `web_fetch`→`url_context` (URL Context) — wrapped in
  an `AgentTool` with `propagate_grounding_metadata=True`. On Bedrock they are `PLANNED`
  (no hosted `web_search` primitive; `web_fetch` can map to the Browser tool). On Anthropic
  both are native built-ins.
- **`:ask`.** Native on Anthropic. On Google it's not enforced under `VertexAiSessionService`
  (a diagnostic). On Bedrock **neither primitive** has an interactive approval channel — the
  Runtime's hosted `/invocations` and the Harness invoke are both request/response — so it's
  `unsupported`; enforce approval **client-side**, or keep `:ask` agents on Anthropic.
- **Subagents (per-agent IDs).** Anthropic gives each agent its own addressable id; Bedrock
  and Google both deploy the whole roster as **one** runtime with in-runtime delegation
  (Strands agents-as-tools / ADK server-side `transfer_to_agent`), so the roster is not
  individually addressable. (Deploy specialists as separate runtimes — or A2A across Google
  deployments — for per-agent ids.)
- **OpenAI.** There is no code-define + OpenAI-host path, so OpenAI is an `export` target,
  never a `deploy` target. Agents + subagents are real (`as_tool` composition,
  trace-verified); skills and MCP compile to guided self-host scaffolding.

## What's been exercised live

This matrix is the *capability* reference — what the compiler maps. For *receipt evidence*
of what actually ran, see [`tested-platforms.md`](tested-platforms.md): all six portability
dimensions on a deployed engine for **both Anthropic and Google** (classified by objective
runtime events). The **Bedrock** story has two paths: the **Harness** single-agent live deploy is
**verified end-to-end by a committed Nova receipt** ([`20260605-121525-harness-bedrock`](../tests/live/receipts/))
— **6/6 single-agent cells EXERCISED** server-side: agent + base-session sandbox (`shell`) + remote
MCP (`docs_read_wiki_structure`, surfaced as `<server>_<tool>`) + S3-loaded skill + `agentcore_browser`.
It proves only the *single-agent* cells — a multi-agent *team* (subagents + cross-agent scoping)
routes to the **Runtime**, whose hosted create stays **build-only by design** (so no AWS cell in the
multi-agent *6-cell* matrix is EXERCISED; the Strands multi-agent composition is separately exercised
live on **Amazon Nova** in a local experiment). Claude inference runs in the harness but is
Gate-A-gated, so the wire-shape receipt is on Nova (model-agnostic). For the honest constraints and
non-goals, see
[`limitations.md`](limitations.md). The exact Anthropic field-level mapping is in
[`anthropic-mapping.md`](anthropic-mapping.md).
