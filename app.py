from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from agents.provider import OpenAICompatibleProvider, StaticProvider
from core.approval import ApprovalGate, ApprovalPolicy
from core.budgets import BudgetConfig
from core.env import load_env_file
from core.orchestrator import ReviewOrchestrator
from core.reporting import export_report, save_report
from core.schemas import ReviewTask
from core.trace_view import load_trace, render_timeline, write_html
from tools.repository import SafeRepository

ROOT = Path(__file__).resolve().parent
OLLAMA_URL = "http://localhost:11434/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GROQ_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODELS = {"groq": "openai/gpt-oss-120b", "ollama": "llama3.2:3b",
                  "gemini": "gemini-flash-latest", "openai": "gpt-4o-mini"}
# Groq retires models over time; try these in order when one is gone or overloaded.
GROQ_FALLBACKS = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b")
# Google retires model names and the free tier is often overloaded (HTTP 503); try these in order.
GEMINI_FALLBACKS = ("gemini-flash-latest", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-lite-latest")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evidence-driven, read-only Python repository review")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("review", "baseline"):
        command = commands.add_parser(name, help="Run the multi-agent review" if name == "review" else "Run the single-agent baseline")
        command.add_argument("repository", type=Path)
        command.add_argument("--goal", "--objective", dest="objective",
                             default="Find correctness, security, and reliability issues.",
                             help="plain-English goal; the lead agent picks specialists from it")
        command.add_argument("--paths", nargs="*", default=[])
        command.add_argument("--provider", choices=("static", "groq", "gemini", "ollama", "openai"), default="static",
                             help="static: offline rules; groq: Groq API, free tier (GROQ_API_KEY); "
                                  "gemini: Google Gemini (GEMINI_API_KEY); ollama: free local model; "
                                  "openai: any OpenAI-compatible API")
        command.add_argument("--model", default=None,
                             help="default: openai/gpt-oss-120b (groq), gemini-flash-latest (gemini), "
                                  "llama3.2:3b (ollama), gpt-4o-mini (openai); groq and gemini fall back to other models")
        command.add_argument("--runs-dir", type=Path, default=None,
                             help="where reports and traces go (default: runs/<project name>/ in this tool's folder)")
        command.add_argument("--max-steps", type=int, default=40)
        command.add_argument("--max-calls", type=int, default=30)
        command.add_argument("--max-tokens", type=int, default=150_000)
        command.add_argument("--max-agent-tokens", type=int, default=60_000)
        command.add_argument("--max-cost", type=float)
        command.add_argument("--max-agent-cost", type=float)
        command.add_argument("--timeout", type=float, default=None, help="per call; default 15s static, 120s for models")
        command.add_argument("--agent-time-budget", type=float, default=None)
        command.add_argument("--run-time-budget", type=float, default=None)
        command.add_argument("--max-agent-calls", type=int, default=10)
        command.add_argument("--batch-tokens", type=int, default=None,
                             help="input tokens per call before files are split; default 2500 ollama, 3000 groq, "
                                  "20000 gemini, 6000 otherwise")
        command.add_argument("--max-retries", type=int, default=2)
        command.add_argument("--max-revisions", type=int, default=1)
        approval = command.add_mutually_exclusive_group()
        approval.add_argument("--yes", action="store_true", help="pre-approve risky actions (paid calls, writes outside runs/)")
        approval.add_argument("--deny-risky", action="store_true", help="refuse every risky action without asking")
        command.add_argument("--export", type=Path, help="copy the Markdown report here (outside runs/ needs approval)")
    trace = commands.add_parser("trace", help="Replay a saved trace as a timeline (no model calls)")
    trace.add_argument("trace_file", type=Path)
    trace.add_argument("--agent", help="only show events from this agent")
    trace.add_argument("--raw", action="store_true", help="print raw JSONL records")
    view = commands.add_parser("view", help="(Re)build the HTML viewer for a saved trace")
    view.add_argument("trace_file", type=Path)
    return parser


def _provider(args: argparse.Namespace, timeout: float):
    if args.provider == "static":
        return StaticProvider()
    model = args.model or os.environ.get(f"{args.provider.upper()}_MODEL") or DEFAULT_MODELS[args.provider]
    if args.provider == "ollama":
        return OpenAICompatibleProvider(model=model, base_url=os.environ.get("OLLAMA_BASE_URL", OLLAMA_URL),
                                        request_timeout=timeout)
    if args.provider == "groq":
        # Groq's free tier allows only a few thousand tokens per minute per model, so calls stay small.
        # gpt-oss models reason before answering, and reasoning counts against max_tokens.
        return OpenAICompatibleProvider(model=model, base_url=GROQ_URL, request_timeout=timeout,
                                        max_completion_tokens=4096, key_env="GROQ_API_KEY",
                                        fallback_models=GROQ_FALLBACKS)
    if args.provider == "gemini":
        # Gemini's OpenAI-compatible endpoint; flash models spend part of max_tokens on thinking, so allow room.
        return OpenAICompatibleProvider(model=model, base_url=GEMINI_URL, request_timeout=timeout,
                                        max_completion_tokens=8192, key_env="GEMINI_API_KEY",
                                        fallback_models=GEMINI_FALLBACKS)
    return OpenAICompatibleProvider(model=model, request_timeout=timeout)


