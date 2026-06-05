"""Idempotency state for the Amazon Bedrock AgentCore *harness* target.

Mirrors ``bedrock_lock.py`` (the runtime lock) but for the single managed harness
a ``--mode harness`` deploy produces. The decision is a *pure* function of
(recorded lock, plan spec hash, target region) -- no network -- so ``agentlift
plan --target bedrock --mode harness`` (and a future deploy) can show exactly what
a deploy would do: a no-op when nothing changed, an ``UpdateHarness`` (keeping the
harness id/ARN) when the spec changed, and a ``CreateHarness`` when there is no
harness yet or the target region moved.

A separate lockfile (``.agentlift-harness.json``) from the runtime's
``.agentlift-bedrock.json`` so a folder can carry both a runtime and a harness
deploy without their identities colliding.

A region change forces a ``create``: the deployed model id is a *regional*
inference profile (``us.anthropic.*`` vs ``eu.anthropic.*``), so the same folder in
two regions is genuinely two harnesses. (The plan's ``spec_hash`` already differs by
region since the resolved profile prefix flows into it; the explicit region check
here makes the intent legible rather than relying on that coupling.)

**Provisional, like the rest of the harness path.** ``harness_target.deploy_harness``
*does* write ``.agentlift-harness.json`` -- the harness create runs live (config-only,
IAM-only) behind a loud preview banner, and that live run is how the wire shape earns
its receipt. The lock is written on a successful create/update; the receipt (not the
lock) is what flips ``_HARNESS_LIVE_VERIFIED``. This module stays pure and
offline-tested so the create/update/skip policy is settled independently of that.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

HARNESS_LOCKFILE_NAME = ".agentlift-harness.json"


@dataclass
class HarnessAction:
    action: str   # "create" | "update" | "skip"
    reason: str


@dataclass
class HarnessLock:
    path: str
    harness_id: Optional[str] = None
    harness_arn: Optional[str] = None
    region: Optional[str] = None
    spec_hash: Optional[str] = None
    display_name: Optional[str] = None
    deploy_model: Optional[str] = None

    @classmethod
    def load(cls, project_root: str) -> "HarnessLock":
        path = os.path.join(project_root, HARNESS_LOCKFILE_NAME)
        data: dict = {}
        if os.path.isfile(path):
            try:
                data = json.load(open(path, "r", encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                data = {}
        return cls(
            path=path,
            harness_id=data.get("harness_id"),
            harness_arn=data.get("harness_arn"),
            region=data.get("region"),
            spec_hash=data.get("spec_hash"),
            display_name=data.get("display_name"),
            deploy_model=data.get("deploy_model"),
        )

    def record(
        self, *, harness_id: str, harness_arn: str, region: str,
        spec_hash: str, display_name: str, deploy_model: str,
    ) -> None:
        self.harness_id = harness_id
        self.harness_arn = harness_arn
        self.region = region
        self.spec_hash = spec_hash
        self.display_name = display_name
        self.deploy_model = deploy_model

    def save(self) -> None:
        data = {
            "version": 1,
            "harness_id": self.harness_id,
            "harness_arn": self.harness_arn,
            "region": self.region,
            "spec_hash": self.spec_hash,
            "display_name": self.display_name,
            "deploy_model": self.deploy_model,
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")


def decide_action(lock: HarnessLock, spec_hash: str, *, region: str) -> HarnessAction:
    """What a deploy of ``spec_hash`` to ``region`` would do."""
    if not lock.harness_id:
        return HarnessAction("create", "no AgentCore harness recorded yet")
    if lock.region != region:
        return HarnessAction(
            "create",
            f"target region {region} differs from the recorded harness's "
            f"{lock.region}; the model is a regional inference profile, so this is a "
            f"new artifact -- creating a new harness",
        )
    if lock.spec_hash and lock.spec_hash == spec_hash:
        return HarnessAction("skip", "spec unchanged; the deployed harness is up to date")
    why = "spec changed" if lock.spec_hash else "no spec hash recorded (older lock)"
    return HarnessAction("update", f"{why}; updating the harness in place (keeps its id/ARN)")
