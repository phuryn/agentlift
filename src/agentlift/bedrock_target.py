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
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .bedrock_codegen import PACKAGE_NAME, write_package
from .bedrock_lock import BedrockLock, decide_action
from .bedrock_plan import (
    DEFAULT_BEDROCK_REGION,
    BedrockDeployPlan,
    _auth_env_var,
    build_bedrock_plan,
    runtime_hosted_deploy_allowed,
    safe_ident,
)
from .model import Project

DOCKERFILE_NAME = "Dockerfile"
DOCKERIGNORE_NAME = ".dockerignore"
NOTES_NAME = "NOTES.txt"

# Env var naming the AgentCore execution role for the hosted Runtime create (shared with
# the harness). The role must trust bedrock-agentcore.amazonaws.com and allow ECR pull
# (the runtime pulls the image) + bedrock:InvokeModel + CloudWatch Logs.
RUNTIME_EXECUTION_ROLE_ENV = "AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN"

# AgentCore Runtime lifecycle states (provisional spellings; reconciled by the first live
# GetAgentRuntime).
_RT_READY_STATES = {"READY", "ACTIVE", "AVAILABLE"}
_RT_FAILED_STATES = {"FAILED", "CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}

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
    """Raised by ``deploy_bedrock`` when a hosted deploy is requested **and the gate is
    closed** (``runtime_hosted_deploy_allowed()`` is False).

    The hosted AgentCore Runtime create is now live-verified (``_RUNTIME_LIVE_VERIFIED``
    is True), so in normal operation a bare ``--mode runtime`` deploy RUNS the create.
    This exception remains the refusal mechanism if the gate is ever forced closed (e.g.
    a future un-verified change), keeping the confirm-live-before-encoding rule enforceable;
    ``--build-only`` always emits just the container artifact."""


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
        "CREATE THE HOSTED RUNTIME",
        "-" * 68,
        "  agentlift can do this for you: drop --build-only and run",
        "    agentlift deploy <path> --target bedrock --mode runtime",
        "  which builds + pushes this image, calls CreateAgentRuntime (PUBLIC network,",
        "  HTTP serverProtocol, IAM-only), polls READY, and writes .agentlift-bedrock.json.",
        "  (Live-verified on Nova -- see tests/live/receipts/*-runtime-bedrock.)",
        "",
        "  To deploy this artifact yourself instead, use either:",
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


class RuntimeExecutionRoleRequired(RuntimeError):
    """A hosted Runtime create needs an execution role ARN and none was supplied.

    Set ``AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN`` (trust ``bedrock-agentcore.amazonaws.com``;
    allow ECR pull + ``bedrock:InvokeModel`` + CloudWatch Logs)."""


class RuntimeDeployFailed(RuntimeError):
    """The runtime reached a terminal failed state, or polling timed out."""


# --------------------------------------------------------------------------- #
# hosted-deploy seams (overridable for offline tests) + helpers
# --------------------------------------------------------------------------- #
def _default_control_client(region: str) -> Any:
    import boto3
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _default_ecr_client(region: str) -> Any:
    import boto3
    return boto3.client("ecr", region_name=region)


def _default_sleep(seconds: float) -> None:  # pragma: no cover - real wall clock
    import time
    time.sleep(seconds)


def _default_docker_runner(cmd: list[str], input_text: Optional[str] = None) -> None:  # pragma: no cover - shells out
    """Run a docker command, raising on non-zero. ``input_text`` is piped to stdin
    (used for ``docker login --password-stdin``)."""
    subprocess.run(cmd, check=True, input=input_text, text=True)


def _ecr_repo_name(plan: BedrockDeployPlan) -> str:
    """ECR repo for this artifact: ``agentlift/<display>`` (display is already safe)."""
    return f"agentlift/{safe_ident(plan.display_name)}"


def _build_and_push(
    *, build_dir: str, image_uri: str, registry: str,
    ecr_client: Any, log: Callable[[str], None],
    runner: Callable[..., None],
) -> None:
    """`docker login` to ECR, then `docker buildx build --platform linux/arm64 --push`.

    AgentCore Runtime requires linux/arm64 images; on an x86 host the build needs QEMU
    binfmt registered (`docker run --privileged --rm tonistiigi/binfmt --install arm64`).
    ``runner(cmd, input_text=...)`` runs one command (seam: a fake records calls in tests).
    The registry password is piped via stdin so it never lands in argv/process listings."""
    import base64
    token = ecr_client.get_authorization_token()
    auth = token["authorizationData"][0]
    user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    log(f"  docker login {registry}...")
    runner(["docker", "login", "--username", user, "--password-stdin", registry],
           input_text=password)
    log(f"  buildx build --platform linux/arm64 --push {image_uri} (arm64; ~minutes)...")
    runner(["docker", "buildx", "build", "--platform", "linux/arm64",
            "-t", image_uri, "--push", build_dir])


def _ensure_ecr_repo(ecr_client: Any, repo: str, log: Callable[[str], None]) -> str:
    """Create the ECR repo if absent; return its registry URI (``<acct>.dkr.ecr...``)."""
    try:
        d = ecr_client.describe_repositories(repositoryNames=[repo])
        uri = d["repositories"][0]["repositoryUri"]
    except Exception:  # noqa: BLE001 - RepositoryNotFoundException or a fake
        r = ecr_client.create_repository(repositoryName=repo)
        uri = r["repository"]["repositoryUri"]
        log(f"  created ECR repo {repo}")
    return uri


def _runtime_create_body(
    plan: BedrockDeployPlan, *, image_uri: str, role_arn: str, env_vars: dict[str, str],
    client_token: str,
) -> dict[str, Any]:
    """The ``CreateAgentRuntime`` request body (live-reconciled shape).

    Container artifact + execution role + PUBLIC network + the HTTP server protocol our
    `BedrockAgentCoreApp` serves (`/invocations` + `/ping` on :8080). No JWT authorizer
    (IAM-only invoke). MCP auth header values ride as ``environmentVariables``."""
    body: dict[str, Any] = {
        "agentRuntimeName": safe_ident(plan.display_name),
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "clientToken": client_token,
    }
    if env_vars:
        body["environmentVariables"] = dict(env_vars)
    return body


def _extract_runtime(resp: Any) -> tuple[str, str, str]:
    """Pull ``(id, arn, status)`` from a create/get response (tolerant of flat/nested)."""
    if not isinstance(resp, dict):
        return "", "", ""
    r = resp.get("agentRuntime") if isinstance(resp.get("agentRuntime"), dict) else resp
    rid = r.get("agentRuntimeId") or r.get("id") or resp.get("agentRuntimeId") or ""
    arn = r.get("agentRuntimeArn") or r.get("arn") or resp.get("agentRuntimeArn") or ""
    status = r.get("status") or resp.get("status") or ""
    return rid, arn, status


# --------------------------------------------------------------------------- #
# deploy (build-only always; hosted path gated on a committed live receipt)
# --------------------------------------------------------------------------- #
def deploy_bedrock(
    project: Project,
    *,
    region: str = DEFAULT_BEDROCK_REGION,
    build_only: bool = False,
    skip_unsupported: bool = False,
    build_root: Optional[str] = None,
    execution_role_arn: Optional[str] = None,
    ecr_repo: Optional[str] = None,
    log: Callable[[str], None] = print,
    # seams (overridable for offline tests)
    control_client: Any = None,
    ecr_client: Any = None,
    docker_runner: Optional[Callable[..., None]] = None,
    sleep: Callable[[float], None] = _default_sleep,
    poll_max_attempts: int = 90,
    poll_delay: float = 10.0,
) -> BedrockDeployResult:
    """Build the Bedrock artifact (``build_only=True``) or run the hosted Runtime deploy.

    ``build_only=True`` materializes the deployable artifact and returns
    ``action="build"``. ``build_only=False`` runs the live hosted create (ECR push +
    ``CreateAgentRuntime`` + poll + lock) -- now the normal path, since
    ``runtime_hosted_deploy_allowed()`` is True (receipt-verified). If the gate is ever
    forced closed it refuses with ``HostedDeployNotLiveVerified`` (the
    confirm-live-before-encoding backstop); ``--build-only`` always just emits the artifact.
    """
    plan = build_bedrock_plan(project, region=region, skip_unsupported=skip_unsupported)
    if not plan.deployable:
        raise ValueError(
            "Bedrock plan has errors; not deployable. Run "
            "`agentlift plan <path> --target bedrock` to see them."
        )

    if not build_only:
        if not runtime_hosted_deploy_allowed():
            raise HostedDeployNotLiveVerified(
                "Hosted deploy to Bedrock AgentCore Runtime is pending a committed live "
                "receipt (needs AWS IAM + an execution role + ECR). No network call was "
                "made and nothing was written. Run `agentlift deploy --target bedrock "
                "--build-only` to materialize the deployable container artifact + runbook."
            )
        return _hosted_deploy(
            plan, project, region=region, build_root=build_root,
            execution_role_arn=execution_role_arn, ecr_repo=ecr_repo, log=log,
            control_client=control_client, ecr_client=ecr_client,
            docker_runner=docker_runner, sleep=sleep,
            poll_max_attempts=poll_max_attempts, poll_delay=poll_delay,
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


# --------------------------------------------------------------------------- #
# hosted Runtime deploy (gated; build context -> ECR -> CreateAgentRuntime -> lock)
# --------------------------------------------------------------------------- #
def _hosted_deploy(
    plan: BedrockDeployPlan,
    project: Project,
    *,
    region: str,
    build_root: Optional[str],
    execution_role_arn: Optional[str],
    ecr_repo: Optional[str],
    log: Callable[[str], None],
    control_client: Any,
    ecr_client: Any,
    docker_runner: Optional[Callable[..., None]],
    sleep: Callable[[float], None],
    poll_max_attempts: int,
    poll_delay: float,
) -> BedrockDeployResult:
    """Materialize the build context, push an ARM64 image to ECR, create/update the
    AgentCore Runtime, poll to READY, and write the ``.agentlift-bedrock.json`` lock.

    Idempotent via the lock's spec hash (``decide_action``): unchanged spec + same region
    -> ``skip`` (no build, no push, no API call); changed spec -> ``update``; first deploy
    or region move -> ``create``. MCP auth header values are resolved from the local env
    into the runtime's ``environmentVariables`` (never written to disk)."""
    role = execution_role_arn or os.environ.get(RUNTIME_EXECUTION_ROLE_ENV)
    if not role:
        raise RuntimeExecutionRoleRequired(
            f"Hosted Runtime deploy needs an execution role ARN. Set "
            f"{RUNTIME_EXECUTION_ROLE_ENV} (trust bedrock-agentcore.amazonaws.com; allow "
            f"ECR pull + bedrock:InvokeModel + CloudWatch Logs) or pass execution_role_arn=."
        )

    lock = BedrockLock.load(project.root)
    decision = decide_action(lock, plan.spec_hash, region=region)
    if decision.action == "skip":
        log(f"  skip: {decision.reason}")
        return BedrockDeployResult(
            action="skip", region=region, spec_hash=plan.spec_hash,
            display_name=plan.display_name,
            deploy_model=plan.agents[0].bedrock_model if plan.agents else "",
            env_var_names=list(plan.env_var_names),
            agent_runtime_arn=lock.agent_runtime_arn or "",
        )

    control = control_client or _default_control_client(region)
    ecr = ecr_client or _default_ecr_client(region)
    runner = docker_runner or _default_docker_runner

    # 1. build context -> ARM64 image -> ECR
    handles = build_artifact(plan, project.root, build_root)
    log(f"  built container context: {handles['build_dir']} "
        f"({len(plan.agents)} agent(s), {len(plan.skill_bundles)} skill bundle(s))")
    repo = ecr_repo or _ecr_repo_name(plan)
    registry_uri = _ensure_ecr_repo(ecr, repo, log)               # <acct>.dkr.ecr...<repo>
    registry = registry_uri.split("/", 1)[0]
    image_uri = f"{registry_uri}:{plan.spec_hash[:12]}"
    _build_and_push(build_dir=handles["build_dir"], image_uri=image_uri,
                    registry=registry, ecr_client=ecr, log=log, runner=runner)

    # 2. resolve MCP auth header values -> runtime environmentVariables (never persisted)
    env_vars, unresolved = resolve_auth_env_vars(project, log)
    if unresolved:
        log(f"  warning: unresolved MCP auth env var(s): {', '.join(unresolved)}")

    # 3. CreateAgentRuntime / UpdateAgentRuntime, then poll to READY
    client_token = f"agentlift-{plan.spec_hash[:24]}"
    body = _runtime_create_body(plan, image_uri=image_uri, role_arn=role,
                                env_vars=env_vars, client_token=client_token)
    if decision.action == "update" and lock.agent_runtime_id:
        log(f"  UpdateAgentRuntime {lock.agent_runtime_id} ({decision.reason})...")
        upd = dict(body); upd.pop("agentRuntimeName", None); upd.pop("clientToken", None)
        upd["agentRuntimeId"] = lock.agent_runtime_id
        resp = control.update_agent_runtime(**upd)
    else:
        log(f"  CreateAgentRuntime {body['agentRuntimeName']} ({decision.reason})...")
        try:
            resp = control.create_agent_runtime(**body)
        except Exception as exc:  # noqa: BLE001
            # an idempotent clientToken whose runtime was since deleted is rejected by
            # AWS -- retry once without it (mirrors the harness create quirk).
            if "ClientToken" in str(exc) or "clientToken" in str(exc):
                log("  clientToken conflict (resource since deleted); retrying without it")
                retry = dict(body); retry.pop("clientToken", None)
                resp = control.create_agent_runtime(**retry)
            else:
                raise

    runtime_id, runtime_arn, status = _extract_runtime(resp)
    status = _poll_runtime_ready(control, runtime_id, status, log=log,
                                 sleep=sleep, max_attempts=poll_max_attempts,
                                 delay=poll_delay)
    log(f"  runtime {runtime_id} -> {status}  arn={runtime_arn}")

    # 4. persist the lock (real ARN -- caller anonymizes before commit)
    deploy_model = plan.agents[0].bedrock_model if plan.agents else ""
    lock.record(agent_runtime_id=runtime_id, agent_runtime_arn=runtime_arn,
                region=region, spec_hash=plan.spec_hash,
                display_name=plan.display_name, deploy_model=deploy_model)
    lock.save()

    return BedrockDeployResult(
        action=decision.action, region=region, spec_hash=plan.spec_hash,
        display_name=plan.display_name, deploy_model=deploy_model,
        env_var_names=list(plan.env_var_names), build_dir=handles["build_dir"],
        agent_runtime_arn=runtime_arn,
    )


def _poll_runtime_ready(
    control: Any, runtime_id: str, status: str, *,
    log: Callable[[str], None], sleep: Callable[[float], None],
    max_attempts: int, delay: float,
) -> str:
    """Poll ``get_agent_runtime`` until READY or a terminal failure; raise on failure
    or timeout. Returns immediately if the create response was already READY."""
    attempts = 0
    while status not in _RT_READY_STATES:
        if status in _RT_FAILED_STATES:
            raise RuntimeDeployFailed(
                f"AgentCore Runtime {runtime_id} reached terminal state {status}")
        if attempts >= max_attempts:
            raise RuntimeDeployFailed(
                f"AgentCore Runtime {runtime_id} not READY after {max_attempts} polls "
                f"(last status {status or 'unknown'})")
        attempts += 1
        sleep(delay)
        _, _, status = _extract_runtime(control.get_agent_runtime(agentRuntimeId=runtime_id))
        log(f"    ... {status or 'provisioning'} (poll {attempts})")
    return status


def invoke_agent_runtime(
    agent_runtime_arn: str, prompt: str, *,
    region: str = DEFAULT_BEDROCK_REGION,
    session_id: str = "agentlift-runtime-verify-session-000000000",
    data_client: Any = None,
) -> dict:
    """Invoke a deployed AgentCore Runtime once. The container serves ``POST
    /invocations``; AgentCore exposes it as ``invoke_agent_runtime`` on the data-plane
    ``bedrock-agentcore`` client. ``runtimeSessionId`` must be >= 33 chars (same floor as
    the harness). Returns the parsed JSON body (the Strands agent's response envelope)."""
    import json
    client = data_client or (__import__("boto3").client("bedrock-agentcore", region_name=region))
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=agent_runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt}).encode("utf-8"),
    )
    body = resp.get("response")
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    try:
        return json.loads(body) if body else {}
    except (json.JSONDecodeError, TypeError):
        return {"raw": body}
