"""Turn a parsed ``Project`` into a deterministic ``HarnessDeployPlan``.

The *config-only* Bedrock mode. Where ``bedrock_plan.py`` compiles the folder to
a **Strands** source package + an ARM64 **AgentCore Runtime** container (your code
is the loop, multi-agent works), this module targets AgentCore's newer **harness**
primitive: a *managed* single agent you declare as config -- ``model`` +
``systemPrompt`` + ``tools`` + ``allowedTools`` + ``skills`` -- and AWS runs the
loop (powered by Strands under the hood). No container, no ECR: a true hosted
``deploy`` needing only IAM. The headline: Claude-native, no remap, simpler than
either the runtime path or Google.

Two AgentCore primitives, kept distinct (this is the crux of the whole target):

  - **AgentCore Runtime** (``bedrock_plan`` / ``--mode runtime``): custom container,
    *your* loop -> Strands agents-as-tools -> server-side subagent delegation works.
  - **AgentCore Harness** (this module / ``--mode harness``): managed, config-only,
    **single agent**. No sub-agent tool type exists, and a custom container's CMD is
    overridden -- so the harness genuinely *cannot* do server-side multi-agent
    delegation. Subagents are therefore ``NOT_SUPPORTED`` here (surfaced, never
    silently flattened), and skills are ``CONDITIONAL`` (the ``HarnessSkill``
    parameter is a *pointer* to a path already in the environment -- it does not
    upload, so a config-only harness with no container has nowhere to put a
    ``SKILL.md`` bundle). ``--mode auto`` routes such folders to the runtime, which
    preserves both losslessly; the harness is the lossless choice only for a single
    skill-less agent.

Like every other agentlift planner this is a *pure* function of the folder (plus
the chosen deploy region): same inputs in, same plan out, no network, no clock.
That is what makes ``agentlift plan --target bedrock --mode harness`` a safe
dry-run and the harness translation unit-testable. The plan is the contract.

**Preview / provisional.** The ``CreateHarness`` control-plane wire shape was read
from AWS docs published after this build's knowledge cutoff; agentlift has no live
receipt for it yet. So the plan is emitted (and offline-tested) with a standing
``bedrock.harness.preview`` info diagnostic, and ``harness_target.py`` prints a
provisional banner before any create. The shape encoded in ``to_create_body`` is
the thing the first live deploy will *verify and reconcile* -- the
confirm-live-before-encoding rule, applied honestly: the plan is provisional until
that receipt lands, at which point ``_HARNESS_LIVE_VERIFIED`` flips.

Secrets never enter the plan. An MCP server with inline auth contributes only the
*names* of the harness env vars its headers will read; the values resolve from the
deployer's local environment at deploy time (folded into ``config.remoteMcp.headers``
over the wire, never inlined into the plan, never hashed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .bedrock_plan import (
    INSTRUCTION_LIMIT,
    _auth_env_var,
    _inline_knowledge,
    resolve_bedrock_model,
    safe_ident,
)
from .diagnostics import Diagnostics
from .lockfile import canonical_hash
from .model import Project

# Whether agentlift's CreateHarness/InvokeHarness wire shape has a committed live
# receipt. Flipped True by tests/live/receipts/20260605-095014-harness-bedrock (Nova on
# us-east-1: create + agent + base-session sandbox (shell+file_operations) +
# agentcore_browser all EXERCISED server-side; remote_mcp WIRED -- the harness preview
# accepts+stores the tool but does not surface the MCP server's tools to the model).
# When True the provisional create banner is suppressed and `auto` may deploy to the
# harness; the AgentCore Harness itself is still an AWS *preview* (see the standing
# bedrock.harness.preview diagnostic).
_HARNESS_LIVE_VERIFIED = True

# AgentCore harness public-preview regions (April 2026). NOT eu-north-1 (where the
# runtime composition was verified) -- so the harness default region differs from
# the runtime's. A region outside this set is a warning, not a refusal: the set may
# expand and an account may have early access.
HARNESS_PREVIEW_REGIONS = ("us-east-1", "us-west-2", "eu-central-1", "ap-southeast-2")
DEFAULT_HARNESS_REGION = "us-west-2"

# Harness name constraint (CreateHarness): ^[a-zA-Z][a-zA-Z0-9_]{0,39}$
_HARNESS_NAME_MAX = 40

# Built-in tool mapping. Unlike the runtime preview (where these are PLANNED), the
# harness covers them -- but via TWO different mechanisms, which matters for honesty:
#   - the sandbox built-ins (bash/read/glob/grep/edit/write) are served by the
#     harness's ALWAYS-PRESENT base session tools (shell + file_operations = @builtin);
#     nothing is added, they are simply native.
#   - the web built-ins (web_search/web_fetch) map to an added agentcore_browser tool.
# (The agentcore_code_interpreter tool type also exists -- a Python/JS sandbox -- but
# no folder built-in requests code execution, so agentlift does not auto-add it.)
# All of this is provisional until the live harness receipt, like the rest of the shape.
_WEB_BUILTINS = {"web_search", "web_fetch"}
_BASE_BUILTINS = {"bash", "edit", "write", "glob", "grep", "read"}
_BROWSER_TOOL_TYPE = "agentcore_browser"


def safe_harness_name(name: str) -> str:
    """A valid ``harnessName`` (``^[a-zA-Z][a-zA-Z0-9_]{0,39}$``).

    ``agentlift_<ident>`` truncated to 40 chars; prefixed if it would not start
    with a letter (``safe_ident`` can leave a leading digit/underscore).
    """
    candidate = f"agentlift_{safe_ident(name)}"
    if not candidate[:1].isalpha():
        candidate = "h_" + candidate
    return candidate[:_HARNESS_NAME_MAX]


# --------------------------------------------------------------------------- #
# mode selection (auto = least-powerful mode that preserves semantics)
# --------------------------------------------------------------------------- #
def select_bedrock_mode(project: Project) -> tuple[str, str]:
    """Choose ``"harness"`` or ``"runtime"`` for ``--mode auto`` and explain why.

    The rule is *least-powerful-mode-that-preserves-semantics*, never a silent
    downgrade: the only thing the managed harness genuinely cannot represent is
    **multi-agent delegation** (it is a single managed agent, with no sub-agent tool
    type), so a coordinator/roster goes to the runtime. A *single* agent -- even with
    skills and remote MCP -- the harness preserves losslessly (skills upload to S3 and
    attach via ``skills[].s3.uri``; remote MCP attaches as a ``remote_mcp`` tool), so it
    stays on the lighter managed path. The returned reason is surfaced so the choice is
    always legible.
    """
    if any(a.subagents for a in project.agents):
        return ("runtime",
                "folder has subagents (coordinator delegation); the managed harness "
                "is single-agent, so runtime (Strands agents-as-tools) preserves it")
    if len(project.agents) > 1:
        return ("runtime",
                f"folder has {len(project.agents)} agents; the managed harness deploys "
                f"one agent, so runtime holds the whole roster")
    return ("harness",
            "single agent (skills attach via S3, remote MCP as a tool); the config-only "
            "managed harness preserves it (no container, IAM-only deploy)")


def harness_auto_deploy_allowed() -> bool:
    """Whether ``--mode auto`` may *deploy* (not just plan) straight to the harness.

    The managed-harness create RUNS live (that is how its provisional wire shape
    earns a receipt), but until that receipt exists the live preview path must be a
    *typed* opt-in (``--mode harness``), never the silent consequence of the ``auto``
    default. So a bare ``deploy --target bedrock`` (auto) that lands on the harness is
    refused while unverified, and ``plan``/dry-run is unaffected. Once a committed
    receipt flips ``_HARNESS_LIVE_VERIFIED``, auto-deploy to the harness is allowed --
    a single reviewable source of truth, no second persisted user flag.
    """
    return _HARNESS_LIVE_VERIFIED


# --------------------------------------------------------------------------- #
# plan dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class HarnessMcpTool:
    """One ``remote_mcp`` tool entry for the harness (URL/streamable-HTTP only)."""
    server: str
    url: str
    auth_env_vars: dict[str, str] = field(default_factory=dict)  # header name -> env var name

    def to_dict(self) -> dict[str, Any]:
        return {"server": self.server, "url": self.url, "auth_env_vars": self.auth_env_vars}


@dataclass
class HarnessSkill:
    """One skill bundle to attach to the harness. agentlift uploads its files to the
    deploy S3 bucket at deploy time and references them via ``skills[].s3.uri`` (the
    live harness loads the bundle from S3 — verified). ``files`` is the upload list
    ``(arcname, abs_path)``; only the name + content hash enter the plan/spec-hash."""
    name: str
    content_hash: str
    files: list = field(default_factory=list)  # (arcname, abs_path) — for upload, not hashed/shown

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "content_hash": self.content_hash}


@dataclass
class HarnessDeployPlan:
    harness_name: str
    display_name: str
    region: str
    folder_model: str
    bedrock_model: str                 # resolved inference-profile id -> bedrockModelConfig.modelId
    instruction: str                   # -> systemPrompt[0].text
    description: str
    mcp: list[HarnessMcpTool] = field(default_factory=list)
    builtin_tool_types: list[str] = field(default_factory=list)  # agentcore_browser / agentcore_code_interpreter
    allowed_tools: list[str] = field(default_factory=list)       # glob allowlist; [] = omit (all allowed)
    skills: list[HarnessSkill] = field(default_factory=list)      # uploaded to S3 at deploy -> skills[].s3.uri
    env_var_names: list[str] = field(default_factory=list)       # harness env vars the deploy must populate (MCP auth)
    mode: str = "harness"
    diagnostics: Diagnostics = field(default_factory=Diagnostics)

    @property
    def deployable(self) -> bool:
        return self.diagnostics.ok

    @property
    def live_verified(self) -> bool:
        return _HARNESS_LIVE_VERIFIED

    def to_hashable(self) -> dict[str, Any]:
        """Content that determines the deployed harness -- the idempotency basis.
        Excludes platform coordinates (account/role) and all secret values, and the
        bare ``region`` (which flows into ``bedrock_model``'s profile prefix, so two
        regions already yield two hashes). MCP auth contributes env-var *names* only.
        """
        return {
            "harness_name": self.harness_name,
            "display_name": self.display_name,
            "bedrock_model": self.bedrock_model,
            "instruction": self.instruction,
            "description": self.description,
            "mcp": [m.to_dict() for m in self.mcp],
            "builtin_tool_types": self.builtin_tool_types,
            "allowed_tools": self.allowed_tools,
            "skills": [s.to_dict() for s in self.skills],
            "env_var_names": self.env_var_names,
        }

    @property
    def spec_hash(self) -> str:
        return canonical_hash(self.to_hashable())

    def to_dict(self) -> dict[str, Any]:
        d = self.to_hashable()
        d["region"] = self.region
        d["mode"] = self.mode
        d["spec_hash"] = self.spec_hash
        d["live_verified"] = self.live_verified
        d["diagnostics"] = [diag.__dict__ for diag in self.diagnostics.items]
        d["deployable"] = self.deployable
        return d

    def to_create_body(
        self, *, execution_role_arn: str, client_token: str, mcp_headers: dict[str, str],
        skill_s3_uris: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Build the ``CreateHarness`` request body (pure -- no env, no network).

        ``mcp_headers`` maps each auth env-var name to its resolved value, and
        ``skill_s3_uris`` maps each skill name to the ``s3://...`` prefix the deployer
        uploaded its bundle to (both resolved at deploy time). The returned body carries
        the resolved values straight to the control-plane client and is **never** written
        to disk, the plan, or the lock. This is the one place the harness wire shape is
        materialized; it is live-verified (``_HARNESS_LIVE_VERIFIED``).
        """
        skill_s3_uris = skill_s3_uris or {}
        body: dict[str, Any] = {
            "harnessName": self.harness_name,
            "executionRoleArn": execution_role_arn,
            "model": {"bedrockModelConfig": {"modelId": self.bedrock_model}},
            "systemPrompt": [{"text": self.instruction}],
            "clientToken": client_token,
        }
        tools: list[dict[str, Any]] = []
        for m in self.mcp:
            remote: dict[str, Any] = {"url": m.url}
            headers = {h: mcp_headers.get(env, "") for h, env in m.auth_env_vars.items()}
            if headers:
                remote["headers"] = headers
            tools.append({"type": "remote_mcp", "name": m.server, "config": {"remoteMcp": remote}})
        for t in self.builtin_tool_types:
            tools.append({"type": t, "name": t})
        if tools:
            body["tools"] = tools
        # skills attach by the S3 prefix the deployer uploaded each bundle to (live-verified:
        # the harness ListObjectsV2's the prefix and loads the bundle; the exec role needs
        # s3:ListBucket + s3:GetObject on the bucket).
        skills = [{"s3": {"uri": skill_s3_uris[s.name]}} for s in self.skills
                  if s.name in skill_s3_uris]
        if skills:
            body["skills"] = skills
        if self.allowed_tools:
            body["allowedTools"] = self.allowed_tools
        return body


