"""The generated Agent Engine source package: agent.py compiles, imports only
google-adk + vertexai, builds the right agents, ships skill bundles, never inlines
a secret, and is byte-for-byte deterministic. The 'imports real ADK' tests are
skipped when google-adk isn't installed (kept offline-by-default)."""
import ast
import os

import pytest

from agentlift.google_codegen import (
    APP_SYMBOL,
    MODULE_NAME,
    PACKAGE_NAME,
    ROOT_SYMBOL,
    _pystr,
    render_agent_py,
    render_text_files,
    skill_file_manifest,
    write_package,
)
from agentlift.google_plan import build_google_plan
from agentlift.model import AgentSpec, Project
from agentlift.parser import parse_project

WEB_TOOLS_FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "live", "fixtures", "web-tools"
)

try:
    import google.adk  # noqa: F401
    import vertexai  # noqa: F401
    _HAS_ADK = True
except Exception:
    _HAS_ADK = False

needs_adk = pytest.mark.skipif(not _HAS_ADK, reason="google-adk / vertexai not installed")


@pytest.fixture
def offline_vertex(monkeypatch):
    """Pin a fake project + anonymous credentials so the generated-agent exec tests build
    REAL adk objects without ever touching ADC or the network.

    ``AdkApp.__init__`` eagerly reads ``initializer.global_config.project``; with no project
    set that calls ``google.auth.default()`` and fails on any box without Application
    Default Credentials (i.e. CI). A project alone is not enough -- the initializer still
    resolves credentials -- so we supply ``AnonymousCredentials`` via the public
    ``vertexai.init`` (the same entrypoint the live harness uses). We then booby-trap
    ``google.auth.default`` to raise, so the test actively *proves* the construction path
    asks for no credentials (the trap has teeth: it fires if the project is unset).
    ``register_operations()`` is a static dict, so no network. The Vertex global config is
    snapshotted and restored so the fake project never leaks into other tests."""
    import google.auth
    import vertexai
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.aiplatform import initializer

    saved = dict(initializer.global_config.__dict__)
    vertexai.init(
        project="agentlift-offline-test",
        location="us-central1",
        credentials=AnonymousCredentials(),
    )

    def _no_adc(*a, **k):
        raise AssertionError("google.auth.default() called -- exec path must not touch ADC")

    monkeypatch.setattr(google.auth, "default", _no_adc)
    try:
        yield
    finally:
        initializer.global_config.__dict__.clear()
        initializer.global_config.__dict__.update(saved)


def _team_plan(examples_dir, **kw):
    project, diags = parse_project(os.path.join(examples_dir, "team"))
    return build_google_plan(project, diags, **kw)


def _auth_plan(fixtures_dir, **kw):
    project, diags = parse_project(os.path.join(fixtures_dir, "mcp-auth"))
    return build_google_plan(project, diags, **kw)


def _web_plan(**kw):
    project, diags = parse_project(WEB_TOOLS_FIXTURE)
    return build_google_plan(project, diags, **kw)


# --- agent.py is valid, self-contained python ------------------------------ #
def test_agent_py_compiles(examples_dir):
    code = render_agent_py(_team_plan(examples_dir))
    compile(code, "agent.py", "exec")  # must parse


