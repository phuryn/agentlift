"""Idempotency state for the Amazon Bedrock AgentCore Runtime target.

Mirrors ``google_lock.py`` but for the single AgentCore Runtime a Bedrock deploy
produces. The decision is a *pure* function of (recorded lock, plan spec hash,
target region) -- no network -- so ``agentlift plan --target bedrock`` (and a
future ``deploy``) can show exactly what a deploy would do: a no-op when nothing
changed, an ``update_agent_runtime`` (keeping the runtime id/ARN) when the spec
changed, and a ``create_agent_runtime`` when there is no runtime yet or the
target region moved.

**Ready infrastructure, write-dead today.** The hosted deploy path
(``create/update_agent_runtime``) is control-plane and needs AWS IAM + an
execution role + ECR -- deferred until there is a live receipt (see
``experiments/bedrock-composition/RESULTS.md``, Gate B). So nothing in the
shipped CLI writes ``.agentlift-bedrock.json`` yet. This module exists, pure and
offline-tested, so the create/update/skip policy is settled and the lock is
ready the moment the live path lands -- not so that the live path can be faked.

A region change forces a ``create``: the deployed model ids are *regional*
inference profiles (``eu.anthropic.*`` vs ``us.anthropic.*``), so the same folder
in two regions is genuinely two artifacts. (The plan's ``spec_hash`` already
differs by region, since the resolved profile prefix flows into it; the explicit
region check here makes the intent legible rather than relying on that coupling.)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

BEDROCK_LOCKFILE_NAME = ".agentlift-bedrock.json"


@dataclass
class BedrockAction:
    action: str   # "create" | "update" | "skip"
    reason: str


@dataclass
class BedrockLock:
    path: str
    agent_runtime_id: Optional[str] = None
    agent_runtime_arn: Optional[str] = None
    region: Optional[str] = None
    spec_hash: Optional[str] = None
    display_name: Optional[str] = None
    deploy_model: Optional[str] = None

    @classmethod
    def load(cls, project_root: str) -> "BedrockLock":
        path = os.path.join(project_root, BEDROCK_LOCKFILE_NAME)
        data: dict = {}
        if os.path.isfile(path):
            try:
                data = json.load(open(path, "r", encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                data = {}
        return cls(
            path=path,
            agent_runtime_id=data.get("agent_runtime_id"),
            agent_runtime_arn=data.get("agent_runtime_arn"),
            region=data.get("region"),
            spec_hash=data.get("spec_hash"),
            display_name=data.get("display_name"),
            deploy_model=data.get("deploy_model"),
        )

    def record(
        self, *, agent_runtime_id: str, agent_runtime_arn: str, region: str,
        spec_hash: str, display_name: str, deploy_model: str,
    ) -> None:
        self.agent_runtime_id = agent_runtime_id
        self.agent_runtime_arn = agent_runtime_arn
        self.region = region
        self.spec_hash = spec_hash
        self.display_name = display_name
        self.deploy_model = deploy_model

    def save(self) -> None:
        data = {
            "version": 1,
            "agent_runtime_id": self.agent_runtime_id,
            "agent_runtime_arn": self.agent_runtime_arn,
            "region": self.region,
            "spec_hash": self.spec_hash,
            "display_name": self.display_name,
            "deploy_model": self.deploy_model,
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")


def decide_action(lock: BedrockLock, spec_hash: str, *, region: str) -> BedrockAction:
    """What a deploy of ``spec_hash`` to ``region`` would do.

    Note (to live-verify before the hosted path ships): an AgentCore Runtime
    ``update`` creates a new immutable runtime *version* and the ``DEFAULT``
    endpoint advances to it, while pinned custom endpoints stay on their version.
    So "update" here means "update the runtime in place (keeps its ARN); DEFAULT
    callers move, pinned endpoints do not" -- the endpoint-routing nuance is a
    deploy-time concern, not an identity change.
    """
    if not lock.agent_runtime_id:
        return BedrockAction("create", "no AgentCore Runtime recorded yet")
    if lock.region != region:
        return BedrockAction(
            "create",
            f"target region {region} differs from the recorded runtime's "
            f"{lock.region}; the model is a regional inference profile, so this is "
            f"a new artifact -- creating a new runtime",
        )
    if lock.spec_hash and lock.spec_hash == spec_hash:
        return BedrockAction("skip", "spec unchanged; the deployed runtime is up to date")
    why = "spec changed" if lock.spec_hash else "no spec hash recorded (older lock)"
    return BedrockAction("update", f"{why}; updating the runtime in place (keeps its ARN)")
