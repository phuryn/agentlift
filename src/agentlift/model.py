"""In-memory representation of a local agent project.

The parser produces a `Project`; the planner consumes it. Nothing here touches
the network — these are plain data holders so the whole front half of the tool
is pure and unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Map from local Claude Code tool names (what you write in CLAUDE.md / frontmatter)
# to Anthropic Managed Agents built-in tool identifiers. Case-insensitive on input.
BUILTIN_TOOL_MAP: dict[str, str] = {
    "read": "read",
    "glob": "glob",
    "grep": "grep",
    "bash": "bash",
    "edit": "edit",
    "multiedit": "edit",
    "write": "write",
    "webfetch": "web_fetch",
    "web_fetch": "web_fetch",
    "websearch": "web_search",
    "web_search": "web_search",
}

# The full set of managed built-in tools (used when an agent enables "all").
ALL_MANAGED_BUILTINS: tuple[str, ...] = (
    "bash", "edit", "read", "write", "glob", "grep", "web_fetch", "web_search",
)


@dataclass
class SkillSpec:
    """A skill discovered on disk (a directory containing SKILL.md)."""
    name: str                      # directory name, e.g. "summarize"
    source_dir: str                # absolute path to the skill directory
    files: list[tuple[str, str]]   # (arcname, abs_path); arcname keeps the "<name>/..." prefix
    content_hash: str              # stable hash over (arcname, bytes) of all files
    description: Optional[str] = None  # SKILL.md frontmatter description (validated pre-flight)
    shared: bool = False           # came from .managed-agents/shared/skills

    @property
    def display_title(self) -> str:
        return self.name


@dataclass
class McpServerSpec:
    """An MCP server entry from a local .mcp.json / mcp.json file."""
    name: str
    transport: str                 # "url" (deployable) or "stdio" (NOT deployable to managed)
    url: Optional[str] = None      # for url transport
    command: Optional[str] = None  # for stdio transport (informational only)
    args: list[str] = field(default_factory=list)
    allowed_tools: Optional[list[str]] = None  # bare tool names; None = all tools from the server
    tool_policies: dict[str, str] = field(default_factory=dict)  # tool name -> "ask" | "allow"
    shared: bool = False
    has_inline_auth: bool = False  # env/headers present in source (can't ride along on managed URL servers)
    # Raw inline auth captured verbatim from the source config. The Anthropic path
    # ignores these (it only reads has_inline_auth); the Google path maps header
    # values to engine env vars by name (the literal value is never inlined into
    # generated code). Empty dicts keep this fully backward compatible.
    headers: dict[str, str] = field(default_factory=dict)  # HTTP headers for url transport
    env: dict[str, str] = field(default_factory=dict)      # env passed to a stdio command


@dataclass
class AgentSpec:
    """One agent: its system prompt plus the resources it is wired to."""
    name: str
    system: str
    model: str
    description: Optional[str] = None
    # built-in tool allowlist as MANAGED names; None means "all builtins enabled"
    builtin_tools: Optional[list[str]] = None
    # managed tool name -> "ask" | "allow" (gate a tool behind approval); default allow
    builtin_tool_policies: dict[str, str] = field(default_factory=dict)
    skills: list[SkillSpec] = field(default_factory=list)
    mcp_servers: list[McpServerSpec] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)   # names of roster agents (makes this a coordinator)
    knowledge_files: list[tuple[str, str]] = field(default_factory=list)  # (relpath, abs_path)
    knowledge_mode: str = "inline"   # inline | skip
    source_dir: str = ""


@dataclass
class Project:
    """A parsed project: the agents plus where they came from."""
    root: str
    agents: list[AgentSpec]
    layout: str  # ".managed-agents" | "single"

    def agent(self, name: str) -> Optional[AgentSpec]:
        for a in self.agents:
            if a.name == name:
                return a
        return None
