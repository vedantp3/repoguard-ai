"""Load ``KEY=value`` lines from the harness's own ``.env`` file.

Only the file next to ``app.py`` is read (never one inside the repository being
reviewed), and variables already set in the environment win. Values are never
printed or logged.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path) -> list[str]:
    """Set missing variables from ``path``; return the names that were loaded."""
    env_path = Path(path)
    if not env_path.is_file():
        return []
    loaded = []
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and value and name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded
