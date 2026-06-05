"""Offline end-to-end smoke of the live *Runtime* verifier's orchestration
(``tests/live/runtime.py`` ``deploy`` -> ``invoke``), driven entirely against fakes
(fake boto3 control/ecr/sts/data clients + a fake docker runner) so it runs in CI with
no creds, no Docker, no network. The per-call wire shape is unit-tested in
``test_bedrock_hosted.py``; this guards the *wrapper* that runs at the live gate -- parse
-> plan -> build context -> (faked) ECR push -> CreateAgentRuntime -> state + sidecar
(account/ECR redacted, real-arn gitignored) -> InvokeAgentRuntime -> 4-state classify ->
receipt -- so the one billable run is one-shot, not a debug session.

It also pins the honest boundary: the team's delegation is PASS-EXERCISED (objective
top-level ``tool_calls``), while nested specialist skill/MCP calls are PASS-WIRED,
text-corroborated -- never silently upgraded to the harness stream's PASS-EXERCISED."""
import importlib.util
import json
import os
import shutil

import pytest

pytest.importorskip("boto3")

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNT = "424242424242"
REGION = "eu-north-1"
ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/agentlift_lead-xyz789012"
REGISTRY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"


def _load():
    path = os.path.join(HERE, "live", "runtime.py")
    spec = importlib.util.spec_from_file_location("live_runtime_orch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeControl:
    def __init__(self):
        self.create_calls, self.delete_calls = [], []

    def create_agent_runtime(self, **kw):
        self.create_calls.append(kw)
        # return READY directly so the poll loop never sleeps in the test
        return {"agentRuntime": {"agentRuntimeId": "agentlift_lead-xyz789012",
                                 "agentRuntimeArn": ARN, "status": "READY"}}

    def list_agent_runtimes(self, **kw):
        return {"agentRuntimes": [{"agentRuntimeId": "agentlift_lead-xyz789012",
                                   "status": "READY"}]}

    def delete_agent_runtime(self, **kw):
        self.delete_calls.append(kw)
        return {}


class _FakeEcr:
    def __init__(self):
        self.created = []

    def describe_repositories(self, **kw):
        raise RuntimeError("RepositoryNotFoundException")

    def create_repository(self, repositoryName):
        self.created.append(repositoryName)
        return {"repository": {"repositoryUri": f"{REGISTRY}/{repositoryName}"}}

    def get_authorization_token(self, **kw):
        import base64
        return {"authorizationData": [{"authorizationToken":
                                       base64.b64encode(b"AWS:pw").decode()}]}


class _FakeSTS:
    def get_caller_identity(self):
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/dev"}


class _FakeData:
    """InvokeAgentRuntime returns the container's app-defined JSON body: a coordinator
    answer that relays both specialists' marker tokens + the skill marker + DeepWiki-
    derived 'react' content, with the objective top-level delegation trace."""
    def invoke_agent_runtime(self, **kw):
        H = _MOD
        body = {
            "result": (f"Researcher (facebook/react sections: Hooks, Components) "
                       f"{H.RESEARCHER_TOKEN}\nBug: use a + b. {H.BUGFINDER_TOKEN}\n"
                       f"{H.LEAD_TOKEN}\n{H.SKILL_MARKER}"),
            "tool_calls": ["tool_bug_finder", "tool_researcher"],
        }
        return {"response": json.dumps(body).encode("utf-8")}


class _Runner:
    def __init__(self):
        self.cmds = []

    def __call__(self, cmd, input_text=None):
        self.cmds.append(cmd)


_MOD = None


@pytest.fixture
def rt_mod(tmp_path, monkeypatch):
    global _MOD
    _MOD = _load()
    H = _MOD
    from agentlift import bedrock_plan, bedrock_target

    # open the gate for this test only (monkeypatch auto-restores to False after)
    monkeypatch.setattr(bedrock_plan, "_RUNTIME_LIVE_VERIFIED", True)
    # fake docker so the (real) build context is materialized but nothing is built/pushed
    runner = _Runner()
    monkeypatch.setattr(bedrock_target, "_default_docker_runner", runner)

    # hermetic copy of the team fixture so the build dir lands in tmp, not the repo
    fixture_copy = str(tmp_path / "runtime-team")
    shutil.copytree(H.FIXTURES["team"], fixture_copy,
                    ignore=shutil.ignore_patterns(".agentlift-*.json", "*.bak",
                                                  ".agentlift-build"))
    monkeypatch.setitem(H.FIXTURES, "team", fixture_copy)
    monkeypatch.setattr(H, "RECEIPTS", str(tmp_path / "receipts"))
    monkeypatch.setenv("AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN",
                       f"arn:aws:iam::{ACCOUNT}:role/agentlift-runtime")
    monkeypatch.setenv("AGENTLIFT_BEDROCK_REGION", REGION)
    monkeypatch.setenv("AGENTLIFT_RUNTIME_MODEL", "amazon.nova-pro-v1:0")

    ctl, ecr, sts, data = _FakeControl(), _FakeEcr(), _FakeSTS(), _FakeData()

    def fake_client(service, **kw):
        return {"bedrock-agentcore-control": ctl, "ecr": ecr, "sts": sts,
                "bedrock-agentcore": data}[service]

    monkeypatch.setattr("boto3.client", fake_client)
    H._fakes = {"ctl": ctl, "ecr": ecr, "runner": runner}
    return H


def test_deploy_then_invoke_writes_redacted_receipt(rt_mod, tmp_path):
    H = rt_mod
    state = H.deploy("team")
    assert state["action"] == "create"
    assert H._fakes["ctl"].create_calls          # CreateAgentRuntime issued
    assert H._fakes["ecr"].created               # ECR repo created
    assert any(c[:2] == ["docker", "buildx"] for c in H._fakes["runner"].cmds)
    # state file is committed -> account + ECR registry must be redacted
    assert ACCOUNT not in json.dumps(state)
    assert "****" in state["agent_runtime_arn"]
    sidecar = json.load(open(os.path.join(H.RECEIPTS, "_secret-runtime-arn.json")))
    assert sidecar["agent_runtime_arn"] == ARN   # real arn, gitignored

    receipt = H.invoke()
    dims = receipt["matrix"]["dimensions"]
    assert dims["create"]["state"] == "PASS-EXERCISED"
    assert dims["agent"]["state"] == "PASS-EXERCISED"
    # delegation is objective (top-level tool_calls) -> PASS-EXERCISED
    assert dims["subagents"]["state"] == "PASS-EXERCISED"
    # nested skill/MCP do NOT cross the /invocations boundary -> PASS-WIRED (corroborated),
    # never silently upgraded to PASS-EXERCISED
    assert dims["skills"]["state"] == "PASS-WIRED"
    assert dims["skills"]["evidence"]["skill_marker"] is True
    assert dims["remote_mcp"]["state"] == "PASS-WIRED"
    assert dims["remote_mcp"]["evidence"]["text_corroborated"] is True
    # the committed receipt is fully redacted
    assert ACCOUNT not in json.dumps(receipt)

    rdirs = [d for d in os.listdir(H.RECEIPTS) if d.endswith("-runtime-bedrock")]
    assert rdirs
    body = open(os.path.join(H.RECEIPTS, rdirs[0], "receipt.json"), encoding="utf-8").read()
    assert ACCOUNT not in body and REGISTRY not in body


def test_teardown_requests_delete(rt_mod):
    H = rt_mod
    H.deploy("team")
    H.teardown()
    assert H._fakes["ctl"].delete_calls[0]["agentRuntimeId"] == "agentlift_lead-xyz789012"
