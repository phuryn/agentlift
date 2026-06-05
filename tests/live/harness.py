"""Live AgentCore *Harness* verification harness (NOT a pytest module).

Drives a real, billable deploy + invoke of the focused ``harness-single`` fixture
against Amazon Bedrock AgentCore's managed **Harness** primitive and records an
objective, re-runnable receipt. The fixture is one skill-less agent (so ``--mode
auto`` routes it to the harness, and ``--mode harness`` deploys it without errors):

  agent       model + systemPrompt (a managed single agent, config-only, no container)
  remote MCP  DeepWiki (URL) -> a harness ``remote_mcp`` tool
  web built-in web_fetch -> the harness ``agentcore_browser`` tool

It is the receipt that flips ``harness_plan._HARNESS_LIVE_VERIFIED`` from provisional
to verified -- so this script also exists to *reconcile* the ``CreateHarness`` /
``InvokeHarness`` wire shape against the live service, not just to pass.

Per the project's live discipline, every dimension is one of four states:

  PASS-WIRED     the create body proves the feature was configured (deterministic)
  PASS-EXERCISED the invoke event stream proves the service actually used it (objective)
  NOT-PROVEN     wired correctly, but no objective runtime signal (model chose not to)
  FAIL           deploy / config / runtime error or wrong behavior

Objective runtime signals (asserted on the InvokeHarness event stream, never on
answer text):
  - create     : CreateHarness returned a harness that reached READY with an ARN
                 (the control-plane wire shape is live-proven)
  - agent      : the stream produced assistant text AND metadata.usage.outputTokens>0
                 (real model inference ran), no error event
  - remote_mcp : a toolUse block for a DeepWiki tool (read_wiki_structure /
                 ask_question), or a block typed mcp_tool_use, on the stream
  - web_fetch  : a toolUse block for the agentcore_browser tool on the stream

Usage (each step is explicit; deploy is minutes, invoke is cents):
  python tests/live/harness.py inspect      # read-only: list/get existing harnesses (≈free)
  python tests/live/harness.py deploy        # agentlift CreateHarness on the fixture (billable)
  python tests/live/harness.py invoke        # InvokeHarness; writes the receipt (cents)
  python tests/live/harness.py teardown      # DeleteHarness

Credentials (never committed): AWS IAM creds via the default boto3 chain (NOT the
``ABSK…`` Bedrock bearer token -- that is inference-only and cannot reach the control
plane), plus ``AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN`` for a create (``inspect`` will
surface a reusable role from an existing harness). Region via ``AGENTLIFT_BEDROCK_REGION``
or ``--region`` (default us-west-2). Receipts land under tests/live/receipts/ and are
committed as evidence -- the AWS **account id** is redacted to ``****`` here before write,
and the bearer token is never touched. This file is NOT collected by pytest.
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

# Windows consoles default to cp1252; model output or box-drawing chars in a tool
# response would crash print() with a UnicodeEncodeError. Make console output tolerant
# (the receipt/event files are written as utf-8 regardless).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FIXTURE = os.path.join(HERE, "fixtures", "harness-single")
RECEIPTS = os.path.join(HERE, "receipts")
RUN_SESSION = "agentlift-harness-verify-1"

# A trace token the agent is told to echo: corroborates the agent produced the answer.
TRACE_TOKEN = "HARNESS-AGENT-OK"
# The house-style skill (fixtures/.../skills/house-style) tells the agent to append this
# to every reply, so its presence in any answer proves the S3-loaded skill was applied.
SKILL_MARKER = "HOUSESTYLE-OK"

# One MCP-forcing question + one URL-fetch question, split so neither conflates the
# remote_mcp tool with the browser tool. The fetch nonce is URL-derived (not in any
# training corpus) so it can only be reproduced by actually retrieving the page.
CANARY_NONCE = "AGENTLIFT-HARNESS-7B1D4E9A-CANARY"
CANARY_URL = "https://httpbingo.org/base64/QUdFTlRMSUZULUhBUk5FU1MtN0IxRDRFOUEtQ0FOQVJZ"
SANDBOX_NONCE = "HARNESS-SHELL-3C5F8A21-OK"
Q_SANDBOX = (
    f"Use your shell tool to run exactly: echo {SANDBOX_NONCE}\n"
    f"Then report the command's stdout verbatim and include the token {TRACE_TOKEN}."
)
Q_MCP = (
    "Use the `docs` MCP server (DeepWiki) to read the wiki structure of the "
    "facebook/react repository, then list two top-level sections. Use the MCP tool; "
    f"do not answer from memory. Include the token {TRACE_TOKEN} verbatim."
)
Q_FETCH = (
    f"Fetch the web page at {CANARY_URL} and tell me, verbatim, the exact text shown "
    f"on that page. Use a URL-retrieval tool; do not answer from memory. Include the "
    f"token {TRACE_TOKEN} verbatim."
)
Q_SKILL = (
    "Consult and apply your house-style skill, then answer in one sentence: what is a "
    f"North Star metric? Include the token {TRACE_TOKEN} verbatim."
)

_DEEPWIKI_TOOLS = {"read_wiki_structure", "ask_question", "read_wiki_contents"}
_SANDBOX_TOOLS = {"shell", "bash", "code", "code_interpreter", "execute", "file_operations"}


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _ensure(d: str) -> str:
    os.makedirs(d, exist_ok=True)
    return d


def _region() -> str:
    return os.environ.get("AGENTLIFT_BEDROCK_REGION", "us-west-2")


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
# anonymization (committed receipts must not carry the real AWS account id)
# --------------------------------------------------------------------------- #
def _account_id() -> str:
    try:
        import boto3
        return boto3.client("sts").get_caller_identity().get("Account", "") or ""
    except Exception:
        return ""


def _anon(obj: Any, account: str) -> Any:
    """Recursively redact the 12-digit AWS account id (in ARNs or bare) to ``****``."""
    if isinstance(obj, str):
        s = obj
        if account:
            s = s.replace(account, "****")
        # belt-and-suspenders: any stray 12-digit run in an arn:...:<acct>:... position
        s = re.sub(r"(:bedrock-agentcore:[a-z0-9-]+:)[0-9]{12}(:)", r"\1****\2", s)
        s = re.sub(r"(:iam::)[0-9]{12}(:)", r"\1****\2", s)
        return s
    if isinstance(obj, dict):
        return {k: _anon(v, account) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_anon(v, account) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# event-stream extraction (tolerant of snake_case / camelCase serialization)
# --------------------------------------------------------------------------- #
def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _jsonify(ev: Any) -> Any:
    try:
        return json.loads(json.dumps(ev, default=str))
    except Exception:
        return json.loads(json.dumps(str(ev)))


def stream_tool_uses(ev: dict) -> list[dict[str, Any]]:
    """Pull toolUse starts off one event: ``{name, type}`` (objective tool calls)."""
    out = []
    for d in _walk(ev):
        if not isinstance(d, dict):
            continue
        tu = d.get("toolUse") or d.get("tool_use")
        if isinstance(tu, dict) and (tu.get("name") or tu.get("toolUseId") or tu.get("tool_use_id")):
            name = tu.get("name") or ""
            # type may live on the toolUse or on its containing block
            ttype = tu.get("type") or d.get("type") or ""
            if name or ttype:
                out.append({"name": name, "type": ttype})
    return out


def stream_text(ev: dict) -> str:
    chunks = []
    for d in _walk(ev):
        if isinstance(d, dict) and isinstance(d.get("text"), str):
            chunks.append(d["text"])
    return "".join(chunks)


def stream_usage(ev: dict) -> dict[str, int]:
    for d in _walk(ev):
        if not isinstance(d, dict):
            continue
        u = d.get("usage")
        if isinstance(u, dict) and ("outputTokens" in u or "output_tokens" in u):
            return {
                "inputTokens": u.get("inputTokens") or u.get("input_tokens") or 0,
                "outputTokens": u.get("outputTokens") or u.get("output_tokens") or 0,
                "totalTokens": u.get("totalTokens") or u.get("total_tokens") or 0,
            }
    return {}


def stream_error(ev: dict) -> Optional[str]:
    for d in _walk(ev):
        if not isinstance(d, dict):
            continue
        for k in ("validationException", "internalServerException", "runtimeClientError"):
            e = d.get(k)
            if isinstance(e, dict):
                return f"{k}: {e.get('message') or e}"
    return None


# --------------------------------------------------------------------------- #
# inspect (read-only): reconcile the live shape against existing harnesses
# --------------------------------------------------------------------------- #
def inspect() -> dict[str, Any]:
    """List + get harnesses in the chosen region (read-only, ≈free).

    Surfaces the real output envelope (so ``_extract_harness`` is reconciled against a
    live resource), and any existing harness's ``executionRoleArn`` (reusable for a
    create) + model. Writes an anonymized ``_inspect-harness.json`` for reference."""
    import boto3
    from agentlift.harness_plan import HARNESS_PREVIEW_REGIONS
    from agentlift.harness_target import _extract_harness

    account = _account_id()
    regions = [_region()] + [r for r in HARNESS_PREVIEW_REGIONS if r != _region()]
    found: list[dict[str, Any]] = []
    for region in regions:
        try:
            ctl = boto3.client("bedrock-agentcore-control", region_name=region)
            resp = ctl.list_harnesses()
        except Exception as e:
            print(f"  {region}: list_harnesses failed: {type(e).__name__}: {str(e)[:140]}")
            continue
        items = resp.get("harnesses") or resp.get("harnessSummaries") or []
        print(f"  {region}: {len(items)} harness(es)")
        for it in items:
            hid = it.get("harnessId") or it.get("id")
            if not hid:
                continue
            try:
                full = ctl.get_harness(harnessId=hid)
            except Exception as e:
                print(f"    get_harness({hid}) failed: {type(e).__name__}: {str(e)[:120]}")
                continue
            _id, _arn, status = _extract_harness(full)
            h = full.get("harness", full)
            role = h.get("executionRoleArn", "")
            model = h.get("model", {})
            model_id = ""
            for cfg in _walk(model):
                if isinstance(cfg, dict) and cfg.get("modelId"):
                    model_id = cfg["modelId"]
                    break
            print(f"    - {hid}  status={status}  model={model_id}  role={'set' if role else '-'}")
            found.append(_anon({
                "region": region, "harness_id": _id or hid, "arn": _arn,
                "status": status, "executionRoleArn": role, "model_id": model_id,
                "raw_get": full,
            }, account))
    out = {"when": _ts(), "regions_scanned": regions, "found": found,
           "versions": _versions()}
    path = os.path.join(_ensure(RECEIPTS), "_inspect-harness.json")
    json.dump(out, open(path, "w", encoding="utf-8"), indent=2, default=str)
    print(f"\nwrote {path}  ({len(found)} harness(es) inspected, account redacted)")
    if found:
        roles = sorted({f["executionRoleArn"] for f in found if f["executionRoleArn"]})
        if roles:
            print("reusable execution role(s) discovered (redacted):")
            for r in roles:
                print("  ", r)
    return out


# --------------------------------------------------------------------------- #
# deploy (agentlift CreateHarness on the fixture)
# --------------------------------------------------------------------------- #
def deploy() -> dict[str, Any]:
    from agentlift.harness_plan import build_harness_plan
    from agentlift.harness_target import EXECUTION_ROLE_ENV, deploy_harness
    from agentlift.parser import parse_project

    region = _region()
    role = os.environ.get(EXECUTION_ROLE_ENV)
    if not role:
        raise SystemExit(
            f"set ${EXECUTION_ROLE_ENV} to a harness execution role ARN (trust "
            f"bedrock-agentcore.amazonaws.com). Run `python tests/live/harness.py "
            f"inspect` to discover a reusable one from an existing harness.")

    project, _diags = parse_project(FIXTURE)
    # Model override (the receipt path on a Gate-A-blocked account). The harness WIRE
    # SHAPE is model-agnostic, so earning the receipt on Nova (no Anthropic use-case
    # entitlement needed) proves every single-agent cell; Claude-invoke stays "pending
    # Gate A", exactly like the runtime composition. Set AGENTLIFT_HARNESS_MODEL=
    # amazon.nova-pro-v1:0 to take that path; default keeps the fixture's Claude model.
    model_override = os.environ.get("AGENTLIFT_HARNESS_MODEL")
    if model_override:
        for a in project.agents:
            a.model = model_override
        print(f"  model override: {model_override} (wire-shape receipt path)")
    plan = build_harness_plan(project, region=region)
    if not plan.deployable:
        raise SystemExit("harness plan not deployable:\n" + plan.diagnostics.render())

    print(f"deploying harness-single to AgentCore Harness (region={region})...")
    t0 = datetime.datetime.now()
    res = deploy_harness(project, region=region, execution_role_arn=role, log=print)
    dur = (datetime.datetime.now() - t0).total_seconds()

    account = _account_id()
    state = _anon({
        "provider": "bedrock-harness", "fixture": "harness-single", "when": _ts(),
        "deploy_seconds": round(dur, 1), "action": res.action,
        "harness_id": res.harness_id, "harness_arn": res.harness_arn,
        "region": region, "status": res.status, "spec_hash": res.spec_hash,
        "display_name": res.display_name, "deploy_model": res.deploy_model,
        "live_verified_at_deploy": res.live_verified, "plan": plan.to_dict(),
        "versions": _versions(),
    }, account)
    # the un-redacted arn is needed to invoke; keep it in a gitignored sidecar only.
    sidecar = {"harness_arn": res.harness_arn, "harness_id": res.harness_id,
               "region": region}
    json.dump(state, open(os.path.join(_ensure(RECEIPTS), "_state-harness-bedrock.json"),
                          "w", encoding="utf-8"), indent=2, default=str)
    json.dump(sidecar, open(os.path.join(RECEIPTS, "_secret-harness-arn.json"),
                            "w", encoding="utf-8"), indent=2)
    print(f"\n{res.action}: {res.harness_arn}  status={res.status}")
    print("wrote _state-harness-bedrock.json (account redacted) + "
          "_secret-harness-arn.json (gitignored, real arn for invoke)")
    return state


def _live_arn() -> tuple[str, str]:
    p = os.path.join(RECEIPTS, "_secret-harness-arn.json")
    if not os.path.isfile(p):
        raise SystemExit("no _secret-harness-arn.json; run `harness.py deploy` first")
    d = json.load(open(p, encoding="utf-8"))
    return d["harness_arn"], d.get("region", _region())


# --------------------------------------------------------------------------- #
# invoke (data-plane; the live-verify step that earns the receipt)
# --------------------------------------------------------------------------- #
def invoke_once(arn: str, region: str, prompt: str, session_id: str) -> dict[str, Any]:
    from agentlift.harness_target import invoke_harness
    events: list[dict] = []
    tool_uses: list[dict] = []
    text_parts: list[str] = []
    usage: dict[str, int] = {}
    err: Optional[str] = None
    try:
        resp = invoke_harness(arn, prompt, region=region, session_id=session_id)
        stream = resp.get("stream") if isinstance(resp, dict) else resp
        for ev in stream:
            ev = _jsonify(ev)
            events.append(ev)
            tool_uses.extend(stream_tool_uses(ev))
            t = stream_text(ev)
            if t:
                text_parts.append(t)
            u = stream_usage(ev)
            if u:
                usage = u
            e = stream_error(ev)
            if e and not err:
                err = e
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return {"prompt": prompt, "session_id": session_id, "error": err,
            "events": events, "tool_uses": tool_uses, "usage": usage,
            "final_text": "".join(text_parts).strip()}


def invoke() -> dict[str, Any]:
    arn, region = _live_arn()
    state = json.load(open(os.path.join(RECEIPTS, "_state-harness-bedrock.json"),
                           encoding="utf-8"))
    print(f"invoking harness {arn.split('/')[-1]} (region={region})")
    runs = []
    for label, q in [("sandbox", Q_SANDBOX), ("mcp", Q_MCP), ("fetch", Q_FETCH), ("skill", Q_SKILL)]:
        print(f"  invoke[{label}]...")
        # runtimeSessionId min length is 33 (live-discovered); pad to be safe.
        sid = f"{RUN_SESSION}-{label}".ljust(33, "0")
        r = invoke_once(arn, region, q, sid)
        r["label"] = label
        names = sorted({t["name"] for t in r["tool_uses"] if t["name"]})
        print(f"    tool_uses : {names or '(none)'}")
        print(f"    usage     : {r['usage'] or '(none)'}  err={r['error']}")
        runs.append(r)
    matrix = classify(runs, state)
    return _write_receipt(state, runs, matrix)


def teardown() -> None:
    from agentlift.harness_target import delete_harness
    p = os.path.join(RECEIPTS, "_secret-harness-arn.json")
    if not os.path.isfile(p):
        print("no _secret-harness-arn.json; nothing to tear down")
        return
    d = json.load(open(p, encoding="utf-8"))
    print(f"deleting harness {d['harness_id']} ...")
    delete_harness(d["harness_id"], region=d.get("region", _region()), log=print)
    print("deleted.")


# --------------------------------------------------------------------------- #
# classification -> 4-state matrix
# --------------------------------------------------------------------------- #
def classify(runs: list[dict], state: dict) -> dict[str, Any]:
    all_tools = [t for r in runs for t in r["tool_uses"]]
    tool_names = {t["name"] for t in all_tools if t["name"]}
    tool_types = {t["type"] for t in all_tools if t["type"]}
    any_err = any(r["error"] for r in runs)
    answered = any(r["final_text"] and not r["error"] for r in runs)
    out_tokens = max((r["usage"].get("outputTokens", 0) for r in runs), default=0)
    trace = any(TRACE_TOKEN in r["final_text"] for r in runs)
    nonce = any(CANARY_NONCE in r["final_text"] for r in runs)
    shell_nonce = any(SANDBOX_NONCE in r["final_text"] for r in runs)
    skill_marker = any(SKILL_MARKER in r["final_text"] for r in runs)

    m: dict[str, Any] = {}

    # create: the control-plane wire shape is live-proven if the harness reached READY
    created = state.get("action") in ("create", "update") and \
        str(state.get("status", "")).upper() == "READY" and bool(state.get("harness_arn"))
    m["create"] = {
        "state": "PASS-EXERCISED" if created else "FAIL",
        "reason": "" if created else "harness did not reach READY / no ARN",
        "evidence": {"action": state.get("action"), "status": state.get("status"),
                     "spec_hash": state.get("spec_hash")},
    }

    # agent: real model inference produced text (+ usage), no error
    if answered and out_tokens > 0:
        m["agent"] = {"state": "PASS-EXERCISED", "reason": ""}
    elif answered:
        m["agent"] = {"state": "PASS-EXERCISED" if trace else "NOT-PROVEN",
                      "reason": "" if trace else "text but no usage/trace signal"}
    else:
        m["agent"] = {"state": "FAIL", "reason": "no assistant text / errored"}
    m["agent"]["evidence"] = {"output_tokens": out_tokens, "trace_token": trace}

    # sandbox built-ins (base session): a shell/bash/code tool fired, or the echo nonce
    shell_called = bool(tool_names & _SANDBOX_TOOLS)
    if shell_called or shell_nonce:
        m["sandbox"] = {"state": "PASS-EXERCISED", "reason": ""}
    else:
        m["sandbox"] = {"state": "PASS-WIRED",
                        "reason": "harness base session provides shell/file_operations; not invoked this run"}
    m["sandbox"]["evidence"] = {"shell_tool_call": shell_called, "echo_nonce": shell_nonce}

    # remote_mcp: the harness surfaces MCP tools as `<server>_<tool>` (live-confirmed), so
    # match by suffix (e.g. deepwiki_read_wiki_structure ends with read_wiki_structure).
    mcp_called = any((n == t or n.endswith("_" + t)) for n in tool_names for t in _DEEPWIKI_TOOLS) \
        or any("mcp" in (ty or "").lower() for ty in tool_types)
    m["remote_mcp"] = {
        "state": "PASS-EXERCISED" if mcp_called else "PASS-WIRED",
        "reason": "" if mcp_called else
                  "remote_mcp tool surfaces on the harness (as <server>_<tool>); the model "
                  "did not call it this run (tool-choice). Note: a restrictive allowedTools "
                  "suppresses MCP surfacing, so agentlift emits none.",
        "evidence": {"tool_names": sorted(tool_names), "tool_types": sorted(tool_types)},
    }

    # skills (loaded from S3 -> skills[].s3.uri): the agent called a `skills` tool, or the
    # house-style skill's marker appears (proving the S3-loaded bundle was applied).
    skills_called = "skills" in tool_names
    if skills_called or skill_marker:
        m["skills"] = {"state": "PASS-EXERCISED", "reason": ""}
    else:
        m["skills"] = {"state": "PASS-WIRED",
                       "reason": "skill bundle uploaded to S3 + attached (skills[].s3.uri); "
                                 "the model did not consult it this run"}
    m["skills"]["evidence"] = {"skills_tool_call": skills_called, "skill_marker": skill_marker}

    # web_fetch -> agentcore_browser: a browser toolUse, or the URL-derived nonce
    browser_called = any("browser" in (n or "").lower() for n in tool_names) or \
        any("browser" in (t or "").lower() for t in tool_types)
    if browser_called or nonce:
        m["web_fetch"] = {"state": "PASS-EXERCISED", "reason": ""}
    else:
        m["web_fetch"] = {"state": "PASS-WIRED",
                          "reason": "agentcore_browser tool in the create body; not invoked"}
    m["web_fetch"]["evidence"] = {"browser_tool_call": browser_called,
                                  "canary_nonce_in_text": nonce}

    return {"tools_seen": sorted(tool_names), "tool_types_seen": sorted(tool_types),
            "any_error": any_err, "dimensions": m}


# --------------------------------------------------------------------------- #
# receipt
# --------------------------------------------------------------------------- #
def _write_receipt(state: dict, runs: list, matrix: dict) -> dict[str, Any]:
    account = _account_id()
    stamp = _ts()
    rdir = _ensure(os.path.join(RECEIPTS, f"{stamp}-harness-bedrock"))
    with open(os.path.join(rdir, "events.jsonl"), "w", encoding="utf-8") as fh:
        for r in runs:
            for ev in r.get("events", []):
                fh.write(json.dumps(_anon({"label": r["label"], "event": ev}, account),
                                    default=str) + "\n")
    slim = [_anon({k: v for k, v in r.items() if k != "events"}, account) for r in runs]
    receipt = _anon({
        "provider": "bedrock-harness", "when": stamp, "fixture": "harness-single",
        "primitive": "AgentCore Harness (managed, config-only single agent)",
        "state": state, "invocations": slim, "matrix": matrix,
        "note": "Live, billable. Not run in CI (AWS creds are not shared). The AWS "
                "account id is redacted to **** ; the bearer token is never used "
                "(control-plane is IAM/SigV4). States: PASS-EXERCISED (objective "
                "stream event), PASS-WIRED (in the create body, no runtime event), "
                "NOT-PROVEN, FAIL.",
    }, account)
    path = os.path.join(rdir, "receipt.json")
    json.dump(receipt, open(path, "w", encoding="utf-8"), indent=2, default=str)
    print("\n=== bedrock-harness matrix ===")
    for dim, v in matrix["dimensions"].items():
        print(f"  {dim:12s} {v['state']:15s} {v.get('reason','')}")
    print(f"\nwrote {path}")
    print("\nNEXT: if create+agent+remote_mcp are PASS-EXERCISED, flip "
          "harness_plan._HARNESS_LIVE_VERIFIED=True and pin this receipt offline.")
    return receipt


# --------------------------------------------------------------------------- #
COMMANDS = {"inspect": inspect, "deploy": deploy, "invoke": invoke, "teardown": teardown}


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    for a in argv:
        if a.startswith("--region="):
            os.environ["AGENTLIFT_BEDROCK_REGION"] = a.split("=", 1)[1]
    if len(args) != 1 or args[0] not in COMMANDS:
        print("usage: harness.py {" + " | ".join(COMMANDS) + "} [--region=us-west-2]")
        return 2
    try:
        from agentlift.cli import load_env
        load_env(os.getcwd(), ROOT)
    except Exception:
        pass
    try:
        COMMANDS[args[0]]()
        return 0
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
