# Tested platforms — receipts + where to find more

What "I ran it across the targets" actually means, with the configuration, the results, and
the console/docs links for each managed-agent platform. **Two targets are tested as a live
hosted deploy** (Anthropic, Google); **AWS Bedrock** is tested as the **live Strands
composition** (bearer-token model inference) with the hosted runtime build-only by design;
**OpenAI** is tested as the **agent-as-tool composition** (it has no code-define + host
path, so there is nothing to "deploy" — see the audit).

| Platform | What was tested | How | Result |
|---|---|---|---|
| **Anthropic** Managed Agents | live deploy + run + graded output; the 6-dimension coverage matrix | `agentlift deploy` → `agents.create`, run a session, LLM-grade | ✅ `tests/live/` + `benchmarks/` (managed vs local, 100% pass); coverage matrix **6/6 dimensions exercised** (native delegation event, both MCP servers, both skill markers) |
| **AWS** Bedrock AgentCore (Strands) | live coordinator→subagent→tool composition (bearer token); compiled container artifact | `python bedrock_strands_subagents.py` (live Bedrock inference); `agentlift deploy --target bedrock --build-only` | ✅ composition **exercised live** (Strands agents-as-tools + deterministic tool, Amazon Nova); container artifact built. Claude id verified answerable; same-Claude receipt pending Gate A. Hosted create build-only (Gate B/IAM) |
| **Google** Vertex AI Agent Engine | live deploy **+ query** of a coordinator + 2 subagents across **all 6 portability dimensions** | `agentlift deploy --target google` → ADK `sub_agents` / `McpToolset` / embedded skills → `agent_engines.create()`, then query the engine | ✅ live `reasoningEngine`; **6/6 dimensions exercised server-side** (`transfer_to_agent`, MCP tool calls, `load_skill`) |
| **OpenAI** Agents SDK | coordinator delegates to a subagent **as a tool** | `researcher.as_tool()`, run with `Runner.run` | ✅ trace `function_call ask_researcher` (in-process loop) |

The composition pattern is the same across all four; what differs is **where the
orchestration loop runs** — the provider's runtime (Anthropic, Google), the AgentCore
Runtime once hosted (AWS), or your app (OpenAI). See
[`experiments/subagent-composition/RESULTS.md`](../experiments/subagent-composition/RESULTS.md)
and [`experiments/bedrock-composition/RESULTS.md`](../experiments/bedrock-composition/RESULTS.md).

---

## Live coverage matrix — receipt evidence, not a capability ranking

One neutral fixture ([`tests/live/fixtures/coverage-matrix`](../tests/live/fixtures/coverage-matrix/))
— a coordinator `lead` over a `researcher` (shared **DeepWiki** MCP + private **GitMCP** + shared
`house-style` skill) and a `reporter` (shared `house-style` + private `report-format` skill) — was
deployed to **both** runtimes and the live engines were queried. Six portability dimensions,
classified by what the runtime *actually did* at run time:

