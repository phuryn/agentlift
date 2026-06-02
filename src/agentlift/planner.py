"""Turn a parsed `Project` into a deterministic `DeployPlan`.

A plan is a pure function of the project on disk: same folder in, same plan out,
no network. That is what makes `agentlift plan` a safe dry-run, makes the whole
translation unit-testable, and makes deploys reproducible.

The plan carries *symbolic* references:
  - skills are referenced as `@skill:<hash8>` (identical skills dedupe to one upload)
  - roster agents are referenced as `@agent:<name>`
The apply step (anthropic_target.py) resolves these to real `skill_...` / `agent_...`
IDs at deploy time.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .diagnostics import Diagnostics
from .model import AgentSpec, Project, SkillSpec

SYSTEM_PROMPT_LIMIT = 100_000   # API max for `system`
_XML_TAG = re.compile(r"<[^>\s][^>]*>")  # rough "looks like an XML tag" detector
MAX_SKILLS = 20
MAX_MCP_SERVERS = 20
MAX_TOOLS = 128


def skill_ref(skill: SkillSpec) -> str:
    return f"@skill:{skill.content_hash[:8]}"


def agent_ref(name: str) -> str:
    return f"@agent:{name}"


@dataclass
class SkillUpload:
    ref: str                       # "@skill:<hash8>"
    content_hash: str
    display_title: str
    source_dir: str
    files: list[tuple[str, str]]   # (arcname, abs_path)
    used_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "content_hash": self.content_hash,
            "display_title": self.display_title,
            "files": [
                {"arcname": a, "bytes": os.path.getsize(p) if os.path.exists(p) else None}
                for a, p in self.files
            ],
            "used_by": self.used_by,
        }


@dataclass
class AgentCreate:
    ref: str                       # "@agent:<name>"
    name: str
    request: dict[str, Any]        # kwargs for agents.create, with symbolic skill/roster refs
    is_coordinator: bool
    depends_on: list[str]          # skill refs + roster agent refs

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "name": self.name,
            "is_coordinator": self.is_coordinator,
            "depends_on": self.depends_on,
            "request": self.request,
        }


@dataclass
class DeployPlan:
    skill_uploads: list[SkillUpload]
    agent_creates: list[AgentCreate]
    diagnostics: Diagnostics

    @property
    def deployable(self) -> bool:
        return self.diagnostics.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_uploads": [s.to_dict() for s in self.skill_uploads],
            "agent_creates": [a.to_dict() for a in self.agent_creates],
            "diagnostics": [d.__dict__ for d in self.diagnostics.items],
            "deployable": self.deployable,
        }


# --------------------------------------------------------------------------- #
def _inline_knowledge(agent: AgentSpec, diags: Diagnostics) -> str:
    """Fold knowledge/*.md into the system prompt (managed agents have no
    persistent local FS). Deterministic, size-guarded."""
    system = agent.system
    if not agent.knowledge_files or agent.knowledge_mode == "skip":
        return system
    parts = [system, "\n\n# Reference material (bundled from knowledge/)\n"]
    budget = SYSTEM_PROMPT_LIMIT - len(system) - 200
    added = 0
    for rel, abs in agent.knowledge_files:
        try:
            content = open(abs, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        block = f"\n## {rel}\n\n```\n{content}\n```\n"
        if len(block) > budget:
            diags.warning(
                "knowledge.truncated",
                f"knowledge files exceed the system-prompt budget; "
                f"{len(agent.knowledge_files) - added} file(s) not inlined "
                f"(use a skill bundle for large reference sets)",
                agent.name,
            )
            break
        parts.append(block)
        budget -= len(block)
        added += 1
    if added:
        diags.info("knowledge.inlined", f"inlined {added} knowledge file(s) into the system prompt", agent.name)
    return "".join(parts)


_POLICY_TYPE = {"ask": "always_ask", "allow": "always_allow"}


def _tool_config(name: str, policy: Optional[str]) -> dict:
    cfg = {"name": name, "enabled": True}
    if policy in _POLICY_TYPE:
        cfg["permission_policy"] = {"type": _POLICY_TYPE[policy]}
    return cfg


def _build_tools(agent: AgentSpec, deployable_mcp, diags: Diagnostics) -> list[dict]:
    """Built-in toolset (with allowlist + per-tool permission) + one mcp_toolset
    per deployable server (with its specific-tool allowlist + per-tool permission)."""
    tools: list[dict] = []

    # built-in toolset
    if agent.builtin_tools is None:
        tools.append({"type": "agent_toolset_20260401", "default_config": {"enabled": True}})
    else:
        configs = [_tool_config(t, agent.builtin_tool_policies.get(t)) for t in agent.builtin_tools]
        tools.append({
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False},
            "configs": configs,
        })

    # one mcp_toolset per deployable server, carrying the specific-tool allowlist
    for srv in deployable_mcp:
        if srv.allowed_tools is None:
            tools.append({
                "type": "mcp_toolset",
                "mcp_server_name": srv.name,
                "default_config": {"enabled": True},
            })
        else:
            tools.append({
                "type": "mcp_toolset",
                "mcp_server_name": srv.name,
                "default_config": {"enabled": False},
                "configs": [_tool_config(t, srv.tool_policies.get(t)) for t in srv.allowed_tools],
            })

    if len(tools) > MAX_TOOLS:
        diags.error("tools.too_many", f"{len(tools)} tool configs exceed the limit of {MAX_TOOLS}", agent.name)
    return tools


def _build_agent_create(
    agent: AgentSpec,
    skill_ref_by_hash: dict[str, str],
    project: Project,
    skip_unsupported: bool,
    diags: Diagnostics,
) -> AgentCreate:
    depends: list[str] = []

    # --- MCP: split deployable (url) from unsupported (stdio) ---
    deployable_mcp = []
    for srv in agent.mcp_servers:
        if srv.transport != "url":
            msg = (
                f"MCP server '{srv.name}' is stdio (command: {srv.command or '?'}); "
                f"Managed Agents only accept remote URL MCP servers. "
                f"Host it behind an HTTPS endpoint and set its 'url'."
            )
            if skip_unsupported:
                diags.warning("mcp.stdio_skipped", msg + " (skipped)", agent.name)
            else:
                diags.error("mcp.stdio_unsupported", msg, agent.name)
            continue
        if srv.has_inline_auth:
            diags.warning(
                "mcp.auth_dropped",
                f"MCP server '{srv.name}' declares inline auth (env/headers); "
                f"Managed URL MCP servers carry no inline credentials. The server "
                f"must be public or authenticate itself. (auth not forwarded)",
                agent.name,
            )
        deployable_mcp.append(srv)

    if len(deployable_mcp) > MAX_MCP_SERVERS:
        diags.error("mcp.too_many", f"{len(deployable_mcp)} MCP servers exceed the limit of {MAX_MCP_SERVERS}", agent.name)

    # --- skills ---
    if len(agent.skills) > MAX_SKILLS:
        diags.error("skills.too_many", f"{len(agent.skills)} skills exceed the limit of {MAX_SKILLS}", agent.name)
    skill_refs = []
    for sk in agent.skills:
        ref = skill_ref(sk)
        skill_refs.append({"type": "custom", "skill_ref": ref})
        depends.append(ref)

    # --- multiagent / subagents ---
    multiagent = None
    is_coordinator = False
    if agent.subagents:
        is_coordinator = True
        roster_refs = []
        for sub in agent.subagents:
            target = project.agent(sub)
            if target is None:
                diags.error("subagent.missing", f"subagent '{sub}' not found in project", agent.name)
                continue
            if target.subagents:
                diags.error(
                    "subagent.depth",
                    f"subagent '{sub}' is itself a coordinator; Managed Agents allow a "
                    f"coordinator depth of 1 (roster agents cannot have their own roster)",
                    agent.name,
                )
                continue
            ref = agent_ref(sub)
            roster_refs.append(ref)
            depends.append(ref)
        multiagent = {"type": "coordinator", "agents": roster_refs}

    # --- system prompt (+ inlined knowledge) ---
    system = _inline_knowledge(agent, diags)
    if len(system) > SYSTEM_PROMPT_LIMIT:
        diags.error("system.too_long", f"system prompt is {len(system)} chars (limit {SYSTEM_PROMPT_LIMIT})", agent.name)

    request: dict[str, Any] = {
        "name": agent.name,
        "model": agent.model,
        "system": system,
        "tools": _build_tools(agent, deployable_mcp, diags),
    }
    if agent.description:
        request["description"] = agent.description
    if skill_refs:
        request["skills"] = skill_refs
    if deployable_mcp:
        request["mcp_servers"] = [
            {"type": "url", "name": s.name, "url": s.url} for s in deployable_mcp
        ]
    if multiagent is not None:
        request["multiagent"] = multiagent

    return AgentCreate(
        ref=agent_ref(agent.name),
        name=agent.name,
        request=request,
        is_coordinator=is_coordinator,
        depends_on=depends,
    )


def build_plan(
    project: Project,
    diags: Optional[Diagnostics] = None,
    skip_unsupported: bool = False,
) -> DeployPlan:
    diags = diags or Diagnostics()

    # 1) collect + dedupe skills by content hash across ALL agents
    uploads: dict[str, SkillUpload] = {}  # hash8 -> upload
    for agent in project.agents:
        for sk in agent.skills:
            ref = skill_ref(sk)
            up = uploads.get(sk.content_hash[:8])
            if up is None:
                # pre-flight: the API rejects XML-like tags in a skill description
                if sk.description and _XML_TAG.search(sk.description):
                    diags.error(
                        "skill.xml_in_description",
                        f"skill '{sk.name}' has angle-bracket tags in its SKILL.md "
                        f"description (the API rejects them) — rephrase without <...>",
                        agent.name,
                    )
                uploads[sk.content_hash[:8]] = SkillUpload(
                    ref=ref, content_hash=sk.content_hash,
                    display_title=sk.display_title, source_dir=sk.source_dir,
                    files=sk.files, used_by=[agent.name],
                )
            else:
                if agent.name not in up.used_by:
                    up.used_by.append(agent.name)
    skill_ref_by_hash = {h: u.ref for h, u in uploads.items()}

    # 2) build agent-create ops
    creates = [
        _build_agent_create(a, skill_ref_by_hash, project, skip_unsupported, diags)
        for a in project.agents
    ]

    # 3) topologically order: roster (non-coordinator) agents before coordinators
    ordered = [c for c in creates if not c.is_coordinator] + [c for c in creates if c.is_coordinator]

    skill_uploads = sorted(uploads.values(), key=lambda u: u.ref)
    return DeployPlan(skill_uploads=skill_uploads, agent_creates=ordered, diagnostics=diags)
