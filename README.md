# agentlift

**Deploy the Claude agents you already run locally to Anthropic's Managed Agents cloud. One folder, one command.**

Anthropic's Managed Agents runs your whole agent loop in the cloud — you call it by ID over REST. But there is no console: to attach a skill, wire an MCP server, or restrict a tool, you write API calls by hand. So most people never move their local agents up.

agentlift closes that gap. Point it at the agent folder you already use with Claude Code / the Agent SDK (`CLAUDE.md` + skills + `.mcp.json`). It uploads your skills, maps your tool allowlist, wires your remote MCP servers, and creates the hosted agent — deterministically, idempotently, no new format to learn.

```bash
pip install -e .                 # PyPI release pending; install from a clone for now
agentlift deploy ./my-agent
agentlift run my-agent --task "what changed in the API this week?"
```

> The agent definition is the portable asset. The runtime is a deploy choice.
> **Own the definition. Rent the runtime.**

---

## Why this exists

`POST /v1/agents` is powerful and completely UI-less. A real agent has a system prompt, a few skills (each a directory of files uploaded via a separate multipart endpoint), an MCP server or two, a tool allowlist, maybe a subagent roster. Wiring all of that by hand — and keeping it in sync as the agent changes — is the reason "just deploy it to the cloud" rarely happens.

agentlift makes the deploy unit the same folder you develop against locally. Nothing new to learn; the thing you already have *is* the input.

## Install

```bash
git clone https://github.com/phuryn/agentlift && cd agentlift
pip install -e .                      # PyPI release pending
export ANTHROPIC_API_KEY=sk-ant-...   # needs Managed Agents beta access
```

## The folder is the agent

agentlift reads a convention you may already use. Minimal single-agent project:

```
my-agent/
└── .managed-agents/              # the deploy folder — everything here is a deploy target
    └── knowledge-agent/
        ├── agent.md              # YAML frontmatter + system prompt
        ├── skills/
        │   └── receipt-stamp/
        │       └── SKILL.md      # uploaded as a managed skill
        └── knowledge/
            └── pm-basics.md      # folded into the system prompt
```

`agent.md`:

```markdown
---
name: knowledge-agent
model: claude-haiku-4-5
tools: [read, glob, grep]      # built-in tool allowlist (omit = all)
---
You are the Knowledge Agent. Answer product questions concisely.
Always sign off as "Best, Knowledge Agent".
```

Why a dedicated `.managed-agents/` folder instead of reusing `.claude/agents/`? Because that's where Claude's **local** agents and native subagents live — and those aren't deploy targets. A separate folder keeps "ship to the cloud" cleanly apart from "runs on my machine." Already have an embedded agent folder (`.claude/agents/<name>/` with `CLAUDE.md` + `.mcp.json` + `.claude/skills/...`)? Point agentlift straight at it to deploy just that one — `CLAUDE.md`, `.mcp.json`, and `.claude/skills/` are all read for back-compat. See [docs/convention.md](docs/convention.md).

## See exactly what will happen (no network)

```console
$ agentlift plan ./examples/quickstart

Skills to upload: 1
  - receipt-stamp  (035823c8, 1 file(s))  used by: knowledge-agent

Agents to create: 1
  - knowledge-agent  [claude-haiku-4-5]
      tools: builtins:read/glob/grep
      skills: @skill:035823c8

Diagnostics:
  info [knowledge-agent]: inlined 1 knowledge file(s) into the system prompt

Deployable: yes
```

The plan is a pure function of the folder — same input, same plan. It is the dry-run, the diff, and the thing the tests assert against.

## Deploy and run

```console
$ agentlift deploy ./examples/quickstart -y
Uploading skills...
  skill 'receipt-stamp': uploaded skill_01Ph... (used by knowledge-agent)
Creating agents...
  agent 'knowledge-agent': created agent_019L... v1
Lockfile written: ./examples/quickstart/.agentlift-lock.json

$ agentlift run knowledge-agent --project ./examples/quickstart \
    --task "What is a North Star metric? One sentence."

[managed] knowledge-agent
  ------------------------------------------------------------
  A North Star metric is the single measure that best captures the value
  users get from your product.

  RECEIPT: metric captured

  Best, Knowledge Agent
  ------------------------------------------------------------
  latency 5.9s | in 4121 out 220 | ~$0.0044 | tool_used=False
```

