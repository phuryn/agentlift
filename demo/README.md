# Demo: one folder, audited across 3 providers and compiled to 2 formats

This is the portability thesis made concrete, and it's all **offline** — no API key,
nothing deployed. Run it yourself:

```bash
pip install agentlift          # or: pip install -e .  from a clone
./demo/portability-demo.sh     # uses examples/team
```

The input is one neutral agent folder — [`examples/team`](../examples/team) — a coordinator
(`lead`) over a `researcher` (shared `docs` MCP) and a `bug-finder` (`bash:ask`), sharing a
`cite-sources` skill. Nothing in it is provider-specific.

---

## 1. `agentlift audit` — how portable is it?

```console
$ agentlift audit examples/team --targets anthropic,google,openai
Portability audit: examples/team
Agents: bug-finder, lead, researcher
Capabilities this folder uses: 8

== Anthropic Managed Agents ==   [8 native]
  native:
    + Hosted runtime (provider runs the loop, callable by id)
    + Built-in tool sandbox (bash / files / glob-grep / web)
    + Per-tool approval gate (:ask / human-in-the-loop)
    + Skills (SKILL.md bundles)
    + Remote MCP servers (URL + allowlist)
    + Subagents -> coordinator (deployed roster)
    + Durable versioned deploy
    + Session / event streaming

== Google Vertex AI Agent Engine (ADK) ==   [4 native, 2 emulated, 1 degraded, 1 unsupported]
  emulated:
    ~ Skills (SKILL.md bundles)
        reason: same SKILL.md spec, but build-time embedded into the deployed artifact (no upload-once shared registry; update = redeploy)
    ~ Subagents -> coordinator (deployed roster)
        reason: root + sub_agents deploy as ONE reasoningEngine with server-side delegation; the roster is not addressable per-agent-id
        fix:    use the A2A protocol across deployments if you need per-agent ids
  degraded:
    ! Built-in tool sandbox (bash / files / glob-grep / web)
        reason: hosted sandbox is Python/JS only - no bash, no network, no glob/grep
        fix:    supply those tools via MCP or external FunctionTools
  unsupported:
    x Per-tool approval gate (:ask / human-in-the-loop)
        reason: ADK tool-confirmation is not enforced with VertexAiSessionService (the Agent Engine session service)
        fix:    enforce approval client-side, or keep :ask agents on the Anthropic target

== OpenAI (Agent Builder / Agents SDK) ==   [3 native, 4 degraded, 1 unsupported]
  degraded:
    ! Hosted runtime (provider runs the loop, callable by id)
        reason: only the Agent Builder visual graph runs on OpenAI; code-defined Agents-SDK agents are self-hosted
        fix:    agentlift export openai-chatkit (self-host), or author in Agent Builder
    ! Built-in tool sandbox / Per-tool approval / Durable versioned deploy
  unsupported:
    x Subagents -> coordinator (deployed roster)
        reason: no deployable coordinator-over-roster; multi-agent is in-process SDK handoffs or static graph nodes

Verdict (lower is more portable):
  Anthropic Managed Agents: drops in cleanly
  Google Vertex AI Agent Engine (ADK): 1 unsupported, 1 degraded
  OpenAI (Agent Builder / Agents SDK): 1 unsupported, 4 degraded
```

The audit is a compiler diagnostic, not a marketing table: it parses *your* folder and tells
you exactly which features survive each runtime, why, and how to work around the gaps.

## 2. `agentlift export anthropic-yaml` — compile to the `ant` format

```console
$ agentlift export anthropic-yaml examples/team --out demo/out/anthropic
Wrote 4 file(s) to demo/out/anthropic:
  bug-finder.agent.yaml
  researcher.agent.yaml
  lead.agent.yaml
  SKILLS.txt
```

`researcher.agent.yaml` is the exact `agents.create` shape — feed it straight to the official
CLI with `ant beta:agents create < researcher.agent.yaml`:

```yaml
name: researcher
model: claude-haiku-4-5
system: 'You are the Researcher. Answer questions thoroughly...'
tools:
- type: agent_toolset_20260401
  default_config: {enabled: false}
  configs:
  - {name: read, enabled: true}
  - {name: web_search, enabled: true}
- type: mcp_toolset
  mcp_server_name: docs
  default_config: {enabled: false}
  configs:
  - {name: search, enabled: true}
skills: [cite-sources]
mcp_servers:
- {type: url, name: docs, url: https://example.com/mcp}
```

`bash:ask` round-trips to `permission_policy: {type: always_ask}`; the `lead` coordinator
round-trips to `multiagent: {type: coordinator, agents: [bug-finder, researcher]}`. **`ant` is
one of agentlift's outputs, not a competitor.**

## 3. `agentlift export google-adk` — compile to a Vertex scaffold (preview)

```console
$ agentlift export google-adk examples/team --out demo/out/google
Wrote 1 file(s) to demo/out/google:
  agent.py
```

A runnable-shaped ADK app, with every gap the audit flagged annotated inline:

```python
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

# --- agent: researcher ---
# DEGRADED (built-in sandbox): hosted sandbox is Python/JS only - no bash, no network, no glob/grep
agent_researcher = LlmAgent(
    name='researcher',
    model='claude-haiku-4-5',          # NOTE: map to a Gemini / Claude-on-Vertex id
    instruction="""You are the Researcher...""",
    tools=[
        McpToolset(connection_params=StreamableHTTPConnectionParams(url='https://example.com/mcp'), tool_filter=['search']),
    ],
)
# ... agent_lead with sub_agents=[agent_bug_finder, agent_researcher]
root_agent = agent_lead
```

---

**One neutral definition. Audited across three runtimes, compiled to two formats — no rewrite.**
That's the asset `ant`/ADK/Agent-Builder can't give you: the definition stays yours.
