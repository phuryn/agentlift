# Provider capability matrix

One neutral `.managed-agents/` folder, three managed-agent runtimes. This is the
row-by-row reference for **what maps where** — the detailed companion to the README's
summary. It is generated from the same source of truth the tooling uses:
[`src/agentlift/capabilities.py`](../src/agentlift/capabilities.py), which `agentlift
audit` and `agentlift export` both read.

**`audit` vs `deploy` — they answer different questions.** `audit` reports each
*platform's* capability (what the runtime could do); `deploy` reports *agentlift's
current implementation* (what the compiler ships today). They agree on almost everything;
where they differ it's called out below (built-in tools and `:ask` on Google: `audit`
rates the platform `degraded`/`unsupported`, and `deploy` correspondingly skips/refuses).

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
| **Built-in tools** | ✅ mapped (`read/glob/grep/bash/edit/write/web_*`) | 🚧 skipped — Vertex sandbox is Python/JS only | 🔁 self-host runner |
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
- **Built-in tools (Google).** Agent Engine's hosted sandbox is Python/JS only — no
  shell, no network fetch, no glob/grep over a workspace (there is no workspace). Supply
  equivalents via an MCP server. The agent deploys without the built-ins, with a warning.
- **`:ask` (Google).** ADK tool-confirmation is not enforced under the Agent Engine
  session service today, so a `:ask`-gated tool stays available without a gate. Keep
  `:ask` agents on the Anthropic target where the gate is real.
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
