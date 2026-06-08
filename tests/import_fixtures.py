"""Mocked provider read-API responses for the import tests (no network).

These dicts are shaped exactly like the real read APIs return (verified against
anthropic SDK 0.107.1 / boto3 1.43.24 in experiments/import-roundtrip and the
docs). They are the canned input the pure importer consumes, so the whole import
mapping is asserted offline — the same discipline as tests/test_planner.py.
"""
from __future__ import annotations

from agentlift.import_model import ImportedSkill
from agentlift.importer import hash_skill_files


def skill_bundle(name: str, body: str = "do the thing", desc: str = "") -> ImportedSkill:
    """Build an ImportedSkill the way the network layer would after a download."""
    front = f"---\nname: {name}\ndescription: {desc or name}\n---\n{body}\n"
    files = {f"{name}/SKILL.md": front.encode("utf-8")}
    return ImportedSkill(name=name, files=files, description=desc or name,
                         content_hash=hash_skill_files(files))


def _toolset(tokens=None, policies=None):
    """An agent_toolset_20260401 entry. tokens=None -> 'all builtins'."""
    if tokens is None:
        return {"type": "agent_toolset_20260401", "default_config": {"enabled": True}}
    configs = []
    policies = policies or {}
    for t in tokens:
        cfg = {"name": t, "enabled": True}
        if t in policies:
            cfg["permission_policy"] = {"type": policies[t]}
        configs.append(cfg)
    return {"type": "agent_toolset_20260401", "default_config": {"enabled": False}, "configs": configs}


def _mcp_toolset(server, tools=None, policies=None):
    """An mcp_toolset entry. tools=None -> all tools from the server enabled."""
    if tools is None:
        return {"type": "mcp_toolset", "mcp_server_name": server, "default_config": {"enabled": True}}
    policies = policies or {}
    configs = []
    for t in tools:
        cfg = {"name": t, "enabled": True}
        if t in policies:
            cfg["permission_policy"] = {"type": policies[t]}
        configs.append(cfg)
    return {"type": "mcp_toolset", "mcp_server_name": server, "default_config": {"enabled": False}, "configs": configs}


# --------------------------------------------------------------------------- #
# A coordinator + two specialists exercising every resource kind:
#   shared skill  : cite-sources (researcher + bug-finder, identical content)
#   shared mcp    : docs         (researcher + bug-finder, identical url + filter)
#   custom skill  : web-notes (researcher), bug-report (bug-finder)
#   custom mcp    : kb (researcher), linter (bug-finder)
#   subagents     : lead -> [researcher, bug-finder]
#   tool policy   : bug-finder bash:ask
# --------------------------------------------------------------------------- #
SKILL_BUNDLES = {
    "skill_cite": skill_bundle("cite-sources", "Always cite your sources.", "Cite sources."),
    "skill_webnotes": skill_bundle("web-notes", "Take structured notes.", "Note-taking."),
    "skill_bugreport": skill_bundle("bug-report", "Write the smallest fix.", "Bug reports."),
}

DOCS_MCP = {"type": "url", "name": "docs", "url": "https://docs.example.com/mcp"}

RESEARCHER = {
    "id": "agent_researcher", "type": "agent", "name": "researcher",
    "description": "Researches and cites.",
    "system": "You research and cite.",
    "model": {"model": "claude-haiku-4-5"},
    "tools": [
        _toolset(["read", "web_search"]),
        _mcp_toolset("docs", ["search"]),
        _mcp_toolset("kb", ["query"]),
    ],
    "mcp_servers": [DOCS_MCP, {"type": "url", "name": "kb", "url": "https://kb.example.com/mcp"}],
    "skills": [{"type": "custom", "skill_id": "skill_cite"},
               {"type": "custom", "skill_id": "skill_webnotes"}],
    "multiagent": None,
}

BUG_FINDER = {
    "id": "agent_bug_finder", "type": "agent", "name": "bug-finder",
    "description": "Finds the one-line bug.",
    "system": "You find the smallest bug.",
    "model": {"model": "claude-haiku-4-5"},
    "tools": [
        _toolset(["read", "glob", "grep", "bash"], {"bash": "always_ask"}),
        _mcp_toolset("docs", ["search"]),
        _mcp_toolset("linter", ["lint"]),
    ],
    "mcp_servers": [DOCS_MCP, {"type": "url", "name": "linter", "url": "https://lint.example.com/mcp"}],
    "skills": [{"type": "custom", "skill_id": "skill_cite"},
               {"type": "custom", "skill_id": "skill_bugreport"}],
    "multiagent": None,
}

LEAD = {
    "id": "agent_lead", "type": "agent", "name": "lead",
    "description": "Coordinator.",
    "system": "You delegate to specialists.",
    "model": {"model": "claude-haiku-4-5"},
    "tools": [_toolset(None)],   # all builtins
    "mcp_servers": [],
    "skills": [],
    "multiagent": {"type": "coordinator",
                   "agents": [{"id": "agent_researcher", "name": "researcher"},
                              {"id": "agent_bug_finder", "name": "bug-finder"}]},
}

TEAM_AGENTS = [LEAD, RESEARCHER, BUG_FINDER]


# --------------------------------------------------------------------------- #
# A Bedrock harness (single agent, config-only) with a remote MCP + browser +
# code-interpreter builtins + an S3 skill.
# --------------------------------------------------------------------------- #
HARNESS = {
    "harnessId": "harness-abc",
    "harnessName": "support-agent",
    "description": "Answers support questions.",
    "model": {"bedrockModelConfig": {"modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0"}},
    "systemPrompt": [{"text": "You answer support questions."}],
    "tools": [
        {"type": "remote_mcp", "name": "docs", "config": {"remoteMcp": {"url": "https://docs.example.com/mcp", "headers": {"Authorization": "Bearer x"}}}},
        {"type": "agentCoreBrowser", "name": "agentCoreBrowser", "config": {"agentCoreBrowser": {"browserArn": "arn:aws:...:browser/x"}}},
        {"type": "agentCoreCodeInterpreter", "name": "agentCoreCodeInterpreter", "config": {"agentCoreCodeInterpreter": {"codeInterpreterArn": "arn:aws:...:ci/x"}}},
    ],
    "skills": [{"s3": {"uri": "s3://my-bucket/agentlift-skills/support-agent/cite-sources/"}}],
    "allowedTools": [],
}

HARNESS_SKILLS = {
    "s3://my-bucket/agentlift-skills/support-agent/cite-sources/": skill_bundle("cite-sources", "Cite.", "Cite."),
}
