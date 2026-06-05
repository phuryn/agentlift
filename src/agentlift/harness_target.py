"""Live deploy to an Amazon Bedrock AgentCore *harness* (the managed single agent).

The pure half (``harness_plan``) turns the folder into a deterministic
``HarnessDeployPlan``; this module is the only harness file that touches the
network. It:

  1. builds the plan (Claude-native model, remote_mcp tools, allowedTools),
  2. reads ``.agentlift-harness.json`` and decides create / update / skip from the
     plan's spec hash (idempotent: an unchanged folder re-deploys to nothing),
  3. resolves MCP auth header values from the *local* environment and folds them
     straight into ``config.remoteMcp.headers`` over the wire (secrets stay out of
     the source, the plan, and the lockfile -- only their env-var names are written
     down),
  4. calls the control-plane ``CreateHarness`` / ``UpdateHarness`` (boto3
     ``bedrock-agentcore-control``, SigV4/IAM -- *not* the Bedrock bearer token,
     which does model inference only and cannot create a resource), polls
     ``GetHarness`` until ``READY``, and records the harness id/ARN + spec hash to
     the lockfile,
  5. exposes ``invoke_harness`` (data-plane ``bedrock-agentcore``) for the
     live-verify step that earns the receipt.

**Why this is the lighter of the two live paths.** ``bedrock_target``'s hosted
runtime create needs a Docker/ECR image build and the ``create_agent_runtime``
control-plane shape (Gate B) -- now live-verified too (``_RUNTIME_LIVE_VERIFIED``).
The harness create is config-only and IAM-only: minutes, no container, no ECR.
So agentlift *runs* it -- that live run is precisely how the wire shape earns its
receipt -- but keeps the confirm-live-before-encoding rule honest with three
guards: every plan carries the standing ``bedrock.harness.preview`` diagnostic,
``deploy_harness`` prints a loud PROVISIONAL banner before any network call, and
``HarnessDeployPlan.live_verified`` stays ``False`` (``_HARNESS_LIVE_VERIFIED`` in
``harness_plan``) until a committed receipt flips it. The shape encoded in
``to_create_body`` is exactly what the first live deploy verifies and reconciles.

The control-plane field/return shapes below were read from AWS docs published
after this build's knowledge cutoff, so the response extraction is deliberately
tolerant (flat or nested) and every guess is marked PROVISIONAL; the live run is
what settles them.

Requires: pip install "boto3>=1.40".
Env: AWS credentials (IAM), ``AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN`` (the harness
     execution role; trust ``bedrock-agentcore.amazonaws.com``). The ``agentcore``
     starter toolkit can auto-create this role. See docs/deploy-bedrock.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .bedrock_target import resolve_auth_env_vars
from .harness_lock import HarnessLock, decide_action
from .harness_plan import (
    DEFAULT_HARNESS_REGION,
    HarnessDeployPlan,
    build_harness_plan,
)
from .model import Project

# Env var the CLI reads to find the harness execution role for a create. The
# starter `agentcore` toolkit auto-creates a suitable role; otherwise create one
# whose trust policy allows bedrock-agentcore.amazonaws.com to assume it and whose
# permissions include bedrock:InvokeModel (+ logs / browser / code-interpreter).
EXECUTION_ROLE_ENV = "AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN"

# Env var naming the S3 bucket agentlift uploads skill bundles to (referenced by the
# harness as skills[].s3.uri; live-verified). The harness execution role must have
# s3:ListBucket + s3:GetObject on this bucket. Only needed when the folder has skills.
S3_BUCKET_ENV = "AGENTLIFT_BEDROCK_S3_BUCKET"

# Where skill bundles land in the bucket: agentlift-skills/<harness>/<skill>/<files>.
_SKILL_S3_PREFIX = "agentlift-skills"

# Lifecycle states (provisional spellings -- reconciled by the first live GetHarness).
_READY_STATES = {"READY", "ACTIVE", "AVAILABLE"}
_FAILED_STATES = {"FAILED", "CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}

_PROVISIONAL_BANNER = (
    "  +----------------------------------------------------------------+\n"
    "  |  AgentCore HARNESS deploy is a PREVIEW (provisional wire shape) |\n"
    "  |  The CreateHarness body has no committed live receipt yet; this |\n"
    "  |  run is how it earns one. A failure here may mean the shape     |\n"
    "  |  needs reconciling, not that your folder is wrong.              |\n"
    "  +----------------------------------------------------------------+"
)


class HarnessExecutionRoleRequired(RuntimeError):
    """A harness create needs an execution role ARN and none was supplied.

    Set ``AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN`` (or pass ``execution_role_arn``).
    The trust policy must allow ``bedrock-agentcore.amazonaws.com`` to assume it."""


class HarnessDeployFailed(RuntimeError):
    """The harness reached a terminal failed state, or polling timed out."""


@dataclass
class HarnessDeployResult:
    action: str                       # "create" | "update" | "skip"
    harness_id: str
    harness_arn: str
    region: str
    spec_hash: str
    display_name: str
    deploy_model: str
    status: str = ""
    env_var_names: list[str] = field(default_factory=list)
    live_verified: bool = False

    @property
    def changed(self) -> bool:
        return self.action != "skip"


# --------------------------------------------------------------------------- #
# network seams (overridable for offline tests)
# --------------------------------------------------------------------------- #
def _default_control_client(region: str) -> Any:
    import boto3
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _default_data_client(region: str) -> Any:
    import boto3
    return boto3.client("bedrock-agentcore", region_name=region)


def _default_s3_client(region: str) -> Any:
    import boto3
    return boto3.client("s3", region_name=region)


class HarnessSkillBucketRequired(RuntimeError):
    """The folder has skill(s) but no S3 bucket was supplied to upload them to.

    Set ``AGENTLIFT_BEDROCK_S3_BUCKET`` (or pass ``skills_bucket``). The harness loads
    skill bundles from S3 (``skills[].s3.uri``); the execution role needs
    ``s3:ListBucket`` + ``s3:GetObject`` on that bucket."""


def _upload_skills(plan, *, bucket: str, s3_client: Any,
                   log: Callable[[str], None]) -> dict[str, str]:
    """Upload each skill bundle's files to S3 and return ``{skill_name: s3_uri_prefix}``.

    Layout: ``s3://<bucket>/agentlift-skills/<harness_name>/<skill_name>/<arcname>``. The
    returned prefix (trailing slash) is what the harness ``skills[].s3.uri`` points at; the
    live harness ``ListObjectsV2``'s the prefix and loads the bundle (verified)."""
    uris: dict[str, str] = {}
    for sk in plan.skills:
        base = f"{_SKILL_S3_PREFIX}/{plan.harness_name}/{sk.name}"
        for arcname, abs_path in sk.files:
            # arcnames are bundle-relative as `<skill>/<path>` (e.g. house-style/SKILL.md);
            # strip the leading `<skill>/` so SKILL.md lands directly under the prefix the
            # harness fetches (it expects SKILL.md at skills[].s3.uri, not nested).
            rel = arcname
            if rel.startswith(sk.name + "/"):
                rel = rel[len(sk.name) + 1:]
            key = f"{base}/{rel.lstrip('/')}"
            with open(abs_path, "rb") as fh:
                s3_client.put_object(Bucket=bucket, Key=key, Body=fh.read())
        uris[sk.name] = f"s3://{bucket}/{base}/"
        log(f"  skill '{sk.name}': uploaded {len(sk.files)} file(s) -> {uris[sk.name]}")
    return uris


