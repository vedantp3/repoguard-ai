from __future__ import annotations

from agents.provider import ReviewProvider
from core.schemas import AgentResult
from agents.roles import INSTRUCTIONS, TOOLS
from tools.tool_gate import ToolGate


class Specialist:
    """Base class for all specialist review agents.

    Subclasses declare ``allowed_tools`` to restrict which tool operations
    their provider call may perform.  The orchestrator builds a ``ToolGate``
    from that set and passes it into ``run()`` so enforcement happens at the
    provider level before any tool executes.
    """

    name = "specialist"
    permission = "Read only the explicitly provided Python files through the repository reader."
    instructions: str = INSTRUCTIONS["single_agent"]
    # Subclasses narrow this; the default is the single-agent baseline's tool set.
    allowed_tools: frozenset[str] = TOOLS["single_agent"]

    def __init__(self, provider: ReviewProvider, name: str) -> None:
        self.provider = provider
        self.name = name

    async def run(self, objective: str, files: list[tuple[str, str]],
                  gate: ToolGate | None = None) -> AgentResult:
        response = await self.provider.review(self.name, objective, files, gate=gate)
        return AgentResult(agent=self.name, findings=response.findings, usage=response.usage)