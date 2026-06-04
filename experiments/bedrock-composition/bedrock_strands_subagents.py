"""Experiment: Strands 'agents as tools' == subagent composition on Claude-on-Bedrock
(confirmed 2026-06-04). Mirrors experiments/subagent-composition/google_adk_subagents.py.

A root `coordinator` agent (Claude on Bedrock) delegates to a `researcher` specialist
agent (wrapped as a tool) and calls a deterministic `@tool`. Runs LOCALLY via Bedrock
model inference -- no AgentCore Runtime / IAM creds / hosted deploy required. The same
composition runs server-side as ONE AgentCore Runtime when deployed, which is why the
(future) capability map will classify Bedrock subagents as `emulated` (one resource),
exactly like Google.

Why this is the right first proof (the "confirm live before encoding" step): the user's
Bedrock *bearer-token API key* (AWS_BEARER_TOKEN_BEDROCK) authenticates MODEL inference
only -- it cannot create a hosted runtime (that's control-plane: IAM + ECR + PassRole).
So, exactly like Google's local subagent experiment, this proves the composition with the
credential we actually have, before any codegen/target work assumes IAM.

Run:
    pip install "strands-agents>=1.42" "boto3>=1.40"
    set AWS_BEARER_TOKEN_BEDROCK=<key>     # Bedrock bearer token (model inference)
    set AWS_REGION=eu-north-1
    python experiments/bedrock-composition/bedrock_strands_subagents.py

Keys are read from the environment; none are hard-coded. See RESULTS.md for output.
"""
import os

from strands import Agent, tool
from strands.models import BedrockModel

# Claude is NATIVE on Bedrock -- no Gemini-style remap. This regional inference-profile id
# was live-verified answerable in eu-north-1 with the bearer token (2026-06-04).
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
REGION = os.environ.get("AWS_REGION", "eu-north-1")

# Objective trace: every entry is an actual python-side tool invocation the coordinator's
# Claude brain decided to make. This is the unforgeable signal (not answer text).
TRACE: list[str] = []


@tool
def population_lookup(city: str) -> str:
    """Return the population of a city from a fixed local table (deterministic tool)."""
    TRACE.append(f"deterministic-tool population_lookup(city={city!r})")
    table = {"paris": "2,102,650", "london": "8,866,180", "tokyo": "13,960,000"}
    return table.get(city.strip().lower(), "unknown")


def make_model() -> BedrockModel:
    # model_id is a BedrockConfig key absorbed by **model_config; region pins the profile.
    return BedrockModel(model_id=MODEL_ID, region_name=REGION, temperature=0)


@tool
def researcher(question: str) -> str:
    """Delegate ONE factual question to a specialist research sub-agent (agent-as-tool)."""
    TRACE.append(f"subagent researcher(question={question!r})")
    specialist = Agent(
        model=make_model(),
        name="researcher",
        system_prompt=(
            "Answer ONE factual question in a single tight sentence, "
            "with a specific number when possible."
        ),
    )
    return str(specialist(question))


def main() -> None:
    coordinator = Agent(
        model=make_model(),
        name="coordinator",
        system_prompt=(
            "You are a coordinator. You do NOT answer factual questions from your own "
            "knowledge. For any factual question, delegate to the `researcher` tool. For "
            "a city population, call the `population_lookup` tool. Then synthesize one "
            "final answer."
        ),
        tools=[researcher, population_lookup],
    )

    q = "What year was the Eiffel Tower completed, and what is the population of Paris?"
    print("MODEL:", MODEL_ID, "region:", REGION)
    print("QUESTION:", q, "\n--- running (live Bedrock inference) ---")
    result = coordinator(q)

    print("\n--- tool-call trace (objective: each is a real invocation) ---")
    for i, entry in enumerate(TRACE, 1):
        print(f"  {i}. {entry}")

    # Cross-check against the model's own toolUse blocks in the conversation history.
    tool_uses = [
        block["toolUse"]["name"]
        for msg in coordinator.messages
        for block in (msg.get("content") or [])
        if isinstance(block, dict) and "toolUse" in block
    ]
    print("  model-emitted toolUse blocks:", tool_uses)

    print("\n--- final coordinator answer ---")
    print(" ", str(result).strip()[:400])

    ok = (
        any("subagent researcher" in e for e in TRACE)
        and any("population_lookup" in e for e in TRACE)
    )
    print("\nOK: coordinator delegated to a sub-agent AND used a deterministic tool."
          if ok else "\nINCOMPLETE: expected both a sub-agent delegation and a tool call.")


if __name__ == "__main__":
    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        raise SystemExit(
            "Set AWS_BEARER_TOKEN_BEDROCK (Bedrock bearer token) and AWS_REGION=eu-north-1."
        )
    main()
