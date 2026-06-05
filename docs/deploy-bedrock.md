# Deploying to Amazon Bedrock AgentCore (the credentials path)

> Status: **two primitives behind `--mode`.** AgentCore exposes a *managed* agent
> (**Harness** — config-only, single agent; a **live `deploy`**) and a *custom-container* agent
> (**Runtime** — multi-agent, build-only). agentlift maps to both; `--mode auto` (the default)
> picks the lightest one that preserves your folder's semantics, never a silent downgrade.
>
> - **`--mode harness` — ✅ live single-agent deploy.** A single agent — with its **skills, remote
>   MCP, sandbox, and browser** — deploys **live over IAM, no container**, via the control-plane
>   `CreateHarness`. The full path is **verified end-to-end by a committed Nova receipt**
>   ([`20260605-121525-harness-bedrock`](../tests/live/receipts/); `_HARNESS_LIVE_VERIFIED = True`):
>   **6/6 single-agent cells EXERCISED** — agent + base-session sandbox (`shell`) + remote MCP
>   (`docs_read_wiki_structure`) + an S3-loaded skill + `agentcore_browser`. Two honest notes — the
>   AgentCore **Harness feature is in AWS public preview**, and Claude inference runs in the harness
>   but is gated by the Anthropic use-case entitlement (**Gate A**, eventually-consistent; the Nova
>   receipt is model-agnostic for the wire shape). Per-tool MCP `allowedTools` narrowing isn't
>   enforced in preview (a restrictive allowlist suppresses MCP surfacing, so agentlift emits none +
>   diagnoses).
> - **`--mode runtime --build-only` — build-only.** Compiles the folder to a **Strands Agents**
>   source package and materializes a complete, deployable **AgentCore Runtime** container
>   artifact — an ARM64 image serving `POST /invocations` + `GET /ping` on `:8080`, plus a
>   `Dockerfile`, `.dockerignore`, and a `NOTES.txt` runbook. A `--mode runtime` deploy
>   *without* `--build-only` (the hosted create) **refuses** — it raises before any AWS call,
>   makes no network request, writes nothing — because the `create_agent_runtime` control-plane
>   wire shape is not live-verified here (the *confirm-live-before-encoding* rule).
>
> Both emit the **Claude model mapping native** — Claude is a first-class Bedrock model, so
> unlike Google the compiler does **no model remap**; a folder's `claude-*` id is emitted as
> its regional Bedrock inference profile directly. Both map **URL MCP servers** (Harness:
> `remote_mcp` tool; Runtime: a Strands `MCPClient` over streamable-HTTP) with a `tool_filter`
> allowlist; inline auth header values resolve from your local environment into the deployed
> resource's `env_vars` at deploy, never inlined into source/plan/lock. **Skills** work on both:
> the Runtime *embeds* the SKILL.md bundles in its source package (`Skill.from_file` +
> `AgentSkills`); the Harness *uploads* each bundle to `$AGENTLIFT_BEDROCK_S3_BUCKET` and attaches
> it via `skills[].s3.uri` (the harness fetches it at invoke — live-verified). Only a **multi-agent
> team** (subagents) routes to the Runtime. `agentlift export bedrock-strands` emits the
> Runtime's Strands scaffold offline. This doc is the credentials/setup for both paths.

## The one thing to get straight: bearer token vs IAM

Bedrock offers **two credential types**, and they are not interchangeable for our purpose:

| Credential | What it authenticates | Good for |
|---|---|---|
| **Bedrock bearer token** (`AWS_BEARER_TOKEN_BEDROCK`) | **model inference** only (`bedrock-runtime`: `converse` / `converse_stream`) | local Strands runs, model inference, testing |
| **AWS IAM credentials** *(required to deploy)* | your **AWS identity** (control-plane + `iam:PassRole`) | **creating / managing** a hosted AgentCore resource (Harness *or* Runtime) |

**You cannot create a hosted resource with the bearer token.** Both control-plane creates —
the managed `bedrock-agentcore-control.create_harness` and the custom-container
`create_agent_runtime` — are SigV4/IAM operations needing IAM credentials, an execution role,
and `iam:PassRole`. (The Runtime additionally needs an **ECR image**; the **Harness needs no
container, no ECR, no Docker** — that is the whole point of the managed primitive, and why its
preview create is cheap enough to *run* rather than refuse.) The bearer token is exactly what
the local [bedrock-composition experiment](../experiments/bedrock-composition/) used — that
runs the agent locally against Bedrock model inference; it does not deploy anything. This is
the same split Google has (an API key ran the local ADK experiment but could not create a
`reasoningEngine`).

