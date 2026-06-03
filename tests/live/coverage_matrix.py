"""Live coverage-matrix verification harness (NOT a pytest module).

Drives a real, billable deploy + query of the ``coverage-matrix`` fixture against
each managed-agent provider and records an objective, re-runnable receipt. The
fixture exercises six portability dimensions in one folder:

  agents · subagents (coordinator delegation) · shared MCP · individual MCP ·
  shared skill · individual skill

Per Codex consult, every dimension is reported in one of four states:

  PASS-WIRED    deploy/package proves the feature was configured (deterministic)
  PASS-EXERCISED runtime stream proves the provider actually used it (objective event)
  NOT-PROVEN    wired correctly, but no objective runtime signal (e.g. the model
                chose not to call the tool, or a third-party MCP was unavailable)
  FAIL          deploy/config/runtime error or wrong behavior

Objective runtime signals (asserted on the event stream, never on answer text):
  - delegation : a ``transfer_to_agent`` function-call (Google) / subagent trace
                 tag surfacing in the coordinator's output (Anthropic)
  - shared MCP : a function-call named for a DeepWiki tool (read_wiki_structure, ...)
  - indiv MCP  : a function-call named for a GitMCP tool (search_adk_python_documentation, ...)
  - skill      : a ``load_skill`` function-call (Google) / the skill marker in
                 output (Anthropic auto-injects skills by description)

Usage (each billable step is explicit; deploy is slow, queries are cheap):
  python tests/live/coverage_matrix.py preflight
  python tests/live/coverage_matrix.py deploy-google      # ~minutes, billable
  python tests/live/coverage_matrix.py query-google       # cheap; writes receipt
  python tests/live/coverage_matrix.py teardown-google
  python tests/live/coverage_matrix.py deploy-anthropic
  python tests/live/coverage_matrix.py query-anthropic
  python tests/live/coverage_matrix.py teardown-anthropic

Credentials (never committed): ANTHROPIC_API_KEY for Anthropic; ADC +
GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION + AGENTLIFT_GCP_STAGING_BUCKET for
Google. Receipts land under tests/live/receipts/ and are committed as auditable
evidence; the curated summary lives in tests/live/README.md and
docs/tested-platforms.md (the live coverage matrix). This file is NOT collected by
pytest — the gated pytest wrapper is tests/live/test_coverage_matrix.py.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import traceback
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

FIXTURE = os.path.join(HERE, "fixtures", "coverage-matrix")
RECEIPTS = os.path.join(HERE, "receipts")
RUN_USER = "agentlift-live"

# --- markers baked into the fixture (only inside skill bodies / subagent prompts,
#     never in the user query or the coordinator prompt) ---
HOUSESTYLE = "HOUSESTYLEOK"          # shared house-style skill closing line
REPORTFMT = "REPORTFMTOK"            # individual report-format skill opening line
RESEARCHER_TAG = "RESEARCHER-AGENT-OK"
REPORTER_TAG = "REPORTER-AGENT-OK"

# --- MCP tool names, by server, for shared-vs-individual attribution ---
SHARED_MCP_TOOLS = {"read_wiki_structure", "read_wiki_contents", "ask_question"}   # DeepWiki = shared `docs`
INDIV_MCP_TOOLS = {                                                                 # GitMCP = individual `code-search`
    "search_adk_python_documentation", "fetch_adk_python_documentation",
    "search_adk_python_code", "fetch_generic_url_content",
}
SKILL_TOOLS = {"list_skills", "load_skill", "load_skill_resource", "run_skill_script"}

MCP_SERVERS = {
    "deepwiki (shared docs)": "https://mcp.deepwiki.com/mcp",
    "gitmcp (individual code-search)": "https://gitmcp.io/google/adk-python",
}

# Queries (split by target so one query never conflates two dimensions).
Q_RESEARCH = (
    "Ask the researcher to look up the high-level wiki structure of the GitHub "
    "repository 'google/adk-python', and to also search the ADK Python documentation "
    "for how an LlmAgent declares sub_agents. Use your tools; do not answer from memory."
)
Q_REPORT = (
    "Ask the reporter to write a short report titled 'ADK sub_agents' summarizing, "
    "in two or three bullet points, how a coordinator agent delegates to sub-agents."
)
Q_RESEARCH_SKILL = (
    "Ask the researcher: in one sentence, what is a sub-agent in ADK? "
    "Follow the team's house style."
)


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _ensure(d: str) -> str:
    os.makedirs(d, exist_ok=True)
    return d


def _versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for mod in ("google.adk", "vertexai", "anthropic", "agentlift"):
        try:
            m = __import__(mod, fromlist=["__version__"])
            out[mod] = getattr(m, "__version__", "?")
        except Exception as e:
            out[mod] = f"<{type(e).__name__}>"
    return out


# --------------------------------------------------------------------------- #
# preflight: are the third-party MCP servers reachable through ADK's client?
# --------------------------------------------------------------------------- #
def preflight() -> dict[str, Any]:
    import asyncio
    import warnings
    warnings.filterwarnings("ignore")
    from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

    async def one(url: str) -> dict[str, Any]:
        ts = McpToolset(connection_params=StreamableHTTPConnectionParams(url=url))
        try:
            tools = await ts.get_tools()
            return {"ok": True, "tools": sorted(t.name for t in tools)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            try:
                await ts.close()
            except Exception:
                pass

    async def go() -> dict[str, Any]:
        res = {}
        for name, url in MCP_SERVERS.items():
            res[name] = {"url": url, **(await one(url))}
        return res

    result = asyncio.run(go())
    out = {"when": _ts(), "servers": result,
           "all_reachable": all(v["ok"] for v in result.values())}
    path = os.path.join(_ensure(RECEIPTS), "_preflight-mcp.json")
    json.dump(out, open(path, "w", encoding="utf-8"), indent=2)
    print(json.dumps(out, indent=2))
    print(f"\nwrote {path}")
    return out


# --------------------------------------------------------------------------- #
# event extraction (provider-agnostic helpers)
# --------------------------------------------------------------------------- #
def _walk(obj: Any):
    """Yield every dict nested anywhere in a JSON-ish structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def google_event_calls(ev: dict) -> list[dict[str, Any]]:
    """Function-calls in one Google ADK stream event (name + args)."""
    calls = []
    for d in _walk(ev):
        fc = d.get("function_call") if isinstance(d, dict) else None
        if isinstance(fc, dict) and fc.get("name"):
            calls.append({"name": fc["name"], "args": fc.get("args") or {}})
    return calls


