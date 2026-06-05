"""The Bedrock deploy step, offline. Two sharp behaviours (settled with Codex
against the confirm-live-before-encoding rule):

  - ``--build-only`` materializes a COMPLETE container artifact (Strands package +
    ARM64 Dockerfile + NOTES runbook) -- the supported path today.
  - a bare (hosted) deploy REFUSES: it raises before any boto3 / AWS payload, makes
    no network call, and writes nothing (no artifact, no lock).

Plus: MCP auth resolves to env-var names (value never inlined), and nothing in the
shipped path writes ``.agentlift-bedrock.json`` (the lock is ready, not live)."""
import os
import shutil

import pytest

from agentlift.bedrock_lock import BEDROCK_LOCKFILE_NAME
from agentlift.bedrock_plan import build_bedrock_plan
from agentlift.bedrock_target import (
    HostedDeployNotLiveVerified,
    build_artifact,
    deploy_bedrock,
    render_deploy_notes,
    render_dockerfile,
    resolve_auth_env_vars,
)
from agentlift.parser import parse_project


def _copy(src_dir, tmp_path, name):
    dst = os.path.join(str(tmp_path), name)
    shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns(
        ".agentlift-lock.json", ".agentlift-*.json", "*.bak", ".agentlift-build"))
    return dst


def _team(examples_dir, tmp_path):
    root = _copy(os.path.join(examples_dir, "team"), tmp_path, "team")
    project, _ = parse_project(root)
    return project, root


# --------------------------------------------------------------------------- #
# Dockerfile (pure)
# --------------------------------------------------------------------------- #
def test_dockerfile_is_arm64_and_serves_8080(examples_dir, tmp_path):
    project, _ = _team(examples_dir, tmp_path)
    df = render_dockerfile(build_bedrock_plan(project))
    assert "FROM --platform=linux/arm64 python:3.12-slim" in df
    assert "EXPOSE 8080" in df
    assert 'CMD ["python", "-m", "agentlift_runtime.agent"]' in df
    assert "COPY agentlift_runtime/ ./agentlift_runtime/" in df


# --------------------------------------------------------------------------- #
# build_artifact (offline, the supported path)
# --------------------------------------------------------------------------- #
def test_build_artifact_materializes_full_context(examples_dir, tmp_path):
    project, root = _team(examples_dir, tmp_path)
    build = os.path.join(str(tmp_path), "out")
    handles = build_artifact(build_bedrock_plan(project), root, build_root=build)

    assert os.path.isfile(os.path.join(build, "agentlift_runtime", "agent.py"))
    assert os.path.isfile(os.path.join(build, "Dockerfile"))
    assert os.path.isfile(os.path.join(build, ".dockerignore"))
    assert os.path.isfile(os.path.join(build, "NOTES.txt"))
    reqs = open(os.path.join(build, "requirements.txt"), encoding="utf-8").read()
    assert "strands-agents" in reqs and "bedrock-agentcore" in reqs
    # the shipped skill bundle lands where Skill.from_file looks for it
    assert os.path.isfile(os.path.join(
        build, "agentlift_runtime", "skills", "cite-sources", "SKILL.md"))
    assert handles["dockerfile"].endswith("Dockerfile")


def test_build_artifact_is_clean_rebuilt(examples_dir, tmp_path):
    project, root = _team(examples_dir, tmp_path)
    build = os.path.join(str(tmp_path), "out")
    build_artifact(build_bedrock_plan(project), root, build_root=build)
    stale = os.path.join(build, "agentlift_runtime", "skills", "stale", "SKILL.md")
    os.makedirs(os.path.dirname(stale), exist_ok=True)
    open(stale, "w").write("x")
    build_artifact(build_bedrock_plan(project), root, build_root=build)
    assert not os.path.exists(stale)   # rmtree'd before the rebuild


# --------------------------------------------------------------------------- #
# NOTES runbook: concrete build/push, MANUAL hosted-create (no guessed wire shape)
# --------------------------------------------------------------------------- #
def test_notes_has_gates_and_points_at_live_hosted_create(examples_dir, tmp_path):
    project, _ = _team(examples_dir, tmp_path)
    notes = render_deploy_notes(build_bedrock_plan(project))
    # two one-time gates surfaced
    assert "Gate A" in notes and "use-case form" in notes
    assert "Gate B" in notes and "execution role" in notes
    # the standard Docker/ECR steps are concrete (those are stable, not guessed)
    assert "docker buildx build --platform linux/arm64" in notes
    assert "aws ecr get-login-password" in notes
    # the build-only NOTES now points at the live hosted-create path (Stage 2 shipped):
    # `--mode runtime` (no --build-only) does CreateAgentRuntime for you
    assert "CreateAgentRuntime" in notes
    assert "--mode runtime" in notes
    assert "docs.aws.amazon.com/bedrock-agentcore" in notes


