"""Gated, read-only live test: import every agent from a real Anthropic account.

This exercises the *real* network path (`anthropic_source.fetch_anthropic_project`
against `client.beta.agents.list/retrieve` + `skills.versions.download`) and proves
the result round-trips through the real parser + planner. It is read-only — it never
creates, updates, or archives anything — so it is safe and costs only a few list/get
calls.

Gated twice so it never runs by accident:
  * the ``live`` marker (CI runs ``pytest -m "not live"``); and
  * ``AGENTLIFT_LIVE_IMPORT=1`` — even ``pytest -m live`` skips it otherwise, since it
    needs at least one agent already deployed in the account to be meaningful.

Run it deliberately:
  AGENTLIFT_LIVE_IMPORT=1 ANTHROPIC_API_KEY=... pytest -m live tests/live/test_import_anthropic.py
"""
from __future__ import annotations

import os
import tempfile

import pytest

pytestmark = pytest.mark.live


@pytest.mark.skipif(os.environ.get("AGENTLIFT_LIVE_IMPORT") != "1",
                    reason="set AGENTLIFT_LIVE_IMPORT=1 to run the read-only live import")
def test_live_import_roundtrips(live_client):
    from agentlift.anthropic_source import fetch_anthropic_project
    from agentlift.folder_writer import write_project
    from agentlift.importer import import_anthropic_agents
    from agentlift.parser import parse_project
    from agentlift.planner import build_plan

    agents_raw, skill_bundles, diags = fetch_anthropic_project(live_client)
    if not agents_raw:
        pytest.skip("no agents in this account to import")

    project = import_anthropic_agents(agents_raw, skill_bundles, diags)
    assert not project.diagnostics.errors, project.diagnostics.render()

    with tempfile.TemporaryDirectory() as out:
        write_project(project, out)
        reparsed, pdiags = parse_project(out)
        plan = build_plan(reparsed, pdiags)
        # every fetched agent reconstructs, and the folder is deployable again
        assert len(reparsed.agents) == len(agents_raw)
        assert plan.deployable, pdiags.render()
