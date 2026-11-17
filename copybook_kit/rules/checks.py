"""Field checks, cross-field comparison and unique-key state."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..model import Record, RuleHit
from ..util import decimal_text
from .binding import CrossBinding, SideBinding
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


@dataclass(frozen=True)
class Comparison:
    """One executed aligned comparison with its full pairing coordinates."""

    coord: tuple[int, ...]
    left_path: str
    right_path: str
    left: Any
    right: Any
    held: bool
    left_byte_offset: int
    right_byte_offset: int
    left_evidence: str | None = None
    right_evidence: str | None = None

    def as_detail(self) -> dict[str, Any]:
        return {
            "coord": list(self.coord),
            "left_path": self.left_path, "right_path": self.right_path,
            "left": scalar_text(self.left), "right": scalar_text(self.right),
            "left_byte_offset": self.left_byte_offset,
            "right_byte_offset": self.right_byte_offset,
            "left_evidence": self.left_evidence, "right_evidence": self.right_evidence,
            "held": self.held,
        }


@dataclass(frozen=True)
class MissingPair:
    """An aligned coordinate where at least one side has no defined value."""

    coord: tuple[int, ...]
    left_path: str | None
    right_path: str | None
    missing_side: str  # "left" | "right" | "both"
    left_byte_offset: int | None
    right_byte_offset: int | None
    decode_error: str | None
    left_evidence: str | None = None
    right_evidence: str | None = None

    def as_detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "coord": list(self.coord), "missing_side": self.missing_side,
        }
        if self.left_path is not None:
            payload["left_path"] = self.left_path
            payload["left_byte_offset"] = self.left_byte_offset
            if self.left_evidence is not None:
                payload["left_evidence"] = self.left_evidence
        if self.right_path is not None:
            payload["right_path"] = self.right_path
            payload["right_byte_offset"] = self.right_byte_offset
            if self.right_evidence is not None:
                payload["right_evidence"] = self.right_evidence
        if self.decode_error is not None:
            payload["decode_error"] = self.decode_error
        return payload


@dataclass(frozen=True)
class CrossResult:
    """Full, verifiable outcome of a cross-field rule on one record."""

    rule: RecordRule
    quantifier: str | None
    comparisons: tuple[Comparison, ...]
    missing: tuple[MissingPair, ...]
    accepted: bool
    witness: Comparison | MissingPair | None


def _decode_error_for(record: Record, path: str) -> str | None:
    for diagnostic in record.errors:
        if diagnostic.field == path:
            return diagnostic.code
    return None


def _side_value(record: Record, side: SideBinding, paths: list[str],
                coord: tuple[int, ...]) -> tuple[Any, str | None, int | None, str | None]:
    """Resolve one side at an aligned coordinate.

    ``paths`` is already filtered to the leaves at this coordinate by
    :meth:`CrossBinding.paths_at`; a scalar broadcast side has one physical
    path and receives the same value for every installment.
    """
    if not paths:
        return None, None, None, None
    path = paths[0]
    offset = side.offsets.get(path, (0, 0))[0]
    return record.flat.get(path), path, offset, _decode_error_for(record, path)


def evaluate_cross(record: Record, rule: RecordRule, binding: CrossBinding | None) -> CrossResult:
    """Evaluate one cross-field rule along its compiled alignment coordinates.

    Missing or failed-to-decode values never count as a successful comparison;
    they are reported separately so neither quantifier can be decided by the
    empty-list defaults of Python's ``all``/``any``.
    """
    if binding is None or binding.is_scalar_pair:
        return _evaluate_legacy(record, rule)
    comparisons: list[Comparison] = []
    missing: list[MissingPair] = []
    for coord in binding.coordinates():
        left_paths = binding.paths_at(binding.left, coord)
        right_paths = binding.paths_at(binding.right, coord)
        left_value, left_path, left_offset, left_error = _side_value(
            record, binding.left, left_paths, coord)
        right_value, right_path, right_offset, right_error = _side_value(
            record, binding.right, right_paths, coord)
        left_evidence = record.evidence.get(left_path) if left_path is not None else None
        right_evidence = record.evidence.get(right_path) if right_path is not None else None
        left_missing = left_value is None
        right_missing = right_value is None
        if left_missing or right_missing:
            missing_side = "both" if left_missing and right_missing else (
                "left" if left_missing else "right")
            missing.append(MissingPair(
                coord, left_path, right_path, missing_side, left_offset, right_offset,
                left_error or right_error, left_evidence, right_evidence))
            continue
        held = _compare(left_value, binding.op, right_value)
        comparisons.append(Comparison(
            coord, left_path, right_path, left_value, right_value, held,
            left_offset if left_offset is not None else 0,
            right_offset if right_offset is not None else 0,
            left_evidence, right_evidence))
    accepted, witness = _quantify(binding.quantifier, comparisons, missing)
    return CrossResult(rule, binding.quantifier, tuple(comparisons), tuple(missing), accepted, witness)


def _quantify(quantifier: str | None, comparisons: list[Comparison],
              missing: list[MissingPair]) -> tuple[bool, Comparison | MissingPair | None]:
    if not comparisons:
        # No defined pair exists: an empty all()/any() would silently accept;
        # the business policy instead rejects and names the missing coordinates.
        return False, missing[0] if missing else None
    if quantifier == "any_aligned":
        for item in comparisons:
            if item.held:
                return True, item
        return False, next((item for item in comparisons if not item.held), None)
    # all_aligned: every *defined* pair must hold; missing pairs are reported
    # alongside but cannot turn a failed defined comparison into an acceptance.
    failures = [item for item in comparisons if not item.held]
    if failures:
        return False, failures[0]
    if missing:
        return False, missing[0]
    return True, comparisons[0]


def _evaluate_legacy(record: Record, rule: RecordRule) -> CrossResult:
    """Pre-binding semantics: a single scalar pair, missing values pass."""
    left, right = first_value(record, rule.left or ""), first_value(record, rule.right or "")
    if left is None or right is None:
        return CrossResult(rule, None, (), (), True, None)
    held = _compare(left, rule.op or "==", right)
    left_found = values_for(record, rule.left or "")
    right_found = values_for(record, rule.right or "")
    comparison = Comparison(
        (), left_found[0][0], right_found[0][0], left, right, held, 0, 0)
    if held:
        return CrossResult(rule, None, (comparison,), (), True, comparison)
    return CrossResult(rule, None, (comparison,), (), False, comparison)


def _absolutize(payload: dict[str, Any], base: int) -> dict[str, Any]:
    """Prefix record-relative byte offsets with the record's file offset."""
    for key in ("left_byte_offset", "right_byte_offset"):
        if key in payload and payload[key] is not None:
            payload[key] = base + payload[key]
    return payload


