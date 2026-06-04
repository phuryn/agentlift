"""Experiment (offline): ADK resolves Claude-on-Vertex, and a Claude main agent
composes with Gemini-backed web sub-agents. Confirmed 2026-06-04 with google-adk 1.34.3.

This is the OFFLINE half of the Claude-on-Vertex spike. It proves the *design* is
mechanically sound without deploying anything or touching ADC / the network:

  1. ADK ships a Claude-on-Vertex model class and the registry resolves Claude model
     ids to it (backed by ``AsyncAnthropicVertex`` -- Claude served on Vertex AI).
  2. A Claude main agent can carry a Gemini-backed web tool-agent via ``AgentTool``.
     This is the mixed-model invariant: Google Search grounding and URL Context are
     *Gemini* built-ins, so the web sub-agent must stay Gemini even when its parent is
     Claude. agentlift's google_codegen encodes exactly this with its ``web_model()``
     helper (web sub-agents use ``web_model``, parents use ``vertex_model``).

What this does NOT prove: that Agent Engine will deploy and run a Claude-on-Vertex
engine end-to-end. That requires Claude enabled in your Vertex Model Garden, a
supported region, and a billable project -- see claude_on_vertex_deploy.py + RESULTS.md.

Run (no credentials, no project needed):
    pip install "google-cloud-aiplatform[adk,agent_engines]" "google-adk>=1.34.3"
    python experiments/claude-on-vertex/claude_on_vertex_construct.py
"""
from __future__ import annotations

# ADK resolves the bare Vertex Claude id (an @version suffix also works). Pick one your
# project has enabled for the deploy half; this matches claude_on_vertex_deploy.py's default.
CLAUDE_VERTEX_MODEL = "claude-sonnet-4-6"
GEMINI_WEB_MODEL = "gemini-2.5-flash"  # web grounding / URL Context are Gemini built-ins


def main() -> None:
    from google.adk.agents import LlmAgent
    from google.adk.models.registry import LLMRegistry
    from google.adk.tools.agent_tool import AgentTool
    from google.adk.tools.google_search_tool import GoogleSearchTool

    # 1) the registry resolves a Claude model id to ADK's Claude-on-Vertex class
    cls = LLMRegistry.resolve(CLAUDE_VERTEX_MODEL)
    print("registry:")
    print(f"  {CLAUDE_VERTEX_MODEL!r} -> {cls.__module__}.{cls.__name__}")
    print(f"  Claude.supported_models() = {cls.supported_models()}")

    # 2) build a Claude main agent that carries a Gemini-backed web sub-agent (AgentTool).
    #    This is the shape agentlift's codegen would emit for a Claude-on-Vertex parent.
    web_search = LlmAgent(
        name="lead_web_search",
        model=GEMINI_WEB_MODEL,
        description="Search the web with Google Search and return grounded findings.",
        instruction="Search and return a concise, cited answer.",
        tools=[GoogleSearchTool()],
    )
    lead = LlmAgent(
        name="lead",
        model=CLAUDE_VERTEX_MODEL,
        description="A Claude-on-Vertex coordinator.",
        instruction="Answer the user; use the web search tool when you need fresh facts.",
        tools=[AgentTool(agent=web_search, propagate_grounding_metadata=True)],
    )

    print("\nconstructed (offline, no ADC):")
    print(f"  parent  : {lead.name}  model={lead.model}  -> {LLMRegistry.resolve(str(lead.model)).__name__}")
    print(f"  web sub : {web_search.name}  model={web_search.model}  "
          f"-> {LLMRegistry.resolve(str(web_search.model)).__name__}")

    assert str(lead.model).startswith("claude"), "parent should be Claude-on-Vertex"
    assert str(web_search.model).startswith("gemini"), "web sub-agent must stay Gemini"
    print("\nOK: Claude main agent + Gemini-pinned web sub-agent compose. Mixed-model invariant holds.")


if __name__ == "__main__":
    main()
