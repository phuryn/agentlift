# Limitations

agentlift surfaces every one of these as a `agentlift plan` diagnostic — never a silent
surprise. They reflect what the Managed Agents API accepts today, not gaps in the
translation.

## Remote MCP only

Managed agents connect to **URL** MCP servers. Local `stdio` servers — the common
`{"command": "npx", "args": [...]}` form — cannot be deployed. Host the server
behind an HTTPS endpoint and give it a `url`.

- default: hard error (`mcp.stdio_unsupported`), deploy blocked
- `--skip-unsupported`: warning, the server is dropped, the rest deploys

## No inline MCP auth (Anthropic)

On **Anthropic**, the managed URL MCP server shape is `{type, name, url}` — there is no
field for headers or env. Any `env`/`headers` in your local `mcp.json` is **not forwarded**
(`mcp.auth_dropped` warning). The server must be public or authenticate itself.
Authenticated remote MCP via the Vaults API is on the roadmap.

On **Google** (`--target google`) and **AWS Bedrock** (`--target bedrock`) inline auth *is*
carried: the header value is resolved from the deployer's local environment at deploy time
and passed as a runtime `env_var` (named `AGENTLIFT_MCP_<SERVER>_<HEADER>`); the generated
source only ever references `os.environ.get(...)`, so the secret never lands in source,
plan, or lockfile. See
[deploy-google.md](deploy-google.md#mcp-auth-headers-secrets-stay-out-of-the-source) and
[deploy-bedrock.md](deploy-bedrock.md#mcp-auth-headers-secrets-stay-out-of-the-source).

## Per-tool MCP filtering on a direct Bedrock Harness attachment (`bedrock.mcp.tool_filter_unenforced`)

This applies **only to the direct `remote_mcp` attachment** path — a raw URL MCP server
attached straight to the AgentCore Harness (preview) or Runtime. On that path a *restrictive*
`allowedTools` suppresses MCP tool surfacing entirely in the preview, so agentlift emits an
**empty** allowlist (surfacing all MCP tools) and diagnoses the dropped narrowing
(`bedrock.mcp.tool_filter_unenforced`) rather than silently shipping a filter that wouldn't
take effect. Per-tool MCP scoping is therefore not enforced on a direct attachment today.

This is **not** a limit of AgentCore as a whole. For **Gateway**-fronted MCP, tool-level
scoping is enforced server-side at the **Gateway/Policy** layer: per AWS's documentation,
AgentCore Gateway aggregates MCP tools and Gateway Policy can filter `tools/list` and
intercept tool calls per principal — so the right place for a per-tool allowlist is the
Gateway/Policy, not the direct attachment. **agentlift has not live-verified the Gateway
path** — that behavior is AWS-documented, not an agentlift-proven claim.

Workaround: for fine-grained per-tool MCP scoping today, front the server with AgentCore
Gateway + Policy (server-side enforcement), or keep the allowlist on a provider that
enforces it (e.g. Anthropic / Google `tool_filter`).

## Runtime `/invocations` traces top-level tool calls only (`bedrock.runtime.nested_trace_boundary`)

On the AWS Bedrock AgentCore **Runtime** (custom container, multi-agent — now a live hosted
deploy), `InvokeAgentRuntime` returns the container's app-defined JSON body, **not** a
tool-event stream. agentlift's generated handler surfaces the coordinator's **top-level**
tool-call trace (read from `AgentResult.metrics.tool_metrics`, fail-open). So **subagent
delegation and root-level skill/MCP calls are objectively traced**, but a specialist's
**internal (nested) skill/MCP calls do not cross the `/invocations` boundary** — they are
wired and output-corroborated, not independently exercised at the boundary.

This is the Runtime analogue of the Google caveat where inner grounding/url-context metadata
does not cross the `AgentTool` → Agent-Engine `stream_query` boundary (see
[tested-platforms.md](tested-platforms.md)). It is **not a correctness bug** — the nested
calls still happen inside the container; it is a trace-*visibility* boundary at the invoke
edge.

Forward note: surfacing nested traces past the boundary is on the roadmap (e.g. via Strands
streaming / callback capture inside the container).

## Knowledge files are inlined

A managed agent runs in a fresh sandbox with no persistent copy of your repo, so
`knowledge/*.md` can't be read off disk the way they are locally. agentlift folds
them into the system prompt under a `# Reference material` section. This works well
for a handful of small reference files; it is size-guarded (overflow warns and
stops). For large reference sets, package them as a **skill** instead — skill
bundles can carry many files, and skill-bundle mode is on the roadmap.

Set `knowledge: skip` in frontmatter to opt out entirely.

## Idempotency is per-spec

A change to an agent's resolved request (system prompt, tools, skills, roster)
produces a new managed agent on the next deploy; the lockfile is updated. Pass
`--prune` to archive the superseded version. Skills are content-addressed, so a
skill edit uploads a new skill; the old one is left in place (skills are cheap and
may be shared).

## Skill descriptions can't contain angle-bracket tags

The API rejects a `SKILL.md` frontmatter `description` that contains XML-like tags
(e.g. `Replace <placeholder> with ...`). agentlift catches this at plan time
(`skill.xml_in_description`) so you get a clear error instead of a deploy-time 400.
The skill *body* may contain anything; only the description is validated.

## Targets differ by handoff

The parser and planner are provider-agnostic — the plan is just "operations" — so the
same folder reaches every target. What differs is how far each runtime takes it:

| Target | Status | Limits |
|---|---|---|
| Anthropic Managed Agents | Live deploy | Reference target; most complete mapping (skills, MCP, `:ask`, coordinator). |
| AWS Bedrock AgentCore | **Harness** = ✅ live single-agent deploy · **Runtime** = ✅ live multi-agent hosted deploy | `auto` (default) picks the least-powerful primitive that preserves semantics, **never a silent downgrade**: a **single agent** → **Harness**, a **multi-agent team** (subagents) → **Runtime**. **`--mode harness`** deploys a config-only managed single agent live (IAM + execution role, no container, minutes), **6/6 verified by a committed Nova receipt** (agent + base-session sandbox + remote MCP + S3-loaded skill + `agentcore_browser`). It maps **Claude natively**, the base-session **shell + file_operations** (sandbox), `web_fetch`→`agentcore_browser` (session-based), **URL MCP** (`remote_mcp` tool; tools surface as `<server>_<tool>`), and **skills** (uploaded to `$AGENTLIFT_BEDROCK_S3_BUCKET`, attached via `skills[].s3.uri`; exec role needs `s3:ListBucket`+`s3:GetObject`). Honest notes: the AgentCore feature is in AWS **public preview**; Claude inference runs but is Gate-A-gated (Nova receipt); per-tool MCP `allowedTools` narrowing isn't enforced on the **direct** attachment in preview (front with Gateway+Policy for server-side scoping). It cannot represent **subagents** (single-agent primitive) — a team routes to the Runtime. **`--mode runtime`** is a **live hosted multi-agent deploy**: agentlift builds the ARM64 **AgentCore Runtime** container → ECR (`buildx --platform linux/arm64 --push`) → `CreateAgentRuntime` (PUBLIC, HTTP, IAM-only) → poll READY → `.agentlift-bedrock.json` → `InvokeAgentRuntime`. It maps **skills** (embedded + `Skill.from_file`/`AgentSkills`), **URL MCP** (`MCPClient` + `tool_filter`, inline auth → AgentCore `env_vars`), subagents (agents-as-tools), and **Claude natively** (regional inference profile, **no remap**). **Live-verified on Nova** (us-east-1): create + agent + subagent **delegation** EXERCISED (a team receipt) + root-level remote MCP EXERCISED (a smoke receipt); nested specialist skill/MCP are wired + text-corroborated (the `/invocations` body isn't a tool-event stream — see above). `--build-only` still emits just the artifact. Gates: the **Anthropic use-case form** (Gate A — eventually consistent, Claude inference; Nova sidesteps it) and **IAM creds + execution role** (+ **ECR** for the Runtime, Gate B). `:ask` is `unsupported` on both (no interactive approval channel). The Strands multi-agent composition is [proven live](tested-platforms.md#amazon-bedrock-agentcore-runtime--harness) on Nova (local **and** hosted); stdio MCP refused. See [deploy-bedrock.md](deploy-bedrock.md). |
| Google Vertex AI Agent Engine | Live deploy, preview | Deployed as a real `reasoningEngine`; maps **skills** (embedded + ADK `load_skill_from_dir`), **URL MCP** (`McpToolset` + `tool_filter`, inline auth → Agent Engine `env_vars`), and the **built-in web tools** (`web_search` → Google Search grounding, `web_fetch` → URL Context, each a wrapped tool-agent), idempotent via a spec hash. **All six portability dimensions exercised live** (delegation, both MCP servers, both skills — see the [coverage matrix](tested-platforms.md#live-coverage-matrix--receipt-evidence-not-a-capability-ranking)); the web tools were separately exercised live (both tool-agents fired on a deployed engine). Not mapped, each with a workaround in [deploy-google.md](deploy-google.md#two-known-gaps-and-how-to-work-around-them): the built-in **sandbox** tools (`bash/files/glob-grep` — Vertex's sandbox is Python/JS only; expose equivalents via a URL MCP server, an explicit non-goal to emulate in-engine) and `:ask`/per-tool approval (gate it client-side); stdio MCP refused; Claude models map to Gemini (Claude-on-Vertex is an [offline-verified spike](../experiments/claude-on-vertex/), not shipped). |
| OpenAI Agents SDK | Export / self-host | Subagents via agent-as-tool; the delegation loop runs in your app — no hosted-deploy target. |

## Cost numbers are estimates

`cost` is a token estimate at published tier rates plus Anthropic cache pricing, the
same methodology as the managed-agents-experiment repo. Treat it as directional, not
billing-accurate. Managed runtimes auto-cache a large context; the local runner's
context is lean — so the two arms are not a controlled cost comparison, just a
real-world readout of each.
