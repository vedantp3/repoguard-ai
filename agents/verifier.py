from __future__ import annotations

from dataclasses import dataclass, field

from core.schemas import Finding
from core.security import redact_text
from agents.roles import TOOLS


@dataclass
class VerificationResult:
    accepted: list[Finding] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    revision_requests: list[dict[str, str]] = field(default_factory=list)


class Verifier:
    name = "verifier"
    permission = "Verify claims only against the supplied repository snapshot; never edit repository files."
    # The verifier never calls the LLM or runs the AST scanner — evidence
    # checking is deterministic and uses only the repository file contents.
    allowed_tools: frozenset[str] = TOOLS["verifier"]

    def verify(self, findings: list[Finding], files: dict[str, str]) -> VerificationResult:
        result = VerificationResult()
        for finding in findings:
            source = files.get(finding.file_path)
            if source is None:
                result.rejected.append({"finding_id": finding.id, "reason": "file is outside the reviewed snapshot"})
                continue
            lines = source.splitlines()
            if not finding.line_numbers or any(line > len(lines) for line in finding.line_numbers):
                result.revision_requests.append({"finding_id": finding.id, "reason": "line evidence is missing or outside the file"})
                continue
            evidenced = any(
                finding.code_evidence.strip() in redact_text(lines[number - 1])
                for number in finding.line_numbers
            )
            if not evidenced:
                # Send it back: models often cite the right issue with the wrong line.
                result.revision_requests.append({"finding_id": finding.id, "reason": "quoted evidence does not match cited source lines"})
                continue
            result.accepted.append(finding.model_copy(update={"verification_status": "verified"}))
        return result