"""The pure Google deploy plan: skills become shipped bundles, URL MCP servers
become McpToolset recipes, stdio/:ask/builtin/auth gaps surface as diagnostics,
and the spec hash is a stable function of the folder + deploy model. No network."""
import os

from agentlift.google_plan import (
    ADK_WEB_REQUIREMENT,
    DEFAULT_GOOGLE_MODEL,
    build_google_plan,
    safe_ident,
    web_tool_agent_name,
)
from agentlift.model import AgentSpec, Project
from agentlift.parser import parse_project

# the focused web-tools fixture lives under the live tree but parses offline
WEB_TOOLS_FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "live", "fixtures", "web-tools"
)


def _plan(path, **kw):
    project, diags = parse_project(path)
    return build_google_plan(project, diags, **kw), project


def _team(examples_dir, **kw):
    return _plan(os.path.join(examples_dir, "team"), **kw)


def _web(**kw):
    return _plan(WEB_TOOLS_FIXTURE, **kw)


def _node(plan, name):
    return next(n for n in plan.agents if n.name == name)


def _codes(plan):
    return [d.code for d in plan.diagnostics.items]


# --- shape ----------------------------------------------------------------- #
def test_team_plan_is_deployable_one_engine(examples_dir):
    plan, _ = _team(examples_dir)
    assert plan.deployable
    assert plan.root_agent == "lead"
    assert plan.display_name == "agentlift-lead"
    assert {n.name for n in plan.agents} == {"lead", "bug-finder", "researcher"}
    # roster defined before the coordinator so codegen can reference sub_agents
    assert plan.agents[-1].name == "lead"
    assert plan.agents[-1].is_coordinator
    assert set(plan.agents[-1].sub_agents) == {"bug-finder", "researcher"}


def test_default_model_and_remap_info(examples_dir):
    plan, _ = _team(examples_dir)
    assert plan.deploy_model == DEFAULT_GOOGLE_MODEL
    # every Claude-origin agent keeps its folder id in the node (resolved at runtime)
    assert _node(plan, "researcher").folder_model == "claude-haiku-4-5"
    codes = [d.code for d in plan.diagnostics.items]
    assert "google.model.remapped" in codes


def test_custom_model_changes_spec_hash(examples_dir):
    plan_a, _ = _team(examples_dir)
    plan_b, _ = _team(examples_dir, model="gemini-2.5-pro")
    assert plan_a.spec_hash != plan_b.spec_hash
    assert plan_b.deploy_model == "gemini-2.5-pro"


# --- skills become shipped bundles ----------------------------------------- #
def test_skills_become_dedup_bundles(examples_dir):
    plan, _ = _team(examples_dir)
    names = {b.name for b in plan.skill_bundles}
    assert names == {"bug-report", "cite-sources"}
    cite = next(b for b in plan.skill_bundles if b.name == "cite-sources")
    # cite-sources is shared by both researcher and bug-finder -> one bundle, two users
    assert set(cite.used_by) == {"bug-finder", "researcher"}
    # each node lists the skill dirs it loads
    assert "cite-sources" in _node(plan, "researcher").skills
    assert set(_node(plan, "bug-finder").skills) == {"bug-report", "cite-sources"}


def test_skill_bundle_files_carry_skill_md(examples_dir):
    plan, _ = _team(examples_dir)
    cite = next(b for b in plan.skill_bundles if b.name == "cite-sources")
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
    assert servers["search"].tool_filter == ["query"]
    # no inline auth in the team example
    assert servers["docs"].auth_env_vars == {}
    assert plan.env_var_names == []


def test_builtin_tools_flagged_degraded_not_dropped(examples_dir):
    plan, _ = _team(examples_dir)
    degraded = [d for d in plan.diagnostics.warnings if d.code == "google.builtin.degraded"]
    # researcher (read, web_search) and bug-finder (read, glob, grep, bash) both use builtins
    flagged = {d.where for d in degraded}
    assert {"researcher", "bug-finder"} <= flagged


# --- web built-ins lower to wrapped tool-agents ---------------------------- #
def test_web_search_lowers_on_team(examples_dir):
    plan, _ = _team(examples_dir)
    # researcher (read, web_search) -> web_search lowers; read stays a degraded gap
    assert _node(plan, "researcher").builtin_web == ["web_search"]
    # bug-finder (read, glob, grep, bash) has no web tool
    assert _node(plan, "bug-finder").builtin_web == []
    # the mapping is surfaced as an info diagnostic, scoped to the agent
    mapped = [d for d in plan.diagnostics.items if d.code == "google.builtin.web_mapped"]
    assert any(d.where == "researcher" for d in mapped)


def test_web_fetch_and_both_lower_on_fixture():
    plan, _ = _web()
    assert plan.deployable
    # coordinator can itself carry web_search alongside its transfer tools
    assert _node(plan, "lead").builtin_web == ["web_search"]
    assert _node(plan, "lead").is_coordinator
    # search-only leaf
    assert _node(plan, "searcher").builtin_web == ["web_search"]
    # both web tools on one agent, sorted (web_fetch before web_search)
    assert _node(plan, "fetcher").builtin_web == ["web_fetch", "web_search"]


