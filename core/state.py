"""Shared run state: the one place every agent's progress is written.

The orchestrator is the only writer. After every change the whole state is
rewritten atomically to ``<run_id>.state.json``, so the file always shows the
latest consistent picture of the run, even if the process dies mid-run.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.security import redact_object


class RunState:
    def __init__(self, run_id: str, goal: str, mode: str, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = {
            "run_id": run_id, "goal": goal, "mode": mode, "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
            "files": [], "plan": None, "agents": {}, "candidates": {},
            "verification": {"accepted": [], "rejected": [], "revision_requests": [], "rounds": 0},
            "untrusted_content_warnings": [], "approvals": [], "budget": {}, "partial": False,
            "stop_reason": None,
        }
        self.save()

    def update(self, **fields: Any) -> None:
        self.data.update(fields)
        self.save()

    def agent(self, name: str, **fields: Any) -> None:
        entry = self.data["agents"].setdefault(name, {
            "status": "pending", "attempts": 0, "errors": [], "findings": 0, "tool_calls": [],
        })
        entry.update(fields)
        self.save()

    def add_candidates(self, agent: str, findings: list[dict[str, Any]]) -> None:
        for finding in findings:
            self.data["candidates"][finding["id"]] = {**finding, "proposed_by": agent}
        self.save()

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(redact_object(self.data), indent=2, default=str), encoding="utf-8")
        os.replace(temporary, self.path)
