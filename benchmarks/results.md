# agentlift benchmark — quickstart `knowledge-agent`

Run 2026-06-02. Model `claude-haiku-4-5`. N=5 per arm. Anthropic Managed Agents (beta).
Same agent folder, two runtimes. Pass = the uploaded skill fired (a `RECEIPT:` line) AND the answer is on-topic. Cost is a token estimate at tier rates (managed auto-caches its context; local context is lean).

## Deploy (cold)
- skill `receipt-stamp` -> `skill_01PhFUEPBdu4ZdEfdqnpriNT`
- agent `knowledge-agent` -> `agent_019LJNFWgFqHNV18SxWGUDnx` v1
- total deploy time: 0.76s

## Run

| Arm | N | Pass% | Median latency | Avg in tok | Avg out tok | Avg cost |
|---|---|---|---|---|---|---|
| managed (cloud) | 5 | 100% | 5.92s | 4121 | 220 | $0.00522 |
| local (your machine) | 5 | 100% | 2.3s | 2617 | 148 | $0.00336 |

## Sample output (skill applied on both runtimes)

Managed (cloud):
```
A North Star metric is the single measure that best captures the value users get from your product.

RECEIPT: metric captured

Best, Knowledge Agent
```

Local (same folder):
```
A North Star metric is a single, primary measurement that defines success and guides all organizational decisions and strategies.

Best, Knowledge Agent

RECEIPT: metric guiding success
```

_Reproduce: `python benchmarks/run_benchmark.py --n 5`_
