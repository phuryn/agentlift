"""The Google deploy step is idempotent and offline-testable: with a mocked
``agent_engines`` it creates on a fresh lock, updates when the spec changed,
skips when unchanged, ships the package via ``extra_packages`` as a relative
``ModuleAgent``, resolves MCP auth header values from the local env into
``env_vars`` (never inlining the secret), and records the resource + spec hash
to ``.agentlift-google.json``. No network."""
import os
import shutil

import pytest

from agentlift.google_lock import GOOGLE_LOCKFILE_NAME, GoogleLock
from agentlift.google_plan import build_google_plan
from agentlift.google_target import (
    ADK_REGISTER_OPERATIONS,
    deploy_google,
    resolve_auth_env_vars,
    resolve_register_operations,
)
from agentlift.parser import parse_project

ENGINE = "projects/p/locations/us-central1/reasoningEngines/999"


# --------------------------------------------------------------------------- #
# fakes + helpers
# --------------------------------------------------------------------------- #
class _FakeRemote:
    def __init__(self, resource_name):
        self.resource_name = resource_name


class _FakeEngines:
    def __init__(self, resource_name=ENGINE):
        self.resource_name = resource_name
        self.create_calls = []
        self.update_calls = []

    def create(self, **kw):
        self.create_calls.append(kw)
        return _FakeRemote(self.resource_name)

    def update(self, *, resource_name, **kw):
        self.update_calls.append({"resource_name": resource_name, **kw})
        return _FakeRemote(resource_name)


def _fake_module_agent(**kw):
    return ("MODULE_AGENT", kw)


def _copy(src_dir, tmp_path, name):
    dst = os.path.join(str(tmp_path), name)
    shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns(
        ".agentlift-lock.json", ".agentlift-*.json", "*.bak"))
    return dst


def _deploy(project, engines, **over):
    kw = dict(
        gcp_project="p", location="us-central1", staging_bucket="gs://bucket",
        log=lambda *_: None, engines=engines,
        make_module_agent=_fake_module_agent,
        init_vertexai=lambda *a: None,
        register_operations=dict(ADK_REGISTER_OPERATIONS),
    )
    kw.update(over)
    return deploy_google(project, **kw)


def _team(examples_dir, tmp_path):
    root = _copy(os.path.join(examples_dir, "team"), tmp_path, "team")
    project, _ = parse_project(root)
    return project, root


# --------------------------------------------------------------------------- #
# create / update / skip
# --------------------------------------------------------------------------- #
def test_create_on_fresh_lock(examples_dir, tmp_path):
    project, root = _team(examples_dir, tmp_path)
    eng = _FakeEngines()
    res = _deploy(project, eng)

    assert res.action == "create"
    assert res.resource_name == ENGINE
    assert len(eng.create_calls) == 1 and not eng.update_calls
    kw = eng.create_calls[0]
    # ships the package as a RELATIVE top-level dir (matches the remote layout)
    assert kw["extra_packages"] == ["agentlift_engine"]
    # the team's no-tools lead enables all built-ins -> web tools lower -> the
    # web ADK floor is pinned alongside the engine requirement
    assert kw["requirements"] == [
        "google-cloud-aiplatform[adk,agent_engines]",
        "google-adk>=1.34.3",
    ]
    assert kw["display_name"] == "agentlift-lead"
    assert kw["gcs_dir_name"] == "agentlift_lead"
    assert kw["env_vars"] is None          # team example has no inline auth
    # ModuleAgent wiring
    _tag, ma = kw["agent_engine"]
    assert ma["module_name"] == "agentlift_engine.agent"
    assert ma["agent_name"] == "adk_app"
    assert ma["sys_paths"] == ["."]
    assert "stream" in ma["register_operations"]


def test_lock_written_after_create(examples_dir, tmp_path):
    project, root = _team(examples_dir, tmp_path)
    plan = build_google_plan(project)
    _deploy(project, _FakeEngines())

    lock = GoogleLock.load(root)
    assert lock.reasoning_engine == ENGINE
    assert lock.spec_hash == plan.spec_hash
    assert lock.project == "p" and lock.location == "us-central1"
    assert lock.display_name == "agentlift-lead"
    assert os.path.isfile(os.path.join(root, GOOGLE_LOCKFILE_NAME))


def test_skip_when_unchanged(examples_dir, tmp_path):
    project, root = _team(examples_dir, tmp_path)
    # first deploy records the lock
    _deploy(project, _FakeEngines())
    # second deploy with a fresh engines recorder: nothing should be called
    eng2 = _FakeEngines()
    res = _deploy(project, eng2)
    assert res.action == "skip"
    assert res.resource_name == ENGINE
    assert not eng2.create_calls and not eng2.update_calls
    # skip does not rebuild the package
    assert res.build_dir is None


