"""`agentlift diff` against the lockfile — offline, no network."""
import os
import shutil

from agentlift.anthropic_target import Deployer
from agentlift.diff import compute_diff
from agentlift.lockfile import Lockfile
from agentlift.parser import parse_project
from agentlift.planner import build_plan


# --- a minimal fake client so we can produce a real lockfile without network ---
class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Skills:
    def __init__(self, c):
        self.c = c

    def create(self, display_title=None, files=None, betas=None):
        self.c["s"] += 1
        return _Obj(id=f"skill_{self.c['s']:03d}")

    def list(self, **kw):
        return []


class _Agents:
    def __init__(self, c):
        self.c = c

    def create(self, betas=None, **req):
        self.c["a"] += 1
        return _Obj(id=f"agent_{self.c['a']:03d}", version=1)

    def archive(self, *a, **k):
        pass


class FakeClient:
    def __init__(self):
        self.c = {"s": 0, "a": 0}
        self.beta = _Obj(skills=_Skills(self.c), agents=_Agents(self.c))


def _plan(path):
    project, diags = parse_project(path)
    return project, build_plan(project, diags)


def test_diff_all_new_when_no_lockfile(examples_dir, tmp_path):
    _project, plan = _plan(os.path.join(examples_dir, "quickstart"))
    lock = Lockfile(path=os.path.join(str(tmp_path), "none.json"))  # empty
    d = compute_diff(plan, lock)
    assert d.skills_new == ["receipt-stamp"]
    assert d.agents_new == ["knowledge-agent"]
    assert d.changes == 2


def test_diff_in_sync_after_deploy(examples_dir, tmp_path):
    dst = os.path.join(str(tmp_path), "team")
    shutil.copytree(os.path.join(examples_dir, "team"), dst)
    project, diags = parse_project(dst)
    plan = build_plan(project, diags)
    Deployer(FakeClient(), project.root).apply(plan)

    lock = Lockfile.load(project.root)
    d = compute_diff(plan, lock)
    assert d.changes == 0
    assert sorted(d.agents_unchanged) == ["bug-finder", "lead", "researcher"]
    assert sorted(d.skills_unchanged) == ["bug-report", "cite-sources"]


def test_diff_detects_change_and_stale(examples_dir, tmp_path):
    dst = os.path.join(str(tmp_path), "team")
    shutil.copytree(os.path.join(examples_dir, "team"), dst)
    project, diags = parse_project(dst)
    Deployer(FakeClient(), project.root).apply(build_plan(project, diags))
    lock = Lockfile.load(project.root)

    # edit the researcher's system prompt -> that agent should read as changed
    rpath = os.path.join(dst, ".managed-agents", "researcher", "agent.md")
    with open(rpath, "a", encoding="utf-8") as fh:
        fh.write("\nAlways answer in British English.\n")
    # remove the lead coordinator folder -> it should read as stale (still in lockfile)
    shutil.rmtree(os.path.join(dst, ".managed-agents", "lead"))

    project2, diags2 = parse_project(dst)
    plan2 = build_plan(project2, diags2)
    d = compute_diff(plan2, lock)
    assert "researcher" in d.agents_changed
    assert "bug-finder" in d.agents_unchanged
    assert "lead" in d.agents_stale
