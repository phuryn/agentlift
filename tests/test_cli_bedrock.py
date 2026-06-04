"""The CLI's Bedrock surface: `plan --target bedrock` is a pure dry run (text +
JSON) and `export bedrock-strands` emits the runnable AgentCore package. The
headline: Claude is native (a regional inference profile), not remapped. The
hosted deploy path is covered separately (test_cli_bedrock_deploy). No network."""
import json
import os

from agentlift.cli import main


# --- plan --target bedrock ------------------------------------------------- #
def test_plan_bedrock_text(examples_dir, capsys):
    rc = main(["plan", os.path.join(examples_dir, "team"), "--target", "bedrock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "AgentCore Runtime: agentlift-lead" in out
    assert "region: eu-north-1" in out
    assert "coordinator -> bug-finder, researcher" in out
    # Claude maps NATIVELY to a regional inference profile (not Gemini-style remap)
    assert "claude-haiku-4-5 -> eu.anthropic.claude-haiku-4-5-20251001-v1:0" in out
    assert "mcp: docs=https://example.com/mcp" in out
    assert "skills:" in out
    assert "Deployable: yes" in out


def test_plan_bedrock_json(examples_dir, capsys):
    rc = main(["plan", os.path.join(examples_dir, "team"), "--target", "bedrock", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["root_agent"] == "lead"
    assert data["region"] == "eu-north-1"
    assert "spec_hash" in data and data["deployable"] is True


def test_plan_bedrock_region_changes_hash(examples_dir, capsys):
    main(["plan", os.path.join(examples_dir, "team"), "--target", "bedrock", "--json"])
    eu = json.loads(capsys.readouterr().out)
    main(["plan", os.path.join(examples_dir, "team"), "--target", "bedrock", "--json",
          "--bedrock-region", "us-east-1"])
    us = json.loads(capsys.readouterr().out)
    # a different region => different regional inference profile => different artifact
    assert eu["spec_hash"] != us["spec_hash"]
    lead_us = next(a for a in us["agents"] if a["name"] == "lead")
    assert lead_us["bedrock_model"].startswith("us.anthropic.")


# --- export bedrock-strands ------------------------------------------------ #
def test_export_bedrock_strands_to_dir(examples_dir, tmp_path, capsys):
    out_dir = os.path.join(str(tmp_path), "pkg")
    rc = main(["export", "bedrock-strands", os.path.join(examples_dir, "team"),
               "--out", out_dir])
    assert rc == 0
    agent_py = os.path.join(out_dir, "agentlift_runtime", "agent.py")
    assert os.path.isfile(agent_py)
    src = open(agent_py, encoding="utf-8").read()
    assert "BedrockAgentCoreApp()" in src
    assert "def _build_lead_agent(ctx):" in src
    # the package depends only on the runtime deps, never agentlift
    reqs = open(os.path.join(out_dir, "requirements.txt"), encoding="utf-8").read()
    assert "strands-agents" in reqs and "bedrock-agentcore" in reqs
    notes = open(os.path.join(out_dir, "NOTES.txt"), encoding="utf-8").read()
    assert "POST /invocations" in notes
    assert "skills/cite-sources/SKILL.md" in notes


def test_export_bedrock_stdout(examples_dir, capsys):
    rc = main(["export", "bedrock-strands", os.path.join(examples_dir, "team")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "agentlift_runtime/agent.py" in out
    assert "BedrockAgentCoreApp" in out
