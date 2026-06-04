"""The Bedrock idempotency lock is pure: create / update / skip is decided from
(recorded lock, plan spec hash, target region) with no network. A region change
forces a create (the model is a regional inference profile -> a new artifact).
Backward compatible: a lock with no spec hash forces an update. Ships now, but is
write-dead until the hosted path lands (asserted in test_bedrock_target)."""
import json
import os

from agentlift.bedrock_lock import BEDROCK_LOCKFILE_NAME, BedrockLock, decide_action

ARN = "arn:aws:bedrock-agentcore:eu-north-1:111122223333:runtime/agentlift-lead-abc"
RID = "agentlift-lead-abc"


def _seed(tmp_path, **over):
    lock = BedrockLock.load(str(tmp_path))
    kw = dict(agent_runtime_id=RID, agent_runtime_arn=ARN, region="eu-north-1",
              spec_hash="HASH", display_name="agentlift-lead",
              deploy_model="eu.anthropic.claude-haiku-4-5-20251001-v1:0")
    kw.update(over)
    lock.record(**kw)
    lock.save()
    return lock


# --- decide_action -------------------------------------------------------- #
def test_create_on_fresh_lock(tmp_path):
    lock = BedrockLock.load(str(tmp_path))   # nothing recorded
    act = decide_action(lock, "HASH", region="eu-north-1")
    assert act.action == "create" and "no AgentCore Runtime" in act.reason


def test_skip_when_unchanged(tmp_path):
    _seed(tmp_path)
    lock = BedrockLock.load(str(tmp_path))
    act = decide_action(lock, "HASH", region="eu-north-1")
    assert act.action == "skip"


def test_update_when_spec_changes(tmp_path):
    _seed(tmp_path)
    lock = BedrockLock.load(str(tmp_path))
    act = decide_action(lock, "NEWHASH", region="eu-north-1")
    assert act.action == "update" and "spec changed" in act.reason


def test_region_change_forces_create(tmp_path):
    # same spec hash, different region -> a new regional artifact -> create
    _seed(tmp_path, region="eu-north-1")
    lock = BedrockLock.load(str(tmp_path))
    act = decide_action(lock, "HASH", region="us-east-1")
    assert act.action == "create" and "region" in act.reason


def test_missing_spec_hash_forces_update(tmp_path):
    # older lock recorded without a spec hash -> redeploy to be safe
    _seed(tmp_path, spec_hash=None)
    lock = BedrockLock.load(str(tmp_path))
    act = decide_action(lock, "HASH", region="eu-north-1")
    assert act.action == "update" and "older lock" in act.reason


# --- persistence ---------------------------------------------------------- #
def test_save_and_reload_roundtrip(tmp_path):
    _seed(tmp_path)
    path = os.path.join(str(tmp_path), BEDROCK_LOCKFILE_NAME)
    assert os.path.isfile(path)
    data = json.load(open(path, encoding="utf-8"))
    assert data["agent_runtime_arn"] == ARN
    assert data["region"] == "eu-north-1"
    assert data["version"] == 1

    again = BedrockLock.load(str(tmp_path))
    assert again.agent_runtime_id == RID
    assert again.spec_hash == "HASH"
    assert again.deploy_model.startswith("eu.anthropic.")


def test_load_missing_is_empty(tmp_path):
    lock = BedrockLock.load(str(tmp_path))
    assert lock.agent_runtime_id is None and lock.region is None


def test_load_corrupt_is_empty(tmp_path):
    path = os.path.join(str(tmp_path), BEDROCK_LOCKFILE_NAME)
    open(path, "w", encoding="utf-8").write("{ not json")
    lock = BedrockLock.load(str(tmp_path))
    assert lock.agent_runtime_id is None
