# `agentlift import` — read a live agent back into the folder

`import` is the inverse of `deploy`. Where `deploy` compiles a `.managed-agents/`
folder into a provider-native agent, `import` reads a **live** agent back out of a
runtime and writes the neutral folder. Once a runtime round-trips — out via `deploy`,
back via `import` — **migration between runtimes falls out for free**: import from one
provider, `deploy` to another.

> *Own the definition. Rent the runtime* — and take the definition back out of any
> runtime you rented.

```bash
# pull every agent from your Anthropic account into ./mine
agentlift import anthropic ./mine

# just one coordinator (its roster subagents come along automatically)
agentlift import anthropic ./mine --agent lead

# preview the mapping + diagnostics, write nothing (the import analogue of `plan`)
agentlift import anthropic ./mine --dry-run

# read a Bedrock AgentCore harness back (single agent, config-only)
agentlift import bedrock ./mine --harness-name support-agent --bedrock-region us-west-2

# migrate: Anthropic → Bedrock harness
agentlift import anthropic ./mine && agentlift deploy ./mine --target bedrock --mode harness
```

## What it does

```
live runtime ──fetch──▶ ImportedProject ──write──▶ .managed-agents/ ──(re-parse+plan)──▶ ✅ deployable
            (network)               (pure)                    (pure)
```

The pipeline mirrors `deploy` in reverse and keeps the same discipline — a pure core
with a thin network edge:

| Stage | Module | Network? |
|---|---|---|
| fetch raw provider shapes | `anthropic_source.py` / `harness_source.py` | ✅ |
| map → neutral `ImportedProject` (the **inverse of the planner**) | `importer.py` | ❌ |
| write the `.managed-agents/` folder (the **inverse of the parser**) | `folder_writer.py` | ❌ |

After writing, `import` **self-verifies**: it re-runs the real `parse` + `plan` over
what it wrote and prints `Round-trip OK` only if the folder is deployable again. The
mapping is asserted offline in [`tests/test_importer.py`](../tests/test_importer.py) and
the full round-trip (including subagent delegation with shared + custom skills and MCP
servers) in [`tests/test_import_roundtrip.py`](../tests/test_import_roundtrip.py).

## Anthropic — full import

Reads `client.beta.agents.list` / `agents.retrieve` and downloads each custom skill's
content via `client.beta.skills.versions.download`. Everything in the folder convention
comes back:

| Read from the agent | Written to the folder |
|---|---|
| `system`, `description`, `model` | `agent.md` frontmatter + body |
| built-in toolset (with `permission_policy`) | `tools: [read, bash:ask, …]` (or omitted = all builtins) |
| `mcp_servers[]` (URL) + the per-server tool filter | `mcp.json` (`url`, `allowedTools`) |
| custom `skills[]` | `skills/<name>/…` (content downloaded + unpacked) |
| `multiagent.agents[]` (a coordinator's roster) | `subagents: [a, b]` (ids resolved to names) |

**Selecting a subset.** `--agent NAME` (repeatable) imports just those agents; any roster
subagents a selected coordinator references are pulled into the closure automatically, so
delegation always re-imports intact.

**Shared resources.** A skill used identically by more than one agent (same content hash)
is hoisted to `shared/skills/`; an MCP server used identically by more than one agent is
hoisted to `shared/mcp.json`. Each agent then references it as `shared/<name>`. This
reproduces the dedup the planner would do, so a re-deploy uploads each shared skill once.

## Bedrock — harness only

`agentlift import bedrock --mode harness` reads `get_harness` and loads each skill bundle
from its `s3://…` URI. The regional inference profile is **reverse-mapped** to the folder
Claude id (`eu.anthropic.claude-haiku-4-5-20251001-v1:0` → `claude-haiku-4-5`);
`agentCoreBrowser` → `web_search`/`web_fetch`, `agentCoreCodeInterpreter` → the sandbox
builtins, and each `remote_mcp` tool → a URL MCP server. A harness is single-agent, so an
import never produces subagents.

**The Runtime is not importable.** A Bedrock AgentCore **Runtime** bakes its agent
definition (system prompt, tools, subagents) into an opaque ARM64 **container image** —
`GetAgentRuntime` returns only a `containerUri`, never the semantics. So
`import bedrock --mode runtime` refuses with that reason (keep the source folder for a
Runtime; the container is a build artifact, not a source of truth). This is the import
analogue of the deploy-time `/invocations` trace boundary.

## What can't round-trip (surfaced, never silently dropped)

Each becomes a visible `Diagnostic` (the same contract as `plan`):

- **Knowledge inlining is one-way.** `deploy` folds `knowledge/*.md` into the system
  prompt; `import` leaves it in the prompt body and flags `import.knowledge_*` — it can't
  reliably re-split inlined reference material back into files.
- **Custom tools / harness inline functions** are referenced, not reconstructable
  (`import.custom_tool_dropped` / `import.inline_function_dropped`).
- **MCP auth values are provider-side.** Only the header/env-var **name** is recovered
  (`import.mcp_auth_env`); the secret never leaves the runtime. Re-supply it at deploy time.
- **Anthropic first-party skills** (`type: anthropic`) are referenced by id with no
  downloadable content (`import.anthropic_skill_ref`).

## Migration

Because the folder is the neutral pivot, import + deploy is a migration:

```bash
agentlift import anthropic ./agent                                  # Anthropic → folder
agentlift deploy ./agent --target bedrock --mode harness            # folder → Bedrock harness
# …and the reverse
agentlift import bedrock ./agent --harness-name my-agent
agentlift deploy ./agent                                            # → Anthropic
```

Anything that doesn't survive a given hop shows up as a diagnostic before you deploy, so a
migration is never a silent lossy copy. See [limitations.md](limitations.md) for the full
list and [provider-matrix.md](provider-matrix.md) for which runtimes are importable.

## Credentials

Same as `deploy`: Anthropic import uses `ANTHROPIC_API_KEY` (env or `.env`); Bedrock import
uses your AWS credentials (boto3 default chain) and the harness's region. Import is
**read-only** — it never creates, updates, or archives anything in the account.
