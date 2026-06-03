"""Provider capability map for managed-agent runtimes.

This is the distilled, verified result of agentlift's provider research
(Anthropic Managed Agents / Google Vertex AI Agent Engine + ADK / OpenAI Agent
Builder + Agents SDK). It is the single source of truth for two compiler
back-ends over the same parsed folder:

  - ``agentlift audit``  cross-references the features a folder actually uses
    against this map and reports native / emulated / degraded / unsupported.
  - ``agentlift export`` (planned) emits a provider-native artifact and uses
    the same tiers to warn about what won't round-trip.

One neutral definition, many backends. Pure data, no network.

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
    {"id": "builtin_sandbox", "label": "Built-in tool sandbox (bash / files / glob-grep / web)"},
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

    "google": {
        "hosted_runtime": {"tier": "native",
            "reason": "deployed ADK agent is a durable reasoningEngines/{id}; Agent Engine runs the loop server-side", "remediation": ""},
        "builtin_sandbox": {"tier": "degraded",
            "reason": "hosted sandbox is Python/JS only - no bash, no network (no built-in web_fetch/web_search), no glob/grep",
            "remediation": "supply those tools via MCP or external FunctionTools"},
        "tool_approval": {"tier": "unsupported",
            "reason": "ADK tool-confirmation is not enforced with VertexAiSessionService (the Agent Engine session service); open bugs",
            "remediation": "enforce approval client-side, or keep :ask agents on the Anthropic target"},
        "skills": {"tier": "emulated",
            "reason": "same SKILL.md spec, but build-time embedded into the deployed artifact (no upload-once shared registry; update = redeploy)",
            "remediation": ""},
        "remote_mcp": {"tier": "native",
            "reason": "ADK McpToolset attaches URL MCP servers with a tool_filter allowlist, server-side", "remediation": ""},
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
            "remediation": "agentlift export openai-chatkit (self-host), or author in Agent Builder"},
        "builtin_sandbox": {"tier": "degraded",
            "reason": "hosted code_interpreter is Python-only + ephemeral; real shell/file tools only in a self-hosted runner; no glob/grep",
            "remediation": "run the exported self-hosted server"},
        "tool_approval": {"tier": "degraded",
            "reason": "tool approvals are client-side (approve()/reject() in your runner); only remote-MCP require_approval is server-side",
            "remediation": "enforce in the self-hosted runner"},
        "skills": {"tier": "native",
            "reason": "hosted Skills API: POST /v1/skills returns a versioned skill_id, attached by reference", "remediation": ""},
        "remote_mcp": {"tier": "native",
            "reason": "Responses API hosted MCP (server_url + allowed_tools + require_approval), executed in OpenAI's runtime", "remediation": ""},
        "subagents": {"tier": "emulated",
            "reason": "a coordinator can call sub-agents as tools (agent-as-tool via the Agents SDK, confirmed working - see experiments/subagent-composition) or compose them as Agent Builder graph nodes; the delegation loop runs in your orchestrator (self-hosted), not OpenAI-hosted, and agents are not separately addressable by id",
            "remediation": "agentlift can emit each subagent as an agent-as-tool on the coordinator (you run the routing loop), or author a workflow graph"},
        "knowledge": {"tier": "native",
            "reason": "File Search over durable vector stores (reusable by vector_store_id)", "remediation": ""},
        "deploy_versioning": {"tier": "degraded",
            "reason": "Agent Builder workflows + Skills are versioned, but there is no programmatic create-API for a hosted free-form agent (Workflows API 'coming soon')",
            "remediation": "author in the Agent Builder UI, or export + self-host"},
        "streaming": {"tier": "native",
            "reason": "Responses API typed SSE; ChatKit streams workflow events", "remediation": ""},
    },
}
