"""PIC and clause parsing for the supported copybook subset."""

from __future__ import annotations

import re

from ..errors import Diagnostic
from ..model import Field
from .lexer import LogicalLine

ALLOWED_LEVELS = {1, 3, 5, *range(10, 50, 5), 49}
FIELD_NAME_RE = re.compile(r"^[A-Z][A-Z0-9-]*$")
CHAR_PIC_RE = re.compile(r"^([XA])(?:\((\d+)\))?$")
NUM_PIC_RE = re.compile(r"^(S)?9(?:\((\d+)\))?(?:V9\((\d+)\))?$")
UNSUPPORTED_USAGES = {"COMP", "COMP-1", "COMP-2", "BINARY", "PACKED-DECIMAL", "COMPUTATIONAL",
                      "COMPUTATIONAL-1", "COMPUTATIONAL-2"}

def diag(code: str, message: str, line: LogicalLine | None, *, field_name: str | None = None,
         column: int | None = None, severity: str = "error") -> Diagnostic:
    return Diagnostic(code, message, line=line.line_no if line else None, field=field_name,
                      column=column, severity=severity)

def parse_pic(pic: str, line: LogicalLine, name: str) -> tuple[Field, list[Diagnostic]]:
    empty = Field(name=name, level=0)
    if match := CHAR_PIC_RE.fullmatch(pic):
        size = int(match.group(2) or "1")
        if size <= 0:
            return empty, [diag("CB001", f"PIC {pic} has a non-positive size", line, field_name=name)]
        return Field(name=name, level=0, pic=pic, category="CHAR", length=size), []
    if not (match := NUM_PIC_RE.fullmatch(pic)):
        return empty, [diag("CB001", f"unsupported PIC {pic!r}", line, field_name=name)]
    whole, fraction = int(match.group(2) or "1"), int(match.group(3) or "0")
    if whole <= 0:
        return empty, [diag("CB001", f"PIC {pic} has zero integer digits", line, field_name=name)]
    return Field(name=name, level=0, pic=pic, category="NUMERIC", digits=whole + fraction,
                 scale=fraction, signed=bool(match.group(1))), []

def _pic(tokens, index, entry, line, seen):
    if "PIC" in seen:
        return index, [diag("CB007", "duplicate PIC clause", line, field_name=entry.name)]
    if index + 1 >= len(tokens):
        return index, [diag("CB007", "PIC is missing its picture string", line, field_name=entry.name)]
    seen.add("PIC")
    parsed, errors = parse_pic(tokens[index + 1], line, entry.name)
    for attr in ("pic", "category", "digits", "scale", "signed", "length"):
        setattr(entry, attr, getattr(parsed, attr))
    return index + 2, errors

def _usage(tokens, index, entry, line, seen):
    if index + 1 >= len(tokens):
        return index, [diag("CB007", "USAGE is missing a value", line, field_name=entry.name)]
    usage = tokens[index + 1].upper()
    if usage not in {"DISPLAY", "COMP-3"}:
        return index + 2, [diag("CB007", f"unsupported USAGE {usage}", line, field_name=entry.name)]
    entry.usage, _ = usage, seen.add("USAGE")
    return index + 2, []

def _occurs(tokens, index, entry, line, seen):
    if "OCCURS" in seen:
        return index, [diag("CB007", "duplicate OCCURS clause", line, field_name=entry.name)]
    seen.add("OCCURS")
    if index + 1 >= len(tokens):
        return index, [diag("CB002", "OCCURS is missing a count", line, field_name=entry.name)]
    token = tokens[index + 1]
    try:
        count = int(token)
    except ValueError:
        count = 0
    if not 1 <= count <= 9999:
        return index, [diag("CB002", f"OCCURS count {token!r} must be 1-9999", line, field_name=entry.name)]
    entry.occurs, index = count, index + 2
    if index < len(tokens) and tokens[index].upper() == "TIMES":
        return index + 1, []
    if index < len(tokens) and tokens[index].upper() == "DEPENDING":
        return index, [diag("CB007", "OCCURS DEPENDING ON is not supported", line, field_name=entry.name)]
    return index, [diag("CB007", "OCCURS must be followed by TIMES", line, field_name=entry.name)]

def _redefines(tokens, index, entry, line, seen):
    if "REDEFINES" in seen:
        return index, [diag("CB007", "duplicate REDEFINES clause", line, field_name=entry.name)]
    seen.add("REDEFINES")
    if index + 1 >= len(tokens):
        return index, [diag("CB003", "REDEFINES is missing a target", line, field_name=entry.name)]
    target = tokens[index + 1]
    if not FIELD_NAME_RE.fullmatch(target):
        return index, [diag("CB003", f"invalid REDEFINES target {target!r}", line, field_name=entry.name)]
    entry.redefines = target
    return index + 2, []

def _sign(tokens, index, entry, line, seen):
    if "SIGN" in seen:
        return index, [diag("CB007", "duplicate SIGN clause", line, field_name=entry.name)]
    seen.add("SIGN")
    if index + 2 >= len(tokens) or tokens[index + 1].upper() != "IS":
        return index, [diag("CB007", "SIGN must use 'SIGN IS LEADING|TRAILING'", line, field_name=entry.name)]
    placement = tokens[index + 2].upper()
    if placement not in {"LEADING", "TRAILING"}:
        return index, [diag("CB007", f"invalid sign placement {placement!r}", line, field_name=entry.name)]
    entry.sign, index = placement, index + 3
    if index < len(tokens) and tokens[index].upper() == "SEPARATE":
        entry.separate, index = True, index + 1
    return index, []

def parse_clauses(tokens: list[str], entry: Field, line: LogicalLine) -> list[Diagnostic]:
    """Consume clauses after ``level name`` and reject everything unknown."""
    errors, index, seen = [], 0, set()
    while index < len(tokens):
        token = tokens[index].upper()
        if token == "PIC":
            index, found = _pic(tokens, index, entry, line, seen)
        elif token == "USAGE":
            index, found = _usage(tokens, index, entry, line, seen)
        elif token == "COMP-3":
            index += 1
            found = [diag("CB007", "duplicate usage clause", line, field_name=entry.name)] if "USAGE" in seen else []
            entry.usage, _ = "COMP-3", seen.add("USAGE")
        elif token in UNSUPPORTED_USAGES | {"RENAMES", "COPY", "DEPENDING"}:
            index, found = index + 1, [diag("CB007", f"unsupported clause {token}", line, field_name=entry.name)]
        elif token == "OCCURS":
            index, found = _occurs(tokens, index, entry, line, seen)
        elif token == "REDEFINES":
            index, found = _redefines(tokens, index, entry, line, seen)
        elif token == "SIGN":
            index, found = _sign(tokens, index, entry, line, seen)
        else:
            index, found = index + 1, [diag("CB007", f"unexpected token {tokens[index - 1]!r}",
                                            line, field_name=entry.name)]
        errors.extend(found)
        if any(item.code == "CB007" for item in found):
            break
    return errors
