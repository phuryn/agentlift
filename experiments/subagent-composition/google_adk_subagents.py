"""Experiment: Google ADK 'sub_agents' == subagent composition (confirmed 2026-06-03).

A root coordinator agent delegates to a 'researcher' sub-agent. Runs LOCALLY via the
Gemini API -- no Vertex AI / GCP project / Agent Engine deploy required. The same
delegation runs server-side as one reasoningEngine when deployed to Agent Engine, which
is why the capability map classifies Google subagents as `emulated` (one resource).

Run:
    pip install google-adk
    export GOOGLE_API_KEY=...            # a Gemini API key from https://aistudio.google.com
    export GOOGLE_GENAI_USE_VERTEXAI=FALSE
    python experiments/subagent-composition/google_adk_subagents.py

Keys are read from the environment; none are hard-coded. See RESULTS.md for output.
"""
import asyncio
import os

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

researcher = LlmAgent(
    name="researcher",
    model="gemini-2.5-flash",
    description="Answers one factual research question with a specific number when possible.",
    instruction="Answer ONE factual question in a single tight sentence, with a specific number when possible.",
)

coordinator = LlmAgent(
    name="coordinator",
    model="gemini-2.5-flash",
    instruction=(
        "You are a coordinator. You do not answer factual questions yourself; "
        "delegate them to the `researcher` sub-agent, then synthesize the final answer."
    ),
    sub_agents=[researcher],
)


async def main():
    q = "How tall is the Eiffel Tower in meters, and what year was it completed?"
    runner = InMemoryRunner(agent=coordinator, app_name="adk_test")
    session = await runner.session_service.create_session(app_name="adk_test", user_id="u1")
    msg = types.Content(role="user", parts=[types.Part(text=q)])
    print("QUESTION:", q, "\n--- event trace ---")
    async for event in runner.run_async(user_id="u1", session_id=session.id, new_message=msg):
        if not (event.content and event.content.parts):
            continue
        for p in event.content.parts:
            fc = getattr(p, "function_call", None)
            if fc:
                print(f"  [delegation] {event.author} -> {fc.name}({dict(fc.args or {})})")
            elif getattr(p, "text", None):
                print(f"  [{event.author}] {p.text.strip()[:200]}")


if __name__ == "__main__":
    if not os.environ.get("GOOGLE_API_KEY"):
        raise SystemExit("Set GOOGLE_API_KEY (a Gemini API key from aistudio.google.com).")
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
    asyncio.run(main())
