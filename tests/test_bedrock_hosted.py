"""The Stage 2 hosted AgentCore Runtime deploy path, offline.

The wire shape (`CreateAgentRuntime`/`InvokeAgentRuntime`) is *encoded* here and gated
behind `_RUNTIME_LIVE_VERIFIED` -- a bare `deploy_bedrock(build_only=False)` still REFUSES
until a committed live receipt flips the flag (the confirm-live-before-encoding rule). These
tests drive the create/poll/lock/invoke flow against fakes (no boto3, no Docker, no network)
so the one billable live run is one-shot, and pin:

  - the CreateAgentRuntime body (ARM64 image + role + PUBLIC network + HTTP protocol, no JWT
    authorizer, MCP-auth env vars), built only after the gate is open;
  - ECR repo create + image build/push are seam-driven (recorded, never really run);
  - idempotency: first deploy -> create + lock; unchanged spec -> skip (no build/push/API);
  - InvokeAgentRuntime: arn + a >=33-char runtimeSessionId + a JSON payload, parsed response;
  - the gate itself: refuse while the flag is False, run the create when forced True.
"""
import json
import os
import shutil

import pytest

from agentlift import bedrock_target as bt
from agentlift.bedrock_lock import BEDROCK_LOCKFILE_NAME, BedrockLock
from agentlift.bedrock_plan import build_bedrock_plan
from agentlift.bedrock_target import (
    HostedDeployNotLiveVerified,
    RuntimeExecutionRoleRequired,
    _extract_runtime,
    _runtime_create_body,
    deploy_bedrock,
    invoke_agent_runtime,
)
from agentlift.parser import parse_project

ACCOUNT = "424242424242"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/agentlift-runtime"


def _copy(src_dir, tmp_path, name):
    dst = os.path.join(str(tmp_path), name)
    shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns(
        ".agentlift-lock.json", ".agentlift-*.json", "*.bak", ".agentlift-build"))
    return dst


def _team(examples_dir, tmp_path):
    root = _copy(os.path.join(examples_dir, "team"), tmp_path, "team")
    project, _ = parse_project(root)
    return project, root


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _FakeControl:
    def __init__(self, account=ACCOUNT, region="eu-north-1"):
        self.create_calls, self.update_calls, self.get_calls = [], [], []
        self._arn = (f"arn:aws:bedrock-agentcore:{region}:{account}:"
                     f"runtime/agentlift_research_lead-xyz789")

    def create_agent_runtime(self, **kw):
        self.create_calls.append(kw)
        return {"agentRuntime": {"agentRuntimeId": "agentlift_research_lead-xyz789",
                                 "agentRuntimeArn": self._arn, "status": "CREATING"}}

    def update_agent_runtime(self, **kw):
        self.update_calls.append(kw)
        return {"agentRuntime": {"agentRuntimeId": kw["agentRuntimeId"],
                                 "agentRuntimeArn": self._arn, "status": "UPDATING"}}

    def get_agent_runtime(self, **kw):
        self.get_calls.append(kw)
        return {"agentRuntime": {"agentRuntimeId": kw["agentRuntimeId"],
                                 "agentRuntimeArn": self._arn, "status": "READY"}}


class _FakeEcr:
    def __init__(self, account=ACCOUNT, region="eu-north-1"):
        self.created, self.registry = [], f"{account}.dkr.ecr.{region}.amazonaws.com"

    def describe_repositories(self, **kw):
        raise RuntimeError("RepositoryNotFoundException")   # force the create branch

    def create_repository(self, repositoryName):
        self.created.append(repositoryName)
        return {"repository": {"repositoryUri": f"{self.registry}/{repositoryName}"}}

    def get_authorization_token(self, **kw):
        import base64
        tok = base64.b64encode(b"AWS:fake-ecr-password").decode()
        return {"authorizationData": [{"authorizationToken": tok}]}


class _Runner:
    def __init__(self):
        self.cmds, self.inputs = [], []

    def __call__(self, cmd, input_text=None):
        self.cmds.append(cmd)
        self.inputs.append(input_text)


def _open_gate(monkeypatch):
    """Force the gate True so the encoded hosted path runs (no committed receipt needed
    for the offline flow test)."""
    monkeypatch.setattr(bt, "runtime_hosted_deploy_allowed", lambda: True)


