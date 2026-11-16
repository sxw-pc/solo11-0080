"""Field/record checks, unique state and rules-contract validation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import FIXTURES, cpy
from copybook_kit.copybook.layout import compile_copybook
from copybook_kit.errors import RulesError
from copybook_kit.model import Record
from copybook_kit.rules.checks import ValidationState, cross_hit, unique_checks, validate_field
from copybook_kit.rules.engine import RuleEngine, assign_record_type
from copybook_kit.rules.loader import load_rules
from copybook_kit.rules.schema import CheckDigitRule, FieldRule, RecordRule, RecordTypeRule, RuleSet

def record(**flat):
    return Record(record_no=1, byte_offset=0, raw=b"", flat=flat)

@pytest.mark.parametrize(("value", "rule", "code"), [
    (None, FieldRule("F", required=True), "R001_REQUIRED"),
    ("Z", FieldRule("F", enum=("A", "B")), "R002_ENUM"),
    (Decimal("11"), FieldRule("F", range_min="0", range_max="10"), "R003_RANGE"),
    ("20261301", FieldRule("F", date_format="%Y%m%d"), "R004_DATE"),
    ("AB", FieldRule("F", pattern=r"[0-9]{3}"), "R005_PATTERN"),
    ("123", FieldRule("F", check_digit=CheckDigitRule((1, 2), 7)), "R006_CHECK_DIGIT"),
    (Decimal("1.234"), FieldRule("F", scale=2), "R007_SCALE"),
])
def test_field_rule_codes(value, rule, code):
    assert [hit.code for hit in validate_field(record(F=value), "F", value, rule)] == [code]

def test_valid_values_and_iso_date_format_regression():
    checks = [("2026-01-02", FieldRule("F", date_format="%Y-%m-%d")),
              ("A", FieldRule("F", enum=("A", "B"), required=True)),
              (Decimal("1.20"), FieldRule("F", range_min="1", range_max="2", scale=2)),
              ("122", FieldRule("F", check_digit=CheckDigitRule((1, 2), 7)))]
    for value, rule in checks:
        assert validate_field(record(F=value), "F", value, rule) == []

def test_cross_field_unique_missing_and_rejected_record_does_not_poison_state():
    cross = RecordRule("order", "cross_field", left="A", op="<=", right="B")
    hit = cross_hit(record(A="2", B="1"), cross)
    assert hit and hit.code == "R009_CROSS_FIELD"
    assert cross_hit(record(A="1", B="2"), cross) is None
    unique = RecordRule("key", "unique_key", fields=("A",))
    rules = RuleSet(name="test", version=1, record_rules=(unique,))
    state = ValidationState()
    assert unique_checks(record(A="x"), rules, state) == []
    assert [hit.code for hit in unique_checks(record(A="x"), rules, state)] == ["R008_UNIQUE"]
    assert [hit.code for hit in unique_checks(record(A=None), rules, state)] == ["R008_UNIQUE"]
    required_rules = RuleSet(name="test", version=1,
                             fields={"A": FieldRule("A", required=True)}, record_rules=(unique,))
    engine, state = RuleEngine(required_rules), ValidationState()
    assert [hit.code for hit in engine.validate_record(record(A=None), state=state)] == ["R001_REQUIRED"]
    assert engine.validate_record(record(A="x"), state=state) == []

def test_record_type_assignment_and_unknown_type():
    rules = RuleSet(name="test", version=1, record_type=RecordTypeRule(field="TYPE", data="D", header="H"))
    data = record(TYPE="D")
    assign_record_type(data, rules)
    assert data.record_type == "D" and not RuleEngine(rules).validate_record(data, ValidationState())
    unknown = record(TYPE="X")
    assign_record_type(unknown, rules)
    assert [hit.code for hit in RuleEngine(rules).validate_record(unknown, ValidationState())] == [
        "R010_RECORD_TYPE"]

def write_rules(tmp_path, text: str):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    return path

def base_layout():
    return compile_copybook(cpy("01 R.", "05 A PIC X(8).", "05 B PIC X(8)."))

def test_loader_accepts_full_date_format_and_fixture_rules(tmp_path):
    path = write_rules(tmp_path, "version: 1\nname: x\nfields:\n  A:\n    date_format: '%Y-%m-%d'\n"
                       "    range: {min: '2020-01-01', max: '2026-12-31'}\n")
    rules = load_rules(path, base_layout())
    assert rules.fields["A"].date_format == "%Y-%m-%d"
    fixture = load_rules(FIXTURES / "rules" / "customer_rules.yaml", None)
    assert fixture.name == "customer-master-demo" and fixture.blank_policies == {}

@pytest.mark.parametrize(("yaml_text", "code"), [
    ("version: 2\nname: x\n", "RC001"), ("version: 1\nname: x\nunknown: true\n", "RC001"),
    ("version: 1\nname: x\nfields:\n  Z: {required: true}\n", "RC004"),
    ("version: 1\nname: x\nfields:\n  A:\n    pattern: '(?=A)'\n", "RC002"),
    ('version: 1\nname: x\nfields:\n  A:\n    required: "yes"\n', "RC003"),
    ("version: 1\nname: x\nfields:\n  A:\n    range: {min: 1.5}\n", "RC003"),
    ("version: 1\nname: x\nfields:\n  A:\n    date_format: '%Q'\n", "RC003"),
    ("version: 1\nname: x\nfields:\n  A:\n    date_format: '%Y-%m-%d'\n    range: {min: nope}\n", "RC003"),
    ("version: 1\nname: x\ntrailer: {count_field: A}\n", "RC001"),
])
def test_loader_rejects_bad_contracts(tmp_path, yaml_text, code):
    with pytest.raises(RulesError) as caught:
        load_rules(write_rules(tmp_path, yaml_text), base_layout())
    assert caught.value.code == code

def test_duplicate_yaml_key_is_rejected(tmp_path):
    with pytest.raises(RulesError):
        load_rules(write_rules(tmp_path, "version: 1\nname: x\nfields:\n  A: {required: true}\n"
                              "  A: {required: false}\n"), base_layout())
