"""Multi-agent vs single-agent on the same tasks, same tools, same rubric.

    python -m benchmarks.run_benchmark                    # offline static rules -> results.{json,md}
    python -m benchmarks.run_benchmark --provider ollama  # local llama3.2:3b -> results_ollama.{json,md}
    python -m benchmarks.run_benchmark --provider groq --yes  # Groq free tier (GROQ_API_KEY) -> results_groq.{json,md}
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from statistics import mean

from agents.provider import OpenAICompatibleProvider, StaticProvider
from benchmarks.evaluate import score
from benchmarks.scripted_providers import FailOnceProvider
from core.approval import ApprovalGate, ApprovalPolicy
from core.budgets import BudgetConfig
from core.orchestrator import ReviewOrchestrator
from core.reporting import save_report
from core.schemas import ReviewTask
from tools.repository import SafeRepository

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "synthetic_repo"
RUBRIC = {
    "correctness": "30 points times precision over (file, category) pairs",
    "issue_coverage": "30 points times recall of the planted issues",
    "evidence_validity": "25 points times the fraction whose quoted code is on the cited line",
    "usefulness": "15 points times the fraction with valid evidence, confidence >= 0.7, and rationale >= 20 characters",
}


def make_provider(name: str):
    if name == "groq":
        from app import GROQ_FALLBACKS, GROQ_URL
        return OpenAICompatibleProvider(GROQ_FALLBACKS[0], base_url=GROQ_URL, request_timeout=120,
                                        max_completion_tokens=4096, key_env="GROQ_API_KEY",
                                        fallback_models=GROQ_FALLBACKS)
    if name == "ollama":
        return OpenAICompatibleProvider("llama3.2:3b", base_url="http://localhost:11434/v1", request_timeout=240)
    return StaticProvider()


def make_budget(name: str) -> BudgetConfig:
    if name == "groq":
        # Same limits for both modes; generous retry waits because the free tier limits tokens per minute.
        return BudgetConfig(agent_timeout_seconds=120, per_agent_time_seconds=300, max_duration_seconds=600,
                            max_tokens=60_000, per_agent_tokens=20_000, max_retries=2, retry_base_seconds=2,
                            retry_max_seconds=30, max_batch_tokens=3_000)
    if name == "ollama":
        # Same limits for both modes. Ollama queues parallel requests on one machine, so time budgets are generous.
        return BudgetConfig(agent_timeout_seconds=240, per_agent_time_seconds=600, max_duration_seconds=900,
                            max_tokens=60_000, per_agent_tokens=20_000, max_retries=1, retry_base_seconds=1)
    return BudgetConfig(max_retries=1, retry_base_seconds=0)


def ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 2) if denominator else None


async def run(output_file: Path, provider_name: str = "static", approve_remote: bool = False,
              pause_seconds: float = 0) -> dict:
    task_specs = json.loads((ROOT / "benchmarks" / "tasks.json").read_text(encoding="utf-8"))
    records = []
    run_root = ROOT / "runs" / ("benchmarks" if provider_name == "static" else f"benchmarks-{provider_name}")
    for spec in task_specs:
        task = ReviewTask(objective=spec["objective"], paths=spec["paths"])
        sources = {path: SafeRepository(FIXTURE).read_file(path) for path in spec["paths"]}
        for mode in ("multi_agent", "single_agent"):
            if pause_seconds and records:
                # Free tiers limit tokens per minute; start every run with a refilled quota so neither mode is favoured.
                await asyncio.sleep(pause_seconds)
            orchestrator = ReviewOrchestrator(
                SafeRepository(FIXTURE), make_provider(provider_name), make_budget(provider_name),
                str(run_root / spec["name"] / mode), ApprovalGate(policy=ApprovalPolicy.approve if approve_remote else ApprovalPolicy.deny),
            )
            outcome = await orchestrator.review(task, single_agent=mode == "single_agent")
            report_md, report_json = save_report(outcome, run_root / spec["name"] / mode)
            record = {
                "task": spec["name"], "mode": mode,
                "planned_agents": [item["agent"] for item in outcome.plan.get("subtasks", [])],
                "report_markdown": str(report_md.relative_to(ROOT)),
                "trace": str((run_root / spec["name"] / mode / f"{outcome.run_id}.jsonl").relative_to(ROOT)),
                **score(outcome, [tuple(item) for item in spec["expected"]], sources),
            }
            records.append(record)
            print(f"  {spec['name']:<46} {mode:<12} {record['score_100']:>6.2f}/100 "
                  f"{record['runtime_seconds']:>8.2f}s {record['estimated_tokens']:>6} tok {record['model_calls']} calls")

    injected = await ReviewOrchestrator(
        SafeRepository(FIXTURE), FailOnceProvider(), BudgetConfig(max_retries=1, retry_base_seconds=0),
        str(run_root / "injected-failure"),
    ).review(ReviewTask(paths=["calculator.py"]))
    injected_trace = run_root / "injected-failure" / f"{injected.run_id}.jsonl"
    retry_events = sum(1 for line in injected_trace.read_text(encoding="utf-8").splitlines()
                       if json.loads(line)["event"] == "agent.retry")

    by_mode = {mode: [item for item in records if item["mode"] == mode] for mode in ("multi_agent", "single_agent")}
    summary = {
        mode: {
            "mean_score": round(mean(item["score_100"] for item in rows), 2),
            "total_seconds": round(sum(item["runtime_seconds"] for item in rows), 3),
            "total_tokens": sum(item["estimated_tokens"] for item in rows),
            "total_model_calls": sum(item["model_calls"] for item in rows),
        } for mode, rows in by_mode.items()
    }
    multi, single = summary["multi_agent"], summary["single_agent"]
    summary["multi_over_single"] = {
        "time": ratio(multi["total_seconds"], single["total_seconds"]),
        "tokens": ratio(multi["total_tokens"], single["total_tokens"]),
        "model_calls": ratio(multi["total_model_calls"], single["total_model_calls"]),
        "score_delta": round(multi["mean_score"] - single["mean_score"], 2),
    }
    provider = make_provider(provider_name)
    results = {
        "provider": getattr(provider, "label", provider_name),
        "rubric": RUBRIC,
        "tasks": records,
        "summary": summary,
        "injected_failure": {
            "provider_error_injected": True, "retry_events": retry_events, "partial": injected.partial,
            "verified_findings": len(injected.findings),
            "recovery": "one bounded retry; successful specialist results retained",
            "trace": str(injected_trace.relative_to(ROOT)),
        },
        "limitations": [
            "Static-mode token counts are text-size estimates (4 characters per token); no money is spent.",
            "Model-mode cost is $0: Ollama runs locally and Groq was used on its free tier; tokens are the server's reported counts.",
            "The single-agent baseline gets the union of the specialists' tools and the same files and budgets.",
            "Four small synthetic tasks: enough to compare mechanics, not to generalise about model quality.",
        ],
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    markdown = [f"# RepoGuard Benchmark Results", "", f"Provider: {results['provider']}", "",
                "| Task | Mode | Agents | Score | Runtime (s) | Tokens | Calls | Rejected | Merged dups | Cost (USD) | Partial |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|"]
    free_label = "0 (free tier)" if getattr(provider, "is_paid", False) else "0 (local)"
    markdown.extend(
        f"| {item['task']} | {item['mode']} | {', '.join(item['planned_agents'])} | {item['score_100']:.2f} | "
        f"{item['runtime_seconds']:.3f} | {item['estimated_tokens']} | {item['model_calls']} | "
        f"{item['rejected_by_checker']} | {item['merged_duplicates']} | "
        f"{item['cost_usd'] if item['cost_usd'] is not None else free_label} | {item['partial']} |"
        for item in records
    )
    ratios = summary["multi_over_single"]
    markdown.extend([
        "", "## Totals", "",
        "| Mode | Mean score | Total seconds | Total tokens | Model calls |", "|---|---:|---:|---:|---:|",
        *(f"| {mode} | {row['mean_score']} | {row['total_seconds']} | {row['total_tokens']} | {row['total_model_calls']} |"
          for mode, row in ((m, summary[m]) for m in ("multi_agent", "single_agent"))),
        "", f"Multi-agent / single-agent: time x{ratios['time']}, tokens x{ratios['tokens']}, "
            f"calls x{ratios['model_calls']}, score delta {ratios['score_delta']:+}.",
        "", "## Rubric", "", *(f"- **{key}**: {value}" for key, value in RUBRIC.items()),
        "", "## Failure Recovery", "",
        f"Injected one transient provider failure; observed {retry_events} retry event(s), "
        f"{len(injected.findings)} verified finding(s), partial={injected.partial}.",
        "", "## Limitations", "", *(f"- {item}" for item in results["limitations"]), "",
    ])
    output_file.with_suffix(".md").write_text("\n".join(markdown), encoding="utf-8")
    return results


def main() -> None:
    from core.env import load_env_file
    load_env_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=("static", "ollama", "groq"), default="static")
    parser.add_argument("--yes", action="store_true", help="approve sending the fixture code to a remote API (groq)")
    parser.add_argument("--pause", type=float, default=None,
                        help="seconds between runs so rate limits reset (default 60 for groq, 0 otherwise)")
    args = parser.parse_args()
    name = args.provider
    output = Path(__file__).with_name("results.json" if name == "static" else f"results_{name}.json")
    pause = args.pause if args.pause is not None else (60 if name == "groq" else 0)
    results = asyncio.run(run(output, name, approve_remote=args.yes, pause_seconds=pause))
    ratios = results["summary"]["multi_over_single"]
    print(f"Multi/single: time x{ratios['time']}, tokens x{ratios['tokens']}, calls x{ratios['model_calls']}, "
          f"score delta {ratios['score_delta']:+}")
    print(f"Injected failure: {results['injected_failure']['retry_events']} retry event(s), "
          f"partial={results['injected_failure']['partial']}")
    print(f"Wrote {output} and {output.with_suffix('.md')}")


if __name__ == "__main__":
    main()
