"""Experiment (live): deploy a Claude-on-Vertex Agent Engine and query it.

This is the LIVE half of the Claude-on-Vertex spike -- the step that would graduate
the path from "offline-verified" to "shippable". It deploys ONE reasoningEngine whose
root agent runs a Claude model on Vertex (optionally with a Gemini-backed web sub-agent),
queries it, and tears it down. It is intentionally NOT wired into agentlift's deploy
path: agentlift will not emit a Claude-on-Vertex deploy until this runs green and the
wire behavior is encoded (the repo's "confirm live before encoding" rule).

PRECONDITIONS (all on you, the deployer):
  * Claude models ENABLED in your Vertex AI Model Garden (a one-time console action;
    Claude on Vertex is an enable-per-project, region-gated partner model).
  * A region that offers the chosen Claude model (e.g. us-east5 -- NOT every region
    serves every Claude model; check Model Garden for availability).
  * A billable GCP project + a Cloud Storage staging bucket + ADC, exactly like a
    normal Google deploy (see docs/deploy-google.md).

Environment:
    GOOGLE_CLOUD_PROJECT=your-project
    GOOGLE_CLOUD_LOCATION=us-east5            # a region where your Claude model is served
    GOOGLE_GENAI_USE_VERTEXAI=TRUE
    AGENTLIFT_GCP_STAGING_BUCKET=gs://your-bucket
    CLAUDE_VERTEX_MODEL=claude-sonnet-4-5@20250929   # the @versioned Vertex Claude id
    # ADC from `gcloud auth application-default login`, or GOOGLE_APPLICATION_CREDENTIALS

Run:
    pip install "google-cloud-aiplatform[adk,agent_engines]" "google-adk>=1.34.3"
    python experiments/claude-on-vertex/claude_on_vertex_deploy.py          # deploy + query
    python experiments/claude-on-vertex/claude_on_vertex_deploy.py teardown # delete it

This script defines the agents inline (it does NOT import agentlift), so it is a clean
probe of the platform, not a test of the compiler. Nothing here is committed with real
identifiers -- everything is read from the environment.
"""
from __future__ import annotations

import os
import sys

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude-on-vertex-state.txt")


def _require(*names: str) -> dict[str, str]:
    vals = {n: os.environ.get(n) for n in names}
    missing = [n for n, v in vals.items() if not v]
    if missing:
        raise SystemExit(
            "missing env var(s): " + ", ".join(missing) + "\nSee the module docstring / "
            "docs/deploy-google.md. Claude must also be enabled in your Vertex Model Garden."
        )
    return vals  # type: ignore[return-value]


def _build_app(claude_model: str):
    """A Claude-on-Vertex root agent carrying a Gemini-backed web sub-agent (AgentTool).

    Mirrors the shape agentlift's google_codegen would emit: parent on Claude, the web
    tool-agent pinned to Gemini (Search grounding / URL Context are Gemini built-ins).
    """
    from google.adk.agents import LlmAgent
    from google.adk.tools.agent_tool import AgentTool
    from google.adk.tools.google_search_tool import GoogleSearchTool
    from vertexai.preview.reasoning_engines import AdkApp

    web_search = LlmAgent(
        name="lead_web_search",
        model="gemini-2.5-flash",
        description="Search the web with Google Search and return grounded findings.",
        instruction="Search and return a concise, cited answer.",
        tools=[GoogleSearchTool()],
    )
    lead = LlmAgent(
        name="lead",
        model=claude_model,
        description="A Claude-on-Vertex coordinator.",
        instruction=(
            "You run on Claude via Vertex AI. Answer concisely. If asked for fresh facts, "
            "use the web search tool. Begin every reply with the literal token CLAUDEVTX so "
            "the experiment can confirm which brain answered."
        ),
        tools=[AgentTool(agent=web_search, propagate_grounding_metadata=True)],
    )
    return AdkApp(agent=lead, enable_tracing=False)


def deploy() -> None:
    import vertexai
    from vertexai import agent_engines

    env = _require("GOOGLE_CLOUD_PROJECT", "AGENTLIFT_GCP_STAGING_BUCKET", "CLAUDE_VERTEX_MODEL")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-east5")
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")

    vertexai.init(
        project=env["GOOGLE_CLOUD_PROJECT"],
        location=location,
        staging_bucket=env["AGENTLIFT_GCP_STAGING_BUCKET"],
    )
    print(f"deploying Claude-on-Vertex engine: model={env['CLAUDE_VERTEX_MODEL']} region={location}")
    remote = agent_engines.create(
        agent_engine=_build_app(env["CLAUDE_VERTEX_MODEL"]),
        requirements=["google-cloud-aiplatform[adk,agent_engines]", "google-adk>=1.34.3"],
    )
    with open(STATE, "w", encoding="utf-8") as fh:
        fh.write(remote.resource_name)
    print(f"deployed: {remote.resource_name}")

    print("\nquerying ('Begin with CLAUDEVTX. One sentence: what model are you?') ...")
    answered = False
    for event in remote.stream_query(
        user_id="u1",
        message="Begin with CLAUDEVTX. In one sentence, what model family are you?",
    ):
        for part in (event.get("content", {}) or {}).get("parts", []) or []:
            text = part.get("text")
            if text:
                answered = True
                print("  ", text.strip()[:300])
    if not answered:
        print("  (no text parts streamed -- inspect the engine logs)")
    print("\nTeardown when done:  python experiments/claude-on-vertex/claude_on_vertex_deploy.py teardown")


def teardown() -> None:
    import vertexai
    from vertexai import agent_engines

    if not os.path.exists(STATE):
        raise SystemExit("no state file; nothing to tear down (or delete the engine in the console).")
    resource_name = open(STATE, encoding="utf-8").read().strip()
    env = _require("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-east5")
    vertexai.init(project=env["GOOGLE_CLOUD_PROJECT"], location=location)
    print(f"deleting {resource_name} ...")
    agent_engines.get(resource_name).delete(force=True)
    os.remove(STATE)
    print("deleted.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "deploy"
    if cmd == "teardown":
        teardown()
    elif cmd == "deploy":
        deploy()
    else:
        raise SystemExit(f"usage: {sys.argv[0]} [deploy|teardown]")
