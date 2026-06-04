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
Capabilities this folder uses: 9

== Anthropic Managed Agents ==   [9 native]
  native:
    + Hosted runtime (provider runs the loop, callable by id)
    + Built-in tool sandbox (bash / files / glob-grep)
    + Built-in web tools (web_search / web_fetch)
    + Per-tool approval gate (:ask / human-in-the-loop)
    + Skills (SKILL.md bundles)
    + Remote MCP servers (URL + allowlist)
    + Subagents -> coordinator (deployed roster)
    + Durable versioned deploy
    + Session / event streaming

== Google Vertex AI Agent Engine (ADK) ==   [4 native, 3 emulated, 1 degraded, 1 unsupported]
  emulated:
    ~ Built-in web tools (web_search / web_fetch)
        reason: web_search maps to Gemini's Google Search grounding and web_fetch to URL Context; agentlift deploy lowers each as a dedicated single-tool ADK sub-agent wrapped in an AgentTool (propagating grounding metadata) so they coexist with MCP/skills/transfer. web_fetch is approximate - URL Context grounds the model over URLs rather than performing a literal on-demand fetch
    ~ Skills (SKILL.md bundles)
        reason: same SKILL.md spec, but build-time embedded into the deployed artifact (no upload-once shared registry; update = redeploy)
    ~ Subagents -> coordinator (deployed roster)
        reason: root + sub_agents deploy as ONE reasoningEngine with server-side delegation; the roster is not addressable per-agent-id
        fix:    use the A2A protocol across deployments if you need per-agent ids
  degraded:
    ! Built-in tool sandbox (bash / files / glob-grep)
        reason: hosted sandbox is Python/JS only - no bash, no file edit/write, no glob/grep over a workspace
        fix:    supply those tools via MCP or external FunctionTools
  unsupported:
    x Per-tool approval gate (:ask / human-in-the-loop)
        reason: ADK tool-confirmation is not enforced with VertexAiSessionService (the Agent Engine session service)
        fix:    enforce approval client-side, or keep :ask agents on the Anthropic target

== OpenAI (Agent Builder / Agents SDK) ==   [3 native, 2 emulated, 4 degraded]
  emulated:
    ~ Built-in web tools (web_search / web_fetch)
        reason: web_search maps natively to the Agents SDK hosted WebSearchTool (Responses API), but there is no hosted web_fetch primitive; the export wires web_fetch as a function tool the self-hosted runner provides
    ~ Subagents -> coordinator (deployed roster)
        reason: agent-as-tool composition works (confirmed in experiments/subagent-composition); the delegation loop runs in your orchestrator, not OpenAI-hosted
  degraded:
    ! Hosted runtime (provider runs the loop, callable by id)
        reason: only the Agent Builder visual graph runs on OpenAI; code-defined Agents-SDK agents are self-hosted
        fix:    agentlift export openai-agents (self-host), or author in Agent Builder
    ! Built-in tool sandbox / Per-tool approval / Durable versioned deploy

Verdict (lower is more portable):
  Anthropic Managed Agents: drops in cleanly
  Google Vertex AI Agent Engine (ADK): 1 unsupported, 1 degraded
  OpenAI (Agent Builder / Agents SDK): 4 feature(s) degrade, none lost
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
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.google_search_tool import GoogleSearchTool

# --- agent: researcher ---
# EMULATED (built-in web): web_search -> Google Search grounding, web_fetch -> URL Context;
#   each lowered as a single-tool sub-agent wrapped in an AgentTool (propagate_grounding_metadata=True)
agent_researcher = LlmAgent(
    name='researcher',
    model=vertex_model('claude-haiku-4-5'),   # claude-haiku-4-5 -> gemini-2.5-flash
    instruction="""You are the Researcher...""",
    tools=[
        McpToolset(connection_params=StreamableHTTPConnectionParams(url='https://example.com/mcp'), tool_filter=['search']),
        AgentTool(agent=LlmAgent(name='researcher_web_search', model=vertex_model('claude-haiku-4-5'),
                                 description='Search the public web with Google Search...',
                                 tools=[GoogleSearchTool()]), propagate_grounding_metadata=True),
    ],
)
# ... agent_lead with sub_agents=[agent_bug_finder, agent_researcher]
root_agent = agent_lead
```

---

**One neutral definition. Audited across three runtimes, compiled to two formats — no rewrite.**
That's the asset `ant`/ADK/Agent-Builder can't give you: the definition stays yours.
