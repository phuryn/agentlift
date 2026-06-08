"""Source-layer wiring tests, driven by fake clients (no network).

These verify that anthropic_source / harness_source call the right read APIs, follow
the roster closure, unpack skill archives, and feed the importer correctly — without
hitting a live account. The mapping itself is covered by test_importer; here we only
exercise the fetch + glue.
"""
from __future__ import annotations

import io
import zipfile

from agentlift.anthropic_source import fetch_anthropic_project
from agentlift.harness_source import fetch_harness
from agentlift.importer import import_anthropic_agents, import_bedrock_harness
from import_fixtures import BUG_FINDER, HARNESS, LEAD, RESEARCHER


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _Obj:
    def __init__(self, d):
        self._d = d

    def model_dump(self):
        return self._d


def _skill_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/SKILL.md", f"---\nname: {name}\ndescription: {name}\n---\nbody\n")
    return buf.getvalue()


class _Binary:
    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw


# skill_id -> the directory name the real versions.retrieve would report
_SKILL_NAMES = {"skill_cite": "cite-sources", "skill_webnotes": "web-notes",
                "skill_bugreport": "bug-report"}


class _FakeSkillVersions:
    def retrieve(self, version, *, skill_id, betas=None):
        name = _SKILL_NAMES.get(skill_id, skill_id)
        return _Obj({"name": name, "description": "d", "directory": name})

    def download(self, version, *, skill_id, betas=None):
        return _Binary(_skill_zip(_SKILL_NAMES.get(skill_id, skill_id)))


class _FakeSkills:
    versions = _FakeSkillVersions()


class _FakeAgents:
    def __init__(self, agents):
        self._by_id = {a["id"]: a for a in agents}

    def list(self, betas=None):
        # the listing only needs id + name
        return [_Obj({"id": a["id"], "name": a["name"]}) for a in self._by_id.values()]

    def retrieve(self, agent_id, betas=None):
        return _Obj(self._by_id[agent_id])


class _FakeBeta:
    def __init__(self, agents):
        self.agents = _FakeAgents(agents)
        self.skills = _FakeSkills()


class _FakeClient:
    def __init__(self, agents):
        self.beta = _FakeBeta(agents)


# --------------------------------------------------------------------------- #
# anthropic_source
# --------------------------------------------------------------------------- #
def test_fetch_pulls_roster_closure_from_one_coordinator():
    """Selecting only the coordinator still imports its subagents (the closure)."""
    client = _FakeClient([LEAD, RESEARCHER, BUG_FINDER])
    agents_raw, skill_bundles, diags = fetch_anthropic_project(client, agent_names=["lead"])
    names = {a["name"] for a in agents_raw}
    assert names == {"lead", "researcher", "bug-finder"}
    # every custom skill the closure references was downloaded + unpacked
    assert {b.name for b in skill_bundles.values()} == {"cite-sources", "web-notes", "bug-report"}
    assert not diags.errors


def test_fetch_all_agents_and_full_import():
    client = _FakeClient([LEAD, RESEARCHER, BUG_FINDER])
    agents_raw, skill_bundles, diags = fetch_anthropic_project(client)
    proj = import_anthropic_agents(agents_raw, skill_bundles, diags)
    assert {a.name for a in proj.agents} == {"lead", "researcher", "bug-finder"}
    assert [s.name for s in proj.shared_skills] == ["cite-sources"]


def test_fetch_unknown_agent_errors():
    client = _FakeClient([LEAD])
    _, _, diags = fetch_anthropic_project(client, agent_names=["nope"])
    assert any(d.code == "import.agent_not_found" for d in diags.errors)


def test_skill_archive_unpack_preserves_prefix():
    client = _FakeClient([RESEARCHER])
    _, skill_bundles, _ = fetch_anthropic_project(client, agent_names=["researcher"])
    cite = next(b for b in skill_bundles.values() if b.name == "cite-sources")
    assert "cite-sources/SKILL.md" in cite.files


def test_skill_files_reprefixed_to_metadata_name():
    """If the download's internal dir differs from the metadata name, files are re-keyed
    to '<name>/...' so the skill dir == its frontmatter ref (no dangling reference)."""
    from agentlift.anthropic_source import fetch_skill_bundle
    from agentlift.diagnostics import Diagnostics

    class _Versions:
        def retrieve(self, version, *, skill_id, betas=None):
            return _Obj({"name": "tidy-name", "description": "d", "directory": "tidy-name"})

        def download(self, version, *, skill_id, betas=None):
            # archive uses a DIFFERENT internal prefix than the metadata name
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("ugly_internal_dir/SKILL.md", "body")
                zf.writestr("ugly_internal_dir/ref.md", "more")
            return _Binary(buf.getvalue())

    class _Skills:
        versions = _Versions()

    class _Beta:
        skills = _Skills()

    class _Client:
        beta = _Beta()

    bundle = fetch_skill_bundle(_Client(), "sk_1", "latest", Diagnostics())
    assert bundle.name == "tidy-name"
    # every file is under the metadata name, not the archive's internal dir
    assert set(bundle.files) == {"tidy-name/SKILL.md", "tidy-name/ref.md"}


# --------------------------------------------------------------------------- #
# harness_source
# --------------------------------------------------------------------------- #
class _FakeS3:
    def __init__(self, objects):
        self._objects = objects  # {key: bytes}

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        return {"Contents": [{"Key": k} for k in self._objects if k.startswith(prefix)],
                "IsTruncated": False}

    def get_object(self, Bucket, Key):
        return {"Body": _Binary(self._objects[Key])}


class _FakeControl:
    def __init__(self, harness):
        self._harness = harness

    def get_harness(self, harnessId):
        return {"harness": self._harness}

    def list_harnesses(self, **kwargs):
        return {"harnesses": [{"harnessName": self._harness["harnessName"],
                               "harnessId": self._harness["harnessId"]}]}


def test_harness_fetch_resolves_name_and_loads_s3_skill(monkeypatch):
    objects = {"agentlift-skills/support-agent/cite-sources/SKILL.md":
               b"---\nname: cite-sources\ndescription: cite\n---\nbody\n"}
    control = _FakeControl(HARNESS)
    s3 = _FakeS3(objects)

    def fake_client(region, service):
        return control if service == "bedrock-agentcore-control" else s3

    monkeypatch.setattr("agentlift.harness_source._client", fake_client)
    harness, skill_bundles, diags = fetch_harness("us-west-2", harness_name="support-agent")
    assert harness["harnessName"] == "support-agent"
    assert len(skill_bundles) == 1
    bundle = next(iter(skill_bundles.values()))
    assert bundle.name == "cite-sources"
    assert "cite-sources/SKILL.md" in bundle.files

    proj = import_bedrock_harness(harness, skill_bundles, diags)
    assert proj.agents[0].model == "claude-haiku-4-5"


def test_harness_fetch_missing_name_errors(monkeypatch):
    control = _FakeControl(HARNESS)
    monkeypatch.setattr("agentlift.harness_source._client",
                        lambda region, service: control)
    _, _, diags = fetch_harness("us-west-2", harness_name="does-not-exist")
    assert any(d.code == "import.harness_not_found" for d in diags.errors)