def test_web_only_folder_has_no_sandbox_degradation():
    # the web-tools fixture uses ONLY web built-ins -> web_mapped info, never the
    # builtin.degraded warning (that is reserved for bash/files/glob-grep).
    plan, _ = _web()
    codes = _codes(plan)
    assert "google.builtin.web_mapped" in codes
    assert "google.builtin.degraded" not in codes


def test_adk_web_requirement_added_only_when_web_present():
    web_plan, _ = _web()
    assert ADK_WEB_REQUIREMENT in web_plan.requirements
    # a folder with no web tool must NOT pin the web ADK floor
    no_web = Project(root="x", layout="single", agents=[
        AgentSpec(name="a", system="hi", model="claude-haiku-4-5", builtin_tools=["read", "bash"]),
    ])
    plan = build_google_plan(no_web)
    assert ADK_WEB_REQUIREMENT not in plan.requirements


def test_builtin_web_enters_spec_hash():
    base = Project(root="x", layout="single", agents=[
        AgentSpec(name="a", system="hi", model="claude-haiku-4-5", builtin_tools=["read"]),
    ])
    withweb = Project(root="x", layout="single", agents=[
        AgentSpec(name="a", system="hi", model="claude-haiku-4-5", builtin_tools=["read", "web_search"]),
    ])
    assert build_google_plan(base).spec_hash != build_google_plan(withweb).spec_hash


def test_web_tool_agent_name_is_function_safe():
    # scoped by the owning agent, valid identifier, distinct per tool
    assert web_tool_agent_name("researcher", "web_search") == "researcher_web_search"
    assert web_tool_agent_name("researcher", "web_fetch") == "researcher_web_fetch"
    # leading digit gets a safe prefix
    assert web_tool_agent_name("1bot", "web_search")[0] == "_"
    # very long parent name is truncated with a stable hash suffix, <= 63 chars
    long_name = web_tool_agent_name("a" * 80, "web_search")
    assert len(long_name) <= 63
    assert long_name == web_tool_agent_name("a" * 80, "web_search")  # deterministic


def test_ask_policy_surfaces_as_unsupported(examples_dir):
    # bug-finder has bash:ask (builtin) -> approval unsupported on Agent Engine
    plan, _ = _team(examples_dir)
    approval = [d for d in plan.diagnostics.warnings if d.code == "google.tool_approval.unsupported"]
    assert any(d.where == "bug-finder" for d in approval)


# --- inline auth maps to engine env vars, never inlined -------------------- #
def test_inline_auth_maps_to_named_env_var(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "mcp-auth"))
    assert plan.deployable
    api = _node(plan, "api")
    secure = next(r for r in api.mcp if r.server == "secure")
    assert secure.auth_env_vars == {"Authorization": "AGENTLIFT_MCP_SECURE_AUTHORIZATION"}
    assert plan.env_var_names == ["AGENTLIFT_MCP_SECURE_AUTHORIZATION"]
    # the secret value/template must NOT appear anywhere in the plan's hashable content
    import json
    blob = json.dumps(plan.to_hashable())
    assert "SECURE_API_TOKEN" not in blob
    assert "Bearer" not in blob
    # but the mapping IS surfaced to the user
    assert any(d.code == "google.mcp.auth_via_env" for d in plan.diagnostics.warnings)


# --- stdio MCP is unsupported ---------------------------------------------- #
def test_stdio_mcp_errors_by_default(fixtures_dir):
    # gmail-agent's .mcp.json declares a stdio (command/npx) server
    plan, _ = _plan(os.path.join(fixtures_dir, "gmail-agent"))
    assert not plan.deployable
    assert any(d.code == "google.mcp.stdio_unsupported" for d in plan.diagnostics.errors)


def test_stdio_mcp_skipped_with_flag(fixtures_dir):
    plan, _ = _plan(os.path.join(fixtures_dir, "gmail-agent"), skip_unsupported=True)
    assert plan.deployable
    assert any(d.code == "google.mcp.stdio_skipped" for d in plan.diagnostics.warnings)
    # no recipe for the skipped stdio server
    assert all(not n.mcp for n in plan.agents)


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
    # machine-specific absolute paths must not leak into the hash basis
    assert "C:\\" not in blob and "/Users/" not in blob and examples_dir not in blob


def test_safe_ident():
    assert safe_ident("bug-finder") == "bug_finder"
    assert safe_ident("a.b c") == "a_b_c"


def test_empty_project_not_deployable(tmp_path):
    from agentlift.model import Project
    plan = build_google_plan(Project(root=str(tmp_path), agents=[], layout="single"))
    assert not plan.deployable
    assert any(d.code == "google.project.empty" for d in plan.diagnostics.errors)
