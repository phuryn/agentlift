"""Apply the plan twice against a fake client and prove the second run is a no-op
(skill dedup + agent spec-hash idempotency). No network involved."""
import os

from skylift.anthropic_target import Deployer
from skylift.lockfile import Lockfile, canonical_hash
from skylift.parser import parse_project
from skylift.planner import build_plan


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeSkills:
    def __init__(self, counter):
        self.counter = counter

    def create(self, display_title=None, files=None, betas=None):
        self.counter["skills"] += 1
        return _Obj(id=f"skill_{self.counter['skills']:03d}", display_title=display_title)


class FakeAgents:
    def __init__(self, counter):
        self.counter = counter
        self.archived = []

    def create(self, betas=None, **req):
        self.counter["agents"] += 1
        return _Obj(id=f"agent_{self.counter['agents']:03d}", version=1)

    def archive(self, agent_id, betas=None):
        self.archived.append(agent_id)


class FakeBeta:
    def __init__(self, counter):
        self.skills = FakeSkills(counter)
        self.agents = FakeAgents(counter)


class FakeClient:
    def __init__(self):
        self.counter = {"skills": 0, "agents": 0}
        self.beta = FakeBeta(self.counter)


def test_lockfile_roundtrip(tmp_path):
    lock = Lockfile.load(str(tmp_path))
    lock.set_skill("hash1", "skill_abc", "demo")
    lock.set_agent("a", "agent_xyz", 1, "spec1", ["skill_abc"])
    lock.save()
    again = Lockfile.load(str(tmp_path))
    assert again.skill_id("hash1") == "skill_abc"
    assert again.agent("a")["agent_id"] == "agent_xyz"


def test_canonical_hash_stable():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


def test_apply_is_idempotent(examples_dir, tmp_path):
    # copy the team example into a temp dir so the lockfile lands there
    import shutil
    src = os.path.join(examples_dir, "team")
    dst = os.path.join(str(tmp_path), "team")
    shutil.copytree(src, dst)

    project, diags = parse_project(dst)
    plan = build_plan(project, diags)
    assert plan.deployable

    client = FakeClient()
    deployer = Deployer(client, project.root)
    r1 = deployer.apply(plan)
    # first run: 2 unique skills (bug-report, cite-sources), 3 agents
    assert client.counter["skills"] == 2
    assert client.counter["agents"] == 3
    assert len(r1.created_agents) == 3

    # second run with a FRESH deployer (reloads the lockfile written by run 1)
    deployer2 = Deployer(client, project.root)
    r2 = deployer2.apply(plan)
    assert client.counter["skills"] == 2   # no new uploads
    assert client.counter["agents"] == 3   # no new creates
    assert len(r2.reused_agents) == 3
    assert len(r2.reused_skills) == 2
