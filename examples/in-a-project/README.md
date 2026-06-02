# Example: `.managed-agents/` embedded in a real project

This folder is a stand-in for an actual project. It has the things a real codebase
has, plus a deploy folder. (It's a normal project with a `.managed-agents/`
subfolder — not a nested git repo.)

```
in-a-project/
├── CLAUDE.md                      # repo-level instructions  (LOCAL — never deployed)
├── src/app.py                     # application code         (LOCAL — never read)
├── .claude/
│   └── agents/
│       └── pr-reviewer.md         # a native Claude subagent (LOCAL — never deployed)
└── .managed-agents/               # ONLY this is deployed
    ├── shared/skills/
    │   ├── cite-sources/SKILL.md  # shared by researcher + fact-checker
    │   └── house-style/SKILL.md   # shared by researcher + summarizer
    ├── orchestrator/agent.md      # coordinator -> researcher, summarizer, fact-checker
    ├── researcher/agent.md
    ├── summarizer/agent.md
    └── fact-checker/agent.md
```

Two things this demonstrates:

**Isolation.** Run `skylift plan .` here. The plan contains exactly the four agents
under `.managed-agents/`. The repo's `CLAUDE.md`, `src/app.py`, and the local
`.claude/agents/pr-reviewer.md` subagent are never read, never uploaded, never in a
deployed agent's context. A cloud agent gets only its own folder (plus `shared/`).

**A set of subagents with shared skills.** `orchestrator` is a coordinator over a
roster of three. Two skills are shared across the roster and upload **once** each:

```console
$ skylift plan .
Skills to upload: 2
  - cite-sources  (…)  used by: researcher, fact-checker
  - house-style   (…)  used by: researcher, summarizer
Agents to create: 4
  - researcher    [claude-haiku-4-5]   tools: builtins:read/web_search
  - summarizer    [claude-haiku-4-5]   tools: builtins:read
  - fact-checker  [claude-haiku-4-5]   tools: builtins:read/web_search
  - orchestrator  [claude-haiku-4-5]   (coordinator -> @agent:researcher, @agent:summarizer, @agent:fact-checker)
Deployable: yes
```

Deploy it like any other project:

```bash
skylift diff .            # what would change
skylift deploy . --yes
skylift run orchestrator --project . --task "Is the Earth's core solid? Cite sources."
skylift destroy . --yes
```
