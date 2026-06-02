# Contributing

Thanks for looking. skylift is small and deliberately layered — the front half is
pure, the back half is the only place that touches the network.

## Layout

```
src/skylift/
  model.py            data classes (Project, AgentSpec, SkillSpec, McpServerSpec)
  parser.py           folder -> Project           (pure)
  planner.py          Project -> DeployPlan        (pure)
  lockfile.py         idempotency state
  anthropic_target.py DeployPlan -> API calls      (network)
  runtime.py          run a deployed agent / the same folder locally
  cost.py             token -> USD estimate
  graders.py          substring + LLM graders
  cli.py              argparse entry point
```

## Dev setup

```bash
python -m pip install -e ".[dev]"
pytest -m "not live"          # fast, no key
ANTHROPIC_API_KEY=... pytest -m live   # hits the real API, costs a few cents
```

## Ground rules

- **Keep `parser.py` and `planner.py` pure.** No network, no clock, no randomness.
  If a behavior can be tested offline, it belongs there and gets an offline test.
- **Every translation rule needs an offline test** asserting the plan
  (`tests/test_planner.py`). The plan is the contract.
- **New API behavior gets confirmed live first**, then encoded. Don't guess wire
  format from docs alone — the beta moves.
- **Surface, don't swallow.** Anything skylift can't translate becomes a `Diagnostic`
  (error or warning), visible in `skylift plan`.

## Adding a provider target

Implement a new target with the same `apply(plan)` contract as
`anthropic_target.Deployer`. The planner already emits provider-agnostic operations;
a target maps them to that provider's API. Keep the convention identical so one
folder deploys anywhere.
