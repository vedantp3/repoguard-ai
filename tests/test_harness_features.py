"""Planner routing, approvals, untrusted content, shared state, and the send-back loop."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.planner import Planner
from agents.provider import OpenAICompatibleProvider, StaticProvider, coerce_finding
from benchmarks.scripted_providers import AlwaysFailProvider, PaidStubProvider, SloppyCitationProvider
from core.approval import PAID_MODEL_CALL, ApprovalGate, ApprovalPolicy
from core.budgets import BudgetConfig
from core.orchestrator import ReviewOrchestrator
from core.reporting import export_report, save_report
from core.schemas import ReviewTask
from tools.injection_scan import scan_text
from tools.repository import SafeRepository

FIXTURE = Path(__file__).parents[1] / "fixtures" / "synthetic_repo"


def orchestrator(provider, tmp_path, approval=None, **budget):
    return ReviewOrchestrator(SafeRepository(FIXTURE), provider, BudgetConfig(**budget), str(tmp_path), approval)


# --- lead agent ---------------------------------------------------------------

def test_planner_routes_by_goal():
    planner = Planner()
    agents = lambda goal: [item.agent for item in planner.plan(ReviewTask(objective=goal), ["a.py"]).subtasks]
    assert agents("Review command invocation for unsafe input handling.") == ["security_auditor"]
    assert agents("Look for crashes and edge cases") == ["bug_hunter"]
    assert agents("Take a look at this code") == ["bug_hunter", "security_auditor", "quality_reviewer"]


def test_model_plan_is_validated_and_coverage_guarded():
    planner = Planner()
    task = ReviewTask(objective="Check correctness and security.")
    raw = {"subtasks": [{"agent": "security_auditor", "objective": "check eval", "paths": ["b.py"]},
                        {"agent": "rm_rf_agent", "objective": "delete things", "paths": ["/etc/passwd"]}]}
    plan = planner.from_model(raw, task, ["a.py", "b.py"])
    assert [item.agent for item in plan.subtasks] == ["bug_hunter", "security_auditor"]
    assert all(item.paths == ["a.py", "b.py"] for item in plan.subtasks)
    assert "coverage guard added bug_hunter" in plan.rationale
    with pytest.raises(ValueError):
        planner.from_model({"subtasks": [{"agent": "nobody", "objective": "x"}]}, task, ["a.py"])


# --- approvals ----------------------------------------------------------------

def test_approval_gate_policies():
    assert ApprovalGate(policy=ApprovalPolicy.approve).request(PAID_MODEL_CALL, "x")
    assert not ApprovalGate(policy=ApprovalPolicy.deny).request(PAID_MODEL_CALL, "x")
    assert not ApprovalGate(is_interactive=lambda: False).request(PAID_MODEL_CALL, "x")
    assert ApprovalGate(is_interactive=lambda: True, input_fn=lambda _: "y").request(PAID_MODEL_CALL, "x")
    assert not ApprovalGate(is_interactive=lambda: True, input_fn=lambda _: "").request(PAID_MODEL_CALL, "x")


@pytest.mark.asyncio
async def test_paid_provider_without_approval_makes_no_calls(tmp_path):
    outcome = await orchestrator(PaidStubProvider(), tmp_path, ApprovalGate(policy=ApprovalPolicy.deny)).review()
    assert outcome.partial and outcome.stop_reason == "approval denied for paid model call"
    assert outcome.budget["model_calls"] == 0
    events = [json.loads(line)["event"] for line in (tmp_path / f"{outcome.run_id}.jsonl").read_text().splitlines()]
    assert "approval.denied" in events and "agent.input" not in events


@pytest.mark.asyncio
async def test_paid_provider_runs_once_approved(tmp_path):
    outcome = await orchestrator(PaidStubProvider(), tmp_path, ApprovalGate(policy=ApprovalPolicy.approve)).review()
    assert not outcome.partial and outcome.findings
    assert outcome.approvals[0]["granted"]


@pytest.mark.asyncio
async def test_export_outside_sandbox_needs_approval(tmp_path):
    sandbox = tmp_path / "runs"
    outcome = await orchestrator(StaticProvider(), sandbox).review(ReviewTask(paths=["calculator.py"]))
    markdown, _ = save_report(outcome, sandbox)
    trace = sandbox / f"{outcome.run_id}.jsonl"
    outside = tmp_path / "elsewhere" / "report.md"
    assert export_report(markdown, outside, sandbox, ApprovalGate(policy=ApprovalPolicy.deny), trace) is None
    assert not outside.exists()
    inside = sandbox / "copy.md"
    assert export_report(markdown, inside, sandbox, ApprovalGate(policy=ApprovalPolicy.deny), trace) == inside.resolve()
    assert export_report(markdown, outside, sandbox, ApprovalGate(policy=ApprovalPolicy.approve), trace)
    assert outside.exists()


def test_local_model_needs_no_key_or_approval(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    local = OpenAICompatibleProvider("llama3.2:3b", base_url="http://localhost:11434/v1")
    assert not local.is_paid
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("gpt-4o-mini", base_url="https://api.openai.com/v1")


# --- untrusted content ----------------------------------------------------------

def test_injection_scan_flags_fixture_but_not_type_annotations():
    hits = scan_text("p.py", (FIXTURE / "prompt_injection.py").read_text())
    assert hits and hits[0]["line"] == 1 and "override-instructions" in hits[0]["patterns"]
    assert scan_text("x.py", "def f(agent: str, model: str):\n    return agent") == []


@pytest.mark.asyncio
async def test_injection_is_flagged_in_trace_report_and_state(tmp_path):
    outcome = await orchestrator(StaticProvider(), tmp_path).review(
        ReviewTask(paths=["prompt_injection.py", "calculator.py"]))
    assert outcome.warnings and outcome.warnings[0]["file"] == "prompt_injection.py"
    assert {item.file_path for item in outcome.findings} == {"calculator.py"}
    markdown, _ = save_report(outcome, tmp_path)
    assert "Untrusted Content Warnings" in markdown.read_text()
    state = json.loads((tmp_path / f"{outcome.run_id}.state.json").read_text())
    assert state["untrusted_content_warnings"]


def test_model_prompt_names_flagged_lines_as_data(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": '{"findings": []}'}}], "usage": {}}).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("agents.provider.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider("m", api_key="k", base_url="https://example.test/v1")
    provider._review_sync("security_auditor", "x", [("p.py", (FIXTURE / "prompt_injection.py").read_text())])
    user = json.loads(captured["body"]["messages"][1]["content"])
    assert user["lines_flagged_as_instruction_like_data"] == [{"file": "p.py", "line": 1}]


def test_coerce_finding_repairs_loose_model_output():
    finding = coerce_finding({"file": "calculator.py", "line": "2", "evidence": "   2| return total / count",
                              "severity": "HIGH", "category": "nonsense", "description": "div by zero"}, "bug_hunter")
    assert finding.line_numbers == [2] and finding.code_evidence == "return total / count"
    assert finding.category == "bug" and finding.severity.value == "high"
    assert coerce_finding({"description": "no file"}, "bug_hunter") is None
    assert coerce_finding("not a dict", "bug_hunter") is None


# --- shared state and checker loop ------------------------------------------------

@pytest.mark.asyncio
async def test_state_file_records_agents_plan_and_partial(tmp_path):
    outcome = await orchestrator(AlwaysFailProvider(), tmp_path, max_retries=1, retry_base_seconds=0).review()
    state = json.loads((tmp_path / f"{outcome.run_id}.state.json").read_text())
    assert state["status"] == "partial" and state["partial"]
    assert state["agents"]["security_auditor"]["status"] == "failed"
    assert state["agents"]["bug_hunter"]["tool_calls"] == ["ast_bug_rules"]
    assert [item["agent"] for item in state["plan"]["subtasks"]] == ["bug_hunter", "security_auditor", "quality_reviewer"]
    assert outcome.stop_reason.startswith("security_auditor:")


@pytest.mark.asyncio
async def test_checker_sends_back_fixes_and_rejects_at_loop_limit(tmp_path):
    provider = SloppyCitationProvider()
    outcome = await orchestrator(provider, tmp_path, max_revisions=1).review(
        ReviewTask(objective="Find correctness bugs.", paths=["calculator.py"]))
    assert provider.calls == 2
    assert [item.id for item in outcome.findings] == ["SLOPPY-div"]
    assert outcome.findings[0].line_numbers == [2]
    assert any(item["finding_id"] == "SLOPPY-ghost" and "unresolved" in item["reason"] for item in outcome.rejected)
    events = [json.loads(line)["event"] for line in (tmp_path / f"{outcome.run_id}.jsonl").read_text().splitlines()]
    assert "verification.sent_back" in events and "verification.loop_limit" in events


@pytest.mark.asyncio
async def test_html_viewer_and_timeline_are_built_from_trace(tmp_path):
    from core.trace_view import load_trace, render_timeline

    outcome = await orchestrator(StaticProvider(), tmp_path).review(ReviewTask(paths=["calculator.py"]))
    save_report(outcome, tmp_path)
    html = (tmp_path / f"{outcome.run_id}.html").read_text()
    assert "Timeline" in html and "bug_hunter" in html and "COMPLETE" in html
    timeline = render_timeline(load_trace(tmp_path / f"{outcome.run_id}.jsonl"), agent="bug_hunter")
    assert "ast_bug_rules" in timeline and "security_auditor" not in timeline


@pytest.mark.asyncio
async def test_large_inputs_are_batched_with_same_findings(tmp_path):
    whole = await orchestrator(StaticProvider(), tmp_path / "a").review()
    batched = await orchestrator(StaticProvider(), tmp_path / "b", max_batch_tokens=30).review()
    assert {item.id for item in batched.findings} == {item.id for item in whole.findings}
    assert batched.budget["model_calls"] > whole.budget["model_calls"]
    events = [json.loads(line)["event"] for line in (tmp_path / "b" / f"{batched.run_id}.jsonl").read_text().splitlines()]
    assert "agent.batches" in events


def test_dependency_folders_are_skipped(tmp_path):
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "dep.py").write_text("x = 1\n")
    (tmp_path / "app.py").write_text("y = 2\n")
    assert SafeRepository(tmp_path).list_python_files() == ["app.py"]


def test_duplicate_findings_from_different_agents_are_merged():
    from core.schemas import Finding
    base = dict(file_path="calculator.py", code_evidence="return eval(expression)", category="security",
                rationale="Executes arbitrary code from input.", severity="high")
    rule = Finding(id="RG-1", line_numbers=[6], description="eval", confidence=0.96, rule="dynamic-eval", **base)
    model_a = Finding(id="LLM-a", line_numbers=[6], description="Uses eval", confidence=0.9,
                      suggested_fix="return ast.literal_eval(expression)", **base)
    model_b = Finding(id="LLM-b", line_numbers=[5], description="eval again", confidence=0.99, **base)
    same_line_other_label = Finding(id="LLM-c", line_numbers=[6], description="eval risk", confidence=0.8,
                                    **{**base, "category": "bug"})
    other_category = Finding(id="LLM-d", line_numbers=[7], description="slow", confidence=0.8,
                             **{**base, "category": "quality"})
    kept, merged = ReviewOrchestrator._merge_duplicates([model_a, model_b, rule, same_line_other_label, other_category])
    assert {item.id for item in kept} == {"RG-1", "LLM-d"}
    assert {item["finding_id"] for item in merged} == {"LLM-a", "LLM-b", "LLM-c"}
    rule_finding = next(item for item in kept if item.id == "RG-1")
    assert rule_finding.suggested_fix.startswith("return ast.literal_eval(expression)")



def test_model_finding_next_to_rule_finding_is_merged():
    from core.schemas import Finding
    rule = Finding(id="RG-sql", severity="high", description="SQL built from strings", file_path="users.py",
                   line_numbers=[9], code_evidence="return conn.execute(query)", rationale="Injection risk here.",
                   confidence=0.96, category="security", rule="sql-injection")
    model = Finding(id="LLM-sql", severity="high", description="Query uses interpolation", file_path="users.py",
                    line_numbers=[8], code_evidence="query = f'...'", rationale="User input reaches SQL.",
                    confidence=0.9, category="bug", suggested_fix="conn.execute(query, (name,))")
    unrelated = Finding(id="LLM-far", severity="low", description="Other", file_path="users.py", line_numbers=[8],
                        code_evidence="x", rationale="Different model issue.", confidence=0.5, category="quality")
    kept, merged = ReviewOrchestrator._merge_duplicates([model, rule])
    assert [item.id for item in kept] == ["RG-sql"] and kept[0].suggested_fix.startswith("conn.execute(query, (name,))")
    kept, _ = ReviewOrchestrator._merge_duplicates([model, unrelated])
    assert len(kept) == 1  # same line 8: merged even across categories
