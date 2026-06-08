# Import plan — reading live agents back into the folder

**Goal:** the reverse of `deploy`. Today agentlift goes *folder → provider*. This
plan adds *provider → folder*: read a live **Anthropic Managed Agent** (and an
**AWS Bedrock AgentCore Harness**) back into a neutral `.managed-agents/` folder.
Once a runtime can be read *in* and written *out*, **migration falls out for free**
— import from Anthropic, `deploy --target bedrock`, and vice-versa.

> Tagline today is *"Own the definition. Rent the runtime."* Import makes it
> *"…and take the definition back out of any runtime you rented."*

*Status: **implemented** (Phase 1 Anthropic + Phase 2 Bedrock harness), offline-tested,
unreleased/under review. This document is kept as the design record; the shipped feature
is documented for users in [import.md](import.md). The feasibility claims below are backed
by offline experiments in [`experiments/import-roundtrip/`](../experiments/import-roundtrip/)
run against the real SDKs (anthropic 0.107.1, boto3 1.43.24) — see Evidence — and are now
realised in `importer.py` / `folder_writer.py` / `anthropic_source.py` / `harness_source.py`,
covered by `tests/test_importer.py`, `tests/test_import_roundtrip.py`, `tests/test_import_source.py`,
and `tests/test_cli_import.py`.*

---

## TL;DR — is it doable?

| Source | Verdict | Why |
|---|---|---|
| **Anthropic Managed Agents** | ✅ **Fully doable** — Phase 1 | The read API returns the *entire* agent definition, and skill **content is downloadable**. A round-trip back through the real parser+planner is proven offline. |
| **Bedrock AgentCore Harness** | ✅ **Doable** — Phase 2 | `GetHarness` returns the full config (model, system prompt, tools, MCP, skill S3 URIs); skill bytes come from S3. Needs one reverse model-map (a pure dict inversion). |
| **Bedrock AgentCore Runtime** | ⚠️ **Out of scope (clean boundary)** | The agent's semantics (system prompt, tools, subagents) are **baked into an opaque ARM64 container image**. `GetAgentRuntime` returns only metadata + a `containerUri`. Not reconstructable from the API — only from agentlift's *own* local build context. |

**So: yes, and the migration loop closes** for the two config-readable runtimes
(Anthropic ⇄ Bedrock-harness). The Runtime boundary is honest and mirrors the
existing `/invocations` trace caveat already documented for deploy — opaque
container in, opaque container out.

---

## Evidence (experiments actually run)

All offline, against the installed SDKs. Reproduce with the scripts in
`experiments/import-roundtrip/`.

**1. The read APIs exist and are rich enough.**

- Anthropic (`anthropic==0.107.1`): `client.beta.agents` exposes
  `list`, `retrieve`, `versions`; `client.beta.skills` exposes
  `list`, `retrieve`, `versions` — and **`versions.download` returns a
  `BinaryAPIResponse`** (the skill bundle). `BetaManagedAgentsAgent` (the
  `retrieve` shape) carries every field we need:
  `name, system, description, model{model}, tools[], mcp_servers[], skills[],
  multiagent{agents[]}, version`.
- Bedrock (`boto3==1.43.24`, `bedrock-agentcore-control`): `list_harnesses` /
  `get_harness` and `list_agent_runtimes` / `get_agent_runtime` all exist.
  `GetHarness` returns a full `harness` structure: `model.bedrockModelConfig.modelId`,
  `systemPrompt[].text`, `tools[]` (`remoteMcp{url,headers}`, `agentCoreBrowser`,
  `agentCoreCodeInterpreter`, `inlineFunction`), `skills[]` (`s3.uri`/`git`/`path`),
  `allowedTools`, `environmentVariables`. `GetAgentRuntime` returns only
  `agentRuntimeArtifact.containerConfiguration.containerUri` for the definition —
  **opaque** (this is the boundary above).

**2. The mapping round-trips through the real pipeline.**
`experiments/import-roundtrip/` mocks two Anthropic `retrieve` responses (a
coordinator `lead` + a roster `bug-finder` with `bash:ask`, two custom skills,
and a URL MCP server with a tool filter), runs a prototype importer that inverts
`planner._build_tools`, then feeds the emitted folder to the **real**
`parser.parse_project` + `planner.build_plan`:

```
$ python experiments/import-roundtrip/test_roundtrip.py
ROUND-TRIP PASS   2 agents, 2 skills, deployable=True
```

The reconstructed `bug-finder/agent.md` comes back with `tools: [read, glob, grep,
bash:ask]`, `skills: [bug-report, cite-sources]`, `mcp: [docs]`, and the planner
re-emits the coordinator roster as `@agent:bug-finder`. **The contract holds in
both directions.**

---

## What maps, field by field

Import is the **inverse of the planner**, so the rules live next to it conceptually.

### Anthropic `BetaManagedAgentsAgent` → `.managed-agents/<name>/`

