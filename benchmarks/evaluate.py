from __future__ import annotations

from core.orchestrator import ReviewOutcome
from core.security import redact_text


def score(outcome: ReviewOutcome, expected: list[tuple[str, str]], sources: dict[str, str]) -> dict:
    expected_set = set(expected)
    predicted = {(item.file_path, item.category) for item in outcome.findings}
    true_positives = len(predicted & expected_set)
    false_positives = len(predicted - expected_set)
    precision = true_positives / (true_positives + false_positives) if predicted else 0.0
    recall = true_positives / len(expected_set) if expected_set else 1.0
    valid_evidence = 0
    useful = 0
    for finding in outcome.findings:
        source_lines = sources.get(finding.file_path, "").splitlines()
        valid = bool(finding.line_numbers) and all(1 <= line <= len(source_lines) for line in finding.line_numbers)
        valid = valid and any(
            finding.code_evidence.strip() in redact_text(source_lines[line - 1])
            for line in finding.line_numbers if 1 <= line <= len(source_lines)
        )
        valid_evidence += int(valid)
        useful += int(valid and finding.confidence >= 0.7 and len(finding.rationale.strip()) >= 20)
    evidence_ratio = valid_evidence / len(outcome.findings) if outcome.findings else 0.0
    usefulness_ratio = useful / len(outcome.findings) if outcome.findings else 0.0
    components = {
        "correctness": round(30 * precision, 2),
        "issue_coverage": round(30 * recall, 2),
        "evidence_validity": round(25 * evidence_ratio, 2),
        "usefulness": round(15 * usefulness_ratio, 2),
    }
    return {
        "score_100": round(sum(components.values()), 2),
        "components": components,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "expected_issues": len(expected_set),
        "verified_findings": len(outcome.findings),
        "runtime_seconds": outcome.duration_seconds,
        "estimated_tokens": outcome.budget.get("tokens"),
        "model_calls": outcome.budget.get("model_calls"),
        "rejected_by_checker": sum(not item["reason"].startswith("duplicate") for item in outcome.rejected),
        "merged_duplicates": sum(item["reason"].startswith("duplicate") for item in outcome.rejected),
        "cost_usd": outcome.budget.get("estimated_cost_usd"),
        "partial": outcome.partial,
        "agent_errors": [error for agent in outcome.agents for error in agent.errors],
    }