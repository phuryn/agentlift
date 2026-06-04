"""The generated AgentCore Runtime source package: agent.py compiles, imports only
strands + mcp + bedrock-agentcore, builds the right request-scoped agent graph,
ships skill bundles, never inlines a secret, and is byte-for-byte deterministic.

Two execution layers beyond the string/shape assertions:
  * a FAKE-module exec that needs no heavy deps -- it drives ``_handle_invocation``
    with a fake root ``Agent.__call__`` that invokes every generated tool once, so
    the sub-agent delegation path AND the per-request MCP ExitStack lifecycle
    actually execute under test (the honest assertion: the graph really runs).
  * a REAL-framework exec (skipped unless strands + bedrock-agentcore are
    installed) that builds the actual ``BedrockAgentCoreApp`` and checks the
    ``/ping`` + ``/invocations`` contract via a Starlette TestClient -- no model,
    no network (agents build lazily inside the entrypoint, not at import)."""
import ast
import os
import sys
import types

import pytest

from agentlift.bedrock_codegen import (
    APP_SYMBOL,
    HANDLER_SYMBOL,
    MODULE_NAME,
    PACKAGE_NAME,
    _pystr,
    render_agent_py,
    render_text_files,
    skill_file_manifest,
    write_package,
)
from agentlift.bedrock_plan import build_bedrock_plan
from agentlift.model import AgentSpec, McpServerSpec, Project
from agentlift.parser import parse_project


def _team_plan(examples_dir, **kw):
    project, diags = parse_project(os.path.join(examples_dir, "team"))
    return build_bedrock_plan(project, diags, **kw)


def _auth_plan(fixtures_dir, **kw):
    project, diags = parse_project(os.path.join(fixtures_dir, "mcp-auth"))
    return build_bedrock_plan(project, diags, **kw)


# --- agent.py is valid, self-contained python ------------------------------ #
def test_agent_py_compiles(examples_dir):
    compile(render_agent_py(_team_plan(examples_dir)), "agent.py", "exec")


