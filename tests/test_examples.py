"""The shipped examples must parse, plan, and be deployable — and the
in-a-project example must demonstrate isolation + shared-skill dedup."""
import os

from skylift.parser import parse_project
from skylift.planner import build_plan


def _plan(path):
    project, diags = parse_project(path)
    return project, build_plan(project, diags)


def test_all_examples_deployable(examples_dir):
    for name in ("quickstart", "team", "in-a-project"):  # all shipped examples
        project, plan = _plan(os.path.join(examples_dir, name))
        assert plan.deployable, f"{name}: {plan.diagnostics.render()}"


def test_inside_a_repo_isolation_and_dedup(examples_dir):
    project, plan = _plan(os.path.join(examples_dir, "in-a-project"))

    # only the 4 agents under .managed-agents/ — the local pr-reviewer subagent
    # and the repo's app code are never picked up
    assert sorted(a.name for a in project.agents) == [
        "fact-checker", "orchestrator", "researcher", "summarizer",
    ]

    # the repo-root CLAUDE.md must not leak into any agent
    for ac in plan.agent_creates:
        assert "INTERNAL_ONLY_MARKER" not in ac.request["system"]

    # two shared skills, each uploaded once, shared across the right agents
    uploads = {u.display_title: u for u in plan.skill_uploads}
    assert set(uploads) == {"cite-sources", "house-style"}
    assert sorted(uploads["cite-sources"].used_by) == ["fact-checker", "researcher"]
    assert sorted(uploads["house-style"].used_by) == ["researcher", "summarizer"]

    # coordinator over the three roster agents
    orch = next(a.request for a in plan.agent_creates if a.name == "orchestrator")
    assert set(orch["multiagent"]["agents"]) == {
        "@agent:researcher", "@agent:summarizer", "@agent:fact-checker",
    }
