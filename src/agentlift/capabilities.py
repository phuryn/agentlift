"""Provider capability map for managed-agent runtimes.

This is the distilled, verified result of agentlift's provider research
(Anthropic Managed Agents / Google Vertex AI Agent Engine + ADK / OpenAI Agent
Builder + Agents SDK). It is the single source of truth for two compiler
back-ends over the same parsed folder:

  - ``agentlift audit``  cross-references the features a folder actually uses
    against this map and reports native / emulated / degraded / unsupported.
  - ``agentlift export`` emits a provider-native artifact, and ``deploy
    --target {anthropic,google,bedrock}`` ships it to the hosted runtime; all
    honor the same tiers (the Google + Bedrock deploys map skills + MCP per the
    rows below).

One neutral definition, many backends. Pure data, no network.

These tiers rate each *platform's* capability (what the hosted runtime could do),
not agentlift's shipped maturity for that feature (the README maturity table owns
that). Bedrock is the sharpest case: AgentCore exposes TWO primitives -- the
custom-container **Runtime** (Strands, multi-agent) and the newer config-only
managed **Harness** (single agent) -- so a Bedrock reason below often names which
primitive serves a feature and how. The harness deploy is a live preview; that
preview status lives in the maturity table + plan diagnostics, not in these tiers.

Support tiers:
  native       first-class; deploys/compiles as-is.
  emulated     supported, but wired differently than the source (no capability
               lost, just a structural change agentlift absorbs).
  degraded     partially supported; a real capability is weakened or lost.
  unsupported  cannot be represented on this target's hosted runtime today.
"""
from __future__ import annotations

TIER_ORDER = ["native", "emulated", "degraded", "unsupported"]

# Ordered: this is also the order the audit report prints features in.
FEATURES = [
    {"id": "hosted_runtime", "label": "Hosted runtime (provider runs the loop, callable by id)"},
    {"id": "builtin_sandbox", "label": "Built-in tool sandbox (bash / files / glob-grep)"},
    {"id": "builtin_web", "label": "Built-in web tools (web_search / web_fetch)"},
    {"id": "tool_approval", "label": "Per-tool approval gate (:ask / human-in-the-loop)"},
    {"id": "skills", "label": "Skills (SKILL.md bundles)"},
    {"id": "remote_mcp", "label": "Remote MCP servers (URL + allowlist)"},
    {"id": "subagents", "label": "Subagents -> coordinator (deployed roster)"},
    {"id": "knowledge", "label": "Knowledge files"},
    {"id": "deploy_versioning", "label": "Durable versioned deploy"},
    {"id": "streaming", "label": "Session / event streaming"},
]


def _all_native(reason: str):
    return {f["id"]: {"tier": "native", "reason": reason, "remediation": ""} for f in FEATURES}


