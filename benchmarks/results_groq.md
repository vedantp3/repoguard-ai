# RepoGuard Benchmark Results

Provider: openai/gpt-oss-120b @ api.groq.com

| Task | Mode | Agents | Score | Runtime (s) | Tokens | Calls | Rejected | Merged dups | Cost (USD) | Partial |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| calculator-correctness-and-expression-safety | multi_agent | bug_hunter, security_auditor | 100.00 | 3.207 | 2735 | 3 | 0 | 2 | 0 (free tier) | False |
| calculator-correctness-and-expression-safety | single_agent | single_agent | 100.00 | 1.690 | 1105 | 1 | 0 | 0 | 0 (free tier) | False |
| shell-input-safety | multi_agent | security_auditor | 100.00 | 2.758 | 1539 | 2 | 0 | 0 | 0 (free tier) | False |
| shell-input-safety | single_agent | single_agent | 100.00 | 1.581 | 1022 | 1 | 0 | 0 | 0 (free tier) | False |
| shared-state-reliability | multi_agent | bug_hunter, security_auditor, quality_reviewer | 100.00 | 3.629 | 3120 | 4 | 0 | 2 | 0 (free tier) | False |
| shared-state-reliability | single_agent | single_agent | 100.00 | 5.043 | 934 | 1 | 0 | 0 | 0 (free tier) | False |
| full-repo-with-hostile-comment | multi_agent | bug_hunter, security_auditor, quality_reviewer | 100.00 | 12.348 | 6543 | 4 | 0 | 7 | 0 (free tier) | False |
| full-repo-with-hostile-comment | single_agent | single_agent | 100.00 | 11.080 | 2447 | 1 | 0 | 0 | 0 (free tier) | False |

## Totals

| Mode | Mean score | Total seconds | Total tokens | Model calls |
|---|---:|---:|---:|---:|
| multi_agent | 100.0 | 21.941 | 13937 | 13 |
| single_agent | 100.0 | 19.394 | 5508 | 4 |

Multi-agent / single-agent: time x1.13, tokens x2.53, calls x3.25, score delta +0.0.

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
