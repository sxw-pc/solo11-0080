"""Field checks, cross-field comparison and unique-key state."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..model import Record, RuleHit
from ..util import decimal_text
from .schema import FieldRule, RecordRule, RuleSet

LEAF_INDEX = re.compile(r"\[\d+\]$")

def leaf_name(path: str) -> str:
    return LEAF_INDEX.sub("", path.rsplit(".", 1)[-1])

def values_for(record: Record, field_name: str) -> list[tuple[str, Any]]:
    return [(path, value) for path, value in record.flat.items() if leaf_name(path) == field_name]

def first_value(record: Record, field_name: str) -> Any:
    found = values_for(record, field_name)
    return found[0][1] if found else None

def scalar_text(value: Any) -> str:
    if value is None:
        return ""
    return decimal_text(value) if isinstance(value, Decimal) else str(value)

class ValidationState:
    """Mutable state carried across records in file order."""

    def __init__(self) -> None:
        self.unique: dict[str, set[tuple[str, ...]]] = {}

    def reset(self) -> None:
        self.unique.clear()

def _hit(record: Record, code: str, message: str, scope: str, field: str | None = None,
         **detail: Any) -> RuleHit:
    return RuleHit(record.record_no, code, message, scope, field, detail)

def _enum(value: Any, rule: FieldRule, record: Record, path: str) -> RuleHit | None:
    if not rule.enum:
        return None
    allowed = {scalar_text(item) for item in rule.enum}
    if scalar_text(value) in allowed:
        return None
    return _hit(record, "R002_ENUM", f"{rule.name} value {scalar_text(value)!r} is not in enum",
                "field", path, allowed=sorted(allowed))

def _parse_date(value: Any, fmt: str) -> date | None:
    try:
        return datetime.strptime(scalar_text(value), fmt).date()
    except (TypeError, ValueError):
        return None

def _date_bound(text: str, fmt: str) -> date | None:
    parsed = _parse_date(text, fmt)
    if parsed is not None:
        return parsed
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None

def _range(value: Any, rule: FieldRule, record: Record, path: str) -> RuleHit | None:
    if rule.range_min is None and rule.range_max is None:
        return None
    if rule.date_format:
        current = _parse_date(value, rule.date_format)
        if current is None:
            return None
        minimum = _date_bound(rule.range_min, rule.date_format) if rule.range_min else None
        maximum = _date_bound(rule.range_max, rule.date_format) if rule.range_max else None
        if (minimum is None and rule.range_min) or (maximum is None and rule.range_max):
            return _hit(record, "R003_RANGE", f"{rule.name} range bound is not a date", "field", path)
        if (minimum is not None and current < minimum) or (maximum is not None and current > maximum):
            return _hit(record, "R003_RANGE", f"{rule.name} is outside the date range", "field", path)
        return None
    try:
        number = Decimal(scalar_text(value))
        minimum = Decimal(rule.range_min) if rule.range_min is not None else None
        maximum = Decimal(rule.range_max) if rule.range_max is not None else None
    except (InvalidOperation, ValueError):
        return _hit(record, "R003_RANGE", f"{rule.name} is not numeric for range check", "field", path)
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        return _hit(record, "R003_RANGE", f"{rule.name} is outside the declared range", "field", path)
    return None

def _check_digit(value: Any, rule: FieldRule, record: Record, path: str) -> RuleHit | None:
    if rule.check_digit is None:
        return None
    digits, weights = scalar_text(value), rule.check_digit.weights
    if len(digits) != len(weights) + 1 or not digits.isascii() or not digits.isdigit():
        return _hit(record, "R006_CHECK_DIGIT", f"{rule.name} check digit cannot be evaluated", "field", path)
    total = sum(int(digit) * weight for digit, weight in zip(digits[:-1], weights))
    expected = (rule.check_digit.modulus - total % rule.check_digit.modulus) % rule.check_digit.modulus
    if expected >= 10 or int(digits[-1]) != expected:
        return _hit(record, "R006_CHECK_DIGIT", f"{rule.name} check digit is invalid", "field", path)
    return None

def validate_field(record: Record, path: str, value: Any, rule: FieldRule) -> list[RuleHit]:
    if rule.required and (value is None or scalar_text(value) == ""):
        return [_hit(record, "R001_REQUIRED", f"{rule.name} is required", "field", path)]
    if value is None or scalar_text(value) == "":
        return []
    if rule.date_format is not None and _parse_date(value, rule.date_format) is None:
        return [_hit(record, "R004_DATE", f"{rule.name} does not match date_format {rule.date_format}",
                     "field", path, value=scalar_text(value))]
    hits = [_enum(value, rule, record, path), _range(value, rule, record, path),
            None if rule.pattern is None or re.fullmatch(rule.pattern, scalar_text(value))
            else _hit(record, "R005_PATTERN", f"{rule.name} does not match pattern", "field", path),
            _check_digit(value, rule, record, path)]
    if rule.scale is not None and isinstance(value, Decimal):
        places = max(0, -value.as_tuple().exponent)
        hits.append(None if places <= rule.scale else _hit(
            record, "R007_SCALE", f"{rule.name} has scale {places}, maximum is {rule.scale}", "field", path))
    return [hit for hit in hits if hit is not None]

def _compare(left: Any, op: str, right: Any) -> bool:
    try:
        left_value: Any = Decimal(scalar_text(left))
        right_value: Any = Decimal(scalar_text(right))
    except (InvalidOperation, ValueError):
        left_value, right_value = scalar_text(left), scalar_text(right)
    if op == "<=":
        return left_value <= right_value
    if op == "<":
        return left_value < right_value
    if op == "==":
        return left_value == right_value
    return left_value != right_value

def cross_hit(record: Record, rule: RecordRule) -> RuleHit | None:
    left, right = first_value(record, rule.left or ""), first_value(record, rule.right or "")
    if left is None or right is None or _compare(left, rule.op or "==", right):
        return None
    return _hit(record, "R009_CROSS_FIELD", f"{rule.left} {rule.op} {rule.right} failed", "record",
                None, left=scalar_text(left), right=scalar_text(right), op=rule.op)

def unique_checks(record: Record, rules: RuleSet, state: ValidationState) -> list[RuleHit]:
    hits: list[RuleHit] = []
    pending: list[tuple[str, tuple[str, ...]]] = []
    for rule in rules.record_rules:
        if rule.kind != "unique_key":
            continue
        values: list[str] = []
        for name in rule.fields:
            found = values_for(record, name)
            if not found or found[0][1] is None:
                hits.append(_hit(record, "R008_UNIQUE", f"unique key {rule.name} has a missing component", "record"))
                break
            values.append(scalar_text(found[0][1]))
        else:
            key = tuple(values)
            if key in state.unique.get(rule.name, set()):
                hits.append(_hit(record, "R008_UNIQUE", f"duplicate unique key for {rule.name}",
                                 "record", None, key=key))
            else:
                pending.append((rule.name, key))
    if hits:
        return hits
    for rule_name, key in pending:
        state.unique.setdefault(rule_name, set()).add(key)
    return []
