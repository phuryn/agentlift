"""agentlift command-line interface.

    agentlift validate <path>                 parse + plan, report problems
    agentlift plan     <path> [--json]        show the deterministic deploy plan (dry run, no network)
    agentlift audit    <path> [--targets ...] portability report across providers (no network)
    agentlift export   <target> <path>        compile the folder to a provider artifact (no network)
    agentlift deploy   <path> [--prune]       upload skills + create agents; write lockfile
    agentlift run      <agent> --task "..."   invoke a deployed agent (or --local)
    agentlift list     <path>                 show what is currently deployed (from the lockfile)
    agentlift destroy  <path>                 archive every agent in the lockfile
    agentlift bench    <agent> --task "..."   managed vs local: latency / tokens / cost / pass
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from typing import Optional

from . import __version__
from .anthropic_target import Deployer
from .graders import llm_grader, substring_grader
from .lockfile import Lockfile
from .parser import DEFAULT_MODEL, parse_project
from .planner import build_plan
from .runtime import RunResult, create_environment, run_local, run_managed


# --------------------------------------------------------------------------- #
# env + client helpers
# --------------------------------------------------------------------------- #
def load_env(*dirs: str) -> None:
    for d in dirs:
        path = os.path.join(d, ".env")
        if not os.path.isfile(path):
            continue
        for line in open(path, "r", encoding="utf-8").read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_client():
    try:
        import anthropic
    except ImportError:
        print("error: the 'anthropic' package is required. pip install anthropic", file=sys.stderr)
        sys.exit(2)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("error: ANTHROPIC_API_KEY not set (put it in .env or the environment).", file=sys.stderr)
        sys.exit(2)
    return anthropic.Anthropic(api_key=key)


# --------------------------------------------------------------------------- #
# pretty printers
# --------------------------------------------------------------------------- #
def print_diagnostics(diags) -> None:
    if diags.items:
        print("Diagnostics:")
        print(diags.render())


def print_plan(plan) -> None:
    print(f"\nSkills to upload: {len(plan.skill_uploads)}")
    for up in plan.skill_uploads:
        print(f"  - {up.display_title}  ({up.content_hash[:8]}, {len(up.files)} file(s))  used by: {', '.join(up.used_by)}")
    print(f"\nAgents to create: {len(plan.agent_creates)}")
    for ac in plan.agent_creates:
        req = ac.request
        def _fmt(c):
            ask = (c.get("permission_policy") or {}).get("type") == "always_ask"
            return c["name"] + ("(ask)" if ask else "")
        tools = []
        for t in req.get("tools", []):
            if t["type"] == "agent_toolset_20260401":
                if t.get("default_config", {}).get("enabled"):
                    tools.append("builtins:all")
                else:
                    tools.append("builtins:" + "/".join(_fmt(c) for c in t.get("configs", [])))
            elif t["type"] == "mcp_toolset":
                if t.get("default_config", {}).get("enabled"):
                    tools.append(f"mcp:{t['mcp_server_name']}:all")
                else:
                    tools.append(f"mcp:{t['mcp_server_name']}:" + "/".join(_fmt(c) for c in t.get("configs", [])))
        line = f"  - {ac.name}  [{req['model']}]"
        if ac.is_coordinator:
            line += "  (coordinator -> " + ", ".join(req["multiagent"]["agents"]) + ")"
        print(line)
        print(f"      tools: {', '.join(tools) or '(none)'}")
        if req.get("skills"):
            print(f"      skills: {', '.join(s['skill_ref'] for s in req['skills'])}")
        if req.get("mcp_servers"):
            print(f"      mcp: {', '.join(s['name'] + '=' + s['url'] for s in req['mcp_servers'])}")
    print()
    print_diagnostics(plan.diagnostics)
    print(f"\nDeployable: {'yes' if plan.deployable else 'NO (fix errors above)'}")


def print_google_plan(plan) -> None:
    """The Google Agent Engine plan: one engine, N agent nodes, shipped skill
    bundles, MCP toolset recipes, and the env vars a deploy must populate."""
    print(f"\nAgent Engine: {plan.display_name}  "
          f"(root: {plan.root_agent}, deploy model: {plan.deploy_model})")
    print(f"Agents: {len(plan.agents)}")
    for n in plan.agents:
        line = f"  - {n.name}  [{n.folder_model}]"
        if n.is_coordinator:
            line += "  (coordinator -> " + ", ".join(n.sub_agents) + ")"
        print(line)
        for r in n.mcp:
            tf = "/".join(r.tool_filter) if r.tool_filter else "all"
            auth = f"  auth->{', '.join(sorted(r.auth_env_vars.values()))}" if r.auth_env_vars else ""
            print(f"      mcp: {r.server}={r.url} [{tf}]{auth}")
        if n.skills:
            print(f"      skills: {', '.join(n.skills)}")
    print(f"\nSkill bundles to ship: {len(plan.skill_bundles)}")
    for b in plan.skill_bundles:
        print(f"  - {b.name}  ({b.content_hash[:8]}, {len(b.files)} file(s))  "
              f"used by: {', '.join(b.used_by)}")
    if plan.env_var_names:
        print("\nAgent Engine env vars the deploy will populate from your local env (MCP auth):")
        for name in plan.env_var_names:
            print(f"  - {name}")
    print(f"\nRequirements: {', '.join(plan.requirements)}")
    print(f"Spec hash: {plan.spec_hash[:12]}")
    print()
    print_diagnostics(plan.diagnostics)
    print(f"\nDeployable: {'yes' if plan.deployable else 'NO (fix errors above)'}")


def print_bedrock_plan(plan) -> None:
    """The Bedrock AgentCore plan: one runtime, N agent nodes (Claude mapped to a
    regional inference profile -- native, not remapped), shipped skill bundles,
    Strands MCP recipes, and the env vars a deploy must populate."""
    print(f"\nAgentCore Runtime: {plan.display_name}  "
          f"(root: {plan.root_agent}, region: {plan.region})")
    print(f"Agents: {len(plan.agents)}")
    for n in plan.agents:
        line = f"  - {n.name}  [{n.folder_model} -> {n.bedrock_model}]"
        if n.is_coordinator:
            line += "  (coordinator -> " + ", ".join(n.sub_agents) + ")"
        print(line)
        for r in n.mcp:
            tf = "/".join(r.tool_filter) if r.tool_filter else "all"
            auth = f"  auth->{', '.join(sorted(r.auth_env_vars.values()))}" if r.auth_env_vars else ""
            print(f"      mcp: {r.server}={r.url} [{tf}]{auth}")
        if n.skills:
            print(f"      skills: {', '.join(n.skills)}")
    print(f"\nSkill bundles to ship: {len(plan.skill_bundles)}")
    for b in plan.skill_bundles:
        print(f"  - {b.name}  ({b.content_hash[:8]}, {len(b.files)} file(s))  "
              f"used by: {', '.join(b.used_by)}")
    if plan.env_var_names:
        print("\nAgentCore env vars the deploy will populate from your local env (MCP auth):")
        for name in plan.env_var_names:
            print(f"  - {name}")
    print(f"\nRequirements: {', '.join(plan.requirements)}")
    print(f"Spec hash: {plan.spec_hash[:12]}")
    print()
    print_diagnostics(plan.diagnostics)
    print(f"\nDeployable: {'yes' if plan.deployable else 'NO (fix errors above)'}")


def print_harness_plan(plan, *, mode_reason: str = "") -> None:
    """The Bedrock AgentCore *harness* plan: one MANAGED single agent (config-only,
    no container, IAM-only deploy), Claude mapped to a regional inference profile
    (native, not remapped), remote_mcp tools, the glob allowlist, and the env vars
    a deploy populates from the local env. A PREVIEW shape -- see diagnostics."""
    print(f"\nAgentCore Harness: {plan.display_name}  "
          f"(name: {plan.harness_name}, region: {plan.region})")
    if mode_reason:
        print(f"  mode: harness  ({mode_reason})")
    print(f"  model: {plan.folder_model} -> {plan.bedrock_model}")
    if plan.instruction:
        print(f"  systemPrompt: {len(plan.instruction)} char(s)")
    for m in plan.mcp:
        auth = (f"  auth->{', '.join(sorted(m.auth_env_vars.values()))}"
                if m.auth_env_vars else "")
        print(f"      mcp: {m.server}={m.url}{auth}")
    if plan.builtin_tool_types:
        print(f"      builtin tool types: {', '.join(plan.builtin_tool_types)}")
    if plan.allowed_tools:
        print(f"      allowedTools: {', '.join(plan.allowed_tools)}")
    if plan.env_var_names:
        print("\nHarness env vars the deploy will populate from your local env (MCP auth):")
        for name in plan.env_var_names:
            print(f"  - {name}")
    print(f"\nSpec hash: {plan.spec_hash[:12]}")
    print("Live-verified wire shape: "
          + ("yes" if plan.live_verified else "no (PREVIEW -- provisional CreateHarness shape)"))
    print()
    print_diagnostics(plan.diagnostics)
    print(f"\nDeployable: {'yes' if plan.deployable else 'NO (fix errors above)'}")


def _resolve_bedrock_mode(project, args):
    """Resolve the Bedrock primitive and its region from ``--mode`` / ``--bedrock-region``.

    ``--mode auto`` defers to ``select_bedrock_mode`` (least-powerful-mode-that-
    preserves-semantics, never a silent downgrade). The two primitives have
    different default regions -- the managed harness is preview in a specific set
    (default us-west-2), the runtime composition was verified in eu-north-1 -- so an
    unset ``--bedrock-region`` resolves per mode; an explicit one always wins.
    Returns ``(mode, region, reason)`` where ``reason`` explains an auto choice."""
    from .bedrock_plan import DEFAULT_BEDROCK_REGION
    from .harness_plan import DEFAULT_HARNESS_REGION, select_bedrock_mode
    mode = getattr(args, "mode", "auto")
    reason = ""
    if mode == "auto":
        mode, reason = select_bedrock_mode(project)
    default_region = DEFAULT_HARNESS_REGION if mode == "harness" else DEFAULT_BEDROCK_REGION
    region = getattr(args, "bedrock_region", None) or default_region
    return mode, region, reason


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_validate(args) -> int:
    project, diags = parse_project(args.path, default_model=args.model)
    plan = build_plan(project, diags, skip_unsupported=args.skip_unsupported)
    print(f"Project: {project.root}  (layout: {project.layout})")
    print(f"Agents: {', '.join(a.name for a in project.agents) or '(none)'}")
    print_plan(plan)
    return 0 if plan.deployable else 1


def cmd_plan(args) -> int:
    project, diags = parse_project(args.path, default_model=args.model)
    target = getattr(args, "target", "anthropic")
    if target == "google":
        from .google_plan import build_google_plan
        plan = build_google_plan(project, diags, model=args.google_model,
                                 skip_unsupported=args.skip_unsupported)
        if args.json:
            print(json.dumps(plan.to_dict(), indent=2))
        else:
            print(f"Project: {project.root}  (layout: {project.layout})  target: google")
            print_google_plan(plan)
        return 0 if plan.deployable else 1
    if target == "bedrock":
        mode, region, reason = _resolve_bedrock_mode(project, args)
        if mode == "harness":
            from .harness_plan import build_harness_plan
            plan = build_harness_plan(project, diags, region=region,
                                      skip_unsupported=args.skip_unsupported)
            if args.json:
                print(json.dumps(plan.to_dict(), indent=2))
            else:
                print(f"Project: {project.root}  (layout: {project.layout})  "
                      f"target: bedrock  mode: harness")
                print_harness_plan(plan, mode_reason=reason)
            return 0 if plan.deployable else 1
        from .bedrock_plan import build_bedrock_plan
        plan = build_bedrock_plan(project, diags, region=region,
                                  skip_unsupported=args.skip_unsupported)
        if args.json:
            print(json.dumps(plan.to_dict(), indent=2))
        else:
            print(f"Project: {project.root}  (layout: {project.layout})  "
                  f"target: bedrock  mode: runtime" + (f"  ({reason})" if reason else ""))
            print_bedrock_plan(plan)
        return 0 if plan.deployable else 1
    plan = build_plan(project, diags, skip_unsupported=args.skip_unsupported)
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
    else:
        print(f"Project: {project.root}  (layout: {project.layout})")
        print_plan(plan)
    return 0 if plan.deployable else 1


def cmd_diff(args) -> int:
    from .diff import check_remote, compute_diff, render_diff
    project, diags = parse_project(args.path, default_model=args.model)
    plan = build_plan(project, diags, skip_unsupported=args.skip_unsupported)
    lock = Lockfile.load(os.path.abspath(args.path))
    d = compute_diff(plan, lock)
    if args.remote:
        load_env(os.getcwd(), os.path.abspath(args.path))
        from .anthropic_target import BETAS
        client = get_client()
        d.remote_missing_agents, d.remote_missing_skills = check_remote(lock, client, BETAS)
    print(f"Project: {project.root}  (layout: {project.layout})")
    print(render_diff(d))
    if not plan.deployable:
        print()
        print_diagnostics(plan.diagnostics)
        return 1
    return 0


def _cmd_deploy_google(args) -> int:
    from .google_plan import build_google_plan
    from .google_target import build_package, deploy_google
    load_env(os.getcwd(), os.path.abspath(args.path))
    project, diags = parse_project(args.path, default_model=args.model)
    plan = build_google_plan(project, diags, model=args.google_model,
                             skip_unsupported=args.skip_unsupported)
    print(f"Project: {project.root}  (layout: {project.layout})  target: google")
    print_google_plan(plan)
    if not plan.deployable:
        print("\nNot deploying: fix the errors above (or pass --skip-unsupported to drop "
              "unsupported pieces).")
        return 1

    if args.build_only:
        handles = build_package(plan, project.root)
        print(f"\nBuilt source package (no deploy): {handles['build_dir']}")
        print(f"  module: {handles['module_name']}  app: {handles['app_symbol']}  "
              f"requirements: {', '.join(handles['requirements'])}")
        return 0

    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    bucket = os.environ.get("AGENTLIFT_GCP_STAGING_BUCKET")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not gcp_project or not bucket:
        print("error: set GOOGLE_CLOUD_PROJECT and AGENTLIFT_GCP_STAGING_BUCKET (gs://...) in the "
              "env, plus ADC (gcloud auth application-default login). See docs/deploy-google.md.",
              file=sys.stderr)
        return 2
    if not args.yes:
        if input("\nProceed with deploy? [y/N] ").strip().lower() != "y":
            print("aborted.")
            return 1
    print(f"Deploying to Google Vertex AI Agent Engine")
    print(f"  project={gcp_project}  region={location}  staging={bucket}")
    res = deploy_google(
        project, gcp_project=gcp_project, location=location,
        staging_bucket=bucket, model=args.google_model,
        skip_unsupported=args.skip_unsupported, log=print,
    )
    verb = {"create": "Deployed", "update": "Updated", "skip": "Up to date"}[res.action]
    print(f"\n{verb}. reasoningEngine: {res.resource_name}")
    if res.env_var_names and res.action != "skip":
        print(f"  populated env var(s): {', '.join(res.env_var_names)}")
    return 0


def _cmd_deploy_bedrock(args) -> int:
    """Bedrock AgentCore deploy -- two primitives behind ``--mode``.

    ``--mode auto`` (default) routes by ``select_bedrock_mode`` (a single skill-less
    agent -> harness; anything multi-agent / subagent / skill-bearing -> runtime).

      - **harness** (managed, config-only): RUNS a real (preview) CreateHarness /
        UpdateHarness via boto3 + IAM -- create/update/skip from ``.agentlift-harness.json``,
        no container. (See ``_cmd_deploy_harness``.)
      - **runtime** (custom container): a bare deploy RUNS the live hosted create
        (build ARM64 image -> ECR -> CreateAgentRuntime -> poll READY ->
        ``.agentlift-bedrock.json`` -> InvokeAgentRuntime), gated by
        ``_RUNTIME_LIVE_VERIFIED`` (now True, receipt-verified). ``--build-only`` instead
        materializes just the deployable container artifact + runbook and exits 0."""
    load_env(os.getcwd(), os.path.abspath(args.path))
    project, diags = parse_project(args.path, default_model=args.model)
    mode, region, reason = _resolve_bedrock_mode(project, args)
    if mode == "harness":
        # While the harness wire shape has no committed receipt, the live preview
        # create must be a *typed* opt-in (--mode harness), never the silent result
        # of the `auto` default. plan/dry-run is unaffected; only deploy is gated.
        from .harness_plan import harness_auto_deploy_allowed
        requested = getattr(args, "mode", "auto")
        if requested != "harness" and not harness_auto_deploy_allowed():
            print(f"Project: {project.root}  (layout: {project.layout})  "
                  f"target: bedrock  mode: harness  ({reason})")
            print("\nauto selected Bedrock AgentCore Harness for this single-agent project, "
                  "but harness live\ndeploy is still preview and not yet receipt-verified.")
            print("  Pass --mode harness to opt into the live preview create, or "
                  "--mode runtime --build-only\n  for the container artifact path.")
            return 2
        return _cmd_deploy_harness(args, project, region, reason)

    from .bedrock_plan import build_bedrock_plan
    from .bedrock_target import HostedDeployNotLiveVerified, deploy_bedrock
    plan = build_bedrock_plan(project, diags, region=region,
                              skip_unsupported=args.skip_unsupported)
    print(f"Project: {project.root}  (layout: {project.layout})  target: bedrock  mode: runtime"
          + (f"  ({reason})" if reason else ""))
    print_bedrock_plan(plan)
    if not plan.deployable:
        print("\nNot building: fix the errors above (or pass --skip-unsupported to drop "
              "unsupported pieces).")
        return 1

    if args.build_only:
        res = deploy_bedrock(project, region=region,
                             skip_unsupported=args.skip_unsupported, build_only=True, log=print)
        print(f"\nBuilt container artifact (no deploy): {res.build_dir}")
        print(f"  next: read {os.path.join(res.build_dir, 'NOTES.txt')} for the build/push + "
              f"hosted-create runbook.")
        return 0

    # bare deploy: RUNS the live hosted create (the gate is open, receipt-verified). The
    # refusal branch only fires if the gate is forced closed (a confirm-live regression guard).
    try:
        res = deploy_bedrock(project, region=region,
                             skip_unsupported=args.skip_unsupported, build_only=False, log=print)
    except HostedDeployNotLiveVerified as e:
        print("\nHosted deploy to Bedrock AgentCore Runtime (AgentCore is in AWS PREVIEW) is "
              "gated off in this build:")
        print("  - Gate A: submit the Anthropic use-case form (Bedrock console -> Model access).")
        print("  - Gate B: AWS IAM creds + an AgentCore execution role (iam:PassRole) + ECR.")
        print(f"\n  {e}")
        print("\n  (Use --build-only for the container artifact, or --mode harness for a single "
              "managed agent.)")
        return 3
    print(f"\n{res.action.title()}d AgentCore Runtime: {res.agent_runtime_arn or '(skip)'}")
    print(f"  region: {res.region}   spec: {res.spec_hash[:12]}   model: {res.deploy_model}")
    print(f"  lock: {os.path.join(project.root, '.agentlift-bedrock.json')}")
    return 0


def _cmd_deploy_harness(args, project, region, reason) -> int:
    """Deploy to a managed AgentCore *harness* (config-only, IAM-only -- no container).

    Unlike the runtime hosted create, this RUNS a real (preview) CreateHarness /
    UpdateHarness: the live run is how the provisional wire shape earns its receipt.
    ``--build-only`` is N/A (there is no container). The execution role comes from
    ``$AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN`` (or the ``agentcore`` starter toolkit)."""
    from .harness_plan import build_harness_plan
    from .harness_target import (EXECUTION_ROLE_ENV, HarnessDeployFailed,
                                 HarnessExecutionRoleRequired, deploy_harness)
    plan = build_harness_plan(project, region=region, skip_unsupported=args.skip_unsupported)
    print(f"Project: {project.root}  (layout: {project.layout})  target: bedrock  mode: harness")
    print_harness_plan(plan, mode_reason=reason)
    if not plan.deployable:
        print("\nNot deploying: fix the errors above (or pass --skip-unsupported to drop "
              "unsupported pieces; multi-agent / subagent / skill folders deploy with "
              "--mode runtime, or --mode auto).")
        return 1
    if args.build_only:
        print("\n--build-only is not applicable to the managed harness (config-only, no "
              "container to build). Deploy it live (drop --build-only), or use "
              "--mode runtime --build-only for the container artifact.")
        return 2

    role = os.environ.get(EXECUTION_ROLE_ENV)
    if not args.yes:
        if input("\nProceed with the (preview) harness deploy? [y/N] ").strip().lower() != "y":
            print("aborted.")
            return 1
    print(f"Deploying to Amazon Bedrock AgentCore harness  (region={region})")
    try:
        res = deploy_harness(project, region=region, execution_role_arn=role,
                             skip_unsupported=args.skip_unsupported, log=print)
    except HarnessExecutionRoleRequired as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 2
    except HarnessDeployFailed as e:
        print(f"\nerror: harness deploy failed: {e}", file=sys.stderr)
        return 3
    verb = {"create": "Deployed", "update": "Updated", "skip": "Up to date"}[res.action]
    print(f"\n{verb}. harness: {res.harness_arn or res.harness_id}  (status {res.status})")
    if res.env_var_names and res.action != "skip":
        print(f"  populated MCP auth header(s) from env: {', '.join(res.env_var_names)}")
    if not res.live_verified:
        print("  note: the harness wire shape is PREVIEW (provisional) -- this run helps "
              "verify it (see the banner above).")
    print("\nRun it:  agentlift run " + (project.agents[0].name if project.agents else "<agent>") +
          ' --project "' + args.path + '" --task "your task here"  (data-plane InvokeHarness)')
    return 0


def cmd_deploy(args) -> int:
    target = getattr(args, "target", "anthropic")
    if target == "google":
        return _cmd_deploy_google(args)
    if target == "bedrock":
        return _cmd_deploy_bedrock(args)
    load_env(os.getcwd(), os.path.abspath(args.path))
    project, diags = parse_project(args.path, default_model=args.model)
    plan = build_plan(project, diags, skip_unsupported=args.skip_unsupported)
    if not plan.deployable:
        print_plan(plan)
        print("\nNot deploying: fix the errors above (or pass --skip-unsupported to drop unsupported pieces).")
        return 1
    print_plan(plan)
    if not args.yes:
        resp = input("\nProceed with deploy? [y/N] ").strip().lower()
        if resp != "y":
            print("aborted.")
            return 1
    client = get_client()
    deployer = Deployer(client, project.root)
    result = deployer.apply(plan, prune=args.prune, log=print)
    print("\nDeployed.")
    print(f"  skills uploaded: {len(result.uploaded_skills)}  reused: {len(result.reused_skills)}")
    print(f"  agents created:  {len(result.created_agents)}  reused: {len(result.reused_agents)}")
    for name in [a.name for a in project.agents]:
        rec = deployer.lock.agent(name)
        if rec:
            print(f"    {name}: {rec['agent_id']} (v{rec['version']})")
    print("\nRun one:  agentlift run " + (project.agents[0].name if project.agents else "<agent>") +
          ' --project "' + args.path + '" --task "your task here"')
    return 0


def _resolve_deployed(path: str, agent_name: str):
    lock = Lockfile.load(os.path.abspath(path))
    rec = lock.agent(agent_name)
    if not rec:
        print(f"error: agent '{agent_name}' not found in lockfile at {path}. Deploy first.", file=sys.stderr)
        sys.exit(2)
    return rec


def cmd_run(args) -> int:
    load_env(os.getcwd(), os.path.abspath(args.project))
    client = get_client()
    project, _ = parse_project(args.project, default_model=args.model)
    agent = project.agent(args.agent)
    if args.local:
        if agent is None:
            print(f"error: agent '{args.agent}' not found in project.", file=sys.stderr)
            return 2
        res = run_local(client, agent, args.task, model=args.model_override)
    else:
        rec = _resolve_deployed(args.project, args.agent)
        model = args.model_override or (agent.model if agent else DEFAULT_MODEL)
        res = run_managed(client, rec["agent_id"], rec["version"], args.task, model=model)
    _print_run("local" if args.local else "managed", args.agent, res)
    return 0 if res.ok else 1


def _print_run(arm: str, name: str, res: RunResult) -> None:
    print(f"\n[{arm}] {name}")
    if not res.ok:
        print(f"  ERROR: {res.error}")
        return
    print("  " + "-" * 60)
    print("  " + res.output.replace("\n", "\n  "))
    print("  " + "-" * 60)
    print(f"  latency {res.latency_s}s | in {res.usage.input_tokens} out {res.usage.output_tokens} "
          f"| ~${res.cost:.5f} | tool_used={res.used_tool}")


def cmd_list(args) -> int:
    lock = Lockfile.load(os.path.abspath(args.path))
    if not lock.agents:
        print("No agents deployed (no lockfile entries).")
        return 0
    print(f"Deployed agents (from {lock.path}):")
    for name, rec in lock.agents.items():
        print(f"  {name}: {rec['agent_id']} (v{rec['version']})  skills={len(rec.get('skill_ids', []))}")
    print(f"\nUploaded skills: {len(lock.skills)}")
    for h, rec in lock.skills.items():
        print(f"  {rec['display_title']}: {rec['skill_id']} ({h[:8]})")
    return 0


def cmd_destroy(args) -> int:
    load_env(os.getcwd(), os.path.abspath(args.path))
    lock = Lockfile.load(os.path.abspath(args.path))
    if not lock.agents:
        print("Nothing to destroy.")
        return 0
    print(f"Will archive {len(lock.agents)} agent(s): {', '.join(lock.agents)}")
    if not args.yes:
        if input("Proceed? [y/N] ").strip().lower() != "y":
            print("aborted.")
            return 1
    client = get_client()
    deployer = Deployer(client, os.path.abspath(args.path))
    archived = deployer.destroy(log=print)
    print(f"Archived {len(archived)} agent(s).")
    return 0


def cmd_bench(args) -> int:
    load_env(os.getcwd(), os.path.abspath(args.project))
    client = get_client()
    project, _ = parse_project(args.project, default_model=args.model)
    agent = project.agent(args.agent)
    rec = _resolve_deployed(args.project, args.agent)
    model = args.model_override or (agent.model if agent else DEFAULT_MODEL)
    must_include = args.expect.split("|") if args.expect else None

    env_id = create_environment(client).id
    arms = ["managed"] + (["local"] if (args.local and agent) else [])
    rows = {}
    for arm in arms:
        results = []
        for i in range(args.n):
            if arm == "managed":
                r = run_managed(client, rec["agent_id"], rec["version"], args.task, model=model, environment_id=env_id)
            else:
                r = run_local(client, agent, args.task, model=model)
            if must_include:
                r_pass = substring_grader(r.output, must_include).passed
            elif args.rubric:
                r_pass = llm_grader(client, args.task, r.output, args.rubric).passed
            else:
                r_pass = r.ok
            results.append((r, r_pass))
            print(f"  [{arm} {i+1}/{args.n}] {r.latency_s}s ${r.cost:.5f} pass={r_pass}")
        rows[arm] = results

    print(f"\n# Benchmark: {args.agent}  (N={args.n})\n")
    print("| Arm | N | Pass% | Median latency | Avg cost |")
    print("|---|---|---|---|---|")
    for arm, results in rows.items():
        npass = sum(1 for _, p in results if p)
        lat = statistics.median([r.latency_s for r, _ in results]) if results else 0
        cost = statistics.mean([r.cost for r, _ in results]) if results else 0
        print(f"| {arm} | {len(results)} | {round(100*npass/len(results))}% | {lat}s | ${cost:.5f} |")
    return 0


# --------------------------------------------------------------------------- #
def cmd_audit(args) -> int:
    from .audit import render_audit, run_audit
    project, diags = parse_project(args.path, default_model=args.model)
    if not project.agents:
        print_diagnostics(diags)
        print("No agents found to audit.")
        return 1
    targets = [t.strip().lower() for t in args.targets.split(",") if t.strip()]
    print(render_audit(project, targets, run_audit(project, targets)))
    return 0


# --------------------------------------------------------------------------- #
def cmd_export(args) -> int:
    from .export import (export_anthropic_yaml, export_bedrock, export_google_adk,
                         export_openai_agents)
    project, diags = parse_project(args.path, default_model=args.model)
    if args.target == "anthropic-yaml":
        plan = build_plan(project, diags, skip_unsupported=args.skip_unsupported)
        files = export_anthropic_yaml(project, plan)
        if not plan.deployable:
            print_diagnostics(plan.diagnostics)
    elif args.target == "google-adk":
        files = export_google_adk(project)
    elif args.target == "bedrock-strands":
        files = export_bedrock(project, region=getattr(args, "bedrock_region", None))
    elif args.target == "openai-agents":
        files = export_openai_agents(project)
    else:
        print(f"unknown export target '{args.target}'", file=sys.stderr)
        return 2
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        for fn, text in files.items():
            dest = os.path.join(args.out, fn.replace("/", os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(text)
        print(f"Wrote {len(files)} file(s) to {args.out}:")
        for fn in files:
            print(f"  {fn}")
    else:
        for fn, text in files.items():
            print(f"# ===== {fn} =====")
            print(text)
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentlift", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"agentlift {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--model", default=DEFAULT_MODEL, help="default model for agents without one in frontmatter")
        sp.add_argument("--skip-unsupported", action="store_true", help="drop unsupported pieces (e.g. stdio MCP) instead of erroring")

    sp = sub.add_parser("validate", help="parse + plan, report problems")
    sp.add_argument("path"); add_common(sp); sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("audit", help="report how portable this folder is across managed-agent providers")
    sp.add_argument("path")
    sp.add_argument("--targets", default="anthropic,bedrock,google,openai",
                    help="comma-separated providers to check (anthropic, bedrock, google, openai)")
    add_common(sp); sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("export", help="compile the folder to a provider-native artifact (no deploy)")
    sp.add_argument("target", choices=["anthropic-yaml", "google-adk", "bedrock-strands", "openai-agents"])
    sp.add_argument("path")
    sp.add_argument("--out", default=None, help="write files to this directory instead of stdout")
    sp.add_argument("--bedrock-region", default="eu-north-1", help="AWS region for the bedrock-strands target (sets the regional Claude inference profile)")
    add_common(sp); sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("plan", help="show the deterministic deploy plan (no network)")
    sp.add_argument("path"); sp.add_argument("--json", action="store_true")
    sp.add_argument("--target", default="anthropic", choices=["anthropic", "google", "bedrock"], help="which target's plan to show")
    sp.add_argument("--mode", default="auto", choices=["auto", "harness", "runtime"], help="Bedrock primitive: harness (managed single agent) | runtime (custom container) | auto = least-powerful mode that preserves semantics")
    sp.add_argument("--google-model", default="gemini-2.5-flash", help="model for the Google target; Claude models in the folder map to this")
    sp.add_argument("--bedrock-region", default=None, help="AWS region for the bedrock target; defaults to us-west-2 (harness preview) or eu-north-1 (runtime). Claude maps to that region's inference profile (native, not remapped)")
    add_common(sp); sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("diff", help="what a deploy would change (vs the lockfile)")
    sp.add_argument("path"); sp.add_argument("--remote", action="store_true", help="also check the live account for deleted objects")
    add_common(sp); sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("deploy", help="deploy to a managed runtime (Anthropic + --target google live; --target bedrock --mode harness AND --mode runtime live, AgentCore preview)")
    sp.add_argument("path"); sp.add_argument("--prune", action="store_true", help="archive superseded agent versions")
    sp.add_argument("--target", default="anthropic", choices=["anthropic", "google", "bedrock"], help="managed runtime to deploy to")
    sp.add_argument("--mode", default="auto", choices=["auto", "harness", "runtime"], help="Bedrock primitive: harness (managed single agent, live) | runtime (custom container, live hosted multi-agent deploy; --build-only for just the artifact) | auto = least-powerful mode that preserves semantics")
    sp.add_argument("--google-model", default="gemini-2.5-flash", help="model for the Google target; Claude models in the folder are mapped to this")
    sp.add_argument("--bedrock-region", default=None, help="AWS region for the bedrock target; defaults to us-west-2 (harness preview) or eu-north-1 (runtime). Claude maps to that region's inference profile (native)")
    sp.add_argument("--build-only", action="store_true", help="build the deployable source package locally without deploying (google + bedrock --mode runtime; N/A to the config-only harness)")
    sp.add_argument("--yes", "-y", action="store_true", help="skip confirmation"); add_common(sp); sp.set_defaults(func=cmd_deploy)

    sp = sub.add_parser("run", help="invoke a deployed agent (or --local)")
    sp.add_argument("agent"); sp.add_argument("--project", default="."); sp.add_argument("--task", required=True)
    sp.add_argument("--local", action="store_true", help="run the same definition locally instead of in the cloud")
    sp.add_argument("--model", default=DEFAULT_MODEL); sp.add_argument("--model-override", default=None)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("list", help="show deployed agents (from the lockfile)")
    sp.add_argument("path"); sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("destroy", help="archive every agent in the lockfile")
    sp.add_argument("path"); sp.add_argument("--yes", "-y", action="store_true"); sp.set_defaults(func=cmd_destroy)

    sp = sub.add_parser("bench", help="managed vs local: latency / tokens / cost / pass")
    sp.add_argument("agent"); sp.add_argument("--project", default="."); sp.add_argument("--task", required=True)
    sp.add_argument("--n", type=int, default=5); sp.add_argument("--local", action="store_true")
    sp.add_argument("--expect", default=None, help="pipe-separated substrings that must appear (substring grader)")
    sp.add_argument("--rubric", default=None, help="rubric for the LLM grader (used if --expect absent)")
    sp.add_argument("--model", default=DEFAULT_MODEL); sp.add_argument("--model-override", default=None)
    sp.set_defaults(func=cmd_bench)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
