"""Live deploy to Google Vertex AI Agent Engine (the hosted runtime).

Builds ADK ``LlmAgent`` objects from the parsed folder (a coordinator over its
sub_agents) and deploys them as ONE ``reasoningEngine`` via ``agent_engines.create()``.
Reads the GCP project / location / staging bucket from the environment + ADC.

PREVIEW scope: Claude models in the folder are mapped to a Gemini model (Agent Engine
on a Gemini project); MCP servers, skills, and built-in tools are noted and skipped for
now (the audit reports those tiers). What this proves: a folder's coordinator + subagents
deploy to a live, hosted, multi-agent runtime addressable by resource name.

Requires: pip install "google-cloud-aiplatform[adk,agent_engines]" google-adk
Env: GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, AGENTLIFT_GCP_STAGING_BUCKET (gs://...),
     ADC (gcloud auth application-default login). See docs/deploy-google.md.
"""
from __future__ import annotations

import os
from typing import Callable, Optional


def _safe(name: str) -> str:
    """A valid ADK agent identifier (used as the transfer_to_agent function name)."""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name)


def build_adk_app(project, model: str, log: Callable[[str], None] = print):
    """Build the ADK app (root LlmAgent + sub_agents) from the parsed folder.

    Pure construction, no network -- used both by deploy and by a dry build check.
    """
    from google.adk.agents import LlmAgent
    from vertexai.preview import reasoning_engines

    if not project.agents:
        raise ValueError("no agents to deploy")

    def model_for(a) -> str:
        m = a.model or ""
        return model if m.startswith("claude") else m

    roster = [a for a in project.agents if not a.subagents]
    coords = [a for a in project.agents if a.subagents]

    skipped = []
    for a in project.agents:
        if a.mcp_servers:
            skipped.append(f"{a.name}: {len(a.mcp_servers)} MCP")
        if a.skills:
            skipped.append(f"{a.name}: {len(a.skills)} skill(s)")
    if skipped:
        log("  preview note - not mapped to Agent Engine yet: " + "; ".join(skipped))

    built = {}
    for a in roster:
        built[a.name] = LlmAgent(
            name=_safe(a.name), model=model_for(a),
            instruction=a.system, description=a.description or a.name,
        )
    for a in coords:
        subs = [built[s] for s in a.subagents if s in built]
        built[a.name] = LlmAgent(
            name=_safe(a.name), model=model_for(a),
            instruction=a.system, description=a.description or a.name,
            sub_agents=subs,
        )
    root_spec = coords[0] if coords else project.agents[0]
    log(f"  built ADK app: root '{root_spec.name}' + {len(built) - 1} subagent(s), model={model}")
    return reasoning_engines.AdkApp(agent=built[root_spec.name], enable_tracing=False), root_spec.name


def deploy_google(
    project,
    *,
    gcp_project: str,
    location: str = "us-central1",
    staging_bucket: str,
    model: str = "gemini-2.5-flash",
    display_name: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> str:
    import vertexai
    from vertexai import agent_engines

    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    vertexai.init(project=gcp_project, location=location, staging_bucket=staging_bucket)

    app, root_name = build_adk_app(project, model, log=log)
    log("  deploying to Agent Engine (container build, a few minutes)...")
    remote = agent_engines.create(
        agent_engine=app,
        display_name=display_name or f"agentlift-{_safe(root_name)}",
        requirements=["google-cloud-aiplatform[adk,agent_engines]"],
    )
    log(f"  deployed: {remote.resource_name}")
    return remote.resource_name
