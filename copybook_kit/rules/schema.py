"""Rule-contract dataclasses, safe YAML loading and per-field schema checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from typing import Any

import yaml

from ..errors import RulesError

MAX_RULE_BYTES, MAX_PATTERN_LENGTH = 512 * 1024, 200
TOP_KEYS = {"version", "name", "record_type", "trailer", "limits", "fields", "record_rules"}
FIELD_KEYS = {"required", "enum", "range", "date_format", "pattern", "check_digit", "scale", "blank"}
RECORD_TYPES, CROSS_OPS = {"unique_key", "cross_field"}, {"<=", "<", "==", "!="}
CROSS_QUANTIFIERS = {"all_aligned", "any_aligned"}
CROSS_FIELD_KEYS = {"kind", "name", "left", "op", "right", "left_path", "right_path", "quantifier"}

@dataclass(frozen=True)
class CheckDigitRule:
    weights: tuple[int, ...]; modulus: int

@dataclass(frozen=True)
class FieldRule:
    name: str; required: bool = False
    enum: tuple[Any, ...] = (); range_min: str | None = None; range_max: str | None = None
    date_format: str | None = None; pattern: str | None = None
    check_digit: CheckDigitRule | None = None; scale: int | None = None

@dataclass(frozen=True)
class RecordRule:
    name: str; kind: str
    fields: tuple[str, ...] = (); left: str | None = None
    op: str | None = None; right: str | None = None
    # Explicit OCCURS path selectors (e.g. "PAIR[*].LOW") and the aligned
    # quantifier; None preserves the legacy single-value cross-field semantics.
    left_path: str | None = None; right_path: str | None = None
    quantifier: str | None = None

@dataclass(frozen=True)
class RecordTypeRule:
    field: str; data: str
    header: str | None = None; trailer: str | None = None

@dataclass(frozen=True)
class TrailerRule:
    count_field: str | None = None; amount_field: str | None = None

@dataclass
class RuleSet:
    name: str; version: int
    fields: dict[str, FieldRule] = dc_field(default_factory=dict)
    record_rules: tuple[RecordRule, ...] = ()
    record_type: RecordTypeRule | None = None; trailer: TrailerRule | None = None
    min_records: int | None = None; max_records: int | None = None
    blank_policies: dict[str, str] = dc_field(default_factory=dict)
    # rule name -> layout-bound alignment plan; populated by load_rules when a
    # compiled Layout is supplied and empty for layout-free legacy contracts.
    cross_bindings: dict[str, Any] = dc_field(default_factory=dict)

class _UniqueSafeLoader(yaml.SafeLoader):
    pass

def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise RulesError(f"duplicate YAML key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result

_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)

def parse_yaml(raw: bytes, source: str) -> dict[str, Any]:
    if len(raw) > MAX_RULE_BYTES:
        raise RulesError(f"rules file exceeds {MAX_RULE_BYTES} bytes")
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=_UniqueSafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RulesError(f"invalid YAML in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise RulesError("rules contract must be a YAML mapping")
    if unknown := set(data) - TOP_KEYS:
        raise RulesError(f"unknown top-level keys: {sorted(unknown)}")
    return data

def string_value(value: Any, label: str) -> str:
    if isinstance(value, float):
        raise RulesError(f"{label} must not be a float; use a quoted decimal string", code="RC003")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RulesError(f"{label} must be a string or integer", code="RC003")
    return str(value)

def safe_pattern(pattern: str) -> re.Pattern[str]:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise RulesError(f"pattern exceeds {MAX_PATTERN_LENGTH} characters", code="RC002")
    if re.search(r"\\[1-9]|\(\?=|\(\?!|\(\?<=|\(\?<!|\(\?P", pattern):
        raise RulesError("pattern uses a backreference or lookaround", code="RC002")
    if re.search(r"\([^)]*[+*{][^)]*\)[+*{]", pattern):
        raise RulesError("pattern contains a nested quantifier", code="RC002")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RulesError(f"pattern does not compile: {exc}", code="RC002") from exc

def check_digit_config(value: Any, name: str) -> CheckDigitRule:
    if not isinstance(value, dict) or set(value) != {"weights", "modulus"}:
        raise RulesError(f"fields.{name}.check_digit needs weights and modulus", code="RC003")
    weights, modulus = value["weights"], value["modulus"]
    if not isinstance(weights, list) or not weights or any(
        isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 9 for item in weights
    ):
        raise RulesError(f"fields.{name}.check_digit.weights must be integers 1-9", code="RC003")
    if isinstance(modulus, bool) or not isinstance(modulus, int) or not 2 <= modulus <= 97:
        raise RulesError(f"fields.{name}.check_digit.modulus must be 2-97", code="RC003")
    return CheckDigitRule(tuple(weights), modulus)

def _range(config: Any, name: str) -> tuple[str | None, str | None]:
    if not isinstance(config, dict) or not config or set(config) - {"min", "max"}:
        raise RulesError(f"fields.{name}.range must contain min and/or max")
    return (string_value(config["min"], f"fields.{name}.range.min") if "min" in config else None,
            string_value(config["max"], f"fields.{name}.range.max") if "max" in config else None)

def _date_format(value: Any, name: str, minimum: str | None, maximum: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise RulesError(f"fields.{name}.date_format must be a non-empty string", code="RC003")
    try:
        datetime.strptime(datetime(2026, 1, 2, 4, 5, 6).strftime(value), value)
    except ValueError as exc:
        raise RulesError(f"fields.{name}.date_format is not usable: {exc}", code="RC003") from exc
    for bound in (minimum, maximum):
        if bound is None:
            continue
        try:
            datetime.strptime(bound, value)
        except ValueError:
            try:
                date.fromisoformat(bound)
            except ValueError as exc:
                raise RulesError(f"fields.{name} range bound {bound!r} is not a date", code="RC003") from exc
    return value

def field_rule(name: str, config: Any) -> FieldRule:
    if not isinstance(config, dict) or not config:
        raise RulesError(f"fields.{name} must be a non-empty mapping")
    if unknown := set(config) - FIELD_KEYS:
        raise RulesError(f"fields.{name} has unknown keys: {sorted(unknown)}")
    if "required" in config and not isinstance(config["required"], bool):
        raise RulesError(f"fields.{name}.required must be a boolean", code="RC003")
    blank = config.get("blank")
    if blank is not None and blank not in {"null", "zero", "error"}:
        raise RulesError(f"fields.{name}.blank must be null, zero or error", code="RC003")
    enum = config.get("enum", [])
    if enum and (not isinstance(enum, list) or any(isinstance(item, (dict, list)) for item in enum)):
        raise RulesError(f"fields.{name}.enum must be a list of scalar values", code="RC003")
    minimum, maximum = _range(config["range"], name) if "range" in config else (None, None)
    date_format = _date_format(config["date_format"], name, minimum, maximum) if "date_format" in config else None
    pattern = config.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise RulesError(f"fields.{name}.pattern must be a string", code="RC002")
        safe_pattern(pattern)
    scale = config.get("scale")
    if scale is not None and (isinstance(scale, bool) or not isinstance(scale, int) or scale < 0):
        raise RulesError(f"fields.{name}.scale must be non-negative", code="RC003")
    return FieldRule(name, config.get("required", False), tuple(enum), minimum, maximum, date_format,
                     pattern, check_digit_config(config["check_digit"], name) if "check_digit" in config else None,
                     scale)
