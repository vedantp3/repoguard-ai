from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agents.bug_hunter import BugHunter
from agents.planner import PlannedSubtask, Planner, ReviewPlan
from agents.provider import ReviewProvider
from agents.quality_reviewer import QualityReviewer
from agents.roles import ROLE_BY_CATEGORY
from agents.security_auditor import SecurityAuditor
from agents.specialist import Specialist
from agents.verifier import Verifier
from core.approval import PAID_MODEL_CALL, ApprovalGate
from core.budgets import BudgetConfig, BudgetExceeded, BudgetLedger
from core.event_logger import EventLogger
from core.security import redact_text
from core.schemas import AgentResult, Finding, ReviewTask
from core.state import RunState
from tools.injection_scan import scan_files
from tools.repository import SafeRepository
from tools.tool_gate import PermissionDenied, ToolGate

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
# Retrying cannot fix these, so they fail the agent at once.
NON_RETRYABLE = (PermissionDenied, BudgetExceeded)


@dataclass
class ReviewOutcome:
    run_id: str
    objective: str
    findings: list[Finding] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    revision_requests: list[dict[str, str]] = field(default_factory=list)
    agents: list[AgentResult] = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    duration_seconds: float = 0
    partial: bool = False
    mode: str = "multi_agent"
    provider: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    run_dir: str = "runs"


