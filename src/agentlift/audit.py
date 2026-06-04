"""``agentlift audit`` - the portability report.

Parse a folder once, detect which capabilities it actually exercises, then
cross-reference each against every target provider's capability map
(``capabilities.py``). This is the diagnostic back-end of the compiler; ``export``
is the codegen back-end over the same parsed model. Pure, no network.
"""
from __future__ import annotations

from .capabilities import CAPABILITIES, FEATURES, TIER_ORDER

# Built-in tool names (already mapped to managed builtins by the parser) that
# imply the agent needs a real execution sandbox, not just a model call.
SANDBOX_TOOLS = {"bash", "edit", "write", "glob", "grep"}
# The web built-ins are a separate capability cell: they reach the public network
# rather than a workspace sandbox, and they map differently per provider.
WEB_TOOLS = {"web_search", "web_fetch"}
_LABELS = {f["id"]: f["label"] for f in FEATURES}


def detect_used_features(project) -> dict:
    """Return ``{feature_id: evidence}`` for every capability this folder uses.

    ``hosted_runtime`` / ``deploy_versioning`` / ``streaming`` are intrinsic to
    deploying anything, so they always apply; the rest are detected from the
    parsed agents.
    """
    used = {
        "hosted_runtime": "the target runs the agent loop and is called by id",
        "deploy_versioning": "agents are durable, versioned, re-deployable objects",
        "streaming": "the caller streams the agent's events",
    }
    sandbox: set = set()
    web: set = set()
    approvals: list = []
    skills: set = set()
    mcp: set = set()
    rosters: list = []
    knowledge = 0

    for a in project.agents:
        if a.builtin_tools is None:
            sandbox |= SANDBOX_TOOLS  # None == all builtins enabled
            web |= WEB_TOOLS
        else:
            sandbox |= set(a.builtin_tools) & SANDBOX_TOOLS
            web |= set(a.builtin_tools) & WEB_TOOLS
        for tool, policy in (a.builtin_tool_policies or {}).items():
            if policy == "ask":
                approvals.append(f"{a.name}:{tool}")
        for s in a.skills:
            skills.add(s.name)
        for m in a.mcp_servers:
            mcp.add(m.name)
            for tool, policy in (m.tool_policies or {}).items():
                if policy == "ask":
                    approvals.append(f"{a.name}:{m.name}.{tool}")
        if a.subagents:
            rosters.append(f"{a.name} -> {', '.join(a.subagents)}")
        if a.knowledge_files:
            knowledge += len(a.knowledge_files)

    if sandbox:
        used["builtin_sandbox"] = "uses " + ", ".join(sorted(sandbox))
    if web:
        used["builtin_web"] = "uses " + ", ".join(sorted(web))
    if approvals:
        used["tool_approval"] = ":ask on " + ", ".join(sorted(set(approvals)))
    if skills:
        used["skills"] = ", ".join(sorted(skills))
    if mcp:
        used["remote_mcp"] = ", ".join(sorted(mcp))
    if rosters:
        used["subagents"] = "; ".join(rosters)
    if knowledge:
        used["knowledge"] = f"{knowledge} file(s)"
    return used


def run_audit(project, targets: list) -> dict:
    """Build the per-target report for the features this folder uses."""
    used = detect_used_features(project)
    ordered_ids = [f["id"] for f in FEATURES if f["id"] in used]
    report: dict = {"used": used, "targets": {}, "summary": {}}
    for target in targets:
        caps = CAPABILITIES.get(target)
        if caps is None:
            report["targets"][target] = None
            continue
        rows = []
        counts = {t: 0 for t in TIER_ORDER}
        for fid in ordered_ids:
            cap = caps.get(fid, {"tier": "unsupported", "reason": "not mapped", "remediation": ""})
            rows.append({
                "id": fid, "label": _LABELS[fid], "evidence": used[fid],
                "tier": cap["tier"], "reason": cap.get("reason", ""),
                "remediation": cap.get("remediation", ""),
            })
            counts[cap["tier"]] += 1
        report["targets"][target] = rows
        report["summary"][target] = counts
    return report


_TIER_GLYPH = {"native": "+", "emulated": "~", "degraded": "!", "unsupported": "x"}
_TARGET_TITLE = {
    "anthropic": "Anthropic Managed Agents",
    "bedrock": "Amazon Bedrock AgentCore Runtime (Strands)",
    "google": "Google Vertex AI Agent Engine (ADK)",
    "openai": "OpenAI (Agent Builder / Agents SDK)",
}


def render_audit(project, targets: list, report: dict) -> str:
    """Compiler-style report: per target, features grouped by support tier."""
    used = report["used"]
    out = [
        f"Portability audit: {project.root}",
        f"Agents: {', '.join(a.name for a in project.agents) or '(none)'}",
        f"Capabilities this folder uses: {len(used)}",
        "",
    ]
    for target in targets:
        title = _TARGET_TITLE.get(target, target)
        rows = report["targets"].get(target)
        if rows is None:
            out.append(f"== {title} ==   (unknown target)")
            out.append("")
            continue
        counts = report["summary"][target]
        summ = ", ".join(f"{counts[t]} {t}" for t in TIER_ORDER if counts.get(t))
        out.append(f"== {title} ==   [{summ}]")
        for tier in TIER_ORDER:
            tier_rows = [r for r in rows if r["tier"] == tier]
            if not tier_rows:
                continue
            out.append(f"  {tier}:")
            for r in tier_rows:
                out.append(f"    {_TIER_GLYPH[tier]} {r['label']}")
                if tier != "native":
                    if r["reason"]:
                        out.append(f"        reason: {r['reason']}")
                    if r["remediation"]:
                        out.append(f"        fix:    {r['remediation']}")
        out.append("")

    out.append("Verdict (lower is more portable):")
    for target in targets:
        counts = report["summary"].get(target)
        if not counts:
            continue
        unsup, deg = counts.get("unsupported", 0), counts.get("degraded", 0)
        if unsup == 0 and deg == 0:
            verdict = "drops in cleanly"
        elif unsup == 0:
            verdict = f"{deg} feature(s) degrade, none lost"
        else:
            verdict = f"{unsup} unsupported, {deg} degraded"
        out.append(f"  {_TARGET_TITLE.get(target, target)}: {verdict}")
    return "\n".join(out)
