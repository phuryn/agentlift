"""In-memory representation of an *imported* project (provider -> folder).

The mirror of `model.py` for the reverse pipeline. Where `AgentSpec`/`SkillSpec`
describe agents discovered *on disk* (skills are file paths), these describe
agents read back *from a runtime* (skills are in-memory bytes, freshly downloaded
from the provider). `folder_writer.py` materialises an `ImportedProject` to a
`.managed-agents/` tree; `parser.parse_project` must then re-parse that tree into
an equivalent `Project`. That round-trip is the import contract.

Everything here is a plain data holder — no network, no file IO.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .diagnostics import Diagnostics


@dataclass
class ImportedSkill:
    """A skill bundle downloaded from a runtime, held in memory."""
    name: str                          # directory name, e.g. "cite-sources"
    files: dict[str, bytes]            # arcname (keeps the "<name>/..." prefix) -> bytes
    description: Optional[str] = None
    content_hash: str = ""             # same hash the parser/planner compute, for dedup


@dataclass
class ImportedMcp:
    """A remote MCP server read back from a runtime (URL transport only)."""
    name: str
    url: Optional[str] = None
    transport: str = "url"
    allowed_tools: Optional[list[str]] = None       # None = all tools from the server
    tool_policies: dict[str, str] = field(default_factory=dict)  # tool -> "ask" | "allow"
    auth_env_names: list[str] = field(default_factory=list)      # header/env *names* only (values are provider-side)


@dataclass
class ImportedAgent:
    """One imported agent plus the resources private to it.

    Shared resources live on the `ImportedProject`; this agent references them by
    name through `skill_refs` / `mcp_refs` (the frontmatter values folder_writer
    emits, e.g. "bug-report" or "shared/cite-sources").
    """
    name: str
    system: str
    model: str                          # folder-facing model id (already reverse-mapped)
    description: Optional[str] = None
    builtin_tools: Optional[list[str]] = None       # local tool tokens; None = all builtins
    builtin_tool_policies: dict[str, str] = field(default_factory=dict)
    local_skills: list[ImportedSkill] = field(default_factory=list)
    local_mcp: list[ImportedMcp] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)   # frontmatter `skills:` values
    mcp_refs: list[str] = field(default_factory=list)      # frontmatter `mcp:` values
    subagents: list[str] = field(default_factory=list)     # roster agent names (coordinator)
    provider_id: str = ""               # the source runtime's agent id (provenance)
    raw_model: str = ""                 # the provider's original model id (pre reverse-map)


@dataclass
class ImportedProject:
    """The whole import: agents + the resources hoisted to `shared/`."""
    agents: list[ImportedAgent] = field(default_factory=list)
    shared_skills: list[ImportedSkill] = field(default_factory=list)
    shared_mcp: list[ImportedMcp] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    source: str = "anthropic"           # provenance tag: "anthropic" | "bedrock-harness"

    def agent(self, name: str) -> Optional[ImportedAgent]:
        for a in self.agents:
            if a.name == name:
                return a
        return None
