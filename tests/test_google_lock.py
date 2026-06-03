"""Google idempotency: the spec-hash lock round-trips, decides create/update/skip
purely, stays backward compatible with the old {reasoning_engine,project,location}
shape, and treats a project/location move as a fresh create. No network."""
import json
import os

from agentlift.google_lock import (
    GOOGLE_LOCKFILE_NAME,
    GoogleLock,
    decide_action,
)

ENGINE = "projects/p/locations/us-central1/reasoningEngines/123"


def _write(tmp_path, data):
    path = os.path.join(str(tmp_path), GOOGLE_LOCKFILE_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return str(tmp_path)


# --- round-trip ------------------------------------------------------------ #
def test_record_and_reload(tmp_path):
    lock = GoogleLock.load(str(tmp_path))
    assert lock.reasoning_engine is None
    lock.record(reasoning_engine=ENGINE, project="p", location="us-central1",
                spec_hash="abc", display_name="agentlift-lead", deploy_model="gemini-2.5-flash")
    lock.save()
    again = GoogleLock.load(str(tmp_path))
    assert again.reasoning_engine == ENGINE
    assert again.spec_hash == "abc"
    assert again.display_name == "agentlift-lead"
    assert again.deploy_model == "gemini-2.5-flash"


# --- decisions ------------------------------------------------------------- #
def test_create_when_no_engine(tmp_path):
    lock = GoogleLock.load(str(tmp_path))
    act = decide_action(lock, "h1", gcp_project="p", location="us-central1")
    assert act.action == "create"


def test_skip_when_spec_unchanged(tmp_path):
    lock = GoogleLock.load(str(tmp_path))
    lock.record(reasoning_engine=ENGINE, project="p", location="us-central1",
                spec_hash="h1", display_name="d", deploy_model="m")
    act = decide_action(lock, "h1", gcp_project="p", location="us-central1")
    assert act.action == "skip"


def test_update_when_spec_changed(tmp_path):
    lock = GoogleLock.load(str(tmp_path))
    lock.record(reasoning_engine=ENGINE, project="p", location="us-central1",
                spec_hash="h1", display_name="d", deploy_model="m")
    act = decide_action(lock, "h2", gcp_project="p", location="us-central1")
    assert act.action == "update"


def test_create_when_project_or_location_moves(tmp_path):
    lock = GoogleLock.load(str(tmp_path))
    lock.record(reasoning_engine=ENGINE, project="p", location="us-central1",
                spec_hash="h1", display_name="d", deploy_model="m")
    moved_proj = decide_action(lock, "h1", gcp_project="OTHER", location="us-central1")
    moved_loc = decide_action(lock, "h1", gcp_project="p", location="europe-west4")
    assert moved_proj.action == "create"
    assert moved_loc.action == "create"


# --- backward compatibility with the old (pre-spec-hash) lock -------------- #
def test_legacy_lock_without_spec_hash_forces_update(tmp_path):
    root = _write(tmp_path, {
        "reasoning_engine": ENGINE, "project": "p", "location": "us-central1",
    })
    lock = GoogleLock.load(root)
    assert lock.reasoning_engine == ENGINE
    assert lock.spec_hash is None
    act = decide_action(lock, "anything", gcp_project="p", location="us-central1")
    assert act.action == "update"  # no recorded hash -> can't prove it's current


def test_corrupt_lock_is_treated_as_empty(tmp_path):
    path = os.path.join(str(tmp_path), GOOGLE_LOCKFILE_NAME)
    open(path, "w").write("{ not json")
    lock = GoogleLock.load(str(tmp_path))
    assert lock.reasoning_engine is None
    assert decide_action(lock, "h", gcp_project="p", location="l").action == "create"


# --- end-to-end with a real plan hash -------------------------------------- #
def test_plan_hash_drives_skip_then_update(examples_dir, tmp_path):
    from agentlift.google_plan import build_google_plan
    from agentlift.parser import parse_project
    project, diags = parse_project(os.path.join(examples_dir, "team"))
    plan = build_google_plan(project, diags)

    lock = GoogleLock.load(str(tmp_path))
    assert decide_action(lock, plan.spec_hash, gcp_project="p", location="l").action == "create"
    lock.record(reasoning_engine=ENGINE, project="p", location="l",
                spec_hash=plan.spec_hash, display_name=plan.display_name,
                deploy_model=plan.deploy_model)
    # same folder -> same hash -> skip
    assert decide_action(lock, plan.spec_hash, gcp_project="p", location="l").action == "skip"
    # a different deploy model -> different hash -> update
    plan2 = build_google_plan(project, diags, model="gemini-2.5-pro")
    assert decide_action(lock, plan2.spec_hash, gcp_project="p", location="l").action == "update"
