"""Gemini provider wiring, provider error handling, .env loading, and suggested fixes."""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from agents.provider import OpenAICompatibleProvider, RateLimited, coerce_finding, parse_json_reply
from agents.provider import StaticProvider
from core.budgets import BudgetConfig
from core.env import load_env_file
from core.orchestrator import ReviewOrchestrator
from core.reporting import save_report
from core.schemas import ReviewTask
from tools.repository import SafeRepository
from tools.static_checks import inspect_python

FIXTURE = Path(__file__).parents[1] / "fixtures" / "synthetic_repo"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": self.content}}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://x", code, "err", headers or {}, io.BytesIO(b""))


def test_gemini_provider_uses_its_own_key_and_is_treated_as_remote(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    provider = OpenAICompatibleProvider("gemini-2.5-flash", base_url=GEMINI_URL, key_env="GEMINI_API_KEY")
    assert provider.url == GEMINI_URL + "/chat/completions"
    assert provider.is_paid  # code leaves the machine, so approval is required
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        OpenAICompatibleProvider("gemini-2.5-flash", base_url=GEMINI_URL, key_env="GEMINI_API_KEY")


def test_rate_limit_becomes_retryable_error_with_wait(monkeypatch):
    def fake_urlopen(request, timeout):
        raise http_error(429, {"Retry-After": "7"})

    monkeypatch.setattr("agents.provider.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider("m", api_key="k", base_url=GEMINI_URL)
    with pytest.raises(RateLimited) as caught:
        provider._chat("s", "u", 10)
    assert caught.value.retry_after == 7


def test_json_mode_rejection_falls_back_to_plain_prompt(monkeypatch):
    bodies = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data)
        bodies.append(body)
        if "response_format" in body:
            raise http_error(400)
        return FakeResponse('```json\n{"findings": []}\n```')

    monkeypatch.setattr("agents.provider.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider("m", api_key="k", base_url=GEMINI_URL)
    response = provider._review_sync("bug_hunter", "x", [("a.py", "x = 1\n")])
    assert response.findings == [] and len(bodies) == 2 and not provider.json_mode


def test_empty_model_reply_is_an_error_not_a_silent_pass(monkeypatch):
    monkeypatch.setattr("agents.provider.urllib.request.urlopen", lambda request, timeout: FakeResponse(None))
    provider = OpenAICompatibleProvider("m", api_key="k", base_url=GEMINI_URL)
    with pytest.raises(RuntimeError, match="no content"):
        provider._chat("s", "u", 10)


def test_parse_json_reply_tolerates_fences_and_chatter():
    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_reply('Here you go: {"b": 2} hope it helps') == {"b": 2}


def test_env_file_fills_missing_variables_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ALREADY_SET", "shell")
    monkeypatch.delenv("FROM_FILE", raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nFROM_FILE="abc"\nALREADY_SET=file\nEMPTY=\n', encoding="utf-8")
    assert load_env_file(env) == ["FROM_FILE"]
    import os
    assert os.environ["FROM_FILE"] == "abc" and os.environ["ALREADY_SET"] == "shell"
    assert load_env_file(tmp_path / "missing.env") == []


def test_every_static_rule_suggests_a_fix():
    source = (FIXTURE / "calculator.py").read_text() + "\n" + (FIXTURE / "command_exec.py").read_text()
    findings = inspect_python("x.py", source) + inspect_python("c.py", (FIXTURE / "cache.py").read_text())
    fixes = {item.rule: item.suggested_fix for item in findings}
    assert "ast.literal_eval(expression)" in fixes["dynamic-eval"]
    assert "if count == 0:" in fixes["unguarded-division"]
    assert "shlex.split(argument)" in fixes["subprocess-shell"] and "shell=False" in fixes["subprocess-shell"]
    assert "cache=None" in fixes["mutable-default"]


def test_model_fix_field_is_kept():
    finding = coerce_finding({"file": "a.py", "line": 1, "evidence": "x", "description": "d",
                              "fix": "y = 1"}, "bug_hunter")
    assert finding.suggested_fix == "y = 1"


@pytest.mark.asyncio
async def test_report_shows_fixes_without_applying_them(tmp_path):
    before = (FIXTURE / "calculator.py").read_text()
    outcome = await ReviewOrchestrator(SafeRepository(FIXTURE), StaticProvider(), BudgetConfig(),
                                       str(tmp_path)).review(ReviewTask(paths=["calculator.py"]))
    markdown, _ = save_report(outcome, tmp_path)
    text = markdown.read_text()
    assert "Suggested fix (not applied)" in text and "ast.literal_eval" in text
    assert "Suggested fix" in (tmp_path / f"{outcome.run_id}.html").read_text()
    assert (FIXTURE / "calculator.py").read_text() == before


def test_sql_built_from_strings_is_flagged_with_parameterised_fix():
    source = (
        'def find(conn, name):\n'
        '    query = f"SELECT * FROM users WHERE name = \'{name}\'"\n'
        '    return conn.execute(query).fetchall()\n'
        'def ok(conn, name):\n'
        '    return conn.execute("SELECT * FROM users WHERE name = ?", (name,))\n'
        'def concat(cur, uid):\n'
        '    cur.execute("DELETE FROM t WHERE id = " + uid)\n'
    )
    flagged = [item for item in inspect_python("db.py", source) if item.rule == "sql-injection"]
    assert sorted(item.line_numbers for item in flagged) == [[3], [7]]
    assert all(item.category == "security" and "?" in item.suggested_fix for item in flagged)


def test_retired_model_falls_back_to_next_model(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout):
        model = json.loads(request.data)["model"]
        seen.append(model)
        if model == "old-model":
            raise urllib.error.HTTPError("https://x", 400, "bad", {}, io.BytesIO(
                b'{"error": {"message": "The model `old-model` has been decommissioned"}}'))
        return FakeResponse('{"findings": []}')

    monkeypatch.setattr("agents.provider.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider("old-model", api_key="k", base_url="https://api.groq.com/openai/v1",
                                        fallback_models=("old-model", "new-model"))
    provider._chat("s", "u", 10)
    assert seen[-1] == "new-model" and provider.model_switches == ["old-model -> new-model (HTTP 400)"]


def test_auth_error_is_not_retried_and_shows_server_message(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError("https://x", 401, "no", {}, io.BytesIO(b'{"error": {"message": "Invalid API Key"}}'))

    monkeypatch.setattr("agents.provider.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider("m", api_key="k", base_url="https://api.groq.com/openai/v1")
    from agents.provider import ProviderError
    with pytest.raises(ProviderError, match="Invalid API Key") as caught:
        provider._chat("s", "u", 10)
    assert not caught.value.retryable


def test_decimal_retry_after_is_parsed(monkeypatch):
    monkeypatch.setattr("agents.provider.urllib.request.urlopen",
                        lambda request, timeout: (_ for _ in ()).throw(http_error(429, {"Retry-After": "2.5"})))
    provider = OpenAICompatibleProvider("m", api_key="k", base_url="https://api.groq.com/openai/v1")
    with pytest.raises(RateLimited) as caught:
        provider._chat("s", "u", 10)
    assert caught.value.retry_after == 2.5


@pytest.mark.asyncio
async def test_scanner_findings_survive_a_failed_model(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError("https://x", 401, "no", {}, io.BytesIO(b'{"error": {"message": "Invalid API Key"}}'))

    monkeypatch.setattr("agents.provider.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider("m", api_key="k", base_url="https://api.groq.com/openai/v1")
    from core.approval import ApprovalGate, ApprovalPolicy
    outcome = await ReviewOrchestrator(SafeRepository(FIXTURE), provider, BudgetConfig(retry_base_seconds=0),
                                       str(tmp_path), ApprovalGate(policy=ApprovalPolicy.approve)
                                       ).review(ReviewTask(paths=["calculator.py"]))
    assert outcome.partial
    assert {item.rule for item in outcome.findings} == {"dynamic-eval", "unguarded-division"}
    assert all("scanner findings only" in agent.errors[0] for agent in outcome.agents)
    assert outcome.budget["model_calls"] == 4  # planner + 3 specialists, no retries for a 401


def test_rate_limited_model_hands_over_to_fallback(monkeypatch):
    def fake_urlopen(request, timeout):
        if json.loads(request.data)["model"] == "busy":
            raise http_error(429, {"Retry-After": "30"})
        return FakeResponse('{"findings": []}')

    monkeypatch.setattr("agents.provider.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider("busy", api_key="k", base_url="https://api.groq.com/openai/v1",
                                        fallback_models=("busy", "spare"))
    provider._chat("s", "u", 10)
    assert provider.model == "spare" and provider.model_switches == ["busy -> spare (HTTP 429)"]
