"""The CLI's Google surface: `plan --target google` is a pure dry run (text +
JSON), `deploy --target google --build-only` materializes the package without a
network, and an undeployable folder (stdio MCP) is refused. No network."""
import json
import os
import shutil

from agentlift.cli import main


def _copy(src_dir, tmp_path, name):
    dst = os.path.join(str(tmp_path), name)
    shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns(
        ".agentlift-lock.json", ".agentlift-*.json", "*.bak"))
    return dst


# --- plan --target google -------------------------------------------------- #
def test_plan_google_text(examples_dir, capsys):
    rc = main(["plan", os.path.join(examples_dir, "team"), "--target", "google"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Agent Engine: agentlift-lead" in out
    assert "coordinator -> bug-finder, researcher" in out
    assert "mcp: docs=https://example.com/mcp" in out
    assert "skills:" in out
    assert "Deployable: yes" in out


def test_plan_google_json(examples_dir, capsys):
    rc = main(["plan", os.path.join(examples_dir, "team"), "--target", "google", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["root_agent"] == "lead"
    assert data["deploy_model"] == "gemini-2.5-flash"
    assert "spec_hash" in data and data["deployable"] is True


def test_plan_google_model_override_changes_hash(examples_dir, capsys):
    main(["plan", os.path.join(examples_dir, "team"), "--target", "google", "--json"])
    a = json.loads(capsys.readouterr().out)["spec_hash"]
    main(["plan", os.path.join(examples_dir, "team"), "--target", "google", "--json",
          "--google-model", "gemini-2.5-pro"])
    b = json.loads(capsys.readouterr().out)["spec_hash"]
    assert a != b


# --- deploy --target google --build-only ----------------------------------- #
def test_deploy_google_build_only(examples_dir, tmp_path, capsys):
    dst = _copy(os.path.join(examples_dir, "team"), tmp_path, "team")
    rc = main(["deploy", dst, "--target", "google", "--build-only"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Built source package (no deploy)" in out
    agent_py = os.path.join(dst, ".agentlift-build", "google", "agentlift_engine", "agent.py")
    assert os.path.isfile(agent_py)
    # build-only must not have written a lockfile (no deploy happened)
    assert not os.path.isfile(os.path.join(dst, ".agentlift-google.json"))


def test_deploy_google_refuses_undeployable(fixtures_dir, tmp_path, capsys):
    dst = _copy(os.path.join(fixtures_dir, "gmail-agent"), tmp_path, "gmail")
    rc = main(["deploy", dst, "--target", "google", "--build-only"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Not deploying" in out
    assert "stdio" in out.lower()


def test_deploy_google_skip_unsupported_builds(fixtures_dir, tmp_path, capsys):
    dst = _copy(os.path.join(fixtures_dir, "gmail-agent"), tmp_path, "gmail")
    rc = main(["deploy", dst, "--target", "google", "--build-only", "--skip-unsupported"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Built source package" in out
