# Tested platforms — receipts + where to find more

What "I ran it on all three" actually means, with the configuration, the results, and the
console/docs links for each managed-agent platform. Two of the three are tested as a **live
hosted deploy**; OpenAI is tested as the **agent-as-tool composition** (it has no
code-define + host path, so there is nothing to "deploy" — see the audit).

| Platform | What was tested | How | Result |
|---|---|---|---|
| **Anthropic** Managed Agents | live deploy + run + graded output | `agentlift deploy` → `agents.create`, run a session, LLM-grade | ✅ `tests/live/` + `benchmarks/` (managed vs local, 100% pass) |
| **Google** Vertex AI Agent Engine | live deploy of a coordinator + 2 subagents | `agentlift deploy --target google` → ADK `sub_agents` → `agent_engines.create()` | ✅ live `reasoningEngine`; server-side `transfer_to_agent` confirmed |
| **OpenAI** Agents SDK | coordinator delegates to a subagent **as a tool** | `researcher.as_tool()`, run with `Runner.run` | ✅ trace `function_call ask_researcher` (in-process loop) |

The pattern is the same across all three; what differs is **where the orchestration loop
runs** — the provider's runtime (Anthropic, Google) or your app (OpenAI). See
[`experiments/subagent-composition/RESULTS.md`](../experiments/subagent-composition/RESULTS.md).

---

## Anthropic Managed Agents (reference target)

- **Config:** the `examples/quickstart` + `examples/team` folders — a coordinator (`lead`)
  over `bug-finder` + `researcher`, a shared skill, a remote MCP server, a `bash:ask` gate.
- **How:** `agentlift deploy ./examples/team --yes` → uploads skills, creates agents in
  dependency order (the `multiagent` coordinator server-side), writes `.agentlift-lock.json`.
- **Result:** validated by `tests/live/` (deploy → run a hosted session → an LLM grades the
  output) and `benchmarks/results.md` (same folder on managed vs local: 100% pass). The
  `RECEIPT:` skill fires **inside Anthropic's container**, proving the uploaded skill rode along.
- **Models:** `claude-haiku-4-5`. **Orchestration loop:** hosted (Anthropic runs delegation).

**More:** managed agents in your workspace → <https://platform.claude.com/workspaces/default/agents>
· docs → <https://platform.claude.com/docs/en/managed-agents/overview>

---

## Google Vertex AI Agent Engine

- **Config:** the same `examples/team` folder, compiled by `agentlift deploy --target google`
  to ADK `LlmAgent`s — a root coordinator (`lead`) over `bug_finder` + `researcher` with
  ADK `sub_agents`, wrapped in an `AdkApp`, deployed via `agent_engines.create()`.
- **Auth + env:** ADC (`gcloud auth application-default login`), `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_CLOUD_LOCATION=us-central1`, a Cloud Storage staging bucket. See
  [`docs/deploy-google.md`](deploy-google.md).
- **Models:** `claude-haiku-4-5` in the folder is mapped to `gemini-2.5-flash` for Agent
  Engine (a Gemini project). **Preview scope:** MCP servers, skills, and built-in tools are
  noted and skipped in this first deploy (the audit reports those tiers).
- **Orchestration loop:** hosted (Vertex runs `transfer_to_agent` delegation server-side as
  one `reasoningEngine`).
- **Result:** live `reasoningEngine`
  `projects/670199341658/locations/us-central1/reasoningEngines/2870053552716251136` (deployed
  2026-06-03 via `agentlift deploy --target google`). Querying the **deployed** engine confirms it
  runs and **delegates server-side**:

  ```
  QUERY: How tall is the Eiffel Tower in meters, and what year was it completed?
    [delegation] lead -> transfer_to_agent({'agent_name': 'researcher'})
    [researcher] The Eiffel Tower is 330 meters (1,083 feet) tall, including the antenna. It was completed in 1889.
  ```

  The coordinator `lead` delegated to the `researcher` subagent **inside Google's runtime**, not in
  the client - the hosted loop. `create()` on Agent Engine *is* the deploy; the engine is live + billable.

**More:** Agent Platform console (visual) → <https://console.cloud.google.com/agent-platform>
· Agent Studio overview → <https://docs.cloud.google.com/gemini-enterprise-agent-platform/agent-studio>
· gcloud SDK → <https://docs.cloud.google.com/sdk/gcloud>

---

## OpenAI (Agents SDK)

- **Config:** a coordinator + a `researcher` sub-agent exposed to it as a tool via
  `researcher.as_tool(tool_name="ask_researcher", ...)`, run with `Runner.run`. Model
  `gpt-5-mini`. Script: [`experiments/subagent-composition/openai_agent_as_tool.py`](../experiments/subagent-composition/openai_agent_as_tool.py).
- **Result:** the coordinator called the sub-agent **as a tool** (trace: `function_call
  ask_researcher` → `ToolCallOutputItem`) and synthesized the answer. This is exactly what
  `agentlift export openai-agents` emits from a folder.
- **Orchestration loop:** **your app** (in-process). OpenAI hosts only an Agent Builder
  visual graph; there is no code-define + OpenAI-host path, so OpenAI is an `export` target,
  never a `deploy` target.

**More:** Agent Builder → <https://platform.openai.com/agent-builder/>
· Agents SDK docs → <https://developers.openai.com/api/docs/guides/agents>

---

*All three were exercised with the live SDKs (not mocked). The subagent-composition traces
are reproducible from [`experiments/subagent-composition/`](../experiments/subagent-composition/);
the Google live deploy from [`docs/deploy-google.md`](deploy-google.md).*
