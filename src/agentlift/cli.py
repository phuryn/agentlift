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
    from .google_target import deploy_google
    load_env(os.getcwd(), os.path.abspath(args.path))
    project, diags = parse_project(args.path, default_model=args.model)
    if not project.agents:
        print_diagnostics(diags)
        print("No agents to deploy.")
        return 1
    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    bucket = os.environ.get("AGENTLIFT_GCP_STAGING_BUCKET")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not gcp_project or not bucket:
        print("error: set GOOGLE_CLOUD_PROJECT and AGENTLIFT_GCP_STAGING_BUCKET (gs://...) in the "
              "env, plus ADC (gcloud auth application-default login). See docs/deploy-google.md.",
              file=sys.stderr)
        return 2
    print(f"Deploying {project.root} to Google Vertex AI Agent Engine")
    print(f"  project={gcp_project}  region={location}  staging={bucket}")
    resource = deploy_google(
        project, gcp_project=gcp_project, location=location,
        staging_bucket=bucket, model=args.google_model, log=print,
    )
    out = os.path.join(os.path.abspath(args.path), ".agentlift-google.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"reasoning_engine": resource, "project": gcp_project, "location": location}, fh, indent=2)
        fh.write("\n")
    print(f"\nDeployed. reasoningEngine: {resource}")
    print(f"  wrote {out}")
    return 0


def cmd_deploy(args) -> int:
    if getattr(args, "target", "anthropic") == "google":
        return _cmd_deploy_google(args)
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
    from .export import export_anthropic_yaml, export_google_adk, export_openai_agents
    project, diags = parse_project(args.path, default_model=args.model)
    plan = build_plan(project, diags, skip_unsupported=args.skip_unsupported)
    if args.target == "anthropic-yaml":
        files = export_anthropic_yaml(project, plan)
        if not plan.deployable:
            print_diagnostics(plan.diagnostics)
    elif args.target == "google-adk":
        files = export_google_adk(project)
    elif args.target == "openai-agents":
        files = export_openai_agents(project)
    else:
        print(f"unknown export target '{args.target}'", file=sys.stderr)
        return 2
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        for fn, text in files.items():
            with open(os.path.join(args.out, fn), "w", encoding="utf-8") as fh:
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
    sp.add_argument("--targets", default="anthropic,google,openai",
                    help="comma-separated providers to check (anthropic, google, openai)")
    add_common(sp); sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("export", help="compile the folder to a provider-native artifact (no deploy)")
    sp.add_argument("target", choices=["anthropic-yaml", "google-adk", "openai-agents"])
    sp.add_argument("path")
    sp.add_argument("--out", default=None, help="write files to this directory instead of stdout")
    add_common(sp); sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("plan", help="show the deterministic deploy plan (no network)")
    sp.add_argument("path"); sp.add_argument("--json", action="store_true"); add_common(sp); sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("diff", help="what a deploy would change (vs the lockfile)")
    sp.add_argument("path"); sp.add_argument("--remote", action="store_true", help="also check the live account for deleted objects")
    add_common(sp); sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("deploy", help="deploy to a managed runtime (Anthropic, or --target google)")
    sp.add_argument("path"); sp.add_argument("--prune", action="store_true", help="archive superseded agent versions")
    sp.add_argument("--target", default="anthropic", choices=["anthropic", "google"], help="managed runtime to deploy to")
    sp.add_argument("--google-model", default="gemini-2.5-flash", help="model for the Google target; Claude models in the folder are mapped to this")
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
