"""The Amazon Bedrock AgentCore Runtime deploy step.

The pure half (``bedrock_plan`` -> ``bedrock_codegen``) turns the folder into a
self-contained Strands source package. This module owns the build artifact and
the (deferred) network path. Its honesty contract -- settled with Codex against
the repo's *confirm-live-before-encoding* rule -- has three sharp behaviours:

  - ``build_artifact``  : materialize a COMPLETE deployable container artifact
    offline (Strands package + ARM64 ``Dockerfile`` + ``.dockerignore`` + a
    ``NOTES.txt`` deploy runbook). Pure apart from filesystem writes. No Docker,
    no ECR, no ``agentcore`` CLI, no network. This is the real, shippable path
    today via ``deploy --target bedrock --build-only``.

  - ``deploy_bedrock(build_only=False)`` : **refuses**, raising
    ``HostedDeployNotLiveVerified`` *before* importing boto3 or constructing any
    AWS control-plane payload. Creating + invoking a hosted AgentCore Runtime is
    control-plane (``bedrock-agentcore-control.create_agent_runtime``) and needs
    AWS IAM + an execution role + ECR -- and agentlift has no live receipt for
    that wire shape yet (Gate B, see experiments/bedrock-composition/RESULTS.md).
    Per the house rule we do not ship an unverified control-plane call -- not in
    Python, and not as a copy-paste payload in NOTES either.

  - ``resolve_auth_env_vars`` : resolve MCP auth header values from the local
    environment into AgentCore ``env_vars`` (by name only in the artifact; the
    value never enters source, plan, or lock). Mirrors the Google target.

So the artifact a user gets is fully deployable; the *final* hosted-creation step
is a clearly-marked manual section with placeholders + links to the current AWS
docs, not a product-generated create call. When a live receipt lands, the hosted
path slots in behind ``deploy_bedrock`` and starts writing ``.agentlift-bedrock.json``
(the pure ``bedrock_lock`` policy is already settled + tested).
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .bedrock_codegen import PACKAGE_NAME, write_package
from .bedrock_plan import (
    DEFAULT_BEDROCK_REGION,
    BedrockDeployPlan,
    _auth_env_var,
    build_bedrock_plan,
)
from .model import Project

DOCKERFILE_NAME = "Dockerfile"
DOCKERIGNORE_NAME = ".dockerignore"
NOTES_NAME = "NOTES.txt"

# Where the AgentCore HTTP-contract + custom-deployment docs live. Surfaced in the
# runbook so the manual hosted-creation step is verified against current AWS docs
# rather than a wire shape agentlift guessed.
_DOC_HTTP_CONTRACT = (
    "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
    "runtime-http-protocol-contract.html"
)
_DOC_CUSTOM_DEPLOY = (
    "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/"
    "getting-started-custom.html"
)

_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)|%(\w+)%")


class HostedDeployNotLiveVerified(RuntimeError):
    """Raised by ``deploy_bedrock`` when a hosted deploy is requested.

    The hosted AgentCore Runtime path is control-plane (IAM + execution role +
    ECR) and not yet live-verified, so agentlift refuses rather than fire an
    unverified ``create_agent_runtime`` (Gate B). The build artifact is the
    supported path today (``--build-only``)."""


@dataclass
class BedrockDeployResult:
    action: str                       # "build" today; "create"/"update"/"skip" once live
    region: str
    spec_hash: str
    display_name: str
    deploy_model: str
    env_var_names: list[str] = field(default_factory=list)
    build_dir: Optional[str] = None
    agent_runtime_arn: str = ""

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
    """Resolve every MCP auth header to ``{runtime_env_var_name: value}``.

    The env-var *names* are re-derived with the same ``_auth_env_var`` the plan +
    generated source use, so they line up with what the runtime reads. The
    *values* are expanded from the deployer's local environment here, to be handed
    to AgentCore as ``env_vars`` at create/update time -- never written into the
    source, the plan, or the lock. Returns ``(env_vars, unresolved_names)`` where
    unresolved means a referenced ``${VAR}`` was not set locally.
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
                        f"'{header}' on '{srv.name}' would deploy empty (env var {name}).")
                env_vars[name] = value
    return env_vars, unresolved