# --------------------------------------------------------------------------- #
# pure: create body + response extraction
# --------------------------------------------------------------------------- #
def test_runtime_create_body_shape(examples_dir, tmp_path):
    project, _ = _team(examples_dir, tmp_path)
    plan = build_bedrock_plan(project, region="eu-north-1")
    body = _runtime_create_body(plan, image_uri="123.dkr.ecr/x:abc", role_arn=ROLE,
                                env_vars={"AGENTLIFT_MCP_X": "v"}, client_token="ct-1")
    assert body["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == "123.dkr.ecr/x:abc"
    assert body["roleArn"] == ROLE
    assert body["networkConfiguration"]["networkMode"] == "PUBLIC"
    assert body["protocolConfiguration"]["serverProtocol"] == "HTTP"
    assert body["clientToken"] == "ct-1"
    assert body["environmentVariables"] == {"AGENTLIFT_MCP_X": "v"}
    # IAM-only invoke: no JWT/OIDC authorizer encoded
    assert "authorizerConfiguration" not in body and "authorizer" not in body


def test_create_body_omits_env_when_empty(examples_dir, tmp_path):
    project, _ = _team(examples_dir, tmp_path)
    plan = build_bedrock_plan(project, region="eu-north-1")
    body = _runtime_create_body(plan, image_uri="u", role_arn=ROLE, env_vars={}, client_token="t")
    assert "environmentVariables" not in body


def test_extract_runtime_tolerant():
    nested = {"agentRuntime": {"agentRuntimeId": "i", "agentRuntimeArn": "a", "status": "READY"}}
    assert _extract_runtime(nested) == ("i", "a", "READY")
    flat = {"agentRuntimeId": "i2", "agentRuntimeArn": "a2", "status": "CREATING"}
    assert _extract_runtime(flat) == ("i2", "a2", "CREATING")
    assert _extract_runtime("nope") == ("", "", "")


# --------------------------------------------------------------------------- #
# the gate: bare hosted deploy refuses until the flag flips
# --------------------------------------------------------------------------- #
def test_gate_refuses_when_closed(examples_dir, tmp_path, monkeypatch):
    # the shipped flag is now True (live-verified, 2026-06-05); force the gate closed to
    # prove the refusal MECHANISM still fires with no side effect.
    monkeypatch.setattr(bt, "runtime_hosted_deploy_allowed", lambda: False)
    project, root = _team(examples_dir, tmp_path)
    with pytest.raises(HostedDeployNotLiveVerified, match="committed live receipt"):
        deploy_bedrock(project, region="eu-north-1", build_only=False)
    assert not os.path.exists(os.path.join(root, ".agentlift-build"))
    assert not os.path.isfile(os.path.join(root, BEDROCK_LOCKFILE_NAME))


def test_missing_role_raises_once_gate_open(examples_dir, tmp_path, monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.delenv("AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN", raising=False)
    project, _ = _team(examples_dir, tmp_path)
    with pytest.raises(RuntimeExecutionRoleRequired, match="execution role"):
        deploy_bedrock(project, region="eu-north-1", build_only=False,
                       control_client=_FakeControl(), ecr_client=_FakeEcr(),
                       docker_runner=_Runner(), sleep=lambda *_: None)


# --------------------------------------------------------------------------- #
# end-to-end hosted flow (fakes): create -> ECR push -> CreateAgentRuntime -> poll -> lock
# --------------------------------------------------------------------------- #
def test_hosted_create_pushes_creates_and_locks(examples_dir, tmp_path, monkeypatch):
    _open_gate(monkeypatch)
    project, root = _team(examples_dir, tmp_path)
    ctl, ecr, runner = _FakeControl(), _FakeEcr(), _Runner()

    res = deploy_bedrock(
        project, region="eu-north-1", build_only=False, execution_role_arn=ROLE,
        control_client=ctl, ecr_client=ecr, docker_runner=runner,
        sleep=lambda *_: None, log=lambda *_: None,
    )

    assert res.action == "create"
    assert res.agent_runtime_arn.endswith("runtime/agentlift_research_lead-xyz789")
    assert res.deploy_model.startswith("eu.anthropic.")
    # ECR repo created, image logged-in + built/pushed (arm64)
    assert ecr.created and ecr.created[0].startswith("agentlift/")
    assert any(c[:3] == ["docker", "login", "--username"] for c in runner.cmds)
    assert "fake-ecr-password" in runner.inputs           # piped via stdin
    buildx = [c for c in runner.cmds if c[:2] == ["docker", "buildx"]]
    assert buildx and "linux/arm64" in buildx[0] and "--push" in buildx[0]
    # CreateAgentRuntime issued with the role + arm64 image uri
    assert ctl.create_calls
    body = ctl.create_calls[0]
    assert body["roleArn"] == ROLE
    assert ":" in body["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
    assert ctl.get_calls                                  # polled to READY

    # lock written with the REAL arn (anonymized by the receipt writer, not here)
    lock_path = os.path.join(root, BEDROCK_LOCKFILE_NAME)
    assert os.path.isfile(lock_path)
    saved = json.load(open(lock_path, encoding="utf-8"))
    assert saved["agent_runtime_id"] == "agentlift_research_lead-xyz789"
    assert saved["region"] == "eu-north-1"
    assert saved["spec_hash"] == res.spec_hash


def test_second_deploy_unchanged_spec_skips(examples_dir, tmp_path, monkeypatch):
    _open_gate(monkeypatch)
    project, root = _team(examples_dir, tmp_path)
    plan = build_bedrock_plan(project, region="eu-north-1")
    # pre-seed the lock as if a prior create landed this exact spec
    lock = BedrockLock.load(root)
    lock.record(agent_runtime_id="rt-1", agent_runtime_arn="arn:rt-1", region="eu-north-1",
                spec_hash=plan.spec_hash, display_name=plan.display_name, deploy_model="m")
    lock.save()

    ctl, ecr, runner = _FakeControl(), _FakeEcr(), _Runner()
    res = deploy_bedrock(
        project, region="eu-north-1", build_only=False, execution_role_arn=ROLE,
        control_client=ctl, ecr_client=ecr, docker_runner=runner,
        sleep=lambda *_: None, log=lambda *_: None,
    )
    assert res.action == "skip"
    assert res.agent_runtime_arn == "arn:rt-1"
    # skip means NO build, NO push, NO API call
    assert not ctl.create_calls and not ecr.created and not runner.cmds


def test_region_move_forces_create(examples_dir, tmp_path, monkeypatch):
    _open_gate(monkeypatch)
    project, root = _team(examples_dir, tmp_path)
    plan_eu = build_bedrock_plan(project, region="eu-north-1")
    lock = BedrockLock.load(root)
    lock.record(agent_runtime_id="rt-eu", agent_runtime_arn="arn:rt-eu", region="eu-north-1",
                spec_hash=plan_eu.spec_hash, display_name=plan_eu.display_name, deploy_model="m")
    lock.save()

    ctl, ecr, runner = _FakeControl(region="us-west-2"), _FakeEcr(region="us-west-2"), _Runner()
    res = deploy_bedrock(
        project, region="us-west-2", build_only=False, execution_role_arn=ROLE,
        control_client=ctl, ecr_client=ecr, docker_runner=runner,
        sleep=lambda *_: None, log=lambda *_: None,
    )
    assert res.action == "create"          # different region = new regional artifact
    assert ctl.create_calls


# --------------------------------------------------------------------------- #
# InvokeAgentRuntime wire shape
# --------------------------------------------------------------------------- #
class _FakeData:
    def __init__(self):
        self.calls = []

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        body = json.dumps({"output": "delegated to researcher; cited sources"}).encode("utf-8")
        return {"response": body}


def test_invoke_agent_runtime_wire():
    data = _FakeData()
    out = invoke_agent_runtime("arn:aws:.../runtime/x", "hello", region="eu-north-1",
                               data_client=data)
    assert out == {"output": "delegated to researcher; cited sources"}
    call = data.calls[0]
    assert call["agentRuntimeArn"] == "arn:aws:.../runtime/x"
    assert len(call["runtimeSessionId"]) >= 33            # AgentCore session-id floor
    assert json.loads(call["payload"].decode("utf-8")) == {"prompt": "hello"}
