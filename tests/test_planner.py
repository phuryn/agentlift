import os

from agentlift.parser import parse_project
from agentlift.planner import build_plan


def _plan(path, **kw):
    project, diags = parse_project(path)
    return project, build_plan(project, diags, **kw)


def _toolset(req):
    for t in req["tools"]:
        if t["type"] == "agent_toolset_20260401":
            return t
    return None


def test_quickstart_plan(examples_dir):
    project, plan = _plan(os.path.join(examples_dir, "quickstart"))
    assert plan.deployable
    assert len(plan.skill_uploads) == 1
    assert plan.skill_uploads[0].display_title == "receipt-stamp"

    assert len(plan.agent_creates) == 1
    req = plan.agent_creates[0].request
    # tool allowlist: defaults off, read/glob/grep on
    ts = _toolset(req)
    assert ts["default_config"]["enabled"] is False
    assert {c["name"] for c in ts["configs"]} == {"read", "glob", "grep"}
    # knowledge inlined into the system prompt
    assert "Reference material" in req["system"]
    assert "North Star" in req["system"]
    # skill referenced symbolically
    assert req["skills"][0]["type"] == "custom"
    assert req["skills"][0]["skill_ref"].startswith("@skill:")


def test_team_dedup_and_coordinator(examples_dir):
    project, plan = _plan(os.path.join(examples_dir, "team"))
    assert plan.deployable

    # cite-sources is shared by all 3 agents -> ONE upload, used_by all three
    cite = [u for u in plan.skill_uploads if u.display_title == "cite-sources"]
    assert len(cite) == 1
    assert sorted(cite[0].used_by) == ["bug-finder", "lead", "researcher"] or \
        sorted(cite[0].used_by) == ["bug-finder", "researcher"]  # lead may not carry skills

    # coordinator (lead) is ordered AFTER its roster agents
    order = [a.name for a in plan.agent_creates]
    assert order.index("lead") > order.index("bug-finder")
    assert order.index("lead") > order.index("researcher")

    lead_req = next(a.request for a in plan.agent_creates if a.name == "lead")
    assert lead_req["multiagent"]["type"] == "coordinator"
    assert set(lead_req["multiagent"]["agents"]) == {"@agent:bug-finder", "@agent:researcher"}

    # researcher carries BOTH a shared url server (docs) and its own private one (search),
    # each with its own mcp_toolset + allowlist
    r_req = next(a.request for a in plan.agent_creates if a.name == "researcher")
    servers = {s["name"]: s for s in r_req["mcp_servers"]}
    assert servers["docs"] == {"type": "url", "name": "docs", "url": "https://example.com/mcp"}
    assert servers["search"] == {"type": "url", "name": "search", "url": "https://search.internal.example.com/mcp"}
    mcp_ts = {t["mcp_server_name"]: t for t in r_req["tools"] if t["type"] == "mcp_toolset"}
    assert mcp_ts["docs"]["configs"] == [{"name": "search", "enabled": True}]
    assert mcp_ts["search"]["configs"] == [{"name": "query", "enabled": True}]


def test_stdio_mcp_rejected_by_default(fixtures_dir):
    project, plan = _plan(os.path.join(fixtures_dir, "gmail-agent"))
    assert not plan.deployable
    assert any(d.code == "mcp.stdio_unsupported" for d in plan.diagnostics.errors)


def test_stdio_mcp_skip_unsupported(fixtures_dir):
    project, plan = _plan(os.path.join(fixtures_dir, "gmail-agent"), skip_unsupported=True)
    assert plan.deployable
    assert any(d.code == "mcp.stdio_skipped" for d in plan.diagnostics.warnings)
    # the dropped server is NOT in the request
    req = plan.agent_creates[0].request
    assert "mcp_servers" not in req


def test_xml_in_skill_description_rejected(fixtures_dir):
    # caught at plan time instead of as a cryptic API 400
    project, plan = _plan(os.path.join(fixtures_dir, "bad-skill"))
    assert not plan.deployable
    assert any(d.code == "skill.xml_in_description" for d in plan.diagnostics.errors)
