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
from agentlift.parser import parse_project

try:
    import google.adk  # noqa: F401
    import vertexai  # noqa: F401
    _HAS_ADK = True
except Exception:
    _HAS_ADK = False

needs_adk = pytest.mark.skipif(not _HAS_ADK, reason="google-adk / vertexai not installed")


def _team_plan(examples_dir, **kw):
    project, diags = parse_project(os.path.join(examples_dir, "team"))
    return build_google_plan(project, diags, **kw)


def _auth_plan(fixtures_dir, **kw):
    project, diags = parse_project(os.path.join(fixtures_dir, "mcp-auth"))
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
def test_agent_py_executes_and_builds_app(examples_dir, tmp_path, monkeypatch):
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
