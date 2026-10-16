import asyncio
from pathlib import Path
import tempfile

import pytest

from agents.provider import ProviderResponse, StaticProvider
from core.budgets import BudgetConfig
from core.orchestrator import ReviewOrchestrator
from core.schemas import Finding, ReviewTask
from tools.repository import SafeRepository


FIXTURE = Path(__file__).parents[1] / "fixtures" / "synthetic_repo"


class TrackingProvider:
    def __init__(self, fail_role=None, delay=0):
        self.active = 0
        self.maximum_active = 0
        self.fail_role = fail_role
        self.delay = delay
        self.calls = {}

    async def review(self, role, objective, files, gate=None):
        self.calls[role] = self.calls.get(role, 0) + 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if role == self.fail_role and self.calls[role] == 1:
                raise RuntimeError("injected failure")
            return ProviderResponse()
        finally:
            self.active -= 1


def make_orchestrator(provider, **budget_overrides):
    budget = BudgetConfig(**budget_overrides)
    return ReviewOrchestrator(SafeRepository(FIXTURE), provider, budget, run_dir=tempfile.mkdtemp())


@pytest.mark.asyncio
async def test_specialists_execute_concurrently():
    provider = TrackingProvider(delay=0.03)
    outcome = await make_orchestrator(provider).review()
    assert provider.maximum_active == 3
    assert len(outcome.agents) == 3


@pytest.mark.asyncio
async def test_failure_retries_and_preserves_partial_results():
    provider = TrackingProvider(fail_role="security_auditor")
    outcome = await make_orchestrator(provider, max_retries=1, retry_base_seconds=0).review()
    assert provider.calls["security_auditor"] == 2
    assert not outcome.partial
    assert len(outcome.agents) == 3


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_reported_as_partial():
    provider = TrackingProvider(delay=0.03)
    outcome = await make_orchestrator(
        provider, agent_timeout_seconds=0.005, max_retries=0,
    ).review()
    assert outcome.partial
    assert all("timeout" in result.errors[0] for result in outcome.agents)


@pytest.mark.asyncio
async def test_global_wall_clock_budget_is_enforced():
    outcome = await make_orchestrator(
        TrackingProvider(delay=0.03), agent_timeout_seconds=1,
        max_duration_seconds=0.005, max_retries=0,
    ).review()
    assert outcome.partial
    assert all(result.errors for result in outcome.agents)


@pytest.mark.asyncio
async def test_global_call_budget_limits_parallel_work():
    outcome = await make_orchestrator(
        TrackingProvider(), max_model_calls=2, max_tokens=100_000,
    ).review()
    assert outcome.budget["model_calls"] == 2
    assert sum(bool(agent.errors) for agent in outcome.agents) == 1


@pytest.mark.asyncio
async def test_cost_budget_blocks_calls_when_estimated_reservation_exceeds_cap():
    outcome = await make_orchestrator(
        TrackingProvider(), max_cost_usd=0,
        input_cost_per_1k_usd=1, output_cost_per_1k_usd=1,
    ).review()
    assert outcome.budget["model_calls"] == 0
    assert all("cost budget exhausted" in agent.errors[0] for agent in outcome.agents)


@pytest.mark.asyncio
async def test_per_agent_cost_and_token_limits_are_enforced():
    cost_outcome = await make_orchestrator(
        TrackingProvider(), per_agent_cost_usd=0,
        input_cost_per_1k_usd=1, output_cost_per_1k_usd=1,
    ).review()
    token_outcome = await make_orchestrator(
        TrackingProvider(), max_tokens=0,
    ).review()
    assert cost_outcome.budget["model_calls"] == 0
    assert token_outcome.budget["model_calls"] == 0
    assert all("budget exhausted" in agent.errors[0] for agent in cost_outcome.agents)
    assert all("token budget exhausted" in agent.errors[0] for agent in token_outcome.agents)


@pytest.mark.asyncio
async def test_local_baseline_returns_only_evidence_verified_findings():
    outcome = await make_orchestrator(StaticProvider()).review(ReviewTask())
    assert {item.category for item in outcome.findings} == {"bug", "security", "quality"}
    assert all(item.verification_status == "verified" for item in outcome.findings)


def test_verifier_rejects_fabricated_evidence():
    from agents.verifier import Verifier

    finding = Finding(id="fake", severity="high", description="unsafe", file_path="calculator.py",
                      line_numbers=[1], code_evidence="made_up()", rationale="unsupported", confidence=0.9)
    result = Verifier().verify([finding], {"calculator.py": "def average(total, count):\n    return total / count"})
    assert result.accepted == []
    assert result.revision_requests[0]["finding_id"] == "fake"


class RevisionProvider:
    def __init__(self):
        self.bug_calls = 0

    async def review(self, role, objective, files, gate=None):
        if role != "bug_hunter":
            return ProviderResponse()
        self.bug_calls += 1
        line = 99 if self.bug_calls == 1 else 2
        finding = Finding(
            id="RG-revision", severity="medium", description="Possible division by zero",
            file_path="calculator.py", line_numbers=[line], code_evidence="return total / count",
            rationale="A zero divisor causes a runtime failure in this function.", confidence=0.9,
            category="bug",
        )
        return ProviderResponse(findings=[finding])


@pytest.mark.asyncio
async def test_verifier_revision_request_is_bounded_and_can_repair_evidence(tmp_path):
    provider = RevisionProvider()
    orchestrator = ReviewOrchestrator(
        SafeRepository(FIXTURE), provider, BudgetConfig(max_revisions=1), run_dir=str(tmp_path),
    )
    outcome = await orchestrator.review(ReviewTask(paths=["calculator.py"]))
    assert provider.bug_calls == 2
    assert [finding.id for finding in outcome.findings] == ["RG-revision"]
    assert outcome.revision_requests == []


@pytest.mark.asyncio
async def test_verifier_does_not_exceed_zero_revision_budget(tmp_path):
    provider = RevisionProvider()
    outcome = await ReviewOrchestrator(
        SafeRepository(FIXTURE), provider, BudgetConfig(max_revisions=0), run_dir=str(tmp_path),
    ).review(ReviewTask(paths=["calculator.py"]))
    assert provider.bug_calls == 1
    assert outcome.findings == []
    assert outcome.revision_requests[0]["finding_id"] == "RG-revision"