> **Anthropic Managed Agents is the reference target** — the fullest, native coordinator / skill /
> MCP mapping. **Google is preview.** The table reports what each *billable run observed at runtime* —
> it is **receipt evidence, not a feature ranking.** Both deployed runtimes exercised all six
> portability dimensions server-side — for async Anthropic subagents the proof is the native
> delegation event, not a completed worker round-trip inside the coordinator's one-shot response.
>
> **Why this matrix is two-provider (no AWS column).** It records what ran inside a *hosted*
> deploy fixture. Bedrock's hosted runtime is **build-only** today (the create call is refused
> until live-verified — see [deploy-bedrock.md](deploy-bedrock.md)), so this fixture was never
> deployed there; retrofitting an AWS column with caveats would weaken the evidence. Bedrock's
> live proof is the **Strands composition** receipt, called out [in its own section
> below](#amazon-bedrock-agentcore-strands).

| Dimension | Anthropic (reference) | Google (preview) |
|---|---|---|
| agents | ✅ EXERCISED | ✅ EXERCISED |
| subagents | ✅ EXERCISED — native delegation event (`session.thread_created` + `agent.thread_message_sent`) | ✅ EXERCISED — `transfer_to_agent` → researcher, reporter |
| shared MCP | ✅ EXERCISED — `read_wiki_structure` (DeepWiki) | ✅ EXERCISED — `read_wiki_structure` (DeepWiki) |
| individual MCP | ✅ EXERCISED — `search`/`fetch_adk_python_documentation` (GitMCP) | ✅ EXERCISED — same |
| shared skill | ✅ EXERCISED — `HOUSESTYLEOK` emitted | ✅ EXERCISED — `list_skills`+`load_skill`, marker |
| individual skill | ✅ EXERCISED — `REPORTFMTOK` emitted | ✅ EXERCISED — marker |

**States:** `EXERCISED` = an objective runtime event proved it · `WIRED` = configured + deployed, no
event this run · `NOT-PROVEN` = wired but no signal. The **wired** layer (what the plan attaches on
each provider) is pinned offline in
[`tests/test_coverage_matrix_plan.py`](../tests/test_coverage_matrix_plan.py) and **runs in CI**; the
`EXERCISED` column comes from live receipts under
[`tests/live/receipts/`](../tests/live/receipts/) (Google `20260604-004318-google`, Anthropic
`20260604-012428-anthropic`). These live runs are **billable and not run in CI** (credentials are not
shared) — reproduce them with
[`tests/live/coverage_matrix.py`](../tests/live/coverage_matrix.py), or via the gated pytest wrapper
[`tests/live/test_coverage_matrix.py`](../tests/live/test_coverage_matrix.py)
(`AGENTLIFT_LIVE_COVERAGE=1 pytest -m live`); see [`tests/live/README.md`](../tests/live/README.md).

**How the two Anthropic cells reached EXERCISED (honest methodology):** an earlier one-shot run left
two cells soft, and the fixes are worth recording because they are *measurement* fixes, not capability
changes. (1) **subagents** — Anthropic's coordinator delegation is **async**: the lead spawns a worker
thread, dispatches the subtask, and returns ("*I've spawned the researcher … stand by*") **before** the
worker's reply lands, so no worker trace tag surfaces in a single-turn answer. We therefore key the
EXERCISED state on the **native delegation events** the runtime *does* emit synchronously —
`session.thread_created` + `agent.thread_message_sent` — which is the objective proof that the
coordinator delegated. (2) **shared MCP** — when the prompt left tool choice open, the model satisfied
it with the *other* (also-wired) GitMCP server; directing the query at the shared DeepWiki server by
name (`read_wiki_structure` on a real repo) exercises the wired server explicitly. Neither was a wiring
gap — the individual MCP server on the same agent and both skills fired regardless.

**A real fix this surfaced (now shipped):** Managed Agents rejects an agent that declares skills but
not the `read` builtin (*"skills require the read tool … to open their `SKILL.md` files"*). The
fixture set `tools: []`; agentlift's planner now **auto-enables `read`** for any skill-bearing agent
and emits a `skills.read_enabled` warning — a portability fix so the same folder deploys to both
runtimes. Google is unaffected (it loads skills via a SkillToolset, independent of builtins).

---

## Anthropic Managed Agents (reference target)

- **Config:** the `examples/quickstart` + `examples/team` folders — a coordinator (`lead`)
  over `bug-finder` + `researcher`, a shared skill, a remote MCP server, a `bash:ask` gate.
- **How:** `agentlift deploy ./examples/team --yes` → uploads skills, creates agents in
  dependency order (the `multiagent` coordinator server-side), writes `.agentlift-lock.json`.
- **Result:** validated by `tests/live/` (deploy → run a hosted session → an LLM grades the
  output) and `benchmarks/results.md` (same folder on managed vs local: 100% pass). The
  `RECEIPT:` skill fires **inside Anthropic's container**, proving the uploaded skill rode along.
  The 6-dimension coverage fixture was also deployed + queried here (receipt
  [`tests/live/receipts/20260604-012428-anthropic/`](../tests/live/receipts/)) — **all six dimensions
  exercised**: the native delegation events (`session.thread_created` + `agent.thread_message_sent`),
  both the shared DeepWiki and private GitMCP servers, and both skill markers fired live. See the
  coverage matrix above for the per-cell evidence and methodology.
- **Models:** `claude-haiku-4-5`. **Orchestration loop:** hosted (Anthropic runs delegation).

**More:** managed agents in your workspace → <https://platform.claude.com/workspaces/default/agents>
· docs → <https://platform.claude.com/docs/en/managed-agents/overview>

---

## Amazon Bedrock AgentCore (Strands)

- **Config:** the [`experiments/bedrock-composition`](../experiments/bedrock-composition/)
  script — a `coordinator` agent (Bedrock model) that delegates one factual question to a
  `researcher` specialist (the Strands **agents-as-tools** idiom = a sub-agent) and calls a
  deterministic `population_lookup` `@tool`. Run **locally** against Bedrock model inference,
  authenticated solely by `AWS_BEARER_TOKEN_BEDROCK` (no IAM, no hosted runtime).
- **How:** `python bedrock_strands_subagents.py` (live inference) for the composition proof;
  `agentlift deploy --target bedrock --build-only` for the deployable container artifact
  (Strands package + ARM64 Dockerfile + `NOTES.txt` runbook).
- **Models:** Claude is **native** on Bedrock — a folder's `claude-haiku-4-5` maps to its
  regional inference profile `eu.anthropic.claude-haiku-4-5-20251001-v1:0` (in `eu-north-1`),
  **no Gemini-style remap**. This is the headline portability story — *as a mapping fact*: the
  compiler emits the Bedrock Claude inference-profile ID directly, the same brain Anthropic
  runs, no substitution. The end-to-end *live* same-Claude composition receipt is still
  **pending stable Gate A** (the composition itself is live-proven on Nova — see the proof
  points below).
- **Orchestration loop:** **your process** today (local inference); the *same* composition
  runs as **one** AgentCore Runtime once hosted (so Bedrock subagents classify `emulated`,
  exactly like Google).

**Proof points (honest status, classified like the matrix above):**

| Bedrock proof point | Status |
|---|---|
| Strands package generation | ✅ offline-tested ([`tests/test_bedrock_*`](../tests/)) |
| AgentCore Runtime container artifact | ✅ build-only path shipped (`deploy --target bedrock --build-only`) |
| Agents-as-tools composition (coordinator → subagent + deterministic tool) | ✅ **EXERCISED live** — objective tool-call trace, on Amazon Nova Pro |
| Claude model mapping | ✅ native Bedrock Claude id supported; **no remap** |
| Claude composition receipt (same brain on AWS) | ⏳ **PENDING** — Claude id returned `BEDROCK_OK` in one window; the per-account Anthropic use-case-form entitlement (Gate A) is eventually consistent and had not durably propagated at capture time |
| Hosted AgentCore create / update | 🚧 **build-only by design** — refused until the control-plane wire shape is live-verified (Gate B/IAM) |

The composition receipt:

```
MODEL: eu.amazon.nova-pro-v1:0 region: eu-north-1
QUESTION: What year was the Eiffel Tower completed, and what is the population of Paris?
--- tool-call trace (objective: each is a real invocation) ---
  1. subagent researcher(question='What year was the Eiffel Tower completed?')
  2. deterministic-tool population_lookup(city='Paris')
  model-emitted toolUse blocks: ['researcher', 'population_lookup']
--- final coordinator answer ---
  The Eiffel Tower was completed in 1889, and the population of Paris is 2,102,650.
OK: coordinator delegated to a sub-agent AND used a deterministic tool.
```

The signal is objective on two independent channels — the python `@tool` bodies actually ran
(the `TRACE` list) **and** the model emitted `toolUse` blocks in its conversation history.
The composition ran on **Nova Pro** because Gate A flapped at capture time; the Claude id was
separately verified answerable (a clean `BEDROCK_OK` via `converse`). Full write-up + the two
gates: [`experiments/bedrock-composition/RESULTS.md`](../experiments/bedrock-composition/RESULTS.md)
and [`docs/deploy-bedrock.md`](deploy-bedrock.md).

**More:** AgentCore overview → <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html>
· HTTP contract → <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html>
· Strands Agents → <https://strandsagents.com/>

---

## Google Vertex AI Agent Engine

- **Config:** the `tests/live/fixtures/coverage-matrix` folder (the 6-dimension fixture above),
  compiled by `agentlift deploy --target google` to ADK `LlmAgent`s — a root coordinator (`lead`)
  over `researcher` + `reporter` with ADK `sub_agents`, each worker carrying its `McpToolset`s and
  embedded skill bundles, wrapped in an `AdkApp`, deployed via `agent_engines.create()`.
- **Auth + env:** ADC (`gcloud auth application-default login`), `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_CLOUD_LOCATION=us-central1`, a Cloud Storage staging bucket. See
  [`docs/deploy-google.md`](deploy-google.md).
- **Models:** `claude-haiku-4-5` in the folder is mapped to `gemini-2.5-flash` for Agent
  Engine (a Gemini project). **Preview scope:** the deploy maps **skills** (SKILL.md bundles
  embedded in the source package, loaded via ADK `load_skill_from_dir`), **URL MCP
  servers** (each an ADK `McpToolset` with a `tool_filter` allowlist; inline auth header
  values resolve from the local env into Agent Engine `env_vars`, never inlined into the
  source), and the **built-in web tools** (`web_search` → Gemini's Google Search grounding,
  `web_fetch` → URL Context, each lowered as a wrapped single-tool ADK sub-agent — see the
  web-tools receipt below). Still skipped: the built-in **sandbox** tools (`bash/files/glob-grep`
  — Vertex's sandbox is Python/JS only) and `:ask`/per-tool approval (not enforced on
  `VertexAiSessionService`); stdio MCP servers are refused. **The skills + MCP wiring is now
  confirmed live, not just by offline tests** — see the coverage matrix above and the receipt below.
- **Orchestration loop:** hosted (Vertex runs `transfer_to_agent` delegation server-side as
  one `reasoningEngine`).
- **Result:** live `reasoningEngine`
  `projects/********/locations/us-central1/reasoningEngines/********` (deployed
  2026-06-04 via `agentlift deploy --target google`, spec hash `e499b41a…`; project id redacted,
  engine since torn down). Querying the
  **deployed** engine exercised **all six dimensions server-side** — delegation, both MCP servers,
  and skill loading:

  ```
  QUERY: Look up the wiki structure of google/adk-python and how LlmAgent declares sub_agents.
    [delegation] lead -> transfer_to_agent({'agent_name': 'researcher'})
    [shared MCP] read_wiki_structure({'repoName': 'google/adk-python'})        # DeepWiki
    [private MCP] search_adk_python_documentation({'query': 'LlmAgent ... sub_agents'})  # GitMCP
    [skills]     list_skills() -> load_skill({'skill_name': 'house-style'})
    [reporter]   load_skill('report-format') ; emits REPORTFMTOK + REPORTER-AGENT-OK
  ```

  Every capability the folder declared fired **inside Google's runtime**, not in the client — the
  hosted loop. `create()` on Agent Engine *is* the deploy; the engine is live + billable. Full
  tool-call evidence: [`tests/live/receipts/20260604-004318-google/receipt.json`](../tests/live/receipts/).
  (An earlier prompt-only receipt — a separate engine, 2026-06-03 — tested just the
  coordinator→subagent shape before the skills/MCP mapping landed; this one supersedes it.)
- **Built-in web tools (separate fixture, exercised live).** The
  [`tests/live/fixtures/web-tools`](../tests/live/fixtures/web-tools/) folder — a `lead`
  coordinator over a `searcher` (carries `web_search`) and a `fetcher` (carries both) — was
  deployed to its own `reasoningEngine` and queried. Both web tool-agents fired server-side:

  ```
  QUERY (search): "...Agent Engine in Vertex AI... search the web, cite the URL. Do not answer from memory."
    [delegation]  lead -> transfer_to_agent({'agent_name': 'searcher'})
    [web_search]  searcher_web_search({'request': 'Agent Engine in Google Vertex AI definition'})  (+2 refined queries)
                  -> grounded, current product copy ("Gemini Enterprise Agent Platform", "ADK", ...)
  QUERY (fetch):  "Fetch https://httpbingo.org/base64/<nonce> and quote it verbatim. Use a URL-retrieval tool."
    [delegation]  lead -> transfer_to_agent({'agent_name': 'fetcher'})
    [web_fetch]   fetcher_web_fetch({'request': 'https://httpbingo.org/base64/...'})
                  -> "The content of the URL is \"AGENTLIFT-URLCTX-9F3A2C7E-CANARY\"."   # nonce returned verbatim
  ```

  The fetch proof is airtight: the response contains a **unique nonce** served by the URL, which a
  model cannot reproduce from memory — so URL Context demonstrably retrieved it. One honest caveat:
  the inner wrapped-agent's structured `grounding_metadata` / `url_context_metadata` does **not**
  cross the `AgentTool` → Agent-Engine `stream_query` boundary (even with
  `propagate_grounding_metadata=True`), so the objective signal is the wrapped-agent `function_call`
  + its `function_response` content, not citation chunks on the outer stream. Receipt:
  [`tests/live/receipts/20260604-115352-web-google/receipt.json`](../tests/live/receipts/);
  reproduce with [`tests/live/web_tools.py`](../tests/live/web_tools.py). Pinned offline in
  [`tests/test_google_plan.py`](../tests/test_google_plan.py) /
  [`tests/test_google_codegen.py`](../tests/test_google_codegen.py).

**More:** Agent Platform console (visual) → <https://console.cloud.google.com/agent-platform>
· Agent Studio overview → <https://docs.cloud.google.com/gemini-enterprise-agent-platform/agent-studio>
· gcloud SDK → <https://docs.cloud.google.com/sdk/gcloud>

---

## OpenAI (Agents SDK)

- **Config:** a coordinator + a `researcher` sub-agent exposed to it as a tool via
  `researcher.as_tool(tool_name="ask_researcher", ...)`, run with `Runner.run`. Model
  `gpt-5-mini`. Script: [`experiments/subagent-composition/openai_agent_as_tool.py`](../experiments/subagent-composition/openai_agent_as_tool.py).
- **Result:** the coordinator called the sub-agent **as a tool** (trace: `function_call
  ask_researcher` → `ToolCallOutputItem`) and synthesized the answer. This is exactly what
  `agentlift export openai-agents` emits from a folder.
- **Orchestration loop:** **your app** (in-process). OpenAI hosts only an Agent Builder
  visual graph; there is no code-define + OpenAI-host path, so OpenAI is an `export` target,
  never a `deploy` target.

**More:** Agent Builder → <https://platform.openai.com/agent-builder/>
· Agents SDK docs → <https://developers.openai.com/api/docs/guides/agents>

---

*All four were exercised with the live SDKs (not mocked). The subagent-composition traces
are reproducible from [`experiments/subagent-composition/`](../experiments/subagent-composition/)
(OpenAI/Google) and [`experiments/bedrock-composition/`](../experiments/bedrock-composition/)
(AWS); the Google live deploy from [`docs/deploy-google.md`](deploy-google.md); the Bedrock
build artifact + gates from [`docs/deploy-bedrock.md`](deploy-bedrock.md).*