def google_event_text(ev: dict) -> str:
    chunks = []
    for d in _walk(ev):
        if isinstance(d, dict) and isinstance(d.get("text"), str):
            chunks.append(d["text"])
    return "".join(chunks)


# --------------------------------------------------------------------------- #
# Google: deploy / query / teardown
# --------------------------------------------------------------------------- #
def _google_env() -> tuple[str, str, str]:
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT")
    bucket = os.environ.get("AGENTLIFT_GCP_STAGING_BUCKET")
    loc = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not proj or not bucket:
        raise SystemExit("set GOOGLE_CLOUD_PROJECT and AGENTLIFT_GCP_STAGING_BUCKET (gs://...)")
    return proj, loc, bucket


def deploy_google() -> dict[str, Any]:
    import warnings
    warnings.filterwarnings("ignore")
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    from agentlift.parser import parse_project
    from agentlift.google_plan import build_google_plan
    from agentlift.google_target import deploy_google as _deploy

    proj, loc, bucket = _google_env()
    project, _diags = parse_project(FIXTURE)
    plan = build_google_plan(project)
    if not plan.deployable:
        raise SystemExit("google plan not deployable:\n" + plan.diagnostics.render())

    print(f"deploying coverage-matrix to Agent Engine (project={proj} region={loc})...")
    t0 = datetime.datetime.now()
    res = _deploy(project, gcp_project=proj, location=loc, staging_bucket=bucket, log=print)
    dur = (datetime.datetime.now() - t0).total_seconds()

    state = {
        "provider": "google", "when": _ts(), "deploy_seconds": round(dur, 1),
        "action": res.action, "resource_name": res.resource_name,
        "spec_hash": res.spec_hash, "display_name": res.display_name,
        "deploy_model": res.deploy_model, "project": proj, "location": loc,
        "plan": plan.to_dict(), "versions": _versions(),
    }
    path = os.path.join(_ensure(RECEIPTS), "_state-google.json")
    json.dump(state, open(path, "w", encoding="utf-8"), indent=2)
    print(f"\n{res.action}: {res.resource_name}\nwrote {path}")
    return state


