from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agents.roles import INSTRUCTIONS, SPECIALISTS, TOOLS
from core.schemas import ReviewTask


class PlannedSubtask(BaseModel):
    agent: str
    objective: str
    paths: list[str] = Field(default_factory=list)


class ReviewPlan(BaseModel):
    subtasks: list[PlannedSubtask]
    method: str = "keyword"   # "keyword" or "model"
    rationale: str = ""


FOCUS = {
    "bug_hunter": "Focus: correctness, edge cases, runtime failures.",
    "security_auditor": "Focus: vulnerabilities and unsafe input handling.",
    "quality_reviewer": "Focus: maintainability and reliability.",
}
KEYWORDS = {
    "bug_hunter": r"bug|correct|edge|crash|logic|runtime|error|exception|divi|overflow|off-by",
    "security_auditor": r"secur|vulnerab|unsafe|inject|secret|credential|shell|eval|attack|exploit|sanitiz|input|auth",
    "quality_reviewer": r"quality|maintain|reliab|smell|refactor|readab|state|leak|design|style|test",
}


class Planner:
    """Lead agent: turns a plain-English goal into specialist subtasks.

    With a model provider it asks the model for a plan and validates it (known
    agents only, files from the snapshot only). Otherwise, or when the model
    plan is invalid, it routes by keywords. A goal that matches no keyword gets
    all three specialists, so nothing is silently skipped.
    """

    name = "planner"
    instructions = INSTRUCTIONS["planner"]
    allowed_tools = TOOLS["planner"]

    def plan(self, task: ReviewTask, available_paths: list[str]) -> ReviewPlan:
        paths = task.paths or available_paths
        goal = task.objective.lower()
        chosen = [agent for agent in SPECIALISTS if re.search(KEYWORDS[agent], goal)]
        rationale = f"goal mentions {', '.join(chosen)} keywords" if chosen else "no focus keywords; using every specialist"
        return ReviewPlan(subtasks=[
            PlannedSubtask(agent=agent, objective=f"{task.objective} {FOCUS[agent]}", paths=paths)
            for agent in chosen or list(SPECIALISTS)
        ], method="keyword", rationale=rationale)

    def from_model(self, raw: Any, task: ReviewTask, available_paths: list[str]) -> ReviewPlan:
        """Validate a model-proposed plan; raise ValueError if it is unusable."""
        allowed_paths = set(task.paths or available_paths)
        try:
            proposed = ReviewPlan.model_validate({"subtasks": raw.get("subtasks", [])})
        except (AttributeError, ValidationError) as exc:
            raise ValueError(f"plan does not match the schema: {type(exc).__name__}") from exc
        # Every specialist reviews every in-scope file: a small model that shards
        # files between agents silently drops coverage, so the model only chooses
        # who runs and how to phrase their focus.
        scope = sorted(allowed_paths)
        subtasks: dict[str, PlannedSubtask] = {}
        for subtask in proposed.subtasks:
            if subtask.agent not in SPECIALISTS or subtask.agent in subtasks:
                continue
            subtasks[subtask.agent] = PlannedSubtask(
                agent=subtask.agent, objective=f"{subtask.objective.strip()[:400]} {FOCUS[subtask.agent]}", paths=scope,
            )
        if not subtasks:
            raise ValueError("plan names no known specialist")
        # Coverage guard: a specialist the goal explicitly asks for is never dropped.
        required = self.plan(task, available_paths)
        added = [item.agent for item in required.subtasks
                 if item.agent not in subtasks and required.rationale.startswith("goal mentions")]
        for item in required.subtasks:
            if item.agent in added:
                subtasks[item.agent] = item.model_copy(update={"paths": scope})
        ordered = [subtasks[agent] for agent in SPECIALISTS if agent in subtasks]
        rationale = "validated model plan" + (f"; coverage guard added {', '.join(added)}" if added else "")
        return ReviewPlan(subtasks=ordered, method="model", rationale=rationale)
