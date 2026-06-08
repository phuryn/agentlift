# Changelog

All notable changes to **agentlift** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions match the published PyPI
releases and git tags ([semantic versioning](https://semver.org/)).

## [Unreleased]

**`agentlift import` — read a live agent back into the folder (the inverse of `deploy`).**
A new reverse pipeline reconstructs a neutral `.managed-agents/` folder from a live managed
runtime, so a runtime now round-trips both ways — and **migration between runtimes falls out
for free** (import from one provider, `deploy` to another). Anthropic import is **full**; AWS
Bedrock import covers the config-only **Harness**. The work is implemented and offline-tested
(full suite green) — this entry is a **draft for review, not yet released**.

### Added
- **`agentlift import <anthropic|bedrock> <out> [--agent N …] [--mode harness] [--harness-id|--harness-name] [--bedrock-region R] [--dry-run]`** —
  a **read-only** command (never creates/updates/archives). It fetches the live agent, maps it to
  a neutral project, writes the folder, and **self-verifies** by re-running the real parse + plan
  (prints `Round-trip OK` only if the result is deployable again). `--dry-run` is the import
  analogue of `plan` (prints the mapping + diagnostics, writes nothing).
- **Anthropic import (full).** `agents.list`/`agents.retrieve` + `skills.versions.download`
  recover system/description/model, built-in tools **with `:ask`/`:allow` permission policies**,
  URL MCP servers + per-server tool filters, **custom skill content**, and a coordinator's
  **`subagents`** (roster ids resolved to names; selecting a coordinator pulls its subagents into
  the closure automatically). Skills/MCP used identically by >1 agent are hoisted to `shared/`
  (skills keyed by content hash, MCP by full identity) — the inverse of the planner's dedup.
- **Bedrock Harness import** (`import bedrock --mode harness`). `get_harness` + S3 skill bundles,
  with a **reverse model-map** (regional inference profile → folder Claude id, e.g.
  `eu.anthropic.claude-haiku-4-5-20251001-v1:0` → `claude-haiku-4-5`), `agentCoreBrowser` → web
  tools, `agentCoreCodeInterpreter` → sandbox builtins, and `remote_mcp` → URL MCP. Single-agent
  by nature, so an import never produces subagents.
- **New modules**, mirroring the deploy pipeline in reverse (pure core + thin network edge):
  `import_model.py`, `importer.py` (inverse of `planner.py`), `folder_writer.py` (inverse of
  `parser.py`) — all pure and offline-tested — plus the network edges `anthropic_source.py` and
  `harness_source.py`.
- **End-to-end tests.** `tests/test_importer.py` (the mapping contract), `tests/test_import_roundtrip.py`
  (provider responses → folder → real parser → real planner, including **subagent delegation with
  shared *and* custom skills and MCP servers**), `tests/test_import_source.py` (fetch wiring via
  fake clients, no network), `tests/test_cli_import.py`, and a gated read-only live test
  `tests/live/test_import_anthropic.py` (`AGENTLIFT_LIVE_IMPORT=1`).
- **Docs.** New [docs/import.md](docs/import.md); import notes added across the convention,
  how-it-works, provider-matrix, anthropic-mapping, deploy-bedrock, deploying, and limitations docs.

### Notes / honest boundaries
- **The Bedrock Runtime is not importable.** A Runtime bakes its agent definition into an opaque
  ARM64 **container image** (`GetAgentRuntime` returns only a `containerUri`), so it can't be read
  back; `import bedrock --mode runtime` refuses with that reason. This is the import analogue of
  the deploy-time `/invocations` trace boundary. Google/OpenAI import is not implemented yet.
- **Four one-way losses, each surfaced as a Diagnostic (never silent):** knowledge inlining is
  one-way (it stays in the prompt body on import); custom tools / harness inline functions are
  dropped; MCP auth **values** are provider-side, so only the header/env-var **name** is recovered;
  Anthropic first-party (`type: anthropic`) skills are referenced by id with no downloadable content.
- **One wire-format item pending a live download:** the Anthropic skill-bundle archive layout (a
  zip whose members carry the `<name>/…` prefix) is the documented shape but is **not yet confirmed
  against a live download** — the unpack is defensive and emits `import.skill_archive_shape` if the
  payload is anything else (the repo's confirm-live-before-trusting rule).

## [0.7.0] — 2026-06-05

**AWS Bedrock AgentCore Runtime is now a live multi-agent hosted deploy** (Stage 2). Both
AgentCore primitives are live-verified: the single-agent **Harness** (0.6.0) and now the
custom-container **Runtime**. `--mode auto` routes a single agent → Harness and a multi-agent
team → Runtime; mapping stays **Claude-native** (no remap), with wire-shape receipts on Nova
because Claude inference on Bedrock is a one-time per-account entitlement (Gate A), not a code gap.

### Added
- **`--mode runtime` — live hosted multi-agent deploy.** `agentlift deploy --target bedrock
  --mode runtime` builds the ARM64 Strands/AgentCore container, creates the ECR repo + logs in +
  `docker buildx --platform linux/arm64 --push`, calls **`CreateAgentRuntime`** (PUBLIC network,
  HTTP `serverProtocol`, IAM-only — no JWT authorizer), polls READY, writes
  `.agentlift-bedrock.json`, and **`InvokeAgentRuntime`**. Gated by `_RUNTIME_LIVE_VERIFIED`
  (now True) — a bare hosted create refused until a committed receipt.
- **Subagent delegation live-proven on a Nova receipt** (`tests/live/receipts/20260605-134012-runtime-bedrock`):
  a coordinator + 2 specialists where create + agent + **delegation** are all PASS-EXERCISED (the
  coordinator's top-level trace named both specialists). A single-agent smoke
  (`20260605-133821-runtime-bedrock`) separately got **remote MCP PASS-EXERCISED** (an objective
  root-level `docs_read_wiki_structure` DeepWiki call).
- **Top-level tool-call trace in the generated handler** — returns `{result, tool_calls?}` where
  `tool_calls` is read from `AgentResult.metrics.tool_metrics` (fail-open: trace extraction never
  breaks the invocation), so the deploy receipt can prove delegation objectively.
- The `boto3` (`bedrock`) optional dependency now also covers the Runtime hosted deploy.

### Changed
- The bare `agentlift deploy --target bedrock --mode runtime` now **deploys live** instead of
  refusing; `--build-only` still emits just the ARM64 container artifact (its `NOTES.txt` now
  points at the live hosted-create path).
- `.agentlift-bedrock.json` (the Runtime lock) is now live-writing (spec hash → create/update/skip).

### Notes / honest boundaries
- **The `/invocations` boundary.** `InvokeAgentRuntime` returns the container's app-defined JSON
  body, not a tool-event stream. So subagent delegation and **root-level** skill/MCP calls are
  objective (PASS-EXERCISED), while a specialist's **nested** skill/MCP calls don't cross the
  boundary → PASS-WIRED + text-corroborated (the runtime analogue of the Google
  `AgentTool`→`stream_query` grounding-metadata caveat).
- **MCP per-tool-filter limitation narrowed.** The unenforced-`allowedTools` limitation applies only
  to the **direct `remote_mcp` attachment** path; for AgentCore **Gateway**-fronted MCP, tool scoping
  is enforced server-side at the Gateway/Policy layer (AWS-documented; agentlift has not yet
  live-verified that path).
- Runtime execution role needs: `bedrock-agentcore.amazonaws.com` trust (`aws:SourceAccount`
  condition — not a region-locked `SourceArn`), ECR pull, `bedrock:InvokeModel`, CloudWatch Logs.

## [0.6.0] — 2026-06-05

**AWS Bedrock AgentCore** joins the deploy targets, with two primitives behind `--mode`
(`auto` picks the least-powerful one that preserves the folder's semantics — never a silent
downgrade). Both are **Claude-native** — no Gemini-style model remap; a folder's `claude-*`
maps to its regional Bedrock inference profile directly.

### Added
- **`--mode harness` — live single-agent deploy** to a managed AgentCore Harness (config-only,
  IAM-only, no container). One agent with its **skills, remote MCP, sandbox, and browser**,
  **6/6 live-verified end-to-end** by a committed Nova receipt
  (`tests/live/receipts/20260605-121525-harness-bedrock`): agent + base-session sandbox
  (`shell`) + remote MCP (`docs_read_wiki_structure`, surfaced as `<server>_<tool>`) + an
  **S3-loaded skill** (`skills[].s3.uri`) + `agentcore_browser`.
  - Skills upload to `$AGENTLIFT_BEDROCK_S3_BUCKET` and attach via `skills[].s3.uri`; the
    execution role needs `s3:ListBucket` + `s3:GetObject`.
  - Idempotent via `.agentlift-harness.json` (spec hash → create/update/skip); a since-deleted
    `clientToken` triggers a retry without it.
- **`--mode runtime --build-only`** — compiles a **Strands** package + a complete deployable
  **ARM64 AgentCore Runtime container** (image + `Dockerfile` + `NOTES.txt` runbook) for the
  multi-agent path.
- `audit` and `export bedrock-strands` cover the new target; `agentlift plan --target bedrock
  [--mode auto|harness|runtime]`.
- `CHANGELOG.md`; a CI doc-link guard (`tests/test_doc_links.py`) that checks every relative
  path + intra-repo anchor in the README and docs.

- **Google built-in web tools** (`--target google`): `web_search` → Google Search grounding and
  `web_fetch` → URL Context, each lowered as a wrapped single-tool ADK sub-agent; exercised on a
  separate live Google deploy.
- **Claude-on-Vertex** — an offline-verified spike (`experiments/claude-on-vertex/`): ADK resolves
  Claude on Vertex and the mixed-model shape composes. Not shipped — a Claude `--google-model` is
  refused (no live receipt yet); folder Claude models still map to Gemini on Google.

### Changed
- **README restructured** (≈494 → ≈214 lines): big-picture-first, with a one-glance provider
  snapshot (AWS Bedrock now first-class), the deep capability matrix moved to
  `docs/provider-matrix.md`, and consistent collapsibles. Updated hero graphic (four backends).
- Provider docs reconciled to the live-verified state (`docs/provider-matrix.md`,
  `deploy-bedrock.md`, `tested-platforms.md`, `limitations.md`).

### Notes / known limits
- A multi-agent **team** (subagents) routes to the **Runtime**; the harness is single-agent.
  The Runtime's *hosted* create (`create_agent_runtime`) is **build-only** here (not yet
  live-verified) — a hosted multi-agent deploy is the next milestone.
- The AgentCore **Harness feature is in AWS public preview**. Claude inference *runs* in the
  harness but is gated by the per-account **Anthropic use-case entitlement (Gate A)**, which is
  eventually-consistent — so the model-agnostic wire-shape receipt was captured on Amazon Nova.
- Per-tool MCP `allowedTools` narrowing is not enforced on the harness preview (a restrictive
  allowlist suppresses MCP tool surfacing), so agentlift emits none and surfaces a diagnostic.
- `:ask` is not enforced on either Bedrock primitive (no interactive approval channel).

## [0.5.0] — 2026-06-04

### Added
- **Google Vertex AI Agent Engine** deploy (`--target google`) gains full **Skills** (embedded
  in the source package, loaded via ADK `load_skill_from_dir`) and **URL MCP** (`McpToolset` +
  `tool_filter`, inline auth resolved into Agent Engine `env_vars`).
- **6/6 live coverage matrix** — one neutral fixture deployed and queried on **both Anthropic
  and Google**, all six portability dimensions (agents · subagents · shared/individual MCP ·
  shared/individual skill) EXERCISED server-side, with committed receipts.
- `docs/provider-matrix.md` — the canonical cell-by-cell capability matrix.

[0.6.0]: https://pypi.org/project/agentlift/0.6.0/
[0.5.0]: https://pypi.org/project/agentlift/0.5.0/
