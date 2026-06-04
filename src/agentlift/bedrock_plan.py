"""Turn a parsed ``Project`` into a deterministic ``BedrockDeployPlan``.

The fourth deploy target: Amazon Bedrock **AgentCore Runtime**. Mirrors
``google_plan.py`` -- a single hosted runtime holding a root **Strands** agent
plus its sub-agents-as-tools (in-model delegation). So the plan describes one
runtime with N agent nodes, the skill bundles shipped into its source package,
and the MCP recipes each agent wires up.

Like the Anthropic and Google planners this is a *pure* function of the folder
(plus the chosen deploy region): same inputs in, same plan out, no network, no
clock. That is what makes ``agentlift plan --target bedrock`` a safe dry-run and
the whole Bedrock translation unit-testable. The plan is the contract:

  - ``bedrock_codegen.py`` (next) renders the plan into a Strands source package
    (a ``BedrockAgentCoreApp`` exposing ``/invocations`` + ``/ping``).
  - ``bedrock_target.py`` (gated on IAM) ships that package to an AgentCore
    Runtime via ``bedrock-agentcore-control.create/update_agent_runtime``.

What is **live-verified** today (see ``experiments/bedrock-composition/``):
the model story and the composition. Claude runs NATIVELY on Bedrock -- no
Gemini-style remap; a Claude folder id maps almost 1:1 to a Bedrock regional
inference-profile id. A Strands coordinator delegating to a sub-agent-as-tool
plus a deterministic tool ran end-to-end with only the bearer token. The model
map below is grounded in the account's actual ``ACTIVE``/``SYSTEM_DEFINED``
inference profiles in eu-north-1 (listed 2026-06-04), not guessed.

What is **not yet implemented** (surfaced as diagnostics, never silently
dropped): built-in sandbox/web tools (they *can* map to AgentCore Code
Interpreter / Browser, unlike Google's non-goal -- but that needs a live
receipt first) and ``:ask`` (no server-side gate on the runtime session).

Secrets never enter the plan. A server with inline auth contributes only the
*names* of the runtime env vars its headers will read at runtime; the values
are resolved from the deployer's local environment at deploy time (handed to
the runtime as env vars, never inlined into source, never hashed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .diagnostics import Diagnostics
from .lockfile import canonical_hash
from .model import AgentSpec, Project

# Strands accepts large system prompts; keep a generous guard so inlined
# knowledge can't produce an absurd instruction string.
INSTRUCTION_LIMIT = 100_000
MAX_SUB_AGENTS = 50

# Built-in (sandbox) tools the folder may enable. On Bedrock these CAN map (the
# AgentCore Code Interpreter / Browser / shell-exec) -- unlike Google, where the
# sandbox is a non-goal. But agentlift has no live receipt for them yet, so this
# preview surfaces them as "planned, not mapped" rather than encoding unverified
# behavior (the confirm-live-before-encoding rule).
_SANDBOX_TOOLS = {"bash", "edit", "write", "glob", "grep", "read"}
_WEB_TOOLS = {"web_search", "web_fetch"}

# Default deploy region. eu-north-1 (Stockholm) is where the live composition was
# verified; AgentCore Runtime is GA in 14 regions.
DEFAULT_BEDROCK_REGION = "eu-north-1"

# Runtime source-package requirements. strands-agents is the compile target; the
# bedrock-agentcore package provides BedrockAgentCoreApp (the /invocations+/ping
# server contract). Floors: Strands' bearer-token-capable boto3 path needs
# boto3>=1.40, and Strands tool-calling + multiagent stabilized at 1.42.
STRANDS_REQUIREMENT = "strands-agents>=1.42"
RUNTIME_REQUIREMENT = "bedrock-agentcore"
BOTO3_REQUIREMENT = "boto3>=1.40"


def safe_ident(name: str) -> str:
    """A valid Strands agent / Python symbol fragment.

    Shared with ``bedrock_target`` and ``bedrock_codegen`` so the name an agent
    is generated under matches everywhere (it is also the agent-as-tool function
    name the coordinator delegates through).
    """
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name)


def _auth_env_var(server: str, header: str) -> str:
    """Stable runtime env-var name that will carry one MCP auth header's value."""
    return "AGENTLIFT_MCP_" + safe_ident(server).upper() + "_" + safe_ident(header).upper()


