# Examples

Two runnable projects. Both work with `skylift plan` offline (no key); deploying
and running needs `ANTHROPIC_API_KEY` with Managed Agents beta access.

## `quickstart/` — one agent, one skill, knowledge

The smallest real agent: a system prompt with an identity sign-off, a tool
allowlist (`read/glob/grep`), one uploaded skill (`receipt-stamp`), and a knowledge
file folded into the prompt.

```bash
skylift plan   ./quickstart
skylift deploy ./quickstart -y
skylift run knowledge-agent --project ./quickstart --task "What is a North Star metric?"
skylift run knowledge-agent --project ./quickstart --task "What is RICE?" --local   # same folder, locally
skylift destroy ./quickstart -y
```

The `RECEIPT:` line in the output is the uploaded skill firing inside the runtime.

## `team/` — multi-agent, shared resources, a coordinator

Shows everything skylift wires:

- **shared skill** `cite-sources` used by two agents → uploaded once
- **shared MCP server** `docs` (remote URL) with a tool allowlist
- a **coordinator** (`lead`) with a `subagents` roster (`bug-finder`, `researcher`)

```bash
skylift plan ./team        # see the dedup, the coordinator ordering, the MCP wiring
skylift deploy ./team -y
skylift run lead --project ./team --task "Find the bug in utils.py and explain RICE."
```

> The `docs` MCP server points at `https://example.com/mcp` (a placeholder). Swap in
> a real remote MCP URL before relying on its tools; the agent deploys and runs
> fine without ever calling it.