# --------------------------------------------------------------------------- #
# deploy_bedrock: build-only works, hosted refuses
# --------------------------------------------------------------------------- #
def test_deploy_build_only_builds_and_writes_no_lock(examples_dir, tmp_path):
    project, root = _team(examples_dir, tmp_path)
    res = deploy_bedrock(project, region="eu-north-1", build_only=True, log=lambda *_: None)
    assert res.action == "build"
    assert res.region == "eu-north-1"
    assert res.deploy_model.startswith("eu.anthropic.")
    assert os.path.isfile(os.path.join(res.build_dir, "Dockerfile"))
    # the lock is write-dead: a build never creates .agentlift-bedrock.json
    assert not os.path.isfile(os.path.join(root, BEDROCK_LOCKFILE_NAME))


def test_hosted_deploy_refuses_when_gate_closed(examples_dir, tmp_path, monkeypatch):
    # the gate MECHANISM: when runtime_hosted_deploy_allowed() is False, a bare hosted
    # deploy refuses before any work. (The shipped flag is now True -- live-verified --
    # so we force the gate closed to exercise the refusal path.)
    import agentlift.bedrock_target as bt
    monkeypatch.setattr(bt, "runtime_hosted_deploy_allowed", lambda: False)
    project, root = _team(examples_dir, tmp_path)
    with pytest.raises(HostedDeployNotLiveVerified, match="committed live receipt"):
        deploy_bedrock(project, region="eu-north-1", build_only=False)
    # refusal fires BEFORE any work: no build dir, no lock
    assert not os.path.exists(os.path.join(root, ".agentlift-build"))
    assert not os.path.isfile(os.path.join(root, BEDROCK_LOCKFILE_NAME))


def test_not_deployable_raises(fixtures_dir, tmp_path):
    # gmail-agent declares a stdio MCP server -> plan errors -> deploy refuses
    root = _copy(os.path.join(fixtures_dir, "gmail-agent"), tmp_path, "gmail")
    project, _ = parse_project(root)
    with pytest.raises(ValueError, match="not deployable"):
        deploy_bedrock(project, region="eu-north-1", build_only=True)


# --------------------------------------------------------------------------- #
# MCP auth -> env vars, secret never inlined into the source
# --------------------------------------------------------------------------- #
def test_auth_header_resolved_into_env_vars(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_API_TOKEN", "super-secret-value")
    root = _copy(os.path.join(fixtures_dir, "mcp-auth"), tmp_path, "mcp-auth")
    project, _ = parse_project(root)

    env_vars, unresolved = resolve_auth_env_vars(project)
    assert env_vars == {"AGENTLIFT_MCP_SECURE_AUTHORIZATION": "Bearer super-secret-value"}
    assert unresolved == []

    build = os.path.join(str(tmp_path), "out")
    build_artifact(build_bedrock_plan(project), root, build_root=build)
    src = open(os.path.join(build, "agentlift_runtime", "agent.py"), encoding="utf-8").read()
    # the env-var NAME is in the source; the secret value + the local var name are not
    assert "AGENTLIFT_MCP_SECURE_AUTHORIZATION" in src
    assert "super-secret-value" not in src
    assert "SECURE_API_TOKEN" not in src


def test_resolve_auth_env_vars_flags_unset(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("SECURE_API_TOKEN", raising=False)
    root = _copy(os.path.join(fixtures_dir, "mcp-auth"), tmp_path, "mcp-auth")
    project, _ = parse_project(root)
    env_vars, unresolved = resolve_auth_env_vars(project)
    assert unresolved == ["AGENTLIFT_MCP_SECURE_AUTHORIZATION"]
    assert "AGENTLIFT_MCP_SECURE_AUTHORIZATION" in env_vars


def test_resolve_auth_env_vars_empty_for_team(examples_dir, tmp_path):
    project, _ = _team(examples_dir, tmp_path)
    env_vars, unresolved = resolve_auth_env_vars(project)
    assert env_vars == {} and unresolved == []
