"""Compute byte storage for a parsed copybook, including OCCURS and REDEFINES."""

from __future__ import annotations

from ..errors import Diagnostic
from ..model import Field, Layout
from .parser import parse_copybook

def _storage_length(entry: Field) -> int:
    if entry.usage == "COMP-3":
        return (entry.digits + 2) // 2
    if entry.is_numeric:
        return entry.digits + (1 if entry.signed and entry.separate else 0)
    return entry.length

def _target(siblings: list[Field], entry: Field, index: int) -> Field | None:
    return next((item for item in siblings[:index] if item.name == entry.redefines), None)

def _layout_entry(entry: Field, errors: list[Diagnostic]) -> int:
    entry.length = _layout_children(entry.children, entry.offset, errors) if entry.children else _storage_length(entry)
    entry.total_length = entry.length * entry.occurs
    return entry.total_length

def _layout_children(children: list[Field], start: int, errors: list[Diagnostic]) -> int:
    cursor = maximum = start
    for index, child in enumerate(children):
        target = None
        if child.redefines:
            target = _target(children, child, index)
            if target is None:
                errors.append(Diagnostic(
                    "CB003", f"REDEFINES target {child.redefines!r} is not a preceding same-level field",
                    field=child.name,
                ))
                child.offset = cursor
                child.length = child.total_length = 0
                continue
            child.offset = target.offset
        else:
            child.offset = cursor
        length = _layout_entry(child, errors)
        if child.redefines:
            if length > target.total_length:
                errors.append(Diagnostic(
                    "CB005", f"REDEFINES {child.name} is larger than target {target.name}", field=child.name,
                ))
            continue
        cursor += length
        maximum = max(maximum, child.offset + length)
    return max(cursor, maximum) - start

def compile_copybook(source: str) -> Layout:
    parsed = parse_copybook(source)
    errors = list(parsed.errors)
    layout = Layout(parsed.roots, 0, errors, list(parsed.warnings))
    if len(parsed.roots) != 1:
        if not any(item.code == "CB004" for item in errors):
            errors.append(Diagnostic("CB006", "copybook must define exactly one 01 record"))
        return layout
    root = parsed.roots[0]
    root.offset = 0
    layout.record_length = _layout_entry(root, errors)
    if layout.record_length == 0:
        errors.append(Diagnostic("CB006", "record length is zero"))
    elif layout.record_length > 32760:
        errors.append(Diagnostic("CB009", f"record length {layout.record_length} exceeds the 32760 byte limit"))
    return layout

def compile_file(path: str) -> Layout:
    with open(path, encoding="utf-8") as handle:
        return compile_copybook(handle.read())
