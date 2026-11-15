"""Decode one fixed-width record into nested values, flat evidence and diagnostics."""

from __future__ import annotations

from typing import Any

from ..errors import Diagnostic
from ..model import Field, Layout, Record
from ..util import sha8
from .display import decode_field
from .failure import FieldDecodeError

def _diagnostic(field: Field, error: FieldDecodeError, path: str, absolute: int, record_no: int) -> Diagnostic:
    detail = dict(error.detail)
    offset = absolute
    if error.code == "RD001" and "relative_offset" in detail:
        offset += int(detail["relative_offset"])
        detail["byte_offset"] = offset
    elif "byte_offset" in detail:
        detail["byte_offset"] = absolute + int(detail["byte_offset"])
    return Diagnostic(error.code, error.message, field=path, byte_offset=offset,
                      record_no=record_no, detail=detail)

def decode_record(layout: Layout, raw: bytes, *, record_no: int, byte_offset: int,
                  charset: str, blank_policies: dict[str, str] | None = None) -> Record:
    record = Record(record_no=record_no, byte_offset=byte_offset, raw=raw)
    if len(raw) != layout.record_length:
        record.errors.append(Diagnostic(
            "RD002", f"record has {len(raw)} bytes, layout expects {layout.record_length}",
            byte_offset=byte_offset, record_no=record_no,
        ))
        return record
    policies = blank_policies or {}

    def put(container: dict[str, Any], field: Field, value: Any) -> None:
        if field.occurs > 1:
            container.setdefault(field.name, []).append(value)
        else:
            container[field.name] = value

    def walk(fields: list[Field], prefix: str, base_shift: int, values: dict[str, Any]) -> None:
        for field in fields:
            for index in range(field.occurs):
                path = f"{prefix}{field.name}{f'[{index}]' if field.occurs > 1 else ''}"
                shift = base_shift + index * field.length
                if field.is_group:
                    group: dict[str, Any] = {}
                    walk(field.children, f"{path}.", shift, group)
                    put(values, field, group)
                    continue
                relative = field.offset + shift
                absolute = byte_offset + relative
                field_bytes = raw[relative : relative + field.length]
                if len(field_bytes) != field.length:
                    record.errors.append(Diagnostic(
                        "RD002", f"record ended while reading {path}", field=path,
                        byte_offset=absolute, record_no=record_no,
                    ))
                    record.flat[path] = None
                    put(values, field, None)
                    continue
                record.evidence[path] = sha8(field_bytes)
                try:
                    value = decode_field(field, field_bytes, charset, policies.get(field.name, "null"))
                except FieldDecodeError as exc:
                    record.errors.append(_diagnostic(field, exc, path, absolute, record_no))
                    value = None
                record.flat[path] = value
                put(values, field, value)

    for root in layout.fields:
        walk(root.children if root.is_group else [root], "", 0, record.values)
    return record
