from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Severity(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    severity: Severity
    description: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    line_numbers: list[int] = Field(default_factory=list)
    code_evidence: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    verification_status: str = "unverified"
    category: str = "quality"
    rule: str | None = None  # static rule id; None for model-produced findings
    suggested_fix: str | None = None  # proposed change; never applied by the harness

    @field_validator("line_numbers")
    @classmethod
    def positive_lines(cls, lines: list[int]) -> list[int]:
        if any(line < 1 for line in lines):
            raise ValueError("line numbers must be positive")
        return lines


class ReviewTask(BaseModel):
    objective: str = "Find correctness, security, and reliability issues."
    paths: list[str] = Field(default_factory=list)


class AgentResult(BaseModel):
    agent: str
    findings: list[Finding] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)