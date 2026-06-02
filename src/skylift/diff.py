"""`skylift diff` — what would a deploy change?

Compares the deterministic plan against the lockfile (offline): which skills are
new, which agents are new/changed/unchanged, and which lockfile entries are stale
(no longer in the folder, would be archived with --prune). An optional remote
check flags lockfile IDs that no longer exist on the account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .anthropic_target import resolve_request
from .lockfile import Lockfile, canonical_hash
from .planner import DeployPlan


@dataclass
class DiffResult:
    skills_new: list[str] = field(default_factory=list)
    skills_unchanged: list[str] = field(default_factory=list)
    agents_new: list[str] = field(default_factory=list)
    agents_changed: list[str] = field(default_factory=list)
    agents_unchanged: list[str] = field(default_factory=list)
    agents_stale: list[str] = field(default_factory=list)        # in lockfile, gone from folder
    remote_missing_agents: list[str] = field(default_factory=list)
    remote_missing_skills: list[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return (len(self.skills_new) + len(self.agents_new)
                + len(self.agents_changed) + len(self.agents_stale))


def compute_diff(plan: DeployPlan, lockfile: Lockfile) -> DiffResult:
    d = DiffResult()

    # skills, keyed by content hash
    skill_ids: dict[str, str] = {}   # @skill:ref -> skill_id (from lockfile)
    for up in plan.skill_uploads:
        sid = lockfile.skill_id(up.content_hash)
        if sid:
            skill_ids[up.ref] = sid
            d.skills_unchanged.append(up.display_title)
        else:
            d.skills_new.append(up.display_title)

    # known agent ids from the lockfile (for roster resolution)
    agent_ids = {f"@agent:{name}": rec["agent_id"]
                 for name, rec in lockfile.agents.items() if rec.get("agent_id")}

    plan_names = set()
    for ac in plan.agent_creates:
        plan_names.add(ac.name)
        rec = lockfile.agent(ac.name)
        if not rec:
            d.agents_new.append(ac.name)
            continue
        try:
            resolved = resolve_request(ac.request, skill_ids, agent_ids)
        except KeyError:
            d.agents_changed.append(ac.name)   # depends on a not-yet-deployed skill/agent
            continue
        if canonical_hash(resolved) == rec.get("spec_hash"):
            d.agents_unchanged.append(ac.name)
        else:
            d.agents_changed.append(ac.name)

    for name in lockfile.agents:
        if name not in plan_names:
            d.agents_stale.append(name)

    return d


def check_remote(lockfile: Lockfile, client, betas) -> tuple[list[str], list[str]]:
    """Best-effort: flag lockfile IDs that no longer exist on the account."""
    missing_agents, missing_skills = [], []
    for name, rec in lockfile.agents.items():
        aid = rec.get("agent_id")
        if not aid:
            continue
        try:
            client.beta.agents.retrieve(aid, betas=betas)
        except Exception:
            missing_agents.append(name)
    for chash, rec in lockfile.skills.items():
        sid = rec.get("skill_id")
        if not sid:
            continue
        try:
            client.beta.skills.retrieve(sid, betas=betas)
        except Exception:
            missing_skills.append(rec.get("display_title", sid))
    return missing_agents, missing_skills


def render_diff(d: DiffResult) -> str:
    lines: list[str] = []
    lines.append("Skills:")
    for s in d.skills_new:
        lines.append(f"  + {s}  (new)")
    for s in d.skills_unchanged:
        lines.append(f"  = {s}  (unchanged)")
    if not (d.skills_new or d.skills_unchanged):
        lines.append("  (none)")
    lines.append("Agents:")
    for a in d.agents_new:
        lines.append(f"  + {a}  (new)")
    for a in d.agents_changed:
        lines.append(f"  ~ {a}  (changed)")
    for a in d.agents_unchanged:
        lines.append(f"  = {a}  (unchanged)")
    if not (d.agents_new or d.agents_changed or d.agents_unchanged):
        lines.append("  (none)")
    if d.agents_stale:
        lines.append("Stale (in lockfile, not in folder — archived with --prune):")
        for a in d.agents_stale:
            lines.append(f"  - {a}")
    if d.remote_missing_agents or d.remote_missing_skills:
        lines.append("Missing on the account (lockfile references a deleted object):")
        for a in d.remote_missing_agents:
            lines.append(f"  ! agent {a}")
        for s in d.remote_missing_skills:
            lines.append(f"  ! skill {s}")
    lines.append("")
    if d.changes == 0:
        lines.append("In sync — deploy would make no changes.")
    else:
        lines.append(f"{d.changes} change(s) pending.  Run: skylift deploy <path>")
    return "\n".join(lines)
