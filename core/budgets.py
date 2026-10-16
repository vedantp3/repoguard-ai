from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetConfig:
    max_steps: int = 32
    max_model_calls: int = 12
    max_tokens: int = 20_000
    max_cost_usd: float | None = None
    per_agent_cost_usd: float | None = None
    input_cost_per_1k_usd: float | None = None
    output_cost_per_1k_usd: float | None = None
    per_agent_steps: int = 4
    per_agent_calls: int = 4
    per_agent_tokens: int = 12_000
    agent_timeout_seconds: float = 15
    per_agent_time_seconds: float = 45
    max_duration_seconds: float = 120
    max_retries: int = 2
    retry_base_seconds: float = 0.05
    retry_max_seconds: float = 1.0  # cap on one backoff wait (raised for rate-limited APIs)
    max_revisions: int = 1
    completion_token_reserve: int = 512
    max_batch_tokens: int = 6_000  # files are split into batches of about this many input tokens per call


@dataclass
class BudgetUsage:
    steps: int = 0
    model_calls: int = 0
    tokens: int = 0
    estimated_cost_usd: float | None = None
    per_agent: dict[str, dict[str, int]] = field(default_factory=dict)
    per_agent_cost_usd: dict[str, float] = field(default_factory=dict)


class BudgetLedger:
    def __init__(self, config: BudgetConfig):
        if (config.max_cost_usd is not None or config.per_agent_cost_usd is not None) and (
            config.input_cost_per_1k_usd is None or config.output_cost_per_1k_usd is None
        ):
            raise ValueError("a cost limit requires input and output token prices")
        self.config = config
        has_prices = config.input_cost_per_1k_usd is not None and config.output_cost_per_1k_usd is not None
        self.usage = BudgetUsage(estimated_cost_usd=0.0 if has_prices else None)
        self._lock = asyncio.Lock()

    async def reserve_call(self, agent: str, token_reserve: int, cost_reserve: float = 0) -> None:
        async with self._lock:
            local = self.usage.per_agent.setdefault(agent, {"steps": 0, "model_calls": 0, "tokens": 0})
            if self.usage.steps + 1 > self.config.max_steps:
                raise BudgetExceeded("global step budget exhausted")
            if self.usage.model_calls + 1 > self.config.max_model_calls:
                raise BudgetExceeded("global model-call budget exhausted")
            if local["steps"] + 1 > self.config.per_agent_steps:
                raise BudgetExceeded(f"{agent} step budget exhausted")
            if local["model_calls"] + 1 > self.config.per_agent_calls:
                raise BudgetExceeded(f"{agent} model-call budget exhausted")
            if self.usage.tokens + token_reserve > self.config.max_tokens:
                raise BudgetExceeded("global token budget exhausted")
            if local["tokens"] + token_reserve > self.config.per_agent_tokens:
                raise BudgetExceeded(f"{agent} token budget exhausted")
            if self.config.max_cost_usd is not None:
                if self.usage.estimated_cost_usd + cost_reserve > self.config.max_cost_usd:
                    raise BudgetExceeded("cost budget exhausted")
            agent_cost = self.usage.per_agent_cost_usd.get(agent, 0.0)
            if self.config.per_agent_cost_usd is not None:
                if agent_cost + cost_reserve > self.config.per_agent_cost_usd:
                    raise BudgetExceeded(f"{agent} cost budget exhausted")
            self.usage.steps += 1
            self.usage.model_calls += 1
            self.usage.tokens += token_reserve
            local["steps"] += 1
            local["model_calls"] += 1
            local["tokens"] += token_reserve
            if self.usage.estimated_cost_usd is not None:
                self.usage.estimated_cost_usd += cost_reserve
                self.usage.per_agent_cost_usd[agent] = agent_cost + cost_reserve

    async def settle_tokens(self, agent: str, reservation: int, actual: int) -> None:
        async with self._lock:
            delta = actual - reservation
            local = self.usage.per_agent[agent]
            self.usage.tokens += delta
            local["tokens"] += delta

    async def settle_cost(self, agent: str, reservation: float, actual: float | None) -> None:
        if actual is None or self.usage.estimated_cost_usd is None:
            return
        async with self._lock:
            self.usage.estimated_cost_usd += actual - reservation
            self.usage.per_agent_cost_usd[agent] = self.usage.per_agent_cost_usd.get(agent, 0.0) + actual - reservation
