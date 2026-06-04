# Deploying to Google Vertex AI Agent Engine (the credentials path)

> Status: **preview, live.** `agentlift deploy --target google` creates or updates a
> Vertex AI Agent Engine `reasoningEngine` from the folder. **All six portability
> dimensions have been exercised live** on a deployed engine — server-side
> coordinator-to-subagent delegation, both a shared and a private URL MCP server, and both
> a shared and a private skill (see the
> [coverage matrix](tested-platforms.md#live-coverage-matrix--receipt-evidence-not-a-capability-ranking)
> and the receipt under [`tests/live/receipts/`](../tests/live/receipts/)). The deploy maps
> **skills** (the SKILL.md bundles ride inside the engine's source package, loaded with
> ADK `load_skill_from_dir` at startup), **URL MCP servers** (each wired as an ADK
> `McpToolset` with a `tool_filter` allowlist; inline auth header values are resolved
> from your local environment at deploy time and passed as Agent Engine `env_vars`, never
> inlined into the generated source), and the **built-in web tools** (`web_search` →
> Gemini's Google Search grounding, `web_fetch` → URL Context, each lowered as a wrapped
> single-tool ADK sub-agent — both exercised live on a deployed engine). Deploys are
> idempotent — a spec hash drives create / update / skip. `agentlift export google-adk`
> emits the ADK scaffold offline. Remaining gaps: `:ask` / per-tool approval (not enforced
> on `VertexAiSessionService`) and the built-in **sandbox** tools (Python/JS only — no
> bash/files/glob-grep); stdio MCP servers can't be deployed (host them behind HTTPS
> first); and Claude models map to Gemini. This doc is the credentials/setup the deploy
> needs.

## The one thing to get straight: API key vs ADC

Google's Agent Platform offers **two authentication methods**, and they are not
interchangeable for our purpose:

| Method | What it authenticates | Good for |
|---|---|---|
| **API key** (`AQ.…`) | the **Model APIs** (calling Gemini) | local ADK runs, model inference, testing |
| **Application Default Credentials (ADC)** *(recommended)* | your **Google Cloud identity** (IAM) | **deploying / managing** hosted agents |

**You cannot deploy a hosted agent with the API key.** Creating an Agent Engine
(`agent_engines.create()` / a `reasoningEngine`) is a Cloud resource operation, so it
authenticates with **ADC**, not the model API key. The API key is exactly what the local
[subagent-composition experiment](../experiments/subagent-composition/) used — that runs
the agent locally; it does not deploy anything.

## What a hosted deploy actually requires

Authentication is necessary but not sufficient. A deploy needs **ADC + three deploy
parameters**:

1. **A GCP project** with **billing enabled** and the **Vertex AI API** turned on.
2. **A region / location** — Agent Engine is region-locked (`us-central1` is the safe default).
3. **A Cloud Storage staging bucket** — **mandatory**. Agent Engine packages your agent's
   code and uploads it to this bucket during deploy (`vertexai.init(staging_bucket="gs://…")`).
4. **ADC** for auth, set up either way:
   - `gcloud auth application-default login` (interactive, your user identity), **or**
   - the console's `setup_adc.sh`, **or**
   - a service-account key via `GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json`.
5. **IAM roles** on whichever identity ADC resolves to: `roles/aiplatform.user` (Vertex AI
   User) + write access to the staging bucket (`roles/storage.objectAdmin` on it).

## Setup, step by step

```bash
# 1. point gcloud at the project + enable the API
gcloud config set project YOUR_PROJECT_ID
gcloud services enable aiplatform.googleapis.com

# 2. a staging bucket in the same region
gcloud storage buckets create gs://YOUR_BUCKET --location=us-central1

# 3. ADC (the recommended auth) - this is what the console's setup_adc.sh does
gcloud auth application-default login
```

## What agentlift will read

Put these in `.env` (gitignored). Note `GOOGLE_GENAI_USE_VERTEXAI=TRUE` flips ADK from the
Gemini API (the API-key path) to Vertex (the ADC path):

