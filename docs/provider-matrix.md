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

These agree on almost everything. Where they differ it's called out. The sharpest case is
**Bedrock's hosted runtime**: the audit rates it `native` (AgentCore genuinely hosts an
agent runtime), but agentlift currently ships only the **build-only** artifact path — it
materializes the deployable container and *refuses* the unverified hosted-create call. So
"AgentCore hosting is native" (platform) and "agentlift's Bedrock deploy is build-only
preview" (implementation) are both true and not contradictory.

**Legend:** ✅ native / maps 1:1 · 🟡 live, hosted preview · 🟠 build-only preview
(compiles + builds a deployable artifact; hosted create is manual until live-verified) ·
🔁 translated to a different shape (export, or model remap) · 🚧 not wired / not enforced
yet (surfaced as a diagnostic, never a silent drop) · ❌ refused / not applicable.

| Capability | Anthropic Managed Agents | AWS Bedrock AgentCore (`--target bedrock`) | Google (`--target google`) | OpenAI |
|---|---|---|---|---|
| **Handoff** | ✅ `deploy` (live, full) | 🟠 `deploy --build-only` (container artifact); hosted create manual | 🟡 `deploy` (live, preview) | 🔁 `export` + self-host |
| **Agents** | ✅ live, per-agent IDs | ✅ compiled to one AgentCore Runtime | ✅ live (one `reasoningEngine`) | 🔁 export |
| **Subagents** | ✅ native, server-side coordinator | ✅ agents-as-tools, one runtime (in-model delegation) | ✅ server-side delegation (ADK `sub_agents`, one engine) | 🔁 `as_tool`, loop runs in your app |
| **Shared skill** | ✅ uploaded once, shared by id | ✅ embedded in source pkg, Strands `Skill.from_file` + `AgentSkills` | ✅ embedded in source pkg, ADK `load_skill_from_dir` | 🔁 Skills-API scaffold (self-host) |
| **Private skill** | ✅ | ✅ | ✅ | 🔁 scaffold |
| **Shared MCP (URL)** | ✅ mapped | ✅ Strands `MCPClient` (streamable-HTTP) + `tool_filter` | ✅ ADK `McpToolset` + `tool_filter` | 🔁 `HostedMCPTool` scaffold |
| **Private MCP (URL)** | ✅ | ✅ | ✅ | 🔁 scaffold |
| **MCP inline auth** | 🚧 dropped (diagnostic) | ✅ resolved to AgentCore Runtime `env_vars` (never inlined) | ✅ resolved to Agent Engine `env_vars` (never inlined) | 🚧 scaffold |
| **stdio MCP** | ❌ refused (host behind HTTPS) | ❌ refused (host behind HTTPS) | ❌ refused (host behind HTTPS) | ❌ n/a |
| **Built-in web tools** (`web_search`/`web_fetch`) | ✅ mapped | 🚧 PLANNED — `web_fetch`→Browser; no hosted `web_search` primitive | 🟡 mapped — `web_search`→Google Search grounding, `web_fetch`→URL Context | 🔁 `WebSearchTool` / self-host fetch |
| **Built-in sandbox tools** (`bash/files/glob-grep`) | ✅ mapped | 🚧 PLANNED — real AgentCore Code Interpreter + Browser (audit: `emulated`), not yet wired | 🚧 skipped — Vertex sandbox is Python/JS only | 🔁 self-host runner |
| **`:ask` per-tool** | ✅ permission policy | ❌ no approval channel on hosted `/invocations` | 🚧 not enforced on `VertexAiSessionService` | 🔁 client-side (your runner) |
| **Idempotency** | ✅ lockfile + content hashes | ✅ `.agentlift-bedrock.json` spec hash → create / update / skip (lock ready) | ✅ `.agentlift-google.json` spec hash → create / update / skip | ❌ n/a |
| **Model mapping** | ✅ Claude (native) | ✅ **Claude-native mapping** — emits the regional inference profile, **no remap** (live Claude composition receipt pending Gate A) | 🔁 Gemini (`gemini-2.5-flash`, override with `--google-model`) | 🔁 `gpt-*` |

