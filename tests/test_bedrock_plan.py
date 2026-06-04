"""The pure Bedrock (AgentCore Runtime) deploy plan: Claude maps NATIVELY to a
Bedrock regional inference profile (no Gemini-style remap), subagents become one
runtime with sub-agents-as-tools, skills become shipped bundles, URL MCP servers
become Strands MCP recipes, and stdio/:ask/builtin gaps surface as diagnostics.
The spec hash is a stable function of the folder + resolved model. No network.

The model map is grounded in the account's actual eu.anthropic.* inference
profiles (eu-north-1, verified 2026-06-04) -- see experiments/bedrock-composition/."""
import os

from agentlift.bedrock_plan import (
    BOTO3_REQUIREMENT,
    DEFAULT_BEDROCK_REGION,
    RUNTIME_REQUIREMENT,
    STRANDS_REQUIREMENT,
    build_bedrock_plan,
    region_prefix,
    resolve_bedrock_model,
    safe_ident,
)
from agentlift.diagnostics import Diagnostics
from agentlift.model import AgentSpec, Project
from agentlift.parser import parse_project


def _plan(path, **kw):
    project, diags = parse_project(path)
    return build_bedrock_plan(project, diags, **kw), project


def _team(examples_dir, **kw):
    return _plan(os.path.join(examples_dir, "team"), **kw)


def _node(plan, name):
    return next(n for n in plan.agents if n.name == name)


def _codes(plan):
    return [d.code for d in plan.diagnostics.items]


# --- shape ----------------------------------------------------------------- #
def test_team_plan_is_deployable_one_runtime(examples_dir):
    plan, _ = _team(examples_dir)
    assert plan.deployable
    assert plan.root_agent == "lead"
    assert plan.display_name == "agentlift-lead"
    assert {n.name for n in plan.agents} == {"lead", "bug-finder", "researcher"}
    # roster (leaves) defined before the coordinator so codegen defines tools first
    assert plan.agents[-1].name == "lead"
    assert plan.agents[-1].is_coordinator
    assert set(plan.agents[-1].sub_agents) == {"bug-finder", "researcher"}
    # the runtime/strands requirements are always present
    assert STRANDS_REQUIREMENT in plan.requirements
    assert RUNTIME_REQUIREMENT in plan.requirements
    assert BOTO3_REQUIREMENT in plan.requirements


# --- model: Claude is NATIVE (mapped, not remapped) ------------------------ #
def test_claude_maps_to_regional_inference_profile(examples_dir):
    plan, _ = _team(examples_dir)  # default region eu-north-1
    # claude-haiku-4-5 carries a date-suffixed Bedrock slug (an alias case)
    haiku = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert _node(plan, "researcher").folder_model == "claude-haiku-4-5"
    assert _node(plan, "researcher").bedrock_model == haiku
    # the resolution is surfaced (never a silent guess), scoped to the agent
    resolved = [d for d in plan.diagnostics.items if d.code == "bedrock.model.resolved"]
    assert any(d.where == "researcher" for d in resolved)
    # and crucially NOT remapped to a non-Claude model -- the portability story
    assert all("gemini" not in n.bedrock_model for n in plan.agents)


def test_bare_id_models_need_no_alias():
    diags = Diagnostics()
    # the newest folder ids ARE the Bedrock profile slugs (no date suffix)
    assert resolve_bedrock_model("claude-sonnet-4-6", "eu-north-1", diags) == \
        "eu.anthropic.claude-sonnet-4-6"
    assert resolve_bedrock_model("claude-opus-4-8", "eu-north-1", diags) == \
        "eu.anthropic.claude-opus-4-8"


def test_region_prefix_selects_profile_family():
    assert region_prefix("eu-north-1") == "eu"
    assert region_prefix("us-east-1") == "us"
    assert region_prefix("ap-southeast-2") == "apac"
    assert region_prefix("ca-central-1") == "global"  # fallback to the always-on family
    diags = Diagnostics()
    assert resolve_bedrock_model("claude-sonnet-4-6", "us-east-1", diags) == \
        "us.anthropic.claude-sonnet-4-6"


def test_region_changes_resolved_model_and_hash(examples_dir):
    eu, _ = _team(examples_dir, region="eu-north-1")
    us, _ = _team(examples_dir, region="us-east-1")
    assert _node(eu, "researcher").bedrock_model.startswith("eu.")
    assert _node(us, "researcher").bedrock_model.startswith("us.")
    # different regional profile id => genuinely different artifact => different hash
    assert eu.spec_hash != us.spec_hash
    # but re-deploying to the same region is stable (idempotency basis)
    eu2, _ = _team(examples_dir, region="eu-north-1")
    assert eu.spec_hash == eu2.spec_hash


def test_non_claude_model_passes_through_with_warning():
    diags = Diagnostics()
    out = resolve_bedrock_model("eu.amazon.nova-pro-v1:0", "eu-north-1", diags)
    assert out == "eu.amazon.nova-pro-v1:0"  # verbatim, not Claude-prefixed
    assert any(d.code == "bedrock.model.non_claude" for d in diags.items)


