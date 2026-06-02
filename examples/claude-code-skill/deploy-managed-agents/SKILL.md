---
name: deploy-managed-agents
description: Deploy, run, list, or tear down the agents in this repo's .managed-agents/ folder on Anthropic Managed Agents. Use when the user says "deploy my agents", "ship this agent to the cloud", "run the deployed agent", or asks about managed-agent deployment.
---

# Deploy managed agents (agentlift)

This repo defines deployable agents under `.managed-agents/`. Use the `agentlift`
CLI to push them to Anthropic's Managed Agents cloud and run them by ID. The user
shouldn't need to learn it — translate their intent into the right command.

Prerequisite: `pip install agentlift` and `ANTHROPIC_API_KEY` set (it's in `.env`).

## What the user wants -> what to run

- "show me what would deploy" / "dry run" -> `agentlift plan .`
- "is this valid?" -> `agentlift validate .`   (exit 1 means there are errors to fix)
- "deploy" / "ship to the cloud" -> `agentlift deploy . --yes`
  - then report the agent IDs printed, and that `.agentlift-lock.json` was written.
- "redeploy" / "I changed an agent" -> `agentlift deploy . --yes --prune`
  - idempotent: unchanged skills/agents are skipped automatically.
- "run <agent> with <task>" -> `agentlift run <agent> --project . --task "<task>"`
- "run it locally" / "same agent on my machine" -> add `--local`
- "what's deployed?" -> `agentlift list .`
- "tear it down" / "delete the agents" -> `agentlift destroy . --yes`

## Rules

- Always run `agentlift plan .` first and show the user the plan before `deploy`.
  Surface any `ERROR` diagnostics (e.g. a stdio MCP server, which can't deploy)
  and stop — don't pass `--skip-unsupported` unless the user agrees to drop those.
- After deploying, the `.agentlift-lock.json` maps each agent to its remote ID.
  Tell the user to commit it so re-deploys are idempotent and `run` works for the
  whole team.
- Never put `ANTHROPIC_API_KEY` on the command line or in a committed file.