## Two one-time gates (both outside agentlift's code path)

A hosted Claude-on-Bedrock deploy clears two independent, per-account gates. agentlift
surfaces both — in `agentlift plan --target bedrock` diagnostics and in the artifact's
`NOTES.txt` readiness checklist — and never silently assumes either.

### Gate A — the Claude-on-Bedrock entitlement (a console action, not a code path)

Claude is native on Bedrock, but invoking an Anthropic model requires submitting the
**Anthropic use-case form** once per account (Bedrock console → **Model access** →
Anthropic). Until it is approved, a `converse`/`converse_stream` call against a Claude
inference profile returns:

```
ResourceNotFoundException: Model use case details have not been submitted for this account.
Fill out the Anthropic use case details form before using the model. If you have already
filled out the form, try again in 15 minutes.
```

This is the **exact parallel to Google needing Claude enabled in Vertex Model Garden** — a
console entitlement, not an agentlift code path. The entitlement is **eventually
consistent**: in our testing a `converse` against `eu.anthropic.claude-haiku-4-5-…` returned
a clean `BEDROCK_OK` in one window, then the use-case-form error in the next — so allow it
to propagate. Non-Claude models (e.g. Amazon Nova) skip Gate A entirely.

### Gate B — the hosted deploy credentials (the auth split)

Creating a hosted AgentCore resource is control-plane and needs **AWS IAM credentials + an
AgentCore execution role (with `iam:PassRole`)** — which the bearer token cannot do. The two
primitives differ in how much *else* they need:

- **Harness** — IAM + an execution role, **no ECR, no Docker, no image build**. That is the
  whole reason agentlift *runs* the harness preview create rather than refusing it: minutes,
  not a container pipeline. Set the role in `$AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN` (below).
- **Runtime** — IAM + an execution role **+ an ECR repository + a pushed ARM64 image**. That
  heavier path is why the runtime hosted create stays manual (see below).

The pure `*_plan.py` + offline tests + codegen + target build paths all run *without* IAM;
only a live create needs it. **Gate A applies only when the model is Claude** — a Nova-backed
harness sidesteps the entitlement entirely, which is exactly how the first harness wire-shape
receipt is cheapest to land (Nova create/invoke proves the shape; the Claude brain swaps in
once Gate A is stable).

## The managed Harness path (✅ live single-agent deploy)

A folder with **one agent** — its skills, remote MCP, sandbox, and browser — deploys live to a
managed AgentCore Harness: config only, no container, minutes. `--mode auto` selects it for any
single-agent folder; a multi-agent *team* routes to the Runtime instead.

```bash
# IAM creds on PATH (NOT the bearer token), the execution role, + a skills bucket if you have skills:
export AGENTLIFT_BEDROCK_EXECUTION_ROLE_ARN=arn:aws:iam::<acct>:role/agentlift-harness
export AGENTLIFT_BEDROCK_S3_BUCKET=my-agentlift-skills-bucket   # only if the folder has skills

agentlift plan   ./my-agent --target bedrock                 # auto -> shows the harness plan
agentlift deploy ./my-agent --target bedrock --mode harness  # runs CreateHarness live
agentlift deploy ./my-agent --target bedrock                 # auto: same harness, live
```

**Execution role needs:** `bedrock:InvokeModel` (the model/inference profile), and — if the
folder has skills — `s3:ListBucket` + `s3:GetObject` on `$AGENTLIFT_BEDROCK_S3_BUCKET` (the
harness fetches the uploaded bundle from S3 at invoke time).

**What the live receipt proves.** [`tests/live/receipts/20260605-121525-harness-bedrock`](../tests/live/receipts/)
(`_HARNESS_LIVE_VERIFIED = True`): `CreateHarness` → READY, then `InvokeHarness` exercised **6/6
single-agent cells** server-side — the **agent** (Nova inference), **base-session sandbox** (`shell`),
**remote MCP** (`docs_read_wiki_structure` — the harness surfaces MCP tools as `<server>_<tool>`),
an **S3-loaded skill** (agentlift uploads the bundle to `$AGENTLIFT_BEDROCK_S3_BUCKET`, the harness
fetches it via `skills[].s3.uri` and applies it), and **`agentcore_browser`** (a *session-based*
browser: init → navigate → read, so `web_fetch` maps to it approximately). Two honest notes: the
AgentCore **Harness feature is in AWS public preview**, and **Claude** inference *runs* in the
harness (a Claude harness answered `INVOKE-OK`) but is gated by the per-account **Anthropic
use-case entitlement (Gate A)**, eventually-consistent (it flapped back to `ResourceNotFoundException`
minutes later) — so the model-agnostic wire-shape receipt was captured on **Nova**. Per-tool MCP
`allowedTools` narrowing isn't enforced in preview (a restrictive allowlist suppresses MCP
surfacing, so agentlift emits none + diagnoses).

