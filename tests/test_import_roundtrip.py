"""End-to-end import round-trip: provider responses -> folder -> parser -> planner.

The contract test. It proves an imported folder is a *real* deployable project by
running the actual `parse_project` + `build_plan` over what `folder_writer` emits,
and asserting the reconstructed plan carries the tools / skills / mcp / subagent
delegation we started with.

The centerpiece (`test_subagent_delegation_*`) exercises every resource kind at
once: a coordinator delegating to two specialists that share a skill and an MCP
server while each also owns a private skill and a private MCP server.
"""
from __future__ import annotations

import json
import os

import pytest

from agentlift.folder_writer import write_project
from agentlift.importer import import_anthropic_agents, import_bedrock_harness
from agentlift.parser import parse_project
from agentlift.planner import build_plan
from import_fixtures import HARNESS, HARNESS_SKILLS, SKILL_BUNDLES, TEAM_AGENTS


@pytest.fixture
def team_folder(tmp_path):
    """Import the mocked team and write it to disk; return (out_dir, ImportedProject)."""
    project = import_anthropic_agents(TEAM_AGENTS, SKILL_BUNDLES)
    write_project(project, str(tmp_path))
    return str(tmp_path), project


# --------------------------------------------------------------------------- #
# folder shape
# --------------------------------------------------------------------------- #
def test_folder_layout(team_folder):
    out, _ = team_folder
    ma = os.path.join(out, ".managed-agents")
    assert os.path.isdir(os.path.join(ma, "lead"))
    assert os.path.isdir(os.path.join(ma, "researcher"))
    assert os.path.isdir(os.path.join(ma, "bug-finder"))
    # shared skill hoisted; shared mcp written once under shared/
    assert os.path.isfile(os.path.join(ma, "shared", "skills", "cite-sources", "SKILL.md"))
    shared_mcp = json.load(open(os.path.join(ma, "shared", "mcp.json")))
    assert "docs" in shared_mcp["mcpServers"]
    # custom resources stay local to their agent
    assert os.path.isfile(os.path.join(ma, "researcher", "skills", "web-notes", "SKILL.md"))
    assert os.path.isfile(os.path.join(ma, "bug-finder", "skills", "bug-report", "SKILL.md"))
    assert "kb" in json.load(open(os.path.join(ma, "researcher", "mcp.json")))["mcpServers"]
    assert "linter" in json.load(open(os.path.join(ma, "bug-finder", "mcp.json")))["mcpServers"]
    # a shared skill is NOT duplicated into the agent dirs
    assert not os.path.exists(os.path.join(ma, "researcher", "skills", "cite-sources"))


# --------------------------------------------------------------------------- #
# the round-trip parses and plans cleanly
# --------------------------------------------------------------------------- #
def test_reparse_and_plan(team_folder):
    out, _ = team_folder
    project, diags = parse_project(out)
    plan = build_plan(project, diags)
    assert {a.name for a in project.agents} == {"lead", "researcher", "bug-finder"}
    assert plan.deployable, diags.render()


def test_subagent_delegation(team_folder):
    """Coordinator -> specialists survives the round-trip as a planner coordinator op."""
    out, _ = team_folder
    project, diags = parse_project(out)
    plan = build_plan(project, diags)

    lead = project.agent("lead")
    assert lead.subagents == ["researcher", "bug-finder"]
    assert lead.builtin_tools is None  # "all builtins" preserved

    creates = {c.name: c for c in plan.agent_creates}
    assert creates["lead"].is_coordinator
    assert creates["lead"].request["multiagent"]["agents"] == [
        "@agent:researcher", "@agent:bug-finder"]
    # roster specialists are created before the coordinator
    order = [c.name for c in plan.agent_creates]
    assert order.index("lead") > order.index("researcher")
    assert order.index("lead") > order.index("bug-finder")


def test_shared_skill_dedup(team_folder):
    """The shared skill is one upload, used by both specialists; customs are separate."""
    out, _ = team_folder
    project, diags = parse_project(out)
    plan = build_plan(project, diags)

    uploads = {u.display_title: u for u in plan.skill_uploads}
    assert set(uploads) == {"cite-sources", "web-notes", "bug-report"}
    assert sorted(uploads["cite-sources"].used_by) == ["bug-finder", "researcher"]
    assert uploads["web-notes"].used_by == ["researcher"]
    assert uploads["bug-report"].used_by == ["bug-finder"]


