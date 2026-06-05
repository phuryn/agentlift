"""The harness deploy step is idempotent and offline-testable: with a fake
``bedrock-agentcore-control`` client it creates on a fresh lock, updates when the
spec changed, skips when unchanged, folds MCP auth header values resolved from the
local env straight into the wire body (never to disk), polls until READY, prints a
PREVIEW banner (the shape is not live-verified yet), refuses a create with no
execution role, and records the harness id/ARN + spec hash to
``.agentlift-harness.json``. No network."""
import json
import os
import shutil

import pytest

from agentlift.harness_lock import HARNESS_LOCKFILE_NAME, HarnessLock
from agentlift.harness_plan import build_harness_plan
from agentlift.harness_target import (
    EXECUTION_ROLE_ENV,
    HarnessDeployFailed,
    HarnessExecutionRoleRequired,
    HarnessSkillBucketRequired,
    deploy_harness,
    delete_harness,
    invoke_harness,
)
from agentlift.parser import parse_project

ROLE = "arn:aws:iam::111122223333:role/agentlift-harness"
HID = "agentlift_api-abc123"
ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/agentlift_api-abc123"


# --------------------------------------------------------------------------- #
# fakes + helpers
# --------------------------------------------------------------------------- #
class _FakeControl:
    """A stand-in for the bedrock-agentcore-control client (records calls)."""

    def __init__(self, *, create_status="READY", get_statuses=None,
                 harness_id=HID, harness_arn=ARN):
        self.create_calls = []
        self.update_calls = []
        self.delete_calls = []
        self.get_calls = []
        self._create_status = create_status
        self._get_statuses = list(get_statuses or [])
        self.harness_id = harness_id
        self.harness_arn = harness_arn

    # The real control-plane returns the resource nested under "harness" with the
    # ARN field named "arn" (not "harnessArn") -- pinned here so the tolerant
    # extractor is tested against the authoritative envelope, not the first guess.
    def _envelope(self, status):
        return {"harness": {"harnessId": self.harness_id, "harnessName": "agentlift_api",
                            "arn": self.harness_arn, "status": status}}

    def create_harness(self, **kw):
        self.create_calls.append(kw)
        return self._envelope(self._create_status)

    def update_harness(self, **kw):
        self.update_calls.append(kw)
        return self._envelope("READY")

    def get_harness(self, **kw):
        self.get_calls.append(kw)
        status = self._get_statuses.pop(0) if self._get_statuses else "READY"
        return self._envelope(status)

    def delete_harness(self, **kw):
        self.delete_calls.append(kw)
        return {}


def _copy(src_dir, tmp_path, name):
    dst = os.path.join(str(tmp_path), name)
    shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns(
        ".agentlift-lock.json", ".agentlift-*.json", "*.bak"))
    return dst


def _deploy(project, control, logs=None, **over):
    kw = dict(
        region="us-west-2", execution_role_arn=ROLE,
        log=(logs.append if logs is not None else (lambda *_: None)),
        control_client=control, sleep=lambda *_: None, poll_delay=0,
    )
    kw.update(over)
    return deploy_harness(project, **kw)


def _mcp_auth(fixtures_dir, tmp_path):
    root = _copy(os.path.join(fixtures_dir, "mcp-auth"), tmp_path, "mcp-auth")
    project, _ = parse_project(root)
    return project, root


