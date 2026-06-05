# Tested platforms — receipts + where to find more

What "I ran it across the targets" actually means, with the configuration, the results, and
the console/docs links for each managed-agent platform. **Two targets are tested as a live
hosted deploy** (Anthropic, Google); **AWS Bedrock** is now tested two ways, **both live** —
the managed **Harness** is a **live single-agent deploy, verified by a committed Nova receipt**
(create + agent + base-session sandbox + `agentcore_browser` EXERCISED), and the custom-container
**Runtime** is a **live hosted multi-agent deploy, verified by two committed Nova receipts**
(container → ECR → `CreateAgentRuntime` → READY → `InvokeAgentRuntime`, with objective
coordinator→subagents delegation EXERCISED); **OpenAI** is tested as the **agent-as-tool
composition** (it has no code-define + host path, so there is nothing to "deploy" — see the audit).

| Platform | What was tested | How | Result |
|---|---|---|---|
| **Anthropic** Managed Agents | live deploy + run + graded output; the 6-dimension coverage matrix | `agentlift deploy` → `agents.create`, run a session, LLM-grade | ✅ `tests/live/` + `benchmarks/` (managed vs local, 100% pass); coverage matrix **6/6 dimensions exercised** (native delegation event, both MCP servers, both skill markers) |
| **AWS** Bedrock AgentCore (Harness + Runtime) | managed Harness single-agent **live deploy + invoke, 6/6** (committed receipt); custom-container Runtime **live hosted multi-agent deploy + invoke** (two committed receipts) | `agentlift deploy --target bedrock --mode harness` then InvokeHarness; `agentlift deploy --target bedrock --mode runtime` (container → ECR → `CreateAgentRuntime` → `InvokeAgentRuntime`) | ✅ **Harness 6/6 EXERCISED live** (receipt `20260605-121525`, Nova: create + agent + base-session sandbox `shell` + remote MCP `docs_read_wiki_structure` + S3-loaded skill + `agentcore_browser`; Claude-invoke Gate-A-gated; AWS Harness feature in preview). ✅ **Runtime hosted multi-agent EXERCISED live** (receipts `20260605-134012` team + `20260605-133821` smoke, Nova us-east-1: coordinator→**both subagents** delegation + root-level remote MCP exercised server-side). Both primitives map Claude **native** (no remap); same-Claude-brain receipt pending Gate A |
| **Google** Vertex AI Agent Engine | live deploy **+ query** of a coordinator + 2 subagents across **all 6 portability dimensions** | `agentlift deploy --target google` → ADK `sub_agents` / `McpToolset` / embedded skills → `agent_engines.create()`, then query the engine | ✅ live `reasoningEngine`; **6/6 dimensions exercised server-side** (`transfer_to_agent`, MCP tool calls, `load_skill`) |
| **OpenAI** Agents SDK | coordinator delegates to a subagent **as a tool** | `researcher.as_tool()`, run with `Runner.run` | ✅ trace `function_call ask_researcher` (in-process loop) |

