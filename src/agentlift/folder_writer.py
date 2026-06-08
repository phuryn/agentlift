"""Materialise an `ImportedProject` to a `.managed-agents/` folder (inverse of the parser).

The output is the canonical folder convention `parser.parse_project` reads — so an
import followed by a parse + plan reproduces a deployable project. Pure file IO: it
writes bytes, it does not touch the network.

Layout written:

    <out>/.managed-agents/
      shared/skills/<name>/...        # skills used identically by >1 agent
      shared/mcp.json                 # MCP servers used identically by >1 agent
      <agent>/agent.md                # frontmatter + system prompt
      <agent>/skills/<name>/...       # private skills
      <agent>/mcp.json                # private MCP servers
"""
from __future__ import annotations

import json
import os
from typing import Any

import yaml

from .import_model import ImportedAgent, ImportedMcp, ImportedProject, ImportedSkill


def _write_skill(skill_root: str, skill: ImportedSkill) -> None:
    """Unpack a skill bundle. Arcnames already carry the '<name>/...' prefix, so they
    land directly under the skills root (matching the upload/parse convention)."""
    for arcname, data in skill.files.items():
        dest = os.path.join(skill_root, arcname.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)


def _mcp_json(servers: list[ImportedMcp]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for s in servers:
        entry: dict[str, Any] = {"type": s.transport}
        if s.url:
            entry["url"] = s.url
        if s.allowed_tools is not None:
            entry["allowedTools"] = [
                f"{t}:{s.tool_policies[t]}" if t in s.tool_policies else t
                for t in s.allowed_tools
            ]
        out[s.name] = entry
    return {"mcpServers": out}


def _frontmatter(agent: ImportedAgent) -> dict[str, Any]:
    """Build the agent.md frontmatter dict in a stable, readable key order."""
    fm: dict[str, Any] = {"name": agent.name}
    if agent.description:
        fm["description"] = agent.description
    if agent.model:
        fm["model"] = agent.model
    if agent.builtin_tools is not None:
        # re-attach :ask/:allow suffixes the planner would emit
        fm["tools"] = [
            f"{t}:{agent.builtin_tool_policies[t]}" if t in agent.builtin_tool_policies else t
            for t in agent.builtin_tools
        ]
    if agent.skill_refs:
        fm["skills"] = list(agent.skill_refs)
    if agent.mcp_refs:
        fm["mcp"] = list(agent.mcp_refs)
    if agent.subagents:
        fm["subagents"] = list(agent.subagents)
    return fm


def write_project(project: ImportedProject, out_dir: str) -> str:
    """Write the whole imported project under `<out_dir>/.managed-agents/`.

    Returns the path of the `.managed-agents/` root that was written.
    """
    root = os.path.join(out_dir, ".managed-agents")
    os.makedirs(root, exist_ok=True)

    # --- shared pool ---
    shared_dir = os.path.join(root, "shared")
    for skill in project.shared_skills:
        _write_skill(os.path.join(shared_dir, "skills"), skill)
    if project.shared_mcp:
        os.makedirs(shared_dir, exist_ok=True)
        with open(os.path.join(shared_dir, "mcp.json"), "w", encoding="utf-8") as fh:
            json.dump(_mcp_json(project.shared_mcp), fh, indent=2)
            fh.write("\n")

    # --- one directory per agent ---
    for agent in project.agents:
        adir = os.path.join(root, agent.name)
        os.makedirs(adir, exist_ok=True)
        for skill in agent.local_skills:
            _write_skill(os.path.join(adir, "skills"), skill)
        if agent.local_mcp:
            with open(os.path.join(adir, "mcp.json"), "w", encoding="utf-8") as fh:
                json.dump(_mcp_json(agent.local_mcp), fh, indent=2)
                fh.write("\n")
        front = yaml.safe_dump(_frontmatter(agent), sort_keys=False, allow_unicode=True).strip()
        body = (agent.system or "").strip()
        with open(os.path.join(adir, "agent.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\n{front}\n---\n{body}\n")

    return root
