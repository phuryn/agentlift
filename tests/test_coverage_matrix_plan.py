"""Offline WIRED contract for the live coverage-matrix fixture.

``tests/live/fixtures/coverage-matrix`` is the single neutral folder deployed
live to BOTH providers by ``tests/live/coverage_matrix.py``. The assertions here
prove the *plan* wires all six portability dimensions on each provider (the
deterministic WIRED layer — "the plan is the contract"); the live harness proves
they are EXERCISED at runtime and records receipts under tests/live/receipts/.

Pure and offline: no credentials, no network, runs in CI. ``build_google_plan``
is pure too (the ADK import only happens in codegen/target), so both providers'
plans are asserted here.

Dimensions: agents · subagents · shared MCP · individual MCP · shared skill ·
individual skill.
"""
import os

from agentlift.google_plan import build_google_plan
from agentlift.parser import parse_project
from agentlift.planner import build_plan

HERE = os.path.dirname(os.path.abspath(__file__))
COVERAGE = os.path.join(HERE, "live", "fixtures", "coverage-matrix")


def _toolset(req):
    for t in req["tools"]:
        if t["type"] == "agent_toolset_20260401":
            return t
    return None


def test_anthropic_plan_wires_all_six_dimensions():
    project, diags = parse_project(COVERAGE)
    plan = build_plan(project, diags)
    assert plan.deployable, plan.diagnostics.render()
    reqs = {a.name: a for a in plan.agent_creates}

    # agents
    assert set(reqs) == {"lead", "researcher", "reporter"}

    # subagents: lead is a coordinator over researcher + reporter
    lead = reqs["lead"].request
    assert reqs["lead"].is_coordinator
    assert lead["multiagent"]["type"] == "coordinator"
    assert set(lead["multiagent"]["agents"]) == {"@agent:researcher", "@agent:reporter"}

    # shared MCP (docs) + individual MCP (code-search) both attached to researcher
    servers = {s["name"] for s in reqs["researcher"].request["mcp_servers"]}
    assert servers == {"docs", "code-search"}

    # shared skill (house-style) deduped to ONE upload, used by both agents
    hs = [u for u in plan.skill_uploads if u.display_title == "house-style"]
    assert len(hs) == 1 and sorted(hs[0].used_by) == ["reporter", "researcher"]
    # individual skill (report-format) private to reporter
    rf = [u for u in plan.skill_uploads if u.display_title == "report-format"]
    assert len(rf) == 1 and rf[0].used_by == ["reporter"]

    # the read fix (live-discovered): skill-bearing agents get `read` enabled so
    # Managed Agents can open SKILL.md, even though the folder set tools: []
    for who in ("researcher", "reporter"):
        ts = _toolset(reqs[who].request)
        assert any(c["name"] == "read" and c["enabled"] for c in ts["configs"]), who
    assert sum(1 for w in plan.diagnostics.warnings if w.code == "skills.read_enabled") == 2


def test_google_plan_wires_all_six_dimensions():
    project, _ = parse_project(COVERAGE)
    plan = build_google_plan(project)
    assert plan.deployable, plan.diagnostics.render()
    nodes = {n.name: n for n in plan.agents}

    # agents + coordinator root
    assert set(nodes) == {"lead", "researcher", "reporter"}
    assert plan.root_agent == "lead"

    # subagents: lead delegates to researcher + reporter (transfer_to_agent targets)
    assert sorted(nodes["lead"].sub_agents) == ["reporter", "researcher"]
    assert nodes["lead"].is_coordinator

    # shared MCP (docs) + individual MCP (code-search) lowered onto researcher
    assert {m.server for m in nodes["researcher"].mcp} == {"docs", "code-search"}

    # shared skill bundle used by both; individual bundle private to reporter
    bundles = {b.name: b for b in plan.skill_bundles}
    assert sorted(bundles["house-style"].used_by) == ["reporter", "researcher"]
    assert bundles["report-format"].used_by == ["reporter"]
    # researcher loads the shared skill; reporter loads both
    assert nodes["researcher"].skills == ["house-style"]
    assert set(nodes["reporter"].skills) == {"house-style", "report-format"}

    # Claude model in the folder is mapped to Gemini for Agent Engine
    assert plan.deploy_model.startswith("gemini")
