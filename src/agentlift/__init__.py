"""agentlift — own your agent definition as a neutral folder; rent the runtime.

One folder (CLAUDE.md / agent.md + skills + MCP config + a subagent roster) is the
provider-neutral definition. agentlift is a compiler over it:

    audit   how it maps across providers (native / emulated / degraded / unsupported)
    export  compile it to a provider artifact (Anthropic YAML for `ant`, Google ADK,
            OpenAI Agents SDK)
    deploy  push it to a managed runtime via API (Anthropic + Google, both live)

Public surface:
    from agentlift import parse_project, build_plan, Deployer
    from agentlift import run_audit, export_anthropic_yaml, export_google_adk
"""

__version__ = "0.5.0"

from .audit import run_audit  # noqa: F401
from .capabilities import CAPABILITIES, FEATURES  # noqa: F401
from .export import export_anthropic_yaml, export_google_adk  # noqa: F401
from .model import AgentSpec, McpServerSpec, Project, SkillSpec  # noqa: F401
from .parser import parse_project  # noqa: F401
from .planner import DeployPlan, build_plan  # noqa: F401

__all__ = [
    "__version__",
    "Project",
    "AgentSpec",
    "SkillSpec",
    "McpServerSpec",
    "parse_project",
    "build_plan",
    "DeployPlan",
    "run_audit",
    "export_anthropic_yaml",
    "export_google_adk",
    "CAPABILITIES",
    "FEATURES",
]
