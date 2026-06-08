"""Round-trip spike: mocked Anthropic read-API responses -> folder -> parser -> planner.

Proves the import mapping is faithful WITHOUT touching the network. We hand the
prototype importer dicts shaped exactly like `BetaManagedAgentsAgent.model_dump()`
(verified against anthropic SDK 0.107.1), let it write a `.managed-agents/` tree,
then run the REAL `parser.build_project` + `planner.build_plan` over it and assert
the reconstructed agents carry the tools/skills/mcp/subagents we started with.

Run:  python experiments/import-roundtrip/test_roundtrip.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from agentlift.parser import parse_project            # the REAL parser
from agentlift.planner import build_plan              # the REAL planner
from prototype_import import import_agent

# ---- Mocked provider read responses (shape == BetaManagedAgentsAgent.model_dump) ----

SKILL_BUNDLES = {
    "skill_bugrep_001": {"bug-report/SKILL.md": b"---\nname: bug-report\ndescription: Write a crisp bug report.\n---\nReport the smallest fix.\n"},
    "skill_cite_002":   {"cite-sources/SKILL.md": b"---\nname: cite-sources\ndescription: Cite your sources.\n---\nAlways cite.\n"},
}
SKILL_NAMES = {"skill_bugrep_001": "bug-report", "skill_cite_002": "cite-sources"}

BUG_FINDER = {
    "id": "agent_bug_finder", "type": "agent", "name": "bug-finder",
    "description": "Reads code and finds the one-line bug.",
    "system": "You are the Bug Finder. Find the smallest bug.",
    "model": {"model": "claude-haiku-4-5"},
    "tools": [
        {"type": "agent_toolset_20260401", "default_config": {"enabled": False},
         "configs": [
             {"name": "read", "enabled": True},
             {"name": "glob", "enabled": True},
             {"name": "grep", "enabled": True},
             {"name": "bash", "enabled": True, "permission_policy": {"type": "always_ask"}},
         ]},
        {"type": "mcp_toolset", "mcp_server_name": "docs",
         "default_config": {"enabled": False},
         "configs": [{"name": "search", "enabled": True}]},
    ],
    "mcp_servers": [{"type": "url", "name": "docs", "url": "https://example.com/mcp"}],
    "skills": [{"type": "custom", "skill_id": "skill_bugrep_001"},
               {"type": "custom", "skill_id": "skill_cite_002"}],
    "multiagent": None,
}

LEAD = {
    "id": "agent_lead", "type": "agent", "name": "lead",
    "description": "Coordinator.",
    "system": "You delegate to specialists.",
    "model": {"model": "claude-haiku-4-5"},
    "tools": [{"type": "agent_toolset_20260401", "default_config": {"enabled": True}}],  # all builtins
    "mcp_servers": [],
    "skills": [],
    "multiagent": {"type": "coordinator", "agents": [{"id": "agent_bug_finder", "name": "bug-finder"}]},
}


def main() -> int:
    with tempfile.TemporaryDirectory() as out:
        diags: list[str] = []
        for agent in (BUG_FINDER, LEAD):
            import_agent(agent, skill_bundles=SKILL_BUNDLES, skill_names=SKILL_NAMES,
                         out_root=out, diagnostics=diags)

        # --- the REAL pipeline accepts the imported folder ---
        project, pdiags = parse_project(out)
        plan = build_plan(project)

        names = {a.name for a in project.agents}
        assert names == {"bug-finder", "lead"}, names

        bf = project.agent("bug-finder")
        assert bf.builtin_tools == ["read", "glob", "grep", "bash"], bf.builtin_tools
        assert bf.builtin_tool_policies.get("bash") == "ask", bf.builtin_tool_policies
        assert {s.name for s in bf.skills} == {"bug-report", "cite-sources"}, bf.skills
        assert [m.name for m in bf.mcp_servers] == ["docs"], bf.mcp_servers
        assert bf.mcp_servers[0].url == "https://example.com/mcp"
        assert bf.mcp_servers[0].allowed_tools == ["search"], bf.mcp_servers[0].allowed_tools

        lead = project.agent("lead")
        assert lead.subagents == ["bug-finder"], lead.subagents
        assert lead.builtin_tools is None, "coordinator kept 'all builtins'"

        # --- the plan is valid and re-derives the same wire shape ---
        assert plan.deployable, [d.__dict__ for d in plan.diagnostics.items]
        creates = {c.name: c for c in plan.agent_creates}
        bf_tools = creates["bug-finder"].request["tools"]
        bf_toolset = next(t for t in bf_tools if t["type"] == "agent_toolset_20260401")
        got = [(c["name"], (c.get("permission_policy") or {}).get("type"))
               for c in bf_toolset["configs"]]
        assert ("bash", "always_ask") in got, got
        assert creates["lead"].is_coordinator
        # planner re-emits the roster as a symbolic @agent ref
        assert creates["lead"].request["multiagent"]["agents"] == ["@agent:bug-finder"]

        print("ROUND-TRIP PASS  ", len(project.agents), "agents,",
              len(plan.skill_uploads), "skills,",
              "deployable=" + str(plan.deployable))
        if diags:
            print("diagnostics:", *diags, sep="\n  ")
        # show one reconstructed agent.md so the artifact is visible
        print("\n--- reconstructed bug-finder/agent.md ---")
        print(open(os.path.join(out, ".managed-agents", "bug-finder", "agent.md")).read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