| Wire field | Folder target | Inversion note |
|---|---|---|
| `name` | frontmatter `name` / dir name | — |
| `system` | `agent.md` body | strip the planner's inlined `# Reference material` block back into `knowledge/`? **No** — knowledge inlining is lossy (one-way); import leaves it in the body and flags it. |
| `description` | frontmatter `description` | — |
| `model.model` | frontmatter `model` | native Claude id, passes through |
| `tools[agent_toolset_20260401]` | frontmatter `tools` | `default_config.enabled==true & no configs` → omit `tools` ("all builtins", `None`); else list each config, appending `:ask`/`:allow` from `permission_policy.type` (inverts `_tool_config`/`_POLICY_TYPE`). |
| `tools[mcp_toolset]` | `mcp.json` `allowedTools` | per-server tool filter + policy suffixes |
| `tools[custom_tool]` | — | **not representable** → diagnostic `import.custom_tool_dropped` |
| `mcp_servers[]` (URL defs) | `mcp.json` `{type:url,url}` | inline auth is provider-side only → never written; flag if the runtime had env-resolved headers |
| `skills[type=custom]` | `skills/<name>/…` | `skills.versions.download` → unzip; bundle keys already carry the `<name>/` prefix |
| `skills[type=anthropic]` | — | first-party skill, no content to fetch → reference-only diagnostic |
| `multiagent.agents[]` | frontmatter `subagents` | resolve roster **ids → names** via the account listing |

### Bedrock `GetHarness.harness` → `.managed-agents/<name>/`

| Wire field | Folder target | Inversion note |
|---|---|---|
| `model.bedrockModelConfig.modelId` | frontmatter `model` | **reverse model-map**: strip `<prefix>.anthropic.` then invert `_CLAUDE_SLUG_ALIASES` (e.g. `eu.anthropic.claude-haiku-4-5-20251001-v1:0` → `claude-haiku-4-5`). Pure dict inversion, offline-testable. Non-Claude modelId passes through verbatim. |
| `systemPrompt[].text` | `agent.md` body | join text blocks |
| `tools[remoteMcp]` | `mcp.json` url+filter | `headers` keys → env-var-name diagnostic (values are provider-side) |
| `tools[agentCoreBrowser]` | `tools: [web_fetch, web_search]` | mirrors the forward `agentcore_browser` mapping (audit-`degraded`) |
| `tools[agentCoreCodeInterpreter]` | `tools: [bash, …]` | the sandbox primitives |
| `tools[inlineFunction]` | — | diagnostic, like `custom_tool` |
| `skills[].s3.uri` | `skills/<name>/…` | `s3:GetObject` over the prefix |
| `allowedTools` | per-tool filter | — |

---

## Architecture — mirror the existing pipeline, reversed

Keep the repo's discipline: **a pure core + a thin network edge, the mapping is
the contract, every rule gets an offline test.** Import is `deploy` run backwards:

```
live IDs ──fetch──▶ ImportModel ──emit──▶ .managed-agents/ folder
         (network)            (pure)
   *_source.py            importer.py        folder_writer.py
   (mirrors *_target)   (mirrors planner)   (the new inverse of parser)
```

New modules (names mirror their forward twins):

| New file | Role | Pure? | Mirrors |
|---|---|---|---|
| `import_model.py` | provider-neutral `ImportedProject`/`ImportedAgent` (close to `AgentSpec` but carries raw provider ids + an `ImportDiagnostics`) | ✅ | `model.py` |
| `importer.py` | `ProviderResponse → ImportedProject`: the inverse mapping rules (invert `_build_tools`, roster id→name, reverse model-map). **No network.** This is the offline-tested contract. | ✅ | `planner.py` |
| `folder_writer.py` | `ImportedProject → .managed-agents/ files` (writes `agent.md`, `mcp.json`, `skills/*`). Round-trip target: its output must re-parse to an equivalent `Project`. | ✅ (file IO only) | `parser.py` (inverse) |
| `anthropic_source.py` | `list/retrieve/skills.versions.download` → raw dicts | ❌ network | `anthropic_target.py` |
| `harness_source.py` | `list_harnesses/get_harness` + S3 skill fetch → raw dicts | ❌ network | `harness_target.py` |

CLI: a new `import` verb (argparse pattern at `cli.py:674+`):

```bash
agentlift import anthropic <out-dir> [--agent NAME ...] [--all]   # default: pick from a listing
agentlift import bedrock   <out-dir> --mode harness [--bedrock-region R]
agentlift import <src> <out> --dry-run    # print the ImportedProject + diagnostics, write nothing
```

`--dry-run` is the import analogue of `agentlift plan` — show the mapping (and every
`import.*` diagnostic) before any file is written. **Surface, don't swallow** still
holds: anything that can't round-trip becomes a visible `Diagnostic`, never a silent drop.

---