The `RECEIPT:` line is the uploaded `SKILL.md` firing **inside the hosted runtime** — proof the skill rode along, not just the prompt.

## Proven, not asserted

`benchmarks/run_benchmark.py` deploys the quickstart agent and runs it on both runtimes. Real numbers ([benchmarks/results.md](benchmarks/results.md), `claude-haiku-4-5`, N=5):

| Arm | N | Pass% | Median latency | Avg cost |
|---|---|---|---|---|
| managed (cloud) | 5 | 100% | 5.9s | $0.0052 |
| local (your machine) | 5 | 100% | 2.3s | $0.0034 |

Pass = the uploaded skill fired **and** the answer was on-topic. Same folder, two runtimes, identical behavior. (The live deploy → cloud-run → skill-applied path is also pinned by `tests/live/`.)

## What agentlift maps

| Local definition | → Managed Agents | Notes |
|---|---|---|
| `CLAUDE.md` / `agent.md` body | `system` prompt | frontmatter sets model, tools, etc. |
| `tools: [read, glob, ...]` | `agent_toolset_20260401` configs | mapped to `read/glob/grep/bash/edit/write/web_fetch/web_search`; unmappable tools dropped with a warning |
| `tools: [bash:ask]` / `allowedTools: [x:ask]` | tool `permission_policy` | `:ask` gates a tool behind caller approval; `:allow` (default) auto-approves — the deployable form of a hook |
| `skills/<name>/SKILL.md` (+ files) | uploaded skill → `{type:"custom", skill_id}` | content-addressed; identical skills upload **once** and are shared across agents |
| `.mcp.json` **remote** server | `mcp_servers:[{type:"url"}]` + `mcp_toolset` | per-server `allowedTools` becomes the **specific-tool** allowlist (and supports `:ask`) |
| `.mcp.json` **stdio** server (`npx ...`) | ✗ rejected | managed agents need a remote URL; clear error (or `--skip-unsupported`) |
| `knowledge/*.md` | folded into `system` | managed agents have no persistent local FS; see [limitations](docs/limitations.md) |
| `subagents: [a, b]` | `multiagent` coordinator | roster deployed first; depth-limit-1 enforced |

Full table and the exact wire format: [docs/anthropic-mapping.md](docs/anthropic-mapping.md).

## Isolation: each agent gets only its folder

A deployed agent's context is exactly its own system prompt + its own (and `shared/`) skills + its own (and `shared/`) MCP servers + its inlined knowledge. The repo-root `CLAUDE.md`, a sibling agent's skills, and your machine's MCP servers **cannot leak in.**

This is the same isolation the local Agent SDK has to fight for — there the CLI walks up the directory tree and pulls in the repo-root `CLAUDE.md`, repo-root skills, and user-level MCP servers unless you set an explicit skills allowlist and `strictMcpConfig: true`. In the cloud there's no tree to walk: the agent only ever gets what agentlift uploads, and agentlift scopes uploads to the agent folder. You get isolation **by construction** — pinned by [`tests/test_isolation.py`](tests/test_isolation.py).

## Permissions and hooks

Claude Code hooks are local scripts, so they can't run in a cloud sandbox. Their main job — gating a tool behind approval — deploys as a per-tool **permission policy**. Append `:ask` to any built-in or specific MCP tool:

```yaml
tools: [read, glob, grep, bash:ask]                 # bash pauses for approval
```
```jsonc
{ "mcpServers": { "github": { "type": "url", "url": "https://…/mcp",
    "allowedTools": ["search_issues", "create_issue:ask"] } } }   // writes gated
```

At runtime an `:ask` call pauses the session (`requires_action`) for your app to approve or reject. Arbitrary hook *code* (custom block logic, PostToolUse capture) doesn't deploy — do path-guarding by not enabling the tool, and metadata capture from the session event stream. Details: [docs/deploying.md](docs/deploying.md#permissions-the-deployable-hook).

