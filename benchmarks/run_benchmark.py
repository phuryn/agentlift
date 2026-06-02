"""Deploy the quickstart agent, run it managed (cloud) and local (same folder),
and write benchmarks/results.md. This is the repo's reproducible "test results"
artifact and the portability proof: one definition, two runtimes.

    ANTHROPIC_API_KEY=... python benchmarks/run_benchmark.py --n 5

Costs a few cents. Archives the agent and removes the temp copy when done.
"""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import statistics
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from skylift.anthropic_target import Deployer            # noqa: E402
from skylift.graders import substring_grader             # noqa: E402
from skylift.parser import parse_project                 # noqa: E402
from skylift.planner import build_plan                   # noqa: E402
from skylift.runtime import create_environment, run_local, run_managed  # noqa: E402

MODEL = "claude-haiku-4-5"
TASK = "What is a North Star metric? Answer in one sentence."
PASS_SUBSTRINGS = ["RECEIPT:", "metric"]


def _load_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    for p in (os.path.join(ROOT, ".env"), r"C:\GitHub\managed-agents-experiment\.env"):
        if os.path.isfile(p):
            for line in open(p, encoding="utf-8").read().splitlines():
                if line.strip().startswith("ANTHROPIC_API_KEY"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("ANTHROPIC_API_KEY not set")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    import anthropic
    client = anthropic.Anthropic(api_key=_load_key())

    workdir = tempfile.mkdtemp(prefix="skylift-bench-")
    proj_dir = os.path.join(workdir, "quickstart")
    shutil.copytree(os.path.join(ROOT, "examples", "quickstart"), proj_dir)

    project, diags = parse_project(proj_dir)
    plan = build_plan(project, diags)
    assert plan.deployable, diags.render()

    deployer = Deployer(client, project.root)
    t0 = time.time()
    result = deployer.apply(plan, log=lambda m: print(m))
    deploy_s = round(time.time() - t0, 2)
    rec = deployer.lock.agent("knowledge-agent")
    agent = project.agent("knowledge-agent")

    env_id = create_environment(client).id
    rows = {"managed": [], "local": []}
    samples = {}
    for arm in ("managed", "local"):
        for i in range(args.n):
            if arm == "managed":
                r = run_managed(client, rec["agent_id"], rec["version"], TASK, model=MODEL, environment_id=env_id)
            else:
                r = run_local(client, agent, TASK, model=MODEL)
            passed = r.ok and substring_grader(r.output, PASS_SUBSTRINGS).passed
            rows[arm].append((r, passed))
            samples.setdefault(arm, r.output)
            print(f"  [{arm} {i+1}/{args.n}] {r.latency_s}s ${r.cost:.5f} pass={passed}")

    # cleanup
    deployer.destroy(log=lambda m: print(m))
    shutil.rmtree(workdir, ignore_errors=True)

    # ---- write report ----
    today = datetime.date.today().isoformat()
    lines = [
        "# skylift benchmark — quickstart `knowledge-agent`",
        "",
        f"Run {today}. Model `{MODEL}`. N={args.n} per arm. Anthropic Managed Agents (beta).",
        "Same agent folder, two runtimes. Pass = the uploaded skill fired (a `RECEIPT:` line) "
        "AND the answer is on-topic. Cost is a token estimate at tier rates (managed auto-caches "
        "its context; local context is lean).",
        "",
        "## Deploy (cold)",
        f"- skill `receipt-stamp` -> `{result.skill_ids.get('@skill:' + plan.skill_uploads[0].content_hash[:8], '?')}`",
        f"- agent `knowledge-agent` -> `{rec['agent_id']}` v{rec['version']}",
        f"- total deploy time: {deploy_s}s",
        "",
        "## Run",
        "",
        "| Arm | N | Pass% | Median latency | Avg in tok | Avg out tok | Avg cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in ("managed", "local"):
        rs = rows[arm]
        npass = sum(1 for _, p in rs if p)
        lat = statistics.median([r.latency_s for r, _ in rs])
        intok = statistics.mean([r.usage.input_tokens for r, _ in rs])
        outtok = statistics.mean([r.usage.output_tokens for r, _ in rs])
        cost = statistics.mean([r.cost for r, _ in rs])
        label = "managed (cloud)" if arm == "managed" else "local (your machine)"
        lines.append(f"| {label} | {len(rs)} | {round(100*npass/len(rs))}% | {lat}s | {round(intok)} | {round(outtok)} | ${cost:.5f} |")

    lines += [
        "",
        "## Sample output (skill applied on both runtimes)",
        "",
        "Managed (cloud):",
        "```",
        samples.get("managed", "").strip(),
        "```",
        "",
        "Local (same folder):",
        "```",
        samples.get("local", "").strip(),
        "```",
        "",
        "_Reproduce: `python benchmarks/run_benchmark.py --n " + str(args.n) + "`_",
    ]
    out = os.path.join(ROOT, "benchmarks", "results.md")
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\nwrote", out)


if __name__ == "__main__":
    main()
