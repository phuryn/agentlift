"""Read live Anthropic Managed Agents back into raw dicts (the inverse of anthropic_target).

Network edge for the Anthropic importer. It only *fetches* — `agents.list`,
`agents.retrieve`, and `skills.versions.{retrieve,download}` — and hands the raw
shapes to the pure `importer.import_anthropic_agents`. No mapping logic lives here
(it lives in `importer.py`), exactly as no networking lives in `planner.py`.

Confirmed against anthropic SDK 0.107.1:
  - client.beta.agents.list(betas=...)                      -> objects with .id/.name
  - client.beta.agents.retrieve(id, betas=...)              -> BetaManagedAgentsAgent
  - client.beta.skills.versions.retrieve(ver, skill_id=...) -> name/description/directory
  - client.beta.skills.versions.download(ver, skill_id=...) -> BinaryAPIResponse (a zip)

The skill archive *layout* (a zip whose members carry the '<name>/...' prefix) is the
documented bundle shape but has NOT been confirmed against a live download here; the
unpack is defensive and surfaces a diagnostic if it is anything else. Confirm-live-
before-trusting, per the repo's wire-format discipline.
"""
from __future__ import annotations

import io
import zipfile
from typing import Any, Optional

from .anthropic_target import BETAS
from .diagnostics import Diagnostics
from .import_model import ImportedSkill
from .importer import hash_skill_files


def _to_dict(obj: Any) -> dict:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return obj
    return dict(obj)


def _unpack_skill_zip(raw: bytes, fallback_name: str, diags: Diagnostics) -> dict[str, bytes]:
    """Unpack a downloaded skill archive into {arcname: bytes}.

    Members are expected to carry the skill's own '<name>/...' prefix. If the payload
    is not a zip, fall back to a single SKILL.md and flag it.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            files = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}
        if files:
            return files
    except zipfile.BadZipFile:
        pass
    diags.warning(
        "import.skill_archive_shape",
        f"skill download for '{fallback_name}' was not a recognised zip bundle; "
        f"stored as a single SKILL.md (verify the archive layout)",
        fallback_name,
    )
    return {f"{fallback_name}/SKILL.md": raw}


def _reprefix(files: dict[str, bytes], name: str) -> dict[str, bytes]:
    """Re-key every member under '<name>/...', stripping whatever top-level directory
    the archive used. This keeps the skill's folder dir, its `ImportedSkill.name`, and
    the agent's `skills:` reference identical — the parser discovers a skill *by its
    directory name*, so a download whose internal prefix differs from the metadata name
    would otherwise write `skills/<other>/` and leave the `skills: [<name>]` ref dangling.
    """
    out: dict[str, bytes] = {}
    for arcname, data in files.items():
        rel = arcname.split("/", 1)[1] if "/" in arcname else arcname
        out[f"{name}/{rel}"] = data
    return out


def fetch_skill_bundle(client, skill_id: str, version: str, diags: Diagnostics) -> Optional[ImportedSkill]:
    """Download one custom skill's content and metadata into an `ImportedSkill`."""
    meta = _to_dict(client.beta.skills.versions.retrieve(version, skill_id=skill_id, betas=BETAS))
    name = meta.get("name") or meta.get("directory") or skill_id
    resp = client.beta.skills.versions.download(version, skill_id=skill_id, betas=BETAS)
    raw = resp.read() if hasattr(resp, "read") else bytes(resp)
    files = _reprefix(_unpack_skill_zip(raw, name, diags), name)
    return ImportedSkill(name=name, files=files, description=meta.get("description"),
                         content_hash=hash_skill_files(files))


def fetch_anthropic_project(
    client,
    agent_names: Optional[list[str]] = None,
    diags: Optional[Diagnostics] = None,
) -> tuple[list[dict], dict[str, ImportedSkill], Diagnostics]:
    """Fetch agents (and their custom-skill bundles) from a live account.

    Returns (agent_raw_dicts, {skill_id: ImportedSkill}, diagnostics). Pass
    `agent_names` to select a subset; otherwise every agent in the account is read.
    The returned closure always includes any roster subagents referenced by a
    selected coordinator, so subagent delegation re-imports intact.
    """
    diags = diags or Diagnostics()
    listing = [_to_dict(a) for a in client.beta.agents.list(betas=BETAS)]
    by_id = {a["id"]: a for a in listing if a.get("id")}
    by_name = {a.get("name"): a for a in listing if a.get("name")}

    if agent_names:
        wanted = {by_name[n]["id"] for n in agent_names if n in by_name}
        missing = [n for n in agent_names if n not in by_name]
        for n in missing:
            diags.error("import.agent_not_found", f"agent '{n}' not found in the account")
    else:
        wanted = set(by_id)

    # retrieve selected agents, then pull in any roster subagents they reference
    retrieved: dict[str, dict] = {}
    queue = list(wanted)
    while queue:
        aid = queue.pop()
        if aid in retrieved or aid not in by_id:
            continue
        full = _to_dict(client.beta.agents.retrieve(aid, betas=BETAS))
        retrieved[aid] = full
        for ref in (full.get("multiagent") or {}).get("agents") or []:
            rid = ref.get("id") if isinstance(ref, dict) else ref
            if not rid or rid in retrieved:
                continue
            if rid in by_id:
                queue.append(rid)
            else:
                # a referenced roster agent that the listing didn't return (archived, or
                # not visible) — surface it, don't silently drop a subagent from the closure
                diags.warning(
                    "import.subagent_unlisted",
                    f"coordinator '{full.get('name', aid)}' references subagent id '{rid}', "
                    f"which is not in the account listing — it will be left unresolved",
                    full.get("name", ""),
                )

    # download every custom skill referenced by the retrieved closure
    skill_bundles: dict[str, ImportedSkill] = {}
    for full in retrieved.values():
        for sref in full.get("skills") or []:
            if sref.get("type") == "anthropic":
                continue
            sid = sref.get("skill_id") or sref.get("id")
            if sid in skill_bundles:
                continue
            version = sref.get("version") or "latest"
            try:
                bundle = fetch_skill_bundle(client, sid, version, diags)
                if bundle:
                    skill_bundles[sid] = bundle
            except Exception as exc:  # noqa: BLE001 - surface, don't crash the whole import
                diags.warning("import.skill_download_failed",
                              f"could not download skill '{sid}': {exc}")

    return list(retrieved.values()), skill_bundles, diags