## Multi-agent, shared resources, subagents

```
.managed-agents/
├── shared/
│   ├── skills/cite-sources/SKILL.md     # one skill, many agents (uploaded once)
│   └── mcp.json                         # one MCP server, many agents
├── bug-finder/agent.md                  # skills: [bug-report, shared/cite-sources]
├── researcher/agent.md                  # mcp: [shared/docs]
└── lead/agent.md                        # subagents: [bug-finder, researcher]  → coordinator
```

Subagents are unambiguous here: `lead`'s roster references other agents **in the
same `.managed-agents/` folder**, so they're deploy targets too. Your local
Claude subagents in `.claude/agents/` are never swept in.

```console
$ agentlift plan ./examples/team
Skills to upload: 2
  - cite-sources  (417213e5, 1 file(s))  used by: bug-finder, researcher
  - bug-report    (6d58998e, 1 file(s))  used by: bug-finder
Agents to create: 3
  - bug-finder  [claude-haiku-4-5]   tools: builtins:read/glob/grep/bash(ask)
  - researcher  [claude-haiku-4-5]   tools: builtins:read/web_search, mcp:docs:all
  - lead        [claude-haiku-4-5]   (coordinator -> @agent:bug-finder, @agent:researcher)
Deployable: yes
```

`bash(ask)` is a per-tool permission: `bug-finder` declares `tools: [..., bash:ask]`,
so the hosted agent pauses for caller approval before each `bash` call.

## How it works

`parse → plan → apply → run`.

- **parse** — read the folder into an in-memory project. Pure file IO.
- **plan** — produce a deterministic list of API operations with symbolic refs (`@skill:hash`, `@agent:name`), skill dedup, validation, and diagnostics. No network. This is what `agentlift plan` prints and what the offline tests assert.
- **apply** — execute the plan: upload skills (deduped), create agents in dependency order, write a `.agentlift-lock.json` mapping local definitions → remote IDs.
- **run** — invoke a deployed agent by ID (or run the same folder locally with `--local`).

The lockfile makes re-deploys idempotent: an unchanged skill is not re-uploaded, an unchanged agent is not re-created (verified in `tests/test_idempotency.py`, no network). Details: [docs/how-it-works.md](docs/how-it-works.md).

## Deploying — three ways, all things you already know

Deploy is declarative: the folder is the desired state, and `deploy` makes the cloud match it. Trigger it however you already work.

1. **A command** (solo): `agentlift plan .` then `agentlift deploy . --yes`.
2. **Git push** (teams, recommended): commit `.managed-agents/`, copy [`examples/deploy-workflow/ci-deploy.yml`](examples/deploy-workflow/ci-deploy.yml) into `.github/workflows/`, add an `ANTHROPIC_API_KEY` secret. Every push that touches the folder validates, deploys (idempotent), and commits the updated lockfile. Review in PRs; roll back with `git revert`.
3. **From Claude Code**: drop [`examples/claude-code-skill/deploy-managed-agents/`](examples/claude-code-skill/) into `.claude/skills/` and just say *"deploy my managed agents."*

Full guide + trade-offs: [docs/deploying.md](docs/deploying.md).

```
agentlift validate <path>              parse + plan, report problems (exit 1 on errors)
agentlift plan     <path> [--json]     show the deploy plan (dry run, no network)
agentlift diff     <path> [--remote]   what a deploy would change vs the lockfile
agentlift deploy   <path> [--prune]    upload skills + create agents; write lockfile
agentlift run <agent> --task "..."     invoke a deployed agent (--local for the same folder locally)
agentlift list     <path>              what's currently deployed (from the lockfile)
agentlift destroy  <path>              archive every agent in the lockfile
agentlift bench <agent> --task "..."   managed vs local: latency / cost / pass
```

`agentlift diff` shows new / changed / unchanged / stale before you deploy:

```console
$ agentlift diff .
Skills:
  + house-style  (new)
  = cite-sources  (unchanged)
Agents:
  ~ researcher  (changed)
  = fact-checker  (unchanged)
Stale (in lockfile, not in folder — archived with --prune):
  - old-agent

2 change(s) pending.  Run: agentlift deploy <path>
```

