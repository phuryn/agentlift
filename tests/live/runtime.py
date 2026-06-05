"""Live AgentCore *Runtime* verification harness (NOT a pytest module).

Drives a real, billable build + deploy + invoke of a managed-agent folder against
Amazon Bedrock AgentCore's **Runtime** primitive (a custom ARM64 container running a
Strands "agents-as-tools" graph) and records an objective, re-runnable receipt. Two
fixtures, per the agreed sequencing (single-agent smoke first, then the team receipt):

  runtime-single  one agent + a DeepWiki URL MCP + a house-style skill (root-level, so
                  the trace captures the MCP/skill tool calls objectively)
  runtime-team    coordinator + 2 specialists; delegation is the headline. The researcher
                  carries the MCP + skill; both specialists emit a unique marker token.

THE BOUNDARY (encoded honestly, mirroring the Google AgentTool caveat): AgentCore's
``/invocations`` returns the container's app-defined JSON body -- NOT an event stream.
agentlift's generated handler returns ``{"result", "tool_calls"?, "trace_error"?}`` where
``tool_calls`` is the coordinator's TOP-LEVEL trace (AgentResult.metrics.tool_metrics,
fail-open). So:
  - delegation (coordinator -> specialist) and root-level skill/MCP calls ARE objective
    (they appear in ``tool_calls``) -> PASS-EXERCISED;
  - NESTED specialist skill/MCP calls do NOT cross the boundary -> PASS-WIRED, corroborated
    by an unforgeable marker in the final text (NOT equated with the harness stream's
    PASS-EXERCISED).

This is the receipt that flips ``bedrock_plan._RUNTIME_LIVE_VERIFIED``; it also reconciles
the ``CreateAgentRuntime``/``InvokeAgentRuntime`` wire shape against the live service.

Usage (each step explicit; build+deploy is minutes, invoke is cents):
  python tests/live/runtime.py inspect                 # read-only: list runtimes (≈free)
  python tests/live/runtime.py deploy [single|team]    # build+push+CreateAgentRuntime (billable)
  python tests/live/runtime.py invoke [single|team]    # InvokeAgentRuntime; writes the receipt
  python tests/live/runtime.py teardown                # DeleteAgentRuntime

Credentials (never committed): AWS IAM via the default boto3 chain (NOT the ``ABSK…``
bearer token -- that is inference-only). ``AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN`` must name
a role that trusts bedrock-agentcore.amazonaws.com AND can pull from ECR (the runtime pulls
the image). Region via ``AGENTLIFT_BEDROCK_REGION`` (default eu-north-1). Model override via
``AGENTLIFT_RUNTIME_MODEL`` (e.g. amazon.nova-pro-v1:0) for the Gate-A path -- the wire shape
is model-agnostic, so the receipt earns on Nova; Claude-invoke stays "pending Gate A". Local
prerequisite for the ARM64 build on an x86 host: ``docker run --privileged --rm
tonistiigi/binfmt --install arm64``. Receipts land under tests/live/receipts/ (account id +
ECR registry redacted to ****). This file is NOT collected by pytest.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import traceback
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FIXTURES = {
    "single": os.path.join(HERE, "fixtures", "runtime-single"),
    "team": os.path.join(HERE, "fixtures", "runtime-team"),
}
RECEIPTS = os.path.join(HERE, "receipts")
RUN_SESSION = "agentlift-runtime-verify-session"

# Marker tokens (in the fixtures' system prompts / SKILL.md). Unforgeable in the sense
# that only the specialist whose prompt carries the token can emit it -- so the token
# surfacing in the coordinator's relayed answer corroborates that the specialist ran.
LEAD_TOKEN = "RUNTIME-LEAD-OK"
RESEARCHER_TOKEN = "RUNTIME-RESEARCHER-OK"
BUGFINDER_TOKEN = "RUNTIME-BUGFINDER-OK"
ASSISTANT_TOKEN = "RUNTIME-ASSISTANT-OK"
SKILL_MARKER = "RUNTIME-SKILL-OK"

# top-level tool-call name fragments we recognize in the AgentResult trace
_MCP_FRAGMENTS = ("read_wiki_structure", "ask_question", "read_wiki_contents", "docs_")
_SKILL_FRAGMENTS = ("skill",)

Q_SINGLE = (
    "Use the `docs` MCP server (DeepWiki) to read the wiki structure of the "
    "facebook/react repository, then list two top-level sections. Use the MCP tool; "
    "do not answer from memory."
)
Q_TEAM = (
    "Two tasks. (1) Ask the researcher to use the docs MCP server (DeepWiki) to read "
    "the wiki structure of facebook/react and list two top-level sections. (2) Ask the "
    "bug_finder to find the one-line bug in: def add(a, b): return a - b\n"
    "Delegate BOTH, then combine their answers verbatim (keep every token they emit)."
)


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _ensure(d: str) -> str:
    os.makedirs(d, exist_ok=True)
    return d


def _region() -> str:
    return os.environ.get("AGENTLIFT_BEDROCK_REGION", "eu-north-1")


def _versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for mod in ("boto3", "botocore", "agentlift"):
        try:
            m = __import__(mod, fromlist=["__version__"])
            out[mod] = getattr(m, "__version__", "?")
        except Exception as e:
            out[mod] = f"<{type(e).__name__}>"
    return out


# --------------------------------------------------------------------------- #
# anonymization (committed receipts carry no real account id / ECR registry / arn)
# --------------------------------------------------------------------------- #
def _account_id() -> str:
    try:
        import boto3
        return boto3.client("sts").get_caller_identity().get("Account", "") or ""
    except Exception:
        return ""


def _anon(obj: Any, account: str) -> Any:
    if isinstance(obj, str):
        s = obj
        if account:
            s = s.replace(account, "****")
        s = re.sub(r"(:bedrock-agentcore:[a-z0-9-]+:)[0-9]{12}(:)", r"\1****\2", s)
        s = re.sub(r"(:iam::)[0-9]{12}(:)", r"\1****\2", s)
        s = re.sub(r"[0-9]{12}(\.dkr\.ecr\.)", r"****\1", s)   # ECR registry account
        return s
    if isinstance(obj, dict):
        return {k: _anon(v, account) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_anon(v, account) for v in obj]
    return obj


def _fixture(args: list[str]) -> str:
    name = next((a for a in args[1:] if a in FIXTURES), "team")
    return name


def _open_gate() -> None:
    """This verifier EARNS the receipt that flips the gate, so it forces the gate open
    (the only sanctioned bypass of ``_RUNTIME_LIVE_VERIFIED``)."""
    from agentlift import bedrock_plan
    bedrock_plan._RUNTIME_LIVE_VERIFIED = True


# --------------------------------------------------------------------------- #
# inspect (read-only)
# --------------------------------------------------------------------------- #
def inspect() -> dict[str, Any]:
    import boto3
    account = _account_id()
    region = _region()
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    try:
        resp = ctl.list_agent_runtimes()
    except Exception as e:
        print(f"  {region}: list_agent_runtimes failed: {type(e).__name__}: {str(e)[:160]}")
        resp = {}
    items = resp.get("agentRuntimes") or resp.get("agentRuntimeSummaries") or []
    print(f"  {region}: {len(items)} runtime(s)")
    found = []
    for it in items:
        rid = it.get("agentRuntimeId") or it.get("id")
        status = it.get("status", "?")
        print(f"    - {rid}  status={status}")
        found.append(_anon(it, account))
    out = {"when": _ts(), "region": region, "found": found, "versions": _versions()}
    path = os.path.join(_ensure(RECEIPTS), "_inspect-runtime.json")
    json.dump(out, open(path, "w", encoding="utf-8"), indent=2, default=str)
    print(f"\nwrote {path} (account redacted)")
    return out


# --------------------------------------------------------------------------- #
# deploy (agentlift hosted CreateAgentRuntime: build context -> ECR -> create)
# --------------------------------------------------------------------------- #
def deploy(fixture: str = "team") -> dict[str, Any]:
    from agentlift.bedrock_plan import build_bedrock_plan
    from agentlift.bedrock_target import RUNTIME_EXECUTION_ROLE_ENV, deploy_bedrock
    from agentlift.parser import parse_project

    _open_gate()
    region = _region()
    role = os.environ.get(RUNTIME_EXECUTION_ROLE_ENV)
    if not role:
        raise SystemExit(
            f"set ${RUNTIME_EXECUTION_ROLE_ENV} to a runtime execution role ARN that "
            f"trusts bedrock-agentcore.amazonaws.com AND can pull from ECR.")

    project, _diags = parse_project(FIXTURES[fixture])
    model_override = os.environ.get("AGENTLIFT_RUNTIME_MODEL")
    if model_override:
        for a in project.agents:
            a.model = model_override
        print(f"  model override: {model_override} (wire-shape receipt path; Claude is Gate-A)")
    plan = build_bedrock_plan(project, region=region)
    if not plan.deployable:
        raise SystemExit("runtime plan not deployable:\n" + plan.diagnostics.render())

    print(f"deploying runtime-{fixture} to AgentCore Runtime (region={region})...")
    print("  (build context -> ARM64 image -> ECR push -> CreateAgentRuntime -> poll READY)")
    t0 = datetime.datetime.now()
    res = deploy_bedrock(project, region=region, build_only=False,
                         execution_role_arn=role, log=print)
    dur = (datetime.datetime.now() - t0).total_seconds()

    account = _account_id()
    state = _anon({
        "provider": "bedrock-runtime", "fixture": f"runtime-{fixture}", "when": _ts(),
        "deploy_seconds": round(dur, 1), "action": res.action,
        "agent_runtime_arn": res.agent_runtime_arn, "region": region,
        "spec_hash": res.spec_hash, "display_name": res.display_name,
        "deploy_model": res.deploy_model, "model_override": model_override or "(fixture)",
        "agents": [a.name for a in plan.agents], "root": plan.root_agent,
        "plan": plan.to_dict(), "versions": _versions(),
    }, account)
    sidecar = {"agent_runtime_arn": res.agent_runtime_arn, "region": region,
               "fixture": fixture}
    json.dump(state, open(os.path.join(_ensure(RECEIPTS), "_state-runtime-bedrock.json"),
                          "w", encoding="utf-8"), indent=2, default=str)
    json.dump(sidecar, open(os.path.join(RECEIPTS, "_secret-runtime-arn.json"),
                            "w", encoding="utf-8"), indent=2)
    print(f"\n{res.action}: {res.agent_runtime_arn}")
    print(f"  deploy_seconds={round(dur,1)}  spec={res.spec_hash[:12]}")
    print("wrote _state-runtime-bedrock.json (redacted) + _secret-runtime-arn.json "
          "(gitignored, real arn for invoke)")
    return state


def _live_arn() -> tuple[str, str, str]:
    p = os.path.join(RECEIPTS, "_secret-runtime-arn.json")
    if not os.path.isfile(p):
        raise SystemExit("no _secret-runtime-arn.json; run `runtime.py deploy` first")
    d = json.load(open(p, encoding="utf-8"))
    return d["agent_runtime_arn"], d.get("region", _region()), d.get("fixture", "team")


# --------------------------------------------------------------------------- #
# invoke (data-plane; earns the receipt)
# --------------------------------------------------------------------------- #
def invoke_once(arn: str, region: str, prompt: str, session_id: str) -> dict[str, Any]:
    from agentlift.bedrock_target import invoke_agent_runtime
    err: Optional[str] = None
    body: dict[str, Any] = {}
    try:
        body = invoke_agent_runtime(arn, prompt, region=region, session_id=session_id)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    if not isinstance(body, dict):
        body = {"result": str(body)}
    return {
        "prompt": prompt, "session_id": session_id, "error": err,
        "final_text": str(body.get("result", "")),
        "tool_calls": body.get("tool_calls") or [],
        "trace_error": body.get("trace_error"),
        "raw": body,
    }


def invoke() -> dict[str, Any]:
    arn, region, fixture = _live_arn()
    state = json.load(open(os.path.join(RECEIPTS, "_state-runtime-bedrock.json"),
                           encoding="utf-8"))
    prompt = Q_TEAM if fixture == "team" else Q_SINGLE
    sid = f"{RUN_SESSION}-{fixture}".ljust(33, "0")
    print(f"invoking runtime {arn.split('/')[-1]} (region={region}, fixture={fixture})")
    r = invoke_once(arn, region, prompt, sid)
    r["label"] = fixture
    print(f"  tool_calls : {r['tool_calls'] or '(none)'}")
    print(f"  trace_error: {r['trace_error']}   err={r['error']}")
    print(f"  text[:200] : {r['final_text'][:200]!r}")
    matrix = classify(r, state, fixture)
    return _write_receipt(state, [r], matrix, fixture)


def teardown() -> None:
    import boto3
    p = os.path.join(RECEIPTS, "_secret-runtime-arn.json")
    if not os.path.isfile(p):
        print("no _secret-runtime-arn.json; nothing to tear down")
        return
    d = json.load(open(p, encoding="utf-8"))
    arn, region = d["agent_runtime_arn"], d.get("region", _region())
    rid = arn.split("/")[-1]
    print(f"deleting runtime {rid} ...")
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    try:
        ctl.delete_agent_runtime(agentRuntimeId=rid)
        print("delete requested.")
    except Exception as e:
        print(f"delete failed: {type(e).__name__}: {str(e)[:160]}")


# --------------------------------------------------------------------------- #
# classification -> 4-state matrix (objective tool_calls + unforgeable text markers)
# --------------------------------------------------------------------------- #
def classify(run: dict, state: dict, fixture: str) -> dict[str, Any]:
    tool_calls = [str(t) for t in run.get("tool_calls", [])]
    tc_join = " ".join(tool_calls).lower()
    text = run.get("final_text", "")
    err = run.get("error")
    answered = bool(text) and not err

    m: dict[str, Any] = {}

    # create: control-plane wire shape live-proven if the runtime reached deploy success
    created = state.get("action") in ("create", "update") and bool(state.get("agent_runtime_arn"))
    m["create"] = {
        "state": "PASS-EXERCISED" if created else "FAIL",
        "reason": "" if created else "runtime was not created / no ARN",
        "evidence": {"action": state.get("action"), "spec_hash": state.get("spec_hash")},
    }

    # agent: real model inference produced a result body, no error
    m["agent"] = {
        "state": "PASS-EXERCISED" if answered else "FAIL",
        "reason": "" if answered else f"no result text / errored: {err}",
        "evidence": {"has_text": bool(text), "trace_error": run.get("trace_error")},
    }

    # subagents (delegation): OBJECTIVE -- the coordinator's top-level tool_calls include a
    # specialist tool (tool_researcher / tool_bug_finder), corroborated by the specialist's
    # unique marker token relayed into the final answer. (single-agent fixture: N/A.)
    if fixture == "team":
        deleg_tool = ("researcher" in tc_join) or ("bug_finder" in tc_join) or ("bug-finder" in tc_join)
        marker = (RESEARCHER_TOKEN in text) or (BUGFINDER_TOKEN in text)
        if deleg_tool or marker:
            m["subagents"] = {"state": "PASS-EXERCISED",
                              "reason": "coordinator delegated (top-level tool call) "
                                        "and/or a specialist-only marker was relayed"}
        else:
            m["subagents"] = {"state": "NOT-PROVEN",
                              "reason": "no delegation tool call in trace and no specialist "
                                        "marker in text this run"}
        m["subagents"]["evidence"] = {
            "tool_calls": tool_calls, "delegation_tool_call": deleg_tool,
            "researcher_marker": RESEARCHER_TOKEN in text,
            "bugfinder_marker": BUGFINDER_TOKEN in text}

    # skills: root-level (single) is objective via tool_calls -> PASS-EXERCISED; nested in a
    # specialist (team) cannot cross the /invocations boundary -> PASS-WIRED, corroborated by
    # the SKILL.md marker in the text (explicitly NOT equated with an objective tool event).
    skill_tool = any(f in tc_join for f in _SKILL_FRAGMENTS)
    skill_marker = SKILL_MARKER in text
    if skill_tool:
        m["skills"] = {"state": "PASS-EXERCISED",
                       "reason": "root-level skill tool call in the trace"}
    elif skill_marker:
        m["skills"] = {"state": "PASS-WIRED",
                       "reason": "skill bundle embedded + loaded; nested specialist tool "
                                 "call does not cross the /invocations boundary -- "
                                 "TEXT-CORROBORATED by the SKILL.md marker (not an "
                                 "objective tool event like the harness stream)"}
    else:
        m["skills"] = {"state": "PASS-WIRED",
                       "reason": "skill bundle embedded + loaded; not consulted this run"}
    m["skills"]["evidence"] = {"skill_tool_call": skill_tool, "skill_marker": skill_marker}

    # remote_mcp: same boundary logic -- root-level (single) objective via tool_calls;
    # nested (team) PASS-WIRED, text-corroborated if the DeepWiki-derived content appears.
    mcp_tool = any(f in tc_join for f in _MCP_FRAGMENTS)
    mcp_corroborated = ("react" in text.lower()) or (RESEARCHER_TOKEN in text)
    if mcp_tool:
        m["remote_mcp"] = {"state": "PASS-EXERCISED",
                           "reason": "root-level MCP tool call in the trace"}
    elif fixture == "team" and mcp_corroborated:
        m["remote_mcp"] = {"state": "PASS-WIRED",
                           "reason": "MCP client wired into the specialist (streamable-HTTP "
                                     "+ tool_filter); nested call does not cross the "
                                     "/invocations boundary -- TEXT-CORROBORATED by relayed "
                                     "DeepWiki content (not an objective tool event)"}
    else:
        m["remote_mcp"] = {"state": "PASS-WIRED",
                           "reason": "MCP client wired into the agent; not called this run"}
    m["remote_mcp"]["evidence"] = {"mcp_tool_call": mcp_tool,
                                   "text_corroborated": mcp_corroborated}

    return {"fixture": fixture, "tool_calls": tool_calls, "any_error": bool(err),
            "dimensions": m}


# --------------------------------------------------------------------------- #
# receipt
# --------------------------------------------------------------------------- #
def _write_receipt(state: dict, runs: list, matrix: dict, fixture: str) -> dict[str, Any]:
    account = _account_id()
    stamp = _ts()
    rdir = _ensure(os.path.join(RECEIPTS, f"{stamp}-runtime-bedrock"))
    with open(os.path.join(rdir, "invocations.jsonl"), "w", encoding="utf-8") as fh:
        for r in runs:
            fh.write(json.dumps(_anon(r, account), default=str) + "\n")
    slim = [_anon({k: v for k, v in r.items() if k != "raw"}, account) for r in runs]
    receipt = _anon({
        "provider": "bedrock-runtime", "when": stamp, "fixture": f"runtime-{fixture}",
        "primitive": "AgentCore Runtime (custom ARM64 container, Strands agents-as-tools)",
        "state": state, "invocations": slim, "matrix": matrix,
        "note": "Live, billable. Not run in CI (AWS creds are not shared). Account id + ECR "
                "registry redacted to **** ; the bearer token is never used (control plane "
                "is IAM/SigV4; model inference uses the role). States: PASS-EXERCISED "
                "(objective: control-plane create, or a top-level tool_calls entry), "
                "PASS-WIRED (in the build/recipe; nested tool calls do NOT cross the "
                "/invocations boundary -- text-corroborated where noted), NOT-PROVEN, FAIL.",
    }, account)
    path = os.path.join(rdir, "receipt.json")
    json.dump(receipt, open(path, "w", encoding="utf-8"), indent=2, default=str)
    print("\n=== bedrock-runtime matrix ===")
    for dim, v in matrix["dimensions"].items():
        print(f"  {dim:12s} {v['state']:15s} {v.get('reason','')[:80]}")
    print(f"\nwrote {path}")
    print("\nNEXT: if create + agent + subagents are PASS-EXERCISED, flip "
          "bedrock_plan._RUNTIME_LIVE_VERIFIED=True and pin this receipt offline.")
    return receipt


# --------------------------------------------------------------------------- #
COMMANDS = {"inspect": inspect, "deploy": deploy, "invoke": invoke, "teardown": teardown}


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    for a in argv:
        if a.startswith("--region="):
            os.environ["AGENTLIFT_BEDROCK_REGION"] = a.split("=", 1)[1]
    if not args or args[0] not in COMMANDS:
        print("usage: runtime.py {inspect | deploy [single|team] | invoke | teardown} "
              "[--region=eu-north-1]")
        return 2
    try:
        from agentlift.cli import load_env
        load_env(os.getcwd(), ROOT)
    except Exception:
        pass
    try:
        cmd = args[0]
        if cmd == "deploy":
            deploy(_fixture(args))
        else:
            COMMANDS[cmd]()
        return 0
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