def test_agent_py_imports_only_runtime_deps(examples_dir):
    code = render_agent_py(_team_plan(examples_dir))
    tree = ast.parse(code)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {n.name.split(".")[0] for n in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert "agentlift" not in roots  # never import agentlift in the runtime container
    assert roots <= {"os", "contextlib", "strands", "mcp", "bedrock_agentcore"}


def test_agent_py_builds_expected_shape(examples_dir):
    code = render_agent_py(_team_plan(examples_dir))
    # the AgentCore server contract
    assert f"{APP_SYMBOL} = BedrockAgentCoreApp()" in code
    assert f"@{APP_SYMBOL}.entrypoint" in code
    assert f"def {HANDLER_SYMBOL}(payload):" in code
    assert "app.run(port=8080)" in code
    # request-scoped graph
    assert "with ExitStack() as stack:" in code
    assert "class _Invocation:" in code
    assert "def _build_lead_agent(ctx):" in code
    # sub-agents are named @tool factories the coordinator wires up
    assert "def _make_researcher_tool(ctx):" in code
    assert "def _make_bug_finder_tool(ctx):" in code
    assert "tools.append(_make_researcher_tool(ctx))" in code
    assert "tools.append(_make_bug_finder_tool(ctx))" in code
    # model is the regional Claude profile (native, not remapped)
    assert "_model('eu.anthropic.claude-haiku-4-5-20251001-v1:0')" in code
    # skills load from the shipped bundle, not inlined
    assert "AgentSkills(skills=[" in code
    assert "_skill('cite-sources')" in code


def test_mcp_uses_server_prefix_and_raw_allowlist(examples_dir):
    code = render_agent_py(_team_plan(examples_dir))
    # researcher's docs server: prefixed by server name, allowlist stays RAW
    assert (
        "ctx.mcp_tools(server='docs', url='https://example.com/mcp', "
        "prefix='docs', allowed=['search'], auth_env={})"
    ) in code
    # the private search server keeps its own raw allowlist + its own prefix
    assert "prefix='search', allowed=['query']" in code


def test_unfiltered_mcp_server_passes_allowed_none():
    # a url server with no allowedTools exposes every tool -> allowed=None
    proj = Project(root="x", layout="single", agents=[
        AgentSpec(
            name="solo", system="s", model="claude-sonnet-4-6",
            mcp_servers=[McpServerSpec(name="open", transport="url",
                                       url="https://open.example.com/mcp")],
        ),
    ])
    code = render_agent_py(build_bedrock_plan(proj))
    assert "prefix='open', allowed=None" in code


def test_coordinator_builder_defined_after_leaves(examples_dir):
    code = render_agent_py(_team_plan(examples_dir))
    # the leaf builders/factories must be defined before the coordinator references them
    assert code.index("def _build_researcher_agent(ctx):") < code.index("def _build_lead_agent(ctx):")
    assert code.index("def _make_bug_finder_tool(ctx):") < code.index("def _build_lead_agent(ctx):")


def test_single_agent_has_no_factories():
    solo = Project(root="x", layout="single", agents=[
        AgentSpec(name="solo", system="hi", model="claude-sonnet-4-6"),
    ])
    code = render_agent_py(build_bedrock_plan(solo))
    compile(code, "agent.py", "exec")
    assert "def _build_solo_agent(ctx):" in code
    assert "_make_" not in code  # no delegation tools when there's no roster
    assert "root = _build_solo_agent(ctx)" in code


# --- secrets never inlined ------------------------------------------------- #
def test_auth_header_reads_env_var_no_secret(fixtures_dir, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "super-secret-value")
    code = render_agent_py(_auth_plan(fixtures_dir))
    compile(code, "agent.py", "exec")
    assert "super-secret-value" not in code
    assert "SECURE_API_TOKEN" not in code
    assert "Bearer" not in code
    # only the env-var NAME rides along, read at invocation by the runtime helper
    assert "'Authorization': 'AGENTLIFT_MCP_SECURE_AUTHORIZATION'" in code
    assert 'os.environ.get(env, "")' in code


# --- determinism / packaging ----------------------------------------------- #
def test_render_is_deterministic(examples_dir):
    assert render_agent_py(_team_plan(examples_dir)) == render_agent_py(_team_plan(examples_dir))


def test_text_files_layout(examples_dir):
    files = render_text_files(_team_plan(examples_dir))
    assert set(files) == {
        f"{PACKAGE_NAME}/__init__.py",
        f"{PACKAGE_NAME}/agent.py",
        "requirements.txt",
    }
    reqs = files["requirements.txt"]
    assert "strands-agents>=1.42" in reqs
    assert "bedrock-agentcore" in reqs
    assert "boto3>=1.40" in reqs


def test_skill_manifest_targets_package_skills_dir(examples_dir):
    manifest = skill_file_manifest(_team_plan(examples_dir))
    rels = [r for r, _ in manifest]
    assert any(r.startswith(f"{PACKAGE_NAME}/skills/cite-sources/") for r in rels)
    assert all(os.path.isfile(src) for _, src in manifest)


def test_write_package_materializes_tree(examples_dir, tmp_path):
    plan = _team_plan(examples_dir)
    build = os.path.join(str(tmp_path), "build")
    handles = write_package(plan, build)
    assert handles["module_name"] == MODULE_NAME
    assert handles["app_symbol"] == APP_SYMBOL
    assert handles["handler_symbol"] == HANDLER_SYMBOL
    assert os.path.isfile(os.path.join(build, PACKAGE_NAME, "agent.py"))
    assert os.path.isfile(os.path.join(build, PACKAGE_NAME, "__init__.py"))
    assert os.path.isfile(os.path.join(build, "requirements.txt"))
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
    ns: dict = {}
    exec(compile("X = " + _pystr(evil), "t.py", "exec"), ns)
    assert ns["X"] == evil


def test_evil_instruction_in_full_agent_py(examples_dir):
    plan = _team_plan(examples_dir)
    plan.agents[0].instruction = 'be careful with """ and a trailing quote "'
    compile(render_agent_py(plan), "agent.py", "exec")


# --------------------------------------------------------------------------- #
# FAKE-module exec: drive the real generated graph with fakes (no heavy deps).
# The fake root Agent.__call__ invokes every tool once, so the sub-agent
# delegation path + per-request MCP ExitStack lifecycle actually execute.
# --------------------------------------------------------------------------- #
def _install_fakes(monkeypatch):
    rec = {
        "agents": [], "models": [], "mcp": [],
        "enter": [], "exit": [], "skills": [],
    }

    class FakeTool:
        def __init__(self, name): self.name = name
        def __call__(self, *a, **k): return "ok"

    class FakeMCPClient:
        def __init__(self, transport_callable, *, prefix=None, tool_filters=None):
            self._prefix = prefix
            rec["mcp"].append({"prefix": prefix, "tool_filters": tool_filters})
        def __enter__(self):
            rec["enter"].append(self._prefix); return self
        def __exit__(self, *a):
            rec["exit"].append(self._prefix); return False
        def list_tools_sync(self):
            return [FakeTool(f"{self._prefix}_probe")]

    class FakeAgent:
        def __init__(self, **kwargs):
            self.name = kwargs.get("name")
            self.tools = kwargs.get("tools") or []
            self.kwargs = kwargs
            rec["agents"].append(self)
        def __call__(self, prompt):
            for t in self.tools:  # force every tool to fire once
                t("probe")
            return f"[{self.name}:{prompt}]"

    class FakeBedrockModel:
        def __init__(self, *, model_id=None, region_name=None, **k):
            rec["models"].append({"model_id": model_id, "region": region_name})

    class FakeSkill:
        @staticmethod
        def from_file(path):
            rec["skills"].append(path); return ("skill", path)

    class FakeAgentSkills:
        def __init__(self, *, skills=None, **k): self.skills = skills

    def fake_tool(fn):  # strands @tool -> here, identity so the closure stays callable
        return fn

    class FakeApp:
        def __init__(self): self.entrypoints = []
        def entrypoint(self, fn): self.entrypoints.append(fn); return fn
        def run(self, **k): pass

    def fake_streamablehttp_client(url, headers=None):
        return ("transport", url, headers)

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    _mod("strands", Agent=FakeAgent, AgentSkills=FakeAgentSkills, Skill=FakeSkill, tool=fake_tool)
    _mod("strands.models", BedrockModel=FakeBedrockModel)
    _mod("strands.tools")
    _mod("strands.tools.mcp", MCPClient=FakeMCPClient)
    _mod("mcp")
    _mod("mcp.client")
    _mod("mcp.client.streamable_http", streamablehttp_client=fake_streamablehttp_client)
    _mod("bedrock_agentcore")
    _mod("bedrock_agentcore.runtime", BedrockAgentCoreApp=FakeApp)
    return rec


def _exec_generated(plan, monkeypatch, tmp_path):
    rec = _install_fakes(monkeypatch)
    build = os.path.join(str(tmp_path), "b")
    write_package(plan, build)
    agent_py = os.path.join(build, PACKAGE_NAME, "agent.py")
    src = open(agent_py, encoding="utf-8").read()
    ns: dict = {"__file__": agent_py}
    exec(compile(src, agent_py, "exec"), ns)
    return ns, rec


def test_generated_graph_runs_and_delegates(examples_dir, monkeypatch, tmp_path):
    ns, rec = _exec_generated(_team_plan(examples_dir), monkeypatch, tmp_path)
    result = ns[HANDLER_SYMBOL]({"prompt": "hello"})
    # the root ran and produced a result envelope
    assert result == {"result": "[lead:hello]"}
    # every agent in the graph was actually constructed (root + both leaves)
    built = {a.name for a in rec["agents"]}
    assert built == {"lead", "researcher", "bug_finder"}
    # the model pinned to the regional Claude profile, in the plan's region
    assert {m["region"] for m in rec["models"]} == {"eu-north-1"}
    assert any(m["model_id"] == "eu.anthropic.claude-haiku-4-5-20251001-v1:0" for m in rec["models"])


def test_generated_graph_mcp_lifecycle_is_request_scoped(examples_dir, monkeypatch, tmp_path):
    ns, rec = _exec_generated(_team_plan(examples_dir), monkeypatch, tmp_path)
    ns[HANDLER_SYMBOL]({"prompt": "hi"})
    # researcher's two servers opened, with the verified prefix + RAW allowlist shape
    docs = next(c for c in rec["mcp"] if c["prefix"] == "docs")
    assert docs["tool_filters"] == {"allowed": ["search"]}
    search = next(c for c in rec["mcp"] if c["prefix"] == "search")
    assert search["tool_filters"] == {"allowed": ["query"]}
    # every MCP client that was entered was also exited -- the ExitStack tore the
    # whole request graph down deterministically (no leaked connections)
    assert rec["enter"] and sorted(rec["enter"]) == sorted(rec["exit"])


def test_generated_graph_loads_skills_from_bundle(examples_dir, monkeypatch, tmp_path):
    ns, rec = _exec_generated(_team_plan(examples_dir), monkeypatch, tmp_path)
    ns[HANDLER_SYMBOL]({"prompt": "hi"})
    loaded = {os.path.basename(os.path.dirname(p)) for p in rec["skills"]}
    assert {"cite-sources", "bug-report"} <= loaded
    assert all(p.endswith("SKILL.md") for p in rec["skills"])


def test_generated_entrypoint_registered(examples_dir, monkeypatch, tmp_path):
    ns, _ = _exec_generated(_team_plan(examples_dir), monkeypatch, tmp_path)
    app = ns[APP_SYMBOL]
    assert len(app.entrypoints) == 1  # exactly one @app.entrypoint
    # the registered entrypoint delegates to the testable handler
    assert app.entrypoints[0]({"prompt": "z"}) == {"result": "[lead:z]"}


# --------------------------------------------------------------------------- #
# REAL-framework exec: build the actual BedrockAgentCoreApp + check /ping.
# Skipped unless strands + bedrock-agentcore are installed (offline-by-default).
# --------------------------------------------------------------------------- #
try:
    import bedrock_agentcore  # noqa: F401
    import strands  # noqa: F401
    from starlette.testclient import TestClient  # noqa: F401
    _HAS_RUNTIME = True
except Exception:
    _HAS_RUNTIME = False

needs_runtime = pytest.mark.skipif(
    not _HAS_RUNTIME, reason="strands / bedrock-agentcore / starlette not installed"
)


@needs_runtime
def test_real_app_builds_and_serves_ping(examples_dir, tmp_path):
    # exec the generated source against the REAL strands + bedrock-agentcore. Agents
    # build lazily inside the entrypoint, so import touches no model and no network.
    from starlette.testclient import TestClient

    plan = _team_plan(examples_dir)
    build = os.path.join(str(tmp_path), "real")
    write_package(plan, build)
    agent_py = os.path.join(build, PACKAGE_NAME, "agent.py")
    src = open(agent_py, encoding="utf-8").read()
    ns: dict = {"__file__": agent_py}
    exec(compile(src, agent_py, "exec"), ns)

    app = ns[APP_SYMBOL]
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/invocations", "/ping"} <= paths
    # /ping serves the runtime health contract without building any agent
    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200
