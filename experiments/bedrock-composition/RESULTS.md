# Experiment: Strands subagent composition on Bedrock (local, bearer-token-verified)

The Bedrock target (planned 4th deploy target) would compile the neutral
`.managed-agents/` folder to a **Strands Agents** source package and host it in an
**AgentCore Runtime**. Before any plan/codegen/target work, this is the *confirm-live-
before-encoding* step — the exact playbook Google began with
([../subagent-composition](../subagent-composition)): prove the composition runs with the
**credential we actually have** (a Bedrock *bearer-token API key*, model-inference only),
not the credential a hosted deploy needs (AWS IAM + an execution role).

## What's proven live (no IAM, no hosted runtime — just the bearer token)

`python bedrock_strands_subagents.py` — confirmed 2026-06-04 in **eu-north-1** with
**strands-agents 1.42.0 / boto3 1.43.22**, authenticated solely by
`AWS_BEARER_TOKEN_BEDROCK`:

```
MODEL: eu.amazon.nova-pro-v1:0 region: eu-north-1
QUESTION: What year was the Eiffel Tower completed, and what is the population of Paris?
--- running (live Bedrock inference) ---
Tool #1: researcher
Tool #2: population_lookup
--- tool-call trace (objective: each is a real invocation) ---
  1. subagent researcher(question='What year was the Eiffel Tower completed?')
  2. deterministic-tool population_lookup(city='Paris')
  model-emitted toolUse blocks: ['researcher', 'population_lookup']
--- final coordinator answer ---
  The Eiffel Tower was completed in 1889, and the population of Paris is 2,102,650.
OK: coordinator delegated to a sub-agent AND used a deterministic tool.
```

Three facts established:

1. **The bearer token authenticates Strands' inference path locally.** Strands' default
   `BedrockModel` drives `bedrock-runtime` (`converse` / `converse_stream`) through boto3,
   which reads `AWS_BEARER_TOKEN_BEDROCK` — **no IAM creds, no `~/.aws/credentials`**. (Floor:
   boto3/botocore **>= 1.40**; 1.34 ignores the bearer var and falls through to
   `NoCredentialsError`.)
2. **Strands "agents as tools" == agentlift's coordinator/subagent shape.** A specialist
   `researcher` agent wrapped as a `@tool` is the Strands idiom for a sub-agent; the
   coordinator delegated to it server-of-the-model's own volition. This is the Bedrock
   analogue of ADK `sub_agents` — one process, in-model delegation — so Bedrock subagents
   will classify `emulated` (one AgentCore Runtime), exactly like Google.
3. **Tool-calling is real, not narrated.** The signal is objective on two independent
   channels: the python tool bodies actually ran (the `TRACE` list), *and* the model
   emitted `toolUse` blocks (`['researcher', 'population_lookup']`) in the conversation
   history. The final answer fuses both tool results.

## What this does NOT prove (two separate gates — keep them distinct)

### Gate A — the Claude-on-Bedrock entitlement (one-time console action, not a code blocker)

The composition above ran on **Amazon Nova Pro**, not Claude. Claude *is* native on
Bedrock and its id + region are **verified correct** — `eu.anthropic.claude-sonnet-4-5-
20250929-v1:0` returned a clean `BEDROCK_OK` earlier in this session via `converse`. But a
later call returned:

```
ResourceNotFoundException: Model use case details have not been submitted for this
account. Fill out the Anthropic use case details form before using the model. If you have
already filled out the form, try again in 15 minutes.
```

This is Bedrock's **Anthropic use-case form** — a one-time, per-account console action the
account owner submits (Bedrock console → Model access → Anthropic). It is the **exact
parallel to Google needing Claude enabled in Vertex Model Garden**: a console entitlement,
not an agentlift code path.

**The entitlement is eventually consistent — and in our testing it *flapped*.** A `converse`
against `eu.anthropic.claude-…` returned a clean `BEDROCK_OK` in one window, then the
use-case-form `ResourceNotFoundException` above in the next — and on the most recent retries
**both** `converse` *and* `converse_stream` (the path Strands actually uses under the hood)
returned the form error. So the receipt is gated on the entitlement *settling*, not on any
code change. We launched a **bounded background poller** to auto-capture the Claude composition
to `_claude_receipt.txt` the moment a `converse`/`converse_stream` succeeds; as of this writing
it has not yet propagated. Once it settles, re-running this same script with
`BEDROCK_MODEL_ID=eu.anthropic.claude-sonnet-4-5-20250929-v1:0` yields the Claude receipt
— the headline portability proof (the *same* Claude brain on Anthropic **and** Bedrock).
Nothing in the composition mechanics changes; only the model id.

### Gate B — the hosted deploy (the auth split, deferred by design)

This is **local** inference. Creating + invoking a hosted **AgentCore Runtime** is
control-plane (`bedrock-agentcore-control.create_agent_runtime`) and needs AWS **IAM
creds + an execution role + ECR/S3 + `iam:PassRole`** — which the bearer token cannot do.
So, mirroring Google (API key ran the local ADK subagent experiment but could not create a
`reasoningEngine`), the hosted receipt waits on IAM setup. The pure `bedrock_plan.py` +
offline tests + `bedrock_codegen.py` can all be built and tested *without* it; only
`bedrock_target.py`'s live path needs IAM.

## What this de-risks for the Bedrock target design

- **Compile target = Strands** (AWS-native, the AgentCore default). The coordinator/subagent
  → agents-as-tools mapping is confirmed to work, so `bedrock_codegen.py` can emit a Strands
  package with confidence.
- **Model story is cleaner than Google.** No Gemini-style remap — Claude stays Claude
  (subject to Gate A). The folder-id → Bedrock-inference-profile-id map (e.g.
  `claude-sonnet-4-6` → `eu.anthropic.claude-sonnet-4-5-...`) is the only model translation.
- **Two gates, both one-time and outside the code path** (Anthropic use-case form;
  IAM/execution-role). Both will be `Diagnostic`s / readiness checks the target surfaces —
  never silent.

## To graduate this

1. Submit the Anthropic use-case form, re-run this script with the Claude id → capture the
   `BEDROCK_OK`/Eiffel receipt on Claude (Gate A closed).
2. Build pure `bedrock_plan.py` (capability matrix: native/mapped/deferred/unsupported) +
   offline tests, then `bedrock_codegen.py` (BedrockAgentCoreApp + Strands package) +
   contract tests (`/ping`, `/invocations` with the model mocked).
3. With IAM creds (Gate B), the gated `bedrock_target.py`: CodeZip/cloud build → deploy →
   invoke → `stop_runtime_session` → `.agentlift-bedrock.json` spec-hash lock.

*Composition half confirmed 2026-06-04 (strands-agents 1.42.0, eu-north-1, bearer token,
Nova Pro). Claude half: NOT-PROVEN (Anthropic use-case form **eventually consistent and
currently flapping** — `BEDROCK_OK` once, then the form error on both `converse` and
`converse_stream`; id + region verified via that earlier `BEDROCK_OK`; bounded poller armed to
auto-capture on settle). Hosted half: NOT-PROVEN (needs IAM — by design).*
