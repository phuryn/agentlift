# Experiment: subagents == agent-as-tool composition (confirmed)

The capability map (`src/agentlift/capabilities.py`) classifies "subagents -> coordinator"
as `native` (Anthropic) or `emulated` (Google, OpenAI) -- never `unsupported`. This
experiment is the receipt: the coordinator-delegates-to-a-sub-agent pattern runs on both
Google and OpenAI today.

Both scripts read keys from the environment and run the orchestration **locally** -- which
is the point: model calls go to the provider, but the routing between agents runs in your
process. No deploy required.

## OpenAI (Agents SDK, `researcher.as_tool()`)

```
$ python openai_agent_as_tool.py
QUESTION: How tall is the Eiffel Tower in meters, and what year was it completed?

FINAL ANSWER:
  The Eiffel Tower is 324 meters tall (including antennas) and was completed in 1889.

--- delegation trace (proof the coordinator called the sub-agent as a tool) ---
  function_call  ask_researcher
  ToolCallOutputItem
```

The coordinator called the `researcher` sub-agent **as a tool** (`ask_researcher`), got the
result, and synthesized the answer. Agent-as-tool == subagent. The loop ran in-process.

## Google (ADK, `sub_agents=[...]`)

```
$ GOOGLE_GENAI_USE_VERTEXAI=FALSE python google_adk_subagents.py
QUESTION: How tall is the Eiffel Tower in meters, and what year was it completed?
--- event trace ---
  [delegation] coordinator -> transfer_to_agent({'agent_name': 'researcher'})
  [researcher] The Eiffel Tower is 330 meters tall.
```

The coordinator delegated to the `researcher` sub-agent via ADK's native
`transfer_to_agent`. Deployed to Vertex Agent Engine, this delegation runs server-side as
one reasoningEngine.

## Conclusion

A subagent roster is a **universal** capability, not a per-provider lottery:

| Provider | Mechanism | Where the delegation loop runs | Tier |
|---|---|---|---|
| Anthropic | native `multiagent` coordinator | provider runtime (hosted) | `native` |
| Google | ADK `sub_agents` -> Agent Engine | provider runtime (hosted, one resource) | `emulated` |
| OpenAI | `agent.as_tool()` / Agent Builder nodes | your orchestrator (self-hosted) | `emulated` |

The difference is only **where the delegation loop runs** -- the provider's runtime, or
yours. That is the precise meaning of "own the definition, rent the runtime": the roster
is portable; the orchestration is either rented (Anthropic, Google) or self-hosted (OpenAI).

*Confirmed 2026-06-03 with openai-agents + google-adk 2.1.0.*
