"""Offline link integrity for README.md + docs/*.md (CI-run, no network): every
relative path target exists, and every intra-repo `#anchor` resolves to a real heading
(GitHub slug rules). External http(s) URLs are out of scope here (network); they're
checked manually. Guards against the kind of stale cross-reference that creeps in when
headings get renamed."""
import os
import re

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _slug(heading: str) -> str:
    h = heading.strip().lower().replace("`", "")
    h = re.sub(r"[^\w\s-]", "", h)
    return h.replace(" ", "-")


def _headings(path: str) -> set[str]:
    out: set[str] = set()
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^#{1,6}\s+(.*)", line.rstrip())
        if m:
            out.add(_slug(m.group(1)))
    return out


def _targets(path: str) -> list[str]:
    text = open(path, encoding="utf-8").read()
    body = re.sub(r"```.*?```", "", text, flags=re.DOTALL)   # ignore code fences
    return re.findall(r"\]\(([^)\s]+)\)", body)


def _md_files() -> list[str]:
    files = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "CLAUDE.md")]
    docs = os.path.join(ROOT, "docs")
    for fn in sorted(os.listdir(docs)):
        if fn.endswith(".md"):
            files.append(os.path.join(docs, fn))
    return [f for f in files if os.path.isfile(f)]


@pytest.mark.parametrize("md", _md_files(), ids=lambda p: os.path.relpath(p, ROOT))
def test_doc_links_resolve(md):
    base = os.path.dirname(md)
    self_anchors = _headings(md)
    broken: list[str] = []
    for raw in _targets(md):
        if raw.startswith(("http://", "https://", "mailto:")):
            continue
        if raw.startswith("#"):
            if raw[1:] not in self_anchors:
                broken.append(f"self-anchor {raw}")
            continue
        path, _, anchor = raw.partition("#")
        fp = os.path.normpath(os.path.join(base, path))
        if not os.path.exists(fp):
            broken.append(f"missing path: {raw}")
            continue
        if anchor and fp.lower().endswith(".md") and anchor not in _headings(fp):
            broken.append(f"missing anchor: {raw}")
    assert not broken, f"{os.path.relpath(md, ROOT)} has broken links:\n  " + "\n  ".join(broken)