## Phase 1 — Anthropic import (the whole loop, one provider)

**Outcome:** `agentlift import anthropic ./out --all` reproduces a deployable
`.managed-agents/` folder from a live account, and `deploy` of that folder is a
no-op against the same account.

1. `import_model.py` + `importer.py` with the **Anthropic inversion rules** and a
   full offline test suite that feeds canned `retrieve` dicts (the experiment's
   fixtures, promoted to `tests/`) and asserts the emitted `ImportedProject`. *The
   mapping is the contract — assert it offline, like `tests/test_planner.py`.*
2. `folder_writer.py` + a **round-trip test**: `importer → folder_writer →
   parse_project → build_plan`, assert `deployable` and that re-planning yields the
   original wire `tools`/`skills`/`multiagent` shapes (the experiment, hardened).
3. `anthropic_source.py`: `agents.list` → pick → `agents.retrieve`; for each custom
   skill, `skills.versions.retrieve` (name/description/directory) + `versions.download`
   (bytes). Reuses the existing `ANTHROPIC_API_KEY` + beta-headers client setup.
4. `import` CLI verb + `--dry-run`. Handle the id→name roster resolution by listing
   first, then retrieving the closure.
5. One **live test** (`-m live`, gated, costs cents): deploy a fixture, import it
   back, assert the folder re-deploys as skip/no-op. Commit the receipt under
   `tests/live/receipts/` (anonymize per CLAUDE.md before any commit).

**Known one-way losses (documented, flagged, not silently dropped):**
- **Knowledge inlining** — the planner folds `knowledge/*.md` into `system`; on import
  it stays in the body (we can't reliably re-split). Diagnostic `import.knowledge_inlined`.
- **shared/ dedup** — a skill shared by N agents arrives N times from the API; the
  importer should detect identical content-hashes and hoist them to `shared/` (reusing
  `SkillSpec.content_hash`) so re-deploy dedups identically.
- **Custom tools / inline functions** — referenced, not reconstructable → diagnostic.
- **MCP inline auth** — provider-side; only the env-var *name* (if any) is recoverable.

## Phase 2 — Bedrock harness import + the migration story

**Outcome:** `agentlift import bedrock ./out --mode harness` reconstructs a
single-agent folder, and **`import anthropic … && deploy --target bedrock`**
(and the reverse) is a working migration.

1. `harness_source.py`: `list_harnesses`/`get_harness` + S3 skill fetch (`s3:GetObject`
   over `skills[].s3.uri`), reusing `harness_target.py`'s region/role env setup.
2. Extend `importer.py` with the **harness inversion rules** + the **reverse model-map**
   (offline-tested dict inversion of `_CLAUDE_SLUG_ALIASES` + `region_prefix` stripping).
   Map `agentCoreBrowser`→web tools, `agentCoreCodeInterpreter`→sandbox tools.
3. `import bedrock` CLI (`--mode harness` only; **`--mode runtime` returns the explicit
   boundary error** — opaque container, mirroring `_RUNTIME_LIVE_VERIFIED`'s honesty gate).
4. **Migration test (the payoff):** import the Phase-1 Anthropic fixture, `deploy
   --target bedrock --mode harness` (or `--build-only` offline), then import *that*
   harness back and assert the two folders are equivalent modulo documented losses.
   This is the cross-runtime round-trip — the feature's whole reason to exist.
5. Update `capabilities.py`? No — that rates platforms. Add an **import maturity row**
   to the README/`IMPLEMENTATION-STATUS.md` table instead (real-vs-roadmap stays honest).

---

## Risks & open questions

- **Skill bundle format.** `versions.download` returns `BinaryAPIResponse` — confirmed
  to *exist*; the exact archive layout (zip vs tar, prefix) must be **confirmed live
  before encoding** (the repo's standing rule), not guessed. Phase-1 step 3 is gated on
  one real download.
- **Bedrock harness preview churn.** `get_harness` field names/states are preview and
  have flapped before (`bedrock.harness.preview`). Keep `harness_source.py` tolerant
  (best-effort `.get`), exactly like `harness_target.py` already is.
- **id↔name stability.** Imported subagent rosters resolve ids→names; two agents with
  the same `name` across versions need disambiguation (use the listing + `version`).
- **Not a goal: importing the Runtime container.** Reconstructing an agent from an
  ARM64 image is out of scope and probably never worth it — the honest answer is "keep
  the source folder; the Runtime is a build artifact, not a source of truth."

## Why this is low-risk to land

The forward pipeline is already pure-core + thin-network with the mapping unit-tested
as the contract. Import slots into the *same* shape, reuses the *same* clients/auth,
and its correctness criterion is mechanical and offline: **importer → folder_writer →
parser → planner must reproduce the deploy plan.** The experiment already passes that
bar for the Anthropic path. Phase 1 is mostly wiring an existing, proven mapping to the
existing, proven read APIs.