# --------------------------------------------------------------------------- #
# model resolution (Claude is NATIVE on Bedrock -- map, don't remap)
# --------------------------------------------------------------------------- #
# Folder-friendly Claude ids whose Bedrock regional-profile slug carries a
# date/version suffix. The newest models (claude-sonnet-4-6, claude-opus-4-7,
# claude-opus-4-8, ...) use the BARE id as their profile slug, so they need no
# alias. Verified against eu.anthropic.* SYSTEM_DEFINED profiles, eu-north-1,
# 2026-06-04. Extend (don't guess) as new profiles are confirmed available.
_CLAUDE_SLUG_ALIASES = {
    "claude-haiku-4-5": "claude-haiku-4-5-20251001-v1:0",
    "claude-sonnet-4": "claude-sonnet-4-20250514-v1:0",
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929-v1:0",
    "claude-opus-4-5": "claude-opus-4-5-20251101-v1:0",
    "claude-opus-4-6": "claude-opus-4-6-v1",
}


def region_prefix(region: str) -> str:
    """The Bedrock cross-region inference-profile prefix for a deploy region.

    eu-* -> 'eu', us-* -> 'us', ap-* -> 'apac'; anything else falls back to
    'global' (the always-on cross-region profile family). The full profile id is
    ``<prefix>.anthropic.<slug>``.
    """
    r = (region or "").lower()
    if r.startswith("eu-"):
        return "eu"
    if r.startswith("us-"):
        return "us"
    if r.startswith("ap-"):
        return "apac"
    return "global"


def resolve_bedrock_model(folder_model: str, region: str, diags: Diagnostics, where: str = "") -> str:
    """Map a folder model id to a Bedrock inference-profile id.

    Claude stays Claude -- the cleaner story vs Google's Gemini remap. A non-Claude
    folder id is passed through verbatim (Bedrock also hosts Nova/Llama/Mistral) with
    a warning, since agentlift's folder model is normally Claude. The resolution is
    always surfaced so it is never a silent guess.
    """
    if not folder_model:
        folder_model = "claude-sonnet-4-6"
    if not folder_model.startswith("claude"):
        diags.warning(
            "bedrock.model.non_claude",
            f"folder model '{folder_model}' is not a Claude id; passing it through "
            f"verbatim as a Bedrock model id (Bedrock also hosts Nova/Llama/Mistral, "
            f"but verify the profile id exists in {region}).",
            where,
        )
        return folder_model
    slug = _CLAUDE_SLUG_ALIASES.get(folder_model, folder_model)
    profile = f"{region_prefix(region)}.anthropic.{slug}"
    diags.info(
        "bedrock.model.resolved",
        f"model '{folder_model}' -> Bedrock inference profile '{profile}' "
        f"(Claude is native on Bedrock; subject to per-account Anthropic use-case "
        f"access + per-region availability).",
        where,
    )
    return profile


# --------------------------------------------------------------------------- #
# plan dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class McpRecipe:
    """One Strands MCP client to attach to an agent (URL/streamable-HTTP only)."""
    server: str
    url: str
    tool_filter: Optional[list[str]]          # allowed tool names; None = all tools
    auth_env_vars: dict[str, str] = field(default_factory=dict)  # header name -> runtime env var name

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "url": self.url,
            "tool_filter": self.tool_filter,
            "auth_env_vars": self.auth_env_vars,
        }


@dataclass
class BedrockAgentNode:
    """One Strands ``Agent`` in the deployed runtime."""
    name: str
    safe_name: str
    folder_model: str                         # the model id from the folder
    bedrock_model: str                        # resolved Bedrock inference-profile id
    instruction: str
    description: str
    mcp: list[McpRecipe] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)      # skill bundle names this agent loads
    sub_agents: list[str] = field(default_factory=list)  # roster agent names (makes it a coordinator)

    @property
    def is_coordinator(self) -> bool:
        return bool(self.sub_agents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "safe_name": self.safe_name,
            "folder_model": self.folder_model,
            "bedrock_model": self.bedrock_model,
            "instruction": self.instruction,
            "description": self.description,
            "mcp": [m.to_dict() for m in self.mcp],
            "skills": self.skills,
            "sub_agents": self.sub_agents,
        }


