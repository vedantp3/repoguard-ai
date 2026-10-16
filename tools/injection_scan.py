"""Flag text in repository files that tries to instruct the agents.

This never blocks a run and never removes content. Hits are logged, shown in
the report, and named in the model prompt so the model is told explicitly that
those lines are data. The real protection is structural: agents have no write
or execution tools, and every finding must survive evidence verification.
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override-instructions", re.compile(
        r"(?i)\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|policy|policies|rule|prompt|guideline)s?\b")),
    # Comments only, so code such as ``agent: str`` does not match.
    ("addresses-the-agent", re.compile(r"(?i)#.*\b(agent|assistant|ai|llm|reviewer|model)\s*[:,]")),
    ("role-reassignment", re.compile(r"(?i)\byou are (now|no longer)\b|\bact as\b|\bnew instructions?\b")),
    ("verdict-dictation", re.compile(
        r"(?i)\b(claim|say|report|mark|declare)\b.{0,40}\b(safe|secure|no (issues|findings|vulnerabilities))\b")),
    ("prompt-exfiltration", re.compile(r"(?i)\b(reveal|print|show|repeat)\b.{0,30}\b(system prompt|api key|secret)s?\b")),
]


def scan_text(path: str, source: str) -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        matched = [name for name, pattern in _PATTERNS if pattern.search(line)]
        if matched:
            hits.append({"file": path, "line": number, "patterns": matched, "text": line.strip()[:200]})
    return hits


def scan_files(files: dict[str, str]) -> list[dict[str, object]]:
    return [hit for path, source in files.items() for hit in scan_text(path, source)]
