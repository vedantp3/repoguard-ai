"""Human-readable views of a run trace: a terminal timeline and a static HTML page.

Both are rebuilt purely from the saved JSONL trace (plus the state file when
present), so inspecting or replaying a run never calls a model.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def load_trace(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _offset(event: dict[str, Any], start: datetime) -> float:
    return (datetime.fromisoformat(event["timestamp"]) - start).total_seconds()


def summarize(event: dict[str, Any]) -> str:
    """One-line description of an event for the timeline."""
    kind = event["event"]
    if kind == "run.started":
        return f"mode={event.get('mode')} provider={event.get('provider')} goal={event.get('objective')!r}"
    if kind == "tool.read_file":
        return f"{event.get('path')} ({event.get('bytes')} bytes)"
    if kind == "tool.list_python_files":
        return f"{event.get('count')} file(s)"
    if kind == "security.untrusted_instruction":
        return f"{event.get('file')}:{event.get('line')} {event.get('patterns')} -> {event.get('text')!r}"
    if kind.startswith("approval."):
        return f"{event.get('action')}: {event.get('summary') or event.get('reason')}"
    if kind == "planner.output":
        plan = event.get("plan", {})
        agents = ", ".join(item["agent"] for item in plan.get("subtasks", []))
        return f"[{plan.get('method')}] -> {agents} ({plan.get('rationale')})"
    if kind == "planner.fallback":
        return f"model plan rejected: {event.get('reason')}"
    if kind == "agent.input":
        files = event.get("files", [])
        names = [item["path"] if isinstance(item, dict) else item for item in files]
        return f"attempt {event.get('attempt')} tools={event.get('allowed_tools')} files={names}"
    if kind == "tool.call":
        return f"{event.get('tool')}"
    if kind == "tool.denied":
        return f"DENIED {event.get('tool')} (allowed: {event.get('allowed_tools')})"
    if kind == "tool.result":
        extras = {key: value for key, value in event.items()
                  if key not in {"timestamp", "event", "agent", "tool", "duration_seconds"}}
        return f"{event.get('tool')} {event.get('duration_seconds')}s {extras}"
    if kind == "agent.output":
        found = len(event.get("result", {}).get("findings", []))
        return f"{found} finding(s), {event.get('tokens')} tokens, {event.get('duration_seconds')}s"
    if kind in {"agent.retry", "agent.failed"}:
        return f"attempt {event.get('attempt', event.get('attempts'))}: {event.get('failure')} {event.get('detail', '')}".strip()
    if kind == "agent.degraded":
        return f"kept {event.get('findings')} scanner finding(s); model failed: {event.get('reason')}"
    if kind == "verification.merged_duplicates":
        return f"merged {len(event.get('merged', []))} duplicate finding(s)"
    if kind == "budget.exhausted":
        return str(event.get("reason"))
    if kind.startswith("verification."):
        parts = []
        for key in ("accepted", "rejected", "requests", "revision_requests", "findings", "unresolved"):
            if event.get(key):
                parts.append(f"{key}={len(event[key])}")
        return " ".join(parts) or "nothing to verify"
    if kind == "run.finished":
        budget = event.get("budget", {})
        return (f"partial={event.get('partial')} findings={event.get('findings')} calls={budget.get('model_calls')} "
                f"tokens={budget.get('tokens')} {event.get('duration_seconds')}s stop={event.get('stop_reason')}")
    return ""


def render_timeline(events: list[dict[str, Any]], agent: str | None = None) -> str:
    if not events:
        return "(empty trace)"
    start = datetime.fromisoformat(events[0]["timestamp"])
    rows = []
    for event in events:
        who = event.get("agent", "harness" if not event["event"].startswith("run.") else "run")
        if agent and who != agent:
            continue
        rows.append(f"{_offset(event, start):8.3f}s  {who:<17} {event['event']:<32} {summarize(event)}")
    return "\n".join(rows)


_STYLE = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--ok:#1f7a4d;--warn:#a65d00;--bad:#b42318;--accent:#3554d1}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;--ok:#4cc38a;--warn:#f0a648;--bad:#f2766b;--accent:#8ea2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.card b{display:block;font-size:20px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
code,pre{font:12px/1.45 ui-monospace,Consolas,monospace}pre{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;max-height:320px;overflow:auto}
.pill{display:inline-block;border-radius:99px;padding:1px 8px;font-size:12px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.scroll{overflow-x:auto}
details summary{cursor:pointer}.ev-agent{font-weight:600}
"""
_BAD = {"agent.failed", "budget.exhausted", "tool.denied", "approval.denied", "verification.loop_limit"}
_WARN = {"agent.retry", "security.untrusted_instruction", "planner.fallback", "verification.sent_back", "approval.requested"}


def _e(value: Any) -> str:
    return html.escape(str(value))


