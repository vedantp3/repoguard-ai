import json

import pytest

from agents.provider import StaticProvider
from core.orchestrator import ReviewOrchestrator
from core.reporting import save_report
from core.security import redact_text
from tools.repository import SafeRepository


@pytest.mark.asyncio
async def test_reports_and_jsonl_trace_redact_secret_values(tmp_path):
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    (repository_root / "settings.py").write_text('API_KEY = "assignment-test-secret"\n', encoding="utf-8")
    run_dir = tmp_path / "runs"
    orchestrator = ReviewOrchestrator(SafeRepository(repository_root), StaticProvider(), run_dir=str(run_dir))
    outcome = await orchestrator.review()
    markdown, report_json = save_report(outcome, run_dir)
    trace_path = run_dir / f"{outcome.run_id}.jsonl"
    persisted = markdown.read_text(encoding="utf-8") + report_json.read_text(encoding="utf-8") + trace_path.read_text(encoding="utf-8")
    assert "assignment-test-secret" not in persisted
    assert "[REDACTED]" in persisted
    assert any(item.description == "Possible hardcoded credential" for item in outcome.findings)
    events = [json.loads(line)["event"] for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert "agent.input" in events
    assert "agent.output" in events
    assert "verification.decision" in events


@pytest.mark.asyncio
async def test_single_agent_baseline_returns_findings_in_one_agent_call(tmp_path):
    repository = SafeRepository(__import__("pathlib").Path(__file__).parents[1] / "fixtures" / "synthetic_repo")
    outcome = await ReviewOrchestrator(repository, StaticProvider(), run_dir=str(tmp_path)).review(single_agent=True)
    assert [item.agent for item in outcome.agents] == ["single_agent"]
    assert {item.category for item in outcome.findings} == {"bug", "security", "quality"}


def test_redaction_covers_suffixed_keys_and_private_key_blocks():
    source = 'MY_API_KEY = "hidden-value"\n-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----'
    cleaned = redact_text(source)
    assert "hidden-value" not in cleaned
    assert "secret material" not in cleaned
    assert "MY_API_KEY = \"[REDACTED]\"" in cleaned


def test_redaction_keeps_environment_lookups_but_masks_values():
    assert redact_text('DB_PASSWORD = os.getenv("DB_PASSWORD")') == 'DB_PASSWORD = os.getenv("DB_PASSWORD")'
    assert redact_text("api_key = settings['API_KEY']") == "api_key = settings['API_KEY']"
    assert redact_text("TOKEN = None") == "TOKEN = None"
    assert redact_text('DB_PASSWORD = "hunter2"') == 'DB_PASSWORD = "[REDACTED]"'
    assert "eyJhbGciOi" not in redact_text("token=eyJhbGciOiJIUzI1NiJ9.e30.abc")