class ReviewOrchestrator:
    def __init__(self, repository: SafeRepository, provider: ReviewProvider,
                 budget: BudgetConfig | None = None, run_dir: str = "runs",
                 approval: ApprovalGate | None = None):
        self.repository = repository
        self.provider = provider
        self.budget_config = budget or BudgetConfig()
        self.run_dir = run_dir
        self.approval = approval or ApprovalGate()
        self.ledger = BudgetLedger(self.budget_config)
        self._agent_start_times: dict[str, float] = {}
        self._global_deadline = 0.0
        self.planner = Planner()
        self.verifier = Verifier()
        self.specialists = {
            "bug_hunter": BugHunter(provider),
            "security_auditor": SecurityAuditor(provider),
            "quality_reviewer": QualityReviewer(provider),
            "single_agent": Specialist(provider, "single_agent"),
        }

    # ------------------------------------------------------------------ helpers
    def _timeout_for(self, agent_name: str) -> float:
        agent_elapsed = time.perf_counter() - self._agent_start_times.setdefault(agent_name, time.perf_counter())
        return min(
            self.budget_config.agent_timeout_seconds,
            self.budget_config.per_agent_time_seconds - agent_elapsed,
            self._global_deadline - time.monotonic(),
        )

    def _cost_reserve(self, input_tokens: int) -> float:
        config = self.budget_config
        if config.input_cost_per_1k_usd is None or config.output_cost_per_1k_usd is None:
            return 0.0
        return (input_tokens * config.input_cost_per_1k_usd
                + config.completion_token_reserve * config.output_cost_per_1k_usd) / 1000

    def _make_gate(self, agent_name: str, allowed: frozenset[str],
                   logger: EventLogger, state: RunState) -> ToolGate:
        def on_event(event: str, **data: Any) -> None:
            logger.emit(event, **data)
            if event == "tool.call":
                calls = state.data["agents"].get(agent_name, {}).get("tool_calls", [])
                state.agent(agent_name, tool_calls=[*calls, data["tool"]])
        return ToolGate(agent_name, allowed, on_event=on_event)

    async def _settle(self, agent_name: str, usage: dict, token_reserve: int,
                      cost_reserve: float, input_tokens_estimate: int) -> None:
        prompt = int(usage.get("prompt_tokens", usage.get("prompt_tokens_estimated", 0)))
        completion = int(usage.get("completion_tokens", usage.get("completion_tokens_estimated", 0)))
        await self.ledger.settle_tokens(agent_name, token_reserve, prompt + completion)
        actual_cost = usage.get("cost_usd")
        config = self.budget_config
        if actual_cost is None and config.input_cost_per_1k_usd is not None and config.output_cost_per_1k_usd is not None:
            actual_cost = ((prompt or input_tokens_estimate) * config.input_cost_per_1k_usd
                           + completion * config.output_cost_per_1k_usd) / 1000
        await self.ledger.settle_cost(agent_name, cost_reserve, actual_cost)

    # ------------------------------------------------------------------ agents
    async def _run_agent(self, agent_name: str, objective: str, files: list[tuple[str, str]],
                         logger: EventLogger, state: RunState) -> AgentResult:
        agent = self.specialists[agent_name]
        gate = self._make_gate(agent_name, agent.allowed_tools, logger, state)
        serialized_input = json.dumps({"objective": objective, "files": files}, ensure_ascii=True).encode("utf-8")
        input_tokens_estimate = len(serialized_input) // 4 + 256
        token_reserve = input_tokens_estimate + self.budget_config.completion_token_reserve
        cost_reserve = self._cost_reserve(input_tokens_estimate)
        state.agent(agent_name, status="running")
        for attempt in range(self.budget_config.max_retries + 1):
            timeout = self._timeout_for(agent_name)
            if timeout <= 0:
                logger.emit("budget.exhausted", agent=agent_name, reason="time budget exhausted")
                state.agent(agent_name, status="stopped", errors=["time budget exhausted"])
                return AgentResult(agent=agent_name, errors=["time budget exhausted"])
            try:
                await self.ledger.reserve_call(agent_name, token_reserve, cost_reserve)
            except BudgetExceeded as exc:
                logger.emit("budget.exhausted", agent=agent_name, reason=str(exc))
                state.agent(agent_name, status="stopped", errors=[str(exc)])
                return AgentResult(agent=agent_name, errors=[str(exc)])
            started = time.perf_counter()
            state.agent(agent_name, attempts=attempt + 1)
            logger.emit("agent.input", agent=agent_name, objective=objective,
                        attempt=attempt + 1, allowed_tools=sorted(gate.allowed),
                        instructions=agent.instructions, timeout_seconds=round(timeout, 3),
                        files=[{"path": path, "content": content} for path, content in files])
            try:
                result = await asyncio.wait_for(agent.run(objective, files, gate=gate), timeout=timeout)
                await self._settle(agent_name, result.usage, token_reserve, cost_reserve, input_tokens_estimate)
                result.usage = {**result.usage, "attempts": attempt + 1,
                                "duration_seconds": round(time.perf_counter() - started, 6)}
                logger.emit("agent.output", agent=agent_name, attempt=attempt + 1,
                            duration_seconds=result.usage["duration_seconds"],
                            tokens=int(result.usage.get("prompt_tokens", result.usage.get("prompt_tokens_estimated", 0)))
                            + int(result.usage.get("completion_tokens", result.usage.get("completion_tokens_estimated", 0))),
                            result=result.model_dump(mode="json"), budget=self.ledger.usage.__dict__)
                state.agent(agent_name, status="done", findings=len(result.findings))
                state.add_candidates(agent_name, [item.model_dump(mode="json") for item in result.findings])
                return result
            except Exception as exc:
                timed_out = isinstance(exc, TimeoutError)
                reason = "timeout" if timed_out else type(exc).__name__
                attempt_duration = round(time.perf_counter() - started, 6)
                # The reserved call was spent; release unused tokens so the ledger stays honest.
                await self.ledger.settle_tokens(agent_name, token_reserve, 0)
                retryable = not isinstance(exc, NON_RETRYABLE) and getattr(exc, "retryable", True)
                if retryable and attempt < self.budget_config.max_retries:
                    delay = self.budget_config.retry_base_seconds * (2 ** attempt)
                    # A rate-limited API says how long to wait; honour it within the cap.
                    delay = max(delay, getattr(exc, "retry_after", None) or 0)
                    delay = min(delay, self.budget_config.retry_max_seconds, max(self._timeout_for(agent_name), 0))
                    logger.emit("agent.retry", agent=agent_name, attempt=attempt + 1, failure=reason,
                                detail=str(exc)[:200], duration_seconds=attempt_duration, delay_seconds=delay)
                    await asyncio.sleep(delay)
                    continue
                logger.emit("agent.failed", agent=agent_name, attempts=attempt + 1, failure=reason,
                            detail=str(exc)[:200], retryable=retryable, duration_seconds=attempt_duration)
                error = f"{reason} after {attempt + 1} attempt(s)"
                salvaged = getattr(exc, "findings", None)
                if salvaged is not None:
                    # The model failed but the agent's own scanners ran: keep their findings, still marked partial.
                    error += "; model unavailable, returned scanner findings only"
                    logger.emit("agent.degraded", agent=agent_name, findings=len(salvaged), reason=str(exc)[:200])
                    state.agent(agent_name, status="degraded", errors=[error], findings=len(salvaged))
                    state.add_candidates(agent_name, [item.model_dump(mode="json") for item in salvaged])
                    return AgentResult(agent=agent_name, findings=salvaged, errors=[error])
                state.agent(agent_name, status="failed", errors=[error])
                return AgentResult(agent=agent_name, errors=[error])
        return AgentResult(agent=agent_name, errors=["retry loop exhausted"])

    def _batches(self, files: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
        """Group files so each call stays near ``max_batch_tokens`` (4 characters per token)."""
        batches: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        size = 0
        for path, content in files:
            tokens = len(content) // 4 + 1
            if current and size + tokens > self.budget_config.max_batch_tokens:
                batches.append(current)
                current, size = [], 0
            current.append((path, content))
            size += tokens
        return [*batches, current] if current else batches or [[]]

    async def _run_batched(self, agent_name: str, objective: str, files: list[tuple[str, str]],
                           logger: EventLogger, state: RunState) -> AgentResult:
        """Run one specialist over its files, in several calls when they don't fit one."""
        batches = self._batches(files)
        if len(batches) == 1:
            return await self._run_agent(agent_name, objective, files, logger, state)
        logger.emit("agent.batches", agent=agent_name, batches=len(batches),
                    files_per_batch=[len(batch) for batch in batches])
        combined = AgentResult(agent=agent_name)
        for number, batch in enumerate(batches, start=1):
            result = await self._run_agent(agent_name, objective, batch, logger, state)
            combined.findings.extend(result.findings)
            combined.errors.extend(f"batch {number}/{len(batches)}: {error}" for error in result.errors)
            for key, value in result.usage.items():
                if isinstance(value, (int, float)):
                    combined.usage[key] = combined.usage.get(key, 0) + value
            if any("budget" in error for error in result.errors):
                skipped = len(batches) - number
                if skipped:
                    combined.errors.append(f"{skipped} batch(es) skipped after budget stop")
                break
        state.agent(agent_name, status="failed" if combined.errors else "done", findings=len(combined.findings),
                    errors=combined.errors)
        return combined

    async def _plan(self, task: ReviewTask, paths: list[str], logger: EventLogger, state: RunState) -> ReviewPlan:
        model_plan = getattr(self.provider, "plan", None)
        if model_plan is None:
            return self.planner.plan(task, paths)
        gate = self._make_gate("planner", self.planner.allowed_tools, logger, state)
        token_reserve = 1024 + self.budget_config.completion_token_reserve
        cost_reserve = self._cost_reserve(1024)
        state.agent("planner", status="running", attempts=1)
        try:
            await self.ledger.reserve_call("planner", token_reserve, cost_reserve)
            logger.emit("agent.input", agent="planner", objective=task.objective, attempt=1,
                        allowed_tools=sorted(gate.allowed), instructions=self.planner.instructions, files=paths)
            started = time.perf_counter()
            response = await asyncio.wait_for(model_plan(task.objective, paths, gate=gate),
                                              timeout=max(self._timeout_for("planner"), 0.001))
            await self._settle("planner", response["usage"], token_reserve, cost_reserve, 1024)
            plan = self.planner.from_model(response["plan"], task, paths)
            logger.emit("agent.output", agent="planner", attempt=1, duration_seconds=round(time.perf_counter() - started, 6),
                        tokens=sum(response["usage"].values()), result=plan.model_dump(mode="json"),
                        budget=self.ledger.usage.__dict__)
            state.agent("planner", status="done")
            return plan
        except Exception as exc:
            fallback = self.planner.plan(task, paths)
            reason = str(exc) if isinstance(exc, (BudgetExceeded, ValueError)) else type(exc).__name__
            logger.emit("planner.fallback", agent="planner", reason=reason[:200], method=fallback.method)
            state.agent("planner", status="fallback", errors=[reason[:200]])
            return fallback.model_copy(update={"rationale": f"model plan unavailable ({reason[:80]}); {fallback.rationale}"})

    @staticmethod
    def _merge_duplicates(findings: list[Finding]) -> tuple[list[Finding], list[dict[str, str]]]:
        """Merge verified findings that describe the same issue: same file and line, or one line apart
        when the categories match or the kept finding comes from a rule.

        Models often ignore role boundaries, so two specialists report the same problem.
        The rule-based finding wins (deterministic evidence and fix), then the most confident one.
        """
        ranked = sorted(findings, key=lambda item: (item.rule is None, -item.confidence,
                                                    SEVERITY_ORDER[item.severity.value], item.id))
        kept: list[Finding] = []
        merged: list[dict[str, str]] = []
        tailored: set[str] = set()
        for item in ranked:
            # Same line: the same issue even if models disagree on its category. Adjacent line: same category,
            # or bug/security next to a rule finding (models mix those labels up, and often cite the line that builds
            # the value while the rule cites the line that uses it).
            twin = next((other for other in kept if other.file_path == item.file_path and (
                set(item.line_numbers) & set(other.line_numbers)
                or ((other.category == item.category
                     or (other.rule is not None and {other.category, item.category} <= {"bug", "security"}))
                    and any(abs(a - b) <= 1 for a in item.line_numbers for b in other.line_numbers)))), None)
            if twin is None:
                kept.append(item)
                continue
            merged.append({"finding_id": item.id, "kept": twin.id})
            if twin.rule and item.rule is None and item.suggested_fix and twin.id not in tailored:
                # Keep the rule's verified evidence, but prefer the model's fix: it is written for this exact code.
                guidance = (twin.suggested_fix or "").splitlines()
                fix = item.suggested_fix + (f"\n# Rule guidance: {guidance[0].strip()}" if guidance else "")
                kept[kept.index(twin)] = twin.model_copy(update={"suggested_fix": fix})
                tailored.add(twin.id)
        return kept, merged

    @staticmethod
    def _drop_duplicates(requests: list[dict[str, str]], candidates: list[Finding],
                         accepted: dict[str, Finding], rejected: list[dict[str, str]]) -> list[dict[str, str]]:
        """Reject send-backs that restate an issue already accepted, instead of spending a call."""
        covered = {(item.file_path, line, item.category) for item in accepted.values() for line in item.line_numbers}
        by_id = {item.id: item for item in candidates}
        kept = []
        for request in requests:
            finding = by_id.get(request["finding_id"])
            if finding and any((finding.file_path, line, finding.category) in covered for line in finding.line_numbers):
                rejected.append({"finding_id": finding.id, "reason": "duplicate of an accepted finding"})
            else:
                kept.append(request)
        return kept

    @staticmethod
    def _revision_objective(items: list[Finding], wanted: dict[str, dict[str, str]]) -> str:
        claims = "; ".join(
            f"[{item.id}] '{item.description[:120]}' cited lines {item.line_numbers} with evidence "
            f"'{item.code_evidence[:80]}' -> {wanted[item.id]['reason']}" for item in items
        )
        return (f"The checker sent back these findings: {claims}. Re-check each against the numbered source. "
                "Return corrected findings with the exact line number and the exact code on that line, "
                "or drop the finding if it is not real.")

    # ------------------------------------------------------------------ run
    async def review(self, task: ReviewTask | None = None, single_agent: bool = False) -> ReviewOutcome:
        started = time.monotonic()
        duration_started = time.perf_counter()
        self._global_deadline = started + self.budget_config.max_duration_seconds
        task = task or ReviewTask()
        mode = "single_agent" if single_agent else "multi_agent"
        provider_label = getattr(self.provider, "label", type(self.provider).__name__)
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        logger = EventLogger(f"{self.run_dir}/{run_id}.jsonl")
        state = RunState(run_id, redact_text(task.objective), mode, f"{self.run_dir}/{run_id}.state.json")
        self.approval.on_event = logger.emit
        logger.emit("run.started", run_id=run_id, objective=redact_text(task.objective), mode=mode,
                    provider=provider_label, budget_config=self.budget_config.__dict__)

        list_started = time.perf_counter()
        paths = self.repository.list_python_files(task.paths or None)
        logger.emit("tool.list_python_files", agent="harness", duration_seconds=round(time.perf_counter() - list_started, 6),
                    count=len(paths), requested_paths=task.paths)
        snapshots: dict[str, str] = {}
        for path in paths:
            read_started = time.perf_counter()
            snapshots[path] = self.repository.read_file(path)
            logger.emit("tool.read_file", agent="harness", path=path,
                        duration_seconds=round(time.perf_counter() - read_started, 6),
                        bytes=len(snapshots[path].encode("utf-8")))
        state.update(files=paths)

        warnings = scan_files(snapshots)
        for hit in warnings:
            logger.emit("security.untrusted_instruction", **hit,
                        handling="kept as data; flagged to the model; cannot trigger tools")
        state.update(untrusted_content_warnings=warnings)

        outcome = ReviewOutcome(run_id=run_id, objective=task.objective, mode=mode, provider=provider_label,
                                warnings=warnings, run_dir=self.run_dir)

        if getattr(self.provider, "is_paid", False):
            config = self.budget_config
            granted = self.approval.request(
                PAID_MODEL_CALL, f"send {len(paths)} file(s) to {provider_label}",
                max_model_calls=config.max_model_calls, max_tokens=config.max_tokens,
                max_cost_usd=config.max_cost_usd if config.max_cost_usd is not None else "no cost cap set",
            )
            state.update(approvals=self.approval.decisions)
            if not granted:
                return self._finish(outcome, state, logger, duration_started, [], [], [], {},
                                    stop_reason="approval denied for paid model call")

        if single_agent:
            plan = ReviewPlan(subtasks=[PlannedSubtask(agent="single_agent", objective=task.objective, paths=paths)],
                              method="baseline", rationale="single agent, one prompt, all tools")
        else:
            plan = await self._plan(task, paths, logger, state)
        self._agent_start_times.update({subtask.agent: time.perf_counter() for subtask in plan.subtasks})
        logger.emit("planner.output", agent="planner", plan=plan.model_dump(mode="json"), paths=paths)
        state.update(plan=plan.model_dump(mode="json"))
        for subtask in plan.subtasks:
            state.agent(subtask.agent, objective=subtask.objective, allowed_tools=sorted(self.specialists[subtask.agent].allowed_tools))

        safe_files = {path: redact_text(source) for path, source in snapshots.items()}
        calls = [self._run_batched(
            subtask.agent, redact_text(subtask.objective),
            [(path, safe_files[path]) for path in subtask.paths if path in safe_files], logger, state,
        ) for subtask in plan.subtasks]
        results = await asyncio.gather(*calls, return_exceptions=True)
        agent_results: list[AgentResult] = []
        for index, result in enumerate(results):
            if isinstance(result, BaseException):
                agent_results.append(AgentResult(agent=plan.subtasks[index].agent, errors=[type(result).__name__]))
            else:
                agent_results.append(result)

        candidates = list({finding.id: finding for result in agent_results for finding in result.findings}.values())
        verification = self.verifier.verify(candidates, snapshots)
        logger.emit("verification.round", round=0, agent="verifier",
                    accepted=[item.id for item in verification.accepted],
                    rejected=verification.rejected, requests=verification.revision_requests)
        accepted_by_id = {finding.id: finding for finding in verification.accepted}
        rejected = list(verification.rejected)
        revision_requests = list(verification.revision_requests)
        for revision_number in range(self.budget_config.max_revisions):
            revision_requests = self._drop_duplicates(revision_requests, candidates, accepted_by_id, rejected)
            if not revision_requests:
                break
            wanted = {request["finding_id"]: request for request in revision_requests}
            # One call per (specialist, file), listing every claim that needs fixing.
            groups: dict[tuple[str, str], list[Finding]] = {}
            for item in candidates:
                if item.id in wanted and item.file_path in safe_files:
                    groups.setdefault((ROLE_BY_CATEGORY.get(item.category, "quality_reviewer"), item.file_path), []).append(item)
            logger.emit("verification.sent_back", round=revision_number + 1, agent="verifier",
                        findings=[{"id": item.id, "to": role, "reason": wanted[item.id]["reason"]}
                                  for (role, _), items in groups.items() for item in items])
            revised = await asyncio.gather(*[
                self._run_agent(role, self._revision_objective(items, wanted), [(path, safe_files[path])], logger, state)
                for (role, path), items in groups.items()
            ])
            agent_results.extend(revised)
            revision_candidates = [finding for result in revised for finding in result.findings
                                   if finding.id not in accepted_by_id]
            candidates.extend(revision_candidates)
            revision_result = self.verifier.verify(revision_candidates, snapshots)
            accepted_by_id.update({item.id: item for item in revision_result.accepted})
            rejected.extend(revision_result.rejected)
            revision_requests = revision_result.revision_requests
            logger.emit("verification.revision", revision=revision_number + 1, agent="verifier",
                        accepted=[item.id for item in revision_result.accepted],
                        rejected=revision_result.rejected, requests=revision_requests)
            state.data["verification"]["rounds"] = revision_number + 1
        revision_requests = self._drop_duplicates(revision_requests, candidates, accepted_by_id, rejected)
        if revision_requests:
            logger.emit("verification.loop_limit", agent="verifier", max_revisions=self.budget_config.max_revisions,
                        unresolved=[item["finding_id"] for item in revision_requests])
            rejected.extend({"finding_id": item["finding_id"],
                             "reason": f"unresolved after {self.budget_config.max_revisions} revision round(s): {item['reason']}"}
                            for item in revision_requests)
        kept, duplicates = self._merge_duplicates(list(accepted_by_id.values()))
        if duplicates:
            logger.emit("verification.merged_duplicates", agent="verifier", merged=duplicates)
            rejected.extend({"finding_id": item["finding_id"], "reason": f"duplicate of {item['kept']}"} for item in duplicates)
        accepted_by_id = {item.id: item for item in kept}
        logger.emit("verification.decision", agent="verifier", accepted=list(accepted_by_id),
                    rejected=rejected, revision_requests=revision_requests)
        errors = [f"{result.agent}: {error}" for result in agent_results for error in result.errors]
        budget_errors = [error for error in errors if "budget" in error]
        stop_reason = (budget_errors or errors or [None])[0]
        return self._finish(outcome, state, logger, duration_started, agent_results,
                            list(accepted_by_id.values()), rejected, {"plan": plan.model_dump(mode="json"),
                                                                      "revision_requests": revision_requests},
                            stop_reason=stop_reason)

    def _finish(self, outcome: ReviewOutcome, state: RunState, logger: EventLogger, duration_started: float,
                agent_results: list[AgentResult], accepted: list[Finding], rejected: list[dict[str, str]],
                extra: dict[str, Any], stop_reason: str | None) -> ReviewOutcome:
        outcome.duration_seconds = round(time.perf_counter() - duration_started, 6)
        outcome.agents = agent_results
        outcome.findings = sorted(accepted, key=lambda item: (
            SEVERITY_ORDER[item.severity.value], item.file_path, item.line_numbers))
        outcome.rejected = rejected
        outcome.revision_requests = extra.get("revision_requests", [])
        outcome.plan = extra.get("plan", {})
        outcome.budget = self.ledger.usage.__dict__
        outcome.approvals = list(self.approval.decisions)
        outcome.stop_reason = stop_reason
        outcome.partial = stop_reason is not None or any(result.errors for result in agent_results)
        state.data["verification"].update(accepted=[item.id for item in accepted], rejected=rejected,
                                          revision_requests=outcome.revision_requests)
        state.update(status="partial" if outcome.partial else "completed", partial=outcome.partial,
                     stop_reason=stop_reason, budget=outcome.budget, approvals=outcome.approvals,
                     finished_at=datetime.now(timezone.utc).isoformat())
        logger.emit("run.finished", run_id=outcome.run_id, duration_seconds=outcome.duration_seconds,
                    partial=outcome.partial, stop_reason=stop_reason, findings=len(outcome.findings),
                    budget=outcome.budget)
        return outcome
