"""Turn provider read-API responses into an `ImportedProject` (the inverse of the planner).

This is the import *contract*: a pure function of the data a runtime hands back,
same responses in => same `ImportedProject` out, no network. The network lives in
`anthropic_source.py` / `harness_source.py`, which fetch the raw dicts this module
consumes — exactly mirroring how `planner.py` is pure and only `*_target.py` touches
the wire.

Two entry points:
  - `import_anthropic_agents(...)`  -- inverts `planner._build_agent_create`
  - `import_bedrock_harness(...)`   -- inverts `harness_plan.build_harness_plan`

Both share the tool-decoding, skill-hashing and shared-resource hoisting below, and
both surface anything that can't round-trip as a `Diagnostic` (never a silent drop).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from .bedrock_plan import _CLAUDE_SLUG_ALIASES
from .diagnostics import Diagnostics
from .import_model import ImportedAgent, ImportedMcp, ImportedProject, ImportedSkill

# Inverse of planner._POLICY_TYPE
_POLICY_FROM_TYPE = {"always_ask": "ask", "always_allow": "allow"}

# Managed builtin names are already valid local tokens; identity keeps the table
# explicit and lets us normalise any future spelling drift in one place.
_LOCAL_TOOL = {
    "read": "read", "glob": "glob", "grep": "grep", "bash": "bash",
    "edit": "edit", "write": "write", "web_fetch": "web_fetch", "web_search": "web_search",
}

# A skill we can recognise the file layout for hashes identically to the parser, so
# the planner dedups an imported shared skill exactly as it would a hand-written one.
def hash_skill_files(files: dict[str, bytes]) -> str:
    """SHA-256 over (arcname, bytes), sorted by arcname — matches parser.hash_skill_dir."""
    h = hashlib.sha256()
    for arcname in sorted(files):
        h.update(arcname.encode("utf-8"))
        h.update(b"\0")
        h.update(files[arcname])
        h.update(b"\0")
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# tool decoding (inverse of planner._build_tools / _tool_config)
# --------------------------------------------------------------------------- #
def decode_tools(
    tools: list[dict[str, Any]], where: str, diags: Diagnostics,
) -> tuple[Optional[list[str]], dict[str, str], dict[str, dict]]:
    """Split a retrieved `tools` array into the folder's three tool channels.

    Returns the canonical (bare-name + separate-policy) shape `AgentSpec` uses — the
    folder_writer reattaches the `:ask`/`:allow` suffix:
      builtin_tokens: bare local tool names for `tools:`, or None = "all builtins".
      builtin_policies: managed tool -> "ask"|"allow".
      mcp_filters: {server_name: {"allowed": [bare names]|None, "policies": {name: pol}}}.
    """
    builtin_tokens: Optional[list[str]] = None
    builtin_policies: dict[str, str] = {}
    mcp_filters: dict[str, dict] = {}
    for t in tools or []:
        ttype = t.get("type")
        if ttype == "agent_toolset_20260401":
            default_on = (t.get("default_config") or {}).get("enabled", True)
            configs = t.get("configs") or []
            if default_on and not configs:
                builtin_tokens = None  # "all builtins"
            else:
                builtin_tokens = []
                for c in configs:
                    local = _LOCAL_TOOL.get(c.get("name", ""), c.get("name", ""))
                    builtin_tokens.append(local)
                    pol = (c.get("permission_policy") or {}).get("type")
                    if pol in _POLICY_FROM_TYPE:
                        # key by the SAME local token folder_writer reattaches the
                        # suffix to (not the raw provider name), so a non-identity
                        # tool map can't silently drop the policy
                        builtin_policies[local] = _POLICY_FROM_TYPE[pol]
        elif ttype == "mcp_toolset":
            srv = t.get("mcp_server_name")
            default_on = (t.get("default_config") or {}).get("enabled", True)
            configs = t.get("configs") or []
            if default_on and not configs:
                mcp_filters[srv] = {"allowed": None, "policies": {}}
            else:
                names, pols = [], {}
                for c in configs:
                    names.append(c["name"])
                    pol = (c.get("permission_policy") or {}).get("type")
                    if pol in _POLICY_FROM_TYPE:
                        pols[c["name"]] = _POLICY_FROM_TYPE[pol]
                mcp_filters[srv] = {"allowed": names, "policies": pols}
        elif ttype == "custom_tool":
            diags.warning(
                "import.custom_tool_dropped",
                f"agent declares a custom tool '{t.get('name', '?')}'; custom tools are "
                f"not representable in the folder (define them in your harness) — dropped",
                where,
            )
    return builtin_tokens, builtin_policies, mcp_filters


# --------------------------------------------------------------------------- #
# model reverse-map (Bedrock regional inference profile -> folder Claude id)
# --------------------------------------------------------------------------- #
_BEDROCK_SLUG_TO_FOLDER = {v: k for k, v in _CLAUDE_SLUG_ALIASES.items()}
_BEDROCK_PROFILE_RE = re.compile(r"^(eu|us|apac|global)\.anthropic\.(.+)$")


def reverse_bedrock_model(model_id: str) -> str:
    """`<prefix>.anthropic.<slug>` -> folder Claude id (inverse of resolve_bedrock_model).

    Strips the cross-region prefix, then inverts `_CLAUDE_SLUG_ALIASES`. A dated slug
    with no alias keeps its (still-valid) full id; a non-Claude profile passes through.
    """
    m = _BEDROCK_PROFILE_RE.match(model_id or "")
    if not m:
        return model_id  # Nova/Llama/etc., or an already-folder id
    slug = m.group(2)
    return _BEDROCK_SLUG_TO_FOLDER.get(slug, slug)


# --------------------------------------------------------------------------- #
# shared-resource hoisting (folder organisation: shared/ vs per-agent)
# --------------------------------------------------------------------------- #
def _mcp_identity(m: ImportedMcp) -> tuple:
    return (m.name, m.url, m.transport, tuple(m.allowed_tools or []),
            tuple(sorted(m.tool_policies.items())), tuple(sorted(m.auth_env_names)))


def _hoist_shared(agents: list[ImportedAgent], project: ImportedProject) -> None:
    """Move resources used identically by >1 agent into the shared pool.

    Skills key on `content_hash` (same key the planner dedups on); MCP servers key on
    full identity (name+url+filter+policies). A resource used by a single agent stays
    local. After this, an agent references a shared resource as `shared/<name>` and
    carries no local copy of it.
    """
    # --- skills (key on content_hash; an agent may name an identical skill differently) ---
    skill_users: dict[str, list[ImportedAgent]] = {}
    skills_by_hash: dict[str, list[ImportedSkill]] = {}
    for a in agents:
        for sk in a.local_skills:
            if a not in skill_users.setdefault(sk.content_hash, []):
                skill_users[sk.content_hash].append(a)
            skills_by_hash.setdefault(sk.content_hash, []).append(sk)
    for chash, users in skill_users.items():
        if len(users) < 2:   # used by a single agent -> stays local
            continue
        # canonical shared name is stable (smallest name) regardless of import order
        shared = min(skills_by_hash[chash], key=lambda s: s.name)
        project.shared_skills.append(shared)
        shared_ref = f"shared/{shared.name}"
        for a in users:
            # drop each agent's OWN ref for this content (its local name may differ
            # from the canonical shared name), then add one shared ref
            local_names = {s.name for s in a.local_skills if s.content_hash == chash}
            a.local_skills = [s for s in a.local_skills if s.content_hash != chash]
            a.skill_refs = [r for r in a.skill_refs if r not in local_names]
            if shared_ref not in a.skill_refs:
                a.skill_refs.append(shared_ref)

    # --- mcp servers (full identity, so the name is part of the key) ---
    mcp_users: dict[tuple, list[ImportedAgent]] = {}
    mcp_by_key: dict[tuple, ImportedMcp] = {}
    for a in agents:
        for srv in a.local_mcp:
            key = _mcp_identity(srv)
            if a not in mcp_users.setdefault(key, []):
                mcp_users[key].append(a)
            mcp_by_key[key] = srv
    for key, users in mcp_users.items():
        if len(users) < 2:
            continue
        shared = mcp_by_key[key]
        project.shared_mcp.append(shared)
        shared_ref = f"shared/{shared.name}"
        for a in users:
            a.local_mcp = [s for s in a.local_mcp if _mcp_identity(s) != key]
            a.mcp_refs = [r for r in a.mcp_refs if r != shared.name]
            if shared_ref not in a.mcp_refs:
                a.mcp_refs.append(shared_ref)

    project.shared_skills.sort(key=lambda s: s.name)
    project.shared_mcp.sort(key=lambda s: s.name)


# --------------------------------------------------------------------------- #
# Anthropic import (inverse of planner._build_agent_create)
# --------------------------------------------------------------------------- #
def import_anthropic_agents(
    agents_raw: list[dict[str, Any]],
    skill_bundles: dict[str, ImportedSkill],   # skill_id -> downloaded bundle
    diags: Optional[Diagnostics] = None,
) -> ImportedProject:
    """Map retrieved `BetaManagedAgentsAgent` dicts to an `ImportedProject`.

    `skill_bundles` carries the already-downloaded custom-skill content, keyed by the
    `skill_id` the agent references (the network layer fetched these).
    """
    diags = diags or Diagnostics()
    project = ImportedProject(diagnostics=diags, source="anthropic")

    # roster references carry agent ids; build id -> name to resolve subagents
    id_to_name = {a.get("id"): a.get("name") for a in agents_raw if a.get("id")}

    for raw in agents_raw:
        name = raw["name"]
        builtin_tokens, builtin_policies, mcp_filters = decode_tools(
            raw.get("tools") or [], name, diags)

        # --- skills (custom only; Anthropic-managed skills are reference-only) ---
        local_skills: list[ImportedSkill] = []
        skill_refs: list[str] = []
        for sref in raw.get("skills") or []:
            sid = sref.get("skill_id") or sref.get("id")
            if sref.get("type") == "anthropic":
                diags.warning(
                    "import.anthropic_skill_ref",
                    f"skill '{sid}' is an Anthropic-managed (first-party) skill; it is "
                    f"referenced by id with no downloadable content — not re-imported",
                    name,
                )
                continue
            bundle = skill_bundles.get(sid)
            if bundle is None:
                diags.warning(
                    "import.skill_missing",
                    f"custom skill '{sid}' had no downloadable bundle — skipped",
                    name,
                )
                continue
            local_skills.append(bundle)
            if bundle.name not in skill_refs:   # an agent may list a skill twice (e.g. two versions)
                skill_refs.append(bundle.name)

        # --- mcp servers (URL defs + the tool filter from the toolset) ---
        local_mcp: list[ImportedMcp] = []
        mcp_refs: list[str] = []
        seen_servers = set()
        for srv in raw.get("mcp_servers") or []:
            sname = srv["name"]
            seen_servers.add(sname)
            flt = mcp_filters.get(sname, {})
            # Anthropic managed URL MCP servers carry no inline auth (the deploy path
            # drops it), so there are no auth env names to recover here.
            local_mcp.append(ImportedMcp(
                name=sname, url=srv.get("url"), transport="url",
                allowed_tools=flt.get("allowed"), tool_policies=flt.get("policies") or {},
            ))
            mcp_refs.append(sname)
        for sname in mcp_filters:
            if sname not in seen_servers:
                diags.warning(
                    "import.mcp_no_url",
                    f"mcp_toolset '{sname}' has no matching server URL definition — "
                    f"tool filter kept, but the server URL is unknown",
                    name,
                )

        # --- subagents (resolve roster ids -> names) ---
        subagents: list[str] = []
        multi = raw.get("multiagent")
        if multi and multi.get("agents"):
            for ref in multi["agents"]:
                if isinstance(ref, dict):
                    resolved = ref.get("name") or id_to_name.get(ref.get("id")) or ref.get("id")
                else:
                    resolved = id_to_name.get(ref, ref)
                if resolved:
                    subagents.append(resolved)
                else:
                    diags.warning(
                        "import.subagent_unresolved",
                        "a coordinator roster entry had no resolvable agent name/id — skipped",
                        name,
                    )

        model = raw.get("model")
        if isinstance(model, dict):
            model = model.get("model")

        project.agents.append(ImportedAgent(
            name=name,
            system=(raw.get("system") or "").strip(),
            model=model or "claude-haiku-4-5",
            description=raw.get("description"),
            builtin_tools=builtin_tokens,
            builtin_tool_policies=builtin_policies,
            local_skills=local_skills,
            local_mcp=local_mcp,
            skill_refs=skill_refs,
            mcp_refs=mcp_refs,
            subagents=subagents,
            provider_id=raw.get("id", ""),
            raw_model=model or "",
        ))

    _hoist_shared(project.agents, project)
    return project


# --------------------------------------------------------------------------- #
# Bedrock harness import (inverse of harness_plan.build_harness_plan)
# --------------------------------------------------------------------------- #
# harness tool `type` -> the builtin tokens it stands for (mirror of the forward map)
_HARNESS_BUILTIN = {
    "agentCoreBrowser": ["web_fetch", "web_search"],
    "agentCoreCodeInterpreter": ["bash", "read", "write", "glob", "grep"],
}


def import_bedrock_harness(
    harness: dict[str, Any],
    skill_bundles: dict[str, ImportedSkill],   # s3 uri -> downloaded bundle
    diags: Optional[Diagnostics] = None,
) -> ImportedProject:
    """Map a `GetHarness` response to a single-agent `ImportedProject`.

    A harness is config-only and single-agent, so this never produces subagents
    (a multi-agent team lives in a Runtime container, which is opaque and not
    importable — that boundary is the runtime analogue of the deploy one).
    """
    diags = diags or Diagnostics()
    project = ImportedProject(diagnostics=diags, source="bedrock-harness")

    name = harness.get("harnessName") or "imported-harness"
    system = "\n".join(b.get("text", "") for b in (harness.get("systemPrompt") or [])).strip()
    model_cfg = (harness.get("model") or {}).get("bedrockModelConfig") or {}
    raw_model = model_cfg.get("modelId", "")
    model = reverse_bedrock_model(raw_model)
    if raw_model and not raw_model.startswith(("eu.anthropic", "us.anthropic", "apac.anthropic", "global.anthropic")):
        diags.info(
            "import.bedrock_model_passthrough",
            f"harness model '{raw_model}' is not a Claude inference profile; kept verbatim",
            name,
        )

    builtin: list[str] = []
    builtin_policies: dict[str, str] = {}
    local_mcp: list[ImportedMcp] = []
    mcp_refs: list[str] = []
    allowed_tools = harness.get("allowedTools") or None
    for tool in harness.get("tools") or []:
        ttype = tool.get("type")
        cfg = tool.get("config") or {}
        if ttype == "remote_mcp" or "remoteMcp" in cfg:
            rmcp = cfg.get("remoteMcp") or {}
            sname = tool.get("name") or "mcp"
            auth_names = sorted((rmcp.get("headers") or {}).keys())
            local_mcp.append(ImportedMcp(
                name=sname, url=rmcp.get("url"), transport="url",
                allowed_tools=None, auth_env_names=auth_names,
            ))
            mcp_refs.append(sname)
            if auth_names:
                diags.info(
                    "import.mcp_auth_env",
                    f"MCP server '{sname}' carried auth header(s) {auth_names}; values are "
                    f"provider-side — only the header name is recorded",
                    name,
                )
        elif ttype in _HARNESS_BUILTIN or any(k in cfg for k in _HARNESS_BUILTIN):
            key = ttype if ttype in _HARNESS_BUILTIN else next(k for k in _HARNESS_BUILTIN if k in cfg)
            for tok in _HARNESS_BUILTIN[key]:
                if tok not in builtin:
                    builtin.append(tok)
        elif ttype == "inlineFunction" or "inlineFunction" in cfg:
            diags.warning(
                "import.inline_function_dropped",
                f"harness inline function '{tool.get('name', '?')}' is not representable "
                f"in the folder — dropped",
                name,
            )

    # --- skills (downloaded from each skills[].s3.uri) ---
    local_skills: list[ImportedSkill] = []
    skill_refs: list[str] = []
    for sk in harness.get("skills") or []:
        uri = (sk.get("s3") or {}).get("uri")
        bundle = skill_bundles.get(uri)
        if bundle is None:
            diags.warning(
                "import.skill_missing",
                f"skill at '{uri}' had no downloadable bundle — skipped",
                name,
            )
            continue
        local_skills.append(bundle)
        skill_refs.append(bundle.name)

    # the harness `allowedTools` narrows the builtin set, when present. Honor the filter
    # literally (an empty intersection means none survive — do NOT fall back to the full
    # set, which would silently re-grant tools the allowlist excluded).
    builtin_tokens: Optional[list[str]] = builtin or None
    if allowed_tools is not None and builtin_tokens is not None:
        builtin_tokens = [t for t in builtin_tokens if t in allowed_tools]

    project.agents.append(ImportedAgent(
        name=name,
        system=system,
        model=model or "claude-haiku-4-5",
        description=harness.get("description"),
        builtin_tools=builtin_tokens,
        builtin_tool_policies=builtin_policies,
        local_skills=local_skills,
        local_mcp=local_mcp,
        skill_refs=skill_refs,
        mcp_refs=mcp_refs,
        subagents=[],
        provider_id=harness.get("harnessId", ""),
        raw_model=raw_model,
    ))
    return project
