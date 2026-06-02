"""skylift — deploy local Claude agents to Anthropic's Managed Agents cloud.

One folder, one command. The agent definition you already run locally
(CLAUDE.md / agent.md + skills + MCP config) becomes a hosted, run-by-ID
managed agent — skills uploaded, tools allowlisted, subagents wired.

Public surface:
    from skylift import parse_project, build_plan, Deployer
"""

__version__ = "0.1.0"

from .model import Project, AgentSpec, SkillSpec, McpServerSpec  # noqa: F401
from .parser import parse_project  # noqa: F401
from .planner import build_plan, DeployPlan  # noqa: F401

__all__ = [
    "__version__",
    "Project",
    "AgentSpec",
    "SkillSpec",
    "McpServerSpec",
    "parse_project",
    "build_plan",
    "DeployPlan",
]
