# Running RepoGuard: setup and use from any folder

Every command here was run on Windows 11 with PowerShell and Python 3.12. The paths use this machine's location, `D:\Placement\Companies\Bytevox`; replace it with wherever you put the project.

## 1. What you need

| Need | Why | Check |
|---|---|---|
| Python 3.11 or newer | runs the harness | `py -0` lists installed versions |
| Internet (once) | `pip install` of pydantic and pytest | |
| A Groq API key (optional, free) | real AI review | <https://console.groq.com/keys> |

Without a key, everything works in **static mode** (rule-based agents, no AI).

## 2. One-time setup

```powershell
# 1. Go to the project folder (or copy/clone it anywhere first, then cd there)
cd D:\Placement\Companies\Bytevox

# 2. Create a virtual environment and install the three dependencies
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Check it works: 67 tests, about 5 seconds
.\.venv\Scripts\python.exe -m pytest -q

# 4. (Optional) Add your Groq key for AI review
Copy-Item .env.example .env
notepad .env          # set GROQ_API_KEY=gsk_...  and save
```

Notes:

- **Keys go in `.env`, never in `.env.example`.** `.env` is gitignored; `.env.example` gets committed. RepoGuard reads only the `.env` in its own folder, never one inside a project you review.
- **Activating is optional.** `.\.venv\Scripts\Activate.ps1` lets you type `python` instead of `.\.venv\Scripts\python.exe`. If PowerShell refuses ("running scripts is disabled"), run `Set-ExecutionPolicy -Scope Process Bypass` first. That only affects the current window.
- **Moved or copied the folder?** A venv stores absolute paths, so delete `.venv` and repeat step 2 in the new location.

## 3. Try it on the built-in examples

From the RepoGuard folder (venv activated, or replace `python` with `.\.venv\Scripts\python.exe`):

```powershell
python app.py review fixtures/synthetic_repo                      # static: no AI, under 1 s
python app.py review fixtures/synthetic_repo --provider groq      # AI: asks y/N before sending code, about 8 s
python app.py baseline fixtures/synthetic_repo --provider groq    # same task, one agent (for comparison)
python demo.py                                                    # 7 scenes: retries, partial results, limits, checker, injection, approval
```

## 4. Review any other project

RepoGuard only **reads** the project's `.py` files. It skips `.venv`, `venv`, `site-packages`, `node_modules`, `build`, `dist`, `.git` and caches, and never changes your code.

### Option A: from inside your project, using the launcher (recommended)

`repoguard.cmd` finds RepoGuard's own `.venv` by itself, so nothing needs to be activated:

```powershell
cd C:\path\to\your-project
D:\Placement\Companies\Bytevox\repoguard.cmd review . --provider groq
```

To type just `repoguard`, add the folder to your PATH once (then open a new terminal):

```powershell
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
[Environment]::SetEnvironmentVariable("Path", "$userPath;D:\Placement\Companies\Bytevox", "User")
# new terminal:
cd C:\path\to\your-project
repoguard review . --provider groq
```

### Option B: give the project path

```powershell
D:\Placement\Companies\Bytevox\.venv\Scripts\python.exe D:\Placement\Companies\Bytevox\app.py review C:\path\to\your-project --provider groq
```

### Focus the review (optional)

```powershell
repoguard review . --provider groq --goal "Check the API handlers for injection and auth problems"
repoguard review . --provider groq --paths api/handlers.py api/auth.py
```

The lead agent picks the specialists from the goal. For example, "unsafe input" sends only the security auditor.

### The approval prompt

With a hosted model, the code leaves your machine, so RepoGuard asks first:

```
[approval needed] paid_model_call: send 1 file(s) to openai/gpt-oss-120b @ api.groq.com
    Approve? [y/N]
```

Type `y`. To skip the prompt, add `--yes`; to refuse automatically, add `--deny-risky`. In a non-interactive shell (CI, a script) there is nobody to ask, so the answer is **no** unless you pass `--yes`.

## 5. Read the results

The terminal prints each verified finding with the first line of its fix. Everything is saved in `runs\<project-folder-name>\` **inside the RepoGuard folder** (not in your project):

| File | Contents |
|---|---|
| `<run-id>.md` | the report: findings by severity, location, quoted evidence, rationale, **suggested fix** code block |
| `<run-id>.html` | the same in a browser, plus each agent's tool calls, tokens, seconds and the full timeline |
| `<run-id>.json` | machine-readable results |
| `<run-id>.jsonl` | the trace: every step, replayable |
| `<run-id>.state.json` | the shared state all agents wrote to |

```powershell
function latest($pattern) { (Get-ChildItem $pattern | Sort-Object LastWriteTime | Select-Object -Last 1).FullName }
start (latest D:\Placement\Companies\Bytevox\runs\your-project\*.html)                               # open the newest viewer
D:\Placement\Companies\Bytevox\repoguard.cmd trace (latest D:\Placement\Companies\Bytevox\runs\your-project\*.jsonl)   # timeline
```

To put the report **into** your project, add `--export REVIEW.md`. Writing outside `runs\` is a risky action, so it asks for approval; `--yes` covers it.

Then apply the fixes you agree with and run the review again: fixed issues disappear from the report.

## 6. Options you will use

| Option | Meaning |
|---|---|
| `--provider static / groq / gemini / ollama / openai` | which brain the agents use (default `static`) |
| `--model NAME` | another model, e.g. `--model qwen/qwen3.8-27b` |
| `--goal "..."` | plain-English goal; decides which specialists run |
| `--paths a.py b.py` | review only these files |
| `--yes` / `--deny-risky` | approve / refuse risky actions without asking |
| `--export FILE` | copy the Markdown report somewhere (approval if outside `runs\`) |
| `--max-calls`, `--max-tokens`, `--max-agent-tokens`, `--timeout`, `--run-time-budget` | limits; the run stops cleanly and says which one was hit |
| `--runs-dir DIR` | save results somewhere else |

`python app.py review --help` lists everything. The exit code is 0 when complete, 1 when the result is **partial**, and 2 when the run couldn't start.

## 7. Troubleshooting

| Message | Fix |
|---|---|
| `No suitable Python runtime found` for `py -3.11` | use `py -3` (any 3.11+), or install Python from python.org |
| `running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope Process Bypass`, or skip activation and use `.\.venv\Scripts\python.exe` |
| `ModuleNotFoundError: No module named 'pydantic'` | you're using a Python without the deps: run step 2, or use the launcher / `.venv\Scripts\python.exe` |
| `Review could not start: GROQ_API_KEY must be set` | put the key in RepoGuard's `.env` (not `.env.example`), or `$env:GROQ_API_KEY = "gsk_..."` |
| `PARTIAL RESULT: approval denied for paid model call` | you answered no, or the shell isn't interactive: add `--yes` |
| `PARTIAL RESULT: ... model unavailable, returned scanner findings only` | Groq failed (rate limit, outage). You still get the rule-based findings; wait a minute and re-run |
| `PARTIAL RESULT: ... budget exhausted` on a big project | raise `--max-tokens` / `--max-calls`, or narrow it with `--paths` |
| Finds nothing in a non-Python project | only `.py` files are reviewed |