# --------------------------------------------------------------------------- #
# create / update / skip
# --------------------------------------------------------------------------- #
def test_create_on_fresh_lock(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    ctl = _FakeControl()
    res = _deploy(project, ctl)

    assert res.action == "create"
    assert res.harness_id == HID and res.harness_arn == ARN
    assert res.status == "READY"
    assert res.live_verified is True
    assert len(ctl.create_calls) == 1 and not ctl.update_calls
    body = ctl.create_calls[0]
    assert body["harnessName"] == "agentlift_api"
    assert body["executionRoleArn"] == ROLE
    assert body["model"]["bedrockModelConfig"]["modelId"] == \
        "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert body["systemPrompt"][0]["text"]
    assert body["clientToken"].startswith("agentlift-")
    tool = next(t for t in body["tools"] if t["type"] == "remote_mcp")
    assert tool["name"] == "secure"


def test_lock_written_after_create(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    plan = build_harness_plan(project, region="us-west-2")
    _deploy(project, _FakeControl())

    lockpath = os.path.join(root, HARNESS_LOCKFILE_NAME)
    assert os.path.isfile(lockpath)
    lock = HarnessLock.load(root)
    assert lock.harness_id == HID and lock.harness_arn == ARN
    assert lock.region == "us-west-2"
    assert lock.spec_hash == plan.spec_hash
    assert lock.display_name == "agentlift-api"
    assert lock.deploy_model == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_skip_when_unchanged(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    _deploy(project, _FakeControl())                 # records the lock
    ctl2 = _FakeControl()
    res = _deploy(project, ctl2)
    assert res.action == "skip"
    assert res.harness_id == HID
    assert not ctl2.create_calls and not ctl2.update_calls


def test_update_when_spec_changes(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    lock = HarnessLock.load(root)
    lock.record(harness_id=HID, harness_arn=ARN, region="us-west-2",
                spec_hash="STALE", display_name="agentlift-api",
                deploy_model="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    lock.save()

    ctl = _FakeControl()
    res = _deploy(project, ctl)
    assert res.action == "update"
    assert not ctl.create_calls and len(ctl.update_calls) == 1
    upd = ctl.update_calls[0]
    assert upd["harnessId"] == HID
    assert "harnessName" not in upd        # name is immutable; not sent on update
    assert "clientToken" not in upd
    assert upd["model"]["bedrockModelConfig"]["modelId"].startswith("us.anthropic.")


class _FakeControlDeletedToken(_FakeControl):
    """First create_harness raises the AWS 'clientToken since deleted' conflict; the
    retry (without clientToken) succeeds -- pins the delete-then-redeploy recovery."""

    def create_harness(self, **kw):
        if "clientToken" in kw and not getattr(self, "_retried", False):
            self._retried = True
            self.first_call = kw
            raise RuntimeError(
                "ConflictException: Resource previously created with clientToken "
                "agentlift-abc has since been deleted. Please retry without clientToken "
                "to create a new resource.")
        return super().create_harness(**kw)


def test_create_retries_without_client_token_when_token_deleted(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    ctl = _FakeControlDeletedToken()
    res = _deploy(project, ctl)
    assert res.action == "create" and res.status == "READY"
    assert len(ctl.create_calls) == 1                   # the successful retry
    assert "clientToken" in ctl.first_call              # first attempt carried the token
    assert "clientToken" not in ctl.create_calls[0]     # retry dropped it (per AWS guidance)


def test_region_change_forces_create(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    plan = build_harness_plan(project, region="us-west-2")
    lock = HarnessLock.load(root)
    lock.record(harness_id=HID, harness_arn=ARN, region="us-west-2",
                spec_hash=plan.spec_hash, display_name="agentlift-api",
                deploy_model="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    lock.save()
    ctl = _FakeControl()
    res = _deploy(project, ctl, region="eu-central-1")
    assert res.action == "create"
    assert len(ctl.create_calls) == 1


# --------------------------------------------------------------------------- #
# guards: execution role, provisional banner, polling, non-deployable
# --------------------------------------------------------------------------- #
def test_create_requires_execution_role(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    ctl = _FakeControl()
    with pytest.raises(HarnessExecutionRoleRequired, match=EXECUTION_ROLE_ENV):
        _deploy(project, ctl, execution_role_arn=None)
    assert not ctl.create_calls            # refused before any network call


def test_provisional_banner_suppressed_when_verified(fixtures_dir, tmp_path, monkeypatch):
    # Post-receipt (the default), the scary provisional-create banner is suppressed --
    # the wire shape is verified, so the deploy must not cry "provisional".
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    logs: list[str] = []
    _deploy(project, _FakeControl(), logs=logs)
    assert not any("provisional wire shape" in line.lower() for line in logs)


def test_provisional_banner_printed_when_unverified(fixtures_dir, tmp_path, monkeypatch):
    # ...and the mechanism is intact: force the flag False and the banner fires again.
    import agentlift.harness_plan as hp
    monkeypatch.setattr(hp, "_HARNESS_LIVE_VERIFIED", False)
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    logs: list[str] = []
    _deploy(project, _FakeControl(), logs=logs)
    assert any("PREVIEW" in line for line in logs)


def test_polls_until_ready(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    ctl = _FakeControl(create_status="CREATING", get_statuses=["CREATING", "READY"])
    res = _deploy(project, ctl)
    assert res.status == "READY"
    assert len(ctl.get_calls) == 2         # polled until READY


def test_poll_failed_state_raises(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "tok")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    ctl = _FakeControl(create_status="CREATING", get_statuses=["CREATE_FAILED"])
    with pytest.raises(HarnessDeployFailed, match="terminal state"):
        _deploy(project, ctl)


def test_not_deployable_raises(examples_dir, tmp_path):
    # team has subagents -> harness plan has errors -> deploy refuses
    root = _copy(os.path.join(examples_dir, "team"), tmp_path, "team")
    project, _ = parse_project(root)
    with pytest.raises(ValueError, match="not deployable"):
        _deploy(project, _FakeControl())


# --------------------------------------------------------------------------- #
# MCP auth -> wire headers, secret never on disk
# --------------------------------------------------------------------------- #
def test_auth_header_resolved_into_wire_not_disk(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "super-secret-value")
    project, root = _mcp_auth(fixtures_dir, tmp_path)
    ctl = _FakeControl()
    res = _deploy(project, ctl)

    assert res.env_var_names == ["AGENTLIFT_MCP_SECURE_AUTHORIZATION"]
    body = ctl.create_calls[0]
    tool = next(t for t in body["tools"] if t["type"] == "remote_mcp")
    # resolved secret rides the wire body only
    assert tool["config"]["remoteMcp"]["headers"]["Authorization"] == \
        "Bearer super-secret-value"
    # ...never the lockfile on disk
    blob = open(os.path.join(root, HARNESS_LOCKFILE_NAME), encoding="utf-8").read()
    assert "super-secret-value" not in blob
    assert "Bearer" not in blob
    data = json.loads(blob)
    assert data["harness_id"] == HID


# --------------------------------------------------------------------------- #
# invoke / delete helpers (data-plane + teardown)
# --------------------------------------------------------------------------- #
class _FakeData:
    def __init__(self):
        self.calls = []

    def invoke_harness(self, **kw):
        self.calls.append(kw)
        return {"stream": [{"messageStart": {"role": "assistant"}}]}


def test_invoke_harness_calls_data_plane():
    # InvokeHarness keys on the ARN (not id), needs a runtimeSessionId, and takes a
    # Converse-style messages array -- no `payload` field (reconciled vs the botocore
    # `bedrock-agentcore` model).
    data = _FakeData()
    out = invoke_harness(ARN, "hello", region="us-west-2", data_client=data)
    assert "stream" in out
    call = data.calls[0]
    assert call["harnessArn"] == ARN
    assert "harnessId" not in call and "payload" not in call
    # required, alnum-start, min length 33 (live-discovered param-validation constraint)
    assert call["runtimeSessionId"] and len(call["runtimeSessionId"]) >= 33
    assert call["messages"] == [{"role": "user", "content": [{"text": "hello"}]}]


def test_delete_harness_calls_control():
    ctl = _FakeControl()
    delete_harness(HID, region="us-west-2", control_client=ctl, log=lambda *_: None)
    assert ctl.delete_calls[0]["harnessId"] == HID


# --------------------------------------------------------------------------- #
# skills -> S3 upload + skills[].s3.uri (live-verified harness skill mechanism)
# --------------------------------------------------------------------------- #
class _FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append({"Bucket": kw["Bucket"], "Key": kw["Key"]})


def test_create_uploads_skills_to_s3_and_references_uri(examples_dir, tmp_path):
    # quickstart = one agent + a `receipt-stamp` skill -> harness uploads the bundle to
    # S3 and attaches it as skills[].s3.uri (no container, no skip-to-runtime).
    root = _copy(os.path.join(examples_dir, "quickstart"), tmp_path, "quickstart")
    project, _ = parse_project(root)
    ctl, s3 = _FakeControl(), _FakeS3()
    res = deploy_harness(project, region="us-west-2", execution_role_arn=ROLE,
                         skills_bucket="my-bucket", control_client=ctl, s3_client=s3,
                         sleep=lambda *_: None, poll_delay=0, log=lambda *_: None)
    assert res.action == "create"
    # the skill's file(s) were uploaded under agentlift-skills/<harness>/<skill>/, with
    # SKILL.md DIRECTLY under the prefix (not nested under a second <skill>/ — the harness
    # fetches SKILL.md at skills[].s3.uri).
    assert s3.puts and all(p["Bucket"] == "my-bucket" for p in s3.puts)
    keys = [p["Key"] for p in s3.puts]
    assert "agentlift-skills/agentlift_knowledge_agent/receipt-stamp/SKILL.md" in keys
    # the create body references the uploaded prefix
    body = ctl.create_calls[0]
    assert body["skills"] == [{"s3": {"uri":
        "s3://my-bucket/agentlift-skills/agentlift_knowledge_agent/receipt-stamp/"}}]


def test_create_with_skills_requires_bucket(examples_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTLIFT_BEDROCK_S3_BUCKET", raising=False)
    root = _copy(os.path.join(examples_dir, "quickstart"), tmp_path, "quickstart")
    project, _ = parse_project(root)
    ctl = _FakeControl()
    with pytest.raises(HarnessSkillBucketRequired, match="AGENTLIFT_BEDROCK_S3_BUCKET"):
        deploy_harness(project, region="us-west-2", execution_role_arn=ROLE,
                       control_client=ctl, sleep=lambda *_: None, poll_delay=0,
                       log=lambda *_: None)
    assert not ctl.create_calls            # refused before any create


def test_bucket_from_env_when_not_passed(examples_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLIFT_BEDROCK_S3_BUCKET", "env-bucket")
    root = _copy(os.path.join(examples_dir, "quickstart"), tmp_path, "quickstart")
    project, _ = parse_project(root)
    ctl, s3 = _FakeControl(), _FakeS3()
    deploy_harness(project, region="us-west-2", execution_role_arn=ROLE,
                   control_client=ctl, s3_client=s3, sleep=lambda *_: None,
                   poll_delay=0, log=lambda *_: None)
    assert all(p["Bucket"] == "env-bucket" for p in s3.puts)
