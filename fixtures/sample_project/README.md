# Sample project (for demos)

A tiny "user service" with five planted problems: a hardcoded password, SQL built with an f-string,
a shell command via `os.system`, an unguarded division, and a mutable default argument.

Copy it anywhere and review it to show that RepoGuard works on any folder:

```powershell
Copy-Item -Recurse fixtures\sample_project D:\Placement\sample_project
cd D:\Placement\sample_project
D:\Placement\Companies\Bytevox\repoguard.cmd review . --provider groq
```