What the deploy does:

1. builds the pure `HarnessDeployPlan` (`agentlift plan --target bedrock --mode harness --json`
   shows it: resolved Claude inference-profile model, `systemPrompt`, `remote_mcp` tools,
   `agentcore_browser` for web built-ins, skill bundles, MCP-auth env-var names, the spec hash,
   `live_verified: true`),
2. uploads any skill bundles to `$AGENTLIFT_BEDROCK_S3_BUCKET` and resolves their `skills[].s3.uri`;
   resolves any MCP inline-auth header values from your local env into harness `env_vars`,
3. calls `CreateHarness` (or `UpdateHarness` / skip, decided by the spec hash in
   `.agentlift-harness.json`; a since-deleted `clientToken` triggers a retry without it), polls
   until `READY`, and records the lock.

The harness **cannot** represent subagents (single-agent primitive) — a multi-agent *team* routes
to the Runtime under `auto`, surfaced as the reason. (Cross-agent shared/private skill *scoping*
needs ≥2 agents, so that too is a Runtime story; a single agent's own skills deploy fine on the
harness.) `--build-only` is **N/A** to the harness (there is no container) and is rejected. The
default region is a harness-preview region (`us-west-2`);
`--bedrock-region` overrides it, and the region flows into the Claude inference-profile prefix
(`us.` / `eu.` / `apac.` / `global.`), so changing it forces a fresh create.

> The lock (`.agentlift-harness.json`) is written on a successful **preview** create/update for
> idempotency, and it carries `live_verified: false` — it is operational state, not proof. Only
> a committed *receipt* flips the verified flag and lets docs claim the cells `EXERCISED`.

## The build-only Runtime path (multi-agent container artifact)

```bash
agentlift deploy ./examples/team --target bedrock --mode runtime --build-only
```

materializes the container build context under `./examples/team/.agentlift-build/bedrock/`:

```
agentlift_runtime/
  agent.py                 # Strands: coordinator + agents-as-tools subagents, MCPClients, AgentSkills
  skills/<name>/SKILL.md    # skill bundles embedded for Skill.from_file
requirements.txt           # strands-agents>=1.42, bedrock-agentcore, boto3>=1.40
Dockerfile                 # FROM --platform=linux/arm64 python:3.12-slim ; EXPOSE 8080
.dockerignore
NOTES.txt                  # the build/push + hosted-create runbook (below)
```

The `NOTES.txt` is the runbook: the two-gate readiness checklist, **concrete** `docker
buildx build --platform linux/arm64` + `aws ecr` build/push commands (those are stable
Docker/ECR steps, not guessed), and a **MANUAL** hosted-create section. That last section
deliberately does **not** emit a `create-agent-runtime` call — it points at the AgentCore
starter toolkit (`agentcore configure`/`launch`) and the current AWS docs, because the
control-plane wire shape is not live-verified here. Building a guessed create call as
copy-paste would be exactly the kind of unverified encoding this repo refuses.

## What a hosted *Runtime* deploy will require (Gate B, step by step)