def test_update_when_spec_changes(examples_dir, tmp_path):
    project, root = _team(examples_dir, tmp_path)
    # seed a lock with a stale spec hash for the same project/location
    lock = GoogleLock.load(root)
    lock.record(reasoning_engine=ENGINE, project="p", location="us-central1",
                spec_hash="STALE", display_name="agentlift-lead", deploy_model="gemini-2.5-flash")
    lock.save()

    eng = _FakeEngines()
    res = _deploy(project, eng)
    assert res.action == "update"
    assert not eng.create_calls and len(eng.update_calls) == 1
    assert eng.update_calls[0]["resource_name"] == ENGINE
    assert eng.update_calls[0]["extra_packages"] == ["agentlift_engine"]


def test_project_move_forces_create(examples_dir, tmp_path):
    project, root = _team(examples_dir, tmp_path)
    plan = build_google_plan(project)
    lock = GoogleLock.load(root)
    # same spec hash, but recorded against a different project -> create new engine
    lock.record(reasoning_engine=ENGINE, project="OTHER", location="us-central1",
                spec_hash=plan.spec_hash, display_name="agentlift-lead", deploy_model="gemini-2.5-flash")
    lock.save()
    eng = _FakeEngines()
    res = _deploy(project, eng)
    assert res.action == "create"
    assert len(eng.create_calls) == 1


# --------------------------------------------------------------------------- #
# custom model re-deploys (spec hash changes)
# --------------------------------------------------------------------------- #
def test_changing_model_triggers_update(examples_dir, tmp_path):
    project, root = _team(examples_dir, tmp_path)
    _deploy(project, _FakeEngines())              # create @ default model
    eng = _FakeEngines()
    res = _deploy(project, eng, model="gemini-2.5-pro")
    assert res.action == "update"
    assert res.deploy_model == "gemini-2.5-pro"
    assert eng.update_calls and not eng.create_calls


# --------------------------------------------------------------------------- #
# MCP auth -> env vars, secret never inlined
# --------------------------------------------------------------------------- #
def test_auth_header_resolved_into_env_vars(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "super-secret-value")
    root = _copy(os.path.join(fixtures_dir, "mcp-auth"), tmp_path, "mcp-auth")
    project, _ = parse_project(root)

    eng = _FakeEngines()
    res = _deploy(project, eng)
    assert res.action == "create"
    kw = eng.create_calls[0]
    assert kw["env_vars"] == {"AGENTLIFT_MCP_SECURE_AUTHORIZATION": "Bearer super-secret-value"}
    assert res.env_var_names == ["AGENTLIFT_MCP_SECURE_AUTHORIZATION"]

    # the secret value must never land in the generated source on disk
    agent_py = os.path.join(res.build_dir, "agentlift_engine", "agent.py")
    src = open(agent_py, encoding="utf-8").read()
    assert "super-secret-value" not in src
    assert "SECURE_API_TOKEN" not in src
    assert "os.environ.get('AGENTLIFT_MCP_SECURE_AUTHORIZATION'" in src


def test_resolve_auth_env_vars_flags_unset(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("SECURE_API_TOKEN", raising=False)
    root = _copy(os.path.join(fixtures_dir, "mcp-auth"), tmp_path, "mcp-auth")
    project, _ = parse_project(root)
    env_vars, unresolved = resolve_auth_env_vars(project)
    assert unresolved == ["AGENTLIFT_MCP_SECURE_AUTHORIZATION"]
    # unset template expands to the literal token (deployed empty-ish), never crashes
    assert "AGENTLIFT_MCP_SECURE_AUTHORIZATION" in env_vars


def test_resolve_auth_env_vars_empty_for_team(examples_dir, tmp_path):
    project, _ = _team(examples_dir, tmp_path)
    env_vars, unresolved = resolve_auth_env_vars(project)
    assert env_vars == {} and unresolved == []


# --------------------------------------------------------------------------- #
# register_operations resolution
# --------------------------------------------------------------------------- #
def test_register_operations_falls_back(tmp_path):
    # a build dir with no importable package -> fallback schema, no exception
    ops = resolve_register_operations(os.path.join(str(tmp_path), "nope"))
    assert ops == {k: list(v) for k, v in ADK_REGISTER_OPERATIONS.items()}
    assert "stream" in ops


def test_not_deployable_raises(fixtures_dir, tmp_path):
    # gmail-agent declares a stdio MCP server -> plan has errors -> deploy refuses
    root = _copy(os.path.join(fixtures_dir, "gmail-agent"), tmp_path, "gmail")
    project, _ = parse_project(root)
    with pytest.raises(ValueError, match="not deployable"):
        _deploy(project, _FakeEngines())


# --------------------------------------------------------------------------- #
# real ADK: the generated package actually imports and yields the op schema
# --------------------------------------------------------------------------- #
try:
    import google.adk  # noqa: F401
    import vertexai  # noqa: F401
    _HAS_ADK = True
except Exception:
    _HAS_ADK = False


@pytest.mark.skipif(not _HAS_ADK, reason="google-adk / vertexai not installed")
def test_resolve_register_operations_real_import(examples_dir, tmp_path):
    from agentlift.google_target import build_package
    project, _ = _team(examples_dir, tmp_path)
    plan = build_google_plan(project)
    handles = build_package(plan, project.root)
    ops = resolve_register_operations(handles["build_dir"])
    assert "stream" in ops and "stream_query" in ops["stream"]
