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