# --------------------------------------------------------------------------- #
# container artifact rendering (pure)
# --------------------------------------------------------------------------- #
def render_dockerfile(plan: BedrockDeployPlan) -> str:
    """The ARM64 container recipe AgentCore Runtime requires.

    AgentCore runs ``linux/arm64`` containers that serve ``POST /invocations`` +
    ``GET /ping`` on port 8080 -- the contract ``BedrockAgentCoreApp`` implements.
    The package's ``__main__`` binds ``0.0.0.0:8080`` via ``app.run``. boto3 is
    floored deliberately (a stale boto3/botocore is a known AgentCore failure
    mode -- the bearer-token inference path needs >= 1.40)."""
    return "\n".join([
        "# Generated by `agentlift deploy --target bedrock --build-only` -- DO NOT hand-edit.",
        "# Amazon Bedrock AgentCore Runtime: linux/arm64, serves POST /invocations +",
        "# GET /ping on :8080 (the BedrockAgentCoreApp contract).",
        "FROM --platform=linux/arm64 python:3.12-slim",
        "",
        "ENV PYTHONUNBUFFERED=1 \\",
        "    PYTHONDONTWRITEBYTECODE=1",
        "",
        "WORKDIR /app",
        "",
        "COPY requirements.txt .",
        "RUN pip install --no-cache-dir -r requirements.txt",
        "",
        f"COPY {PACKAGE_NAME}/ ./{PACKAGE_NAME}/",
        "",
        "EXPOSE 8080",
        f'CMD ["python", "-m", "{PACKAGE_NAME}.agent"]',
        "",
    ])


def render_dockerignore() -> str:
    return "\n".join([
        "__pycache__/", "*.pyc", ".agentlift-build/", ".agentlift-*.json",
        ".env", "*.bak", "",
    ])


