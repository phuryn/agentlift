"""Offline guard for the live harness verifier's event-stream parsing + 4-state
classification (``tests/live/harness.py``). The verifier itself is billable and not
collected by pytest, but the logic that decides what counts as PASS-EXERCISED is a
*contract*: it must fire on the real ``InvokeHarness`` event-stream shape and stay
honest (no false EXERCISED). So we exercise it here against synthetic events shaped
like the botocore ``bedrock-agentcore`` model output (messageStart / contentBlockStart
with a toolUse / contentBlockDelta text / metadata.usage / messageStop). No network."""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _load():
    path = os.path.join(HERE, "live", "harness.py")
    spec = importlib.util.spec_from_file_location("live_harness_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _load()


def _events_with_mcp_call_and_text(usage_out=42):
    """A stream that calls a DeepWiki MCP tool (harness names it <server>_<tool>), then
    answers with the trace token."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {
            "toolUse": {"name": "docs_read_wiki_structure", "toolUseId": "t1",
                        "type": "mcp_tool_use", "serverName": "docs"}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {
            "text": f"Top sections: A, B. {H.TRACE_TOKEN}"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 100, "outputTokens": usage_out,
                                "totalTokens": 100 + usage_out},
                      "metrics": {"latencyMs": 1200}}},
    ]


def _ready_state():
    return {"action": "create", "status": "READY",
            "harness_arn": "arn:aws:bedrock-agentcore:us-west-2:****:harness/agentlift_assistant-abc1234567",
            "spec_hash": "734eb51e607d"}


# --------------------------------------------------------------------------- #
# stream extraction
# --------------------------------------------------------------------------- #
def test_stream_extractors_pull_tooluse_text_usage():
    evs = _events_with_mcp_call_and_text()
    tools = [t for ev in evs for t in H.stream_tool_uses(ev)]
    names = {t["name"] for t in tools}
    types = {t["type"] for t in tools}
    assert "docs_read_wiki_structure" in names      # harness prefixes MCP tools <server>_<tool>
    assert "mcp_tool_use" in types
    text = "".join(H.stream_text(ev) for ev in evs)
    assert H.TRACE_TOKEN in text
    usage = {}
    for ev in evs:
        u = H.stream_usage(ev)
        if u:
            usage = u
    assert usage["outputTokens"] == 42
    assert all(H.stream_error(ev) is None for ev in evs)


def test_stream_error_surfaces_validation_exception():
    ev = {"validationException": {"message": "bad field", "reason": "FieldValidationFailed"}}
    assert "bad field" in (H.stream_error(ev) or "")


# --------------------------------------------------------------------------- #
# classification -> 4-state matrix
# --------------------------------------------------------------------------- #
def test_classify_create_and_agent_and_mcp_exercised():
    run = {"label": "mcp", "error": None, "events": _events_with_mcp_call_and_text(),
           "tool_uses": [t for ev in _events_with_mcp_call_and_text()
                         for t in H.stream_tool_uses(ev)],
           "usage": {"outputTokens": 42}, "final_text": f"answer {H.TRACE_TOKEN}"}
    matrix = H.classify([run], _ready_state())
    dims = matrix["dimensions"]
    assert dims["create"]["state"] == "PASS-EXERCISED"
    assert dims["agent"]["state"] == "PASS-EXERCISED"
    assert dims["remote_mcp"]["state"] == "PASS-EXERCISED"   # docs_read_wiki_structure (prefixed)
    # browser + shell + skills not called -> WIRED, never a false EXERCISED
    assert dims["web_fetch"]["state"] == "PASS-WIRED"
    assert dims["sandbox"]["state"] == "PASS-WIRED"
    assert dims["skills"]["state"] == "PASS-WIRED"


def test_classify_skills_exercised_on_tool_and_marker():
    run = {"label": "agent", "error": None, "events": [], "tool_uses": [{"name": "skills", "type": ""}],
           "usage": {"outputTokens": 12}, "final_text": f"answer {H.TRACE_TOKEN} {H.SKILL_MARKER}"}
    matrix = H.classify([run], _ready_state())
    assert matrix["dimensions"]["skills"]["state"] == "PASS-EXERCISED"


def test_classify_skills_wired_when_not_consulted():
    run = {"label": "agent", "error": None, "events": [], "tool_uses": [],
           "usage": {"outputTokens": 12}, "final_text": f"answer {H.TRACE_TOKEN}"}
    matrix = H.classify([run], _ready_state())
    assert matrix["dimensions"]["skills"]["state"] == "PASS-WIRED"


def test_classify_sandbox_exercised_on_shell_tool():
    run = {"label": "sandbox", "error": None, "events": [], "tool_uses": [{"name": "shell", "type": ""}],
           "usage": {"outputTokens": 12}, "final_text": f"ran it {H.TRACE_TOKEN}"}
    matrix = H.classify([run], _ready_state())
    assert matrix["dimensions"]["sandbox"]["state"] == "PASS-EXERCISED"


def test_classify_sandbox_exercised_on_echo_nonce():
    run = {"label": "sandbox", "error": None, "events": [], "tool_uses": [],
           "usage": {"outputTokens": 12}, "final_text": f"stdout: {H.SANDBOX_NONCE} {H.TRACE_TOKEN}"}
    matrix = H.classify([run], _ready_state())
    assert matrix["dimensions"]["sandbox"]["state"] == "PASS-EXERCISED"


def test_classify_create_fails_when_not_ready():
    run = {"label": "mcp", "error": None, "events": [], "tool_uses": [],
           "usage": {}, "final_text": ""}
    bad = dict(_ready_state(), status="CREATE_FAILED")
    matrix = H.classify([run], bad)
    assert matrix["dimensions"]["create"]["state"] == "FAIL"
    # no text/usage -> agent FAIL too (honest: nothing ran)
    assert matrix["dimensions"]["agent"]["state"] == "FAIL"


def test_classify_web_fetch_exercised_on_nonce():
    run = {"label": "fetch", "error": None, "events": [], "tool_uses": [],
           "usage": {"outputTokens": 10},
           "final_text": f"the page said {H.CANARY_NONCE} {H.TRACE_TOKEN}"}
    matrix = H.classify([run], _ready_state())
    assert matrix["dimensions"]["web_fetch"]["state"] == "PASS-EXERCISED"


def test_classify_remote_mcp_wired_when_not_called():
    run = {"label": "mcp", "error": None, "events": [], "tool_uses": [],
           "usage": {"outputTokens": 10}, "final_text": f"answer {H.TRACE_TOKEN}"}
    matrix = H.classify([run], _ready_state())
    # MCP configured in the create body but not called -> WIRED, not EXERCISED
    assert matrix["dimensions"]["remote_mcp"]["state"] == "PASS-WIRED"


# --------------------------------------------------------------------------- #
# anonymization (committed receipts must not carry the real account id)
# --------------------------------------------------------------------------- #
def test_anon_redacts_account_in_arn():
    arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/agentlift_assistant-abc1234567"
    out = H._anon({"arn": arn, "role": "arn:aws:iam::123456789012:role/r"}, "123456789012")
    assert "123456789012" not in out["arn"]
    assert "****" in out["arn"]
    assert "123456789012" not in out["role"]


def test_anon_redacts_account_even_without_sts():
    # belt-and-suspenders regex path: redact the account in an arn even if account=""
    arn = "arn:aws:bedrock-agentcore:us-west-2:999988887777:harness/x-abc1234567"
    out = H._anon(arn, "")
    assert "999988887777" not in out
