"""Live deploy to Google Vertex AI Agent Engine (the hosted runtime).

The pure half (``google_plan`` -> ``google_codegen``) turns the folder into a
self-contained source package; this module is the only Google file that touches
the network. It:

  1. builds the deterministic ``GoogleDeployPlan`` (skills, MCP toolsets, models),
  2. reads ``.agentlift-google.json`` and decides create / update / skip from the
     plan's spec hash (idempotent: an unchanged folder re-deploys to nothing),
  3. materializes the package on disk and ships it to Agent Engine as
     ``extra_packages`` via a ``ModuleAgent`` (source-based deploy -- the engine
     imports ``agentlift_engine.agent`` and serves its ``adk_app``),
  4. resolves MCP auth header values from the *local* environment and passes them
     as Agent Engine ``env_vars`` (secrets stay out of the source, the plan, and
     the lockfile -- only their env-var names are ever written down),
  5. records the resulting resource name + spec hash back to the lockfile.

The remote layout mirrors what ADK's own ``adk deploy agent_engine`` produces: a
relative top-level package shipped via ``extra_packages`` and imported by module
name, with the package's parent (the container working dir) on ``sys.path``.

Requires: pip install "google-cloud-aiplatform[adk,agent_engines]" google-adk
Env: GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, AGENTLIFT_GCP_STAGING_BUCKET (gs://...),
     ADC (gcloud auth application-default login). See docs/deploy-google.md.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .diagnostics import Diagnostics
from .google_codegen import APP_SYMBOL, MODULE_NAME, PACKAGE_NAME, write_package
from .google_lock import GoogleLock, decide_action
from .google_plan import (
    DEFAULT_GOOGLE_MODEL,
    GoogleDeployPlan,
    _auth_env_var,
    build_google_plan,
    safe_ident,
)
from .model import Project

# The ADK app exposes a fixed set of session/query operations. We read it from
# the generated app at deploy (authoritative for the installed ADK version), but
# keep this as a fallback so a deploy never fails just because the local import
# of the generated module is unavailable. (Verified against google-adk 1.34.x.)
ADK_REGISTER_OPERATIONS: dict[str, list[str]] = {
    "": ["get_session", "list_sessions", "create_session", "delete_session"],
    "async": [
        "async_get_session", "async_list_sessions", "async_create_session",
        "async_delete_session", "async_add_session_to_memory", "async_search_memory",
    ],
    "stream": ["stream_query", "streaming_agent_run_with_events"],
    "async_stream": ["async_stream_query"],
    "bidi_stream": ["bidi_stream_query"],
}

_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)|%(\w+)%")


@dataclass
class GoogleDeployResult:
    action: str                       # "create" | "update" | "skip"
    resource_name: str
    spec_hash: str
    display_name: str
    deploy_model: str
    env_var_names: list[str] = field(default_factory=list)
    build_dir: Optional[str] = None

    @property
    def changed(self) -> bool:
        return self.action != "skip"


# --------------------------------------------------------------------------- #
# auth: resolve header values from the local env (secrets never leave it as text)
# --------------------------------------------------------------------------- #
def _referenced_vars(template: str) -> list[str]:
    """Env-var names referenced by a header template (``${VAR}`` / ``$VAR`` / ``%VAR%``)."""
    return [m.group(1) or m.group(2) or m.group(3) for m in _VAR_RE.finditer(template)]


def resolve_auth_env_vars(
    project: Project, log: Callable[[str], None] = lambda *_: None,
) -> tuple[dict[str, str], list[str]]:
    """Resolve every MCP auth header to ``{engine_env_var_name: value}``.

    The env-var *names* are re-derived with the same ``_auth_env_var`` the plan
    uses, so they line up with what the generated source reads. The *values* are
    expanded from the deployer's local environment here and handed to Agent
    Engine as ``env_vars`` -- they are never written into the source, the plan,
    or the lockfile. Returns ``(env_vars, unresolved_names)`` where unresolved
    means a referenced ``${VAR}`` was not set locally (deployed as empty).
    """
    env_vars: dict[str, str] = {}
    unresolved: list[str] = []
    for agent in project.agents:
        for srv in agent.mcp_servers:
            if srv.transport != "url" or not srv.headers:
                continue
            for header, template in srv.headers.items():
                name = _auth_env_var(srv.name, header)
                missing = [v for v in _referenced_vars(template) if v not in os.environ]
                value = os.path.expandvars(template)
                if missing:
                    unresolved.append(name)
                    log(f"  warning: {', '.join(missing)} not set locally; MCP header "
                        f"'{header}' on '{srv.name}' deploys empty (env var {name}).")
                env_vars[name] = value
    return env_vars, unresolved


# --------------------------------------------------------------------------- #
# network seams (overridable for offline tests)
# --------------------------------------------------------------------------- #
def _default_engines():
    from vertexai import agent_engines
    return agent_engines


def _default_init(gcp_project: str, location: str, staging_bucket: str) -> None:
    import vertexai
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    vertexai.init(project=gcp_project, location=location, staging_bucket=staging_bucket)


def _default_make_module_agent(*, module_name, agent_name, register_operations, sys_paths):
    from vertexai.agent_engines import ModuleAgent
    return ModuleAgent(
        module_name=module_name, agent_name=agent_name,
        register_operations=register_operations, sys_paths=sys_paths,
    )


@contextlib.contextmanager
def _pushd(path: str):
    """Run with ``path`` as cwd so ``extra_packages`` tar entries are relative
    (``agentlift_engine/...``) -- the layout ``ModuleAgent`` expects remotely."""
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def resolve_register_operations(
    build_dir: str, log: Callable[[str], None] = lambda *_: None,
) -> dict[str, list[str]]:
    """Import the freshly-generated app and ask it for its operation schema.

    Authoritative for the installed ADK version; falls back to the captured
    schema if the import is unavailable. The generated module is unloaded
    afterwards so repeated deploys in one process never see a stale import."""
    import importlib
    import sys

    saved = list(sys.path)
    sys.path.insert(0, build_dir)
    try:
        mod = importlib.import_module(MODULE_NAME)
        mod = importlib.reload(mod)
        app = getattr(mod, APP_SYMBOL)
        ops = app.register_operations()
        return {k: list(v) for k, v in ops.items()}
    except Exception as e:  # pragma: no cover - exercised via the fallback test
        log(f"  note: using built-in operation schema (generated app not importable: {e})")
        return {k: list(v) for k, v in ADK_REGISTER_OPERATIONS.items()}
    finally:
        sys.path[:] = saved
        for key in [k for k in sys.modules if k == PACKAGE_NAME or k.startswith(PACKAGE_NAME + ".")]:
            sys.modules.pop(key, None)


# --------------------------------------------------------------------------- #
# deploy
# --------------------------------------------------------------------------- #
def build_package(
    plan: GoogleDeployPlan, project_root: str, build_root: Optional[str] = None,
) -> dict[str, Any]:
    """Materialize the deploy package under ``build_root`` (default:
    ``<project>/.agentlift-build/google``). Cleaned first so a removed skill or
    server never lingers from a previous build."""
    build_dir = build_root or os.path.join(project_root, ".agentlift-build", "google")
    shutil.rmtree(build_dir, ignore_errors=True)
    return write_package(plan, build_dir)


def deploy_google(
    project: Project,
    *,
    gcp_project: str,
    location: str = "us-central1",
    staging_bucket: str,
    model: str = DEFAULT_GOOGLE_MODEL,
    skip_unsupported: bool = False,
    build_root: Optional[str] = None,
    log: Callable[[str], None] = print,
    # seams (overridable for offline tests)
    engines: Any = None,
    make_module_agent: Optional[Callable[..., Any]] = None,
    init_vertexai: Optional[Callable[[str, str, str], None]] = None,
    register_operations: Optional[dict[str, list[str]]] = None,
) -> GoogleDeployResult:
    """Deploy ``project`` to one Agent Engine reasoningEngine, idempotently."""
    plan = build_google_plan(project, model=model, skip_unsupported=skip_unsupported)
    if not plan.deployable:
        raise ValueError(
            "Google plan has errors; not deployable. Run "
            "`agentlift plan <path> --target google` to see them."
        )

    lock = GoogleLock.load(project.root)
    decision = decide_action(lock, plan.spec_hash, gcp_project=gcp_project, location=location)
    if decision.action == "skip":
        log(f"  up to date ({decision.reason}); nothing to deploy.")
        return GoogleDeployResult(
            "skip", lock.reasoning_engine or "", plan.spec_hash,
            plan.display_name, plan.deploy_model, list(plan.env_var_names),
        )

    handles = build_package(plan, project.root, build_root)
    build_dir = handles["build_dir"]
    log(f"  built source package: {build_dir}{os.sep}{PACKAGE_NAME}/ "
        f"({len(plan.agents)} agent(s), {len(plan.skill_bundles)} skill bundle(s))")

    env_vars, _unresolved = resolve_auth_env_vars(project, log=log)
    if plan.env_var_names:
        log(f"  MCP auth -> Agent Engine env var(s): {', '.join(plan.env_var_names)}")

    reg_ops = register_operations or resolve_register_operations(build_dir, log=log)

    (init_vertexai or _default_init)(gcp_project, location, staging_bucket)
    eng = engines if engines is not None else _default_engines()
    factory = make_module_agent or _default_make_module_agent
    gcs_dir_name = safe_ident(plan.display_name)

    log(f"  {decision.action}: {decision.reason}")
    log("  deploying to Agent Engine (container build, a few minutes)...")
    with _pushd(build_dir):
        module_agent = factory(
            module_name=MODULE_NAME, agent_name=APP_SYMBOL,
            register_operations=reg_ops, sys_paths=["."],
        )
        common = dict(
            agent_engine=module_agent,
            requirements=list(plan.requirements),
            extra_packages=[PACKAGE_NAME],
            env_vars=env_vars or None,
            display_name=plan.display_name,
            gcs_dir_name=gcs_dir_name,
        )
        if decision.action == "create":
            remote = eng.create(**common)
        else:
            remote = eng.update(resource_name=lock.reasoning_engine, **common)

    resource = getattr(remote, "resource_name", None) or getattr(remote, "name", "")
    log(f"  deployed: {resource}")

    lock.record(
        reasoning_engine=resource, project=gcp_project, location=location,
        spec_hash=plan.spec_hash, display_name=plan.display_name,
        deploy_model=plan.deploy_model,
    )
    lock.save()
    log(f"  wrote {lock.path}")

    return GoogleDeployResult(
        decision.action, resource, plan.spec_hash, plan.display_name,
        plan.deploy_model, list(plan.env_var_names), build_dir,
    )
