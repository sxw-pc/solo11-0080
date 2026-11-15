"""YAML rules contract loading and layout cross-checks."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..errors import RulesError
from ..model import Layout
from .schema import (CROSS_OPS, RECORD_TYPES, FieldRule, RecordRule, RecordTypeRule,
                     RuleSet, TrailerRule, field_rule, parse_yaml)

def _record_rule(index: int, config: Any) -> RecordRule:
    if not isinstance(config, dict):
        raise RulesError(f"record_rules[{index}] must be a mapping")
    kind = config.get("kind")
    if kind not in RECORD_TYPES:
        raise RulesError(f"record_rules[{index}].kind must be one of {sorted(RECORD_TYPES)}")
    name = config.get("name") or f"{kind}-{index + 1}"
    if not isinstance(name, str) or not name:
        raise RulesError(f"record_rules[{index}].name must be a non-empty string", code="RC003")
    allowed = {"kind", "name", "fields"} if kind == "unique_key" else {"kind", "name", "left", "op", "right"}
    if set(config) - allowed:
        raise RulesError(f"record_rules[{index}] has unknown keys")
    if kind == "unique_key":
        fields = config.get("fields")
        if not isinstance(fields, list) or not fields or any(not isinstance(item, str) for item in fields):
            raise RulesError(f"record_rules[{index}].fields must be a non-empty list")
        return RecordRule(name=name, kind=kind, fields=tuple(fields))
    left, op, right = config.get("left"), config.get("op"), config.get("right")
    if not isinstance(left, str) or not isinstance(right, str) or op not in CROSS_OPS:
        raise RulesError(f"record_rules[{index}] needs left, right and op in {sorted(CROSS_OPS)}", code="RC003")
    return RecordRule(name=name, kind=kind, left=left, op=op, right=right)

def _record_type(config: Any) -> RecordTypeRule:
    if not isinstance(config, dict) or set(config) - {"field", "data", "header", "trailer"}:
        raise RulesError("record_type must contain field, data and optional header/trailer")
    if not isinstance(config.get("field"), str) or not isinstance(config.get("data"), str):
        raise RulesError("record_type.field and record_type.data are required strings")
    values = [item for item in (config["data"], config.get("header"), config.get("trailer")) if item is not None]
    if len(values) != len(set(values)):
        raise RulesError("record_type marker values must be distinct", code="RC003")
    return RecordTypeRule(config["field"], config["data"], config.get("header"), config.get("trailer"))

def _trailer(config: Any) -> TrailerRule:
    if not isinstance(config, dict) or set(config) - {"count_field", "amount_field"}:
        raise RulesError("trailer must contain count_field and/or amount_field")
    count_field, amount_field = config.get("count_field"), config.get("amount_field")
    for label, value in (("count_field", count_field), ("amount_field", amount_field)):
        if value is not None and not isinstance(value, str):
            raise RulesError(f"trailer.{label} must be a string", code="RC003")
    if not count_field and not amount_field:
        raise RulesError("trailer must declare count_field and/or amount_field")
    return TrailerRule(count_field, amount_field)

def _limits(config: Any) -> tuple[int | None, int | None]:
    if not isinstance(config, dict) or set(config) - {"min_records", "max_records"}:
        raise RulesError("limits may only contain min_records and max_records")
    minimum, maximum = config.get("min_records"), config.get("max_records")
    for label, value in (("min_records", minimum), ("max_records", maximum)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise RulesError(f"limits.{label} must be a non-negative integer", code="RC003")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise RulesError("limits.min_records exceeds limits.max_records", code="RC003")
    return minimum, maximum

def _validate_against_layout(rules: RuleSet, layout: Layout) -> None:
    leaves = layout.elementary_names()
    for rule in rules.fields.values():
        if rule.name not in leaves:
            raise RulesError(f"rules reference unknown elementary field {rule.name}", code="RC004")
        field = layout.find_field(rule.name)
        if rule.check_digit and field and len(rule.check_digit.weights) != field.digits - 1:
            raise RulesError(f"check_digit weights for {rule.name} must have {field.digits - 1} entries", code="RC003")
        if rule.range_min and rule.range_max:
            try:
                if Decimal(rule.range_min) > Decimal(rule.range_max):
                    raise RulesError(f"fields.{rule.name}.range min is greater than max", code="RC003")
            except InvalidOperation:
                pass
    for rule in rules.record_rules:
        for name in rule.fields or (rule.left, rule.right):
            if name and name not in leaves:
                raise RulesError(f"record rule {rule.name} references unknown field {name}", code="RC004")
    if rules.record_type and rules.record_type.field not in leaves:
        raise RulesError(f"record_type.field references unknown field {rules.record_type.field}", code="RC004")
    for label, name in (("count_field", rules.trailer and rules.trailer.count_field),
                        ("amount_field", rules.trailer and rules.trailer.amount_field)):
        if name and name not in leaves:
            raise RulesError(f"trailer.{label} references unknown field {name}", code="RC004")

def load_rules(path: str | Path, layout: Layout | None = None) -> RuleSet:
    path = Path(path)
    try:
        data = parse_yaml(path.read_bytes(), str(path))
    except OSError as exc:
        raise RulesError(f"cannot read rules file {path}: {exc}", code="IO001") from exc
    version = data.get("version")
    if version != 1:
        raise RulesError("version must be 1")
    name = data.get("name", path.stem)
    if not isinstance(name, str) or not name:
        raise RulesError("name must be a non-empty string")
    fields_config = data.get("fields", {})
    if not isinstance(fields_config, dict):
        raise RulesError("fields must be a mapping")
    fields: dict[str, FieldRule] = {}
    blanks: dict[str, str] = {}
    for field_name, config in fields_config.items():
        if not isinstance(field_name, str):
            raise RulesError("field names must be strings")
        fields[field_name] = field_rule(field_name, config)
        if isinstance(config, dict) and config.get("blank"):
            blanks[field_name] = config["blank"]
    raw_record_rules = data.get("record_rules", [])
    if not isinstance(raw_record_rules, list):
        raise RulesError("record_rules must be a list")
    record_type = _record_type(data["record_type"]) if "record_type" in data else None
    trailer = _trailer(data["trailer"]) if "trailer" in data else None
    if trailer and (record_type is None or record_type.trailer is None):
        raise RulesError("trailer rules require record_type.trailer")
    minimum, maximum = _limits(data.get("limits", {}))
    rules = RuleSet(name, version, fields, tuple(_record_rule(i, item) for i, item in enumerate(raw_record_rules)),
                    record_type, trailer, minimum, maximum, blanks)
    if layout is not None:
        _validate_against_layout(rules, layout)
    return rules
