"""`agentlift audit`: capability-map integrity, folder feature detection, tiers."""
import os

from agentlift.audit import detect_used_features, render_audit, run_audit
from agentlift.capabilities import CAPABILITIES, FEATURES, TIER_ORDER
from agentlift.parser import parse_project


def _team(examples_dir):
    project, _ = parse_project(os.path.join(examples_dir, "team"))
    return project


def test_capability_map_covers_every_feature_for_every_provider():
    feature_ids = {f["id"] for f in FEATURES}
    for provider, caps in CAPABILITIES.items():
        assert set(caps) == feature_ids, f"{provider}: feature set mismatch"
        for cap in caps.values():
            assert cap["tier"] in TIER_ORDER
            # every non-native row must explain itself
            assert cap["tier"] == "native" or cap["reason"]


def test_anthropic_is_the_all_native_reference():
    assert all(c["tier"] == "native" for c in CAPABILITIES["anthropic"].values())


def test_detect_features_on_team(examples_dir):
    used = detect_used_features(_team(examples_dir))
    for fid in ("hosted_runtime", "builtin_sandbox", "tool_approval", "skills",
                "remote_mcp", "subagents", "deploy_versioning", "streaming"):
        assert fid in used, f"expected {fid} to be detected in the team example"
    assert "knowledge" not in used  # team ships no knowledge/ files


def test_audit_tiers_match_research(examples_dir):
    report = run_audit(_team(examples_dir), ["anthropic", "google", "openai"])

    anth = {r["id"]: r["tier"] for r in report["targets"]["anthropic"]}
    assert set(anth.values()) == {"native"}

    goog = {r["id"]: r["tier"] for r in report["targets"]["google"]}
    assert goog["tool_approval"] == "unsupported"   # :ask not enforced on the hosted runtime
    assert goog["builtin_sandbox"] == "degraded"    # python/js only, no bash
    assert goog["subagents"] == "emulated"          # one resource, not per-agent-id
    assert goog["remote_mcp"] == "native"

    oai = {r["id"]: r["tier"] for r in report["targets"]["openai"]}
    assert oai["subagents"] == "emulated"           # agent-as-tool composition works (confirmed); loop runs in your orchestrator
    assert oai["hosted_runtime"] == "degraded"      # graph-only / self-host


def test_audit_summary_counts(examples_dir):
    report = run_audit(_team(examples_dir), ["google"])
    counts = report["summary"]["google"]
    # counts cover exactly the used features, no double counting
    assert sum(counts.values()) == len(report["targets"]["google"])
    assert counts["unsupported"] >= 1


def test_audit_unknown_target_is_none(examples_dir):
    report = run_audit(_team(examples_dir), ["nope"])
    assert report["targets"]["nope"] is None


def test_render_is_stable_text(examples_dir):
    text = render_audit(_team(examples_dir), ["anthropic", "google", "openai"],
                        run_audit(_team(examples_dir), ["anthropic", "google", "openai"]))
    assert "Portability audit:" in text
    assert "Anthropic Managed Agents" in text
    assert "Verdict" in text
    # the degraded/unsupported reasons must surface for the user
    assert "reason:" in text