# --- skills become shipped bundles ----------------------------------------- #
def test_skills_become_dedup_bundles(examples_dir):
    plan, _ = _team(examples_dir)
    names = {b.name for b in plan.skill_bundles}
    assert names == {"bug-report", "cite-sources"}
    cite = next(b for b in plan.skill_bundles if b.name == "cite-sources")
    assert set(cite.used_by) == {"bug-finder", "researcher"}
    assert "cite-sources" in _node(plan, "researcher").skills
    assert set(_node(plan, "bug-finder").skills) == {"bug-report", "cite-sources"}
    arcnames = [a for a, _ in cite.files]
    assert any(a.endswith("SKILL.md") for a in arcnames)
    assert all(a.startswith("cite-sources/") for a in arcnames)


# --- MCP url servers become recipes ---------------------------------------- #
def test_url_mcp_becomes_recipe_with_tool_filter(examples_dir):
    plan, _ = _team(examples_dir)
    researcher = _node(plan, "researcher")
    servers = {r.server: r for r in researcher.mcp}
    assert set(servers) == {"docs", "search"}
    assert servers["docs"].url == "https://example.com/mcp"
    assert servers["docs"].tool_filter == ["search"]
    assert servers["docs"].auth_env_vars == {}
    assert plan.env_var_names == []


def test_inline_auth_maps_to_named_env_var(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))
    assert plan.deployable
    api = _node(plan, "api")
    secure = next(r for r in api.mcp if r.server == "secure")
    assert secure.auth_env_vars == {"Authorization": "AGENTLIFT_MCP_SECURE_AUTHORIZATION"}
    assert plan.env_var_names == ["AGENTLIFT_MCP_SECURE_AUTHORIZATION"]
    # the secret value/template must NOT appear in the plan's hashable content
    import json
    blob = json.dumps(plan.to_hashable())
    assert "SECURE_API_TOKEN" not in blob
    assert "Bearer" not in blob
    assert any(d.code == "bedrock.mcp.auth_via_env" for d in plan.diagnostics.warnings)


def test_stdio_mcp_errors_by_default(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "gmail-agent"))
    assert not plan.deployable
    assert any(d.code == "bedrock.mcp.stdio_unsupported" for d in plan.diagnostics.errors)


def test_stdio_mcp_skipped_with_flag(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "gmail-agent"), skip_unsupported=True)
    assert plan.deployable
    assert any(d.code == "bedrock.mcp.stdio_skipped" for d in plan.diagnostics.warnings)
    assert all(not n.mcp for n in plan.agents)


# --- built-in tools: surfaced as planned (not yet mapped), never dropped ---- #
def test_builtin_tools_flagged_planned_not_dropped(examples_dir):
    plan, _ = _team(examples_dir)
    # bug-finder (read, glob, grep, bash) -> sandbox planned; researcher has web_search
    sandbox = {d.where for d in plan.diagnostics.warnings if d.code == "bedrock.builtin.sandbox_planned"}
    web = {d.where for d in plan.diagnostics.warnings if d.code == "bedrock.builtin.web_planned"}
    assert "bug-finder" in sandbox
    assert "researcher" in web


def test_ask_policy_surfaces_as_unsupported(examples_dir):
    plan, _ = _team(examples_dir)
    approval = [d for d in plan.diagnostics.warnings if d.code == "bedrock.tool_approval.unsupported"]
    assert any(d.where == "bug-finder" for d in approval)


# --- determinism / idempotency basis --------------------------------------- #
def test_plan_is_deterministic(examples_dir):
    a, _ = _team(examples_dir)
    b, _ = _team(examples_dir)
    assert a.spec_hash == b.spec_hash
    assert a.to_hashable() == b.to_hashable()


def test_spec_hash_excludes_abs_paths(examples_dir):
    plan, _ = _team(examples_dir)
    import json
    blob = json.dumps(plan.to_hashable())
    assert "C:\\" not in blob and "/Users/" not in blob and examples_dir not in blob


def test_safe_ident():
    assert safe_ident("bug-finder") == "bug_finder"
    assert safe_ident("a.b c") == "a_b_c"


def test_empty_project_not_deployable(tmp_path):
    plan = build_bedrock_plan(Project(root=str(tmp_path), agents=[], layout="single"))
    assert not plan.deployable
    assert any(d.code == "bedrock.project.empty" for d in plan.diagnostics.errors)


def test_subagent_depth_is_rejected():
    # a sub_agent that is itself a coordinator -> depth-2; this target is depth-1
    proj = Project(root="x", layout="single", agents=[
        AgentSpec(name="root", system="s", model="claude-sonnet-4-6", subagents=["mid"]),
        AgentSpec(name="mid", system="s", model="claude-sonnet-4-6", subagents=["leaf"]),
        AgentSpec(name="leaf", system="s", model="claude-sonnet-4-6"),
    ])
    plan = build_bedrock_plan(proj)
    assert not plan.deployable
    assert any(d.code == "bedrock.subagent.depth" for d in plan.diagnostics.errors)