def _default_sleep(seconds: float) -> None:  # pragma: no cover - real wall clock
    import time
    time.sleep(seconds)


def _client_token(plan: HarnessDeployPlan) -> str:
    """A deterministic idempotency token: re-running the same spec dedupes at the
    AWS layer too (CreateHarness ``clientToken``). No clock, no randomness."""
    return f"agentlift-{plan.spec_hash[:48]}"


def _extract_harness(resp: Any) -> tuple[str, str, str]:
    """Pull ``(id, arn, status)`` from a create/get response (PROVISIONAL).

    Tolerant of flat (``{"harnessId": ...}``) or nested (``{"harness": {...}}``)
    shapes since the exact response envelope is not live-verified yet."""
    if not isinstance(resp, dict):
        return "", "", ""
    h = resp.get("harness") if isinstance(resp.get("harness"), dict) else resp
    hid = h.get("harnessId") or h.get("id") or resp.get("harnessId") or ""
    arn = h.get("harnessArn") or h.get("arn") or resp.get("harnessArn") or ""
    status = h.get("status") or resp.get("status") or ""
    return hid, arn, status


def _update_body(create_body: dict[str, Any], harness_id: str) -> dict[str, Any]:
    """Reshape a create body into an UpdateHarness body (PROVISIONAL).

    Carries the mutable config (model / systemPrompt / tools / allowedTools) keyed
    by ``harnessId``; drops create-only fields (``harnessName`` is immutable, the
    ``clientToken`` is per-create). ``executionRoleArn`` is kept -- whether it is
    updatable is one of the things the live run reconciles."""
    body: dict[str, Any] = {"harnessId": harness_id}
    for key in ("model", "systemPrompt", "tools", "allowedTools", "executionRoleArn"):
        if key in create_body:
            body[key] = create_body[key]
    return body


