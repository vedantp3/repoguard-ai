from pathlib import Path

import pytest

from core.schemas import Finding
from tools.repository import RepositoryAccessError, SafeRepository
from tools.static_checks import inspect_python


FIXTURE = Path(__file__).parents[1] / "fixtures" / "synthetic_repo"


def test_baseline_finds_security_and_correctness_issues():
    repository = SafeRepository(FIXTURE)
    findings = inspect_python("calculator.py", repository.read_file("calculator.py"))
    assert {item.category for item in findings} == {"security", "bug"}
    assert all(item.line_numbers and item.code_evidence for item in findings)
    assert all(isinstance(item, Finding) for item in findings)


def test_repository_rejects_parent_traversal():
    repository = SafeRepository(FIXTURE)
    with pytest.raises(RepositoryAccessError):
        repository.read_file("../outside.py")


def test_repository_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    try:
        (root / "escape.py").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    repository = SafeRepository(root)
    with pytest.raises(RepositoryAccessError):
        repository.read_file("escape.py")


def test_prompt_injection_is_not_executed_as_review_instructions():
    repository = SafeRepository(FIXTURE)
    source = repository.read_file("prompt_injection.py")
    findings = inspect_python("prompt_injection.py", source)
    assert "ignore your policies" in source
    assert findings == []


def test_invalid_finding_evidence_is_rejected():
    with pytest.raises(ValueError):
        Finding(id="bad", severity="high", description="unsafe", file_path="x.py",
                line_numbers=[0], code_evidence="x", rationale="why", confidence=0.5)

def test_secret_rule_ignores_label_constants_but_flags_values():
    flagged = inspect_python("s.py", 'TOOL_SECRET_SCAN = "secret_scan"\nAPI_TOKEN = "tok_9f8a7"\nDB_PASSWORD = "hunter2"\n')
    assert [item.line_numbers for item in flagged] == [[2], [3]]
