"""`agentlift deploy --target bedrock`: the two sharp paths. `--build-only`
materializes the deployable container artifact and exits 0; a bare deploy refuses
the hosted path (preview, not live-verified), fires no network call, writes
nothing, and exits non-zero. No network either way."""
import os
import shutil

from agentlift.bedrock_lock import BEDROCK_LOCKFILE_NAME
from agentlift.cli import main


def _copy(src_dir, tmp_path, name):
    dst = os.path.join(str(tmp_path), name)
    shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns(
        ".agentlift-*.json", "*.bak", ".agentlift-build"))
    return dst


def test_deploy_build_only_exits_zero_and_writes_artifact(examples_dir, tmp_path, capsys):
    root = _copy(os.path.join(examples_dir, "team"), tmp_path, "team")
    rc = main(["deploy", root, "--target", "bedrock", "--build-only"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Built container artifact" in out
    build = os.path.join(root, ".agentlift-build", "bedrock")
    assert os.path.isfile(os.path.join(build, "Dockerfile"))
    assert os.path.isfile(os.path.join(build, "agentlift_runtime", "agent.py"))
    assert os.path.isfile(os.path.join(build, "NOTES.txt"))
    # build-only never writes the lock
    assert not os.path.isfile(os.path.join(root, BEDROCK_LOCKFILE_NAME))


def test_bare_deploy_refuses_no_side_effect(examples_dir, tmp_path, capsys):
    root = _copy(os.path.join(examples_dir, "team"), tmp_path, "team")
    rc = main(["deploy", root, "--target", "bedrock"])
    out = capsys.readouterr().out
    assert rc == 3                                   # non-zero "not yet"
    assert "PREVIEW" in out
    assert "Gate A" in out and "Gate B" in out
    assert "--build-only" in out
    # bare deploy is print-and-exit: no artifact, no lock
    assert not os.path.exists(os.path.join(root, ".agentlift-build"))
    assert not os.path.isfile(os.path.join(root, BEDROCK_LOCKFILE_NAME))


def test_bedrock_region_flows_to_build(examples_dir, tmp_path, capsys):
    root = _copy(os.path.join(examples_dir, "team"), tmp_path, "team")
    rc = main(["deploy", root, "--target", "bedrock", "--build-only",
               "--bedrock-region", "us-east-1"])
    assert rc == 0
    capsys.readouterr()
    df = open(os.path.join(root, ".agentlift-build", "bedrock", "agentlift_runtime",
                           "agent.py"), encoding="utf-8").read()
    assert "REGION = 'us-east-1'" in df
    assert "us.anthropic." in df