def _env_price(name: str) -> float | None:
    return float(os.environ[name]) if os.environ.get(name) else None


async def _review(args: argparse.Namespace) -> int:
    if args.runs_dir is None:
        # Keep output next to the tool, one folder per reviewed project, never inside the project itself.
        args.runs_dir = ROOT / "runs" / (args.repository.resolve().name or "repository")
    uses_model = args.provider != "static"
    timeout = args.timeout or (120 if uses_model else 15)
    agent_budget = args.agent_time_budget or timeout * 3
    run_budget = args.run_time_budget or max(agent_budget * 2, 120)
    provider = _provider(args, min(timeout, agent_budget, run_budget))
    budget = BudgetConfig(
        max_steps=args.max_steps, max_model_calls=args.max_calls, max_tokens=args.max_tokens,
        per_agent_tokens=args.max_agent_tokens,
        per_agent_calls=args.max_agent_calls, per_agent_steps=args.max_agent_calls,
        max_batch_tokens=args.batch_tokens or {"ollama": 2_500, "groq": 3_000, "gemini": 20_000}.get(args.provider, 6_000),
        max_cost_usd=args.max_cost, per_agent_cost_usd=args.max_agent_cost,
        input_cost_per_1k_usd=_env_price("REPOGUARD_INPUT_COST_PER_1K"),
        output_cost_per_1k_usd=_env_price("REPOGUARD_OUTPUT_COST_PER_1K"),
        agent_timeout_seconds=timeout, max_retries=args.max_retries,
        per_agent_time_seconds=agent_budget, max_duration_seconds=run_budget,
        max_revisions=args.max_revisions,
        retry_base_seconds=2 if uses_model else 0.05,
        # Hosted free tiers rate-limit (HTTP 429); wait up to 30 s between attempts.
        retry_max_seconds=30 if uses_model else 1.0,
    )
    policy = ApprovalPolicy.approve if args.yes else ApprovalPolicy.deny if args.deny_risky else ApprovalPolicy.prompt
    approval = ApprovalGate(policy=policy)
    orchestrator = ReviewOrchestrator(SafeRepository(args.repository), provider, budget, str(args.runs_dir), approval)
    outcome = await orchestrator.review(
        ReviewTask(objective=args.objective, paths=args.paths), single_agent=args.command == "baseline",
    )
    markdown, json_report = save_report(outcome, args.runs_dir)
    trace_path = args.runs_dir / f"{outcome.run_id}.jsonl"
    print(f"Run: {outcome.run_id}  ({outcome.mode}, {outcome.provider})")
    print(f"Plan: {', '.join(item['agent'] for item in outcome.plan.get('subtasks', [])) or '-'}"
          f"  [{outcome.plan.get('method', '-')}]")
    for finding in outcome.findings:
        print(f"  {finding.severity.value.upper():<8} {finding.file_path}:{','.join(map(str, finding.line_numbers))}"
              f"  {finding.description}")
        if finding.suggested_fix:
            fix_lines = finding.suggested_fix.splitlines()
            print("           fix: " + fix_lines[0].strip() + (" ..." if len(fix_lines) > 1 else ""))
    if outcome.warnings:
        print(f"Untrusted content: {len(outcome.warnings)} instruction-like line(s) flagged and treated as data")
    budget_used = outcome.budget
    print(f"Verified findings: {len(outcome.findings)}; rejected: {len(outcome.rejected)}; "
          f"calls: {budget_used['model_calls']}; tokens: {budget_used['tokens']}; {outcome.duration_seconds:.2f}s")
    if outcome.partial:
        print(f"PARTIAL RESULT: {outcome.stop_reason or 'an agent failed; see trace'}")
    if args.export:
        exported = export_report(markdown, args.export, args.runs_dir, ApprovalGate(policy=policy), trace_path)
        print(f"Export: {exported}" if exported else "Export: refused (approval not granted)")
        write_html(trace_path)
    print(f"Report: {markdown}\nJSON:   {json_report}\nTrace:  {trace_path}\n"
          f"State:  {args.runs_dir / (outcome.run_id + '.state.json')}\nViewer: {trace_path.with_suffix('.html')}")
    return 1 if outcome.partial else 0


def _trace(args: argparse.Namespace) -> int:
    try:
        records = load_trace(args.trace_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Unable to read trace: {type(exc).__name__}")
        return 2
    if args.raw:
        for record in records:
            print(json.dumps(record, ensure_ascii=True))
    else:
        print(render_timeline(records, agent=args.agent))
    return 0


def main() -> int:
    # Model output can contain characters the Windows console code page can't show; replace, don't crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    load_env_file(ROOT / ".env")
    args = _parser().parse_args()
    if args.command == "trace":
        return _trace(args)
    if args.command == "view":
        print(write_html(args.trace_file))
        return 0
    try:
        return asyncio.run(_review(args))
    except (ValueError, OSError) as exc:
        print(f"Review could not start: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
