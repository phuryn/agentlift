"""The pure AgentCore *harness* (config-only managed agent) deploy plan.

Mirrors ``test_bedrock_plan.py`` for the harness mode: a single managed agent
declared as config (model -> bedrockModelConfig, systemPrompt blocks, remote_mcp +
agentcore_browser tools, an allowedTools glob allowlist), with the harness's
single-agent limits surfaced as diagnostics (subagents NOT_SUPPORTED, >1 agent
NOT_SUPPORTED, skills CONDITIONAL) and ``--mode auto`` routing multi-agent/skill
folders to the runtime. Claude maps NATIVELY to a regional inference profile (no
remap). The spec hash is stable and excludes secrets + abs paths. No network.

The harness wire shape is a documented PREVIEW (no live receipt yet); these tests
pin the *contract* the first live deploy will reconcile."""
import json
import os

from agentlift.diagnostics import Diagnostics
from agentlift.harness_plan import (
    DEFAULT_HARNESS_REGION,
    HARNESS_PREVIEW_REGIONS,
    build_harness_plan,
    safe_harness_name,
    select_bedrock_mode,
)
from agentlift.model import AgentSpec, Project
from agentlift.parser import parse_project


def _plan(path, **kw):
    project, diags = parse_project(path)
    return build_harness_plan(project, diags, **kw), project


def _codes(plan):
    return [d.code for d in plan.diagnostics.items]


# --- mode selection: auto = least-powerful mode that preserves semantics ---- #
def test_auto_picks_harness_for_single_skilless_agent(fixtures_dir):
    project, _ = parse_project(os.path.join(fixtures_dir, "mcp-auth"))
    mode, reason = select_bedrock_mode(project)
    assert mode == "harness"
    assert "single" in reason.lower()


def test_auto_picks_runtime_for_subagents(examples_dir):
    project, _ = parse_project(os.path.join(examples_dir, "team"))
    mode, reason = select_bedrock_mode(project)
    assert mode == "runtime"
    assert "subagent" in reason.lower()


def test_auto_keeps_single_agent_with_skills_on_harness(examples_dir):
    # quickstart: one agent, no subagents, bundles a skill -> the harness now handles
    # skills (uploaded to S3, attached via skills[].s3.uri), so auto keeps it on harness.
    project, _ = parse_project(os.path.join(examples_dir, "quickstart"))
    assert len(project.agents) == 1 and not any(a.subagents for a in project.agents)
    assert any(a.skills for a in project.agents)
    mode, reason = select_bedrock_mode(project)
    assert mode == "harness"
    assert "single agent" in reason.lower()


# --- single-agent happy path (config-only harness) ------------------------- #
def test_single_agent_harness_is_deployable(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))
    assert plan.deployable
    assert plan.mode == "harness"
    assert plan.harness_name == "agentlift_api"
    assert plan.display_name == "agentlift-api"
    # the AWS-preview status is always surfaced; the wire shape is now receipt-verified
    assert "bedrock.harness.preview" in _codes(plan)
    assert plan.live_verified is True


def test_default_region_is_a_preview_region():
    assert DEFAULT_HARNESS_REGION in HARNESS_PREVIEW_REGIONS


def test_default_region_emits_no_region_warning(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))  # default us-west-2
    assert "bedrock.harness.region_preview" not in _codes(plan)


def test_non_preview_region_warns_not_refuses(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"), region="eu-north-1")
    assert plan.deployable  # a warning, never a refusal
    assert "bedrock.harness.region_preview" in _codes(plan)


# --- model: Claude is NATIVE (mapped, not remapped) ------------------------ #
def test_claude_maps_to_regional_profile(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))  # us-west-2
    assert plan.folder_model == "claude-haiku-4-5"
    assert plan.bedrock_model == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert "gemini" not in plan.bedrock_model
    # goes into the create body under bedrockModelConfig.modelId
    body = plan.to_create_body(execution_role_arn="arn:role", client_token="t", mcp_headers={})
    assert body["model"]["bedrockModelConfig"]["modelId"] == plan.bedrock_model


def test_region_changes_resolved_model_and_hash(fixtures_dir):
    west, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"), region="us-west-2")
    central, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"), region="eu-central-1")
    assert west.bedrock_model.startswith("us.")
    assert central.bedrock_model.startswith("eu.")
    assert west.spec_hash != central.spec_hash
    again, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"), region="us-west-2")
    assert west.spec_hash == again.spec_hash  # idempotency basis


# --- MCP url -> remote_mcp; inline auth -> env-var names -------------------- #
def test_url_mcp_becomes_remote_mcp_tool_with_auth_env(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))
    assert [m.server for m in plan.mcp] == ["secure"]
    secure = plan.mcp[0]
    assert secure.url == "https://secure.internal.example.com/mcp"
    assert secure.auth_env_vars == {"Authorization": "AGENTLIFT_MCP_SECURE_AUTHORIZATION"}
    assert plan.env_var_names == ["AGENTLIFT_MCP_SECURE_AUTHORIZATION"]
    assert "bedrock.mcp.auth_via_env" in _codes(plan)
    # the create body carries the RESOLVED header value (deploy-time secret), but
    # only when handed the resolved map -- the plan itself never holds it
    body = plan.to_create_body(
        execution_role_arn="arn:role", client_token="t",
        mcp_headers={"AGENTLIFT_MCP_SECURE_AUTHORIZATION": "Bearer xyz"},
    )
    tool = next(t for t in body["tools"] if t["type"] == "remote_mcp")
    assert tool["name"] == "secure"
    assert tool["config"]["remoteMcp"]["headers"]["Authorization"] == "Bearer xyz"