@dataclass
class SkillBundle:
    """A skill directory shipped into the runtime source package under skills/<name>/."""
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
class BedrockDeployPlan:
    display_name: str
    root_agent: str
    region: str
    agents: list[BedrockAgentNode]
    skill_bundles: list[SkillBundle]
    requirements: list[str]
    env_var_names: list[str]                  # runtime env vars the deploy must populate (MCP auth)
    diagnostics: Diagnostics

    @property
    def deployable(self) -> bool:
        return self.diagnostics.ok

    def to_hashable(self) -> dict[str, Any]:
        """The content that determines the deployed artifact -- the basis for the
        idempotency spec hash. Excludes platform coordinates (account/ECR/role) and
        all secret values; those decide *where*/*with what creds*, not *what* is
        deployed. The resolved Bedrock model id IS hashed (it is the literal model
        the generated source pins). ``region`` is not a separate hashed field, but
        it flows into each node's ``bedrock_model`` (the profile prefix), so the
        same folder deployed to two regions yields two hashes -- genuinely two
        artifacts pinned to two regional inference profiles. Re-deploying to the
        *same* region is stable, which is what idempotency needs."""
        return {
            "display_name": self.display_name,
            "root_agent": self.root_agent,
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
        d["region"] = self.region
        d["spec_hash"] = self.spec_hash
        d["diagnostics"] = [diag.__dict__ for diag in self.diagnostics.items]
        d["deployable"] = self.deployable
        return d


# --------------------------------------------------------------------------- #
# knowledge (folded into the instruction; same treatment as the other planners)
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
                "bedrock.knowledge.truncated",
                f"knowledge files exceed the instruction budget; "
                f"{len(agent.knowledge_files) - added} file(s) not inlined",
                agent.name,
            )
            break
        parts.append(block)
        budget -= len(block)
        added += 1
    if added:
        diags.info("bedrock.knowledge.inlined",
                   f"inlined {added} knowledge file(s) into the instruction", agent.name)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# MCP lowering
