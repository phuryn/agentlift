"""Turn a parsed ``Project`` into a deterministic ``GoogleDeployPlan``.

Mirrors ``planner.py`` for the Google Vertex AI Agent Engine target, but the
shape is different: an Agent Engine deploy is ONE ``reasoningEngine`` holding a
root ADK ``LlmAgent`` plus its ``sub_agents`` (server-side delegation). So the
plan describes a single engine with N agent nodes, the skill bundles shipped
into its source package, and the MCP toolset recipes each agent wires up.

Like the Anthropic planner this is a *pure* function of the folder (plus the
chosen deploy model): same inputs in, same plan out, no network, no clock. That
is what makes ``agentlift plan --target google`` a safe dry-run and the whole
Google translation unit-testable. The plan is the contract:

  - ``google_codegen.py`` renders the plan into a source package (agent.py +
    requirements + the skill bundles) the engine imports remotely.
  - ``google_target.py`` ships that package via ``agent_engines.create/update``.

Secrets never enter the plan. A server with inline auth contributes only the
*names* of the engine env vars its headers will read at runtime; the values are
resolved from the deployer's local environment at deploy time and handed to
Agent Engine as ``env_vars`` (never inlined into source, never hashed, never
written to the lockfile).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .diagnostics import Diagnostics
from .lockfile import canonical_hash
from .model import AgentSpec, McpServerSpec, Project

# Agent Engine accepts the same large prompts as the API; keep a generous guard
# so inlined knowledge can't produce an absurd instruction string.
INSTRUCTION_LIMIT = 100_000
MAX_SUB_AGENTS = 50

# Built-in (sandbox) tools the folder may enable. Agent Engine's hosted sandbox
# is Python/JS only, so these do not map to native tools today -- we surface that
# rather than silently dropping them. (Same gap the audit reports as "degraded".)
_SANDBOX_TOOLS = {"bash", "edit", "write", "glob", "grep", "read"}

# The two web built-ins DO map to Agent Engine: web_search -> Gemini's Google
# Search grounding, web_fetch -> URL Context. Each is lowered as a dedicated
# single-tool ADK sub-agent exposed to the owning agent via an AgentTool (the
# pattern ADK itself uses in create_google_search_agent / create_url_context_agent),
# so a built-in web tool can coexist with MCP toolsets, skills and transfer tools
# on the same node regardless of topology.
_WEB_TOOLS = {"web_search", "web_fetch"}

DEFAULT_GOOGLE_MODEL = "gemini-2.5-flash"
ENGINE_REQUIREMENT = "google-cloud-aiplatform[adk,agent_engines]"
# Pin the ADK floor that ships the AgentTool(propagate_grounding_metadata=...),
# GoogleSearchTool and url_context APIs the web-tool lowering relies on. Only added
# to requirements when a node actually maps a web tool.
ADK_WEB_REQUIREMENT = "google-adk>=1.34.3"


def safe_ident(name: str) -> str:
    """A valid ADK agent identifier / Python symbol fragment.

    Shared with ``google_target`` and ``google_codegen`` so the name an agent is
    deployed under matches everywhere (it is also the ``transfer_to_agent``
    function name the runtime delegates through).
    """
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name)


def _auth_env_var(server: str, header: str) -> str:
    """Stable engine env-var name that will carry one MCP auth header's value."""
    return "AGENTLIFT_MCP_" + safe_ident(server).upper() + "_" + safe_ident(header).upper()


# A Gemini function-tool / ADK agent name must start with a letter or underscore,
# contain only [A-Za-z0-9_], and stay <= 63 chars. The wrapped web sub-agent's name
# becomes the transfer/function name the model sees, so it must satisfy that here.
_NAME_MAX = 63


def web_tool_agent_name(parent_safe_name: str, tool: str) -> str:
    """Deterministic, function-safe name for a web tool's wrapped sub-agent.

    ``tool`` is ``web_search`` / ``web_fetch``. The name is scoped by the owning
    agent so two agents that both map web_search get distinct tool names, and it is
    truncated with a short content hash if the parent name is long. Shared with
    ``google_codegen`` so the generated source and the plan agree on every name.
    """
    base = f"{parent_safe_name}_{tool}"
    if base and base[0].isdigit():
        base = "_" + base
    if len(base) <= _NAME_MAX:
        return base
    suffix = "_" + canonical_hash(base)[:8]
    return base[: _NAME_MAX - len(suffix)] + suffix