# --------------------------------------------------------------------------- #
# MCP lowering (URL only; inline auth -> env-var names)
# --------------------------------------------------------------------------- #
def _build_mcp_tools(
    agent, skip_unsupported: bool, diags: Diagnostics,
) -> tuple[list[HarnessMcpTool], set[str], bool]:
    """Lower an agent's MCP servers to harness ``remote_mcp`` tools.

    Returns ``(tools, env_var_names, any_restricted)``. ``any_restricted`` is True
    if any server narrows its tool set (an ``allowed_tools`` list), which is what
    forces an explicit ``allowedTools`` allowlist (the harness has no per-server
    ``tool_filter`` the way Strands does -- restriction rides the global allowlist).
    """
    tools: list[HarnessMcpTool] = []
    env_vars: set[str] = set()
    any_restricted = False
    for srv in agent.mcp_servers:
        if srv.transport != "url":
            msg = (
                f"MCP server '{srv.name}' is stdio (command: {srv.command or '?'}); "
                f"the AgentCore harness attaches only remote URL MCP servers. Host it "
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
                f"local environment at deploy and sent as harness MCP header(s) "
                f"(env var name(s) {', '.join(sorted(auth_env.values()))}; not inlined "
                f"into the plan or source).",
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
                f"{', '.join(asks)} is not enforced on the hosted harness (no "
                f"interactive approval channel); keep :ask agents on the Anthropic target.",
                agent.name,
            )

        if srv.allowed_tools is not None:
            any_restricted = True
            diags.warning(
                "bedrock.mcp.tool_filter_unenforced",
                f"MCP server '{srv.name}' narrows its tools to "
                f"{', '.join(srv.allowed_tools)}, but the harness preview does not enforce "
                f"a per-tool MCP allowlist (a restrictive `allowedTools` suppresses MCP-tool "
                f"surfacing entirely — live-observed). All of the server's tools surface on "
                f"the harness; keep tool-restricted MCP on Anthropic / --mode runtime "
                f"(`tool_filter`) if the restriction is load-bearing.",
                agent.name,
            )
        tools.append(HarnessMcpTool(server=srv.name, url=srv.url or "", auth_env_vars=auth_env))
    return tools, env_vars, any_restricted


