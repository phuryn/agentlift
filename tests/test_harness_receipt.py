"""Pin the committed live Harness receipt as the source of truth for the
``_HARNESS_LIVE_VERIFIED`` claim (CI-run, offline). The receipt under
``tests/live/receipts/*-harness-bedrock/`` is the objective evidence that agentlift's
CreateHarness/InvokeHarness wire shape ran live; this test asserts its shape, that the
flag agrees with it, and -- a committed-secrets guard -- that no real AWS account id
leaked into the committed receipt/events."""
import glob
import json
import os
import re

from agentlift.harness_plan import _HARNESS_LIVE_VERIFIED

HERE = os.path.dirname(os.path.abspath(__file__))
RECEIPTS = os.path.join(HERE, "live", "receipts")


def _harness_receipts():
    return sorted(glob.glob(os.path.join(RECEIPTS, "*-harness-bedrock")))


def test_a_committed_harness_receipt_exists():
    assert _harness_receipts(), "no committed AgentCore Harness receipt under tests/live/receipts/"


def test_flag_agrees_with_committed_receipt():
    # _HARNESS_LIVE_VERIFIED may only be True if a committed receipt proves the wire shape.
    rcpts = _harness_receipts()
    if _HARNESS_LIVE_VERIFIED:
        assert rcpts, "_HARNESS_LIVE_VERIFIED is True but no committed receipt backs it"


def test_committed_receipt_matrix_is_honest():
    """The canonical receipt proves the full single-agent harness deploy: create + agent +
    base-session sandbox + remote MCP + S3-loaded skill + agentcore_browser. Nothing FAILed.
    (Tool cells are model-tool-choice dependent run-to-run, so the strict 6/6 claim lives in
    the committed receipt; the test gates the deterministic create+agent and that the receipt
    is a genuine 6-cell single-agent matrix with no FAIL.)"""
    rdir = _harness_receipts()[-1]
    rc = json.load(open(os.path.join(rdir, "receipt.json"), encoding="utf-8"))
    dims = rc["matrix"]["dimensions"]
    for name, cell in dims.items():
        assert cell["state"] != "FAIL", (name, cell)
    for name in ("create", "agent"):
        assert dims[name]["state"] == "PASS-EXERCISED", (name, dims[name])
    # the full single-agent cell set is present, each EXERCISED or honestly WIRED
    for name in ("sandbox", "remote_mcp", "skills", "web_fetch"):
        assert dims[name]["state"] in {"PASS-EXERCISED", "PASS-WIRED"}, (name, dims[name])
    # at least three tool cells actually fired server-side on this committed receipt (6/6)
    exercised = sum(dims[c]["state"] == "PASS-EXERCISED"
                    for c in ("sandbox", "remote_mcp", "skills", "web_fetch"))
    assert exercised >= 3, {c: dims[c]["state"] for c in ("sandbox", "remote_mcp", "skills", "web_fetch")}
    # the model that earned the receipt is recorded; status READY
    assert "nova" in rc["state"]["deploy_model"].lower() or "claude" in rc["state"]["deploy_model"].lower()
    assert rc["state"]["status"] == "READY"


def test_committed_receipt_is_anonymized():
    """Committed evidence must never carry a real 12-digit AWS account id (the receipt
    writer redacts it to ****; only the gitignored sidecar holds the real ARN)."""
    acct = re.compile(r"\b\d{12}\b")
    for rdir in _harness_receipts():
        for fn in ("receipt.json", "events.jsonl"):
            p = os.path.join(rdir, fn)
            if not os.path.isfile(p):
                continue
            body = open(p, encoding="utf-8").read()
            hits = [m for m in acct.findall(body)]
            assert not hits, f"{p} contains a 12-digit account id: {set(hits)}"
            assert "****" in body or fn == "events.jsonl", f"{p} missing redaction marker"