def render_html(events: list[dict[str, Any]], state: dict[str, Any] | None = None) -> str:
    state = state or {}
    finished = next((e for e in reversed(events) if e["event"] == "run.finished"), {})
    started = next((e for e in events if e["event"] == "run.started"), {})
    budget = finished.get("budget", {})
    partial = finished.get("partial")
    status = ("<span class='pill bad'>PARTIAL</span>" if partial else "<span class='pill ok'>COMPLETE</span>") if finished else "<span class='pill warn'>RUNNING / CRASHED</span>"
    cards = [("Status", status), ("Verified findings", finished.get("findings", "-")),
             ("Model calls", budget.get("model_calls", "-")), ("Tokens", budget.get("tokens", "-")),
             ("Seconds", finished.get("duration_seconds", "-")), ("Stop reason", finished.get("stop_reason") or "none")]
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Run {_e(state.get('run_id', started.get('run_id', '')))}</title><style>{_STYLE}</style></head><body><main>",
             f"<h1>RepoGuard run {_e(started.get('run_id', ''))}</h1>",
             f"<div class='muted'>Goal: {_e(started.get('objective', ''))} &middot; mode {_e(started.get('mode', ''))} &middot; provider {_e(started.get('provider', ''))}</div>",
             "<div class='cards'>" + "".join(f"<div class='card'><span class='muted'>{_e(k)}</span><b>{v if k == 'Status' else _e(v)}</b></div>" for k, v in cards) + "</div>"]

    agents = state.get("agents", {})
    if agents:
        parts.append("<h2>Agents (shared state)</h2><div class='scroll'><table><tr><th>Agent</th><th>Status</th><th>Attempts</th><th>Candidates</th><th>Tools called</th><th>Errors</th></tr>")
        for name, info in agents.items():
            cls = {"done": "ok", "failed": "bad", "stopped": "bad", "fallback": "warn"}.get(info.get("status"), "")
            parts.append(f"<tr><td class='ev-agent'>{_e(name)}</td><td class='{cls}'>{_e(info.get('status'))}</td><td>{_e(info.get('attempts'))}</td>"
                         f"<td>{_e(info.get('findings'))}</td><td><code>{_e(', '.join(info.get('tool_calls', [])))}</code></td><td class='bad'>{_e('; '.join(info.get('errors', [])))}</td></tr>")
        parts.append("</table></div>")

    decision = next((e for e in reversed(events) if e["event"] == "verification.decision"), None)
    candidates = state.get("candidates", {})
    if decision is not None:
        parts.append("<h2>Findings</h2><div class='scroll'><table><tr><th>Severity</th><th>Location</th><th>Finding</th><th>Evidence</th><th>Suggested fix</th><th>By</th></tr>")
        for finding_id in decision.get("accepted", []):
            item = candidates.get(finding_id, {"id": finding_id})
            lines = ",".join(str(n) for n in item.get("line_numbers", []))
            parts.append(f"<tr><td>{_e(item.get('severity', ''))}</td><td><code>{_e(item.get('file_path', ''))}:{_e(lines)}</code></td>"
                         f"<td>{_e(item.get('description', finding_id))}</td><td><code>{_e(item.get('code_evidence', ''))}</code></td>"
                         f"<td><pre>{_e(item.get('suggested_fix') or '')}</pre></td><td>{_e(item.get('proposed_by', ''))}</td></tr>")
        for rejected in decision.get("rejected", []):
            parts.append(f"<tr class='bad'><td>rejected</td><td colspan='5'>{_e(rejected.get('finding_id'))}: {_e(rejected.get('reason'))}</td></tr>")
        parts.append("</table></div>")

    if not events:
        parts.append("<p>Empty trace.</p>")
    else:
        start = datetime.fromisoformat(events[0]["timestamp"])
        parts.append("<h2>Timeline</h2><div class='scroll'><table><tr><th>t+s</th><th>Agent</th><th>Event</th><th>Detail</th></tr>")
        for event in events:
            cls = "bad" if event["event"] in _BAD else "warn" if event["event"] in _WARN else ""
            payload = json.dumps({k: v for k, v in event.items() if k not in {"timestamp", "event"}}, indent=2, default=str)
            parts.append(f"<tr><td><code>{_offset(event, start):.3f}</code></td><td class='ev-agent'>{_e(event.get('agent', ''))}</td>"
                         f"<td class='{cls}'><code>{_e(event['event'])}</code></td><td>{_e(summarize(event))}"
                         f"<details><summary class='muted'>payload</summary><pre>{_e(payload)}</pre></details></td></tr>")
        parts.append("</table></div>")
    parts.append("</main></body></html>")
    return "\n".join(parts)


def write_html(trace_path: str | Path) -> Path:
    trace_path = Path(trace_path)
    state_path = trace_path.with_suffix(".state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    output = trace_path.with_suffix(".html")
    output.write_text(render_html(load_trace(trace_path), state), encoding="utf-8")
    return output
