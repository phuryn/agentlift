# Live verification — the coverage matrix

These tests **deploy a real agent to a real managed runtime, query it, and record what the
runtime actually did.** They cost money and need credentials, so **they do not run in CI**
(we don't share keys). What runs in CI is the *offline* proof that the plan wires every
dimension — [`tests/test_coverage_matrix_plan.py`](../test_coverage_matrix_plan.py). This
folder is the *live* proof, kept here as receipts so the claim is auditable, not asserted.

## What's covered

One neutral fixture — [`fixtures/coverage-matrix`](fixtures/coverage-matrix/) — exercises
**six portability dimensions in a single folder**, so one deploy proves the whole capability
model on each provider:

| Dimension | In the fixture |
|---|---|
| agents | three agents: `lead`, `researcher`, `reporter` |
| subagents | `lead` is a coordinator over `researcher` + `reporter` |
| shared MCP | `researcher` uses `shared/docs` → **DeepWiki** (`https://mcp.deepwiki.com/mcp`) |
| individual MCP | `researcher` uses its private `code-search` → **GitMCP** (`https://gitmcp.io/google/adk-python`) |
| shared skill | both workers load `shared/house-style` (emits `HOUSESTYLEOK`) |
| individual skill | `reporter` loads its private `report-format` (emits `REPORTFMTOK`) |

Both MCP servers are **real, public, no-auth** endpoints, so the run is reproducible by
anyone with provider credentials.

## The 4-state model (why a green check isn't always the whole story)

Every dimension is reported as one of four states — **we only claim "verified" off an
objective runtime event, never off answer text:**

- **EXERCISED** — the runtime stream shows the provider *actually used* it (a
  `transfer_to_agent` / `session.thread_created` + `agent.thread_message_sent` / `load_skill` /
  MCP `tool_use` event).
- **WIRED** — configured + deployed correctly, but no runtime event observed this run.
- **NOT-PROVEN** — wired, but no objective signal (e.g. async delegation didn't surface in a
  one-shot query, or the model chose a different tool).
- **FAIL** — deploy/config/runtime error.

## Latest results

| Dimension | Anthropic (reference) | Google (preview) |
|---|---|---|
| agents | ✅ EXERCISED | ✅ EXERCISED |
| subagents | ✅ EXERCISED (native delegation event) | ✅ EXERCISED (`transfer_to_agent`) |
| shared MCP | ✅ EXERCISED (`read_wiki_structure`) | ✅ EXERCISED |
| individual MCP | ✅ EXERCISED | ✅ EXERCISED |
| shared skill | ✅ EXERCISED | ✅ EXERCISED |
| individual skill | ✅ EXERCISED | ✅ EXERCISED |

**Both deployed runtimes exercised all six portability dimensions server-side (6/6).** For async
Anthropic subagents the proof is the native delegation event (`session.thread_created` +
`agent.thread_message_sent`), not a completed worker round-trip — the coordinator's delegation is
**async**, so the worker's reply lands after a one-shot query returns. The full per-cell evidence and
methodology are in
[`docs/tested-platforms.md`](../../docs/tested-platforms.md#live-coverage-matrix--receipt-evidence-not-a-capability-ranking).

Receipts (committed as evidence; each holds the prompts, the observed tool calls, and the
4-state classification):

- `receipts/20260604-004318-google/receipt.json` — Google, 6/6 EXERCISED
- `receipts/20260604-012428-anthropic/receipt.json` — Anthropic, 6/6 EXERCISED

## Reproduce it

```bash
# Anthropic — needs ANTHROPIC_API_KEY (Managed Agents beta)
python tests/live/coverage_matrix.py preflight
python tests/live/coverage_matrix.py deploy-anthropic     # billable
python tests/live/coverage_matrix.py query-anthropic      # writes a receipt
python tests/live/coverage_matrix.py teardown-anthropic

# Google — needs ADC + GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION + AGENTLIFT_GCP_STAGING_BUCKET
python tests/live/coverage_matrix.py deploy-google         # ~minutes, billable
python tests/live/coverage_matrix.py query-google          # writes a receipt
python tests/live/coverage_matrix.py teardown-google
```

There is also a thin pytest wrapper, [`test_coverage_matrix.py`](test_coverage_matrix.py),
gated behind both credentials **and** an explicit `AGENTLIFT_LIVE_COVERAGE=1` opt-in, so it
skips by default (including in CI) and only runs when you mean it.

> **Always tear down.** A deployed Anthropic agent and a Google `reasoningEngine` are live,
> billable resources. The `teardown-*` steps archive/delete them; run them when you're done.