def test_secret_never_enters_plan(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))
    blob = json.dumps(plan.to_hashable())
    assert "SECURE_API_TOKEN" not in blob
    assert "Bearer" not in blob
    assert "Authorization" in blob  # the header NAME is fine, the value is not present


# --- allowedTools: never restrictive (it suppresses MCP surfacing, live-observed) --- #
def test_no_restrictive_allowlist_even_when_server_narrows(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))  # secure -> allowedTools ["lookup"]
    # a restrictive allowedTools breaks remote-MCP tool surfacing on the harness, so we
    # emit none (all the server's tools surface) and diagnose the un-enforced narrowing.
    assert plan.allowed_tools == []
    assert "bedrock.mcp.tool_filter_unenforced" in _codes(plan)


def test_per_tool_ask_still_flagged_unsupported(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-perm"))  # github: search_issues, create_issue:ask
    assert plan.allowed_tools == []
    assert "bedrock.tool_approval.unsupported" in _codes(plan)


def test_no_allowlist_when_nothing_restricted(tmp_path):
    # a URL server with no allowedTools -> all tools usable -> omit allowedTools
    from agentlift.model import McpServerSpec
    proj = Project(root=str(tmp_path), layout="single", agents=[
        AgentSpec(name="solo", system="s", model="claude-sonnet-4-6", builtin_tools=["read"],
                  mcp_servers=[McpServerSpec(name="open", transport="url",
                                             url="https://x/mcp", allowed_tools=None)]),
    ])
    plan = build_harness_plan(proj)
    assert plan.allowed_tools == []
    assert [m.server for m in plan.mcp] == ["open"]


# --- built-in tools: web -> Browser; sandbox -> native base tools ----------- #
def test_web_builtin_maps_to_browser_tool(tmp_path):
    proj = Project(root=str(tmp_path), layout="single", agents=[
        AgentSpec(name="webby", system="s", model="claude-sonnet-4-6",
                  builtin_tools=["web_search", "read"]),
    ])
    plan = build_harness_plan(proj)
    assert plan.builtin_tool_types == ["agentcore_browser"]
    assert "bedrock.harness.builtin_mapped" in _codes(plan)
    assert "bedrock.harness.builtin_native" in _codes(plan)  # read served by base tools
    body = plan.to_create_body(execution_role_arn="arn:role", client_token="t", mcp_headers={})
    assert any(t["type"] == "agentcore_browser" for t in body["tools"])


def test_sandbox_builtin_adds_no_tool(tmp_path):
    proj = Project(root=str(tmp_path), layout="single", agents=[
        AgentSpec(name="sandy", system="s", model="claude-sonnet-4-6",
                  builtin_tools=["bash", "read", "glob"]),
    ])
    plan = build_harness_plan(proj)
    assert plan.builtin_tool_types == []  # base shell + file_operations cover these
    assert "bedrock.harness.builtin_native" in _codes(plan)


# --- harness single-agent limits: surfaced, never silent ------------------- #
def test_subagents_unsupported_by_default(examples_dir):
    plan, _ = _plan(os.path.join(examples_dir, "team"))
    assert not plan.deployable
    assert "bedrock.harness.subagents_unsupported" in _codes(plan)


def test_subagents_flatten_with_skip(examples_dir):
    plan, _ = _plan(os.path.join(examples_dir, "team"), skip_unsupported=True)
    assert plan.deployable
    codes = _codes(plan)
    assert "bedrock.harness.subagents_skipped" in codes
    assert "bedrock.harness.multi_agent_skipped" in codes
    # only the coordinator survives as the single harness agent
    assert plan.harness_name == "agentlift_lead"


def test_skills_supported_via_s3(examples_dir):
    # quickstart bundles a skill -> the harness deploy is now deployable: agentlift uploads
    # the bundle to S3 and attaches it via skills[].s3.uri (live-verified).
    plan, _ = _plan(os.path.join(examples_dir, "quickstart"))
    assert plan.deployable
    assert "bedrock.harness.skills_via_s3" in _codes(plan)
    assert [s.name for s in plan.skills] == ["receipt-stamp"]
    assert plan.skills[0].content_hash and plan.skills[0].files   # carries files for upload
    # the skill name + content hash are part of the idempotency spec hash
    assert "skills" in plan.to_hashable() and plan.to_hashable()["skills"]
    # knowledge is still inlined into the instruction (harness systemPrompt)
    assert "bedrock.knowledge.inlined" in _codes(plan)


# --- stdio MCP refused (same as runtime / the other targets) --------------- #
def test_stdio_mcp_errors_by_default(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "gmail-agent"))
    assert not plan.deployable
    assert "bedrock.mcp.stdio_unsupported" in _codes(plan)


# --- determinism / hash hygiene -------------------------------------------- #
def test_plan_is_deterministic(fixtures_dir):
    a, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))
    b, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))
    assert a.spec_hash == b.spec_hash
    assert a.to_hashable() == b.to_hashable()


def test_spec_hash_excludes_abs_paths(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))
    blob = json.dumps(plan.to_hashable())
    assert "C:\\" not in blob and "/Users/" not in blob and fixtures_dir not in blob


# --- harness name constraint ^[a-zA-Z][a-zA-Z0-9_]{0,39}$ ------------------- #
def test_safe_harness_name():
    assert safe_harness_name("lead") == "agentlift_lead"
    assert safe_harness_name("bug-finder") == "agentlift_bug_finder"
    long = safe_harness_name("x" * 80)
    assert len(long) <= 40 and long[0].isalpha()


def test_empty_project_not_deployable(tmp_path):
    plan = build_harness_plan(Project(root=str(tmp_path), agents=[], layout="single"))
    assert not plan.deployable
    assert "bedrock.project.empty" in _codes(plan)