def render_deploy_notes(plan: BedrockDeployPlan) -> str:
    """A deploy runbook: readiness checklist + concrete local build/push commands +
    a clearly-marked MANUAL hosted-creation section (placeholders + doc links, NOT
    a product-generated ``create_agent_runtime`` call -- agentlift has no live
    receipt for that wire shape yet, so it refuses to render one as fact)."""
    lines = [
        "agentlift -> Amazon Bedrock AgentCore Runtime  (preview build artifact)",
        "=" * 68,
        "",
        f"  runtime name : {plan.display_name}",
        f"  root agent   : {plan.root_agent}",
        f"  region       : {plan.region}",
        f"  model(s)     : " + ", ".join(
            f"{n.name} -> {n.bedrock_model}" for n in plan.agents),
        f"  skill bundles: {len(plan.skill_bundles)}   "
        f"agents: {len(plan.agents)}   spec hash: {plan.spec_hash[:12]}",
        "",
        "This directory IS the deployable container build context: the Strands",
        f"source package ({PACKAGE_NAME}/), its requirements.txt, and an ARM64",
        "Dockerfile that serves POST /invocations + GET /ping on :8080.",
        "",
        "READINESS CHECKLIST (two one-time, per-account gates -- both outside the code path)",
        "-" * 68,
        "  [ ] Gate A - Claude on Bedrock: submit the Anthropic use-case form",
        "      (Bedrock console -> Model access -> Anthropic). The pinned model id",
        "      is a regional inference profile and needs this entitlement, exactly",
        "      like enabling Claude in Vertex Model Garden. Non-Claude models skip this.",
        "  [ ] Gate B - hosted deploy creds: AWS IAM credentials, an AgentCore",
        "      execution role (with iam:PassRole), and an ECR repository in the",
        "      deploy region. The Bedrock bearer token authenticates MODEL inference",
        "      only -- it cannot create a runtime.",
        "",
        "BUILD + PUSH THE IMAGE (concrete -- standard Docker/ECR, run from this dir)",
        "-" * 68,
        f"  REGION={plan.region}",
        "  ACCOUNT=$(aws sts get-caller-identity --query Account --output text)",
        "  REPO=agentlift-" + plan.root_agent,
        "  ECR=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com",
        "",
        "  aws ecr create-repository --repository-name $REPO --region $REGION || true",
        "  aws ecr get-login-password --region $REGION \\",
        "    | docker login --username AWS --password-stdin $ECR",
        "  docker buildx build --platform linux/arm64 \\",
        "    -t $ECR/$REPO:latest --push .",
        "",
    ]
    if plan.env_var_names:
        lines += [
            "MCP AUTH ENV VARS (set these on the runtime at create time; values come",
            "from YOUR local env -- agentlift never writes the values to disk):",
            *[f"  - {name}" for name in plan.env_var_names],
            "",
        ]
    lines += [
        "CREATE THE HOSTED RUNTIME (MANUAL -- verify against current AWS docs)",
        "-" * 68,
        "  agentlift does NOT emit the create-agent-runtime call: that control-plane",
        "  wire shape is not yet live-verified here, so shipping it (even as a",
        "  copy-paste command) would be guessing. Use one of:",
        "",
        "    - the bedrock-agentcore starter toolkit (`agentcore configure` / `launch`), or",
        "    - bedrock-agentcore-control create-agent-runtime with this image, your",
        "      execution role ARN, and a PUBLIC network configuration.",
        "",
        "  Required inputs you now have: container image URI (pushed above),",
        "  execution role ARN (Gate B), region (" + plan.region + ").",
        "",
        "  AgentCore HTTP contract : " + _DOC_HTTP_CONTRACT,
        "  Custom container deploy : " + _DOC_CUSTOM_DEPLOY,
        "",
        "LOCAL SMOKE TEST (optional, before pushing)",
        "-" * 68,
        "  docker buildx build --platform linux/arm64 -t agentlift-bedrock:local --load .",
        "  docker run --rm -p 8080:8080 \\",
        "".join(["    "] + [f"-e {n}=... " for n in plan.env_var_names] +
                ["agentlift-bedrock:local"]),
        "  curl localhost:8080/ping",
        '  curl -XPOST localhost:8080/invocations -d \'{"prompt":"hello"}\'',
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# build (offline; the supported path today)
# --------------------------------------------------------------------------- #
def build_artifact(
    plan: BedrockDeployPlan, project_root: str, build_root: Optional[str] = None,
) -> dict[str, Any]:
    """Materialize the deployable container build context under ``build_root``
    (default ``<project>/.agentlift-build/bedrock``). Cleaned first so a removed
    skill or server never lingers from a previous build. Filesystem writes only --
    no Docker, no network."""
    build_dir = build_root or os.path.join(project_root, ".agentlift-build", "bedrock")
    shutil.rmtree(build_dir, ignore_errors=True)
    handles = write_package(plan, build_dir)
    extra = {
        DOCKERFILE_NAME: render_dockerfile(plan),
        DOCKERIGNORE_NAME: render_dockerignore(),
        NOTES_NAME: render_deploy_notes(plan),
    }
    for fn, text in extra.items():
        with open(os.path.join(build_dir, fn), "w", encoding="utf-8") as fh:
            fh.write(text)
    handles["dockerfile"] = os.path.join(build_dir, DOCKERFILE_NAME)
    handles["notes"] = os.path.join(build_dir, NOTES_NAME)
    return handles


# --------------------------------------------------------------------------- #
# deploy (build-only works; hosted path refuses until live-verified)
# --------------------------------------------------------------------------- #
def deploy_bedrock(
    project: Project,
    *,
    region: str = DEFAULT_BEDROCK_REGION,
    build_only: bool = False,
    skip_unsupported: bool = False,
    build_root: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> BedrockDeployResult:
    """Build the Bedrock artifact (``build_only=True``) or refuse the hosted deploy.

    There is no live network path yet: ``build_only=False`` raises
    ``HostedDeployNotLiveVerified`` *before* importing boto3 or building any AWS
    payload (Gate B). ``build_only=True`` materializes the deployable artifact and
    returns ``action="build"``.
    """
    plan = build_bedrock_plan(project, region=region, skip_unsupported=skip_unsupported)
    if not plan.deployable:
        raise ValueError(
            "Bedrock plan has errors; not deployable. Run "
            "`agentlift plan <path> --target bedrock` to see them."
        )

    if not build_only:
        raise HostedDeployNotLiveVerified(
            "Hosted deploy to Bedrock AgentCore Runtime is a preview pending a live "
            "receipt (needs AWS IAM + an execution role + ECR -- the bearer token "
            "cannot create a runtime). No network call was made and nothing was "
            "written. Run `agentlift deploy --target bedrock --build-only` to "
            "materialize the deployable container artifact + a deploy runbook."
        )

    handles = build_artifact(plan, project.root, build_root)
    log(f"  built container artifact: {handles['build_dir']}")
    log(f"    package: {handles['module_name']}  app: {handles['app_symbol']}")
    log(f"    {len(plan.agents)} agent(s), {len(plan.skill_bundles)} skill bundle(s), "
        f"Dockerfile + NOTES.txt (deploy runbook)")
    if plan.env_var_names:
        log(f"    MCP auth env var(s) to set at create: {', '.join(plan.env_var_names)}")

    return BedrockDeployResult(
        action="build",
        region=plan.region,
        spec_hash=plan.spec_hash,
        display_name=plan.display_name,
        deploy_model=plan.agents[0].bedrock_model if plan.agents else "",
        env_var_names=list(plan.env_var_names),
        build_dir=handles["build_dir"],
    )