## Where the deployed IDs live

`deploy` writes **`.agentlift-lock.json`** next to the path you deployed — a map from each local definition to the remote object it became (`skill_…`, `agent_…`, version, spec hash). **Commit it.** It's what makes re-deploys idempotent (unchanged skills/agents are skipped), makes `agentlift run lead …` resolve by name, and lets a teammate or CI reuse the same cloud objects instead of duplicating them. It holds only IDs/hashes/titles — no secrets. It's per Anthropic account; commit it when your team shares one. More: [docs/deploying.md](docs/deploying.md#where-the-ids-live-the-lockfile).

## Tests

```bash
pytest -m "not live"     # deterministic translation + idempotency — no API key, runs in CI
pytest -m live           # deploy to the real API, run, LLM-grade the output (needs ANTHROPIC_API_KEY)
```

Offline tests pin the translation (tool mapping, per-tool permissions, skill dedup, stdio rejection, coordinator ordering, context isolation, diff, idempotency). Live tests deploy to Anthropic and confirm the uploaded skill actually fires in the cloud, graded by an LLM. CI runs the offline suite on every push and the live suite when an `ANTHROPIC_API_KEY` secret is present ([.github/workflows/ci.yml](.github/workflows/ci.yml)). A separate on-demand [live-demo workflow](.github/workflows/live-demo.yml) deploys the team example to a real account, runs the benchmark, and tears everything down — so the deploy path is demonstrably live, not just asserted.

## Limitations (read these)

- **Remote MCP only.** Managed agents connect to URL MCP servers; local `stdio` servers (`npx ...`) can't be deployed. Host them behind HTTPS first.
- **No inline MCP auth.** A managed URL MCP server carries no credentials in this API shape. The server must be public or authenticate itself.
- **Knowledge files are inlined** into the system prompt (no persistent local FS in the managed sandbox). Large reference sets should become a skill bundle.
- **Anthropic only, for now.** The planner is provider-agnostic; OpenAI / Google targets are on the roadmap.

Each of these is surfaced as a `agentlift plan` diagnostic, not a silent surprise. More: [docs/limitations.md](docs/limitations.md).

## Documentation

Everything is here or one click away:

| Doc | What's in it |
|---|---|
| [docs/convention.md](docs/convention.md) | The `.managed-agents/` folder spec, frontmatter, skills, MCP, `:ask` permissions, native subagents |
| [docs/deploying.md](docs/deploying.md) | The three deploy paths, the lockfile / where IDs live, isolation, hooks |
| [docs/how-it-works.md](docs/how-it-works.md) | `parse → plan → apply → run`, determinism, idempotency, the confirmed wire format |
| [docs/anthropic-mapping.md](docs/anthropic-mapping.md) | Exact local → Managed Agents field mapping + API constraints |
| [docs/limitations.md](docs/limitations.md) | Honest constraints (stdio MCP, MCP auth, knowledge inlining, skill descriptions) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Architecture and dev setup |

### Examples ([examples/](examples/))

- [`quickstart/`](examples/quickstart/) — one agent, one skill, knowledge, a tool allowlist
- [`team/`](examples/team/) — multi-agent: coordinator + roster, a shared skill, a remote MCP server, a `bash:ask` permission
- [`in-a-project/`](examples/in-a-project/) — `.managed-agents/` embedded in a real project; proves isolation (repo `CLAUDE.md`, app code, and a local `.claude/agents/` subagent are never deployed) + a coordinator with two shared-skill subagents
- [`deploy-workflow/`](examples/deploy-workflow/) — the git-push-to-deploy GitHub Action
- [`claude-code-skill/`](examples/claude-code-skill/) — deploy from inside Claude Code

## Roadmap

- Authenticated remote MCP via the Vaults API
- `agentlift diff --remote` deeper drift detection (full account reconciliation)
- Additional deploy targets (OpenAI Agent Builder, Google Managed Agents) behind the same convention
- A skill-bundle mode for large `knowledge/` sets

## License

MIT — see [LICENSE](LICENSE). Built on the [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python).
