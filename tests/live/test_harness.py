"""Gated pytest wrapper around the live AgentCore Harness verifier.

The thin pytest entry point for the full live verification in ``harness.py`` (a
standalone script, not collected by pytest). It deploys the single-agent
``harness-single`` fixture to a real managed AgentCore **Harness**, invokes it,
classifies what the runtime actually did, asserts the receipt, then tears it down.

Gated **twice** so it never runs by accident:
  * the ``live`` marker — excluded by CI's ``pytest -m "not live"``; and
  * an explicit ``AGENTLIFT_LIVE_HARNESS=1`` opt-in — so even ``pytest -m live``
    skips it unless you mean it (it is billable and stands up a real harness).

It also requires AWS creds (``boto3`` default chain — NOT the bearer token) and a
harness execution role in ``AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN``.

What it asserts vs. what the receipt records (4-state model): the test gates the
*deterministic* signals — nothing FAILs, and ``create`` + ``agent`` land EXERCISED
(the control-plane shape is proven and real inference ran). The MCP / browser cells
are model-tool-choice dependent, so they may be WIRED; the headline EXERCISED claims
live in the committed receipt under ``receipts/``.

Run it deliberately:
  AGENTLIFT_LIVE_HARNESS=1 pytest -m live tests/live/test_harness.py
"""
import importlib.util
import os

import pytest

pytestmark = pytest.mark.live

HERE = os.path.dirname(os.path.abspath(__file__))
_OPT_IN = os.environ.get("AGENTLIFT_LIVE_HARNESS") == "1"
_ROLE = os.environ.get("AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN")


def _load_harness():
    """Import harness.py by path (no ``test_`` prefix, and tests/live is not a package)."""
    path = os.path.join(HERE, "harness.py")
    spec = importlib.util.spec_from_file_location("live_harness", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assert_matrix(matrix):
    """Gate: nothing FAILed; the control-plane shape + real inference are EXERCISED."""
    dims = matrix["dimensions"]
    for name, cell in dims.items():
        assert cell["state"] != "FAIL", (name, cell)
    # deterministic on every run: the create reached READY, and invoking produced
    # real model output (tokens). sandbox/browser are tool-choice dependent -> may be
    # WIRED; remote_mcp is WIRED in the current harness preview (tools not surfaced).
    for name in ("create", "agent"):
        assert dims[name]["state"] == "PASS-EXERCISED", (name, dims[name])
    for name in ("sandbox", "remote_mcp", "web_fetch", "skills"):
        assert dims[name]["state"] in {"PASS-EXERCISED", "PASS-WIRED"}, (name, dims[name])


@pytest.mark.skipif(not _OPT_IN, reason="set AGENTLIFT_LIVE_HARNESS=1 to run the billable harness deploy")
@pytest.mark.skipif(not _ROLE, reason="AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN not set")
def test_bedrock_harness_exercised():
    h = _load_harness()
    h.deploy()
    try:
        receipt = h.invoke()
        _assert_matrix(receipt["matrix"])
    finally:
        h.teardown()
