# skylift

**Deploy the Claude agents you already run locally to Anthropic's Managed Agents cloud. One folder, one command.**

Anthropic's Managed Agents runs your whole agent loop in the cloud — you call it by ID over REST. But there is no console: to attach a skill, wire an MCP server, or restrict a tool, you write API calls by hand. So most people never move their local agents up.

skylift closes that gap. Point it at the agent folder you already use with Claude Code / the Agent SDK (`CLAUDE.md` + skills + `.mcp.json`). It uploads your skills, maps your tool allowlist, wires your remote MCP servers, and creates the hosted agent — deterministically, idempotently, no new format to learn.

```bash
pip install -e .                 # PyPI release pending; install from a clone for now
skylift deploy ./my-agent
skylift run my-agent --task "what changed in the API this week?"
```

> The agent definition is the portable asset. The runtime is a deploy choice.
> **Own the definition. Rent the runtime.**

---

## Why this exists

`POST /v1/agents` is powerful and completely UI-less. A real agent has a system prompt, a few skills (each a directory of files uploaded via a separate multipart endpoint), an MCP server or two, a tool allowlist, maybe a subagent roster. Wiring all of that by hand — and keeping it in sync as the agent changes — is the reason "just deploy it to the cloud" rarely happens.

skylift makes the deploy unit the same folder you develop against locally. Nothing new to learn; the thing you already have *is* the input.

## Install

```bash
git clone https://github.com/phuryn/managed-agents-API && cd managed-agents-API
pip install -e .                      # PyPI release pending
export ANTHROPIC_API_KEY=sk-ant-...   # needs Managed Agents beta access
```

## The folder is the agent

skylift reads a convention you may already use. Minimal single-agent project:

```
my-agent/
└── .agents/
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

It also reads the **Claude Code embedded-agents layout** unchanged — `.claude/agents/<name>/CLAUDE.md` + `.mcp.json` + `.claude/skills/...`. If you built local agents that way, they deploy as-is. See [docs/convention.md](docs/convention.md).

## See exactly what will happen (no network)

```console
$ skylift plan ./examples/quickstart

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
$ skylift deploy ./examples/quickstart -y
Uploading skills...
  skill 'receipt-stamp': uploaded skill_01Ph... (used by knowledge-agent)
Creating agents...
  agent 'knowledge-agent': created agent_019L... v1
Lockfile written: ./examples/quickstart/.skylift-lock.json

$ skylift run knowledge-agent --project ./examples/quickstart \
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

## What skylift maps

| Local definition | → Managed Agents | Notes |
|---|---|---|
| `CLAUDE.md` / `agent.md` body | `system` prompt | frontmatter sets model, tools, etc. |
| `tools: [read, glob, ...]` | `agent_toolset_20260401` configs | mapped to `read/glob/grep/bash/edit/write/web_fetch/web_search`; unmappable tools dropped with a warning |
| `skills/<name>/SKILL.md` (+ files) | uploaded skill → `{type:"custom", skill_id}` | content-addressed; identical skills upload **once** and are shared across agents |
| `.mcp.json` **remote** server | `mcp_servers:[{type:"url"}]` + per-tool `mcp_toolset` allowlist | per-server `allowedTools` becomes the tool allowlist |
| `.mcp.json` **stdio** server (`npx ...`) | ✗ rejected | managed agents need a remote URL; clear error (or `--skip-unsupported`) |
| `knowledge/*.md` | folded into `system` | managed agents have no persistent local FS; see [limitations](docs/limitations.md) |
| `subagents: [a, b]` | `multiagent` coordinator | roster deployed first; depth-limit-1 enforced |

Full table and the exact wire format: [docs/anthropic-mapping.md](docs/anthropic-mapping.md).

## Multi-agent, shared resources, subagents

```
.agents/
├── shared/
│   ├── skills/cite-sources/SKILL.md     # one skill, many agents (uploaded once)
│   └── mcp.json                         # one MCP server, many agents
├── bug-finder/agent.md                  # skills: [bug-report, shared/cite-sources]
├── researcher/agent.md                  # mcp: [shared/docs]
└── lead/agent.md                        # subagents: [bug-finder, researcher]  → coordinator
```

```console
$ skylift plan ./examples/team
Skills to upload: 2
  - cite-sources  (417213e5, 1 file(s))  used by: bug-finder, researcher
  - bug-report    (6d58998e, 1 file(s))  used by: bug-finder
Agents to create: 3
  - bug-finder  [claude-haiku-4-5]   tools: builtins:read/glob/grep/bash
  - researcher  [claude-haiku-4-5]   tools: builtins:read/web_search, mcp:docs
  - lead        [claude-haiku-4-5]   (coordinator -> @agent:bug-finder, @agent:researcher)
Deployable: yes
```

## How it works

`parse → plan → apply → run`.

- **parse** — read the folder into an in-memory project. Pure file IO.
- **plan** — produce a deterministic list of API operations with symbolic refs (`@skill:hash`, `@agent:name`), skill dedup, validation, and diagnostics. No network. This is what `skylift plan` prints and what the offline tests assert.
- **apply** — execute the plan: upload skills (deduped), create agents in dependency order, write a `.skylift-lock.json` mapping local definitions → remote IDs.
- **run** — invoke a deployed agent by ID (or run the same folder locally with `--local`).

The lockfile makes re-deploys idempotent: an unchanged skill is not re-uploaded, an unchanged agent is not re-created (verified in `tests/test_idempotency.py`, no network). Details: [docs/how-it-works.md](docs/how-it-works.md).

## Commands

```
skylift validate <path>              parse + plan, report problems (exit 1 on errors)
skylift plan     <path> [--json]     show the deploy plan (dry run, no network)
skylift deploy   <path> [--prune]    upload skills + create agents; write lockfile
skylift run <agent> --task "..."     invoke a deployed agent (--local for the same folder locally)
skylift list     <path>              what's currently deployed (from the lockfile)
skylift destroy  <path>              archive every agent in the lockfile
skylift bench <agent> --task "..."   managed vs local: latency / cost / pass
```

## Tests

```bash
pytest -m "not live"     # deterministic translation + idempotency — no API key, runs in CI
pytest -m live           # deploy to the real API, run, LLM-grade the output (needs ANTHROPIC_API_KEY)
```

Offline tests pin the translation (tool mapping, skill dedup, stdio rejection, coordinator ordering, idempotency). Live tests deploy to Anthropic and confirm the uploaded skill actually fires in the cloud, graded by an LLM. CI runs the offline suite on every push and the live suite when an `ANTHROPIC_API_KEY` secret is present. See [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Limitations (read these)

- **Remote MCP only.** Managed agents connect to URL MCP servers; local `stdio` servers (`npx ...`) can't be deployed. Host them behind HTTPS first.
- **No inline MCP auth.** A managed URL MCP server carries no credentials in this API shape. The server must be public or authenticate itself.
- **Knowledge files are inlined** into the system prompt (no persistent local FS in the managed sandbox). Large reference sets should become a skill bundle.
- **Anthropic only, for now.** The planner is provider-agnostic; OpenAI / Google targets are on the roadmap.

Each of these is surfaced as a `skylift plan` diagnostic, not a silent surprise. More: [docs/limitations.md](docs/limitations.md).

## Roadmap

- Authenticated remote MCP via the Vaults API
- `skylift diff` against the live account
- Additional deploy targets (OpenAI Agent Builder, Google Managed Agents) behind the same convention
- A skill-bundle mode for large `knowledge/` sets

## License

MIT — see [LICENSE](LICENSE). Built on the [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python).
