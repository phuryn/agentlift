"""The deploy lockfile: maps local definitions to the remote IDs they became.

Committed alongside the project so re-deploys are idempotent — an unchanged skill
is not re-uploaded, an unchanged agent is not re-created. Keyed by content hash
(skills) and a canonical spec hash (agents).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

LOCKFILE_NAME = ".agentlift-lock.json"


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass
class Lockfile:
    path: str
    skills: dict[str, dict] = field(default_factory=dict)   # content_hash -> {skill_id, display_title}
    agents: dict[str, dict] = field(default_factory=dict)   # name -> {agent_id, version, spec_hash, skill_ids}

    @classmethod
    def load(cls, project_root: str) -> "Lockfile":
        path = os.path.join(project_root, LOCKFILE_NAME)
        if os.path.isfile(path):
            try:
                data = json.load(open(path, "r", encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}
        return cls(
            path=path,
            skills=data.get("skills", {}) or {},
            agents=data.get("agents", {}) or {},
        )

    def save(self) -> None:
        data = {"version": 1, "skills": self.skills, "agents": self.agents}
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")

    # skills
    def skill_id(self, content_hash: str) -> Optional[str]:
        rec = self.skills.get(content_hash)
        return rec.get("skill_id") if rec else None

    def set_skill(self, content_hash: str, skill_id: str, display_title: str) -> None:
        self.skills[content_hash] = {"skill_id": skill_id, "display_title": display_title}

    # agents
    def agent(self, name: str) -> Optional[dict]:
        return self.agents.get(name)

    def set_agent(self, name: str, agent_id: str, version: Any, spec_hash: str, skill_ids: list[str]) -> None:
        self.agents[name] = {
            "agent_id": agent_id, "version": version,
            "spec_hash": spec_hash, "skill_ids": skill_ids,
        }
