# Changelog

All notable changes to **agentlift** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions match the published PyPI
releases and git tags ([semantic versioning](https://semver.org/)).

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
