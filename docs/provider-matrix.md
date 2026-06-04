# Provider capability matrix

One neutral `.managed-agents/` folder, three managed-agent runtimes. This is the
row-by-row reference for **what maps where** — the detailed companion to the README's
summary. It is generated from the same source of truth the tooling uses:
[`src/agentlift/capabilities.py`](../src/agentlift/capabilities.py), which `agentlift
audit` and `agentlift export` both read.

**`audit` vs `deploy` — they answer different questions.** `audit` reports each
*platform's* capability (what the runtime could do); `deploy` reports *agentlift's
current implementation* (what the compiler ships today). They agree on almost everything;
where they differ it's called out below (the built-in **sandbox** tools and `:ask` on
Google: `audit` rates the platform `degraded`/`unsupported`, and `deploy` correspondingly
skips/refuses — the built-in **web** tools now map on both sides).

**Legend:** ✅ native / maps 1:1 · 🟡 live, preview · 🔁 translated to a different shape
(export, or model remap) · 🚧 not mapped / not enforced yet (surfaced as a diagnostic,
never a silent drop) · ❌ refused / not applicable.

| Capability | Anthropic Managed Agents | Google (`--target google`) | OpenAI |
|---|---|---|---|
| **Handoff** | ✅ `deploy` (live, full) | 🟡 `deploy` (live, preview) | 🔁 `export` + self-host |
| **Agents** | ✅ live, per-agent IDs | ✅ live (one `reasoningEngine`) | 🔁 export |
| **Subagents** | ✅ native, server-side coordinator | ✅ server-side delegation (ADK `sub_agents`, one engine) | 🔁 `as_tool`, loop runs in your app |
| **Shared skill** | ✅ uploaded once, shared by id | ✅ embedded in source pkg, ADK `load_skill_from_dir` | 🔁 Skills-API scaffold (self-host) |
| **Private skill** | ✅ | ✅ | 🔁 scaffold |
| **Shared MCP (URL)** | ✅ mapped | ✅ `McpToolset` + `tool_filter` | 🔁 `HostedMCPTool` scaffold |
| **Private MCP (URL)** | ✅ | ✅ | 🔁 scaffold |
| **MCP inline auth** | 🚧 dropped (diagnostic) | ✅ resolved to Agent Engine `env_vars` (never inlined) | 🚧 scaffold |
| **stdio MCP** | ❌ refused (host behind HTTPS) | ❌ refused (host behind HTTPS) | ❌ n/a |
| **Built-in web tools** (`web_search`/`web_fetch`) | ✅ mapped | 🟡 mapped — `web_search`→Google Search grounding, `web_fetch`→URL Context (each a wrapped tool-agent) | 🔁 `WebSearchTool` / self-host fetch |
| **Built-in sandbox tools** (`bash/files/glob-grep`) | ✅ mapped | 🚧 skipped — Vertex sandbox is Python/JS only | 🔁 self-host runner |
| **`:ask` per-tool** | ✅ permission policy | 🚧 not enforced on `VertexAiSessionService` | 🔁 client-side (your runner) |
| **Idempotency** | ✅ lockfile + content hashes | ✅ `.agentlift-google.json` spec hash → create / update / skip | ❌ n/a |
| **Model** | ✅ Claude (native) | 🔁 Gemini (`gemini-2.5-flash`, override with `--google-model`) | 🔁 `gpt-*` |

## How to read the non-obvious cells

- **MCP inline auth.** Anthropic's managed URL-MCP shape carries no credentials, so an
  inline auth header is **dropped with a warning** — the server must be public or
  self-authenticating. Google **does** carry it: the header *value* resolves from the
  deployer's local environment at deploy time into an Agent Engine `env_var`; only the
  env-var *name* is ever written into the plan, source, or lockfile.
- **stdio MCP.** A hosted engine can't spawn a local subprocess, so a `command:`/`npx`
  server is refused on both deploy targets. Host it behind an HTTPS URL first.
- **Built-in web tools (Google).** `web_search` and `web_fetch` *do* map: deploy lowers
  each as a dedicated single-tool ADK sub-agent — `web_search`→`GoogleSearchTool()`
  (Gemini's Google Search grounding), `web_fetch`→`url_context` (URL Context) — wrapped in
  an `AgentTool` with `propagate_grounding_metadata=True` so the grounding/retrieval
  metadata surfaces on the outer event stream. The wrap is unconditional (an agent with no
  `tools:` enables all built-ins, so it gets both), which keeps the coordinator's own
  `web_search` from colliding with ADK's injected `transfer_to_agent` tools. `web_fetch` is
  **approximate**: URL Context decides what to fetch from the prompt rather than taking an
  explicit URL argument. Deploy pins `google-adk>=1.34.3` when any web tool is present.
- **Built-in sandbox tools (Google).** Agent Engine's hosted sandbox is Python/JS only — no
  shell, no glob/grep over a workspace (there is no workspace). `bash/edit/write/glob/grep/read`
  deploy without the built-in, with a warning. Emulating a shell+FS *inside* the engine is an
  explicit **non-goal** (it would be the silent degradation the tool exists to surface); the
  supported path is a URL MCP server, which does deploy — see
  [the workaround](deploy-google.md#two-known-gaps-and-how-to-work-around-them).
- **`:ask` (Google).** ADK tool-confirmation is not enforced under the Agent Engine
  session service today, so a `:ask`-gated tool stays available without a gate. Enforce
  approval **client-side** in the loop that calls the engine, or keep `:ask` agents on the
  Anthropic target where the gate is native — see
  [the workaround](deploy-google.md#two-known-gaps-and-how-to-work-around-them).
- **Model (Google).** Claude folder models map to Gemini (`gemini-2.5-flash`). Keeping a
  Claude brain via **Claude-on-Vertex** is offline-verified but **not shipped** — ADK 1.34.3
  resolves Claude on Vertex and the mixed-model shape composes (web sub-agents stay Gemini),
  but with no live receipt a Claude `--google-model` is refused, not silently deployed. See
  [`experiments/claude-on-vertex/`](../experiments/claude-on-vertex/).
- **Subagents (per-agent IDs).** Anthropic gives each agent its own addressable id;
  Google deploys the whole roster as **one** `reasoningEngine` with server-side
  delegation, so the roster is not individually addressable (the A2A protocol across
  separate deployments would be the path to per-agent ids).
- **OpenAI.** There is no code-define + OpenAI-host path, so OpenAI is an `export` target,
  never a `deploy` target. Agents + subagents are real (`as_tool` composition,
  trace-verified); skills and MCP compile to guided self-host scaffolding.

## What's been exercised live

This matrix is the *capability* reference — what the compiler maps. For *receipt
evidence* of what actually ran on a deployed engine (all six portability dimensions, both
Anthropic and Google, classified by objective runtime events), see
[`tested-platforms.md`](tested-platforms.md). For the honest constraints and non-goals,
see [`limitations.md`](limitations.md). The exact Anthropic field-level mapping is in
[`anthropic-mapping.md`](anthropic-mapping.md).
