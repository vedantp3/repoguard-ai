from __future__ import annotations

import json
from pathlib import Path

from core.approval import WRITE_OUTSIDE_SANDBOX, ApprovalGate
from core.event_logger import EventLogger
from core.orchestrator import ReviewOutcome
from core.security import redact_object
from core.trace_view import write_html


def save_report(outcome: ReviewOutcome, output_dir: str | Path) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    payload = redact_object({
        "run_id": outcome.run_id,
        "objective": outcome.objective,
        "duration_seconds": outcome.duration_seconds,
        "partial": outcome.partial,
        "stop_reason": outcome.stop_reason,
        "mode": outcome.mode,
        "provider": outcome.provider,
        "plan": outcome.plan,
        "untrusted_content_warnings": outcome.warnings,
        "approvals": outcome.approvals,
        "findings": [item.model_dump(mode="json") for item in outcome.findings],
        "rejected": outcome.rejected,
        "revision_requests": outcome.revision_requests,
        "agents": [item.model_dump(mode="json") for item in outcome.agents],
        "budget": outcome.budget,
    })
    json_path = output / f"{outcome.run_id}.json"
    markdown_path = output / f"{outcome.run_id}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    lines = [f"# RepoGuard Review: {payload['run_id']}", ""]
    if payload["partial"]:
        lines.extend([f"> **PARTIAL RESULT** - {payload['stop_reason'] or 'one or more agents failed'}. "
                      "Findings below are verified but coverage is incomplete.", ""])
    lines.extend([f"Objective: {payload['objective']}", f"Mode: {payload['mode']} | Provider: {payload['provider']}",
                  f"Duration: {payload['duration_seconds']:.3f}s", f"Partial result: {payload['partial']}", ""])
    plan = payload["plan"] or {}
    if plan.get("subtasks"):
        lines.extend([f"## Plan ({plan.get('method')}: {plan.get('rationale')})", ""])
        lines.extend(f"- `{item['agent']}`: {item['objective']} ({len(item['paths'])} file(s))" for item in plan["subtasks"])
        lines.append("")
    if payload["untrusted_content_warnings"]:
        lines.extend(["## Untrusted Content Warnings", "",
                      "These lines look like instructions to the agents. They were treated as data only.", ""])
        lines.extend(f"- `{item['file']}:{item['line']}` {', '.join(item['patterns'])}: `{item['text']}`"
                     for item in payload["untrusted_content_warnings"])
        lines.append("")
    if payload["approvals"]:
        lines.extend(["## Approvals", ""])
        lines.extend(f"- {item['action']}: {'GRANTED' if item['granted'] else 'DENIED'} ({item['reason']})"
                     for item in payload["approvals"])
        lines.append("")
    lines.extend([f"## Findings ({len(payload['findings'])})", ""])
    for finding in payload["findings"]:
        locations = ", ".join(str(number) for number in finding["line_numbers"])
        lines.extend([
            f"### {finding['severity'].upper()}: {finding['description']}",
            f"- ID: `{finding['id']}` | Confidence: {finding['confidence']:.0%} | Verification: {finding['verification_status']}",
            f"- Location: `{finding['file_path']}:{locations}`",
            f"- Evidence: `{finding['code_evidence']}`",
            f"- Rationale: {finding['rationale']}",
        ])
        if finding.get("suggested_fix"):
            lines.extend(["- Suggested fix (not applied):", "", "```python", finding["suggested_fix"], "```"])
        lines.append("")
    if not payload["findings"]:
        lines.append("No verified findings.")
    lines.extend(["", "## Verification", ""])
    lines.extend(f"- Rejected `{item['finding_id']}`: {item['reason']}" for item in payload["rejected"])
    lines.extend(f"- Revision requested for `{item['finding_id']}`: {item['reason']}"
                 for item in payload["revision_requests"])
    if not payload["rejected"] and not payload["revision_requests"]:
        lines.append("All reported candidates with supported evidence were accepted; no revision remains open.")
    lines.extend(["", "## Execution", "", f"- Budget usage: `{json.dumps(payload['budget'], sort_keys=True)}`"])
    lines.extend(f"- {agent['agent']}: {len(agent['findings'])} candidate(s), {len(agent['errors'])} error(s)"
                 for agent in payload["agents"])
    lines.append(f"- Trace: `{outcome.run_id}.jsonl` | State: `{outcome.run_id}.state.json` | Viewer: `{outcome.run_id}.html`")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    trace = Path(outcome.run_dir) / f"{outcome.run_id}.jsonl"
    if trace.exists():
        write_html(trace)
    return markdown_path, json_path


def export_report(markdown_path: Path, destination: Path, sandbox: Path,
                  approval: ApprovalGate, trace_path: Path) -> Path | None:
    """Copy a report to ``destination``. Writing outside the sandbox needs approval."""
    target = destination.resolve()
    if target.is_dir():
        target = target / markdown_path.name
    approval.on_event = EventLogger(trace_path).emit
    try:
        target.relative_to(sandbox.resolve())
        inside = True
    except ValueError:
        inside = False
    if not inside and not approval.request(WRITE_OUTSIDE_SANDBOX, f"write report to {target}",
                                           sandbox=str(sandbox.resolve())):
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown_path.read_text(encoding="utf-8"), encoding="utf-8")
    approval.on_event("file.write", agent="harness", path=str(target), inside_sandbox=inside)
    return target