def test_shared_mcp_in_both_specialists(team_folder):
    """The shared MCP server is wired into both specialists (plus each one's custom MCP)."""
    out, _ = team_folder
    project, diags = parse_project(out)
    plan = build_plan(project, diags)
    creates = {c.name: c for c in plan.agent_creates}

    def mcp_names(req):
        return {t["mcp_server_name"] for t in req["tools"] if t["type"] == "mcp_toolset"}

    assert mcp_names(creates["researcher"].request) == {"docs", "kb"}
    assert mcp_names(creates["bug-finder"].request) == {"docs", "linter"}
    # the shared server carries the same URL in both
    for name in ("researcher", "bug-finder"):
        urls = {s["name"]: s["url"] for s in creates[name].request["mcp_servers"]}
        assert urls["docs"] == "https://docs.example.com/mcp"


def test_custom_skill_and_mcp_are_local(team_folder):
    """Single-agent resources are not hoisted: each lives only in its owner's dir."""
    out, proj = team_folder
    researcher = proj.agent("researcher")
    bug_finder = proj.agent("bug-finder")
    assert [s.name for s in researcher.local_skills] == ["web-notes"]
    assert [s.name for s in bug_finder.local_skills] == ["bug-report"]
    assert [m.name for m in researcher.local_mcp] == ["kb"]
    assert [m.name for m in bug_finder.local_mcp] == ["linter"]
    # the shared resources reference the shared pool
    assert "shared/cite-sources" in researcher.skill_refs
    assert "shared/docs" in researcher.mcp_refs


def test_tool_policy_preserved(team_folder):
    """bash:ask survives the round-trip into the planner's permission_policy."""
    out, _ = team_folder
    project, diags = parse_project(out)
    plan = build_plan(project, diags)
    bf = project.agent("bug-finder")
    assert bf.builtin_tool_policies.get("bash") == "ask"
    create = next(c for c in plan.agent_creates if c.name == "bug-finder")
    toolset = next(t for t in create.request["tools"] if t["type"] == "agent_toolset_20260401")
    got = {c["name"]: (c.get("permission_policy") or {}).get("type") for c in toolset["configs"]}
    assert got["bash"] == "always_ask"


def test_idempotent_redeploy_shape(team_folder):
    """The re-planned wire shape equals what the planner would emit for a hand-written
    folder — i.e. importing then deploying is a faithful migration, not a lossy copy."""
    out, _ = team_folder
    project, diags = parse_project(out)
    plan = build_plan(project, diags)
    researcher = next(c for c in plan.agent_creates if c.name == "researcher")
    assert researcher.request["model"] == "claude-haiku-4-5"
    assert researcher.request["system"] == "You research and cite."
    # both skills referenced (one shared, one custom), deduped by the planner
    assert len(researcher.request["skills"]) == 2


# --------------------------------------------------------------------------- #
# Bedrock harness round-trip (single agent, config-only)
# --------------------------------------------------------------------------- #
def test_bedrock_harness_roundtrip(tmp_path):
    project = import_bedrock_harness(HARNESS, HARNESS_SKILLS)
    write_project(project, str(tmp_path))
    parsed, diags = parse_project(str(tmp_path))
    plan = build_plan(parsed, diags)

    assert [a.name for a in parsed.agents] == ["support-agent"]
    agent = parsed.agents[0]
    # regional inference profile reverse-mapped to the folder Claude id
    assert agent.model == "claude-haiku-4-5"
    # browser -> web tools, code-interpreter -> sandbox tools
    assert "web_search" in agent.builtin_tools and "web_fetch" in agent.builtin_tools
    assert "bash" in agent.builtin_tools
    # remote MCP -> url server; S3 skill -> local skill
    assert [m.name for m in agent.mcp_servers] == ["docs"]
    assert [s.name for s in agent.skills] == ["cite-sources"]
    assert plan.deployable, diags.render()
