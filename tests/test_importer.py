"""Unit tests for the pure importer mapping (the inverse-of-planner contract).

These assert the mapping directly on canned provider dicts — the import analogue of
tests/test_planner.py. The folder round-trip lives in tests/test_import_roundtrip.py.
"""
from __future__ import annotations

from agentlift.diagnostics import Diagnostics
from agentlift.importer import (
    decode_tools,
    import_anthropic_agents,
    import_bedrock_harness,
    reverse_bedrock_model,
)
from import_fixtures import HARNESS, HARNESS_SKILLS, SKILL_BUNDLES, TEAM_AGENTS


# --------------------------------------------------------------------------- #
# tool decoding (inverse of planner._build_tools)
# --------------------------------------------------------------------------- #
def test_decode_all_builtins():
    tokens, pols, mcp = decode_tools(
        [{"type": "agent_toolset_20260401", "default_config": {"enabled": True}}],
        "a", Diagnostics())
    assert tokens is None and pols == {} and mcp == {}


def test_decode_allowlist_with_policy():
    tools = [{"type": "agent_toolset_20260401", "default_config": {"enabled": False},
              "configs": [{"name": "read"}, {"name": "bash", "permission_policy": {"type": "always_ask"}}]}]
    tokens, pols, _ = decode_tools(tools, "a", Diagnostics())
    # canonical shape: bare names + a separate policy map (folder_writer reattaches :ask)
    assert tokens == ["read", "bash"]
    assert pols == {"bash": "ask"}


def test_decode_mcp_filter():
    tools = [{"type": "mcp_toolset", "mcp_server_name": "docs", "default_config": {"enabled": False},
              "configs": [{"name": "search", "permission_policy": {"type": "always_allow"}}]}]
    _, _, mcp = decode_tools(tools, "a", Diagnostics())
    assert mcp["docs"]["allowed"] == ["search"]
    assert mcp["docs"]["policies"] == {"search": "allow"}


def test_decode_mcp_all_tools():
    tools = [{"type": "mcp_toolset", "mcp_server_name": "docs", "default_config": {"enabled": True}}]
    _, _, mcp = decode_tools(tools, "a", Diagnostics())
    assert mcp["docs"]["allowed"] is None


def test_custom_tool_diagnostic():
    diags = Diagnostics()
    decode_tools([{"type": "custom_tool", "name": "my_fn"}], "a", diags)
    assert any(d.code == "import.custom_tool_dropped" for d in diags.items)