def _cross_hit_detail(result: CrossResult, record: Record, binding: CrossBinding | None) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "rule": result.rule.name, "op": result.rule.op,
        "quantifier": result.quantifier,
        "left": result.rule.left, "right": result.rule.right,
        "coordinates": [list(coord) for coord in
                        (binding.coordinates() if binding is not None else [()])],
        "comparisons": [_absolutize(item.as_detail(), record.byte_offset)
                        for item in result.comparisons],
        "missing": [_absolutize(item.as_detail(), record.byte_offset)
                    for item in result.missing],
    }
    if isinstance(result.witness, Comparison):
        witness = result.witness
        detail["witness"] = {
            "kind": "comparison", "coord": list(witness.coord),
            "left_path": witness.left_path, "right_path": witness.right_path,
            "left": scalar_text(witness.left), "right": scalar_text(witness.right),
            "left_byte_offset": record.byte_offset + witness.left_byte_offset,
            "right_byte_offset": record.byte_offset + witness.right_byte_offset,
            "left_evidence": witness.left_evidence, "right_evidence": witness.right_evidence,
            "held": witness.held,
        }
    elif isinstance(result.witness, MissingPair):
        witness = result.witness
        payload: dict[str, Any] = {"kind": "missing", "coord": list(witness.coord),
                                   "missing_side": witness.missing_side}
        if witness.left_path is not None:
            payload["left_path"] = witness.left_path
            payload["left_byte_offset"] = record.byte_offset + (witness.left_byte_offset or 0)
            if witness.left_evidence is not None:
                payload["left_evidence"] = witness.left_evidence
        if witness.right_path is not None:
            payload["right_path"] = witness.right_path
            payload["right_byte_offset"] = record.byte_offset + (witness.right_byte_offset or 0)
            if witness.right_evidence is not None:
                payload["right_evidence"] = witness.right_evidence
        if witness.decode_error is not None:
            payload["decode_error"] = witness.decode_error
        detail["witness"] = payload
    return detail


def cross_hit(record: Record, rule: RecordRule, binding: CrossBinding | None = None) -> RuleHit | None:
    if binding is None and rule.quantifier is not None:
        # Quantified rules require the layout binding compiled at load time.
        # Falling back to first-value would reintroduce the OCCURS ambiguity
        # this contract exists to prevent.
        raise RuntimeError(
            f"cross-field rule {rule.name!r} declares quantifier {rule.quantifier!r} but no layout "
            "binding was compiled; load the rules contract with a Layout")
    result = evaluate_cross(record, rule, binding)
    if result.accepted:
        return None
    witness = result.witness
    if isinstance(witness, Comparison):
        location = f"{witness.left_path} vs {witness.right_path}"
        message = (f"{rule.name} {rule.quantifier or 'cross_field'} failed at {location}: "
                   f"{scalar_text(witness.left)} {rule.op} {scalar_text(witness.right)} is false")
    else:
        coord_text = "[" + "][".join(str(index) for index in (witness.coord if witness else ())) + "]"
        message = (f"{rule.name} {rule.quantifier or 'cross_field'} has no defined comparison "
                   f"at {coord_text}; missing {witness.missing_side if witness else 'values'}")
    field_path = witness.left_path if isinstance(witness, (Comparison, MissingPair)) else None
    return _hit(record, "R009_CROSS_FIELD", message, "record", field_path,
                **_cross_hit_detail(result, record, binding))

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
