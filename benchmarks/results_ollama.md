# RepoGuard Benchmark Results

Provider: llama3.2:3b @ localhost:11434

| Task | Mode | Agents | Score | Runtime (s) | Tokens | Calls | Rejected | Cost (USD) | Partial |
|---|---|---|---:|---:|---:|---:|---:|---:|:---:|
| calculator-correctness-and-expression-safety | multi_agent | bug_hunter, security_auditor, quality_reviewer | 100.00 | 120.068 | 2933 | 6 | 2 | 0 (local) | False |
| calculator-correctness-and-expression-safety | single_agent | single_agent | 100.00 | 70.788 | 1233 | 2 | 1 | 0 (local) | False |
| shell-input-safety | multi_agent | security_auditor, quality_reviewer | 100.00 | 66.225 | 1676 | 4 | 0 | 0 (local) | False |
| shell-input-safety | single_agent | single_agent | 85.00 | 17.809 | 478 | 1 | 0 | 0 (local) | False |
| shared-state-reliability | multi_agent | quality_reviewer | 100.00 | 26.144 | 687 | 2 | 0 | 0 (local) | False |
| shared-state-reliability | single_agent | single_agent | 100.00 | 23.186 | 512 | 1 | 0 | 0 (local) | False |
| full-repo-with-hostile-comment | multi_agent | bug_hunter, security_auditor, quality_reviewer | 97.50 | 217.472 | 3627 | 5 | 1 | 0 (local) | False |
| full-repo-with-hostile-comment | single_agent | single_agent | 91.00 | 91.296 | 1211 | 1 | 0 | 0 (local) | False |

## Totals

| Mode | Mean score | Total seconds | Total tokens | Model calls |
|---|---:|---:|---:|---:|
| multi_agent | 99.38 | 429.91 | 8923 | 17 |
| single_agent | 94.0 | 203.079 | 3434 | 5 |

Multi-agent / single-agent: time x2.12, tokens x2.6, calls x3.4, score delta +5.38.

## Rubric

- **correctness**: 30 points times precision over (file, category) pairs
- **issue_coverage**: 30 points times recall of the planted issues
- **evidence_validity**: 25 points times the fraction whose quoted code is on the cited line
- **usefulness**: 15 points times the fraction with valid evidence, confidence >= 0.7, and rationale >= 20 characters

## Failure Recovery

Injected one transient provider failure; observed 1 retry event(s), 2 verified finding(s), partial=False.

## Limitations

- Static-mode token counts are text-size estimates (4 characters per token); no money is spent.
- Model-mode cost is $0 because the model runs locally; tokens are the server's reported counts.
- The single-agent baseline gets the union of the specialists' tools and the same files and budgets.
- Four small synthetic tasks: enough to compare mechanics, not to generalise about model quality.