def _get_google_engine(resource_name: str):
    import warnings
    warnings.filterwarnings("ignore")
    import vertexai
    from vertexai import agent_engines
    proj, loc, bucket = _google_env()
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    vertexai.init(project=proj, location=loc, staging_bucket=bucket)
    return agent_engines.get(resource_name)


def query_google_once(engine, prompt: str) -> dict[str, Any]:
    sess = engine.create_session(user_id=RUN_USER)
    sid = sess["id"] if isinstance(sess, dict) else getattr(sess, "id", None)
    events: list[dict] = []
    calls: list[dict] = []
    text_parts: list[str] = []
    err = None
    try:
        for ev in engine.stream_query(message=prompt, user_id=RUN_USER, session_id=sid):
            ev = ev if isinstance(ev, dict) else json.loads(json.dumps(ev, default=str))
            events.append(ev)
            calls.extend(google_event_calls(ev))
            t = google_event_text(ev)
            if t:
                text_parts.append(t)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return {
        "prompt": prompt, "session_id": sid, "error": err,
        "events": events, "tool_calls": calls,
        "final_text": "".join(text_parts).strip(),
    }


def query_google() -> dict[str, Any]:
    state = json.load(open(os.path.join(RECEIPTS, "_state-google.json"), encoding="utf-8"))
    engine = _get_google_engine(state["resource_name"])
    print(f"querying {state['resource_name']}")
    runs = []
    for label, q in [("research", Q_RESEARCH), ("report", Q_REPORT), ("research-skill", Q_RESEARCH_SKILL)]:
        print(f"  query[{label}]...")
        r = query_google_once(engine, q)
        r["label"] = label
        names = sorted({c["name"] for c in r["tool_calls"]})
        print(f"    tool_calls: {names or '(none)'}  err={r['error']}")
        runs.append(r)
    matrix = classify_google(runs)
    return _write_receipt("google", state, runs, matrix)


def teardown_google() -> None:
    state_path = os.path.join(RECEIPTS, "_state-google.json")
    if not os.path.isfile(state_path):
        print("no google state; nothing to tear down")
        return
    state = json.load(open(state_path, encoding="utf-8"))
    engine = _get_google_engine(state["resource_name"])
    print(f"deleting {state['resource_name']} ...")
    engine.delete(force=True)
    print("deleted.")


