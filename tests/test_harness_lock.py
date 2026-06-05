"""The harness idempotency lock is pure: create / update / skip is decided from
(recorded lock, plan spec hash, target region) with no network. A region change
forces a create (the model is a regional inference profile -> a new artifact). A
separate ``.agentlift-harness.json`` so a folder can carry both a runtime and a
harness deploy without their identities colliding. ``deploy_harness`` writes this
lock on a successful (preview) create/update -- exercised via seams in
test_harness_target."""
import json
import os

from agentlift.harness_lock import HARNESS_LOCKFILE_NAME, HarnessLock, decide_action

ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/agentlift_api-abc"
HID = "agentlift_api-abc"


def _seed(tmp_path, **over):
    lock = HarnessLock.load(str(tmp_path))
    kw = dict(harness_id=HID, harness_arn=ARN, region="us-west-2",
              spec_hash="HASH", display_name="agentlift-api",
              deploy_model="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    kw.update(over)
    lock.record(**kw)
    lock.save()
    return lock


# --- decide_action -------------------------------------------------------- #
def test_create_on_fresh_lock(tmp_path):
    lock = HarnessLock.load(str(tmp_path))
    act = decide_action(lock, "HASH", region="us-west-2")
    assert act.action == "create" and "no AgentCore harness" in act.reason


def test_skip_when_unchanged(tmp_path):
    _seed(tmp_path)
    lock = HarnessLock.load(str(tmp_path))
    act = decide_action(lock, "HASH", region="us-west-2")
    assert act.action == "skip"


def test_update_when_spec_changes(tmp_path):
    _seed(tmp_path)
    lock = HarnessLock.load(str(tmp_path))
    act = decide_action(lock, "NEWHASH", region="us-west-2")
    assert act.action == "update" and "spec changed" in act.reason


def test_region_change_forces_create(tmp_path):
    _seed(tmp_path, region="us-west-2")
    lock = HarnessLock.load(str(tmp_path))
    act = decide_action(lock, "HASH", region="eu-central-1")
    assert act.action == "create" and "region" in act.reason


def test_missing_spec_hash_forces_update(tmp_path):
    _seed(tmp_path, spec_hash=None)
    lock = HarnessLock.load(str(tmp_path))
    act = decide_action(lock, "HASH", region="us-west-2")
    assert act.action == "update" and "older lock" in act.reason


# --- persistence ---------------------------------------------------------- #
def test_save_and_reload_roundtrip(tmp_path):
    _seed(tmp_path)
    path = os.path.join(str(tmp_path), HARNESS_LOCKFILE_NAME)
    assert os.path.isfile(path)
    data = json.load(open(path, encoding="utf-8"))
    assert data["harness_arn"] == ARN
    assert data["region"] == "us-west-2"
    assert data["version"] == 1

    again = HarnessLock.load(str(tmp_path))
    assert again.harness_id == HID
    assert again.spec_hash == "HASH"
    assert again.deploy_model.startswith("us.anthropic.")


def test_separate_lockfile_from_runtime(tmp_path):
    # the harness lock must NOT be the runtime's .agentlift-bedrock.json
    assert HARNESS_LOCKFILE_NAME == ".agentlift-harness.json"
    _seed(tmp_path)
    assert os.path.isfile(os.path.join(str(tmp_path), HARNESS_LOCKFILE_NAME))
    assert not os.path.isfile(os.path.join(str(tmp_path), ".agentlift-bedrock.json"))


def test_load_missing_is_empty(tmp_path):
    lock = HarnessLock.load(str(tmp_path))
    assert lock.harness_id is None and lock.region is None


def test_load_corrupt_is_empty(tmp_path):
    path = os.path.join(str(tmp_path), HARNESS_LOCKFILE_NAME)
    open(path, "w", encoding="utf-8").write("{ not json")
    lock = HarnessLock.load(str(tmp_path))
    assert lock.harness_id is None
