"""Deterministic field profiling over accepted data records."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from ..model import Record
from ..rules.checks import leaf_name
from ..util import decimal_text, json_safe

def _sortable(value: str) -> tuple[int, str]:
    try:
        return (0, format(Decimal(value), "f").rjust(40, "0"))
    except (InvalidOperation, TypeError, ValueError):
        return (1, value)

def _profile_path(path: str, values: list[Any]) -> dict[str, Any]:
    present = [value for value in values if value is not None]
    text_values = [decimal_text(value) if isinstance(value, Decimal) else str(value) for value in present]
    counter = Counter(text_values)
    top = [
        {"value": value, "count": count}
        for value, count in sorted(counter.items(), key=lambda item: (-item[1], _sortable(item[0])))
        [:5]
    ]
    numeric = [value for value in present if isinstance(value, Decimal)]
    text = [value for value in present if not isinstance(value, Decimal)]
    if numeric and text:
        field_type = "mixed"
    elif numeric:
        field_type = "number"
    else:
        field_type = "text"
    payload: dict[str, Any] = {
        "path": path,
        "name": leaf_name(path),
        "field_type": field_type,
        "observation_count": len(values),
        "present_count": len(present),
        "null_count": len(values) - len(present),
        "distinct_count": len(counter),
        "top_values": top,
    }
    if numeric:
        payload["minimum"] = decimal_text(min(numeric))
        payload["maximum"] = decimal_text(max(numeric))
    if text:
        lengths = [len(value) for value in text]
        payload["min_length"] = min(lengths)
        payload["max_length"] = max(lengths)
        payload["minimum"] = min(text_values)
        payload["maximum"] = max(text_values)
    return payload

def build_profile(records: list[Record], generated_at: str) -> dict[str, Any]:
    """Profile accepted data records only; paths are sorted lexicographically."""

    values_by_path: dict[str, list[Any]] = {}
    for record in records:
        for path, value in record.flat.items():
            values_by_path.setdefault(path, []).append(value)
    fields = [
        _profile_path(path, values_by_path[path])
        for path in sorted(values_by_path)
    ]
    return json_safe(
        {
            "schema_version": 1,
            "generated_at": generated_at,
            "record_count": len(records),
            "field_count": len(fields),
            "fields": fields,
        }
    )
