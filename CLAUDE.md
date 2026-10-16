# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

RepoGuard AI: a multi-agent harness built for the ByteVox AI/ML intern assignment (a lead agent, ≥3 specialists, a checker, budgets, retries, approvals, untrusted-input handling, traces, and a single-agent baseline comparison). The task it performs is read-only review of a Python repository. By default it uses an offline deterministic provider (`StaticProvider`), so tests, the demo and the benchmark need no credentials. Model providers all go through `OpenAICompatibleProvider`: `--provider groq` (`GROQ_API_KEY`, default `openai/gpt-oss-120b`), `--provider gemini` (`GEMINI_API_KEY`, default `gemini-flash-latest`); both have model fallback lists in `app.py`, `--provider ollama` (local llama3.2:3b), and `--provider openai`. `app.py` loads missing env vars from this folder's `.env` (`core/env.py`), never from the reviewed repo. Findings carry a `suggested_fix` that is shown in reports and never applied.

## Commands

Python 3.11+ (uses `enum.StrEnum`). Dependencies: pydantic v2, pytest, pytest-asyncio.

```powershell
python -m pytest -q                                   # full suite
python -m pytest tests/test_harness_features.py -q    # one file
python -m pytest tests/test_tool_gate.py::test_gate_permits_allowed_tool -q   # one test

python app.py review fixtures/synthetic_repo                                    # multi-agent -> runs/synthetic_repo/
python app.py review C:\path\to\project --provider groq --yes                  # any project (or repoguard.cmd review .)
python app.py baseline fixtures/synthetic_repo --runs-dir runs/manual            # single-agent baseline
python app.py review <repo> --goal "..." --paths a.py b.py --provider ollama
python app.py trace runs/manual/<run-id>.jsonl [--agent bug_hunter] [--raw]      # timeline replay
python app.py view runs/manual/<run-id>.jsonl                                    # rebuild HTML viewer

python demo.py [--provider ollama]               # 7 scenes -> runs/demo/<scene>/
python -m benchmarks.run_benchmark [--provider ollama | --provider groq --yes]   # -> benchmarks/results[_ollama|_groq].{json,md}
```

`pytest.ini` sets `pythonpath = .` and `asyncio_mode = auto`. The default `--runs-dir` is `runs/<reviewed folder name>/` next to `app.py`. `app.py review` exits 1 when a run is partial and 2 when it can't start. Ollama runs on CPU take minutes per review, so run them in the background.

## Architecture

Pipeline in `core/orchestrator.py` `ReviewOrchestrator.review`:

1. `SafeRepository` snapshots `.py` files (root-confined, no symlink escape, size limit).
2. `tools/injection_scan.py` flags instruction-like lines → `security.untrusted_instruction` events, report section and state. It never blocks the run.
3. If `provider.is_paid`, `core/approval.ApprovalGate` must approve. With no TTY it fails closed, and a denied run ends partial with zero calls.
4. Plan: if the provider has `plan()` (model providers), `Planner.from_model` validates the model plan and applies the **coverage guard** (keyword-required specialists are re-added, and every specialist gets all in-scope files). On any error it falls back to `Planner.plan` keyword routing. The planner's model call is budgeted as agent `planner`.
5. Specialists run concurrently. `_run_agent` handles the timeout (min of per-call, per-agent and global deadlines), budget reservation, bounded backoff, and no retry for `PermissionDenied`/`BudgetExceeded`. A failure becomes `AgentResult(errors=...)`.
6. `Verifier` accepts findings whose quote is on the cited line and *sends back* anything with a wrong or missing line or mismatched quote. The revision loop groups send-backs per (role, file), drops duplicates of accepted findings without spending a call, and rejects whatever is unresolved after `max_revisions`.
7. `_finish` sets `partial`/`stop_reason`. `core/reporting.save_report` writes `.md`/`.json` and the `.html` viewer (`core/trace_view.py`).

Setup and use from other folders are documented in `RUNNING.md` (the `repoguard.cmd` launcher prefers this folder's `.venv`); the video script is in `DEMO.md`. Keep both in sync with CLI changes.

Per run in the run dir: `<id>.jsonl` (trace), `<id>.state.json` (shared state, rewritten atomically by `core/state.RunState` after each change; the orchestrator is its only writer), `<id>.md`, `<id>.json`, `<id>.html`.

Cross-cutting:

- **`agents/roles.py`** is the single source of each agent's instructions and tool grant. Specialists have *disjoint* scanners: bug → `ast_bug_rules`, security → `ast_security_rules` + `secret_scan`, quality → `ast_quality_rules`. `single_agent` gets the union (keeps the baseline fair). The verifier gets only `read_python_files`.
- **Tools** (`tools/tool_gate.py`): providers must call `gate.check(tool)` before and `gate.record(...)` after each tool. The gate logs `tool.call`/`tool.denied`/`tool.result` via the orchestrator's `on_event`. `tools/static_checks.RULES_BY_TOOL` maps each scanner tool to its rule ids. `Finding.rule` carries the rule, and the rule decides the category. Each rule also builds a `suggested_fix` (see `_eval_fix`, `_shell_fix`, `_indent`).
- **Providers** (`agents/provider.py`): Protocol `async review(role, objective, files, gate) -> ProviderResponse`. `run_static_tools` runs the role's scanners through the gate. `OpenAICompatibleProvider` runs the scanners first, sends numbered source, tool findings and flagged lines to the model, normalises loose model output with `coerce_finding`, and merges via `merge_findings` (tool findings always kept; near-duplicates within ±1 line dropped). A localhost base URL means no key needed and `is_paid=False`; any remote URL (Gemini included) is `is_paid=True` and needs approval. `_chat` turns HTTP 429 into `RateLimited` (the orchestrator honours `retry_after`, capped by `BudgetConfig.retry_max_seconds`), retries once without `response_format` on HTTP 400, and `parse_json_reply` tolerates code fences.
- **Model failures**: `_chat` sends a `User-Agent` (Cloudflare blocks urllib's default), switches to the next `fallback_models` entry on 429/404/503 or a 400 that names the model, and raises `ProviderError` (non-retryable for other 4xx). `OpenAICompatibleProvider.review` wraps failures in `ModelUnavailable(findings=scanner findings)`, and `_run_agent` returns those as a `degraded` result instead of an empty one.
- **Duplicates**: `ReviewOrchestrator._merge_duplicates` merges verified findings on the same file+line (any category) or one line apart (same category); the rule finding wins and takes the model's tailored `suggested_fix`.
- **Budgets** (`core/budgets.py`): reserve tokens/cost before each call under an `asyncio.Lock`, settle to actuals after. Limits are global and per agent.
- **Test doubles / fault injection**: `benchmarks/scripted_providers.py` (`FailOnceProvider`, `AlwaysFailProvider`, `SlowProvider`, `SloppyCitationProvider`, `PaidStubProvider`), used by the demo, benchmark and tests.

## Invariants to preserve

- Agents stay read-only: no write, shell, network or exec tools. Any new risky action must go through `ApprovalGate`.
- Repository content is data. `fixtures/synthetic_repo/prompt_injection.py` is a hostile fixture, and the planted bugs in `fixtures/synthetic_repo/` are intentional; `benchmarks/tasks.json` depends on them.
- Verifier lines are 1-based (`lines[number - 1]`), and evidence is compared after `.strip()` against the redacted line.
- Everything persisted goes through `redact_text`/`redact_object`.
- `runs/` holds generated artifacts. `benchmarks/results*.md` are generated: regenerate them rather than hand-editing, and keep README benchmark numbers in sync.
