from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterable

from core.schemas import Finding, Severity
from core.security import redact_text
from tools.tool_gate import (
    TOOL_AST_BUG_RULES, TOOL_AST_QUALITY_RULES, TOOL_AST_SECURITY_RULES, TOOL_SECRET_SCAN,
)

# Each static tool owns a disjoint set of rules, so every specialist can be
# granted only the scanner that matches its role.
RULES_BY_TOOL: dict[str, frozenset[str]] = {
    TOOL_AST_BUG_RULES: frozenset({"unguarded-division", "syntax-error"}),
    TOOL_AST_SECURITY_RULES: frozenset({"dynamic-eval", "dynamic-exec", "os-system", "subprocess-shell", "sql-injection"}),
    TOOL_SECRET_SCAN: frozenset({"hardcoded-secret"}),
    TOOL_AST_QUALITY_RULES: frozenset({"mutable-default"}),
}
_SECURITY_RULES = RULES_BY_TOOL[TOOL_AST_SECURITY_RULES] | RULES_BY_TOOL[TOOL_SECRET_SCAN]


def _finding(path: str, line: int, snippet: str, rule: str, severity: Severity,
             description: str, rationale: str, fix: str | None = None) -> Finding:
    digest = hashlib.sha1(f"{path}:{line}:{rule}".encode()).hexdigest()[:10]
    return Finding(
        id=f"RG-{digest}", severity=severity, description=description,
        file_path=path, line_numbers=[line], code_evidence=redact_text(snippet.strip())[:300],
        rationale=rationale, confidence=0.96, rule=rule, suggested_fix=redact_text(fix) if fix else None,
        category=("security" if rule in _SECURITY_RULES
                  else "bug" if rule in RULES_BY_TOOL[TOOL_AST_BUG_RULES] else "quality"),
    )


_SECRET_WORDS = ("secret", "password", "token", "api_key")


def _is_label(value: str) -> bool:
    """True for empty strings and snake_case names like "secret_scan" that only name the concept."""
    return not value or (re.fullmatch(r"[a-z]+(_[a-z]+)+", value) is not None
                         and any(word in value for word in _SECRET_WORDS))


def inspect_python(path: str, source: str, tools: Iterable[str] | None = None) -> list[Finding]:
    """Run the static rules owned by ``tools`` (all rules when ``tools`` is None)."""
    if tools is None:
        return _inspect_all(path, source)
    rules = frozenset().union(*(RULES_BY_TOOL[tool] for tool in tools))
    return [item for item in _inspect_all(path, source) if item.rule in rules]


def _indent(original_line: str, code: str) -> str:
    """Indent replacement code to match the line it replaces."""
    prefix = original_line[: len(original_line) - len(original_line.lstrip())]
    return "\n".join(prefix + part for part in code.splitlines())


def _target_name(targets: list[ast.expr]) -> str:
    return next((target.id for target in targets if isinstance(target, ast.Name)), "SECRET")


def _eval_fix(function: str, line: str) -> str:
    if function == "eval":
        return (_indent(line, line.strip().replace("eval(", "ast.literal_eval(", 1))
                + "\n# import ast; literal_eval accepts only Python literals. For arithmetic, parse with ast "
                  "and allow only number and operator nodes.")
    return "Remove exec(); map the allowed actions to functions in a dict and call the selected function."


def _shell_fix(line: str) -> str:
    code = re.sub(r"subprocess\.run\(\s*([A-Za-z_]\w*)", r"subprocess.run(shlex.split(\1)", line.strip(), count=1)
    code = code.replace("shell=True", "shell=False, check=True")
    return _indent(line, code) + "\n# import shlex; better still, accept a list and allowlist the program name."


def _is_built_string(node: ast.expr) -> bool:
    """f-string with values, or a string combined with + / % (possible SQL text)."""
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(part, ast.FormattedValue) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        sides = (node.left, node.right)
        return any(isinstance(side, ast.Constant) and isinstance(side.value, str)
                   and any(word in side.value.upper() for word in ("SELECT", "INSERT", "UPDATE", "DELETE", "WHERE"))
                   for side in sides)
    return False


