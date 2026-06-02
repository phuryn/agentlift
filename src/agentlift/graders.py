"""Graders for evaluating agent output.

Two flavors:
  - `substring_grader` — deterministic, offline-safe (exact phrases must / must not appear)
  - `llm_grader` — an LLM judges open-ended output against a rubric (used for
    identity, tone, "answered only from knowledge", etc.)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class Grade:
    passed: bool
    reason: str


def substring_grader(
    output: str,
    must_include: Optional[list[str]] = None,
    must_exclude: Optional[list[str]] = None,
) -> Grade:
    o = (output or "").lower()
    for s in must_include or []:
        if s.lower() not in o:
            return Grade(False, f"missing required phrase: {s!r}")
    for s in must_exclude or []:
        if s.lower() in o:
            return Grade(False, f"contains forbidden phrase: {s!r}")
    return Grade(True, "all substring checks passed")


_GRADER_SYSTEM = (
    "You are a strict evaluator. Given a task, an agent's answer, and a rubric, "
    "decide if the answer satisfies the rubric. Respond with ONLY a JSON object: "
    '{"pass": true|false, "reason": "<one sentence>"}. No other text.'
)


def llm_grader(client, task: str, answer: str, rubric: str, model: str = "claude-haiku-4-5") -> Grade:
    prompt = (
        f"TASK:\n{task}\n\nAGENT ANSWER:\n{answer}\n\nRUBRIC:\n{rubric}\n\n"
        "Return the JSON verdict now."
    )
    resp = client.messages.create(
        model=model, max_tokens=300, system=_GRADER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    # tolerate code fences / stray text around the JSON
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
        return Grade(bool(data.get("pass")), str(data.get("reason", "")))
    except (json.JSONDecodeError, ValueError):
        return Grade(False, f"grader returned unparseable output: {text[:160]}")