def test_agent_py_imports_only_adk_and_vertexai(examples_dir):
    code = render_agent_py(_team_plan(examples_dir))
    tree = ast.parse(code)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {n.name.split(".")[0] for n in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    # never import agentlift remotely; only the engine's own deps + stdlib os
    assert "agentlift" not in roots
    assert roots <= {"os", "google", "vertexai"}


def test_agent_py_builds_expected_shape(examples_dir):
    code = render_agent_py(_team_plan(examples_dir))
    assert "LlmAgent(" in code
    assert f"{ROOT_SYMBOL} = " in code
    assert f"{APP_SYMBOL} = AdkApp(" in code
    assert "def vertex_model(" in code and "AGENTLIFT_GOOGLE_MODEL" in code
    # MCP toolsets for the researcher's two url servers
    assert code.count("McpToolset(") == 2
    assert "StreamableHTTPConnectionParams(url='https://example.com/mcp')" in code
    assert "tool_filter=['search']" in code
    # skills load from the shipped bundle, not inlined
    assert "SkillToolset(skills=[" in code
    assert "load_skill_from_dir(" in code
    assert "_skill('cite-sources')" in code
    # coordinator wires sub_agents by variable
    assert "sub_agents=[agent_bug_finder, agent_researcher]" in code


def test_coordinator_defined_after_roster(examples_dir):
    code = render_agent_py(_team_plan(examples_dir))
    # the lead's variable must be assigned after the agents it references
    assert code.index("agent_bug_finder = LlmAgent(") < code.index("agent_lead = LlmAgent(")
    assert code.index("agent_researcher = LlmAgent(") < code.index("agent_lead = LlmAgent(")


# --- web built-ins lower to wrapped AgentTool sub-agents ------------------- #
def test_web_search_emits_agenttool_wrapper(examples_dir):
    code = render_agent_py(_team_plan(examples_dir))
    # conditional imports ride in
    assert "from google.adk.tools.agent_tool import AgentTool" in code
    assert "from google.adk.tools.google_search_tool import GoogleSearchTool" in code
    # the factory + a call scoped to the researcher
    assert "def _web_search_tool(name, model):" in code
    assert "_web_search_tool('researcher_web_search'," in code
    assert "GoogleSearchTool()" in code
    assert "propagate_grounding_metadata=True" in code


def test_search_only_folder_omits_url_context():
    # a folder whose only web tool is web_search must NOT import url_context or emit
    # the fetch factory (conditional-import discipline, no unused symbols)
    search_only = Project(root="x", layout="single", agents=[
        AgentSpec(name="a", system="hi", model="claude-haiku-4-5", builtin_tools=["web_search"]),
    ])
    code = render_agent_py(build_google_plan(search_only))
    compile(code, "agent.py", "exec")
    assert "GoogleSearchTool" in code and "_web_search_tool(" in code
    assert "url_context" not in code
    assert "_web_fetch_tool" not in code


def test_web_fetch_and_both_emit_url_context():
    code = render_agent_py(_web_plan())
    compile(code, "agent.py", "exec")
    assert "from google.adk.tools import url_context" in code
    assert "def _web_fetch_tool(name, model):" in code
    assert "tools=[url_context]" in code
    # both web tools on the fetcher, each scoped by the owning agent's name
    assert "_web_fetch_tool('fetcher_web_fetch'," in code
    assert "_web_search_tool('fetcher_web_search'," in code
    # the coordinator carries web_search alongside its sub_agents (always-wrap so it
    # never collides with the injected transfer tools)
    assert "_web_search_tool('lead_web_search'," in code
    assert "sub_agents=[agent_searcher, agent_fetcher]" in code


def test_web_sub_agents_pin_a_gemini_model():
    # Google Search / URL Context are Gemini built-ins: a wrapped web sub-agent must run
    # on a web-capable Gemini model regardless of the parent agent's (here Claude) model.
    code = render_agent_py(_web_plan())
    compile(code, "agent.py", "exec")
    assert "def web_model(folder_model):" in code
    # web sub-agents are constructed with web_model(...); parent agents with vertex_model(...)
    assert "_web_search_tool('lead_web_search', web_model('claude-haiku-4-5'))" in code
    assert "model=vertex_model('claude-haiku-4-5')" in code
    # the generated web_model forces Gemini for a non-Gemini parent, passes Gemini through
    import re
    ns = {"DEFAULT_VERTEX_MODEL": "gemini-2.5-flash"}
    exec(re.search(r"def web_model\(folder_model\):\n(?:    .*\n)+", code).group(0), ns)
    assert ns["web_model"]("claude-haiku-4-5") == "gemini-2.5-flash"
    assert ns["web_model"]("claude-sonnet-4-5@20250929").startswith("gemini")
    assert ns["web_model"]("gemini-2.5-pro") == "gemini-2.5-pro"


def test_no_web_tools_means_no_web_imports():
    no_web = Project(root="x", layout="single", agents=[
        AgentSpec(name="a", system="hi", model="claude-haiku-4-5", builtin_tools=["read", "bash"]),
    ])
    code = render_agent_py(build_google_plan(no_web))
    compile(code, "agent.py", "exec")
    assert "AgentTool" not in code
    assert "GoogleSearchTool" not in code
    assert "url_context" not in code
    assert "_web_search_tool" not in code and "_web_fetch_tool" not in code


def test_web_agent_py_imports_only_adk_and_vertexai():
    code = render_agent_py(_web_plan())
    tree = ast.parse(code)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {n.name.split(".")[0] for n in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert "agentlift" not in roots
    assert roots <= {"os", "google", "vertexai"}


@needs_adk
def test_web_agent_py_executes_and_wraps_real_adk_tools(tmp_path, offline_vertex):
    # build the REAL ADK objects from the generated source: the wrapped web tools
    # must construct AND coexist with sub_agents/transfer tools on the coordinator.
    plan = _web_plan()
    build = os.path.join(str(tmp_path), "build")
    write_package(plan, build)
    agent_py = os.path.join(build, PACKAGE_NAME, "agent.py")
    src = open(agent_py, encoding="utf-8").read()
    ns: dict = {"__file__": agent_py}
    exec(compile(src, agent_py, "exec"), ns)
    root = ns[ROOT_SYMBOL]
    assert root.name == "lead"
    # the coordinator wraps web_search as an AgentTool tool AND keeps its roster
    tool_names = {getattr(t, "name", None) for t in root.tools}
    assert "lead_web_search" in tool_names
    assert {s.name for s in root.sub_agents} == {"searcher", "fetcher"}
    # the fetcher carries both wrapped web tools
    fetcher = next(s for s in root.sub_agents if s.name == "fetcher")
    fetcher_tools = {getattr(t, "name", None) for t in fetcher.tools}
    assert {"fetcher_web_fetch", "fetcher_web_search"} <= fetcher_tools
    # mixed-model invariant: every folder agent here is Claude, but each wrapped web
    # sub-agent must resolve to a Gemini model (Search/URL-Context are Gemini built-ins)
    web_tool = next(t for t in root.tools if getattr(t, "name", None) == "lead_web_search")
    assert str(web_tool.agent.model).startswith("gemini")
    assert ns["web_model"]("claude-haiku-4-5").startswith("gemini")


# --- secrets never inlined ------------------------------------------------- #
def test_auth_header_reads_env_var_no_secret(fixtures_dir, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "super-secret-value")
    code = render_agent_py(_auth_plan(fixtures_dir))
    compile(code, "agent.py", "exec")
    # value/template never appears; only the env-var NAME is referenced
    assert "super-secret-value" not in code
    assert "SECURE_API_TOKEN" not in code
    assert "Bearer" not in code
    assert "os.environ.get('AGENTLIFT_MCP_SECURE_AUTHORIZATION'" in code
    assert "headers={" in code


# --- determinism ----------------------------------------------------------- #
def test_render_is_deterministic(examples_dir):
    a = render_agent_py(_team_plan(examples_dir))
    b = render_agent_py(_team_plan(examples_dir))
    assert a == b


def test_text_files_layout(examples_dir):
    files = render_text_files(_team_plan(examples_dir))
    assert set(files) == {
        f"{PACKAGE_NAME}/__init__.py",
        f"{PACKAGE_NAME}/agent.py",
        "requirements.txt",
    }
    assert "google-cloud-aiplatform[adk,agent_engines]" in files["requirements.txt"]


def test_skill_manifest_targets_package_skills_dir(examples_dir):
    manifest = skill_file_manifest(_team_plan(examples_dir))
    rels = [r for r, _ in manifest]
    assert any(r.startswith(f"{PACKAGE_NAME}/skills/cite-sources/") for r in rels)
    assert all(os.path.isfile(src) for _, src in manifest)


# --- write_package materializes a usable tree ------------------------------ #
def test_write_package_materializes_tree(examples_dir, tmp_path):
    plan = _team_plan(examples_dir)
    build = os.path.join(str(tmp_path), "build")
    handles = write_package(plan, build)
    assert handles["module_name"] == MODULE_NAME
    assert handles["app_symbol"] == APP_SYMBOL
    assert os.path.isfile(os.path.join(build, PACKAGE_NAME, "agent.py"))
    assert os.path.isfile(os.path.join(build, PACKAGE_NAME, "__init__.py"))
    assert os.path.isfile(os.path.join(build, "requirements.txt"))
    # a skill file landed where load_skill_from_dir(_HERE/skills/<name>) will look
    cite = os.path.join(build, PACKAGE_NAME, "skills", "cite-sources", "SKILL.md")
    assert os.path.isfile(cite)


# --- adversarial string escaping ------------------------------------------- #
@pytest.mark.parametrize("evil", [
    'ends with a quote "',
    'has """ triple quotes inside',
    'a backslash \\ and a quote "',
    'trailing backslash \\',
    'mixed """\\""" chaos "',
])
def test_pystr_always_parses_and_roundtrips(evil):
    src = "X = " + _pystr(evil)
    ns: dict = {}
    exec(compile(src, "t.py", "exec"), ns)  # must parse AND execute
    assert ns["X"] == evil


def test_evil_instruction_in_full_agent_py(examples_dir):
    plan = _team_plan(examples_dir)
    plan.agents[0].instruction = 'be careful with """ and a trailing quote "'
    code = render_agent_py(plan)
    compile(code, "agent.py", "exec")  # whole module still parses


# --- imports against the REAL adk (offline construction, no deploy) -------- #
@needs_adk
def test_agent_py_executes_and_builds_app(examples_dir, tmp_path, offline_vertex):
    # construct the real ADK objects from the generated source -- proves the
    # imports/シグ are right and that MCP/skills build lazily without a network.
    plan = _team_plan(examples_dir)
    build = os.path.join(str(tmp_path), "build")
    write_package(plan, build)
    agent_py = os.path.join(build, PACKAGE_NAME, "agent.py")
    src = open(agent_py, encoding="utf-8").read()
    ns: dict = {"__file__": agent_py}
    exec(compile(src, agent_py, "exec"), ns)
    app = ns[APP_SYMBOL]
    assert ns[ROOT_SYMBOL].name == "lead"
    # AdkApp is the operation-registrable object ModuleAgent will target
    assert hasattr(app, "register_operations")
    ops = app.register_operations()
    assert "stream" in ops
