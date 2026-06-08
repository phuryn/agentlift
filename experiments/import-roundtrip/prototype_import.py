"""Prototype: Anthropic Managed Agent  ->  .managed-agents/ folder (the INVERSE of the planner).

This is a *spike*, not shipped code. It exists to de-risk the import feature by
proving the hard part offline: that a provider's read-API wire shape can be
mapped back into agentlift's neutral folder and survive a clean round-trip
through the real `parser.build_project` + `planner.build_plan`.

It deliberately mirrors `planner._build_tools` / `_build_agent_create` in reverse,
so the two stay legible side by side. No network: callers pass dicts shaped like
`anthropic.types.beta.BetaManagedAgentsAgent.model_dump()` and skill bundles as
in-memory file maps (what `skills.versions.download` would yield, unzipped).

Run:  python experiments/import-roundtrip/prototype_import.py
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import yaml

# Inverse of planner._POLICY_TYPE
_POLICY_FROM_TYPE = {"always_ask": "ask", "always_allow": "allow"}

# Inverse of model.BUILTIN_TOOL_MAP for display (managed name -> local token).
# Managed names are already valid local tokens, so identity is fine; we keep the
# table to document the contract and to normalise web_* spellings.
_LOCAL_TOOL = {
    "read": "read", "glob": "glob", "grep": "grep", "bash": "bash",
    "edit": "edit", "write": "write",
    "web_fetch": "web_fetch", "web_search": "web_search",
}


def _tool_token(cfg: dict[str, Any]) -> str:
    """One toolset `config` entry -> a `tools:` list token (inverts _tool_config)."""
    name = _LOCAL_TOOL.get(cfg["name"], cfg["name"])
    policy = (cfg.get("permission_policy") or {}).get("type")
    suffix = _POLICY_FROM_TYPE.get(policy)
    return f"{name}:{suffix}" if suffix else name


def _decode_tools(tools: list[dict[str, Any]]) -> tuple[Optional[list[str]], dict[str, dict]]:
    """Split a retrieved `tools` array into (frontmatter `tools:` list, mcp tool-filters).

    Returns:
      builtin_tokens: list for the `tools:` frontmatter key, or None to mean
                      "all builtins" (when the toolset default_config is enabled).
      mcp_filters:    {server_name: {"allowed": [tokens] | None}} carrying the
                      per-server allowlist + policy suffixes.
    """
    builtin_tokens: Optional[list[str]] = None
    mcp_filters: dict[str, dict] = {}
    for t in tools:
        ttype = t.get("type")
        if ttype == "agent_toolset_20260401":
            default_on = (t.get("default_config") or {}).get("enabled", True)
            if default_on and not t.get("configs"):
                builtin_tokens = None  # "all builtins"
            else:
                builtin_tokens = [_tool_token(c) for c in (t.get("configs") or [])]
        elif ttype == "mcp_toolset":
            srv = t["mcp_server_name"]
            default_on = (t.get("default_config") or {}).get("enabled", True)
            if default_on and not t.get("configs"):
                mcp_filters[srv] = {"allowed": None}   # all tools
            else:
                mcp_filters[srv] = {"allowed": [_tool_token(c) for c in (t.get("configs") or [])]}
        # custom_tool -> not representable in the folder; caller surfaces a diagnostic
    return builtin_tokens, mcp_filters


def import_agent(
    agent: dict[str, Any],
    *,
    skill_bundles: dict[str, dict[str, bytes]],   # skill_id -> {relpath: bytes}
    skill_names: dict[str, str],                  # skill_id -> directory name
    out_root: str,
    diagnostics: list[str],
) -> None:
    """Write one retrieved agent into `<out_root>/.managed-agents/<name>/`.

    `agent` is shaped like BetaManagedAgentsAgent.model_dump(): keys name, system,
    description, model{model}, tools[], mcp_servers[], skills[], multiagent{agents[]}.
    """
    name = agent["name"]
    adir = os.path.join(out_root, ".managed-agents", name)
    os.makedirs(adir, exist_ok=True)

    builtin_tokens, mcp_filters = _decode_tools(agent.get("tools") or [])

    # --- frontmatter (inverse of _build_agent_create.request) ---
    fm: dict[str, Any] = {"name": name}
    if agent.get("description"):
        fm["description"] = agent["description"]
    model = agent.get("model")
    if isinstance(model, dict):
        model = model.get("model")
    if model:
        fm["model"] = model
    if builtin_tokens is not None:
        fm["tools"] = builtin_tokens

    # --- skills: download bundle to skills/<dir>/, list names in frontmatter ---
    skill_dir_names: list[str] = []
    for sref in agent.get("skills") or []:
        sid = sref.get("skill_id") or sref.get("id")
        if sref.get("type") == "anthropic":
            diagnostics.append(
                f"{name}: skill {sid} is an Anthropic-managed skill (not custom); "
                f"referenced by id, content not re-imported"
            )
            continue
        dirname = skill_names.get(sid, sid)
        bundle = skill_bundles.get(sid)
        if not bundle:
            diagnostics.append(f"{name}: custom skill {sid} has no downloadable bundle")
            continue
        # Bundle keys carry the skill's own `<name>/...` prefix (the planner's
        # arcname convention), so they unpack directly under `skills/`.
        sk_root = os.path.join(adir, "skills")
        for rel, data in bundle.items():
            dest = os.path.join(sk_root, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
        skill_dir_names.append(dirname)
    if skill_dir_names:
        fm["skills"] = skill_dir_names

    # --- mcp servers: write mcp.json (url transport + tool filter) ---
    servers = {}
    for srv in agent.get("mcp_servers") or []:
        sname = srv["name"]
        entry: dict[str, Any] = {"type": "url", "url": srv.get("url")}
        flt = mcp_filters.get(sname, {}).get("allowed")
        if flt is not None:
            entry["allowedTools"] = flt
        servers[sname] = entry
    # mcp servers that only showed up as a tool filter (no URL def) -> diagnostic
    for sname in mcp_filters:
        if sname not in servers:
            diagnostics.append(f"{name}: mcp_toolset '{sname}' has no server URL definition")
    if servers:
        if [s for s in servers if mcp_filters.get(s, {}).get("allowed") is not None] or servers:
            fm["mcp"] = list(servers.keys())
        with open(os.path.join(adir, "mcp.json"), "w") as fh:
            json.dump({"mcpServers": servers}, fh, indent=2)

    # --- subagents (inverse of multiagent.agents references) ---
    multi = agent.get("multiagent")
    if multi and multi.get("agents"):
        # references carry agent ids; the importer resolves ids -> names via the roster
        fm["subagents"] = [a["name"] if isinstance(a, dict) and "name" in a else a
                           for a in multi["agents"]]

    # --- write agent.md ---
    front = yaml.safe_dump(fm, sort_keys=False).strip()
    body = (agent.get("system") or "").strip()
    with open(os.path.join(adir, "agent.md"), "w") as fh:
        fh.write(f"---\n{front}\n---\n{body}\n")