The composition pattern is the same across all four; what differs is **where the
orchestration loop runs** — the provider's runtime (Anthropic, Google), the hosted AgentCore
Runtime (AWS), or your app (OpenAI). See
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
> deploy of **this exact fixture** — a coordinator over two subagents with shared + private skills.
> Bedrock now **hosts a multi-agent team live** (the custom-container **Runtime** is no longer
> build-only — `CreateAgentRuntime` → `InvokeAgentRuntime` is receipt-verified, see
> [deploy-bedrock.md](deploy-bedrock.md)), and the managed **Harness** deploys a live **single
> agent**. But the AWS receipts run their **own** team fixture, not *this* one, and the
> `/invocations` boundary returns the container's JSON body rather than an event stream, so only
> the **coordinator's top-level** delegation/tool calls cross as objective events (PASS-EXERCISED);
> a subagent's *nested* skill/MCP calls stay text-corroborated (PASS-WIRED). Mapping that onto this
> fixture's six per-cell `EXERCISED` claims one-for-one would overstate the AWS evidence, so AWS
> keeps its own section. Bedrock's live proofs are the **Runtime** receipts (team + smoke) and the
> **Harness** receipt, called out [in their own section below](#amazon-bedrock-agentcore-runtime--harness).

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

## Amazon Bedrock AgentCore (Runtime + Harness)

Bedrock has **two deploy primitives** behind `--mode`, **both now live-verified by committed Nova
receipts**: the managed **Harness** (config-only single agent) **deploys + invokes live**, and the
custom-container **Runtime** (multi-agent) **builds → pushes to ECR → `CreateAgentRuntime` → polls
READY → `InvokeAgentRuntime`**, with the coordinator's delegation to **both** subagents exercised
server-side. The earlier local **Strands composition** proof (the Runtime's brain, run against
Bedrock inference) still stands as a third corroborating data point.

- **Config:** the [`experiments/bedrock-composition`](../experiments/bedrock-composition/)
  script — a `coordinator` agent (Bedrock model) that delegates one factual question to a
  `researcher` specialist (the Strands **agents-as-tools** idiom = a sub-agent) and calls a
  deterministic `population_lookup` `@tool`. Run **locally** against Bedrock model inference,
  authenticated solely by `AWS_BEARER_TOKEN_BEDROCK` (no IAM, no hosted runtime).
- **How:** `agentlift deploy --target bedrock --mode runtime` for the hosted multi-agent live
  deploy — agentlift builds the ARM64 container (Strands package + ARM64 Dockerfile + `NOTES.txt`
  runbook), creates/logs-in to the ECR repo and pushes it (`docker buildx --platform linux/arm64
  --push`), then `CreateAgentRuntime` (networkMode=`PUBLIC`, serverProtocol=`HTTP`, IAM-only — no
  JWT authorizer), polls READY, writes `.agentlift-bedrock.json`, and `InvokeAgentRuntime`s it
  (`--build-only` still stops at the container artifact). `agentlift deploy --target bedrock --mode
  harness` for the managed single-agent live deploy (IAM + an execution role, no container — wire
  shape verified by the committed Nova receipt). `python bedrock_strands_subagents.py` (live
  inference) for the original local composition proof.
- **Models:** Claude is **native** on Bedrock — a folder's `claude-haiku-4-5` maps to its
  regional inference profile `eu.anthropic.claude-haiku-4-5-20251001-v1:0` (in `eu-north-1`),
  **no Gemini-style remap**. This is the headline portability story — *as a mapping fact*: the
  compiler emits the Bedrock Claude inference-profile ID directly, the same brain Anthropic
  runs, no substitution. The end-to-end *live* same-Claude composition receipt is still
  **pending stable Gate A** (the composition itself is live-proven on Nova — see the proof
  points below).
- **Orchestration loop:** **hosted** for the Runtime — the whole multi-agent composition runs as
  **one** AgentCore Runtime container server-side (so Bedrock subagents classify `emulated`,
  exactly like Google), proven by the receipts below. The Harness runs a **single** agent
  server-side — no in-runtime delegation — so it is the path for single-agent folders, not a team.

**Proof points (honest status, classified like the matrix above):**

| Bedrock proof point | Status |
|---|---|
| Strands package generation (Runtime) | ✅ offline-tested ([`tests/test_bedrock_*`](../tests/)) |
| Harness plan + codegen + lock (config-only single agent) | ✅ offline-tested ([`tests/test_harness_*`](../tests/), [`tests/test_cli_harness.py`](../tests/test_cli_harness.py)) |
| AgentCore Runtime container artifact | ✅ build path shipped (`--mode runtime --build-only` stops here; a full `--mode runtime` deploys it) |
| Agents-as-tools composition (coordinator → subagent + deterministic tool) | ✅ **EXERCISED live** — objective tool-call trace, on Amazon Nova Pro |
| Claude model mapping (both primitives) | ✅ native Bedrock Claude id supported; **no remap** (non-Claude like `us.amazon.nova-pro-v1:0` passes through verbatim) |
| Claude composition receipt (same brain on AWS) | ⏳ **Gate-A-gated** — Claude *does* run live (it answered `INVOKE-OK` in the Harness), but the per-account Anthropic use-case entitlement (Gate A) is eventually-consistent and flapped back to `ResourceNotFoundException` minutes later; the wire-shape receipts are on Nova (model-agnostic). A same-Claude-brain receipt is pending that entitlement — **not a code gap** |
| Hosted AgentCore **Runtime** create / invoke (agentlift pipeline) | ✅ **EXERCISED live** — `CreateAgentRuntime` → READY → `InvokeAgentRuntime`, multi-agent team, two committed Nova receipts ([see below](#runtime-receipts-team--smoke)) |
| Managed **Harness** live single-agent deploy (agentlift pipeline) | ✅ **6/6 EXERCISED live** (receipt [`20260605-121525-harness-bedrock`](../tests/live/receipts/)) — `CreateHarness` → READY, then `InvokeHarness`: agent (Nova) + base-session sandbox (`shell`) + remote MCP (`docs_read_wiki_structure`, surfaced as `<server>_<tool>`) + S3-loaded skill (`skills[].s3.uri`) + `agentcore_browser`, all server-side. (AWS Harness feature in preview; per-tool MCP `allowedTools` narrowing not enforced in preview.) |

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

### Runtime receipts (team + smoke)

The hosted **Runtime** is now live-verified by **two committed receipts** (Amazon Nova Pro,
region `us-east-1`), classified with the same four states the coverage matrix uses
(`PASS-EXERCISED` = an objective runtime event proved it · `PASS-WIRED` = configured + deployed,
no event crossed the boundary this run · `NOT-PROVEN` = wired but no signal · `FAIL`). The
pipeline for both: agentlift builds the ARM64 container → pushes to ECR → `CreateAgentRuntime`
(networkMode=`PUBLIC`, serverProtocol=`HTTP`, IAM-only, no JWT authorizer) → polls READY → writes
`.agentlift-bedrock.json` → `InvokeAgentRuntime`.

**One honest boundary caveat (the runtime analogue of the Google `AgentTool` → `stream_query`
metadata caveat above):** `InvokeAgentRuntime` returns the container's **app-defined JSON body**,
not an event stream. agentlift's handler returns `{result, tool_calls?}`, where `tool_calls` is the
**coordinator's top-level** trace (`AgentResult.metrics.tool_metrics`, fail-open). So
coordinator/root tool calls cross as objective events (`PASS-EXERCISED`); a **nested specialist's**
skill/MCP calls do **not** cross the boundary, so they stay `PASS-WIRED` + text-corroborated.

**Receipt [`20260605-134012-runtime-bedrock`](../tests/live/receipts/) — TEAM (the headline):** a
coordinator over two specialists (a `researcher` + a `bug-finder`).

| Dimension | Status |
|---|---|
| create (`CreateAgentRuntime` → READY) | ✅ PASS-EXERCISED |
| agent (root invoke returned a fused answer) | ✅ PASS-EXERCISED |
| **subagents** | ✅ PASS-EXERCISED — the coordinator's top-level trace `tool_calls` was `['bug_finder', 'researcher']`: objective delegation to **both** specialists. The final answer fused react wiki sections from the researcher **and** the bug fix from the bug-finder |
| skills | 🟡 PASS-WIRED — embedded in the source package; nested in a specialist, so no event crossed the `/invocations` boundary |
| remote MCP | 🟡 PASS-WIRED — nested in a specialist; text-corroborated by the real react content in the fused answer |

Nothing `FAIL`ed.

**Receipt [`20260605-133821-runtime-bedrock`](../tests/live/receipts/) — SMOKE (single agent):**
validates the deployment shape **and** root-level trace capture.

| Dimension | Status |
|---|---|
| create (`CreateAgentRuntime` → READY) | ✅ PASS-EXERCISED |
| agent (root invoke) | ✅ PASS-EXERCISED |
| **remote MCP** | ✅ PASS-EXERCISED — an objective **root-level** `docs_read_wiki_structure` DeepWiki call returning real react wiki sections ("Overview" / "Feature Flags System") — unforgeable from memory |
| skills | 🟡 PASS-WIRED — embedded in the source package, no event this run |

Both receipts run on **Nova Pro** to prove the control plane, container, invocation path, and
delegation. The model **mapping** is Claude-native — no remap: a folder's `claude-*` maps to its
regional Bedrock inference profile, while a non-Claude id like `us.amazon.nova-pro-v1:0` passes
through verbatim. The same-Claude-brain receipt is pending the one-time per-account Anthropic
use-case entitlement (Gate A, eventually-consistent) — **a pending entitlement, not a code gap.**
Nova is **not** claimed equivalent to Claude; it proves the path the Claude brain will ride.

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
live Harness + Runtime deploys, the receipts, and the two gates from
[`docs/deploy-bedrock.md`](deploy-bedrock.md).*
