from __future__ import annotations

from pathlib import Path


# Dependency, cache and VCS folders are not the project's own code.
SKIP_DIRS = frozenset({".venv", "venv", "env", ".env", "site-packages", "node_modules", "__pycache__",
                       ".git", ".tox", ".mypy_cache", ".pytest_cache", "build", "dist"})


class RepositoryAccessError(ValueError):
    pass


class SafeRepository:
    """Read-only access to Python files below one resolved repository root."""

    def __init__(self, root: str | Path, max_file_bytes: int = 200_000):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise RepositoryAccessError("repository root must be a directory")
        self.max_file_bytes = max_file_bytes

    def _resolve(self, relative_path: str | Path) -> Path:
        path = Path(relative_path)
        if path.is_absolute():
            raise RepositoryAccessError("absolute paths are not allowed")
        candidate = self.root / path
        try:
            candidate.resolve(strict=False).relative_to(self.root)
        except ValueError as exc:
            raise RepositoryAccessError("path escapes repository root") from exc
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise RepositoryAccessError("path does not exist") from exc
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise RepositoryAccessError("path escapes repository root") from exc
        if not candidate.is_file() or candidate.suffix != ".py":
            raise RepositoryAccessError("only existing Python files can be read")
        if candidate.stat().st_size > self.max_file_bytes:
            raise RepositoryAccessError("file exceeds configured size limit")
        return candidate

    def list_python_files(self, paths: list[str] | None = None) -> list[str]:
        if paths:
            resolved = [self._resolve(path) for path in paths]
            return sorted(path.relative_to(self.root).as_posix() for path in resolved)
        found: list[str] = []
        for candidate in self.root.rglob("*.py"):
            if SKIP_DIRS.intersection(candidate.relative_to(self.root).parts[:-1]):
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self.root)
                if candidate.is_symlink() or not resolved.is_file():
                    continue
                if resolved.stat().st_size <= self.max_file_bytes:
                    found.append(resolved.relative_to(self.root).as_posix())
            except (OSError, ValueError):
                continue
        return sorted(found)

    def read_file(self, relative_path: str) -> str:
        path = self._resolve(relative_path)
        return path.read_text(encoding="utf-8", errors="replace")