def _poll_until_ready(
    control: Any, harness_id: str, *, log: Callable[[str], None],
    sleep: Callable[[float], None], max_attempts: int, delay: float,
) -> str:
    """Poll ``GetHarness`` until READY (or a terminal failure / timeout).

    The ``GetHarness`` identifier kwarg spelling is provisional; reconciled live."""
    last = ""
    for _ in range(max_attempts):
        resp = control.get_harness(harnessId=harness_id)
        _id, _arn, status = _extract_harness(resp)
        last = status or last
        if status in _READY_STATES:
            return status
        if status in _FAILED_STATES:
            raise HarnessDeployFailed(
                f"harness {harness_id} entered terminal state {status!r}")
        log(f"  ...status {status or 'unknown'} (waiting)")
        sleep(delay)
    raise HarnessDeployFailed(
        f"harness {harness_id} did not reach READY within "
        f"{max_attempts} polls (last status {last or 'unknown'})")


# --------------------------------------------------------------------------- #
# deploy
# --------------------------------------------------------------------------- #
def deploy_harness(
    project: Project,
    *,
    region: str = DEFAULT_HARNESS_REGION,
    execution_role_arn: Optional[str] = None,
    skills_bucket: Optional[str] = None,
    skip_unsupported: bool = False,
    log: Callable[[str], None] = print,
    # seams (overridable for offline tests)
    control_client: Any = None,
    s3_client: Any = None,
    sleep: Callable[[float], None] = _default_sleep,
    poll_max_attempts: int = 60,
    poll_delay: float = 5.0,
) -> HarnessDeployResult:
    """Deploy ``project`` to one managed AgentCore harness, idempotently.

    Runs a real (preview) ``CreateHarness`` / ``UpdateHarness`` behind a loud
    PROVISIONAL banner. Raises ``ValueError`` if the plan has errors (run
    ``agentlift plan --target bedrock --mode harness`` to see them) and
    ``HarnessExecutionRoleRequired`` if a create is needed without a role ARN.
    """
    plan = build_harness_plan(project, region=region, skip_unsupported=skip_unsupported)
    if not plan.deployable:
        raise ValueError(
            "Harness plan has errors; not deployable. Run "
            "`agentlift plan <path> --target bedrock --mode harness` to see them. "
            "(Multi-agent / subagent / skill folders should deploy with --mode "
            "runtime, or --mode auto.)"
        )

    # honesty guard: the shape is not live-verified, so say so before any network.
    if not plan.live_verified:
        log(_PROVISIONAL_BANNER)

    lock = HarnessLock.load(project.root)
    decision = decide_action(lock, plan.spec_hash, region=region)
    if decision.action == "skip":
        log(f"  up to date ({decision.reason}); nothing to deploy.")
        return HarnessDeployResult(
            "skip", lock.harness_id or "", lock.harness_arn or "", region,
            plan.spec_hash, plan.display_name, plan.bedrock_model, status="READY",
            env_var_names=list(plan.env_var_names), live_verified=plan.live_verified,
        )

    if decision.action == "create":
        role = execution_role_arn
        if not role:
            raise HarnessExecutionRoleRequired(
                f"creating a harness needs an execution role ARN. Set "
                f"${EXECUTION_ROLE_ENV} (trust bedrock-agentcore.amazonaws.com), or "
                f"let the `agentcore` starter toolkit create one. No network call "
                f"was made."
            )

    # resolve MCP auth header values from the LOCAL env -> wire headers (never to disk)
    env_vars, _unresolved = resolve_auth_env_vars(project, log=log)
    if plan.env_var_names:
        log(f"  MCP auth -> harness header(s) from local env: "
            f"{', '.join(plan.env_var_names)}")

    # upload skill bundles to S3 and resolve their skills[].s3.uri prefixes
    skill_s3_uris: dict[str, str] = {}
    if plan.skills:
        bucket = skills_bucket or os.environ.get(S3_BUCKET_ENV)
        if not bucket:
            raise HarnessSkillBucketRequired(
                f"folder has skill(s) {', '.join(s.name for s in plan.skills)}; set "
                f"${S3_BUCKET_ENV} to an S3 bucket agentlift can upload them to (the "
                f"harness loads skills from S3, and the execution role needs s3:ListBucket "
                f"+ s3:GetObject on it). No network call was made."
            )
        s3 = s3_client if s3_client is not None else _default_s3_client(region)
        log(f"  uploading {len(plan.skills)} skill bundle(s) to s3://{bucket}/...")
        skill_s3_uris = _upload_skills(plan, bucket=bucket, s3_client=s3, log=log)

    body = plan.to_create_body(
        execution_role_arn=execution_role_arn or "",
        client_token=_client_token(plan),
        mcp_headers=env_vars,
        skill_s3_uris=skill_s3_uris,
    )

    control = control_client if control_client is not None else _default_control_client(region)
    log(f"  {decision.action}: {decision.reason}")
    log(f"  model: {plan.folder_model} -> {plan.bedrock_model}  region: {region}")

    if decision.action == "create":
        log("  CreateHarness (managed, config-only -- no container build)...")
        try:
            resp = control.create_harness(**body)
        except Exception as e:  # noqa: BLE001 - seam may raise botocore ClientError or a fake
            # AWS remembers a clientToken -> resource mapping even after the resource is
            # deleted, and refuses to reuse it ("Resource previously created with
            # clientToken ... has since been deleted. Please retry without clientToken").
            # Our token is the (deterministic) spec hash, so a delete-then-redeploy of the
            # same spec hits this. The lockfile already gives agentlift-layer idempotency,
            # so per AWS's own guidance we retry once without the token to create fresh.
            msg = str(e)
            if "clientToken" in msg and ("has since been deleted" in msg
                                         or "without clientToken" in msg):
                log("  clientToken maps to a since-deleted resource; "
                    "retrying CreateHarness without it...")
                resp = control.create_harness(
                    **{k: v for k, v in body.items() if k != "clientToken"})
            else:
                raise
    else:
        log("  UpdateHarness (in place; keeps the harness id/ARN)...")
        resp = control.update_harness(**_update_body(body, lock.harness_id or ""))

    harness_id, harness_arn, status = _extract_harness(resp)
    harness_id = harness_id or lock.harness_id or ""
    harness_arn = harness_arn or lock.harness_arn or ""
    if status not in _READY_STATES:
        status = _poll_until_ready(
            control, harness_id, log=log, sleep=sleep,
            max_attempts=poll_max_attempts, delay=poll_delay,
        )
    log(f"  harness {status}: {harness_arn or harness_id}")

    lock.record(
        harness_id=harness_id, harness_arn=harness_arn, region=region,
        spec_hash=plan.spec_hash, display_name=plan.display_name,
        deploy_model=plan.bedrock_model,
    )
    lock.save()
    log(f"  wrote {lock.path}")

    return HarnessDeployResult(
        decision.action, harness_id, harness_arn, region, plan.spec_hash,
        plan.display_name, plan.bedrock_model, status=status,
        env_var_names=list(plan.env_var_names), live_verified=plan.live_verified,
    )