CAPABILITIES = {
    # The reference target: agentlift's folder model is shaped to map 1:1 here.
    "anthropic": _all_native("agentlift's reference target; the folder maps 1:1"),

    # Amazon Bedrock AgentCore Runtime, compiled via the Strands Agents SDK.
    # The headline portability story: Claude is NATIVE here (a regional inference
    # profile), so the SAME model runs on Anthropic AND AWS.
    "bedrock": {
        "hosted_runtime": {"tier": "native",
            "reason": "two AgentCore primitives: the custom-container Runtime (agentlift ships a Strands app serving POST /invocations + GET /ping, addressable by ARN) and the config-only managed Harness (agentlift declares model + systemPrompt + tools, AWS runs the loop); both are durable server-side runtimes", "remediation": ""},
        "builtin_sandbox": {"tier": "emulated",
            "reason": "the managed Harness base session always carries shell + file_operations, so the sandbox built-ins (bash / read / write / glob-grep) map onto those native @builtin tools (config-only, no container); the Runtime path has a richer AgentCore Code Interpreter (shell + filesystem) that is still PLANNED there - so nothing is lost, it is wired through a different primitive than the source's native built-ins",
            "remediation": "single skill-less agents map natively on the harness today; for the Runtime path, expose equivalents via a URL MCP server until the Code Interpreter is wired"},
        "builtin_web": {"tier": "degraded",
            "reason": "the managed Harness maps web_search / web_fetch onto its agentcore_browser tool, but a browser is not a first-class hosted web_search grounding primitive the way Anthropic and Gemini expose one (web_fetch maps cleanly; web_search is approximate); the Runtime path surfaces both as PLANNED",
            "remediation": "supply a dedicated web_search via a search MCP server, or keep search-heavy agents on Anthropic / Google"},
        "tool_approval": {"tier": "unsupported",
            "reason": "neither hosted primitive has an interactive approval channel - the Runtime /invocations call is request/response and the managed Harness invoke is non-interactive - so :ask cannot be enforced server-side",
            "remediation": "enforce approval client-side, or keep :ask agents on the Anthropic target"},
        "skills": {"tier": "emulated",
            "reason": "same SKILL.md spec; on the Runtime path agentlift embeds the bundles in the source package and loads them with Strands Skill.from_file + AgentSkills at startup (update = redeploy). The managed Harness 'skills' parameter is only a POINTER to a path already baked into the environment - it does not upload - so a skill-bearing folder routes to the Runtime (no upload-once shared registry on either)",
            "remediation": ""},
        "remote_mcp": {"tier": "native",
            "reason": "URL MCP servers map on both primitives: the Runtime as a Strands MCPClient (streamable-HTTP) with a tool_filter allowlist + server-name prefix, the Harness as a remote_mcp tool with an allowedTools glob allowlist; inline auth headers resolve from the local env into AgentCore env vars / harness headers at deploy (stdio/command servers remain unsupported)",
            "remediation": "host stdio MCP servers behind an HTTPS URL to deploy them"},
        "subagents": {"tier": "emulated",
            "reason": "the Runtime deploys root + roster as ONE container; each sub-agent becomes an agents-as-tools @tool the coordinator delegates to in-model (not per-agent-id the way Anthropic gives each its own id). The managed Harness is single-agent (no sub-agent tool type), so multi-agent folders route to the Runtime",
            "remediation": "deploy specialists as separate runtimes/harnesses and call across them if you need per-agent ids"},
        "knowledge": {"tier": "emulated",
            "reason": "no single bundled primitive; agentlift folds knowledge/ files into the system prompt at build (large sets truncate, surfaced as a diagnostic); Bedrock Knowledge Bases offer a RAG primitive agentlift does not yet wire",
            "remediation": "for large corpora, attach a Bedrock Knowledge Base or a retrieval MCP server"},
        "deploy_versioning": {"tier": "native",
            "reason": "create/update keeps the resource identity (Runtime ARN / harness id); agentlift's spec-hash lock drives idempotent create/update/skip (.agentlift-bedrock.json for the Runtime, .agentlift-harness.json for the Harness)", "remediation": ""},
        "streaming": {"tier": "native",
            "reason": "the Runtime streams the /invocations response (Strands emits incremental events); the managed Harness streams InvokeHarness events", "remediation": ""},
    },

    "google": {
        "hosted_runtime": {"tier": "native",
            "reason": "deployed ADK agent is a durable reasoningEngines/{id}; Agent Engine runs the loop server-side", "remediation": ""},
        "builtin_sandbox": {"tier": "degraded",
            "reason": "hosted sandbox is Python/JS only - no bash, no file edit/write, no glob/grep over a workspace",
            "remediation": "supply those tools via MCP or external FunctionTools"},
        "builtin_web": {"tier": "emulated",
            "reason": "web_search maps to Gemini's Google Search grounding and web_fetch to URL Context; agentlift deploy lowers each as a dedicated single-tool ADK sub-agent wrapped in an AgentTool (propagating grounding metadata) so they coexist with MCP/skills/transfer. web_fetch is approximate - URL Context grounds the model over URLs rather than performing a literal on-demand fetch",
            "remediation": ""},
        "tool_approval": {"tier": "unsupported",
            "reason": "ADK tool-confirmation is not enforced with VertexAiSessionService (the Agent Engine session service); open bugs",
            "remediation": "enforce approval client-side, or keep :ask agents on the Anthropic target"},
        "skills": {"tier": "emulated",
            "reason": "same SKILL.md spec; agentlift deploy ships the bundles inside the engine's source package and loads them with ADK load_skill_from_dir at startup (no upload-once shared registry, so update = redeploy)",
            "remediation": ""},
        "remote_mcp": {"tier": "native",
            "reason": "agentlift deploy wires each URL MCP server as an ADK McpToolset with a tool_filter allowlist, server-side; inline auth headers are passed as Agent Engine env vars resolved at deploy (stdio/command servers remain unsupported)",
            "remediation": "host stdio MCP servers behind an HTTPS URL to deploy them"},
        "subagents": {"tier": "emulated",
            "reason": "root + sub_agents deploy as ONE reasoningEngine with server-side delegation; the roster is not addressable per-agent-id (Anthropic gives each its own id)",
            "remediation": "use the A2A protocol across deployments if you need per-agent ids"},
        "knowledge": {"tier": "emulated",
            "reason": "no single bundled primitive; maps to Skill L3 references + a Vertex RAG Engine corpus", "remediation": ""},
        "deploy_versioning": {"tier": "native",
            "reason": "durable addressable reasoningEngine; idempotent update() keeps the id (revisions/canary are still preview)", "remediation": ""},
        "streaming": {"tier": "native",
            "reason": "async_stream_query streams response/events from the deployed app", "remediation": ""},
    },

    "openai": {
        "hosted_runtime": {"tier": "degraded",
            "reason": "only the Agent Builder VISUAL graph runs on OpenAI (called by workflow_id via ChatKit sessions); code-defined Agents-SDK agents are self-hosted - there is no code-define + OpenAI-host path",
            "remediation": "agentlift export openai-agents (self-host), or author in Agent Builder"},
        "builtin_sandbox": {"tier": "degraded",
            "reason": "hosted code_interpreter is Python-only + ephemeral; real shell/file tools only in a self-hosted runner; no glob/grep",
            "remediation": "run the exported self-hosted server"},
        "builtin_web": {"tier": "emulated",
            "reason": "web_search maps natively to the Agents SDK hosted WebSearchTool (Responses API), but there is no hosted web_fetch primitive; the export wires web_fetch as a function tool the self-hosted runner provides",
            "remediation": "run the exported self-hosted runner; add a fetch FunctionTool for web_fetch"},
        "tool_approval": {"tier": "degraded",
            "reason": "tool approvals are client-side (approve()/reject() in your runner); only remote-MCP require_approval is server-side",
            "remediation": "enforce in the self-hosted runner"},
        "skills": {"tier": "native",
            "reason": "hosted Skills API: POST /v1/skills returns a versioned skill_id, attached by reference", "remediation": ""},
        "remote_mcp": {"tier": "native",
            "reason": "Responses API hosted MCP (server_url + allowed_tools + require_approval), executed in OpenAI's runtime", "remediation": ""},
        "subagents": {"tier": "emulated",
            "reason": "a coordinator can call sub-agents as tools (agent-as-tool via the Agents SDK, confirmed working - see experiments/subagent-composition) or compose them as Agent Builder graph nodes; the delegation loop runs in your orchestrator (self-hosted), not OpenAI-hosted, and agents are not separately addressable by id",
            "remediation": "run agentlift export openai-agents - each subagent becomes an as_tool on the coordinator (you run the routing loop); or author a workflow graph"},
        "knowledge": {"tier": "native",
            "reason": "File Search over durable vector stores (reusable by vector_store_id)", "remediation": ""},
        "deploy_versioning": {"tier": "degraded",
            "reason": "Agent Builder workflows + Skills are versioned, but there is no programmatic create-API for a hosted free-form agent (Workflows API 'coming soon')",
            "remediation": "author in the Agent Builder UI, or export + self-host"},
        "streaming": {"tier": "native",
            "reason": "Responses API typed SSE; ChatKit streams workflow events", "remediation": ""},
    },
}