```
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_GENAI_USE_VERTEXAI=TRUE
AGENTLIFT_GCP_STAGING_BUCKET=gs://your-bucket
# ADC is read from `gcloud auth application-default login`, OR set:
# GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

## MCP auth headers (secrets stay out of the source)

If a URL MCP server in the folder carries an inline auth header (e.g.
`"Authorization": "Bearer ${SECURE_API_TOKEN}"`), agentlift does **not** write the secret
into the generated agent code. At deploy time it:

1. derives a stable Agent Engine env-var name from the server + header (e.g.
   `AGENTLIFT_MCP_<SERVER>_<HEADER>`),
2. resolves the template against **your local environment** (`SECURE_API_TOKEN` above) and
   passes the resulting value as an Agent Engine `env_var`,
3. emits `os.environ.get("AGENTLIFT_MCP_…")` in the `McpToolset` headers — so only the
   env-var *name* is ever written to disk, the plan, or the lockfile.

`agentlift plan --target google` prints the env-var names it will populate (under
**"Env vars to populate"**) so you can confirm the referenced local variables are set
before deploying. A referenced-but-unset variable is flagged, not silently skipped.

## Two known gaps, and how to work around them

Google deploy maps skills, URL MCP (with inline auth), and the built-in **web** tools. Two
capabilities don't map — but both have a workaround that keeps the *same* neutral folder.

### Built-in sandbox tools (`bash`/`files`/`glob`/`grep`) → expose them as an MCP server

Agent Engine's hosted sandbox runs **Python/JS only** — there is no shell, and no workspace
filesystem to glob/grep over. So `bash`/`edit`/`write`/`glob`/`grep`/`read` deploy *without*
their built-in (a `google.builtin.degraded` warning, never a silent drop).

The escape hatch: anything the sandbox tools would have done, a **URL MCP server can do** —
and URL MCP *is* mapped on Google. Host a small MCP server that exposes the capability you
need (a filesystem server over a bucket/volume, a shell-exec server, a code-search server),
put it behind HTTPS, and add it to the agent's `mcp.json`. The agent then calls e.g.
`fs.read`/`shell.run` as MCP tools instead of the built-in `read`/`bash`. agentlift wires it
as an ADK `McpToolset` with a `tool_filter` allowlist, and inline auth resolves into an
Agent Engine `env_var` (see above) — so a private server stays private. This is the
provider-neutral way to give a Google deploy real filesystem/shell reach: the *definition*
is still one folder; only the runtime substrate differs (rented MCP server vs Anthropic's
built-in sandbox).

> Reframe, not a TODO: emulating Anthropic's sandbox tools *inside* Agent Engine is an
> explicit **non-goal** — the substrate is Python/JS, not a shell+FS, and pretending
> otherwise would be the kind of silent degradation agentlift exists to surface. MCP is the
> supported path.

### `:ask` / per-tool approval → gate it in your runner (or keep the agent on Anthropic)

A `:ask` suffix is a human-in-the-loop **permission policy**. On Anthropic it deploys as a
real per-tool gate. On Google it is **not enforced** — ADK tool-confirmation doesn't ride
through `VertexAiSessionService` on the deployed engine today — so a `:ask`-gated tool stays
callable without a prompt (a `google.tool_approval.unsupported` warning at plan time).

Two ways to keep the approval semantics:

1. **Gate client-side.** You already invoke a deployed engine *from your own code*
   (`remote.stream_query(...)`) — that loop is where approval belongs. Stream the events,
   pause when the model requests a `:ask`-marked tool, prompt your operator, and only then
   continue. The deployed engine is a callable; the human-in-the-loop lives in the caller,
   exactly as it would for any hosted API. (This is the same "where does the loop run"
   split as subagents: rented runtime, self-hosted control.)
2. **Keep `:ask` agents on Anthropic.** If the gate must be enforced *by the runtime* rather
   than your caller, deploy that agent to Anthropic, where the policy is native. The folder
   is unchanged; only the target differs.

Either way the `:ask` in the folder is never lost — it surfaces as a diagnostic, and the
policy is honored at the boundary you control.

## Cost

A deployed Agent Engine is billed compute (it provisions a managed `reasoningEngine`),
plus model tokens per run. The local ADK path (API key, no deploy) is just model tokens.
Tear down deployed engines you are not using.

## Why the local experiment only needed the API key

`experiments/subagent-composition/google_adk_subagents.py` runs the coordinator + sub-agent
**in your process** against the Gemini API, with `GOOGLE_GENAI_USE_VERTEXAI=FALSE`. That
proves the *composition* (subagents delegate). A hosted deploy is the separate step that
needs everything above. This is the same "where does the loop run" distinction the audit
makes: local/your-app vs hosted.