def _inspect_all(path: str, source: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        line = max(exc.lineno or 1, 1)
        return [_finding(path, line, lines[line - 1] if line <= len(lines) else "",
                         "syntax-error", Severity.high, "Python file has a syntax error",
                         f"The parser reports {exc.msg} on this line.",
                         f"Fix the syntax error the parser reports here: {exc.msg}.")]

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            names = [target.id.lower() for target in targets if isinstance(target, ast.Name)]
            if any(any(word in name for word in _SECRET_WORDS) for name in names):
                if isinstance(value, ast.Constant) and isinstance(value.value, str) and not _is_label(value.value):
                    line = node.lineno
                    findings.append(_finding(
                        path, line, lines[line - 1], "hardcoded-secret", Severity.high,
                        "Possible hardcoded credential", "Credential-like variable is assigned a string literal; load secrets from the environment or a secret store.",
                        f'Remove the literal and read it at runtime with os.environ["{_target_name(targets)}"] '
                        "(import os); set the value in the environment or a secret manager, and rotate the exposed one.",
                    ))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
            line = node.lineno
            findings.append(_finding(
                path, line, lines[line - 1], f"dynamic-{node.func.id}", Severity.high,
                f"Use of {node.func.id} can execute attacker-controlled Python",
                "Dynamic code execution is unsafe when any part of the expression is untrusted.",
                _eval_fix(node.func.id, lines[line - 1]),
            ))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "system" and isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                line = node.lineno
                findings.append(_finding(
                    path, line, lines[line - 1], "os-system", Severity.high,
                    "Shell command is constructed through os.system",
                    "Shell interpretation can turn untrusted arguments into command execution.",
                    _indent(lines[line - 1], "subprocess.run([program, *arguments], check=True)  "
                            "# import subprocess; pass a list, no shell, allowlist the program"),
                ))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run":
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
                shell_true = any(
                    keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                    for keyword in node.keywords
                )
                if shell_true:
                    line = node.lineno
                    findings.append(_finding(
                        path, line, lines[line - 1], "subprocess-shell", Severity.high,
                        "subprocess.run enables shell=True",
                        "Shell command parsing can enable command injection if arguments contain untrusted data.",
                        _shell_fix(lines[line - 1]),
                    ))

    # Queries built with f-strings, + or % and passed to .execute(): classic SQL injection.
    query_vars = {
        target.id: node.value for node in ast.walk(tree) if isinstance(node, ast.Assign) and _is_built_string(node.value)
        for target in node.targets if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"execute", "executemany"} \
                and node.args:
            query = node.args[0]
            built = _is_built_string(query) or (isinstance(query, ast.Name) and query.id in query_vars)
            if built:
                line = node.lineno
                findings.append(_finding(
                    path, line, lines[line - 1], "sql-injection", Severity.high,
                    "SQL query is built from strings and passed to execute()",
                    "Interpolating values into SQL lets crafted input change the query; use bound parameters.",
                    _indent(lines[line - 1], 'conn.execute("SELECT ... WHERE name = ?", (name,))  '
                            "# placeholders + a parameter tuple; the driver escapes values (%s for psycopg/MySQL)"),
                ))

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and isinstance(node.right, ast.Name):
            line = node.lineno
            findings.append(_finding(
                path, line, lines[line - 1], "unguarded-division", Severity.medium,
                "Division may fail when the divisor is zero",
                "The divisor is a variable and this expression does not establish a non-zero precondition.",
                _indent(lines[line - 1], f'if {node.right.id} == 0:\n    raise ValueError("{node.right.id} must not be zero")\n'
                        + lines[line - 1].strip()),
            ))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = node.args.posonlyargs + node.args.args
            for argument, default in zip(positional[len(positional) - len(node.args.defaults):], node.args.defaults):
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    line = default.lineno
                    empty = {ast.List: "[]", ast.Dict: "{}", ast.Set: "set()"}[type(default)]
                    findings.append(_finding(
                        path, line, lines[line - 1], "mutable-default", Severity.medium,
                        "Function uses a mutable default argument",
                        "The same mutable object is reused across calls, which can leak state between invocations.",
                        f"Default `{argument.arg}` to None and create the object inside the function:\n"
                        f"    def {node.name}(..., {argument.arg}=None):\n"
                        f"        if {argument.arg} is None:\n"
                        f"            {argument.arg} = {empty}",
                    ))
    return findings


def inspect_files(files: Iterable[tuple[str, str]], tools: Iterable[str] | None = None) -> list[Finding]:
    selected = None if tools is None else list(tools)
    return [finding for path, source in files for finding in inspect_python(path, source, selected)]