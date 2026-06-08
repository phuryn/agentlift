"""Read a live Bedrock AgentCore Harness back into a raw dict (inverse of harness_target).

Network edge for the Bedrock-harness importer. It fetches `get_harness` and the
skill bundles from S3, and hands them to the pure `importer.import_bedrock_harness`.
No mapping logic here (it lives in `importer.py`), mirroring how `harness_plan.py`
is pure and only `harness_target.py` touches the wire.

Only the **harness** is importable. A Bedrock **Runtime** bakes its agent definition
into an opaque ARM64 container image (`GetAgentRuntime` returns only a containerUri),
so it cannot be read back — that boundary is the runtime analogue of the deploy-time
`/invocations` trace boundary, and `import bedrock --mode runtime` refuses with it.

Confirmed against boto3 1.43.24 (bedrock-agentcore-control, preview): get_harness ->
{harness: {harnessName, model.bedrockModelConfig.modelId, systemPrompt[].text,
tools[], skills[].s3.uri, allowedTools, ...}}. Field access is defensive because the
preview shape can change (`bedrock.harness.preview`).
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

from .diagnostics import Diagnostics
from .import_model import ImportedSkill
from .importer import hash_skill_files


def _client(region: str, service: str):
    import boto3
    return boto3.client(service, region_name=region)


def _skill_name_from_uri(uri: str) -> str:
    """The last non-empty path segment of an s3://bucket/prefix/<name>/ uri."""
    parts = [p for p in urlparse(uri).path.split("/") if p]
    return parts[-1] if parts else "skill"


def _fetch_s3_skill(s3, uri: str, diags: Diagnostics) -> Optional[ImportedSkill]:
    """Load a skill bundle from its s3://bucket/prefix/ location into an ImportedSkill.

    The harness uploads SKILL.md (and siblings) directly under the prefix; we re-prefix
    each object with the skill's '<name>/...' so it round-trips through the parser.
    """
    parsed = urlparse(uri)
    bucket, prefix = parsed.netloc, parsed.path.lstrip("/")
    if not prefix.endswith("/"):
        prefix += "/"
    name = _skill_name_from_uri(uri)
    files: dict[str, bytes] = {}
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix):]
            data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            files[f"{name}/{rel}"] = data
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    if not files:
        diags.warning("import.skill_missing", f"no objects under skill prefix '{uri}'")
        return None
    desc = None
    return ImportedSkill(name=name, files=files, description=desc,
                         content_hash=hash_skill_files(files))


def fetch_harness(
    region: str,
    harness_id: Optional[str] = None,
    harness_name: Optional[str] = None,
    diags: Optional[Diagnostics] = None,
) -> tuple[dict, dict[str, ImportedSkill], Diagnostics]:
    """Fetch one harness (+ its S3 skill bundles) from a live account.

    Select by `harness_id`, or by `harness_name` (resolved via list_harnesses).
    Returns (harness_dict, {s3_uri: ImportedSkill}, diagnostics).
    """
    diags = diags or Diagnostics()
    control = _client(region, "bedrock-agentcore-control")

    if harness_id is None and harness_name:
        token = None
        while harness_id is None:
            kwargs: dict[str, Any] = {}
            if token:
                kwargs["nextToken"] = token
            page = control.list_harnesses(**kwargs)
            for h in page.get("harnesses") or []:
                if h.get("harnessName") == harness_name:
                    harness_id = h.get("harnessId")
                    break
            token = page.get("nextToken")
            if not token:
                break
        if harness_id is None:
            diags.error("import.harness_not_found",
                        f"no harness named '{harness_name}' in {region}")
            return {}, {}, diags

    harness = control.get_harness(harnessId=harness_id).get("harness") or {}

    skill_bundles: dict[str, ImportedSkill] = {}
    skills = harness.get("skills") or []
    if skills:
        s3 = _client(region, "s3")
        for sk in skills:
            uri = (sk.get("s3") or {}).get("uri")
            if not uri:
                if sk.get("git") or sk.get("path"):
                    diags.warning("import.skill_non_s3",
                                  "skill is git/path-sourced, not S3 — not re-imported",
                                  harness.get("harnessName", ""))
                continue
            try:
                bundle = _fetch_s3_skill(s3, uri, diags)
                if bundle:
                    skill_bundles[uri] = bundle
            except Exception as exc:  # noqa: BLE001
                diags.warning("import.skill_download_failed",
                              f"could not load skill from '{uri}': {exc}")

    return harness, skill_bundles, diags