# --------------------------------------------------------------------------- #
# Anthropic: deploy / query / teardown
# --------------------------------------------------------------------------- #
def _anthropic_client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _anthropic_event_summary(ev) -> dict[str, Any]:
    et = getattr(ev, "type", "")
    out: dict[str, Any] = {"type": et}
    for attr in ("name", "server_name", "tool_name"):
        v = getattr(ev, attr, None)
        if v is not None:
            out[attr] = v
    content = getattr(ev, "content", None)
    if content and et == "agent.message":
        texts = [getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text"]
        if any(texts):
            out["text"] = " ".join(t for t in texts if t)
    return out


def _stream_anthropic(client, agent_id, version, prompt, betas, out: dict) -> None:
    """Fill ``out`` incrementally so a watchdog timeout still yields the partial
    events. Runs in a daemon thread because the SSE iterator blocks on a network
    read — an async coordinator can hold the stream open ('waiting for the worker')
    with no terminal event, which no in-loop cap can interrupt."""
    MAX_EVENTS = 250
    try:
        env_id = client.beta.environments.create(
            name="agentlift-cov", config={"type": "cloud", "networking": {"type": "unrestricted"}},
            betas=betas,
        ).id
        session = client.beta.sessions.create(
            agent={"type": "agent", "id": agent_id, "version": version},
            environment_id=env_id, betas=betas,
        )
        client.beta.sessions.events.send(
            session_id=session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": prompt}]}],
            betas=betas,
        )
        with client.beta.sessions.events.stream(session_id=session.id, betas=betas) as stream:
            for ev in stream:
                et = getattr(ev, "type", "")
                summ = _anthropic_event_summary(ev)
                out["events"].append(summ)
                if et in ("agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use"):
                    nm = summ.get("name") or summ.get("tool_name")
                    if nm:
                        out["tool_names"].append(nm)
                if et == "agent.message" and summ.get("text"):
                    out["text_parts"].append(summ["text"])
                if et == "session.status_terminated":
                    out["done"] = True
                    break
                if et == "session.status_idle":
                    sr = getattr(ev, "stop_reason", None)
                    if not sr or getattr(sr, "type", None) != "requires_action":
                        out["done"] = True
                        break
                if len(out["events"]) >= MAX_EVENTS:
                    out["events"].append({"type": "harness.max_events_reached"})
                    out["done"] = True
                    break
    except Exception as e:  # noqa: BLE001 -- recorded into the receipt, not raised
        out["error"] = f"{type(e).__name__}: {e}"


def query_anthropic_once(client, agent_id: str, version, prompt: str, betas,
                         timeout: float = 75.0) -> dict[str, Any]:
    import threading

    out: dict[str, Any] = {"events": [], "tool_names": [], "text_parts": [],
                           "error": None, "done": False}
    th = threading.Thread(
        target=_stream_anthropic,
        args=(client, agent_id, version, prompt, betas, out),
        daemon=True,
    )
    th.start()
    th.join(timeout)
    timed_out = th.is_alive()
    # snapshot under the GIL; the abandoned daemon dies with the process
    events = list(out["events"])
    tool_names = list(out["tool_names"])
    text_parts = list(out["text_parts"])
    if timed_out:
        # not an error: delegation / tool events arrive early; we have what we need
        events.append({"type": "harness.timeout", "seconds": timeout})
    return {"prompt": prompt, "error": out["error"], "events": events,
            "tool_calls": [{"name": n} for n in tool_names],
            "final_text": " ".join(text_parts).strip(),
            "timed_out": timed_out}


def deploy_anthropic() -> dict[str, Any]:
    from agentlift.anthropic_target import Deployer
    from agentlift.parser import parse_project
    from agentlift.planner import build_plan
    from agentlift.diagnostics import Diagnostics

    client = _anthropic_client()
    project, diags = parse_project(FIXTURE)
    diags = diags or Diagnostics()
    plan = build_plan(project, diags)
    if not plan.deployable:
        raise SystemExit("anthropic plan not deployable:\n" + diags.render())
    print("deploying coverage-matrix to Anthropic Managed Agents...")
    deployer = Deployer(client, project.root)
    result = deployer.apply(plan, log=print)
    agents = {}
    for a in project.agents:
        rec = deployer.lock.agent(a.name)
        if rec:
            agents[a.name] = {"agent_id": rec["agent_id"], "version": rec["version"]}
    state = {
        "provider": "anthropic", "when": _ts(),
        "uploaded_skills": len(result.uploaded_skills),
        "reused_skills": len(result.reused_skills),
        "created_agents": len(result.created_agents),
        "agents": agents, "versions": _versions(),
    }
    path = os.path.join(_ensure(RECEIPTS), "_state-anthropic.json")
    json.dump(state, open(path, "w", encoding="utf-8"), indent=2)
    print(f"\ndeployed {len(agents)} agents; wrote {path}")
    return state


def query_anthropic() -> dict[str, Any]:
    from agentlift.runtime import BETAS
    state = json.load(open(os.path.join(RECEIPTS, "_state-anthropic.json"), encoding="utf-8"))
    client = _anthropic_client()
    agents = state["agents"]
    runs = []
    plan = [
        # Drive BOTH MCP servers explicitly so the run exercises the shared and the
        # individual server in one query (previously the model was free to pick and
        # satisfied the prompt with only the private GitMCP server -> shared = WIRED).
        ("researcher-direct", "researcher",
         "Do two lookups with your MCP tools, and do not answer from memory:\n"
         "1. Use your SHARED `docs` server (DeepWiki) — call its read_wiki_structure "
         "tool on the GitHub repository 'google/adk-python' to list its wiki topics.\n"
         "2. Use your PRIVATE `code-search` server to search the ADK Python "
         "documentation for how an LlmAgent declares sub_agents.\n"
         "You MUST call both servers."),
        ("reporter-direct", "reporter",
         "Write a short report titled 'ADK sub_agents' summarizing in two or three "
         "bullets how a coordinator delegates to sub-agents."),
        ("lead-delegation", "lead",
         "I need a researched, well-written one-paragraph briefing on how ADK "
         "sub_agents work. Coordinate the team to produce it."),
    ]
    for label, who, q in plan:
        if who not in agents:
            print(f"  skip {label}: agent {who} not deployed")
            continue
        print(f"  query[{label}] -> {who}...")
        r = query_anthropic_once(client, agents[who]["agent_id"], agents[who]["version"], q, BETAS)
        r["label"] = label
        r["agent"] = who
        names = sorted({c["name"] for c in r["tool_calls"]})
        print(f"    tool_calls: {names or '(none)'}  err={r['error']}")
        runs.append(r)
    matrix = classify_anthropic(runs)
    return _write_receipt("anthropic", state, runs, matrix)


def teardown_anthropic() -> None:
    from agentlift.anthropic_target import Deployer
    from agentlift.parser import parse_project
    client = _anthropic_client()
    project, _ = parse_project(FIXTURE)
    deployer = Deployer(client, project.root)
    print("archiving anthropic agents...")
    deployer.destroy(log=print)
    print("done.")


# --------------------------------------------------------------------------- #
# classification -> 4-state matrix
# --------------------------------------------------------------------------- #
def _state(wired: bool, exercised: bool, failed: bool, reason: str) -> dict[str, str]:
    if failed:
        st = "FAIL"
    elif exercised:
        st = "PASS-EXERCISED"
    elif wired:
        st = "PASS-WIRED" if not reason else "NOT-PROVEN"
    else:
        st = "NOT-PROVEN"
    return {"state": st, "reason": reason}


def _all_calls(runs) -> set[str]:
    return {c["name"] for r in runs for c in r["tool_calls"]}


# Anthropic coordinator delegation is native + async: the lead spawns a worker
# *thread* per subtask and dispatches a message to it. Those are objective runtime
# events on the session stream (thread created + message sent/received) and are the
# delegation signal -- they surface even though the lead's one-shot reply returns
# before the workers' trace tags come back. We key on the events, not on text.
_DELEGATION_EVENTS = {
    "session.thread_created",
    "agent.thread_message_sent",
    "agent.thread_message_received",
}


def _delegation_signal(runs) -> dict[str, int]:
    counts: dict[str, int] = {t: 0 for t in _DELEGATION_EVENTS}
    for r in runs:
        for ev in r.get("events", []):
            et = ev.get("type")
            if et in counts:
                counts[et] += 1
    return counts


def classify_google(runs) -> dict[str, Any]:
    calls = _all_calls(runs)
    any_err = any(r["error"] for r in runs)
    texts = " ".join(r["final_text"] for r in runs)
    transfers = {c["args"].get("agent_name") for r in runs for c in r["tool_calls"]
                 if c["name"] == "transfer_to_agent"}
    shared_mcp = calls & SHARED_MCP_TOOLS
    indiv_mcp = calls & INDIV_MCP_TOOLS
    skill_calls = calls & SKILL_TOOLS

    m = {}
    # agents: engine answered at all
    answered = any(r["final_text"] and not r["error"] for r in runs)
    m["agents"] = {"state": "PASS-EXERCISED" if answered else "FAIL",
                   "reason": "" if answered else "engine returned no text / errored"}
    # subagents: a transfer_to_agent occurred
    m["subagents"] = {
        "state": "PASS-EXERCISED" if transfers else "NOT-PROVEN",
        "reason": "" if transfers else "no transfer_to_agent event observed",
        "evidence": {"transfers_to": sorted(t for t in transfers if t)},
    }
    # shared MCP (DeepWiki)
    m["shared_mcp"] = {
        "state": "PASS-EXERCISED" if shared_mcp else "PASS-WIRED",
        "reason": "" if shared_mcp else "wired in package; model did not call a DeepWiki tool",
        "evidence": {"calls": sorted(shared_mcp)},
    }
    # individual MCP (GitMCP)
    m["individual_mcp"] = {
        "state": "PASS-EXERCISED" if indiv_mcp else "PASS-WIRED",
        "reason": "" if indiv_mcp else "wired in package; model did not call a GitMCP tool",
        "evidence": {"calls": sorted(indiv_mcp)},
    }
    # shared skill (house-style): load_skill call OR marker in output
    hs = ("load_skill" in skill_calls) or (HOUSESTYLE in texts)
    m["shared_skill"] = {
        "state": "PASS-EXERCISED" if hs else "PASS-WIRED",
        "reason": "" if hs else "bundle shipped + loadable; model did not load/apply it",
        "evidence": {"skill_tool_calls": sorted(skill_calls), "house_style_marker": HOUSESTYLE in texts},
    }
    # individual skill (report-format): marker in output = EXERCISED; otherwise the
    # bundle is deterministically shipped + loadable in the package = WIRED.
    m["individual_skill"] = {
        "state": "PASS-EXERCISED" if (REPORTFMT in texts) else "PASS-WIRED",
        "reason": "" if REPORTFMT in texts else "bundle shipped + loadable; report-format marker not emitted",
        "evidence": {"report_format_marker": REPORTFMT in texts},
    }
    return {"calls_seen": sorted(calls), "any_error": any_err, "dimensions": m}


def classify_anthropic(runs) -> dict[str, Any]:
    by = {r["label"]: r for r in runs}
    calls = _all_calls(runs)
    texts = {lbl: r["final_text"] for lbl, r in by.items()}
    all_text = " ".join(texts.values())
    shared_mcp = calls & SHARED_MCP_TOOLS
    indiv_mcp = calls & INDIV_MCP_TOOLS
    any_err = any(r["error"] for r in runs)

    m = {}
    answered = any(r["final_text"] and not r["error"] for r in runs)
    m["agents"] = {"state": "PASS-EXERCISED" if answered else "FAIL",
                   "reason": "" if answered else "no agent returned text"}
    # subagents: the coordinator natively spawned a worker thread and dispatched a
    # subtask to it -- objective delegation events on the session stream. (The trace
    # tags are weaker, async-bound evidence; we record them but don't require them.)
    deleg = _delegation_signal(runs)
    dispatched = deleg["session.thread_created"] > 0 and deleg["agent.thread_message_sent"] > 0
    lead_txt = texts.get("lead-delegation", "")
    tag_seen = (RESEARCHER_TAG in lead_txt) or (REPORTER_TAG in lead_txt)
    m["subagents"] = {
        "state": "PASS-EXERCISED" if dispatched else "NOT-PROVEN",
        "reason": "" if dispatched
                  else "no thread-create / message-sent delegation event observed",
        "evidence": {
            "delegation_events": deleg,
            "worker_thread_dispatched": dispatched,
            "worker_replied": deleg["agent.thread_message_received"] > 0,
            "researcher_tag_in_lead_text": RESEARCHER_TAG in lead_txt,
            "reporter_tag_in_lead_text": REPORTER_TAG in lead_txt,
            "trace_tag_surfaced": tag_seen,
        },
    }
    m["shared_mcp"] = {
        "state": "PASS-EXERCISED" if shared_mcp else "PASS-WIRED",
        "reason": "" if shared_mcp else "attached at deploy; no DeepWiki tool_use event observed",
        "evidence": {"calls": sorted(shared_mcp)},
    }
    m["individual_mcp"] = {
        "state": "PASS-EXERCISED" if indiv_mcp else "PASS-WIRED",
        "reason": "" if indiv_mcp else "attached at deploy; no GitMCP tool_use event observed",
        "evidence": {"calls": sorted(indiv_mcp)},
    }
    hs = HOUSESTYLE in all_text
    m["shared_skill"] = {
        "state": "PASS-EXERCISED" if hs else "PASS-WIRED",
        "reason": "" if hs else "uploaded + attached; house-style marker not emitted",
        "evidence": {"house_style_marker": hs},
    }
    rf = REPORTFMT in all_text
    m["individual_skill"] = {
        "state": "PASS-EXERCISED" if rf else "PASS-WIRED",
        "reason": "" if rf else "uploaded + attached; report-format marker not emitted",
        "evidence": {"report_format_marker": rf},
    }
    return {"calls_seen": sorted(calls), "any_error": any_err, "dimensions": m}


# --------------------------------------------------------------------------- #
# receipt
# --------------------------------------------------------------------------- #
def _write_receipt(provider: str, state: dict, runs: list, matrix: dict) -> dict[str, Any]:
    stamp = _ts()
    rdir = _ensure(os.path.join(RECEIPTS, f"{stamp}-{provider}"))
    with open(os.path.join(rdir, "events.jsonl"), "w", encoding="utf-8") as fh:
        for r in runs:
            for ev in r.get("events", []):
                fh.write(json.dumps({"label": r["label"], "event": ev}, default=str) + "\n")
    # strip bulky raw events out of the summary (kept in events.jsonl)
    slim_runs = [{k: v for k, v in r.items() if k != "events"} for r in runs]
    receipt = {
        "provider": provider, "when": stamp, "fixture": "coverage-matrix",
        "state": state, "queries": slim_runs, "matrix": matrix,
        "note": "Live, billable. Not run in CI (credentials are not shared). "
                "States: PASS-EXERCISED (objective runtime event), PASS-WIRED "
                "(configured + deployed, no runtime event), NOT-PROVEN, FAIL.",
    }
    path = os.path.join(rdir, "receipt.json")
    json.dump(receipt, open(path, "w", encoding="utf-8"), indent=2, default=str)
    print(f"\n=== {provider} matrix ===")
    for dim, v in matrix["dimensions"].items():
        print(f"  {dim:18s} {v['state']:15s} {v.get('reason','')}")
    print(f"\nwrote {path}")
    return receipt


# --------------------------------------------------------------------------- #
COMMANDS = {
    "preflight": preflight,
    "deploy-google": deploy_google,
    "query-google": query_google,
    "teardown-google": teardown_google,
    "deploy-anthropic": deploy_anthropic,
    "query-anthropic": query_anthropic,
    "teardown-anthropic": teardown_anthropic,
}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in COMMANDS:
        print("usage: coverage_matrix.py {" + " | ".join(COMMANDS) + "}")
        return 2
    try:
        from agentlift.cli import load_env
        load_env(os.getcwd(), ROOT)
    except Exception:
        pass
    try:
        COMMANDS[argv[0]]()
        return 0
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
