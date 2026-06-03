"""Idempotency state for the Google Vertex AI Agent Engine target.

Mirrors ``lockfile.py`` but for the single ``reasoningEngine`` a Google deploy
produces. Committed alongside the project as ``.agentlift-google.json`` so a
re-deploy is a no-op when nothing changed, an ``update()`` (keeping the resource
id) when the spec changed, and a ``create()`` when there is no engine yet or the
target project/location moved.

The decision is a *pure* function of (recorded lock, plan spec hash, target
coordinates) -- no network -- so ``agentlift plan --target google`` can show
exactly what a deploy would do. Backward compatible: older lock files that only
carried ``{reasoning_engine, project, location}`` load fine (their absent
``spec_hash`` simply forces an ``update`` on the next deploy).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

GOOGLE_LOCKFILE_NAME = ".agentlift-google.json"


@dataclass
class GoogleAction:
    action: str   # "create" | "update" | "skip"
    reason: str


@dataclass
class GoogleLock:
    path: str
    reasoning_engine: Optional[str] = None
    project: Optional[str] = None
    location: Optional[str] = None
    spec_hash: Optional[str] = None
    display_name: Optional[str] = None
    deploy_model: Optional[str] = None

    @classmethod
    def load(cls, project_root: str) -> "GoogleLock":
        path = os.path.join(project_root, GOOGLE_LOCKFILE_NAME)
        data: dict = {}
        if os.path.isfile(path):
            try:
                data = json.load(open(path, "r", encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                data = {}
        return cls(
            path=path,
            reasoning_engine=data.get("reasoning_engine"),
            project=data.get("project"),
            location=data.get("location"),
            spec_hash=data.get("spec_hash"),
            display_name=data.get("display_name"),
            deploy_model=data.get("deploy_model"),
        )

    def record(
        self, *, reasoning_engine: str, project: str, location: str,
        spec_hash: str, display_name: str, deploy_model: str,
    ) -> None:
        self.reasoning_engine = reasoning_engine
        self.project = project
        self.location = location
        self.spec_hash = spec_hash
        self.display_name = display_name
        self.deploy_model = deploy_model

    def save(self) -> None:
        data = {
            "version": 1,
            "reasoning_engine": self.reasoning_engine,
            "project": self.project,
            "location": self.location,
            "spec_hash": self.spec_hash,
            "display_name": self.display_name,
            "deploy_model": self.deploy_model,
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")


def decide_action(
    lock: GoogleLock, spec_hash: str, *, gcp_project: str, location: str,
) -> GoogleAction:
    """What a deploy of ``spec_hash`` to ``gcp_project``/``location`` would do."""
    if not lock.reasoning_engine:
        return GoogleAction("create", "no engine recorded yet")
    if lock.project != gcp_project or lock.location != location:
        return GoogleAction(
            "create",
            f"target {gcp_project}/{location} differs from the recorded engine's "
            f"{lock.project}/{lock.location}; creating a new one",
        )
    if lock.spec_hash and lock.spec_hash == spec_hash:
        return GoogleAction("skip", "spec unchanged; the deployed engine is up to date")
    why = "spec changed" if lock.spec_hash else "no spec hash recorded (older lock)"
    return GoogleAction("update", f"{why}; updating the engine in place (keeps its id)")