# --------------------------------------------------------------------------- #
def _build_mcp_recipes(
    agent: AgentSpec, skip_unsupported: bool, diags: Diagnostics,
) -> tuple[list[McpRecipe], set[str]]:
    """Lower an agent's MCP servers to Strands MCP client recipes.

    Returns (recipes, env_var_names). stdio servers are unsupported (skipped or
    errored). URL servers map; inline auth headers map to runtime env vars by
    name -- values resolved at deploy, never inlined here.
    """
    recipes: list[McpRecipe] = []
    env_vars: set[str] = set()
    for srv in agent.mcp_servers:
        if srv.transport != "url":
            msg = (
                f"MCP server '{srv.name}' is stdio (command: {srv.command or '?'}); "
                f"AgentCore Runtime attaches only remote URL MCP servers. Host it "
                f"behind an HTTPS endpoint and set its 'url'."
            )
            if skip_unsupported:
                diags.warning("bedrock.mcp.stdio_skipped", msg + " (skipped)", agent.name)
            else:
                diags.error("bedrock.mcp.stdio_unsupported", msg, agent.name)
            continue

        auth_env: dict[str, str] = {}
        if srv.headers:
            for header in sorted(srv.headers):
                name = _auth_env_var(srv.name, header)
                auth_env[header] = name
                env_vars.add(name)
            diags.warning(
                "bedrock.mcp.auth_via_env",
                f"MCP server '{srv.name}' declares inline auth header(s) "
                f"{', '.join(sorted(srv.headers))}; their values are read from your "
                f"local environment at deploy and stored as runtime env var(s) "
                f"{', '.join(sorted(auth_env.values()))} (not inlined into the source).",
                agent.name,
            )
        elif srv.has_inline_auth:
            diags.warning(
                "bedrock.mcp.auth_dropped",
                f"MCP server '{srv.name}' declares inline 'env' but no headers; "
                f"a URL MCP server authenticates via headers, so this is not forwarded.",
                agent.name,
            )

        asks = [t for t, p in (srv.tool_policies or {}).items() if p == "ask"]
        if asks:
            diags.warning(
                "bedrock.tool_approval.unsupported",
                f"MCP server '{srv.name}': per-tool approval (:ask) on "
                f"{', '.join(asks)} is not enforced on AgentCore Runtime; the tool "
                f"stays available without a gate (keep :ask agents on the Anthropic target).",
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
# built-in tools (preview: surface, do not encode unverified mappings)
# --------------------------------------------------------------------------- #
def _flag_builtin_tools(agent: AgentSpec, diags: Diagnostics) -> None:
    """Surface the built-in story per agent. On Bedrock the sandbox/web tools CAN
    map (AgentCore Code Interpreter / Browser / shell-exec) -- but with no live
    receipt yet, this preview does NOT encode them; it flags them as planned. The
    agent deploys without them. :ask is unenforceable on the runtime session.
    Nothing is dropped silently."""
    if agent.builtin_tools is None:
        web = sorted(_WEB_TOOLS)
        sandbox = sorted(_SANDBOX_TOOLS)
    else:
        enabled = set(agent.builtin_tools)
        web = sorted(enabled & _WEB_TOOLS)
        sandbox = sorted(enabled & _SANDBOX_TOOLS)

    if web:
        diags.warning(
            "bedrock.builtin.web_planned",
            f"built-in web tool(s) {', '.join(web)} are not mapped in this Bedrock "
            f"preview; planned via AgentCore Browser / a fetch tool. The agent deploys "
            f"without them (supply equivalents via a URL MCP server meanwhile).",
            agent.name,
        )
    if sandbox:
        diags.warning(
            "bedrock.builtin.sandbox_planned",
            f"built-in tool(s) {', '.join(sandbox)} are not mapped in this Bedrock "
            f"preview; planned via AgentCore Code Interpreter / shell-exec (these DO "
            f"map on Bedrock, unlike Google -- pending a live receipt). The agent "
            f"deploys without them (supply equivalents via a URL MCP server meanwhile).",
            agent.name,
        )
    asks = [t for t, p in (agent.builtin_tool_policies or {}).items() if p == "ask"]
    if asks:
        diags.warning(
            "bedrock.tool_approval.unsupported",
            f"per-tool approval (:ask) on built-in {', '.join(asks)} is not enforced "
            f"on AgentCore Runtime.",
            agent.name,
        )


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build_bedrock_plan(
    project: Project,
    diags: Optional[Diagnostics] = None,
    *,
    region: str = DEFAULT_BEDROCK_REGION,
    skip_unsupported: bool = False,
) -> BedrockDeployPlan:
    diags = diags or Diagnostics()

    if not project.agents:
        diags.error("bedrock.project.empty", "no agents to deploy")
        return BedrockDeployPlan(
            display_name="agentlift", root_agent="", region=region,
            agents=[], skill_bundles=[],
            requirements=[STRANDS_REQUIREMENT, RUNTIME_REQUIREMENT, BOTO3_REQUIREMENT],
            env_var_names=[], diagnostics=diags,
        )

    # which agents are roster (leaves) vs coordinators (root)
    coords = [a for a in project.agents if a.subagents]
    root_spec = coords[0] if coords else project.agents[0]

    # 1) collect + dedupe skill bundles by name across all agents (the dir name is
    #    the load key). Identical names with different content are a conflict the
    #    one source package can't represent (one dir, one content).
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
                        "bedrock.skill.name_collision",
                        f"two different skills are both named '{sk.name}'; the runtime "
                        f"ships one skills/{sk.name}/ directory, so names must be unique.",
                        agent.name,
                    )
                elif agent.name not in existing.used_by:
                    existing.used_by.append(agent.name)

    # 2) build agent nodes
    nodes: list[BedrockAgentNode] = []
    env_var_names: set[str] = set()
    for agent in project.agents:
        recipes, evs = _build_mcp_recipes(agent, skip_unsupported, diags)
        env_var_names |= evs
        _flag_builtin_tools(agent, diags)

        folder_model = agent.model or "claude-sonnet-4-6"
        bedrock_model = resolve_bedrock_model(folder_model, region, diags, agent.name)

        # validate subagents (depth-1 roster, matching the verified composition)
        subs: list[str] = []
        if agent.subagents:
            if len(agent.subagents) > MAX_SUB_AGENTS:
                diags.error("bedrock.subagents.too_many",
                            f"{len(agent.subagents)} sub_agents exceed the limit of {MAX_SUB_AGENTS}",
                            agent.name)
            for sub in agent.subagents:
                target = project.agent(sub)
                if target is None:
                    diags.error("bedrock.subagent.missing",
                                f"sub_agent '{sub}' not found in project", agent.name)
                    continue
                if target.subagents:
                    diags.error(
                        "bedrock.subagent.depth",
                        f"sub_agent '{sub}' is itself a coordinator; this target deploys "
                        f"a depth-1 roster (root + leaf sub_agents-as-tools).",
                        agent.name,
                    )
                    continue
                subs.append(sub)

        instruction = _inline_knowledge(agent, diags)
        if len(instruction) > INSTRUCTION_LIMIT:
            diags.error("bedrock.instruction.too_long",
                        f"instruction is {len(instruction)} chars (limit {INSTRUCTION_LIMIT})",
                        agent.name)

        nodes.append(BedrockAgentNode(
            name=agent.name,
            safe_name=safe_ident(agent.name),
            folder_model=folder_model,
            bedrock_model=bedrock_model,
            instruction=instruction,
            description=agent.description or agent.name,
            mcp=recipes,
            skills=[sk.name for sk in agent.skills],
            sub_agents=subs,
        ))

    # order roster (leaves) before coordinators so codegen defines tools first
    ordered = [n for n in nodes if not n.is_coordinator] + [n for n in nodes if n.is_coordinator]

    requirements = [STRANDS_REQUIREMENT, RUNTIME_REQUIREMENT, BOTO3_REQUIREMENT]
    skill_bundles = sorted(bundles.values(), key=lambda b: b.name)

    return BedrockDeployPlan(
        display_name=f"agentlift-{safe_ident(root_spec.name)}",
        root_agent=root_spec.name,
        region=region,
        agents=ordered,
        skill_bundles=skill_bundles,
        requirements=requirements,
        env_var_names=sorted(env_var_names),
        diagnostics=diags,
    )