# --------------------------------------------------------------------------- #
# plan dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class McpRecipe:
    """One ADK ``McpToolset`` to attach to an agent (URL transport only)."""
    server: str
    url: str
    tool_filter: Optional[list[str]]          # allowed tool names; None = all tools
    auth_env_vars: dict[str, str] = field(default_factory=dict)  # header name -> engine env var name

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "url": self.url,
            "tool_filter": self.tool_filter,
            "auth_env_vars": self.auth_env_vars,
        }


@dataclass
class GoogleAgentNode:
    """One ADK ``LlmAgent`` in the deployed engine."""
    name: str
    safe_name: str
    folder_model: str                         # the model id from the folder (resolved at runtime)
    instruction: str
    description: str
    mcp: list[McpRecipe] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)      # skill bundle names this agent loads
    sub_agents: list[str] = field(default_factory=list)  # roster agent names (makes it a coordinator)
    builtin_web: list[str] = field(default_factory=list)  # web built-ins lowered to wrapped tool-agents

    @property
    def is_coordinator(self) -> bool:
        return bool(self.sub_agents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "safe_name": self.safe_name,
            "folder_model": self.folder_model,
            "instruction": self.instruction,
            "description": self.description,
            "mcp": [m.to_dict() for m in self.mcp],
            "skills": self.skills,
            "sub_agents": self.sub_agents,
            "builtin_web": self.builtin_web,
        }


@dataclass
class SkillBundle:
    """A skill directory shipped into the engine source package under skills/<name>/."""
    name: str
    content_hash: str
    files: list[tuple[str, str]]              # (arcname, abs_path); arcname keeps the "<name>/..." prefix
    used_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # abs paths are machine-specific and must NOT enter the spec hash; the
        # content_hash already captures the bytes. Record arcnames for visibility.
        return {
            "name": self.name,
            "content_hash": self.content_hash,
            "files": [a for a, _ in self.files],
            "used_by": self.used_by,
        }


@dataclass
class GoogleDeployPlan:
    display_name: str
    root_agent: str
    deploy_model: str
    agents: list[GoogleAgentNode]
    skill_bundles: list[SkillBundle]
    requirements: list[str]
    env_var_names: list[str]                  # engine env vars the deploy must populate (MCP auth)
    diagnostics: Diagnostics

    @property
    def deployable(self) -> bool:
        return self.diagnostics.ok

    def to_hashable(self) -> dict[str, Any]:
        """The content that determines the deployed artifact -- the basis for the
        idempotency spec hash. Excludes platform coordinates (project/location/
        bucket) and all secret values; those decide *where*/*with what creds*, not
        *what* is deployed."""
        return {
            "display_name": self.display_name,
            "root_agent": self.root_agent,
            "deploy_model": self.deploy_model,
            "agents": [a.to_dict() for a in self.agents],
            "skill_bundles": [b.to_dict() for b in self.skill_bundles],
            "requirements": self.requirements,
            "env_var_names": self.env_var_names,
        }

    @property
    def spec_hash(self) -> str:
        return canonical_hash(self.to_hashable())

    def to_dict(self) -> dict[str, Any]:
        d = self.to_hashable()
        d["spec_hash"] = self.spec_hash
        d["diagnostics"] = [diag.__dict__ for diag in self.diagnostics.items]
        d["deployable"] = self.deployable
        return d


