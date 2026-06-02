"""Invoke agents — the deployed managed one, or the same definition locally.

`run_managed` calls the hosted agent by ID (create env -> session -> stream
events -> usage). `run_local` runs the SAME folder on your machine via the
Messages API plus local tool execution, so you can prove the definition is
portable: one folder, two runtimes, same behavior.
"""
from __future__ import annotations

import glob as _glob
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .cost import Usage, estimate_cost, usage_from_session
from .model import AgentSpec

BETAS = ["managed-agents-2026-04-01", "skills-2025-10-02"]


@dataclass
class RunResult:
    output: str = ""
    latency_s: float = 0.0
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    used_tool: bool = False
    status: str = ""
    ok: bool = True
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# managed
# --------------------------------------------------------------------------- #
def create_environment(client, betas: Optional[list[str]] = None, name: str = "skylift-env"):
    return client.beta.environments.create(
        name=name, config={"type": "cloud", "networking": {"type": "unrestricted"}},
        betas=betas or BETAS,
    )


def run_managed(
    client, agent_id: str, version: Any, task: str, *,
    model: str, environment_id: Optional[str] = None,
    betas: Optional[list[str]] = None,
) -> RunResult:
    betas = betas or BETAS
    t0 = time.time()
    try:
        if environment_id is None:
            environment_id = create_environment(client, betas).id
        session = client.beta.sessions.create(
            agent={"type": "agent", "id": agent_id, "version": version},
            environment_id=environment_id, betas=betas,
        )
        client.beta.sessions.events.send(
            session_id=session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": task}]}],
            betas=betas,
        )
        texts: list[str] = []
        used_tool = False
        with client.beta.sessions.events.stream(session_id=session.id, betas=betas) as stream:
            for ev in stream:
                et = getattr(ev, "type", "")
                if et in ("agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use"):
                    used_tool = True
                content = getattr(ev, "content", None)
                if content and et == "agent.message":
                    for b in content:
                        if getattr(b, "type", None) == "text":
                            texts.append(b.text)
                if et == "session.status_terminated":
                    break
                if et == "session.status_idle":
                    sr = getattr(ev, "stop_reason", None)
                    if not sr or getattr(sr, "type", None) != "requires_action":
                        break
        sess = client.beta.sessions.retrieve(session_id=session.id, betas=betas)
        usage = usage_from_session(getattr(sess, "usage", None))
        return RunResult(
            output=" ".join(texts).strip(),
            latency_s=round(time.time() - t0, 2),
            usage=usage,
            cost=estimate_cost(usage, model),
            used_tool=used_tool,
            status=str(getattr(sess, "status", "")),
        )
    except Exception as e:
        return RunResult(ok=False, error=f"{type(e).__name__}: {e}", latency_s=round(time.time() - t0, 2))


# --------------------------------------------------------------------------- #
# local (same definition, your machine)
# --------------------------------------------------------------------------- #
_LOCAL_TOOLS = [
    {"name": "read_file", "description": "Read a UTF-8 text file by path (relative to the agent dir).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_files", "description": "List files matching a glob (relative to the agent dir).",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "run_bash", "description": "Run a shell command in the agent dir and return stdout/stderr.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
]


def _safe(path: str, root: str) -> Optional[str]:
    full = os.path.abspath(os.path.join(root, path))
    if not full.startswith(os.path.abspath(root)):
        return None
    if ".env" in os.path.basename(full):
        return None
    return full


def _exec_local_tool(name: str, args: dict, root: str) -> str:
    try:
        if name == "read_file":
            p = _safe(args.get("path", ""), root)
            if not p or not os.path.isfile(p):
                return f"ERROR: cannot read {args.get('path')!r}"
            return open(p, "r", encoding="utf-8", errors="replace").read()[:20000]
        if name == "list_files":
            matches = _glob.glob(os.path.join(root, args.get("pattern", "*")), recursive=True)
            return "\n".join(os.path.relpath(m, root) for m in sorted(matches)[:200]) or "(no matches)"
        if name == "run_bash":
            proc = subprocess.run(
                args.get("command", ""), shell=True, cwd=root, capture_output=True,
                text=True, timeout=60,
            )
            return (proc.stdout + proc.stderr)[:20000]
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    return "ERROR: unknown tool"


def run_local(client, agent: AgentSpec, task: str, *, model: Optional[str] = None, max_turns: int = 8) -> RunResult:
    """Run the agent definition locally: Messages API + local tool execution.
    Skills' SKILL.md are inlined into the system prompt (same content the managed
    runtime loads them as)."""
    model = model or agent.model
    t0 = time.time()
    system = agent.system
    for sk in agent.skills:
        skill_md = os.path.join(sk.source_dir, "SKILL.md")
        if os.path.isfile(skill_md):
            system += f"\n\n# Skill: {sk.name}\n\n" + open(skill_md, "r", encoding="utf-8", errors="replace").read()

    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    in_tok = out_tok = 0
    used_tool = False
    final_text: list[str] = []
    try:
        for _ in range(max_turns):
            resp = client.messages.create(
                model=model, max_tokens=2048, system=system,
                tools=_LOCAL_TOOLS, messages=messages,
            )
            in_tok += getattr(resp.usage, "input_tokens", 0) or 0
            out_tok += getattr(resp.usage, "output_tokens", 0) or 0
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    final_text.append(b.text)
            if not tool_uses:
                break
            used_tool = True
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in tool_uses:
                out = _exec_local_tool(tu.name, tu.input, agent.source_dir or ".")
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
            messages.append({"role": "user", "content": results})
        usage = Usage(input_tokens=in_tok, output_tokens=out_tok)
        return RunResult(
            output="\n".join(t for t in final_text if t).strip(),
            latency_s=round(time.time() - t0, 2),
            usage=usage, cost=estimate_cost(usage, model),
            used_tool=used_tool, status="local",
        )
    except Exception as e:
        return RunResult(ok=False, error=f"{type(e).__name__}: {e}", latency_s=round(time.time() - t0, 2))
