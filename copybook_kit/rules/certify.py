"""Offline satisfiability certification of a rules contract against one layout.

A YAML contract can pass every schema check while no fixed-length raw record can
simultaneously satisfy it.  This module never runs a pipeline and never touches
the ledger; it reasons about the byte domains a copybook can actually encode and
the clauses the rules engine evaluates, then returns one of three verdicts:

* ``SAT``     - a concrete record-length byte witness was encoded, decoded with
                :func:`decode_record` and accepted by :class:`RuleEngine`.
* ``UNSAT``   - an auditable contradiction chain built only from supported
                clauses (encodable field domains, enum/range/date/required/
                check_digit and ``<=``/``<`` cross-field relations).
* ``UNKNOWN`` - a clause outside the supported reasoning fragment (arbitrary
                pattern over an open domain, REDEFINES overlap, OCCURS multi
                values, ``==``/``!=`` relations) blocks a complete proof, or a
                bounded witness search spent its budget.

Two honest modes are provided:

* ``witness`` (test-data delivery): search within a fixed random budget.  A
  found witness is fully verified; an exhausted budget is ``UNKNOWN``, never
  ``UNSAT`` - absence of a sample is not a proof.
* ``proof`` (contract review): domain intersection and cross-field propagation.
  Supported fragments yield ``SAT``/``UNSAT`` certificates; everything else is
  reported as ``UNKNOWN`` with the abandoned clause, unless an independent
  contradiction already proves ``UNSAT``.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..decode.record import decode_record
from ..model import Field, Layout, LeafOccurrence
from ..rules.checks import ValidationState, scalar_text
from ..rules.engine import RuleEngine, assign_record_type
from ..rules.schema import FieldRule, RecordRule, RuleSet
from ..util import sha8

DEFAULT_SEED = 20261117
DEFAULT_BUDGET = 200
# Prefix space that is fully enumerated for check-digit reasoning (10**6
# covers every PIC 9(6) field such as CONTROL-NO).
CHECK_DIGIT_ENUM_CAP = 1_000_000
MAX_FIXPOINT_ITERATIONS = 64
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "

# ---------------------------------------------------------------------------
# Result reasons (human-readable, stable identifiers for the report)
# ---------------------------------------------------------------------------
REASON_OCCURS = "occurs_multi_value_not_reasoned"
REASON_REDEFINES = "redefines_overlap_not_reasoned"
REASON_PATTERN_OPEN = "pattern_over_open_domain_not_reasoned"
REASON_PATTERN_NUMERIC = "pattern_on_numeric_domain_not_reasoned"
REASON_RANGE_CHAR = "numeric_range_on_character_field_not_reasoned"
REASON_DATE_CHAR = "date_format_on_character_field_not_reasoned"
REASON_CHECK_DIGIT_OPEN = "check_digit_over_unenumerated_domain"
REASON_CROSS_OP = "cross_field_operator_not_reasoned"
REASON_CROSS_KIND = "cross_field_operands_have_incomparable_domains"
REASON_OPEN_REQUIRED = "required_character_field_has_an_open_domain"
REASON_BUDGET = "search_budget_exhausted"
REASON_WITNESS_UNSAT = "proof_level_contradiction_not_claimed_by_witness_mode"
REASON_VERIFY = "witness_verification_failed"
REASON_CONSTRUCT = "witness_construction_failed"
REASON_DATE_WIDTH = "date_format_width_does_not_match_field"
REASON_CROSS_SIDE = "cross_field_references_an_abandoned_field"


@dataclass
class Domain:
    """Reasoned value domain for a single-occurrence elementary field."""

    name: str
    occurrence: LeafOccurrence
    rule: FieldRule | None
    kind: str  # "char" | "integer" | "date" | "blank-only"
    lo: int | None = None
    hi: int | None = None
    atoms: set[Any] = field(default_factory=set)
    explicit: bool = False
    date_format: str | None = None
    sources: list[str] = field(default_factory=list)

    @property
    def fld(self) -> Field:
        return self.occurrence.field


@dataclass
class Contradiction(Exception):
    """Raised internally when supported clauses prove the empty domain."""

    steps: list[dict[str, str]]


@dataclass
class Abandoned(Exception):
    """Raised internally when a field must leave the reasoned fragment."""

    source: str
    reason: str


# ---------------------------------------------------------------------------
# Encoding: every witness byte must come through the same layout the decoder
# understands, so an unencodable value can never masquerade as SAT.
# ---------------------------------------------------------------------------
def _encode_char(fld: Field, text: str, charset: str) -> bytes | None:
    """Encode a CHAR value left-justified with trailing spaces, like the writer.

    The decoder strips trailing spaces, so a one-character enum on X(2) is
    genuinely encodable as ``b"A "``; a value whose encoded width exceeds the
    field or whose bytes leave no legal padding is rejected.
    """
    try:
        raw = text.encode(charset)
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if len(raw) > fld.length:
        return None
    return raw.ljust(fld.length, b" ")


def _encode_integer(fld: Field, value: int) -> bytes | None:
    if fld.usage == "COMP-3":
        if value < 0 and not fld.signed:
            return None
        magnitude = abs(value)
        if magnitude > 10 ** fld.digits - 1:
            return None
        nibbles = [int(ch) for ch in str(magnitude).zfill(fld.digits)]
        nibbles.append(0xD if value < 0 else 0xC)
        if len(nibbles) % 2:
            nibbles.insert(0, 0)
        raw = bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles), 2))
        return raw if len(raw) == (fld.digits + 2) // 2 else None
    # DISPLAY numeric
    if fld.signed and not fld.separate:
        return None  # decoder rejects every nonblank overpunch (DE001)
    if value < 0 and not fld.signed:
        return None
    digits = str(abs(value)).zfill(fld.digits)
    if len(digits) != fld.digits or not digits.isascii() or not digits.isdigit():
        return None
    if fld.signed:
        sign = "-" if value < 0 else "+"
        text = sign + digits if fld.sign == "LEADING" else digits + sign
    else:
        text = digits
    raw = text.encode("ascii")
    return raw if len(raw) == fld.length else None


def _encode_date(fld: Field, ordinal: int, fmt: str) -> bytes | None:
    text = date.fromordinal(ordinal).strftime(fmt)
    if len(text) != fld.digits or not text.isascii() or not text.isdigit():
        return None
    return text.encode("ascii")


def _blank_filler(occ: LeafOccurrence, charset: str, blank_error: bool) -> bytes:
    fld = occ.field
    if fld.category == "CHAR":
        if blank_error:
            text = "0".ljust(fld.length)
            raw = _encode_char(fld, text, charset)
            if raw is not None:
                return raw
        return b" " * fld.length
    raw = _encode_integer(fld, 0)
    if raw is None:  # signed DISPLAY without SEPARATE: only blanks decode
        return b" " * fld.length
    return raw


# ---------------------------------------------------------------------------
# Domain construction
# ---------------------------------------------------------------------------
def _integer_base_interval(fld: Field) -> tuple[int, int] | None:
    """Integers whose fixed-point encoding this field can decode nonblank."""
    if fld.usage == "DISPLAY" and fld.signed and not fld.separate:
        return None  # overpunch unsupported -> no nonblank value decodes
    top = 10 ** fld.digits - 1
    if fld.signed:
        return -top, top
    return 0, top


def _scaled_bound(text: str, scale: int, *, is_min: bool) -> int | None:
    try:
        scaled = Decimal(text).scaleb(scale)
    except Exception:  # pragma: no cover - loader validates numeric bounds
        return None
    if scaled == scaled.to_integral_value():
        return int(scaled)
    # Bound falls between representable grid points.
    return int(scaled.to_integral_value(rounding="ROUND_CEILING" if is_min else "ROUND_FLOOR"))


def _parse_date_bound(text: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(text, fmt).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None


def _check_digit_of(prefix_digits: str, weights: tuple[int, ...], modulus: int) -> int | None:
    total = sum(int(digit) * weight for digit, weight in zip(prefix_digits, weights))
    expected = (modulus - total % modulus) % modulus
    return expected if expected < 10 else None


def _filter_check_digit(candidates: set[int], rule: FieldRule, fld: Field,
                        source: str) -> set[int]:
    cd = rule.check_digit
    assert cd is not None
    kept: set[int] = set()
    rejected_example = None
    for value in sorted(candidates):
        text = str(value).zfill(fld.digits)
        expected = _check_digit_of(text[:-1], cd.weights, cd.modulus)
        if expected is not None and int(text[-1]) == expected:
            kept.add(value)
        elif rejected_example is None:
            rejected_example = (text, expected)
    if not kept:
        text, expected = rejected_example or ("", None)
        raise Contradiction([
            {"source": f"layout.{fld.name}", "detail": f"PIC {fld.pic} encodes digit strings of length {fld.digits}"},
            {"source": source, "detail": f"enum/range leaves only {sorted(candidates)!r}"},
            {"source": f"fields.{fld.name}.check_digit",
             "detail": (f"weights={list(cd.weights)} modulus={cd.modulus}: prefix {text[:-1]!r} "
                        f"requires check digit {expected}, but the candidate ends in {text[-1:]!r}")},
        ])
    return kept


def _check_digit_interval(fld: Field, rule: FieldRule, lo: int, hi: int) -> tuple[set[int], bool]:
    """Enumerate check-valid codes when the prefix space is small enough."""
    cd = rule.check_digit
    assert cd is not None
    prefix_count = 10 ** (fld.digits - 1)
    if prefix_count > CHECK_DIGIT_ENUM_CAP:
        return set(), False
    valid: set[int] = set()
    for prefix in range(prefix_count):
        check = _check_digit_of(str(prefix).zfill(fld.digits - 1), cd.weights, cd.modulus)
        if check is None:
            continue
        value = prefix * 10 + check
        # Encoding zero-fills to the full PIC width; a shorter code would gain
        # leading zeroes and decode to a different check digit, so only codes
        # that occupy every digit position are valid witnesses.
        if value >= 10 ** (fld.digits - 1) and lo <= value <= hi:
            valid.add(value)
    return valid, True


def _build_numeric_domain(name: str, occ: LeafOccurrence, rule: FieldRule | None,
                          marker_values: set[str], blank_policy: str,
                          abandoned: list[dict[str, str]]) -> Domain:
    fld = occ.field
    source = f"fields.{name}"
    base = _integer_base_interval(fld)
    blank_error = blank_policy == "error"
    if base is None:
        # Signed DISPLAY without SEPARATE: the decoder rejects nonblank bytes
        # (DE001), and blank bytes are only legal when the blank policy turns
        # them into null/zero; blank=error raises DE003 instead.
        blank_decodable = blank_policy in ("null", "zero")
        if (rule is not None and (rule.required or blank_error or rule.enum or rule.range_min is not None
                                  or rule.range_max is not None or rule.date_format or rule.check_digit)) \
                or not blank_decodable:
            raise Contradiction([
                {"source": f"layout.{name}",
                 "detail": f"PIC {fld.pic} DISPLAY without SIGN SEPARATE rejects nonblank data with DE001"},
                {"source": f"fields.{name}.blank",
                 "detail": f"blank policy {blank_policy!r} leaves no decodable byte sequence"},
            ])
        return Domain(name, occ, rule, "blank-only", sources=[f"layout.{name}"])

    lo, hi = base
    dom = Domain(name, occ, rule, "integer", lo=lo, hi=hi, sources=[f"layout.{name}"])
    if rule is None:
        return dom

    if rule.pattern is not None:
        abandoned.append({"source": f"{source}.pattern", "reason": REASON_PATTERN_NUMERIC})
    if rule.date_format is not None:
        return _build_date_domain(dom, rule, abandoned)
    if rule.range_min is not None:
        bound = _scaled_bound(rule.range_min, fld.scale, is_min=True)
        if bound is not None:
            dom.lo = max(dom.lo, bound)
            dom.sources.append(f"{source}.range.min")
    if rule.range_max is not None:
        bound = _scaled_bound(rule.range_max, fld.scale, is_min=False)
        if bound is not None:
            dom.hi = min(dom.hi, bound)
            dom.sources.append(f"{source}.range.max")
    if dom.lo > dom.hi:
        raise Contradiction([
            {"source": f"layout.{name}", "detail": f"PIC {fld.pic} encodable integer domain is [{lo}, {hi}]"},
            {"source": f"{source}.range",
             "detail": f"range {rule.range_min!r}..{rule.range_max!r} intersects it at [{dom.lo}, {dom.hi}]"},
        ])
    if rule.enum:
        candidates: set[int] = set()
        rejected: list[str] = []
        for item in rule.enum:
            try:
                value = int(Decimal(scalar_text(item)).scaleb(fld.scale))
            except Exception:
                rejected.append(scalar_text(item))
                continue
            if dom.lo <= value <= dom.hi:
                candidates.add(value)
            else:
                rejected.append(scalar_text(item))
        dom.sources.append(f"{source}.enum")
        if not candidates:
            raise Contradiction([
                {"source": f"layout.{name}", "detail": f"PIC {fld.pic} encodable integer domain is [{lo}, {hi}]"},
                {"source": f"{source}.range", "detail": f"after range: [{dom.lo}, {dom.hi}]"},
                {"source": f"{source}.enum", "detail": f"enum {list(rule.enum)!r} has no member inside the domain "
                                                       f"(rejected {rejected!r})"},
            ])
        dom.atoms, dom.explicit = candidates, True
    if rule.scale is not None and rule.scale < fld.scale:
        raise Contradiction([
            {"source": f"layout.{name}", "detail": f"every decoded value of PIC {fld.pic} carries {fld.scale} "
                                                   "decimal places"},
            {"source": f"{source}.scale", "detail": f"declared maximum scale {rule.scale} is always exceeded"},
        ])
    if rule.check_digit is not None:
        cd_source = f"{source}.check_digit"
        if dom.explicit:
            dom.atoms = _filter_check_digit(dom.atoms, rule, fld, cd_source)
            dom.sources.append(cd_source)
        else:
            valid, complete = _check_digit_interval(fld, rule, dom.lo, dom.hi)
            if not complete:
                abandoned.append({"source": cd_source, "reason": REASON_CHECK_DIGIT_OPEN})
            else:
                if not valid:
                    raise Contradiction([
                        {"source": f"layout.{name}", "detail": f"PIC {fld.pic} domain [{dom.lo}, {dom.hi}]"},
                        {"source": cd_source,
                         "detail": f"weights={list(rule.check_digit.weights)} modulus={rule.check_digit.modulus} "
                                   "produces no code inside the domain"},
                    ])
                dom.atoms, dom.explicit = valid, True
                dom.sources.append(cd_source)
    _apply_marker(dom, marker_values, numeric=True)
    return dom


def _build_date_domain(dom: Domain, rule: FieldRule,
                       abandoned: list[dict[str, str]]) -> Domain:
    fld = dom.fld
    fmt = rule.date_format
    assert fmt is not None
    sample = date(2026, 1, 2).strftime(fmt)
    if len(sample) != fld.digits or not sample.isdigit():
        raise Contradiction([
            {"source": f"layout.{dom.name}", "detail": f"PIC {fld.pic} holds {fld.digits} digit characters"},
            {"source": f"fields.{dom.name}.date_format",
             "detail": f"format {fmt!r} encodes dates as {sample!r} of width {len(sample)} "
                       f"(reason {REASON_DATE_WIDTH})"},
        ])
    dom.kind, dom.date_format = "date", fmt
    dom.lo, dom.hi = date(1, 1, 1).toordinal(), date(9999, 12, 31).toordinal()
    dom.sources.append(f"fields.{dom.name}.date_format")
    if rule.range_min is not None:
        bound = _parse_date_bound(rule.range_min, fmt)
        if bound is not None:
            dom.lo = max(dom.lo, bound.toordinal())
            dom.sources.append(f"fields.{dom.name}.range.min")
    if rule.range_max is not None:
        bound = _parse_date_bound(rule.range_max, fmt)
        if bound is not None:
            dom.hi = min(dom.hi, bound.toordinal())
            dom.sources.append(f"fields.{dom.name}.range.max")
    if dom.lo > dom.hi:
        raise Contradiction([
            {"source": f"layout.{dom.name}", "detail": f"PIC {fld.pic} date domain {fmt!r}"},
            {"source": f"fields.{dom.name}.range",
             "detail": f"range {rule.range_min!r}..{rule.range_max!r} contains no encodable date"},
        ])
    if rule.enum:
        ordinals: set[int] = set()
        for item in rule.enum:
            parsed = _parse_date_bound(scalar_text(item), fmt)
            if parsed is not None and dom.lo <= parsed.toordinal() <= dom.hi:
                ordinals.add(parsed.toordinal())
        if not ordinals:
            raise Contradiction([
                {"source": f"fields.{dom.name}.range", "detail": f"date window [{dom.lo}, {dom.hi}]"},
                {"source": f"fields.{dom.name}.enum", "detail": f"enum {list(rule.enum)!r} names no date in the window"},
            ])
        dom.atoms, dom.explicit = ordinals, True
        dom.sources.append(f"fields.{dom.name}.enum")
    if rule.check_digit is not None:
        abandoned.append({"source": f"fields.{dom.name}.check_digit", "reason": REASON_CHECK_DIGIT_OPEN})
    if rule.pattern is not None:
        abandoned.append({"source": f"fields.{dom.name}.pattern", "reason": REASON_PATTERN_NUMERIC})
    return dom


def _apply_marker(dom: Domain, marker_values: set[str], *, numeric: bool) -> None:
    if not marker_values:
        return
    if dom.kind == "char":
        dom.atoms = {value for value in dom.atoms if value in marker_values}
        if not dom.atoms:
            raise Contradiction([
                {"source": "record_type", "detail": f"data marker must be one of {sorted(marker_values)!r}"},
                {"source": f"fields.{dom.name}.enum", "detail": "no field candidate matches the data marker"},
            ])
    elif numeric:
        kept = set()
        for value in marker_values:
            try:
                kept.add(int(Decimal(value)))
            except ValueError:
                continue
        if dom.explicit:
            dom.atoms &= kept
            if not dom.atoms:
                raise Contradiction([
                    {"source": "record_type", "detail": f"data marker {sorted(marker_values)!r}"},
                    {"source": f"fields.{dom.name}.enum", "detail": "no numeric candidate matches the marker"},
                ])
        elif kept:
            marker = next(iter(kept))
            if not dom.lo <= marker <= dom.hi:
                raise Contradiction([
                    {"source": f"fields.{dom.name}.range", "detail": f"encodable domain is {_domain_window(dom)}"},
                    {"source": "record_type", "detail": f"data marker requires the value {marker}"},
                ])
            dom.atoms, dom.explicit = {marker}, True
            dom.sources.append("record_type.data")


def _build_char_domain(name: str, occ: LeafOccurrence, rule: FieldRule | None,
                       marker_values: set[str], charset: str,
                       abandoned: list[dict[str, str]]) -> Domain:
    fld = occ.field
    source = f"fields.{name}"
    dom = Domain(name, occ, rule, "char", sources=[f"layout.{name}"])
    candidates: set[str] | None = None
    if rule is not None:
        if rule.range_min is not None or rule.range_max is not None:
            abandoned.append({"source": f"{source}.range", "reason": REASON_RANGE_CHAR})
        if rule.date_format is not None:
            abandoned.append({"source": f"{source}.date_format", "reason": REASON_DATE_CHAR})
        if rule.enum:
            candidates = {scalar_text(item) for item in rule.enum}
            dom.sources.append(f"{source}.enum")
    if candidates is not None:
        encodable = set()
        for text in candidates:
            if _encode_char(fld, text, charset) is not None:
                encodable.add(text)
        if not encodable:
            raise Contradiction([
                {"source": f"layout.{name}",
                 "detail": f"PIC {fld.pic} field width is {fld.length} {charset} bytes"},
                {"source": f"{source}.enum",
                 "detail": f"enum {sorted(candidates)!r} has no member that encodes into the field width"},
            ])
        candidates = encodable
        if rule is not None and rule.pattern is not None:
            matched = {text for text in candidates if re.fullmatch(rule.pattern, text)}
            if not matched:
                raise Contradiction([
                    {"source": f"layout.{name}", "detail": f"PIC {fld.pic} byte-width domain after enum: {sorted(candidates)!r}"},
                    {"source": f"{source}.enum", "detail": f"enum {sorted(candidates)!r}"},
                    {"source": f"{source}.pattern",
                     "detail": f"pattern {rule.pattern!r} full-matches none of the enum candidates"},
                ])
            candidates = matched
            dom.sources.append(f"{source}.pattern")
        if rule is not None and rule.check_digit is not None:
            numeric_candidates = {int(text) for text in candidates if text.isdigit()}
            kept = _filter_check_digit(numeric_candidates, rule, fld, f"{source}.check_digit")
            candidates = {str(value).zfill(fld.digits) for value in kept}
            dom.sources.append(f"{source}.check_digit")
        dom.atoms, dom.explicit = candidates, True
        _apply_marker(dom, marker_values, numeric=False)
        return dom
    # Open character domain.
    if rule is not None and rule.pattern is not None:
        raise Abandoned(f"{source}.pattern", REASON_PATTERN_OPEN)
    if rule is not None and rule.check_digit is not None:
        raise Abandoned(f"{source}.check_digit", REASON_CHECK_DIGIT_OPEN)
    if rule is not None and rule.required:
        raise Abandoned(source, REASON_OPEN_REQUIRED)
    if marker_values:
        # Marker without an enum clause: the single marker text is an explicit
        # encodable requirement for data records.
        encodable = {text for text in marker_values if _encode_char(fld, text, charset) is not None}
        if not encodable:
            raise Contradiction([
                {"source": "record_type", "detail": f"data marker {sorted(marker_values)!r}"},
                {"source": f"layout.{name}", "detail": "marker cannot be encoded into the field width"},
            ])
        dom.atoms, dom.explicit = encodable, True
        dom.sources.append("record_type.data")
    return dom


# ---------------------------------------------------------------------------
# Certification orchestration
# ---------------------------------------------------------------------------
def _occurrence_map(layout: Layout) -> dict[str, list[LeafOccurrence]]:
    mapping: dict[str, list[LeafOccurrence]] = {}
    for occ in layout.leaves():
        mapping.setdefault(occ.field.name, []).append(occ)
    return mapping


def _overlaps(occ: LeafOccurrence, all_leaves: list[LeafOccurrence]) -> bool:
    start, end = occ.offset, occ.offset + occ.length
    for other in all_leaves:
        if other is occ or other.field.name == occ.field.name:
            continue
        if other.offset < end and start < other.offset + other.length:
            return True
    return False


def _cross_domains(rules: RuleSet, domains: dict[str, Domain],
                   abandoned: list[dict[str, str]]) -> list[tuple[RecordRule, Domain, Domain]]:
    reasoned: list[tuple[RecordRule, Domain, Domain]] = []
    for index, rule in enumerate(rules.record_rules):
        source = f"record_rules[{index}]({rule.name})"
        if rule.kind != "cross_field":
            continue
        if rule.op not in ("<=", "<"):
            abandoned.append({"source": source, "reason": REASON_CROSS_OP})
            continue
        left = domains.get(rule.left or "")
        right = domains.get(rule.right or "")
        if left is None or right is None:
            abandoned.append({"source": source, "reason": REASON_CROSS_SIDE})
            continue
        if left.kind != right.kind or left.kind not in ("integer", "date"):
            abandoned.append({"source": source, "reason": REASON_CROSS_KIND})
            continue
        reasoned.append((rule, left, right))
    return reasoned


def _value_label(dom: Domain, ordinal: int) -> str:
    if dom.kind == "date":
        return f"{date.fromordinal(ordinal).strftime(dom.date_format or '%Y%m%d')} (ordinal {ordinal})"
    return str(ordinal)


def _domain_window(dom: Domain) -> str:
    if dom.kind == "date":
        return f"[{_value_label(dom, dom.lo)}, {_value_label(dom, dom.hi)}]"
    return f"[{dom.lo}, {dom.hi}]"


def _propagate_cross(relations: list[tuple[RecordRule, Domain, Domain]]) -> list[dict[str, str]] | None:
    """Tighten domain bounds from <= / < relations; return a contradiction chain."""
    original = {id(dom): (dom.lo, dom.hi) for _, left, right in relations for dom in (left, right)}
    for _ in range(MAX_FIXPOINT_ITERATIONS):
        changed = False
        for rule, left, right in relations:
            slack = 1 if rule.op == "<" else 0
            new_left_hi = min(left.hi, right.hi - slack)
            new_right_lo = max(right.lo, left.lo + slack)
            if new_left_hi != left.hi:
                left.hi, changed = new_left_hi, True
            if new_right_lo != right.lo:
                right.lo, changed = new_right_lo, True
        if not changed:
            break
    for rule, left, right in relations:
        if left.lo > left.hi or right.lo > right.hi:
            lo0, hi0 = original[id(left)]
            return [
                {"source": f"fields.{left.name}.range",
                 "detail": f"declared window [{_value_label(left, lo0)}, {_value_label(left, hi0)}] narrowed "
                           f"to {_domain_window(left)}"},
                {"source": f"fields.{right.name}.range",
                 "detail": f"declared window [{_value_label(right, original[id(right)][0])}, "
                           f"{_value_label(right, original[id(right)][1])}] narrowed to {_domain_window(right)}"},
                {"source": f"record_rules:{rule.name}",
                 "detail": f"requires {rule.left} {rule.op} {rule.right}, but every left value exceeds "
                           "every right value"},
            ]
        slack = 1 if rule.op == "<" else 0
        if left.lo > right.hi - slack:
            return [
                {"source": f"fields.{left.name}.range",
                 "detail": f"smallest left value is {_value_label(left, left.lo)}"},
                {"source": f"fields.{right.name}.range",
                 "detail": f"largest right value is {_value_label(right, right.hi)}"},
                {"source": f"record_rules:{rule.name}",
                 "detail": f"{rule.left} {rule.op} {rule.right} cannot hold for any pair"},
            ]
        if left.explicit and not any(left.lo <= atom <= left.hi for atom in left.atoms):
            return [{"source": f"fields.{left.name}.enum",
                     "detail": "no enumerated candidate survives the cross relation"}]
        if right.explicit and not any(right.lo <= atom <= right.hi for atom in right.atoms):
            return [{"source": f"fields.{right.name}.enum",
                     "detail": "no enumerated candidate survives the cross relation"}]
    return None


def build_domains(layout: Layout, rules: RuleSet, charset: str,
                  abandoned: list[dict[str, str]]) -> dict[str, Domain]:
    occurrences = _occurrence_map(layout)
    all_leaves = layout.leaves()
    marker_name = rules.record_type.field if rules.record_type is not None else None
    marker_values: set[str] = {rules.record_type.data} if rules.record_type is not None else set()
    domains: dict[str, Domain] = {}
    for name, rule in rules.fields.items():
        occs = occurrences.get(name, [])
        source = f"fields.{name}"
        if len(occs) != 1 or occs[0].field.occurs > 1:
            abandoned.append({"source": source, "reason": REASON_OCCURS})
            continue
        occ = occs[0]
        if _overlaps(occ, all_leaves):
            abandoned.append({"source": source, "reason": REASON_REDEFINES})
            continue
        policy = rules.blank_policies.get(name, "null")
        # The data marker constrains only the marker field itself.
        field_markers = marker_values if name == marker_name else set()
        try:
            if occ.field.category == "CHAR":
                dom = _build_char_domain(name, occ, rule, field_markers, charset, abandoned)
            else:
                dom = _build_numeric_domain(name, occ, rule, field_markers, policy, abandoned)
        except Abandoned as give_up:
            abandoned.append({"source": give_up.source, "reason": give_up.reason})
            continue
        domains[name] = dom
    # The record-type marker field may have no field rule at all.
    if marker_name is not None and marker_name not in domains and marker_name in occurrences:
        occs = occurrences[marker_name]
        if len(occs) == 1 and not _overlaps(occs[0], all_leaves):
            try:
                if occs[0].field.category == "CHAR":
                    domains[marker_name] = _build_char_domain(
                        marker_name, occs[0], None, marker_values, charset, abandoned)
            except Abandoned as give_up:
                abandoned.append({"source": give_up.source, "reason": give_up.reason})
    return domains


def _atom_to_bytes(dom: Domain, atom: Any, charset: str) -> bytes | None:
    fld = dom.fld
    if dom.kind == "char":
        return _encode_char(fld, atom, charset)
    if dom.kind == "date":
        return _encode_date(fld, atom, dom.date_format or "%Y%m%d")
    if dom.kind == "integer":
        return _encode_integer(fld, atom)
    return None


def _choose_atom(dom: Domain) -> Any:
    if dom.kind == "char":
        return sorted(dom.atoms)[0]
    if dom.explicit:
        viable = [atom for atom in dom.atoms if dom.lo <= atom <= dom.hi]
        return min(viable)
    return dom.lo


def _out_of_scope(rules: RuleSet) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, rule in enumerate(rules.record_rules):
        if rule.kind == "unique_key":
            items.append({"source": f"record_rules[{index}]({rule.name})",
                          "reason": "unique_key is a multi-record state property"})
    if rules.record_type is not None and (rules.record_type.header or rules.record_type.trailer):
        items.append({"source": "record_type.header/trailer",
                      "reason": "placement rules describe whole files, not one data record"})
    if rules.trailer is not None:
        items.append({"source": "trailer", "reason": "trailer reconciliation is a file-level constraint"})
    if rules.min_records is not None or rules.max_records is not None:
        items.append({"source": "limits", "reason": "record counts are file-level constraints"})
    return items


def _coverage(rules: RuleSet, domains: dict[str, Domain],
              abandoned: list[dict[str, str]]) -> dict[str, list[Any]]:
    reasoned: list[str] = []
    for name, dom in sorted(domains.items()):
        for source in dom.sources:
            reasoned.append(source)
    for index, rule in enumerate(rules.record_rules):
        if rule.kind == "cross_field" and rule.op in ("<=", "<") \
                and (rule.left in domains) and (rule.right in domains):
            reasoned.append(f"record_rules[{index}]({rule.name})")
    if rules.record_type is not None:
        reasoned.append("record_type.data")
    return {"reasoned": sorted(set(reasoned)),
            "abandoned": sorted(abandoned, key=lambda item: item["source"]),
            "out_of_scope": sorted(_out_of_scope(rules), key=lambda item: item["source"])}


def verify_witness(layout: Layout, rules: RuleSet, charset: str, raw: bytes) -> dict[str, Any]:
    """Official decode + rule-engine re-check; the only accepted SAT evidence."""
    if len(raw) != layout.record_length:
        return {"ok": False, "stage": "length", "errors": [f"witness has {len(raw)} bytes, expected {layout.record_length}"],
                "hits": []}
    record = decode_record(layout, raw, record_no=1, byte_offset=0, charset=charset,
                           blank_policies=dict(rules.blank_policies))
    decode_errors = [item.to_dict() for item in record.errors]
    assign_record_type(record, rules)
    hits = RuleEngine(rules).validate_record(record, ValidationState())
    hit_payload = [hit.to_dict() for hit in hits]
    return {"ok": not decode_errors and not hit_payload,
            "stage": "decode+rules", "errors": decode_errors, "hits": hit_payload,
            "record_type": record.record_type,
            "values": {path: scalar_text(value) for path, value in sorted(record.flat.items())}}


def _witness_field_rows(layout: Layout, raw: bytes) -> list[dict[str, Any]]:
    rows = []
    for occ in layout.leaves():
        chunk = raw[occ.offset:occ.offset + occ.length]
        rows.append({"path": occ.path, "offset": occ.offset, "length": occ.length,
                     "evidence_sha8": sha8(chunk)})
    return rows


def _assemble(layout: Layout, rules: RuleSet, domains: dict[str, Domain], charset: str,
              atom_picker) -> tuple[bytes | None, str | None]:
    raw = bytearray(layout.record_length)
    claimed: set[int] = set()
    for occ in layout.leaves():
        if occ.offset in claimed:
            continue  # REDEFINES/overlapping branch keeps the already-encoded bytes
        dom = domains.get(occ.field.name)
        if dom is not None and dom.kind != "blank-only":
            chunk = _atom_to_bytes(dom, atom_picker(dom), charset)
            if chunk is None:
                return None, f"value for {occ.field.name} is not encodable in {charset}"
        else:
            policy = rules.blank_policies.get(occ.field.name, "null")
            chunk = _blank_filler(occ, charset, policy == "error")
        if len(chunk) != occ.length:
            return None, f"encoded {occ.field.name} as {len(chunk)} bytes, field needs {occ.length}"
        raw[occ.offset:occ.offset + occ.length] = chunk
        claimed.add(occ.offset)
    return bytes(raw), None


def _sample_atom(rng, dom: Domain) -> Any:
    if dom.kind == "char":
        return rng.choice(sorted(dom.atoms))
    if dom.explicit:
        viable = [atom for atom in dom.atoms if dom.lo <= atom <= dom.hi]
        return rng.choice(viable)
    return rng.randint(dom.lo, dom.hi)


def _witness_search(layout: Layout, rules: RuleSet, domains: dict[str, Domain], charset: str,
                    budget: int, seed: int) -> tuple[bytes | None, int]:
    rng = random.Random(seed)
    for attempt in range(budget):
        raw, _error = _assemble(layout, rules, domains, charset, lambda dom: _sample_atom(rng, dom))
        if raw is None:
            continue  # construction failure still consumes one attempt of the budget
        # Fill open / abandoned leaves with random encodable content.
        buffer = bytearray(raw)
        claimed = {occ.offset for occ in layout.leaves() if occ.field.name in domains}
        for occ in layout.leaves():
            if occ.offset in claimed:
                continue
            fld = occ.field
            if fld.category == "CHAR":
                text = "".join(rng.choice(_ALPHABET) for _ in range(fld.length))
                chunk = _encode_char(fld, text, charset) or (b" " * fld.length)
            else:
                base = _integer_base_interval(fld)
                if base is None:
                    chunk = b" " * fld.length
                else:
                    chunk = _encode_integer(fld, rng.randint(*base)) or _blank_filler(occ, charset, False)
            buffer[occ.offset:occ.offset + fld.length] = chunk
            claimed.add(occ.offset)
        candidate = bytes(buffer)
        if verify_witness(layout, rules, charset, candidate)["ok"]:
            return candidate, attempt + 1
    return None, budget


def certify(layout: Layout, rules: RuleSet, *, mode: str = "proof", charset: str = "utf-8",
            budget: int = DEFAULT_BUDGET, seed: int = DEFAULT_SEED,
            copybook_sha256: str | None = None, rules_sha256: str | None = None) -> dict[str, Any]:
    """Certify whether one data record can satisfy the contract on the layout."""
    if mode not in ("proof", "witness"):
        raise ValueError("mode must be 'proof' or 'witness'")
    abandoned: list[dict[str, str]] = []
    report: dict[str, Any] = {
        "verdict": "UNKNOWN", "mode": mode, "charset": charset,
        "record_length": layout.record_length,
        "copybook_sha256": copybook_sha256, "rules_sha256": rules_sha256,
        "search": {"budget": budget, "attempts": 0, "exhausted": False},
    }
    domains: dict[str, Domain] = {}
    proof_chain: list[dict[str, str]] | None = None
    try:
        domains = build_domains(layout, rules, charset, abandoned)
        relations = _cross_domains(rules, domains, abandoned)
        proof_chain = _propagate_cross(relations)
    except Contradiction as exc:
        proof_chain = exc.steps

    coverage = _coverage(rules, domains, abandoned)
    report["coverage"] = coverage

    # Witness mode exists to find test data; it is never allowed to certify
    # impossibility.  Even a proof-level contradiction is reported as UNKNOWN so
    # callers cannot mistake a missing sample for an audited UNSAT.
    if mode == "witness":
        if proof_chain is not None:
            report["unknown_reasons"] = [REASON_WITNESS_UNSAT]
            report["unknown_detail"] = ("witness mode never concludes UNSAT; proof-mode domain analysis found "
                                        "the attached contradiction chain")
            report["proof_contradiction_chain"] = proof_chain
            report["search"] = {"budget": budget, "attempts": 0, "exhausted": True}
            return report
        raw, attempts = _witness_search(layout, rules, domains, charset, budget, seed)
        report["search"] = {"budget": budget, "attempts": attempts, "exhausted": raw is None}
        if raw is None:
            report["unknown_reasons"] = [REASON_BUDGET]
            report["unknown_detail"] = ("witness mode never concludes UNSAT: the bounded search found no "
                                        "accepted record; run proof mode for a domain-level certificate")
            return report
        return _sat_report(report, layout, rules, domains, charset, raw)

    if proof_chain is not None:
        report.update(verdict="UNSAT", contradiction_chain=proof_chain, unknown_reasons=[])
        return report

    # Proof mode: no contradiction, but abandoned clauses block a full SAT proof.
    if abandoned:
        report["unknown_reasons"] = sorted({item["reason"] for item in abandoned})
        report["unknown_detail"] = "supported fragments are jointly satisfiable, but abandoned clauses " \
                                   "prevent a complete proof"
        return report

    raw, error = _assemble(layout, rules, domains, charset, _choose_atom)
    if raw is None:
        report["unknown_reasons"] = [REASON_CONSTRUCT]
        report["unknown_detail"] = error
        return report
    return _sat_report(report, layout, rules, domains, charset, raw)


def _sat_report(report: dict[str, Any], layout: Layout, rules: RuleSet,
                domains: dict[str, Domain], charset: str, raw: bytes) -> dict[str, Any]:
    verification = verify_witness(layout, rules, charset, raw)
    if not verification["ok"]:
        report.update(verdict="UNKNOWN", unknown_reasons=[REASON_VERIFY],
                      unknown_detail={"stage": verification["stage"], "decode_errors": verification["errors"],
                                      "rule_hits": verification["hits"]})
        return report
    report.update(verdict="SAT", witness={
        "raw_hex": raw.hex(), "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "length": len(raw), "record_type": verification["record_type"],
        "fields": _witness_field_rows(layout, raw),
        "decoded_values": verification["values"],
        "decode_record": {"errors": verification["errors"]},
        "rule_engine": {"hits": verification["hits"], "accepted": True},
    })
    report["unknown_reasons"] = []
    return report