# --------------------------------------------------------------------------- #
# knowledge (folded into the instruction; Agent Engine has no persistent FS the
# agent can read by default -- same treatment as the Anthropic planner)
# --------------------------------------------------------------------------- #
def _inline_knowledge(agent: AgentSpec, diags: Diagnostics) -> str:
    system = agent.system
    if not agent.knowledge_files or agent.knowledge_mode == "skip":
        return system
    parts = [system, "\n\n# Reference material (bundled from knowledge/)\n"]
    budget = INSTRUCTION_LIMIT - len(system) - 200
    added = 0
    for rel, abs_path in agent.knowledge_files:
        try:
            content = open(abs_path, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        block = f"\n## {rel}\n\n```\n{content}\n```\n"
        if len(block) > budget:
            diags.warning(
                "google.knowledge.truncated",
                f"knowledge files exceed the instruction budget; "
                f"{len(agent.knowledge_files) - added} file(s) not inlined",
                agent.name,
            )
            break
        parts.append(block)
        budget -= len(block)
        added += 1
    if added:
        diags.info("google.knowledge.inlined",
                   f"inlined {added} knowledge file(s) into the instruction", agent.name)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# MCP lowering
# --------------------------------------------------------------------------- #
def _build_mcp_recipes(
    agent: AgentSpec, skip_unsupported: bool, diags: Diagnostics,
) -> tuple[list[McpRecipe], set[str]]:
    """Lower an agent's MCP servers to ADK ``McpToolset`` recipes.

    Returns (recipes, env_var_names). stdio servers are unsupported (skipped or
    errored). URL servers map natively; inline auth headers are mapped to engine
    env vars by name -- values are resolved at deploy, never inlined here.
    """
    recipes: list[McpRecipe] = []
    env_vars: set[str] = set()
    for srv in agent.mcp_servers:
        if srv.transport != "url":
            msg = (
                f"MCP server '{srv.name}' is stdio (command: {srv.command or '?'}); "
                f"Agent Engine attaches only remote URL MCP servers. Host it behind "
                f"an HTTPS endpoint and set its 'url'."
            )
            if skip_unsupported:
                diags.warning("google.mcp.stdio_skipped", msg + " (skipped)", agent.name)
            else:
                diags.error("google.mcp.stdio_unsupported", msg, agent.name)
            continue

        auth_env: dict[str, str] = {}
        if srv.headers:
            for header in sorted(srv.headers):
                name = _auth_env_var(srv.name, header)
                auth_env[header] = name
                env_vars.add(name)
            diags.warning(
                "google.mcp.auth_via_env",
                f"MCP server '{srv.name}' declares inline auth header(s) "
                f"{', '.join(sorted(srv.headers))}; their values are read from your "
                f"local environment at deploy and stored as Agent Engine env var(s) "
                f"{', '.join(sorted(auth_env.values()))} (not inlined into the source).",
                agent.name,
            )
        elif srv.has_inline_auth:
            # inline `env` (stdio-style) on a URL server -- nothing to forward
            diags.warning(
                "google.mcp.auth_dropped",
                f"MCP server '{srv.name}' declares inline 'env' but no headers; "
                f"a URL MCP server authenticates via headers, so this is not forwarded.",
                agent.name,
            )

        # surface (do not silently drop) any :ask policy -- unenforceable on Agent Engine
        asks = [t for t, p in (srv.tool_policies or {}).items() if p == "ask"]
        if asks:
            diags.warning(
                "google.tool_approval.unsupported",
                f"MCP server '{srv.name}': per-tool approval (:ask) on "
                f"{', '.join(asks)} is not enforced on Agent Engine; the tool stays "
                f"available without a gate (keep :ask agents on the Anthropic target).",
                agent.name,
            )

        recipes.append(McpRecipe(
            server=srv.name,
            url=srv.url or "",
            tool_filter=list(srv.allowed_tools) if srv.allowed_tools is not None else None,
            auth_env_vars=auth_env,
        ))
    return recipes, env_vars


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def _builtin_web_tools(agent: AgentSpec) -> list[str]:
    """The web built-ins this agent enables, lowered to Agent Engine (sorted).

    ``builtin_tools is None`` means "all built-ins enabled", so both web tools map.
    """
    if agent.builtin_tools is None:
        return sorted(_WEB_TOOLS)
    return sorted(set(agent.builtin_tools) & _WEB_TOOLS)


def _flag_builtin_gap(agent: AgentSpec, diags: Diagnostics) -> None:
    """Surface the built-in story per agent: web tools MAP (info), the rest of the
    sandbox does NOT (warning), and :ask on any built-in is unenforceable (warning).
    Nothing is dropped silently."""
    if agent.builtin_tools is None:
        non_web = sorted(_SANDBOX_TOOLS)
    else:
        non_web = sorted(set(agent.builtin_tools) & _SANDBOX_TOOLS)
    web = _builtin_web_tools(agent)

    if web:
        diags.info(
            "google.builtin.web_mapped",
            f"built-in web tool(s) {', '.join(web)} map to Gemini grounding on Agent "
            f"Engine (web_search -> Google Search, web_fetch -> URL Context), each lowered "
            f"as a dedicated wrapped tool-agent so they coexist with MCP/skills/transfer.",
            agent.name,
        )
    if non_web:
        diags.warning(
            "google.builtin.degraded",
            f"built-in tool(s) {', '.join(non_web)} are not mapped to Agent Engine (its "
            f"hosted sandbox is Python/JS only -- no bash/files/glob/grep); supply "
            f"equivalents via an MCP server. The agent deploys without them.",
            agent.name,
        )
    asks = [t for t, p in (agent.builtin_tool_policies or {}).items() if p == "ask"]
    if asks:
        diags.warning(
            "google.tool_approval.unsupported",
            f"per-tool approval (:ask) on built-in {', '.join(asks)} is not enforced "
            f"on Agent Engine.",
            agent.name,
        )


def build_google_plan(
    project: Project,
    diags: Optional[Diagnostics] = None,
    *,
    model: str = DEFAULT_GOOGLE_MODEL,
    skip_unsupported: bool = False,
) -> GoogleDeployPlan:
    diags = diags or Diagnostics()

    if not project.agents:
        diags.error("google.project.empty", "no agents to deploy")
        return GoogleDeployPlan(
            display_name="agentlift", root_agent="", deploy_model=model,
            agents=[], skill_bundles=[], requirements=[ENGINE_REQUIREMENT],
            env_var_names=[], diagnostics=diags,
        )

    # which agents are roster vs coordinators (root)
    roster = [a for a in project.agents if not a.subagents]
    coords = [a for a in project.agents if a.subagents]
    root_spec = coords[0] if coords else project.agents[0]

    # 1) collect + dedupe skill bundles by name across all agents (the dir name is
    #    the load_skill_from_dir key). Identical names with different content are a
    #    conflict the engine can't represent (one dir, one content).
    bundles: dict[str, SkillBundle] = {}
    for agent in project.agents:
        for sk in agent.skills:
            existing = bundles.get(sk.name)
            if existing is None:
                bundles[sk.name] = SkillBundle(
                    name=sk.name, content_hash=sk.content_hash,
                    files=sk.files, used_by=[agent.name],
                )
            else:
                if existing.content_hash != sk.content_hash:
                    diags.error(
                        "google.skill.name_collision",
                        f"two different skills are both named '{sk.name}'; the engine "
                        f"ships one skills/{sk.name}/ directory, so names must be unique.",
                        agent.name,
                    )
                elif agent.name not in existing.used_by:
                    existing.used_by.append(agent.name)

    # 2) build agent nodes
    nodes: list[GoogleAgentNode] = []
    env_var_names: set[str] = set()
    any_mcp = False
    remapped: list[str] = []
    for agent in project.agents:
        recipes, evs = _build_mcp_recipes(agent, skip_unsupported, diags)
        env_var_names |= evs
        any_mcp = any_mcp or bool(recipes)
        _flag_builtin_gap(agent, diags)

        if agent.model and agent.model.startswith("claude"):
            remapped.append(agent.name)

        # validate subagents (depth-1 roster, like the proven live deploy)
        subs: list[str] = []
        if agent.subagents:
            if len(agent.subagents) > MAX_SUB_AGENTS:
                diags.error("google.subagents.too_many",
                            f"{len(agent.subagents)} sub_agents exceed the limit of {MAX_SUB_AGENTS}",
                            agent.name)
            for sub in agent.subagents:
                target = project.agent(sub)
                if target is None:
                    diags.error("google.subagent.missing",
                                f"sub_agent '{sub}' not found in project", agent.name)
                    continue
                if target.subagents:
                    diags.error(
                        "google.subagent.depth",
                        f"sub_agent '{sub}' is itself a coordinator; this target deploys "
                        f"a depth-1 roster (root + leaf sub_agents).",
                        agent.name,
                    )
                    continue
                subs.append(sub)

        instruction = _inline_knowledge(agent, diags)
        if len(instruction) > INSTRUCTION_LIMIT:
            diags.error("google.instruction.too_long",
                        f"instruction is {len(instruction)} chars (limit {INSTRUCTION_LIMIT})",
                        agent.name)

        nodes.append(GoogleAgentNode(
            name=agent.name,
            safe_name=safe_ident(agent.name),
            folder_model=agent.model or model,
            instruction=instruction,
            description=agent.description or agent.name,
            mcp=recipes,
            skills=[sk.name for sk in agent.skills],
            sub_agents=subs,
            builtin_web=_builtin_web_tools(agent),
        ))

    if remapped:
        diags.info(
            "google.model.remapped",
            f"Claude model(s) on {', '.join(remapped)} map to '{model}' on Vertex "
            f"(override with --google-model / AGENTLIFT_GOOGLE_MODEL).",
        )

    # order roster before coordinators so codegen can define sub_agents first
    ordered = [n for n in nodes if not n.is_coordinator] + [n for n in nodes if n.is_coordinator]

    requirements = [ENGINE_REQUIREMENT]
    if any(n.builtin_web for n in nodes):
        requirements.append(ADK_WEB_REQUIREMENT)
    skill_bundles = sorted(bundles.values(), key=lambda b: b.name)

    return GoogleDeployPlan(
        display_name=f"agentlift-{safe_ident(root_spec.name)}",
        root_agent=root_spec.name,
        deploy_model=model,
        agents=ordered,
        skill_bundles=skill_bundles,
        requirements=requirements,
        env_var_names=sorted(env_var_names),
        diagnostics=diags,
    )
