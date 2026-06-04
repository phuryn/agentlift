# Experiment: Claude-on-Vertex as a Google deploy model (offline-verified spike)

The Google target maps Claude folder models to a Gemini default (`gemini-2.5-flash`)
because Agent Engine's first-party model is Gemini. The open question this spike answers:
**could agentlift instead keep a Claude brain when deploying to Google — Claude served on
Vertex AI — so the *same* model runs on both targets?**

Short answer: the *design* is mechanically sound (proven offline below), but agentlift
does **not** ship a Claude-on-Vertex deploy path yet — there is no live receipt, and the
repo's rule is *confirm live before encoding*. This folder is the evidence + the live
probe that would graduate it.

## What's proven offline (no ADC, no network, no project)

`python claude_on_vertex_construct.py` — confirmed 2026-06-04 with **google-adk 1.34.3**
(already our pinned floor, `google-adk>=1.34.3` — **no new dependency**):

```
registry:
  'claude-sonnet-4-6' -> google.adk.models.anthropic_llm.Claude
  Claude.supported_models() = ['claude-3-.*', 'claude-.*-4.*']

constructed (offline, no ADC):
  parent  : lead  model=claude-sonnet-4-6  -> Claude
  web sub : lead_web_search  model=gemini-2.5-flash  -> Gemini

OK: Claude main agent + Gemini-pinned web sub-agent compose. Mixed-model invariant holds.
```

Two facts established:

1. **ADK natively resolves Claude on Vertex.** `LLMRegistry.resolve("claude-sonnet-4-6")`
   returns `google.adk.models.anthropic_llm.Claude`, backed by `AsyncAnthropicVertex` —
   Claude served through Vertex AI, no extra package. An `LlmAgent(model="claude-…")` is a
   valid ADK agent.
2. **The mixed-model invariant holds.** A Claude parent agent can carry a Gemini-backed
   web tool-agent via `AgentTool`. This matters because `web_search`/`web_fetch` lower to
   `GoogleSearchTool`/`url_context`, which are **Gemini built-ins** — they cannot run on a
   Claude model. So a Claude parent must pin its wrapped web sub-agents to Gemini.

### The Vertex Claude id resolves bare (a `@version` suffix is optional)

ADK resolves the **bare** Vertex Claude id (`claude-sonnet-4-6`) — confirmed above against
`Claude.supported_models() = ['claude-3-.*', 'claude-.*-4.*']` — and an `@versioned` form
(`claude-sonnet-4-5@20250929`) resolves too. So a future passthrough maps folder ids →
Vertex Claude ids subject to **per-region/Model-Garden availability**, not a mandatory
version-pinning step.

## What this does NOT prove (the live half — blocked)

`claude_on_vertex_deploy.py` is the live probe that would close the loop: it deploys ONE
`reasoningEngine` with a Claude-on-Vertex root + Gemini web sub-agent, queries it (the
instruction prepends a literal `CLAUDEVTX` token so the reply confirms which brain
answered), and tears it down. It is **env-driven and committed without identifiers**, and
has **not been run yet**. Preconditions:

- **Claude enabled in the project's Vertex AI Model Garden** — a one-time console action;
  Claude on Vertex is an enable-per-project, region-gated partner model. *Now satisfied:*
  `claude-sonnet-4-6` was enabled in this project (2026-06-04), so this is no longer the
  blocker — only running the probe is.
- **The model-call region (the one live unknown).** An Agent Engine *resource* deploys to a
  real region; at runtime the in-engine ADK Claude client calls `AsyncAnthropicVertex` with
  `GOOGLE_CLOUD_LOCATION`. If the model is served only at the **global** endpoint (the
  Vertex quickstart uses `region="global"`), the probe injects `GOOGLE_CLOUD_LOCATION=global`
  as an engine env var (`CLAUDE_VERTEX_REGION=global`) while the engine stays in a real
  region. Whether one knob or the override is needed is exactly what the live run settles.
- **A billable project + staging bucket + ADC**, exactly like a normal Google deploy.

Until the probe runs green, "Agent Engine will deploy *and run* a Claude-on-Vertex engine
end-to-end" is **NOT-PROVEN** — distinct from the offline-verified construction.

## What shipped in agentlift as a result of this spike

The decision (converged with Codex): ship the seam and the guardrail, **not** a
user-facing passthrough flag. Concretely:

1. **The mixed-model seam is encoded in codegen** ([google_codegen.py](../../src/agentlift/google_codegen.py)).
   Wrapped web sub-agents now resolve their model through a dedicated `web_model()` helper
   instead of inheriting the parent's `vertex_model()`. `web_model()` pins any non-Gemini
   folder model to the Gemini default — so the moment a Claude parent is allowed, its web
   sub-agents stay Gemini *by construction*. It is a behavioral no-op today (parents
   already map to Gemini) but makes the invariant robust to a future passthrough. Pinned by
   `test_web_sub_agents_pin_a_gemini_model` in
   [tests/test_google_codegen.py](../../tests/test_google_codegen.py).
2. **A planner guard refuses a Claude `--google-model`** ([google_plan.py](../../src/agentlift/google_plan.py)).
   Selecting `--google-model claude-…` would silently encode this unsupported path, so the
   planner emits `google.deploy_model.claude_unsupported` and blocks the deploy, pointing
   here. Pinned by `test_claude_deploy_model_is_rejected` in
   [tests/test_google_plan.py](../../tests/test_google_plan.py).

## To graduate this to a shipped feature

1. Run `claude_on_vertex_deploy.py` against a project with Claude enabled in Model Garden;
   capture the `CLAUDEVTX`-prefixed reply as a receipt (the unforgeable signal that the
   Claude brain — not the Gemini default — answered).
2. Encode the wire behavior: the folder-id → Vertex-Claude-id map (per Model-Garden/region
   availability; bare id ok), and whatever `requirements`/region constraints the live
   deploy revealed (notably whether the model call needs the `global` endpoint).
3. Replace the planner guard with a real passthrough (e.g. `--google-model claude-…` or a
   per-agent opt-in), keeping `web_model()` pinning the web sub-agents to Gemini.

*Offline half confirmed 2026-06-04 with google-adk 1.34.3. Live half: NOT-PROVEN (Model
Garden now enabled for `claude-sonnet-4-6`; deploy probe not yet run).*
