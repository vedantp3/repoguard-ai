"""Scripted providers that inject faults on purpose, for the demo, benchmark and tests.

Each wraps StaticProvider (so tool gates and tool logging still apply) and changes
one behaviour: fail once, always fail, hang, cite the wrong line, or pose as a
paid remote API.
"""
from __future__ import annotations

import asyncio

from agents.provider import ProviderResponse, StaticProvider
from core.schemas import Finding


class FailOnceProvider:
    """Injects one transient failure for one role to exercise bounded retry."""

    label = "static + one injected failure"

    def __init__(self, role: str = "security_auditor"):
        self.inner = StaticProvider()
        self.role = role
        self.failed = False

    async def review(self, role, objective, files, gate=None):
        if role == self.role and not self.failed:
            self.failed = True
            raise RuntimeError("injected transient provider failure")
        return await self.inner.review(role, objective, files, gate=gate)


class AlwaysFailProvider(FailOnceProvider):
    """One role fails on every attempt, so the run must end as partial."""

    label = "static + permanent failure"

    async def review(self, role, objective, files, gate=None):
        if role == self.role:
            raise ConnectionError("injected permanent provider outage")
        return await self.inner.review(role, objective, files, gate=gate)


class SlowProvider(FailOnceProvider):
    """One role hangs far longer than its timeout."""

    label = "static + hanging agent"

    async def review(self, role, objective, files, gate=None):
        if role == self.role:
            await asyncio.sleep(30)
        return await self.inner.review(role, objective, files, gate=gate)


class SloppyCitationProvider:
    """Like a weak model: the bug hunter first cites the wrong line, then fixes it
    when the checker sends the finding back. It also invents one issue that can
    never be evidenced, which the checker must reject at the loop limit."""

    label = "scripted sloppy model"

    def __init__(self):
        self.calls = 0

    async def review(self, role, objective, files, gate=None):
        if role != "bug_hunter":
            return ProviderResponse()
        self.calls += 1
        revising = "sent back" in objective
        right = Finding(id="SLOPPY-div", severity="medium", description="Division by zero when count is 0",
                        file_path="calculator.py", line_numbers=[2 if revising else 6],
                        code_evidence="return total / count", category="bug", confidence=0.8,
                        rationale="average(10, 0) raises ZeroDivisionError.")
        invented = Finding(id="SLOPPY-ghost", severity="high", description="Unchecked recursion depth",
                           file_path="calculator.py", line_numbers=[5], code_evidence="return calculate(expression)",
                           category="bug", confidence=0.7, rationale="Claimed recursion that does not exist.")
        return ProviderResponse(findings=[right, invented], usage={"prompt_tokens": 120, "completion_tokens": 60})


class PaidStubProvider(StaticProvider):
    """Poses as a billed remote API so the approval gate must ask first."""

    is_paid = True
    label = "stub paid API (no real network calls)"
