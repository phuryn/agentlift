"""The CLI's Bedrock *harness* surface (``--mode``). ``plan --target bedrock``
routes by ``--mode``: ``auto`` sends a single skill-less agent to the managed
harness and a multi-agent/subagent folder to the runtime (never a silent
downgrade); ``--mode harness`` / ``--mode runtime`` force the primitive. The
harness plan is a pure dry run (text + JSON); the deploy path runs a real
(preview) create but refuses up front when no execution role is set -- so this
exercises every branch with NO network."""
import json
import os
import shutil

import pytest

from agentlift.cli import main
from agentlift.harness_lock import HARNESS_LOCKFILE_NAME


def _copy(src_dir, tmp_path, name):
    dst = os.path.join(str(tmp_path), name)
    shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns(
        ".agentlift-*.json", "*.bak", ".agentlift-build"))
    return dst


def _mcp_auth(fixtures_dir, tmp_path):
    return _copy(os.path.join(fixtures_dir, "mcp-auth"), tmp_path, "mcp-auth")


# --- plan routing ---------------------------------------------------------- #
def test_plan_auto_routes_single_agent_to_harness(fixtures_dir, capsys):
    # mcp-auth = one skill-less agent with a URL MCP server -> auto picks harness
    rc = main(["plan", os.path.join(fixtures_dir, "mcp-auth"), "--target", "bedrock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode: harness" in out
    assert "AgentCore Harness: agentlift-api  (name: agentlift_api, region: us-west-2)" in out
    # Claude maps NATIVELY to a regional inference profile (not Gemini-style remap)
    assert "claude-haiku-4-5 -> us.anthropic.claude-haiku-4-5-20251001-v1:0" in out
    assert "mcp: secure=https://secure.internal.example.com/mcp" in out
    assert "auth->AGENTLIFT_MCP_SECURE_AUTHORIZATION" in out
    assert "PREVIEW" in out          # honest: provisional wire shape
    assert "Deployable: yes" in out


def test_plan_auto_routes_team_to_runtime(examples_dir, capsys):
    # team has subagents -> auto must NOT downgrade to the single-agent harness
    rc = main(["plan", os.path.join(examples_dir, "team"), "--target", "bedrock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode: runtime" in out
    assert "AgentCore Runtime: agentlift-lead" in out
    assert "region: eu-north-1" in out          # runtime's default region, unchanged


def test_plan_force_runtime_on_single_agent(fixtures_dir, capsys):
    rc = main(["plan", os.path.join(fixtures_dir, "mcp-auth"),
               "--target", "bedrock", "--mode", "runtime"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode: runtime" in out
    assert "AgentCore Runtime:" in out
    assert "region: eu-north-1" in out          # runtime default, not the harness us-west-2


def test_plan_force_harness_region_default(fixtures_dir, capsys):
    rc = main(["plan", os.path.join(fixtures_dir, "mcp-auth"),
               "--target", "bedrock", "--mode", "harness"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "region: us-west-2" in out           # harness preview default


def test_plan_explicit_region_wins(fixtures_dir, capsys):
    rc = main(["plan", os.path.join(fixtures_dir, "mcp-auth"), "--target", "bedrock",
               "--mode", "harness", "--bedrock-region", "eu-central-1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "region: eu-central-1" in out
    assert "claude-haiku-4-5 -> eu.anthropic." in out   # region flows into the profile


def test_plan_harness_json(fixtures_dir, capsys):
    rc = main(["plan", os.path.join(fixtures_dir, "mcp-auth"),
               "--target", "bedrock", "--mode", "harness", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["mode"] == "harness"
    assert data["region"] == "us-west-2"
    assert data["live_verified"] is True         # wire shape verified by committed receipt
    assert data["deployable"] is True
    assert "spec_hash" in data
    assert data["bedrock_model"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# --- deploy guards (no network) -------------------------------------------- #
def test_deploy_harness_without_role_refuses(fixtures_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN", raising=False)
    root = _mcp_auth(fixtures_dir, tmp_path)
    rc = main(["deploy", root, "--target", "bedrock", "--mode", "harness", "--yes"])
    cap = capsys.readouterr()
    assert rc == 2
    assert "execution role" in (cap.out + cap.err).lower()
    assert "PREVIEW" in cap.out                    # banner still printed
    # refused before any network call -> no lock written
    assert not os.path.isfile(os.path.join(root, HARNESS_LOCKFILE_NAME))


def test_deploy_harness_build_only_is_not_applicable(fixtures_dir, tmp_path, capsys):
    root = _mcp_auth(fixtures_dir, tmp_path)
    rc = main(["deploy", root, "--target", "bedrock", "--mode", "harness", "--build-only"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "build-only is not applicable" in out
    assert not os.path.isfile(os.path.join(root, HARNESS_LOCKFILE_NAME))


def test_deploy_auto_guard_refuses_when_unverified(fixtures_dir, tmp_path, capsys, monkeypatch):
    # The auto-deploy guard is keyed on _HARNESS_LIVE_VERIFIED. While False, a *bare*
    # deploy (auto, not typed --mode harness) must refuse rc 2 and demand the explicit
    # opt-in -- never silently create a live preview resource (the guard fires before any
    # network: no lock). Force the flag False to pin the mechanism even post-receipt.
    import agentlift.harness_plan as hp
    monkeypatch.setattr(hp, "_HARNESS_LIVE_VERIFIED", False)
    monkeypatch.setenv("AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN",
                       "arn:aws:iam::111122223333:role/agentlift-harness")
    root = _mcp_auth(fixtures_dir, tmp_path)
    rc = main(["deploy", root, "--target", "bedrock", "--yes"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "mode: harness" in out                       # auto's choice is still shown
    assert "Pass --mode harness to opt into" in out     # the explicit opt-in
    assert "not yet receipt-verified" in out
    assert not os.path.isfile(os.path.join(root, HARNESS_LOCKFILE_NAME))


def test_deploy_auto_reaches_harness_create_when_verified(fixtures_dir, tmp_path, capsys, monkeypatch):
    # Post-receipt (_HARNESS_LIVE_VERIFIED True, the default), a bare auto deploy of a
    # single skill-less agent proceeds straight to the live harness create -- no typed
    # opt-in needed. With a fake control client it completes + writes the lock (no network).
    monkeypatch.setenv("AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN",
                       "arn:aws:iam::111122223333:role/agentlift-harness")
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    root = _mcp_auth(fixtures_dir, tmp_path)

    import agentlift.harness_target as ht

    class _FakeControl:
        def create_harness(self, **kw):
            return {"harness": {"harnessId": "agentlift_api-xyz",
                                "arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:"
                                       "harness/agentlift_api-xyz", "status": "READY"}}

        def get_harness(self, **kw):
            return {"harness": {"harnessId": "agentlift_api-xyz", "status": "READY"}}

    monkeypatch.setattr(ht, "_default_control_client", lambda region: _FakeControl())
    rc = main(["deploy", root, "--target", "bedrock", "--yes"])   # bare auto, no --mode
    out = capsys.readouterr().out
    assert rc == 0
    assert "Deployed. harness:" in out
    assert "Pass --mode harness to opt into" not in out           # guard did not fire
    assert os.path.isfile(os.path.join(root, HARNESS_LOCKFILE_NAME))


def test_deploy_explicit_mode_harness_reaches_create_branch(fixtures_dir, tmp_path, capsys, monkeypatch):
    # typed --mode harness is the opt-in: it bypasses the auto-guard and reaches the
    # create branch, where a missing role refuses rc 2 (proving dispatch, no network).
    monkeypatch.delenv("AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN", raising=False)
    root = _mcp_auth(fixtures_dir, tmp_path)
    rc = main(["deploy", root, "--target", "bedrock", "--mode", "harness", "--yes"])
    cap = capsys.readouterr()
    assert rc == 2
    assert "execution role" in (cap.out + cap.err).lower()   # role refusal, not the auto-guard
    assert "Pass --mode harness to opt into" not in cap.out


def test_deploy_harness_create_writes_lock(fixtures_dir, tmp_path, capsys, monkeypatch):
    """With a role set + a fake control client (seam), the CLI harness path runs a
    create and records the lock -- the full happy path, still no real network."""
    monkeypatch.setenv("AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN",
                       "arn:aws:iam::111122223333:role/agentlift-harness")
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    root = _mcp_auth(fixtures_dir, tmp_path)

    import agentlift.harness_target as ht

    class _FakeControl:
        def create_harness(self, **kw):
            return {"harnessId": "agentlift_api-xyz",
                    "harnessArn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:"
                                  "harness/agentlift_api-xyz",
                    "status": "READY"}

        def get_harness(self, **kw):
            return {"harness": {"harnessId": "agentlift_api-xyz", "status": "READY"}}

    monkeypatch.setattr(ht, "_default_control_client", lambda region: _FakeControl())
    rc = main(["deploy", root, "--target", "bedrock", "--mode", "harness", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Deployed. harness:" in out
    assert os.path.isfile(os.path.join(root, HARNESS_LOCKFILE_NAME))
