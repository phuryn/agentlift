"""Execute a `DeployPlan` against the Anthropic Managed Agents API.

This is the only module that touches the network. It resolves the plan's
symbolic refs (`@skill:...`, `@agent:...`) to real IDs, uploads skills (deduped
via the lockfile), creates agents in dependency order, and records the result so
the next deploy is idempotent.

Confirmed wire format (probed live against the API 2026-06-02):
  - skills:  client.beta.skills.create(display_title, files=[("<name>/SKILL.md", bytes, ct)], betas=[...])  -> .id
  - agents:  client.beta.agents.create(name, model, system, tools, skills, mcp_servers, multiagent, betas) -> .id/.version
  - betas:   managed-agents-2026-04-01 (+ skills-2025-10-02 when referencing custom skills)
"""
from __future__ import annotations

import copy
import mimetypes
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .lockfile import Lockfile, canonical_hash
from .planner import DeployPlan

BETAS = ["managed-agents-2026-04-01", "skills-2025-10-02"]


def _content_type(arcname: str) -> str:
    if arcname.lower().endswith(".md"):
        return "text/markdown"
    guessed, _ = mimetypes.guess_type(arcname)
    return guessed or "text/plain"


def resolve_request(request: dict, skill_ids: dict[str, str], agent_ids: dict[str, str]) -> dict:
    """Replace the plan's symbolic refs with real IDs. Raises KeyError if a ref
    has no mapping (used by both apply and diff). skill refs: skills[].skill_ref
    -> skill_id; roster refs: multiagent.agents[] (@agent:name) -> agent_id."""
    req = copy.deepcopy(request)
    if "skills" in req:
        req["skills"] = [{"type": "custom", "skill_id": skill_ids[s["skill_ref"]]} for s in req["skills"]]
    if req.get("multiagent"):
        roster = [agent_ids[r] for r in req["multiagent"]["agents"]]
        req["multiagent"] = {"type": req["multiagent"]["type"], "agents": roster}
    return req


@dataclass
class DeployResult:
    skill_ids: dict[str, str] = field(default_factory=dict)     # @skill:hash8 -> skill_id
    agent_ids: dict[str, str] = field(default_factory=dict)     # @agent:name  -> agent_id
    agent_versions: dict[str, Any] = field(default_factory=dict)
    uploaded_skills: list[str] = field(default_factory=list)    # display titles actually uploaded
    reused_skills: list[str] = field(default_factory=list)
    created_agents: list[str] = field(default_factory=list)
    reused_agents: list[str] = field(default_factory=list)


class Deployer:
    def __init__(self, client, project_root: str, betas: Optional[list[str]] = None):
        self.client = client
        self.lock = Lockfile.load(project_root)
        self.betas = betas or BETAS

    # -- skills ----------------------------------------------------------- #
    @staticmethod
    def _upload_title(display_title: str, content_hash: str) -> str:
        """Content-addressed title. Skill display_titles must be globally unique
        per account (the API 400s on reuse), so we suffix the content hash. Two
        identical skills always resolve to the same title -> natural dedup, even
        across machines without a shared lockfile."""
        return f"{display_title}-{content_hash[:8]}"

    def _find_existing_skill(self, upload_title: str) -> Optional[str]:
        try:
            for s in self.client.beta.skills.list(source="custom", betas=self.betas):
                if getattr(s, "display_title", None) == upload_title:
                    return s.id
        except Exception:  # pragma: no cover - network/listing optional
            return None
        return None

    def _upload_skills(self, plan: DeployPlan, result: DeployResult, log: Callable[[str], None]) -> None:
        for up in plan.skill_uploads:
            existing = self.lock.skill_id(up.content_hash)
            if existing:
                result.skill_ids[up.ref] = existing
                result.reused_skills.append(up.display_title)
                log(f"  skill '{up.display_title}': reuse {existing}")
                continue
            upload_title = self._upload_title(up.display_title, up.content_hash)
            remote = self._find_existing_skill(upload_title)
            if remote:
                self.lock.set_skill(up.content_hash, remote, up.display_title)
                result.skill_ids[up.ref] = remote
                result.reused_skills.append(up.display_title)
                log(f"  skill '{up.display_title}': found existing {remote}")
                continue
            files = []
            for arcname, abs_path in up.files:
                with open(abs_path, "rb") as fh:
                    files.append((arcname, fh.read(), _content_type(arcname)))
            resp = self.client.beta.skills.create(
                display_title=upload_title, files=files, betas=self.betas,
            )
            sid = getattr(resp, "id", None) or getattr(resp, "skill_id", None)
            self.lock.set_skill(up.content_hash, sid, up.display_title)
            result.skill_ids[up.ref] = sid
            result.uploaded_skills.append(up.display_title)
            log(f"  skill '{up.display_title}': uploaded {sid} (used by {', '.join(up.used_by)})")

    # -- agents ----------------------------------------------------------- #
    def _create_agents(self, plan: DeployPlan, result: DeployResult, prune: bool, log: Callable[[str], None]) -> None:
        for ac in plan.agent_creates:
            req = resolve_request(ac.request, result.skill_ids, result.agent_ids)
            spec_hash = canonical_hash(req)
            prev = self.lock.agent(ac.name)
            if prev and prev.get("spec_hash") == spec_hash:
                result.agent_ids[ac.ref] = prev["agent_id"]
                result.agent_versions[ac.name] = prev.get("version")
                result.reused_agents.append(ac.name)
                log(f"  agent '{ac.name}': unchanged, reuse {prev['agent_id']}")
                continue
            agent = self.client.beta.agents.create(betas=self.betas, **req)
            result.agent_ids[ac.ref] = agent.id
            result.agent_versions[ac.name] = agent.version
            result.created_agents.append(ac.name)
            log(f"  agent '{ac.name}': created {agent.id} v{agent.version}"
                + (" (coordinator)" if ac.is_coordinator else ""))
            if prune and prev and prev.get("agent_id") and prev["agent_id"] != agent.id:
                try:
                    self.client.beta.agents.archive(prev["agent_id"], betas=self.betas)
                    log(f"    pruned old {prev['agent_id']}")
                except Exception as e:  # pragma: no cover - network
                    log(f"    prune failed for {prev['agent_id']}: {e}")
            skill_ids = [s["skill_id"] for s in req.get("skills", [])]
            self.lock.set_agent(ac.name, agent.id, agent.version, spec_hash, skill_ids)

    # -- public ----------------------------------------------------------- #
    def apply(self, plan: DeployPlan, prune: bool = False, log: Optional[Callable[[str], None]] = None) -> DeployResult:
        if not plan.deployable:
            raise ValueError("plan has errors; not deployable. Run `skylift plan` to see them.")
        log = log or (lambda *_: None)
        result = DeployResult()
        log("Uploading skills...")
        self._upload_skills(plan, result, log)
        log("Creating agents...")
        self._create_agents(plan, result, prune, log)
        self.lock.save()
        log(f"Lockfile written: {self.lock.path}")
        return result

    def destroy(self, log: Optional[Callable[[str], None]] = None) -> list[str]:
        """Archive every agent recorded in the lockfile."""
        log = log or (lambda *_: None)
        archived = []
        for name, rec in list(self.lock.agents.items()):
            aid = rec.get("agent_id")
            if not aid:
                continue
            try:
                self.client.beta.agents.archive(aid, betas=self.betas)
                archived.append(name)
                log(f"  archived '{name}' ({aid})")
            except Exception as e:  # pragma: no cover - network
                log(f"  archive failed for '{name}' ({aid}): {e}")
        self.lock.agents = {}
        self.lock.save()
        return archived
