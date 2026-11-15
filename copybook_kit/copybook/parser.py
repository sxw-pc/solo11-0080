"""Parser entry point for the deliberately small copybook subset."""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from ..errors import Diagnostic
from ..model import Field
from .clauses import ALLOWED_LEVELS, FIELD_NAME_RE, diag, parse_clauses
from .lexer import LogicalLine, lex_copybook

@dataclass
class ParseResult:
    roots: list[Field]
    errors: list[Diagnostic] = dc_field(default_factory=list)
    warnings: list[Diagnostic] = dc_field(default_factory=list)

def _parse_line(line: LogicalLine) -> tuple[Field | None, list[Diagnostic]]:
    text = line.text[:-1].rstrip() if line.text.endswith(".") else line.text
    tokens = text.split()
    if len(tokens) < 2:
        return None, [diag("CB007", "copybook statement is incomplete", line)]
    level_text = tokens[0]
    if not re.fullmatch(r"\d{2}", level_text):
        return None, [diag("CB004", f"invalid level {level_text!r}", line)]
    if int(level_text) not in ALLOWED_LEVELS:
        return None, [diag("CB004", f"level {level_text} is not supported", line)]
    name = tokens[1]
    if not FIELD_NAME_RE.fullmatch(name):
        return None, [diag("CB007", f"invalid field name {name!r}", line)]
    entry = Field(name=name, level=int(level_text))
    return entry, parse_clauses(tokens[2:], entry, line)

def _validate_entry(entry: Field) -> list[Diagnostic]:
    failures: list[tuple[str, str]] = []
    if entry.children and entry.pic is not None:
        failures.append(("CB007", f"group {entry.name} cannot declare PIC"))
    if not entry.children and entry.pic is None:
        failures.append(("CB007", f"{entry.name} has neither PIC nor children"))
    if entry.is_group and (entry.usage != "DISPLAY" or entry.sign != "NONE" or entry.separate):
        failures.append(("CB007", f"group {entry.name} cannot declare USAGE/SIGN"))
    if entry.pic and entry.usage == "COMP-3" and not entry.is_numeric:
        failures.append(("CB007", f"COMP-3 requires a numeric PIC on {entry.name}"))
    if entry.usage == "COMP-3" and (entry.sign != "NONE" or entry.separate):
        failures.append(("CB007", f"COMP-3 {entry.name} cannot use SIGN SEPARATE"))
    if entry.sign != "NONE" and not entry.signed:
        failures.append(("CB007", f"{entry.name} declares SIGN but its PIC is unsigned"))
    return [diag(code, message, None, field_name=entry.name) for code, message in failures]

def _walk(entries: list[Field]):
    for entry in entries:
        yield entry
        yield from _walk(entry.children)

def parse_copybook(source: str) -> ParseResult:
    lexed = lex_copybook(source)
    errors, roots, stack = list(lexed.errors), [], []
    for line in lexed.lines:
        entry, found = _parse_line(line)
        errors.extend(found)
        if entry is None:
            continue
        if entry.level == 1:
            if roots:
                errors.append(diag("CB004", "multiple 01 records are not supported", line, field_name=entry.name))
                continue
            roots, stack = [entry], [entry]
            continue
        if not stack:
            errors.append(diag("CB004", "the first statement must be level 01", line, field_name=entry.name))
            continue
        while stack and entry.level <= stack[-1].level:
            stack.pop()
        if not stack:
            errors.append(diag("CB004", f"level {entry.level:02d} has no level-01 parent", line, field_name=entry.name))
            continue
        parent = stack[-1]
        if parent.pic is not None:
            errors.append(diag("CB004", f"elementary {parent.name} cannot have children", line, field_name=entry.name))
            continue
        parent.children.append(entry)
        stack.append(entry)
    for entry in _walk(roots):
        errors.extend(_validate_entry(entry))
    return ParseResult(roots=roots, errors=errors)
