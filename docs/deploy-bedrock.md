# Deploying to Amazon Bedrock AgentCore (the credentials path)

> Status: **build-only preview.** `agentlift deploy --target bedrock --build-only`
> compiles the folder to a **Strands Agents** source package and materializes a complete,
> deployable **AgentCore Runtime** container artifact — an ARM64 image that serves
> `POST /invocations` + `GET /ping` on `:8080`, plus an ARM64 `Dockerfile`, a
> `.dockerignore`, and a `NOTES.txt` runbook. That artifact path is what ships today. A
> **bare** `agentlift deploy --target bedrock` (the hosted create) **refuses** — it raises
> before any AWS call, makes no network request, and writes nothing — because the AgentCore
> control-plane wire shape is not yet live-verified here (the *confirm-live-before-encoding*
> rule; the same reason Claude-on-Vertex is an offline spike, not shipped). The compile maps
> **skills** (the SKILL.md bundles ride inside the runtime's source package, loaded with
> Strands `Skill.from_file` + `AgentSkills` at startup), **URL MCP servers** (each wired as
> a Strands `MCPClient` over streamable-HTTP with a raw-name `tool_filter` allowlist and a
> server-name prefix; inline auth header values resolve from your local environment into
> AgentCore Runtime `env_vars` at deploy, never inlined into the generated source), and the
> **Claude model mapping native** — Claude is a first-class Bedrock model, so unlike Google the
> compiler does **no model remap**; a folder's `claude-*` id is emitted as its regional Bedrock
> inference profile directly (a mapping fact — the live same-Claude composition receipt is
> pending stable Gate A; the composition itself is live-proven on Nova).
> `agentlift export bedrock-strands` emits the same Strands scaffold offline. This
> doc is the credentials/setup the (eventual) hosted deploy needs, and the runbook for the
> artifact you build today.

## The one thing to get straight: bearer token vs IAM

Bedrock offers **two credential types**, and they are not interchangeable for our purpose:

| Credential | What it authenticates | Good for |
|---|---|---|
| **Bedrock bearer token** (`AWS_BEARER_TOKEN_BEDROCK`) | **model inference** only (`bedrock-runtime`: `converse` / `converse_stream`) | local Strands runs, model inference, testing |
| **AWS IAM credentials** *(required to deploy)* | your **AWS identity** (control-plane + `iam:PassRole`) | **creating / managing** a hosted AgentCore Runtime |

**You cannot create a hosted runtime with the bearer token.** Creating an AgentCore Runtime
(`bedrock-agentcore-control.create_agent_runtime`) is a control-plane operation: it needs
IAM credentials, an execution role, an ECR image, and `iam:PassRole`. The bearer token is
exactly what the local [bedrock-composition experiment](../experiments/bedrock-composition/)
used — that runs the agent locally against Bedrock model inference; it does not deploy
anything. This is the same split Google has (an API key ran the local ADK experiment but
could not create a `reasoningEngine`).

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

### Gate B — the hosted deploy credentials (the auth split, deferred by design)

Creating + invoking a hosted AgentCore Runtime is control-plane and needs **AWS IAM
credentials + an AgentCore execution role (with `iam:PassRole`) + an ECR repository** in the
deploy region — which the bearer token cannot do. So, mirroring Google, the hosted receipt
waits on IAM setup. The pure `bedrock_plan.py` + offline tests + `bedrock_codegen.py` +
`bedrock_target.py` build path can all run *without* it; only the live hosted create needs
it (and that step stays manual today — see below).

## The build-only path (what ships today)

```bash
agentlift deploy ./examples/team --target bedrock --build-only
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

## What a hosted deploy will require (Gate B, step by step)

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
   passes the resulting value as an AgentCore Runtime `env_var`,
3. emits `os.environ.get("AGENTLIFT_MCP_…")` in the `MCPClient` headers — so only the
   env-var *name* is ever written to disk, the plan, or the lockfile.

This is the identical secret-handling discipline as the Google target: a referenced-but-unset
variable is flagged, not silently skipped, and the value never lands in source.

## Two known gaps, and how to work around them

The compile maps skills, URL MCP (with inline auth), and the Claude model natively. Two
capability areas are surfaced as `PLANNED` diagnostics today and not yet wired — both with a
workaround that keeps the *same* neutral folder.

### Built-in sandbox tools (`bash`/`files`/`glob`/`grep`)

Unlike Google's Python/JS-only sandbox, Bedrock genuinely offers a **real** sandbox — the
AgentCore **Code Interpreter** (shell + filesystem) and **Browser** tools. So for Bedrock
this is a *not-yet-wired* item, not a non-goal: the audit rates it `emulated` (the platform
can do it) and `agentlift plan` marks the built-ins `PLANNED` rather than dropping them
silently. Until they are wired, expose equivalents through a **URL MCP server** (which *does*
compile), or keep sandbox-heavy agents on Anthropic.

### Built-in web tools (`web_search`/`web_fetch`)

`web_fetch` can map to the AgentCore Browser tool, but Bedrock has no first-class hosted
`web_search` primitive the way Anthropic and Gemini do. Both are surfaced as `PLANNED` today;
supply `web_search` via a search MCP server, or keep web agents on Anthropic / Google.

### `:ask` / per-tool approval

The hosted `/invocations` call is request/response with no interactive approval channel, and
Strands human-in-the-loop hooks do not cross the hosted boundary. So `:ask` is **unsupported**
on the AgentCore Runtime (a `bedrock.tool_approval.unsupported` diagnostic). Enforce approval
client-side in the loop that calls the runtime, or keep `:ask` agents on Anthropic where the
gate is native.

## Cost

A deployed AgentCore Runtime is billed compute plus model tokens per run. The local Strands
path (bearer token, no deploy) is just model tokens. Tear down runtimes you are not using.

## Why the local experiment only needed the bearer token

[`experiments/bedrock-composition/bedrock_strands_subagents.py`](../experiments/bedrock-composition/bedrock_strands_subagents.py)
runs the coordinator + sub-agent **in your process** against Bedrock model inference,
authenticated solely by `AWS_BEARER_TOKEN_BEDROCK`. That proves the *composition* (the
coordinator delegates to a researcher sub-agent and calls a deterministic tool — objective
tool-call trace, confirmed live). A hosted deploy is the separate step that needs Gate B.
This is the same "where does the loop run" distinction the audit makes: local/your-app vs
hosted.
