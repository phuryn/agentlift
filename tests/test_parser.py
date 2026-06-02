import os

from agentlift.parser import parse_project, split_frontmatter


def test_split_frontmatter():
    fm, body = split_frontmatter("---\nname: x\nmodel: m\n---\nhello body")
    assert fm == {"name": "x", "model": "m"}
    assert body == "hello body"


def test_split_frontmatter_none():
    fm, body = split_frontmatter("no frontmatter here")
    assert fm == {}
    assert body == "no frontmatter here"


def test_parse_quickstart(examples_dir):
    project, diags = parse_project(os.path.join(examples_dir, "quickstart"))
    assert project.layout == ".managed-agents"
    assert [a.name for a in project.agents] == ["knowledge-agent"]
    a = project.agents[0]
    assert a.model == "claude-haiku-4-5"
    assert a.builtin_tools == ["read", "glob", "grep"]
    assert [s.name for s in a.skills] == ["receipt-stamp"]
    assert any("pm-basics.md" in rel for rel, _ in a.knowledge_files)
    assert diags.ok


def test_parse_team_shared_and_coordinator(examples_dir):
    project, diags = parse_project(os.path.join(examples_dir, "team"))
    names = sorted(a.name for a in project.agents)
    assert names == ["bug-finder", "lead", "researcher"]

    bug = project.agent("bug-finder")
    assert sorted(s.name for s in bug.skills) == ["bug-report", "cite-sources"]
    # cite-sources came from shared/
    assert any(s.name == "cite-sources" and s.shared for s in bug.skills)

    researcher = project.agent("researcher")
    assert [s.name for s in researcher.mcp_servers] == ["docs"]
    assert researcher.mcp_servers[0].transport == "url"
    assert researcher.mcp_servers[0].allowed_tools == ["search"]

    lead = project.agent("lead")
    assert lead.subagents == ["bug-finder", "researcher"]
    assert diags.ok


def test_parse_single_dir_backcompat(fixtures_dir):
    # point straight at one agent folder (e.g. an existing .claude/agents/<name>/);
    # CLAUDE.md, .mcp.json, and .claude/skills/ are all read for back-compat
    project, diags = parse_project(os.path.join(fixtures_dir, "gmail-agent"))
    assert project.layout == "single"
    a = project.agent("gmail-agent")
    assert a is not None
    # skill discovered under .claude/skills
    assert [s.name for s in a.skills] == ["summarize"]
    # stdio MCP parsed as stdio (planner will reject it)
    assert a.mcp_servers[0].name == "gmail"
    assert a.mcp_servers[0].transport == "stdio"
    assert a.mcp_servers[0].allowed_tools == ["search_emails", "read_email"]
