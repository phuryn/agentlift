"""`agentlift export`: anthropic-yaml round-trips to the wire shape; google-adk
emits valid Python with the audit's gaps annotated inline."""
import os

import yaml

from agentlift.export import export_anthropic_yaml, export_google_adk
from agentlift.parser import parse_project
from agentlift.planner import build_plan


def _team(examples_dir):
    project, diags = parse_project(os.path.join(examples_dir, "team"))
    return project, build_plan(project, diags)


# --- anthropic-yaml -------------------------------------------------------- #
def test_anthropic_yaml_round_trips_and_maps(examples_dir):
    project, plan = _team(examples_dir)
    files = export_anthropic_yaml(project, plan)

    assert {"bug-finder.agent.yaml", "researcher.agent.yaml", "lead.agent.yaml"} <= set(files)
    assert "SKILLS.txt" in files

    body = yaml.safe_load(files["researcher.agent.yaml"])   # must be valid YAML
    assert body["name"] == "researcher"
    assert body["model"]
    assert body["system"].strip()
    assert any(t["type"] == "agent_toolset_20260401" for t in body["tools"])
    assert any(t.get("type") == "mcp_toolset" for t in body["tools"])
    assert body["mcp_servers"][0] == {"type": "url", "name": "docs", "url": "https://example.com/mcp"}
    assert "cite-sources" in body["skills"]   # skill referenced by readable title, not @skill:hash


def test_anthropic_yaml_coordinator(examples_dir):
    project, plan = _team(examples_dir)
    lead = yaml.safe_load(export_anthropic_yaml(project, plan)["lead.agent.yaml"])
    assert lead["multiagent"]["type"] == "coordinator"
    assert set(lead["multiagent"]["agents"]) == {"bug-finder", "researcher"}  # @agent: stripped


def test_anthropic_yaml_preserves_ask_permission(examples_dir):
    project, plan = _team(examples_dir)
    body = yaml.safe_load(export_anthropic_yaml(project, plan)["bug-finder.agent.yaml"])
    builtin = next(t for t in body["tools"] if t["type"] == "agent_toolset_20260401")
    bash = next(c for c in builtin["configs"] if c["name"] == "bash")
    assert bash["permission_policy"]["type"] == "always_ask"


def test_anthropic_yaml_no_symbolic_refs_leak(examples_dir):
    project, plan = _team(examples_dir)
    for text in export_anthropic_yaml(project, plan).values():
        assert "@skill:" not in text
        assert "@agent:" not in text


# --- google-adk ------------------------------------------------------------ #
def test_google_adk_is_valid_python(examples_dir):
    project, _ = _team(examples_dir)
    code = export_google_adk(project)["agent.py"]
    compile(code, "agent.py", "exec")          # must parse
    assert "LlmAgent(" in code
    assert "sub_agents=[" in code
    assert "root_agent = " in code
    assert "McpToolset(" in code               # the docs MCP server maps over
    assert "def vertex_model(" in code         # runnable model mapping, not a stale literal
    assert "AGENTLIFT_GOOGLE_MODEL" in code     # env override for when model names change
    assert "model=vertex_model(" in code        # every agent resolves its model through it


def test_google_adk_annotates_the_gaps(examples_dir):
    project, _ = _team(examples_dir)
    code = export_google_adk(project)["agent.py"]
    # the same gaps the audit reports for Google must appear as inline warnings
    assert "UNSUPPORTED" in code               # :ask
    assert "DEGRADED" in code                  # sandbox


# --- openai-agents --------------------------------------------------------- #
def test_openai_agents_is_valid_python_with_subagent_tools(examples_dir):
    from agentlift.export import export_openai_agents
    project, _ = _team(examples_dir)
    code = export_openai_agents(project)["agent.py"]
    compile(code, "agent.py", "exec")          # must parse
    assert "Agent(" in code
    assert ".as_tool(" in code                 # subagents exposed as tools
    assert "Runner.run(" in code               # runnable entrypoint
    # the coordinator's roster shows up as ask_<name> tools
    assert "ask_bug_finder" in code and "ask_researcher" in code
