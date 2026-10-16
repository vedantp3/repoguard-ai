@echo off
rem RepoGuard launcher: works from any folder, e.g.  repoguard review . --provider groq
rem Uses this folder's .venv when it exists, otherwise the python on PATH.
set "REPOGUARD_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%REPOGUARD_PY%" set "REPOGUARD_PY=python"
"%REPOGUARD_PY%" "%~dp0app.py" %*
