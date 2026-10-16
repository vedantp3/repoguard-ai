from __future__ import annotations

import re


_ASSIGNMENT_SECRET = re.compile(
    r"(?i)(\b[\w-]*(?:api[_-]?key|secret|token|password|passwd)[\w-]*\b\s*[:=]\s*)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;#]+)"
)
_TOKEN_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")
_BEARER_SECRET = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]+=*")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


# Unquoted right-hand sides that are code, not secret values: env lookups, calls, subscripts, constants.
_CODE_VALUE = re.compile(r"(?:os\.|getenv\b|environ\b|(?:None|True|False)\b|[A-Za-z_][\w.]*\s*[(\[])")


def _redact_assignment(match: re.Match[str]) -> str:
    value = match.group(2)
    if value[0] not in "\"'" and _CODE_VALUE.match(value):
        return match.group(0)  # e.g. DB_PASSWORD = os.getenv("DB_PASSWORD") is the fix, not a leak
    return f'{match.group(1)}"[REDACTED]"'


def redact_text(value: str) -> str:
    value = _ASSIGNMENT_SECRET.sub(_redact_assignment, value)
    value = _TOKEN_SECRET.sub("[REDACTED]", value)
    value = _BEARER_SECRET.sub(r"\1[REDACTED]", value)
    return _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", value)


def redact_object(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_object(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_object(item) for key, item in value.items()}
    return value