def _build_allowed_tools(
    agent, mcp: list[HarnessMcpTool], builtin_types: list[str], any_restricted: bool,
) -> list[str]:
    """The harness ``allowedTools`` glob allowlist — deliberately **always empty**.

    Live finding: a *restrictive* ``allowedTools`` on the harness suppresses remote-MCP
    tool surfacing to the model entirely (both ``@<server>/<tool>`` and ``@<server>_<tool>``
    forms removed the MCP tools, while ``@builtin`` / ``@<browser>`` kept working). So
    agentlift does **not** emit a partial allowlist — it would silently drop the very MCP
    tools the folder wants. With no allowlist the harness exposes its full configured
    toolset (base-session shell + file_operations, any added ``agentcore_*`` tool, and every
    MCP server tool as ``<server>_<tool>``), which is what we want. Per-tool MCP narrowing is
    surfaced as the ``bedrock.mcp.tool_filter_unenforced`` diagnostic instead of being faked
    here. (Kept as a function/seam so the plan shape and tests are stable.)"""
    return []


# --------------------------------------------------------------------------- #
# built-in tools (map to harness tool types; provisional like the rest)
# --------------------------------------------------------------------------- #
def _collect_builtin_tool_types(agent, diags: Diagnostics) -> list[str]:
    """Map the agent's enabled built-ins to added harness tool types.

    Returns only the tool types agentlift *adds* (today: ``agentcore_browser`` for
    the web built-ins). The sandbox built-ins need no added tool -- they are served
    by the harness's always-present base session tools (shell + file_operations,
    i.e. ``@builtin``). ``None`` means "all built-ins" (the parser's default when no
    ``tools:`` allowlist is given); we do NOT silently add the Browser tool in that
    case -- it is mapped only when web_* is explicitly listed, never a silent grant.
    """
    if agent.builtin_tools is None:
        diags.info(
            "bedrock.harness.builtin_default",
            "no explicit tool allowlist; the harness keeps its always-present base "
            "tools (shell + file_operations) but agentlift does not auto-add the "
            "Browser / Code Interpreter tool types (list web_search/web_fetch in "
            "'tools:' to map the Browser).",
            agent.name,
        )
        return []
    enabled = set(agent.builtin_tools)
    web = sorted(enabled & _WEB_BUILTINS)
    base = sorted(enabled & _BASE_BUILTINS)
    types: list[str] = [_BROWSER_TOOL_TYPE] if web else []
    if base:
        diags.info(
            "bedrock.harness.builtin_native",
            f"built-in tool(s) {', '.join(base)} are served by the harness base "
            f"session tools (shell + file_operations); native, nothing added "
            f"(provisional until the live harness receipt).",
            agent.name,
        )
    if web:
        diags.info(
            "bedrock.harness.builtin_mapped",
            f"built-in web tool(s) {', '.join(web)} -> harness tool type "
            f"{_BROWSER_TOOL_TYPE} (AgentCore Browser; provisional until the live "
            f"harness receipt).",
            agent.name,
        )
    asks = [t for t, p in (agent.builtin_tool_policies or {}).items() if p == "ask"]
    if asks:
        diags.warning(
            "bedrock.tool_approval.unsupported",
            f"per-tool approval (:ask) on built-in {', '.join(asks)} is not enforced "
            f"on the hosted harness.",
            agent.name,
        )
    return types


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build_harness_plan(
    project: Project,
    diags: Optional[Diagnostics] = None,
    *,
    region: str = DEFAULT_HARNESS_REGION,
    skip_unsupported: bool = False,
) -> HarnessDeployPlan:
    """``Project`` -> ``HarnessDeployPlan`` (a single managed AgentCore harness).

    The harness is single-agent and config-only, so this enforces those limits as
    *surfaced diagnostics*, never silent drops:
      - subagents          -> NOT_SUPPORTED (error; ``skip_unsupported`` flattens to root)
      - >1 agent           -> NOT_SUPPORTED (error; ``skip_unsupported`` keeps the root only)
      - skill bundles       -> CONDITIONAL (error; ``skip_unsupported`` omits them)
    ``--mode auto`` (see ``select_bedrock_mode``) routes such folders to the runtime
    so the user rarely hits these; they fire when ``--mode harness`` is forced.
    """
    diags = diags or Diagnostics()

    diags.info(
        "bedrock.harness.preview",
        "AgentCore Harness is an AWS PREVIEW feature. agentlift's CreateHarness/"
        "InvokeHarness wire shape is live-verified (receipt 20260605-095014-harness-"
        "bedrock, Nova on us-east-1: create + agent + base-session sandbox + "
        "agentcore_browser EXERCISED). Two honest caveats: a remote_mcp tool is accepted "
        "+ stored but the harness preview does not surface the MCP server's tools to the "
        "model (WIRED, not yet exercisable); and Claude inference runs in the harness but "
        "is gated by the per-account Anthropic use-case entitlement (Gate A), which is "
        "eventually-consistent.",
    )

    if region not in HARNESS_PREVIEW_REGIONS:
        diags.warning(
            "bedrock.harness.region_preview",
            f"region '{region}' is not in the AgentCore harness public-preview set "
            f"({', '.join(HARNESS_PREVIEW_REGIONS)}); the deploy may fail with a "
            f"region-unavailable error. Override with --bedrock-region.",
        )

    if not project.agents:
        diags.error("bedrock.project.empty", "no agents to deploy")
        return HarnessDeployPlan(
            harness_name="agentlift", display_name="agentlift", region=region,
            folder_model="", bedrock_model="", instruction="", description="",
            diagnostics=diags,
        )

    # the harness is one agent: the coordinator if there is one (its instruction is
    # the orchestration prompt), else the first agent. Extra agents can't ride along.
    coords = [a for a in project.agents if a.subagents]
    root = coords[0] if coords else project.agents[0]

    if root.subagents:
        msg = (
            f"agent '{root.name}' has subagents {root.subagents}; the managed harness "
            f"is single-agent (no sub-agent tool type, and a custom container's CMD is "
            f"overridden), so server-side delegation is NOT supported. Use --mode "
            f"runtime (Strands agents-as-tools), or --mode auto."
        )
        if skip_unsupported:
            diags.warning("bedrock.harness.subagents_skipped",
                          msg + " (flattened: deploying the root agent alone)", root.name)
        else:
            diags.error("bedrock.harness.subagents_unsupported", msg, root.name)

    extras = [a for a in project.agents if a.name != root.name]
    if extras:
        names = ", ".join(a.name for a in extras)
        msg = (
            f"folder has {len(project.agents)} agents ({names} besides '{root.name}'); "
            f"the managed harness deploys one agent. Use --mode runtime to hold the "
            f"whole roster, or --mode auto."
        )
        if skip_unsupported:
            diags.warning("bedrock.harness.multi_agent_skipped",
                          msg + f" (deploying '{root.name}' only)", root.name)
        else:
            diags.error("bedrock.harness.multi_agent_unsupported", msg, root.name)

    # Skills attach to the harness via S3 (live-verified): agentlift uploads each bundle
    # to the deploy S3 bucket at apply time and references it as skills[].s3.uri. Only the
    # name + content hash enter the plan/spec-hash; the files ride to the deployer for upload.
    skills: list[HarnessSkill] = []
    for sk in root.skills:
        skills.append(HarnessSkill(name=sk.name, content_hash=sk.content_hash,
                                   files=list(sk.files)))
    if skills:
        diags.info(
            "bedrock.harness.skills_via_s3",
            f"skill(s) {', '.join(s.name for s in skills)} upload to the deploy S3 bucket "
            f"($AGENTLIFT_BEDROCK_S3_BUCKET) and attach via skills[].s3.uri; the harness "
            f"execution role needs s3:ListBucket + s3:GetObject on that bucket "
            f"(live-verified).",
            root.name,
        )

    # MCP + built-ins for the root agent (no restrictive allowlist — see _build_allowed_tools)
    mcp, env_vars, any_restricted = _build_mcp_tools(root, skip_unsupported, diags)
    builtin_types = _collect_builtin_tool_types(root, diags)
    allowed = _build_allowed_tools(root, mcp, builtin_types, any_restricted)

    folder_model = root.model or "claude-sonnet-4-6"
    bedrock_model = resolve_bedrock_model(folder_model, region, diags, root.name)

    instruction = _inline_knowledge(root, diags)
    if len(instruction) > INSTRUCTION_LIMIT:
        diags.error("bedrock.instruction.too_long",
                    f"instruction is {len(instruction)} chars (limit {INSTRUCTION_LIMIT})",
                    root.name)

    return HarnessDeployPlan(
        harness_name=safe_harness_name(root.name),
        display_name=f"agentlift-{safe_ident(root.name)}",
        region=region,
        folder_model=folder_model,
        bedrock_model=bedrock_model,
        instruction=instruction,
        description=root.description or root.name,
        mcp=mcp,
        builtin_tool_types=builtin_types,
        allowed_tools=allowed,
        skills=skills,
        env_var_names=sorted(env_vars),
        mode="harness",
        diagnostics=diags,
    )
