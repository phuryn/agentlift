"""Offline end-to-end smoke of the live harness verifier's orchestration
(``tests/live/harness.py`` ``deploy`` -> ``invoke``), driven entirely against a fake
boto3 so it runs in CI with no creds and no network. The per-call wire shape is unit-
tested in ``test_harness_target.py``; this guards the *wrapper* that actually runs at
the live gate -- parse -> plan -> CreateHarness -> state + sidecar files (account
redacted / real-ARN gitignored) -> InvokeHarness -> 4-state classify -> receipt --
so the one billable run is one-shot, not a debug session.

The fake clients return the authoritative botocore envelopes: control-plane nests the
resource under ``harness`` with an ``arn`` field; the data plane returns ``{"stream":
[events...]}`` shaped like the InvokeHarness model output."""
import importlib.util
import json
import os
import shutil

import pytest

# this test patches boto3.client to drive the deploy wrapper offline; skip cleanly if the
# AWS extra (`pip install agentlift[bedrock]`) isn't installed rather than erroring.
pytest.importorskip("boto3")

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNT = "424242424242"          # fake account id the receipt must redact
ARN = f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:harness/agentlift_assistant-abc1234567"


def _load():
    path = os.path.join(HERE, "live", "harness.py")
    spec = importlib.util.spec_from_file_location("live_harness_orch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeControl:
    def __init__(self):
        self.create_calls, self.delete_calls = [], []

    def _env(self, status="READY"):
        return {"harness": {"harnessId": "agentlift_assistant-abc1234567",
                            "harnessName": "agentlift_assistant", "arn": ARN,
                            "status": status, "executionRoleArn":
                            f"arn:aws:iam::{ACCOUNT}:role/agentlift-harness"}}

    def create_harness(self, **kw):
        self.create_calls.append(kw)
        return self._env("READY")

    def get_harness(self, **kw):
        return self._env("READY")

    def delete_harness(self, **kw):
        self.delete_calls.append(kw)
        return {}


class _FakeData:
    def invoke_harness(self, **kw):
        # a stream that calls the prefixed MCP tool (<server>_<tool>) + the skills tool,
        # then answers with the trace + house-style marker
        H = _MOD
        return {"stream": [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"contentBlockIndex": 0, "start": {
                "toolUse": {"name": "docs_read_wiki_structure", "toolUseId": "t1",
                            "type": "mcp_tool_use"}}}},
            {"contentBlockStart": {"contentBlockIndex": 1, "start": {
                "toolUse": {"name": "skills", "toolUseId": "t2"}}}},
            {"contentBlockDelta": {"contentBlockIndex": 2, "delta": {
                "text": f"Sections: A, B. {H.TRACE_TOKEN} {H.SKILL_MARKER}"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 50, "outputTokens": 30,
                                    "totalTokens": 80}, "metrics": {"latencyMs": 900}}},
        ]}


class _FakeS3:
    def put_object(self, **kw):
        pass


class _FakeSTS:
    def get_caller_identity(self):
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/dev"}


_MOD = None


@pytest.fixture
def harness_mod(tmp_path, monkeypatch):
    global _MOD
    _MOD = _load()
    H = _MOD
    # hermetic: deploy a COPY of the fixture in tmp (stripped of any lockfile) so the
    # test never mutates the real fixture and never sees a stale .agentlift-harness.json
    # from a prior run (which would turn create -> skip).
    fixture_copy = str(tmp_path / "harness-single")
    shutil.copytree(H.FIXTURE, fixture_copy,
                    ignore=shutil.ignore_patterns(".agentlift-*.json", "*.bak"))
    monkeypatch.setattr(H, "FIXTURE", fixture_copy)
    # redirect receipts to a temp dir so we never touch the real receipts/
    monkeypatch.setattr(H, "RECEIPTS", str(tmp_path / "receipts"))
    monkeypatch.setenv("AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN",
                       f"arn:aws:iam::{ACCOUNT}:role/agentlift-harness")
    monkeypatch.setenv("AGENTLIFT_BEDROCK_REGION", "us-west-2")
    monkeypatch.setenv("AGENTLIFT_BEDROCK_S3_BUCKET", "fake-bucket")   # fixture has a skill

    ctl, data, sts, s3 = _FakeControl(), _FakeData(), _FakeSTS(), _FakeS3()

    def fake_client(service, **kw):
        return {"bedrock-agentcore-control": ctl, "bedrock-agentcore": data,
                "sts": sts, "s3": s3}[service]

    monkeypatch.setattr("boto3.client", fake_client)
    H._fakes = {"ctl": ctl, "data": data}     # for assertions
    return H


def test_deploy_then_invoke_writes_redacted_receipt(harness_mod, tmp_path):
    H = harness_mod
    state = H.deploy()
    assert state["action"] == "create"
    assert H._fakes["ctl"].create_calls            # CreateHarness was issued
    # state file is committed -> account must be redacted; sidecar holds the real arn
    assert ACCOUNT not in json.dumps(state)
    assert "****" in state["harness_arn"]
    sidecar = json.load(open(os.path.join(H.RECEIPTS, "_secret-harness-arn.json")))
    assert sidecar["harness_arn"] == ARN          # real arn, gitignored

    receipt = H.invoke()
    dims = receipt["matrix"]["dimensions"]
    assert dims["create"]["state"] == "PASS-EXERCISED"
    assert dims["agent"]["state"] == "PASS-EXERCISED"
    assert dims["remote_mcp"]["state"] == "PASS-EXERCISED"   # docs_read_wiki_structure fired
    assert dims["skills"]["state"] == "PASS-EXERCISED"       # skills tool + house-style marker
    # the committed receipt is fully redacted
    assert ACCOUNT not in json.dumps(receipt)

    # a timestamped receipt dir with receipt.json + events.jsonl exists, all redacted
    rdirs = [d for d in os.listdir(H.RECEIPTS) if d.endswith("-harness-bedrock")]
    assert rdirs
    rdir = os.path.join(H.RECEIPTS, rdirs[0])
    body = open(os.path.join(rdir, "receipt.json"), encoding="utf-8").read()
    assert ACCOUNT not in body
    events = open(os.path.join(rdir, "events.jsonl"), encoding="utf-8").read()
    assert ACCOUNT not in events and H.TRACE_TOKEN in events


def test_teardown_calls_delete(harness_mod):
    H = harness_mod
    H.deploy()
    H.teardown()
    assert H._fakes["ctl"].delete_calls[0]["harnessId"] == "agentlift_assistant-abc1234567"
