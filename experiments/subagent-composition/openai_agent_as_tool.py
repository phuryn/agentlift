"""Experiment: OpenAI 'agent-as-tool' == subagent composition (confirmed 2026-06-03).

A coordinator agent delegates a research subtask to a 'researcher' sub-agent that is
exposed to it as a TOOL (researcher.as_tool(...)). The orchestration loop runs IN THIS
PROCESS via the Agents SDK -- the point: OpenAI runs each model call, but the routing
between agents runs in your app. This is why the capability map classifies OpenAI
subagents as `emulated` (loop in your orchestrator), not `unsupported`.

Run:
    pip install openai-agents
    export OPENAI_API_KEY=sk-...
    python experiments/subagent-composition/openai_agent_as_tool.py

Keys are read from the environment; none are hard-coded. See RESULTS.md for output.
"""
import asyncio
import os

from agents import Agent, Runner

researcher = Agent(
    name="researcher",
    model="gpt-5-mini",
    instructions="You answer ONE factual question in a single tight sentence, with a specific number when possible.",
)

coordinator = Agent(
    name="coordinator",
    model="gpt-5-mini",
    instructions=(
        "You are a coordinator. You do not answer factual questions yourself. "
        "For anything that needs a fact, call the `ask_researcher` tool with a precise question, "
        "then synthesize the final answer from what it returns."
    ),
    tools=[
        researcher.as_tool(
            tool_name="ask_researcher",
            tool_description="Ask the researcher sub-agent one precise factual question; returns one sentence.",
        )
    ],
)


async def main():
    q = "How tall is the Eiffel Tower in meters, and what year was it completed?"
    result = await Runner.run(coordinator, q)
    print("QUESTION:", q)
    print("\nFINAL ANSWER:\n ", result.final_output)
    print("\n--- delegation trace (proof the coordinator called the sub-agent as a tool) ---")
    for item in result.new_items:
        raw = getattr(item, "raw_item", None)
        kind = getattr(raw, "type", type(item).__name__)
        name = getattr(raw, "name", "") or ""
        if "tool" in str(kind).lower() or "ask_researcher" in name:
            print(f"  {kind}  {name}".rstrip())


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY first.")
    asyncio.run(main())
