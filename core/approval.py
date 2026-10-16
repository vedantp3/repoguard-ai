"""Human approval for risky actions.

Two actions are treated as risky: calling a paid, remote model service, and
writing a file outside the ``runs/`` sandbox. Each needs an explicit human
decision. The gate fails closed: with the default ``prompt`` policy and no
interactive terminal, the request is denied.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

PAID_MODEL_CALL = "paid_model_call"
WRITE_OUTSIDE_SANDBOX = "write_outside_sandbox"


class ApprovalPolicy(StrEnum):
    prompt = "prompt"    # ask a human on the terminal; deny when there is none
    approve = "approve"  # pre-approved on the command line (--yes)
    deny = "deny"        # never allow risky actions


@dataclass
class ApprovalGate:
    policy: ApprovalPolicy = ApprovalPolicy.prompt
    input_fn: Callable[[str], str] = input
    is_interactive: Callable[[], bool] = field(default=lambda: sys.stdin.isatty())
    on_event: Callable[..., None] | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def request(self, action: str, summary: str, **detail: Any) -> bool:
        self._emit("approval.requested", action=action, summary=summary, **detail)
        if self.policy == ApprovalPolicy.approve:
            granted, reason = True, "pre-approved with --yes"
        elif self.policy == ApprovalPolicy.deny:
            granted, reason = False, "policy denies risky actions"
        elif not self.is_interactive():
            granted, reason = False, "no interactive terminal to ask (fail closed)"
        else:
            lines = [f"\n[approval needed] {action}: {summary}"]
            lines.extend(f"    {key}: {value}" for key, value in detail.items())
            print("\n".join(lines))
            try:
                answer = self.input_fn("    Approve? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            granted = answer in {"y", "yes"}
            reason = "approved by operator" if granted else "declined by operator"
        decision = {"action": action, "summary": summary, "granted": granted, "reason": reason, **detail}
        self.decisions.append(decision)
        self._emit("approval.granted" if granted else "approval.denied", action=action, reason=reason)
        return granted

    def _emit(self, event: str, **data: Any) -> None:
        if self.on_event is not None:
            self.on_event(event, **data)