## How to read the non-obvious cells

- **Handoff (Bedrock).** `agentlift deploy --target bedrock --build-only` is the shipped
  path: it compiles a Strands package and builds a complete ARM64 AgentCore Runtime
  container artifact (image + Dockerfile + a `NOTES.txt` runbook). A *bare* hosted deploy
  **refuses** — it raises before any AWS call and writes nothing — because the AgentCore
  control-plane create wire shape is not live-verified here (the same *confirm-live-before-encoding*
  rule that keeps Claude-on-Vertex an offline spike). See [deploy-bedrock.md](deploy-bedrock.md).
- **Model mapping (Bedrock) — the headline.** Bedrock exposes Claude as a native model family,
  so unlike Google agentlift emits the Bedrock Claude inference-profile ID directly rather than
  remapping to another model: a folder's `claude-haiku-4-5` maps to its regional Bedrock
  inference profile (`eu.anthropic.claude-haiku-4-5-20251001-v1:0` in `eu-north-1`; the region
  prefix follows the deploy region). This is a **mapping fact** — the compiler does no
  Gemini-style substitution — not a proven-inference claim. End-to-end Claude composition on
  Bedrock is **pending stable Gate A entitlement** (the per-account **Anthropic use-case form**,
  eventually consistent) and per-region availability; the Strands composition is itself
  live-proven on Amazon Nova (see
  [tested-platforms.md](tested-platforms.md#amazon-bedrock-agentcore-strands)).
- **MCP inline auth.** Anthropic's managed URL-MCP shape carries no credentials, so an
  inline auth header is **dropped with a warning** — the server must be public or
  self-authenticating. Both Bedrock and Google **carry it**: the header *value* resolves
  from the deployer's local environment at deploy time into a runtime `env_var`; only the
  env-var *name* is ever written into the plan, source, or lockfile.
- **stdio MCP.** A hosted engine can't spawn a local subprocess, so a `command:`/`npx`
  server is refused on all three deploy targets. Host it behind an HTTPS URL first.
- **Built-in sandbox tools — Bedrock vs Google differ.** This is the one row where Bedrock
  is *more* capable than Google: AgentCore offers a **real** sandbox (the Code Interpreter
  with shell + filesystem, plus a Browser tool), so the audit rates it `emulated`
  (platform-capable), not `degraded`. agentlift's compile does not wire it **yet** (a
  `PLANNED` diagnostic, never a silent drop). Google's hosted sandbox is genuinely Python/JS
  only — no shell, no workspace — so there it is `degraded` and skipped. Either way the
  supported path today is a URL MCP server.
- **Built-in web tools (Google).** `web_search` and `web_fetch` *do* map on Google: deploy
  lowers each as a dedicated single-tool ADK sub-agent — `web_search`→`GoogleSearchTool()`
  (Gemini's Google Search grounding), `web_fetch`→`url_context` (URL Context) — wrapped in
  an `AgentTool` with `propagate_grounding_metadata=True`. On Bedrock they are `PLANNED`
  (no hosted `web_search` primitive; `web_fetch` can map to the Browser tool). On Anthropic
  both are native built-ins.
- **`:ask`.** Native on Anthropic. On Google it's not enforced under `VertexAiSessionService`
  (a diagnostic). On Bedrock the hosted `/invocations` call is request/response with no
  interactive approval channel, so it's `unsupported` — enforce approval **client-side**, or
  keep `:ask` agents on Anthropic.
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
runtime events), and the **Bedrock** proof callout (the Strands composition exercised live
via the bearer token; the hosted runtime is build-only by design). For the honest
constraints and non-goals, see [`limitations.md`](limitations.md). The exact Anthropic
field-level mapping is in [`anthropic-mapping.md`](anthropic-mapping.md).