(The managed Harness needs only steps 1 + 3 — no ECR, no image build. These extra steps are
the Runtime's container pipeline.)

```bash
# 1. AWS IAM credentials on PATH (env vars, profile, or instance role) — NOT the bearer token
aws sts get-caller-identity                      # should resolve your IAM identity

# 2. an ECR repository in the deploy region
aws ecr create-repository --repository-name agentlift-lead --region eu-north-1

# 3. an AgentCore execution role with iam:PassRole (the runtime assumes it)
#    see the AgentCore custom-container deploy docs (linked from NOTES.txt)

# 4. build + push the ARM64 image (from the build dir)
aws ecr get-login-password --region eu-north-1 \
  | docker login --username AWS --password-stdin <acct>.dkr.ecr.eu-north-1.amazonaws.com
docker buildx build --platform linux/arm64 -t <acct>.dkr.ecr.eu-north-1.amazonaws.com/agentlift-lead:latest --push .

# 5. create the runtime from that image (MANUAL today — starter toolkit or create-agent-runtime)
```

## What agentlift will read

Put these in `.env` (gitignored). The bearer token drives **local** Strands runs; IAM
credentials (whichever way you supply them) drive a **hosted** deploy:

```
AWS_REGION=eu-north-1
AWS_BEARER_TOKEN_BEDROCK=...        # local model inference (Strands runs); never committed
# IAM for a hosted deploy is read from the standard AWS chain:
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN, or AWS_PROFILE, or an instance role
```

`agentlift plan --target bedrock` prints the resolved model id per agent, the spec hash, and
the env-var names any inline-auth MCP server needs — so you can confirm everything before a
build.

## MCP auth headers (secrets stay out of the source)

If a URL MCP server in the folder carries an inline auth header (e.g.
`"Authorization": "Bearer ${SECURE_API_TOKEN}"`), agentlift does **not** write the secret
into the generated agent code. At deploy time it:

1. derives a stable AgentCore env-var name from the server + header (e.g.
   `AGENTLIFT_MCP_<SERVER>_<HEADER>`),
2. resolves the template against **your local environment** (`SECURE_API_TOKEN` above) and
   passes the resulting value as a deployed-resource `env_var` (harness `env_vars` or runtime
   `env_vars`),
3. emits `os.environ.get("AGENTLIFT_MCP_…")` in the MCP headers — so only the env-var *name*
   is ever written to disk, the plan, or the lockfile.

This is the identical secret-handling discipline as the Google target: a referenced-but-unset
variable is flagged, not silently skipped, and the value never lands in source.

## Built-in tools and `:ask` (where the two primitives differ)

The built-in sandbox/web tools map **differently per primitive** — the Harness covers them
today, the Runtime has them as `PLANNED`:

### Built-in sandbox tools (`bash`/`files`/`glob`/`grep`)

- **Harness:** the managed base session **always carries** shell + file_operations
  (`@builtin`), so the sandbox built-ins map onto those native tools — config-only, nothing
  added. (This is provisional with the rest of the harness shape until the receipt.)
- **Runtime:** Bedrock genuinely offers a **real** sandbox — the AgentCore **Code Interpreter**
  (shell + filesystem) — but agentlift does not wire it into the container yet, so it is
  surfaced as `PLANNED`. Until then, expose equivalents through a **URL MCP server** (which
  *does* compile), or use `--mode harness` for a single agent.

### Built-in web tools (`web_search`/`web_fetch`)

- **Harness:** both map onto the `agentcore_browser` tool. `web_fetch` maps cleanly; `web_search`
  is *approximate* — a browser is not a first-class hosted `web_search` grounding primitive the
  way Anthropic and Gemini expose one (hence the audit rates web built-ins `degraded`).
- **Runtime:** `PLANNED` (browser tool not yet wired). Supply `web_search` via a search MCP
  server, or keep web-heavy agents on Anthropic / Google.

### `:ask` / per-tool approval — **unsupported on both**

Neither hosted primitive has an interactive approval channel: the Runtime `/invocations` call
is request/response, the Harness invoke is non-interactive, and Strands human-in-the-loop
hooks do not cross the hosted boundary. So `:ask` is **unsupported** on AgentCore (a
`bedrock.tool_approval.unsupported` diagnostic, never a silent drop). Enforce approval
client-side in the loop that calls the resource, or keep `:ask` agents on Anthropic where the
gate is native.

## Cost

A deployed AgentCore resource is billed compute plus model tokens per run; the managed Harness
adds no container/ECR cost. The local Strands path (bearer token, no deploy) is just model
tokens. Tear down harnesses/runtimes you are not using from the **AWS console** (or
`bedrock-agentcore-control delete_harness` / `delete_agent_runtime` via boto3) — `agentlift
destroy` archives **Anthropic** agents from the lockfile only, it does not yet delete AgentCore
resources.

## Why the local experiment only needed the bearer token

[`experiments/bedrock-composition/bedrock_strands_subagents.py`](../experiments/bedrock-composition/bedrock_strands_subagents.py)
runs the coordinator + sub-agent **in your process** against Bedrock model inference,
authenticated solely by `AWS_BEARER_TOKEN_BEDROCK`. That proves the *composition* (the
coordinator delegates to a researcher sub-agent and calls a deterministic tool — objective
tool-call trace, confirmed live). A hosted deploy is the separate step that needs Gate B.
This is the same "where does the loop run" distinction the audit makes: local/your-app vs
hosted.
