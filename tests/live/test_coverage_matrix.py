"""Gated pytest wrapper around the live coverage-matrix harness.

This is the *thin* pytest entry point for the full live verification in
``coverage_matrix.py`` (which is a standalone script, not collected by pytest).
It deploys the six-dimension fixture to a real managed runtime, queries the live
engine, classifies what the runtime actually did, and asserts the receipt — then
tears the deployment down.

It is gated **twice** so it never runs by accident:
  * the ``live`` marker — excluded by CI's ``pytest -m "not live"``; and
  * an explicit ``AGENTLIFT_LIVE_COVERAGE=1`` opt-in — so even ``pytest -m live``
    skips it unless you mean it (it is slow + billable, and deploys a full
    multi-agent team, not a single agent).

What it asserts vs. what the receipt records (per the 4-state model): the test
gates the *deterministic* signal — nothing FAILs, and the dimensions that fire on
every run land EXERCISED. The strict, headline "6/6 EXERCISED" claim lives in the
committed receipts under ``receipts/`` (model tool-choice is nondeterministic, so a
hard 6/6 assertion would be flaky; the shared-MCP cell is allowed to be WIRED).

Run it deliberately:
  AGENTLIFT_LIVE_COVERAGE=1 pytest -m live tests/live/test_coverage_matrix.py
"""
import importlib.util
import os

import pytest

pytestmark = pytest.mark.live

HERE = os.path.dirname(os.path.abspath(__file__))
_OPT_IN = os.environ.get("AGENTLIFT_LIVE_COVERAGE") == "1"


def _load_harness():
    """Import coverage_matrix.py by path (it has no ``test_`` prefix, so pytest
    does not collect it, and ``tests/live`` is not a package)."""
    path = os.path.join(HERE, "coverage_matrix.py")
    spec = importlib.util.spec_from_file_location("coverage_matrix", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assert_matrix(matrix, *, allow_wired_shared_mcp=True):
    """Common gate: nothing FAILed, the deterministic dimensions are EXERCISED."""
    dims = matrix["dimensions"]
    assert matrix["any_error"] is False, matrix
    for name, cell in dims.items():
        assert cell["state"] != "FAIL", (name, cell)
    # deterministic on every run (delegation event, the agent's own private MCP
    # server, and the skill markers always fire for this fixture)
    for name in ("agents", "subagents", "individual_mcp", "shared_skill", "individual_skill"):
        assert dims[name]["state"] == "PASS-EXERCISED", (name, dims[name])
    # shared MCP is model-tool-choice dependent; the explicit prompt drives it to
    # EXERCISED, but accept WIRED so the gate is not flaky.
    allowed = {"PASS-EXERCISED", "PASS-WIRED"} if allow_wired_shared_mcp else {"PASS-EXERCISED"}
    assert dims["shared_mcp"]["state"] in allowed, dims["shared_mcp"]


@pytest.mark.skipif(not _OPT_IN, reason="set AGENTLIFT_LIVE_COVERAGE=1 to run the billable coverage matrix")
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
def test_anthropic_coverage_matrix_exercised():
    h = _load_harness()
    h.deploy_anthropic()
    try:
        receipt = h.query_anthropic()
        _assert_matrix(receipt["matrix"])
    finally:
        h.teardown_anthropic()


@pytest.mark.skipif(not _OPT_IN, reason="set AGENTLIFT_LIVE_COVERAGE=1 to run the billable coverage matrix")
@pytest.mark.skipif(
    not (os.environ.get("GOOGLE_CLOUD_PROJECT") and os.environ.get("AGENTLIFT_GCP_STAGING_BUCKET")),
    reason="Google ADC / GOOGLE_CLOUD_PROJECT / AGENTLIFT_GCP_STAGING_BUCKET not set",
)
def test_google_coverage_matrix_exercised():
    h = _load_harness()
    h.deploy_google()
    try:
        receipt = h.query_google()
        _assert_matrix(receipt["matrix"])
    finally:
        h.teardown_google()
