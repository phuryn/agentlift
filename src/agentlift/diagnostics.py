"""Structured warnings and errors surfaced during parse and plan.

A plan with any `error` diagnostics is not deployable; warnings are advisory
(things agentlift transformed or dropped, with the reason).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Diagnostic:
    level: str   # "error" | "warning" | "info"
    code: str    # short machine code, e.g. "mcp.stdio_unsupported"
    message: str
    where: str = ""  # agent name / file, for context

    def render(self) -> str:
        tag = {"error": "ERROR", "warning": "warn", "info": "info"}[self.level]
        loc = f" [{self.where}]" if self.where else ""
        return f"  {tag}{loc}: {self.message}"


@dataclass
class Diagnostics:
    items: list[Diagnostic] = field(default_factory=list)

    def error(self, code: str, message: str, where: str = "") -> None:
        self.items.append(Diagnostic("error", code, message, where))

    def warning(self, code: str, message: str, where: str = "") -> None:
        self.items.append(Diagnostic("warning", code, message, where))

    def info(self, code: str, message: str, where: str = "") -> None:
        self.items.append(Diagnostic("info", code, message, where))

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.items if d.level == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.items if d.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        if not self.items:
            return "  (no diagnostics)"
        return "\n".join(d.render() for d in self.items)
