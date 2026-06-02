"""Parse a local agent project into a `Project`.

Supported layouts (auto-detected, in priority order):

1. `.managed-agents/`     — the deploy folder. Everything inside it is a deploy
   target *by virtue of being there*. A dedicated name so managed deploy targets
   never get confused with Claude's local agents/subagents (which live in
   `.claude/agents/` and are intentionally NOT scanned).
       .managed-agents/
         shared/skills/<skill>/SKILL.md      # skills shared across agents
         shared/mcp.json                     # MCP servers shared across agents
         <agent>/agent.md                    # YAML frontmatter + system prompt
         <agent>/skills/<skill>/SKILL.md
         <agent>/mcp.json
         <agent>/knowledge/*.md

2. a single agent directory passed directly (must contain agent.md or CLAUDE.md).
   Use this to deploy exactly one agent — including an existing Claude Code
   embedded-agent folder (`.claude/agents/<name>/`): point skylift straight at it.

`.claude/agents/` is deliberately never auto-scanned: that folder holds local
subagents (single `.md` files) and local embedded agents, which are not deploy
targets. Keep what you want in the cloud under `.managed-agents/`.

Everything here is pure file IO — no network.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

import yaml

from .diagnostics import Diagnostics
from .model import (
    BUILTIN_TOOL_MAP,
    AgentSpec,
    McpServerSpec,
    Project,
    SkillSpec,
)

DEFAULT_MODEL = "claude-haiku-4-5"
_SKILL_SUBDIRS = ["skills", os.path.join(".claude", "skills")]
_MCP_FILENAMES = ["mcp.json", ".mcp.json"]


# --------------------------------------------------------------------------- #
# frontmatter
# --------------------------------------------------------------------------- #
def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Frontmatter is a leading YAML block
    delimited by `---` lines. Missing/!invalid frontmatter -> ({}, full text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    # find the closing '---' after line 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:]).lstrip("\n")
            try:
                fm = yaml.safe_load(raw) or {}
                if not isinstance(fm, dict):
                    fm = {}
            except yaml.YAMLError:
                fm = {}
            return fm, body
    return {}, text


# --------------------------------------------------------------------------- #
# skills
# --------------------------------------------------------------------------- #
def hash_skill_dir(skill_dir: str) -> tuple[list[tuple[str, str]], str]:
    """Collect all files under a skill dir as (arcname, abs_path), where arcname
    keeps the '<skill-name>/...' prefix the upload API expects. Returns a stable
    content hash over (arcname, bytes) so identical skills dedupe to one upload."""
    skill_name = os.path.basename(os.path.normpath(skill_dir))
    files: list[tuple[str, str]] = []
    for dirpath, _dirs, filenames in os.walk(skill_dir):
        for fn in sorted(filenames):
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, skill_dir).replace(os.sep, "/")
            arcname = f"{skill_name}/{rel}"
            files.append((arcname, abs_path))
    files.sort(key=lambda t: t[0])
    h = hashlib.sha256()
    for arcname, abs_path in files:
        h.update(arcname.encode("utf-8"))
        h.update(b"\0")
        with open(abs_path, "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return files, h.hexdigest()


def discover_skills(base_dir: str, shared: bool) -> dict[str, SkillSpec]:
    """Find every skill (a dir containing SKILL.md) directly under base_dir's
    skill subdirectories. Returns {skill_name: SkillSpec}."""
    found: dict[str, SkillSpec] = {}
    for sub in _SKILL_SUBDIRS:
        skills_root = os.path.join(base_dir, sub)
        if not os.path.isdir(skills_root):
            continue
        for entry in sorted(os.listdir(skills_root)):
            sdir = os.path.join(skills_root, entry)
            if not os.path.isdir(sdir):
                continue
            skill_md = os.path.join(sdir, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            files, chash = hash_skill_dir(sdir)
            fm, _ = split_frontmatter(open(skill_md, "r", encoding="utf-8", errors="replace").read())
            found[entry] = SkillSpec(
                name=entry, source_dir=sdir, files=files,
                content_hash=chash, description=fm.get("description"), shared=shared,
            )
    return found


# --------------------------------------------------------------------------- #
# mcp
# --------------------------------------------------------------------------- #
def parse_mcp_file(path: str, shared: bool) -> dict[str, McpServerSpec]:
    servers: dict[str, McpServerSpec] = {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    raw = data.get("mcpServers") or data.get("mcp_servers") or {}
    for name, cfg in raw.items():
        url = cfg.get("url")
        command = cfg.get("command")
        transport = cfg.get("type") or ("url" if url else "stdio")
        if transport not in ("url", "stdio"):
            transport = "url" if url else "stdio"
        raw_allowed = cfg.get("allowedTools") or cfg.get("allowed_tools")
        allowed = None
        policies: dict[str, str] = {}
        if raw_allowed is not None:
            allowed = []
            for t in raw_allowed:
                tname, policy = _split_policy(str(t))
                allowed.append(tname)
                if policy:
                    policies[tname] = policy
        has_auth = bool(cfg.get("env") or cfg.get("headers"))
        servers[name] = McpServerSpec(
            name=name, transport=transport, url=url, command=command,
            args=list(cfg.get("args") or []), allowed_tools=allowed,
            tool_policies=policies, shared=shared, has_inline_auth=has_auth,
        )
    return servers


def discover_mcp(base_dir: str, shared: bool) -> dict[str, McpServerSpec]:
    for fn in _MCP_FILENAMES:
        path = os.path.join(base_dir, fn)
        if os.path.isfile(path):
            return parse_mcp_file(path, shared)
    return {}


# --------------------------------------------------------------------------- #
# knowledge
# --------------------------------------------------------------------------- #
def discover_knowledge(agent_dir: str) -> list[tuple[str, str]]:
    kdir = os.path.join(agent_dir, "knowledge")
    out: list[tuple[str, str]] = []
    if not os.path.isdir(kdir):
        return out
    for dirpath, _dirs, filenames in os.walk(kdir):
        for fn in sorted(filenames):
            if fn.lower().endswith((".md", ".txt", ".json", ".csv")):
                abs_path = os.path.join(dirpath, fn)
                rel = os.path.relpath(abs_path, agent_dir).replace(os.sep, "/")
                out.append((rel, abs_path))
    out.sort(key=lambda t: t[0])
    return out


# --------------------------------------------------------------------------- #
# tool resolution
# --------------------------------------------------------------------------- #
def _split_policy(token: str) -> tuple[str, Optional[str]]:
    """Split a 'name' / 'name:ask' / 'name:allow' token into (name, policy|None)."""
    name = token.strip()
    policy = None
    if ":" in name:
        name, suffix = name.rsplit(":", 1)
        suffix = suffix.strip().lower()
        if suffix in ("ask", "allow"):
            policy = suffix
        else:
            name = token.strip()  # not a policy suffix; leave the name intact
    return name.strip(), policy


def resolve_builtin_tools(
    fm_tools: Optional[list], agent_name: str, diags: Diagnostics
) -> tuple[Optional[list[str]], dict[str, str]]:
    """Map a frontmatter `tools:` list of local names to managed builtins, parsing
    optional ':ask' / ':allow' permission suffixes. Returns (names|None, policies).
    None names means "enable all builtins" (no `tools:` key)."""
    if fm_tools is None:
        return None, {}
    managed: list[str] = []
    policies: dict[str, str] = {}
    for t in fm_tools:
        raw, policy = _split_policy(str(t))
        key = raw.lower()
        if key.startswith("mcp__"):
            continue  # MCP tools are handled via mcp config, not the builtin toolset
        mapped = BUILTIN_TOOL_MAP.get(key)
        if mapped is None:
            diags.warning(
                "tools.unmapped",
                f"tool '{raw}' has no Managed Agents built-in equivalent; dropped",
                agent_name,
            )
            continue
        if mapped not in managed:
            managed.append(mapped)
        if policy:
            policies[mapped] = policy
    return managed, policies


# --------------------------------------------------------------------------- #
# resource references (shared/x vs local x)
# --------------------------------------------------------------------------- #
def _resolve_refs(
    refs: Optional[list],
    local: dict,
    shared: dict,
    kind: str,
    agent_name: str,
    diags: Diagnostics,
) -> list:
    """Resolve an explicit frontmatter ref list (entries 'name' or 'shared/name')
    against local + shared pools. If refs is None, default to ALL local entries."""
    if refs is None:
        return list(local.values())
    out = []
    for ref in refs:
        ref = str(ref).strip()
        if ref.startswith("shared/"):
            name = ref[len("shared/"):]
            item = shared.get(name)
            if item is None:
                diags.error(f"{kind}.missing", f"shared {kind} '{name}' not found", agent_name)
                continue
        else:
            item = local.get(ref) or shared.get(ref)
            if item is None:
                diags.error(f"{kind}.missing", f"{kind} '{ref}' not found", agent_name)
                continue
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# agent loading
# --------------------------------------------------------------------------- #
def _read_agent_file(agent_dir: str) -> Optional[str]:
    for fn in ("agent.md", "CLAUDE.md"):
        path = os.path.join(agent_dir, fn)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
    return None


def load_agent(
    agent_dir: str,
    shared_skills: dict[str, SkillSpec],
    shared_mcp: dict[str, McpServerSpec],
    default_model: str,
    diags: Diagnostics,
) -> Optional[AgentSpec]:
    text = _read_agent_file(agent_dir)
    if text is None:
        return None
    fm, body = split_frontmatter(text)
    name = str(fm.get("name") or os.path.basename(os.path.normpath(agent_dir)))

    local_skills = discover_skills(agent_dir, shared=False)
    local_mcp = discover_mcp(agent_dir, shared=False)

    skills = _resolve_refs(fm.get("skills"), local_skills, shared_skills, "skill", name, diags)
    mcp = _resolve_refs(fm.get("mcp"), local_mcp, shared_mcp, "mcp", name, diags)

    subagents = [str(s).strip() for s in (fm.get("subagents") or [])]

    knowledge_mode = str(fm.get("knowledge") or "inline").lower()
    knowledge = discover_knowledge(agent_dir) if knowledge_mode != "skip" else []

    builtin_tools, builtin_tool_policies = resolve_builtin_tools(fm.get("tools"), name, diags)

    return AgentSpec(
        name=name,
        system=body.strip(),
        model=str(fm.get("model") or default_model),
        description=fm.get("description"),
        builtin_tools=builtin_tools,
        builtin_tool_policies=builtin_tool_policies,
        skills=skills,
        mcp_servers=mcp,
        subagents=subagents,
        knowledge_files=knowledge,
        knowledge_mode=knowledge_mode,
        source_dir=agent_dir,
    )


# --------------------------------------------------------------------------- #
# project detection
# --------------------------------------------------------------------------- #
def parse_project(
    path: str,
    default_model: str = DEFAULT_MODEL,
    diags: Optional[Diagnostics] = None,
) -> tuple[Project, Diagnostics]:
    diags = diags or Diagnostics()
    path = os.path.abspath(path)

    managed_agents = os.path.join(path, ".managed-agents")

    if os.path.isdir(managed_agents):
        return _parse_multi(path, managed_agents, ".managed-agents", default_model, diags)

    # single agent dir? (e.g. point straight at a .claude/agents/<name>/ folder)
    if _read_agent_file(path) is not None:
        agent = load_agent(path, {}, {}, default_model, diags)
        agents = [agent] if agent else []
        return Project(root=path, agents=agents, layout="single"), diags

    diags.error(
        "project.not_found",
        f"no agent project at {path} (expected a .managed-agents/ folder, "
        f"or an agent.md/CLAUDE.md directly here)",
    )
    return Project(root=path, agents=[], layout="single"), diags


def _parse_multi(
    root: str, agents_root: str, layout: str, default_model: str,
    diags: Diagnostics,
) -> tuple[Project, Diagnostics]:
    shared_skills: dict[str, SkillSpec] = {}
    shared_mcp: dict[str, McpServerSpec] = {}
    shared_dir = os.path.join(agents_root, "shared")
    if os.path.isdir(shared_dir):
        shared_skills = discover_skills(shared_dir, shared=True)
        shared_mcp = discover_mcp(shared_dir, shared=True)

    agents: list[AgentSpec] = []
    for entry in sorted(os.listdir(agents_root)):
        if entry == "shared":
            continue
        adir = os.path.join(agents_root, entry)
        if not os.path.isdir(adir):
            continue
        agent = load_agent(adir, shared_skills, shared_mcp, default_model, diags)
        if agent is not None:
            agents.append(agent)

    if not agents:
        diags.error("project.empty", f"no agents found under {agents_root}")
    return Project(root=root, agents=agents, layout=layout), diags
