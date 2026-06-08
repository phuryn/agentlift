"""The CLI's `import` surface, driven through `main()` with a fake fetch (no network).

Exercises every branch: anthropic import (write + round-trip report), --dry-run
(no files), and the Bedrock --mode runtime refusal (an opaque container can't be
read back).
"""
from __future__ import annotations

import os

from agentlift.cli import main
from import_fixtures import SKILL_BUNDLES, TEAM_AGENTS


def _patch_anthropic(monkeypatch):
    """Stub the network: get_client + fetch_anthropic_project return the canned team."""
    from agentlift import cli

    monkeypatch.setattr(cli, "get_client", lambda: object())

    def fake_fetch(client, agent_names=None, diags=None):
        from agentlift.diagnostics import Diagnostics
        return list(TEAM_AGENTS), dict(SKILL_BUNDLES), diags or Diagnostics()

    monkeypatch.setattr("agentlift.anthropic_source.fetch_anthropic_project", fake_fetch)


def test_import_anthropic_writes_and_roundtrips(monkeypatch, tmp_path, capsys):
    _patch_anthropic(monkeypatch)
    rc = main(["import", "anthropic", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Imported 3 agent(s) from anthropic" in out
    assert "coordinator -> [researcher, bug-finder]" in out
    assert "Round-trip OK" in out
    # files really landed
    assert os.path.isfile(os.path.join(tmp_path, ".managed-agents", "lead", "agent.md"))
    assert os.path.isfile(os.path.join(
        tmp_path, ".managed-agents", "shared", "skills", "cite-sources", "SKILL.md"))


def test_import_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    _patch_anthropic(monkeypatch)
    rc = main(["import", "anthropic", str(tmp_path), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry run" in out
    assert not os.path.exists(os.path.join(tmp_path, ".managed-agents"))


def test_import_bedrock_runtime_refused(tmp_path, capsys):
    rc = main(["import", "bedrock", str(tmp_path), "--mode", "runtime"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "opaque container" in err
    assert not os.path.exists(os.path.join(tmp_path, ".managed-agents"))


def test_import_bedrock_harness_writes(monkeypatch, tmp_path, capsys):
    from agentlift.diagnostics import Diagnostics
    from import_fixtures import HARNESS, HARNESS_SKILLS

    def fake_fetch(region, harness_id=None, harness_name=None, diags=None):
        return HARNESS, dict(HARNESS_SKILLS), diags or Diagnostics()

    monkeypatch.setattr("agentlift.harness_source.fetch_harness", fake_fetch)
    rc = main(["import", "bedrock", str(tmp_path), "--harness-name", "support-agent"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Imported 1 agent(s) from bedrock-harness" in out
    assert "Round-trip OK" in out
    assert os.path.isfile(os.path.join(
        tmp_path, ".managed-agents", "support-agent", "agent.md"))
