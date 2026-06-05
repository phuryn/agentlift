"""Pin the committed live AgentCore *Runtime* receipt as the source of truth for the
``_RUNTIME_LIVE_VERIFIED`` claim (CI-run, offline). The receipt(s) under
``tests/live/receipts/*-runtime-bedrock/`` are the objective evidence that agentlift's
CreateAgentRuntime/InvokeAgentRuntime wire shape ran live (ARM64 image -> ECR ->
CreateAgentRuntime -> InvokeAgentRuntime on Nova). This test asserts their shape, that the
flag agrees, that the headline (multi-agent DELEGATION) was PASS-EXERCISED, and -- a
committed-secrets guard -- that no real AWS account id leaked into the committed files."""
import glob
import json
import os
import re

from agentlift.bedrock_plan import _RUNTIME_LIVE_VERIFIED

HERE = os.path.dirname(os.path.abspath(__file__))
RECEIPTS = os.path.join(HERE, "live", "receipts")


def _runtime_receipts():
    return sorted(glob.glob(os.path.join(RECEIPTS, "*-runtime-bedrock")))


def _by_fixture():
    out = {}
    for rdir in _runtime_receipts():
        rc = json.load(open(os.path.join(rdir, "receipt.json"), encoding="utf-8"))
        out[rc.get("fixture", os.path.basename(rdir))] = (rdir, rc)
    return out


def test_a_committed_runtime_receipt_exists():
    assert _runtime_receipts(), "no committed AgentCore Runtime receipt under tests/live/receipts/"


def test_flag_agrees_with_committed_receipt():
    # _RUNTIME_LIVE_VERIFIED may only be True if a committed receipt proves the wire shape.
    if _RUNTIME_LIVE_VERIFIED:
        assert _runtime_receipts(), "_RUNTIME_LIVE_VERIFIED is True but no committed receipt backs it"


def test_team_receipt_proves_delegation():
    """The team receipt is the headline: a real multi-agent Runtime where the coordinator
    DELEGATED to specialists (objective top-level tool_calls). create + agent + subagents
    must be PASS-EXERCISED; nothing FAILed; skills/MCP are EXERCISED or honestly WIRED."""
    by = _by_fixture()
    rdir_rc = by.get("runtime-team")
    assert rdir_rc, f"no team receipt among {list(by)}"
    _, rc = rdir_rc
    dims = rc["matrix"]["dimensions"]
    for name, cell in dims.items():
        assert cell["state"] != "FAIL", (name, cell)
    for name in ("create", "agent", "subagents"):
        assert dims[name]["state"] == "PASS-EXERCISED", (name, dims[name])
    # delegation is objective: the coordinator's top-level trace named the specialists
    assert dims["subagents"]["evidence"]["delegation_tool_call"] is True
    tc = rc["matrix"]["tool_calls"]
    assert any("researcher" in t for t in tc) and any("bug" in t for t in tc), tc
    # nested specialist skill/MCP do not cross the /invocations boundary -> WIRED, not faked
    for name in ("skills", "remote_mcp"):
        assert dims[name]["state"] in {"PASS-EXERCISED", "PASS-WIRED"}, (name, dims[name])
    # Nova earned the model-agnostic wire-shape receipt (Claude is Gate-A-gated)
    assert "nova" in rc["state"]["deploy_model"].lower() or "claude" in rc["state"]["deploy_model"].lower()


def test_smoke_receipt_exercises_root_level_mcp_if_present():
    """The single-agent smoke validates the deployment shape AND that root-level tool calls
    are objectively captured -- when present, its remote_mcp is PASS-EXERCISED (an objective
    docs_read_wiki_structure call), a stronger MCP signal than the nested team path."""
    by = _by_fixture()
    rdir_rc = by.get("runtime-single")
    if not rdir_rc:
        return  # smoke receipt optional; the team receipt is the gating evidence
    _, rc = rdir_rc
    dims = rc["matrix"]["dimensions"]
    assert dims["create"]["state"] == "PASS-EXERCISED"
    assert dims["agent"]["state"] == "PASS-EXERCISED"
    assert dims["remote_mcp"]["state"] in {"PASS-EXERCISED", "PASS-WIRED"}


def test_committed_receipt_is_anonymized():
    """Committed evidence must never carry a real 12-digit AWS account id or ECR registry
    (the receipt writer redacts both to ****; only the gitignored sidecar holds the real ARN)."""
    acct = re.compile(r"\b\d{12}\b")
    for rdir in _runtime_receipts():
        for fn in ("receipt.json", "invocations.jsonl"):
            p = os.path.join(rdir, fn)
            if not os.path.isfile(p):
                continue
            body = open(p, encoding="utf-8").read()
            hits = set(acct.findall(body))
            assert not hits, f"{p} contains a 12-digit account id: {hits}"
            assert ".dkr.ecr" not in body or "****.dkr.ecr" in body, f"{p} has an un-redacted ECR registry"
