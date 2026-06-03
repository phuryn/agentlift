# AGENTS.md

**Read [CLAUDE.md](CLAUDE.md) first — it is the canonical guide for this repository.**

It covers the architecture (`parse → plan → apply → run`), the module map, the
`.managed-agents/` folder convention, per-provider status (Anthropic / Google /
OpenAI), the commands, and the dev workflow + ground rules.

## Quick orientation

agentlift compiles one neutral agent folder to multiple managed-agent runtimes.
The front half is **pure** (`parser.py`, `planner.py`, `capabilities.py`,
`export.py`); only the `*_target.py` and `runtime.py` modules touch the network.

```bash
python -m pip install -e ".[dev]"
pytest -m "not live"     # deterministic suite CI runs — start here
```

Non-negotiables (full version in [CLAUDE.md](CLAUDE.md)):

- Keep `parser.py` and `planner.py` pure — no network, clock, or randomness.
- Every translation rule gets an offline test asserting the plan. The plan is the contract.
- Surface untranslatable things as `Diagnostic`s; never drop silently.
- `capabilities.py` is the single source of truth for provider support tiers.