# --------------------------------------------------------------------------- #
# model reverse-map (Bedrock)
# --------------------------------------------------------------------------- #
def test_reverse_bedrock_aliased():
    assert reverse_bedrock_model("eu.anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"
    assert reverse_bedrock_model("us.anthropic.claude-sonnet-4-5-20250929-v1:0") == "claude-sonnet-4-5"


def test_reverse_bedrock_bare_id_passthrough():
    # a newer model whose profile slug IS the bare id keeps it
    assert reverse_bedrock_model("us.anthropic.claude-opus-4-8") == "claude-opus-4-8"


def test_reverse_bedrock_non_claude_passthrough():
    assert reverse_bedrock_model("us.amazon.nova-pro-v1:0") == "us.amazon.nova-pro-v1:0"
    assert reverse_bedrock_model("claude-haiku-4-5") == "claude-haiku-4-5"


# --------------------------------------------------------------------------- #
# Anthropic import mapping
# --------------------------------------------------------------------------- #
def test_anthropic_roster_id_to_name():
    proj = import_anthropic_agents(TEAM_AGENTS, SKILL_BUNDLES)
    lead = proj.agent("lead")
    assert lead.subagents == ["researcher", "bug-finder"]


def test_anthropic_hoists_shared_resources():
    proj = import_anthropic_agents(TEAM_AGENTS, SKILL_BUNDLES)
    assert [s.name for s in proj.shared_skills] == ["cite-sources"]
    assert [s.name for s in proj.shared_mcp] == ["docs"]
    # custom resources are NOT in the shared pool
    researcher = proj.agent("researcher")
    assert [s.name for s in researcher.local_skills] == ["web-notes"]


def test_anthropic_skill_content_hash_matches_dedup():
    """The imported shared skill's hash equals the bundle hash, so the planner dedups it."""
    proj = import_anthropic_agents(TEAM_AGENTS, SKILL_BUNDLES)
    cite = proj.shared_skills[0]
    assert cite.content_hash == SKILL_BUNDLES["skill_cite"].content_hash


def test_anthropic_managed_skill_ref_diagnostic():
    agent = {"id": "a1", "name": "a1", "system": "s", "model": {"model": "claude-haiku-4-5"},
             "tools": [], "mcp_servers": [],
             "skills": [{"type": "anthropic", "skill_id": "pdf"}], "multiagent": None}
    proj = import_anthropic_agents([agent], {})
    assert any(d.code == "import.anthropic_skill_ref" for d in proj.diagnostics.items)
    assert proj.agent("a1").skill_refs == []


def test_hoist_same_content_different_names():
    """Two agents whose skills have identical content but were given DIFFERENT directory
    names hoist cleanly: each agent drops its own local name and references the one
    canonical shared skill (regression for the name-vs-content-hash hoisting bug)."""
    from agentlift.import_model import ImportedSkill
    from agentlift.importer import hash_skill_files

    # same bytes under each agent's own dir name -> same content_hash, different names
    files = {"shared-thing/SKILL.md": b"---\nname: x\ndescription: d\n---\nidentical body\n"}
    h = hash_skill_files(files)
    a_sk = ImportedSkill(name="alpha-notes", files=files, content_hash=h)
    b_sk = ImportedSkill(name="beta-notes", files=files, content_hash=h)

    a = {"id": "a", "name": "a", "system": "s", "model": {"model": "claude-haiku-4-5"},
         "tools": [], "mcp_servers": [], "multiagent": None,
         "skills": [{"type": "custom", "skill_id": "ka"}]}
    b = {"id": "b", "name": "b", "system": "s", "model": {"model": "claude-haiku-4-5"},
         "tools": [], "mcp_servers": [], "multiagent": None,
         "skills": [{"type": "custom", "skill_id": "kb"}]}
    proj = import_anthropic_agents([a, b], {"ka": a_sk, "kb": b_sk})

    # one canonical shared skill, both agents reference it, neither keeps a local copy or a stale ref
    assert len(proj.shared_skills) == 1
    canonical = proj.shared_skills[0].name
    for ag in ("a", "b"):
        agent = proj.agent(ag)
        assert agent.local_skills == []
        assert agent.skill_refs == [f"shared/{canonical}"]


def test_anthropic_distinct_filters_stay_local():
    """Same server name but different tool filters must NOT be hoisted to shared."""
    a = {"id": "a", "name": "a", "system": "s", "model": {"model": "claude-haiku-4-5"},
         "mcp_servers": [{"type": "url", "name": "docs", "url": "https://d/mcp"}],
         "tools": [{"type": "mcp_toolset", "mcp_server_name": "docs", "default_config": {"enabled": False},
                    "configs": [{"name": "search"}]}], "skills": [], "multiagent": None}
    b = {"id": "b", "name": "b", "system": "s", "model": {"model": "claude-haiku-4-5"},
         "mcp_servers": [{"type": "url", "name": "docs", "url": "https://d/mcp"}],
         "tools": [{"type": "mcp_toolset", "mcp_server_name": "docs", "default_config": {"enabled": False},
                    "configs": [{"name": "query"}]}], "skills": [], "multiagent": None}
    proj = import_anthropic_agents([a, b], {})
    assert proj.shared_mcp == []  # filters differ -> not identical -> stays local
    assert [m.name for m in proj.agent("a").local_mcp] == ["docs"]


# --------------------------------------------------------------------------- #
# Bedrock harness import mapping
# --------------------------------------------------------------------------- #
def test_harness_single_agent_no_subagents():
    proj = import_bedrock_harness(HARNESS, HARNESS_SKILLS)
    assert len(proj.agents) == 1
    assert proj.agents[0].subagents == []


def test_harness_browser_and_ci_to_builtins():
    proj = import_bedrock_harness(HARNESS, HARNESS_SKILLS)
    tools = proj.agents[0].builtin_tools
    assert "web_search" in tools and "web_fetch" in tools  # agentCoreBrowser
    assert "bash" in tools                                  # agentCoreCodeInterpreter


def test_harness_mcp_auth_env_name_only():
    proj = import_bedrock_harness(HARNESS, HARNESS_SKILLS)
    docs = proj.agents[0].local_mcp[0]
    assert docs.name == "docs"
    assert docs.auth_env_names == ["Authorization"]  # name only, never the value
    assert any(d.code == "import.mcp_auth_env" for d in proj.diagnostics.items)
