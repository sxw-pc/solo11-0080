"""Fixed-format copybook columns, comments and continuation lines."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import Diagnostic

@dataclass(frozen=True)
class LogicalLine:
    text: str
    line_no: int

@dataclass(frozen=True)
class LexResult:
    lines: list[LogicalLine]
    errors: list[Diagnostic]

def _error(line_no: int, message: str, column: int | None = None) -> Diagnostic:
    return Diagnostic("CB008", message, line=line_no, column=column)

def lex_copybook(source: str) -> LexResult:
    """Read columns 8-72; columns 1-6 are sequence area and 73+ are ignored."""
    lines: list[LogicalLine] = []
    errors: list[Diagnostic] = []
    pending: list[str] = []
    pending_line: int | None = None

    def flush() -> None:
        nonlocal pending, pending_line
        text = " ".join(part for part in pending if part).strip()
        if pending_line is not None and text:
            lines.append(LogicalLine(text, pending_line))
        pending, pending_line = [], None

    for line_no, raw in enumerate(source.splitlines(), start=1):
        if not raw.strip():
            continue
        if len(raw) < 7:
            errors.append(_error(line_no, "a non-empty line needs columns 1-7", 1))
            continue
        if "\t" in raw:
            errors.append(_error(line_no, "tabs are not allowed in fixed-format input", 1))
            continue
        indicator, body = raw[6], raw[7:72].rstrip()
        if indicator in "*/":
            continue
        if indicator == "-":
            if pending_line is None:
                errors.append(_error(line_no, "continuation has no preceding line", 7))
            elif body:
                pending.append(body)
            continue
        if indicator != " ":
            errors.append(_error(line_no, f"invalid indicator {indicator!r}", 7))
            continue
        flush()
        if body:
            pending, pending_line = [body], line_no
    flush()
    return LexResult(lines, errors)

def lex_file(path: str) -> LexResult:
    with open(path, encoding="utf-8") as handle:
        return lex_copybook(handle.read())