# --------------------------------------------------------------------------- #
# invoke (data-plane; the live-verify step that earns the receipt)
# --------------------------------------------------------------------------- #
def invoke_harness(
    harness_arn: str, prompt: str, *, region: str = DEFAULT_HARNESS_REGION,
    session_id: str = "agentlift-harness-verify-session-000001", data_client: Any = None,
) -> Any:
    """Invoke a deployed harness with ``prompt`` (data-plane ``InvokeHarness``).

    Reconciled against the installed ``bedrock-agentcore`` botocore model (more
    authoritative than the post-cutoff docs the first cut was written from):
    ``InvokeHarness`` keys on the harness **ARN** (not its id), requires a
    ``runtimeSessionId`` (pattern ``[a-zA-Z0-9][a-zA-Z0-9-_]*``, **min length 33** --
    live-discovered: a shorter id fails client-side param validation), and takes a
    Converse-style ``messages`` array -- there is no ``payload`` field. It returns
    an event ``stream`` (``messageStart`` / ``contentBlockDelta`` / ``messageStop``
    / ``metadata.usage``), so the live-verify step asserts on the objective
    tool-call / response events in that stream. What the first live invoke still
    settles is runtime *behavior* (event ordering, the tool_use block content), not
    the request envelope."""
    data = data_client if data_client is not None else _default_data_client(region)
    return data.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )


# --------------------------------------------------------------------------- #
# teardown (cleanup live experiments)
# --------------------------------------------------------------------------- #
def delete_harness(
    harness_id: str, *, region: str = DEFAULT_HARNESS_REGION,
    control_client: Any = None, log: Callable[[str], None] = print,
) -> None:
    """Delete a harness (``DeleteHarness``) -- for tearing down live test deploys."""
    control = control_client if control_client is not None else _default_control_client(region)
    control.delete_harness(harnessId=harness_id)
    log(f"  deleted harness {harness_id}")
