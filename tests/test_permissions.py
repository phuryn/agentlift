"""Per-tool permission policies: `:ask` / `:allow` -> permission_policy on the
built-in toolset and on specific MCP tools. This is the deployable form of a
PreToolUse 'ask' hook."""
import os

from skylift.parser import parse_project
from skylift.planner import build_plan


def _plan(path):
    project, diags = parse_project(path)
    return project, build_plan(project, diags)


def _builtin_configs(req):
    for t in req["tools"]:
        if t["type"] == "agent_toolset_20260401":
            return {c["name"]: c for c in t.get("configs", [])}
    return {}


def test_builtin_tool_ask_policy(examples_dir):
    _project, plan = _plan(os.path.join(examples_dir, "team"))
    req = next(a.request for a in plan.agent_creates if a.name == "bug-finder")
    cfgs = _builtin_configs(req)
    # bash was declared as `bash:ask`
    assert cfgs["bash"]["permission_policy"] == {"type": "always_ask"}
    # the others have no policy (default allow)
    assert "permission_policy" not in cfgs["read"]


def test_mcp_specific_tool_ask_policy(fixtures_dir):
    _project, plan = _plan(os.path.join(fixtures_dir, "mcp-perm"))
    assert plan.deployable
    req = plan.agent_creates[0].request
    mcp_ts = [t for t in req["tools"] if t["type"] == "mcp_toolset"][0]
    cfgs = {c["name"]: c for c in mcp_ts["configs"]}
    # specific MCP tools, with create_issue gated behind approval
    assert set(cfgs) == {"search_issues", "create_issue"}
    assert "permission_policy" not in cfgs["search_issues"]
    assert cfgs["create_issue"]["permission_policy"] == {"type": "always_ask"}
