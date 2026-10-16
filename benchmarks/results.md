# RepoGuard Benchmark Results

Provider: static (offline AST rules, no model)

| Task | Mode | Agents | Score | Runtime (s) | Tokens | Calls | Rejected | Merged dups | Cost (USD) | Partial |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| calculator-correctness-and-expression-safety | multi_agent | bug_hunter, security_auditor | 100.00 | 0.022 | 323 | 2 | 0 | 0 | 0 (local) | False |
| calculator-correctness-and-expression-safety | single_agent | single_agent | 100.00 | 0.015 | 297 | 1 | 0 | 0 | 0 (local) | False |
| shell-input-safety | multi_agent | security_auditor | 100.00 | 0.017 | 167 | 1 | 0 | 0 | 0 (local) | False |
| shell-input-safety | single_agent | single_agent | 100.00 | 0.020 | 167 | 1 | 0 | 0 | 0 (local) | False |
| shared-state-reliability | multi_agent | quality_reviewer | 100.00 | 0.020 | 158 | 1 | 0 | 0 | 0 (local) | False |
| shared-state-reliability | single_agent | single_agent | 100.00 | 0.022 | 158 | 1 | 0 | 0 | 0 (local) | False |
| full-repo-with-hostile-comment | multi_agent | bug_hunter, security_auditor, quality_reviewer | 100.00 | 0.059 | 851 | 3 | 0 | 0 | 0 (local) | False |
| full-repo-with-hostile-comment | single_agent | single_agent | 100.00 | 0.021 | 653 | 1 | 0 | 0 | 0 (local) | False |

## Totals

| Mode | Mean score | Total seconds | Total tokens | Model calls |
|---|---:|---:|---:|---:|
| multi_agent | 100.0 | 0.119 | 1499 | 7 |
| single_agent | 100.0 | 0.078 | 1275 | 4 |

Multi-agent / single-agent: time x1.53, tokens x1.18, calls x1.75, score delta +0.0.

## Rubric

- **correctness**: 30 points times precision over (file, category) pairs
- **issue_coverage**: 30 points times recall of the planted issues
- **evidence_validity**: 25 points times the fraction whose quoted code is on the cited line
- **usefulness**: 15 points times the fraction with valid evidence, confidence >= 0.7, and rationale >= 20 characters

## Failure Recovery

Injected one transient provider failure; observed 1 retry event(s), 2 verified finding(s), partial=False.

## Limitations

- Static-mode token counts are text-size estimates (4 characters per token); no money is spent.
- Model-mode cost is $0: Ollama runs locally and Groq was used on its free tier; tokens are the server's reported counts.
- The single-agent baseline gets the union of the specialists' tools and the same files and budgets.
- Four small synthetic tasks: enough to compare mechanics, not to generalise about model quality.
