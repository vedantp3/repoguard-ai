"""Per-agent tool allowlist enforcement.

Each agent declares an ``allowed_tools`` frozenset.  Before any tool is
invoked, the provider calls ``ToolGate.check(tool_name)`` so that a
mis-configured or compromised agent cannot use a tool it was not granted.
Violations raise ``PermissionDenied`` rather than silently proceeding.

Every check (allowed or denied) and every tool result is reported to an
optional ``on_event`` callback, which the orchestrator wires to the JSONL
trace so a run shows exactly which agent called which tool and what came back.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Canonical tool names used across providers and agent declarations.
TOOL_READ_FILES: str = "read_python_files"
TOOL_AST_BUG_RULES: str = "ast_bug_rules"            # division, syntax errors
TOOL_AST_SECURITY_RULES: str = "ast_security_rules"  # eval/exec, shell execution
TOOL_SECRET_SCAN: str = "secret_scan"                # hardcoded credentials
TOOL_AST_QUALITY_RULES: str = "ast_quality_rules"    # mutable defaults
TOOL_LLM_REVIEW: str = "llm_review"
TOOL_LLM_PLAN: str = "llm_plan"

STATIC_RULE_TOOLS: frozenset[str] = frozenset({
    TOOL_AST_BUG_RULES, TOOL_AST_SECURITY_RULES, TOOL_SECRET_SCAN, TOOL_AST_QUALITY_RULES,
})
ALL_TOOLS: frozenset[str] = frozenset({TOOL_READ_FILES, TOOL_LLM_REVIEW, TOOL_LLM_PLAN}) | STATIC_RULE_TOOLS

EventSink = Callable[..., None]


class PermissionDenied(RuntimeError):
    """Raised when an agent attempts to use a tool outside its allowlist."""


class ToolGate:
    """Enforces a per-agent tool allowlist at runtime.

    >>> gate = ToolGate("bug_hunter", frozenset({"read_python_files", "ast_bug_rules"}))
    >>> gate.check("ast_bug_rules")         # OK — no exception
    >>> gate.check("secret_scan")           # raises PermissionDenied
    """

    def __init__(self, agent: str, allowed: frozenset[str], on_event: EventSink | None = None) -> None:
        self.agent = agent
        self.allowed = frozenset(allowed)
        self.on_event = on_event
        self.calls: list[str] = []

    def _emit(self, event: str, **data: Any) -> None:
        if self.on_event is not None:
            self.on_event(event, agent=self.agent, **data)

    def check(self, tool: str) -> None:
        """Raise ``PermissionDenied`` if *tool* is not in the allowlist."""
        if tool not in self.allowed:
            self._emit("tool.denied", tool=tool, allowed_tools=sorted(self.allowed))
            raise PermissionDenied(
                f"Agent '{self.agent}' is not permitted to use tool '{tool}'. "
                f"Allowed: {sorted(self.allowed)}"
            )
        self.calls.append(tool)
        self._emit("tool.call", tool=tool)

    def record(self, tool: str, seconds: float, **summary: Any) -> None:
        """Log what a permitted tool returned (counts, not raw content)."""
        self._emit("tool.result", tool=tool, duration_seconds=round(seconds, 6), **summary)

    @classmethod
    def permissive(cls, agent: str) -> "ToolGate":
        """Return a gate that permits every defined tool (backward-compat helper)."""
        return cls(agent, ALL_TOOLS)
