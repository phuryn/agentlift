"""Live, end-to-end: deploy the quickstart example to Anthropic Managed Agents,
run it, and confirm the uploaded skill is actually applied by the hosted runtime.
Requires ANTHROPIC_API_KEY (managed-agents beta). Costs a few cents. Cleans up.

Run with:  pytest -m live
"""
import os
import shutil

import pytest

from skylift.anthropic_target import Deployer
from skylift.graders import llm_grader, substring_grader
from skylift.parser import parse_project
from skylift.planner import build_plan
from skylift.runtime import run_local, run_managed

pytestmark = pytest.mark.live


@pytest.fixture
def deployed_quickstart(examples_dir, tmp_path, live_client):
    dst = os.path.join(str(tmp_path), "quickstart")
    shutil.copytree(os.path.join(examples_dir, "quickstart"), dst)
    project, diags = parse_project(dst)
    plan = build_plan(project, diags)
    assert plan.deployable, diags.render()
    deployer = Deployer(live_client, project.root)
    result = deployer.apply(plan, log=lambda *_: None)
    yield project, deployer, result
    deployer.destroy(log=lambda *_: None)


def test_deploy_and_skill_applies_in_cloud(deployed_quickstart, live_client):
    project, deployer, result = deployed_quickstart
    assert len(result.created_agents) == 1
    rec = deployer.lock.agent("knowledge-agent")
    assert rec and rec["agent_id"].startswith("agent_")

    res = run_managed(
        live_client, rec["agent_id"], rec["version"],
        "What is a North Star metric? Answer in one sentence.",
        model="claude-haiku-4-5",
    )
    assert res.ok, res.error
    assert res.output, "no output from managed agent"
    # the uploaded SKILL.md instructed a RECEIPT: line -> proves the skill rode along
    assert substring_grader(res.output, must_include=["RECEIPT:"]).passed, res.output
    # identity from the system prompt survived the deploy
    grade = llm_grader(
        live_client,
        "What is a North Star metric?",
        res.output,
        "The answer defines a North Star metric AND signs off as 'Best, Knowledge Agent'.",
    )
    assert grade.passed, grade.reason


def test_same_definition_runs_locally(deployed_quickstart, live_client):
    """Portability: the same folder runs on the local runtime too."""
    project, _deployer, _result = deployed_quickstart
    agent = project.agent("knowledge-agent")
    res = run_local(live_client, agent, "What is RICE prioritization? One sentence.")
    assert res.ok, res.error
    assert res.output
    assert substring_grader(res.output, must_include=["RECEIPT:"]).passed, res.output
