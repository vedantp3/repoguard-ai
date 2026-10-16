"""Role instructions and tool grants: one place to read what each agent may do."""
from __future__ import annotations

from tools.tool_gate import (
    TOOL_AST_BUG_RULES, TOOL_AST_QUALITY_RULES, TOOL_AST_SECURITY_RULES,
    TOOL_LLM_PLAN, TOOL_LLM_REVIEW, TOOL_READ_FILES, TOOL_SECRET_SCAN,
)

INSTRUCTIONS: dict[str, str] = {
    "planner": (
        "You are the lead reviewer. Split the goal into subtasks for the specialists that are relevant to it. "
        "Only dispatch a specialist when the goal needs that kind of review."
    ),
    "bug_hunter": "Find logic errors, edge cases, and likely runtime failures (category: bug).",
    "security_auditor": "Find vulnerabilities, unsafe input handling, and secret exposure risks (category: security).",
    "quality_reviewer": "Find maintainability and reliability risks with concrete impact (category: quality).",
    "single_agent": "Find correctness (bug), security, and reliability (quality) issues.",
}

TOOLS: dict[str, frozenset[str]] = {
    "planner": frozenset({TOOL_LLM_PLAN}),
    "bug_hunter": frozenset({TOOL_READ_FILES, TOOL_AST_BUG_RULES, TOOL_LLM_REVIEW}),
    "security_auditor": frozenset({TOOL_READ_FILES, TOOL_AST_SECURITY_RULES, TOOL_SECRET_SCAN, TOOL_LLM_REVIEW}),
    "quality_reviewer": frozenset({TOOL_READ_FILES, TOOL_AST_QUALITY_RULES, TOOL_LLM_REVIEW}),
    # The baseline gets the union of the specialists' tools so the comparison is fair.
    "single_agent": frozenset({
        TOOL_READ_FILES, TOOL_AST_BUG_RULES, TOOL_AST_SECURITY_RULES,
        TOOL_SECRET_SCAN, TOOL_AST_QUALITY_RULES, TOOL_LLM_REVIEW,
    }),
    "verifier": frozenset({TOOL_READ_FILES}),
}

SPECIALISTS: tuple[str, ...] = ("bug_hunter", "security_auditor", "quality_reviewer")
CATEGORY_BY_ROLE: dict[str, str] = {"bug_hunter": "bug", "security_auditor": "security", "quality_reviewer": "quality"}
ROLE_BY_CATEGORY: dict[str, str] = {category: role for role, category in CATEGORY_BY_ROLE.items()}
