"""Context isolation: a deployed agent gets ONLY its own folder (plus shared/),
never the repo-root CLAUDE.md, repo-root skills, or a sibling agent's skills.

In the local Agent SDK this isolation takes explicit flags (skills allowlist +
strictMcpConfig) because the CLI walks up the directory tree. In the managed
cloud the agent only ever gets what skylift uploads — and skylift scopes uploads
to the agent folder. This test pins that guarantee."""
import os

from skylift.parser import parse_project
from skylift.planner import build_plan


def test_repo_context_does_not_leak(fixtures_dir):
    root = os.path.join(fixtures_dir, "isolation")
    project, diags = parse_project(root)
    plan = build_plan(project, diags)
    assert plan.deployable

    alpha = next(a.request for a in plan.agent_creates if a.name == "alpha")

    # the repo-root CLAUDE.md must not be in alpha's system prompt
    assert "SECRET_MARKER_DO_NOT_LEAK" not in alpha["system"]
    assert "ALPHA_SYSTEM_PROMPT" in alpha["system"]

    # alpha uploads exactly its own skill — not the repo skill, not beta's
    titles = {u.display_title for u in plan.skill_uploads}
    assert "repo-skill" not in titles
    assert "alpha-skill" in titles and "beta-skill" in titles  # each agent's own
    alpha_skill_refs = [s["skill_ref"] for s in alpha.get("skills", [])]
    alpha_skill = next(u for u in plan.skill_uploads if u.display_title == "alpha-skill")
    beta_skill = next(u for u in plan.skill_uploads if u.display_title == "beta-skill")
    assert alpha_skill.ref in alpha_skill_refs
    assert beta_skill.ref not in alpha_skill_refs  # alpha cannot see beta's skill


def test_each_agent_scoped_to_its_folder(fixtures_dir):
    root = os.path.join(fixtures_dir, "isolation")
    project, _ = parse_project(root)
    alpha = project.agent("alpha")
    beta = project.agent("beta")
    assert [s.name for s in alpha.skills] == ["alpha-skill"]
    assert [s.name for s in beta.skills] == ["beta-skill"]
