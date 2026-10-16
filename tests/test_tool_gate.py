"""Tests for per-agent tool-gate enforcement.

These tests verify that:
1. A gate permits allowed tools and raises PermissionDenied on disallowed ones.
2. The Verifier is restricted to read-only tools (no LLM, no AST scanner).
3. StaticProvider raises PermissionDenied when an agent's gate blocks ast_analysis.
4. The prompt_injection fixture is processed as inert data — its hostile comment
   does not alter the harness output.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from agents.provider import StaticProvider
from agents.verifier import Verifier
from core.budgets import BudgetConfig
from core.orchestrator import ReviewOrchestrator
from core.schemas import ReviewTask
from tools.repository import SafeRepository
from tools.tool_gate import (
    ALL_TOOLS,
    STATIC_RULE_TOOLS,
    TOOL_AST_BUG_RULES,
    TOOL_AST_QUALITY_RULES,
    TOOL_AST_SECURITY_RULES,
    TOOL_LLM_REVIEW,
    TOOL_SECRET_SCAN,
    TOOL_READ_FILES,
    PermissionDenied,
    ToolGate,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "synthetic_repo"


# ---------------------------------------------------------------------------
# Unit tests for ToolGate itself
# ---------------------------------------------------------------------------

def test_gate_permits_allowed_tool() -> None:
    gate = ToolGate("bug_hunter", frozenset({TOOL_AST_BUG_RULES}))
    gate.check(TOOL_AST_BUG_RULES)  # must not raise


def test_gate_raises_permission_denied_for_disallowed_tool() -> None:
    gate = ToolGate("read_only_agent", frozenset({TOOL_READ_FILES}))
    with pytest.raises(PermissionDenied, match="read_only_agent"):
        gate.check(TOOL_LLM_REVIEW)


def test_gate_error_message_lists_allowed_tools() -> None:
    gate = ToolGate("my_agent", frozenset({TOOL_READ_FILES}))
    with pytest.raises(PermissionDenied, match=TOOL_READ_FILES):
        gate.check(TOOL_AST_BUG_RULES)


def test_permissive_gate_allows_all_tools() -> None:
    gate = ToolGate.permissive("single_agent")
    for tool in ALL_TOOLS:
        gate.check(tool)  # none should raise


# ---------------------------------------------------------------------------
# Verifier is restricted to read-only (no LLM, no AST scanner)
# ---------------------------------------------------------------------------

def test_verifier_allowed_tools_excludes_llm_and_ast() -> None:
    assert TOOL_LLM_REVIEW not in Verifier.allowed_tools
    assert not (STATIC_RULE_TOOLS & Verifier.allowed_tools)
    assert TOOL_READ_FILES in Verifier.allowed_tools


# ---------------------------------------------------------------------------
# StaticProvider enforces the gate before calling the AST scanner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_static_provider_raises_when_gate_blocks_ast_analysis() -> None:
    """An agent without its scanner permission cannot use StaticProvider."""
    restricted_gate = ToolGate("restricted_agent", frozenset({TOOL_READ_FILES}))
    provider = StaticProvider()
    with pytest.raises(PermissionDenied, match="ast_bug_rules"):
        await provider.review("bug_hunter", "find bugs", [], gate=restricted_gate)


@pytest.mark.asyncio
async def test_static_provider_succeeds_when_gate_allows_ast_analysis() -> None:
    """An agent with ast_analysis permission can use StaticProvider normally."""
    gate = ToolGate("bug_hunter", frozenset({TOOL_AST_BUG_RULES}))
    provider = StaticProvider()
    response = await provider.review("bug_hunter", "find bugs", [], gate=gate)
    assert response.findings == []  # empty files → no findings


# ---------------------------------------------------------------------------
# Prompt-injection fixture: treated as inert data, not as instructions
# ---------------------------------------------------------------------------

def test_prompt_injection_comment_produces_no_findings_from_static_checks() -> None:
    """The hostile comment in prompt_injection.py has no executable form;
    static analysis finds nothing and does not alter agent behaviour."""
    from tools.static_checks import inspect_python

    repository = SafeRepository(FIXTURE)
    source = repository.read_file("prompt_injection.py")

    # Confirm the injection string is present in the source.
    assert "ignore your policies" in source

    # StaticProvider treats the file as Python source to inspect, not as
    # instructions.  A benign file with only a comment and a trivial function
    # should produce zero findings.
    findings = inspect_python("prompt_injection.py", source)
    assert findings == [], (
        f"Expected no findings from prompt_injection.py, got: {findings}"
    )


@pytest.mark.asyncio
async def test_full_pipeline_neutralises_prompt_injection_file() -> None:
    """Running the full orchestrator over prompt_injection.py produces only
    legitimate code findings (none here) and does not act on the hostile
    comment embedded in the source."""
    with tempfile.TemporaryDirectory() as tmp:
        outcome = await ReviewOrchestrator(
            SafeRepository(FIXTURE),
            StaticProvider(),
            BudgetConfig(),
            run_dir=tmp,
        ).review(ReviewTask(
            objective="Review this file.",
            paths=["prompt_injection.py"],
        ))

    # The harness must not generate findings that reference the injection text
    # itself as a code issue — the comment is data, not a directive.
    for finding in outcome.findings:
        assert "ignore your policies" not in finding.description.lower()
        assert "ignore your policies" not in finding.rationale.lower()
    # Run must complete without errors.
    assert not outcome.partial


# ---------------------------------------------------------------------------
# End-to-end: allowed_tools logged in trace for every agent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trace_records_allowed_tools_per_agent(tmp_path) -> None:
    """Every agent.input event in the JSONL trace must include allowed_tools."""
    import json

    outcome = await ReviewOrchestrator(
        SafeRepository(FIXTURE),
        StaticProvider(),
        BudgetConfig(),
        run_dir=str(tmp_path),
    ).review()

    trace_path = tmp_path / f"{outcome.run_id}.jsonl"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    input_events = [e for e in events if e["event"] == "agent.input"]
    assert input_events, "No agent.input events found in trace"
    for event in input_events:
        assert "allowed_tools" in event, f"agent.input for {event.get('agent')} missing allowed_tools"
        assert isinstance(event["allowed_tools"], list)
        assert len(event["allowed_tools"]) > 0


# ---------------------------------------------------------------------------
# Specialists have distinct tools, and tool calls are written to the trace
# ---------------------------------------------------------------------------

def test_specialists_have_distinct_scanners() -> None:
    from agents.bug_hunter import BugHunter
    from agents.quality_reviewer import QualityReviewer
    from agents.security_auditor import SecurityAuditor

    scanners = {cls.__name__: cls.allowed_tools & STATIC_RULE_TOOLS
                for cls in (BugHunter, SecurityAuditor, QualityReviewer)}
    assert scanners == {
        "BugHunter": {TOOL_AST_BUG_RULES},
        "SecurityAuditor": {TOOL_AST_SECURITY_RULES, TOOL_SECRET_SCAN},
        "QualityReviewer": {TOOL_AST_QUALITY_RULES},
    }


@pytest.mark.asyncio
async def test_bug_hunter_cannot_borrow_security_scanner() -> None:
    gate = ToolGate("bug_hunter", frozenset({TOOL_AST_BUG_RULES}))
    with pytest.raises(PermissionDenied, match="ast_security_rules"):
        await StaticProvider().review("security_auditor", "x", [], gate=gate)


@pytest.mark.asyncio
async def test_trace_records_each_tool_call_and_result(tmp_path) -> None:
    import json

    outcome = await ReviewOrchestrator(
        SafeRepository(FIXTURE), StaticProvider(), BudgetConfig(), run_dir=str(tmp_path),
    ).review(ReviewTask(paths=["calculator.py"]))
    events = [json.loads(line) for line in (tmp_path / f"{outcome.run_id}.jsonl").read_text().splitlines()]
    calls = {(e["agent"], e["tool"]) for e in events if e["event"] == "tool.call"}
    assert ("bug_hunter", TOOL_AST_BUG_RULES) in calls
    assert ("security_auditor", TOOL_SECRET_SCAN) in calls
    assert all(agent != "bug_hunter" or tool != TOOL_SECRET_SCAN for agent, tool in calls)
    assert any(e["event"] == "tool.result" and "findings" in e for e in events)
