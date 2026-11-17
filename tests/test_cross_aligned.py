"""Aligned cross-field rules over OCCURS groups, broadcast, ambiguity and evidence.

These tests pin the behavior that replaces the old "take the first same-named
leaf" shortcut:

* repeated leaves are paired by their full OCCURS coordinate tuple, never by
  the accidental order of flattened lists;
* ``all_aligned`` / ``any_aligned`` are two explicit business policies with
  defined missing-value handling and verifiable witness coordinates;
* ambiguous projections (distinct OCCURS ancestors, REDEFINES same-name
  conflicts) are rejected while the contract loads, before any record runs;
* R009 evidence keeps decoded paths, absolute raw-byte offsets and the rule
  name, and record-level checks never mutate unique-key cross-record state.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from conftest import NOW, cpy
from copybook_kit.copybook.layout import compile_copybook
from copybook_kit.decode.record import decode_record
from copybook_kit.errors import RulesError
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.pipeline.runner import run
from copybook_kit.rules.checks import ValidationState, cross_hit, evaluate_cross
from copybook_kit.rules.engine import RuleEngine
from copybook_kit.rules.loader import load_rules


def write_rules(tmp_path, text: str, name: str = "rules.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def pair_layout():
    return compile_copybook(cpy(
        "01 R.",
        "05 PAIR OCCURS 2 TIMES.",
        "10 LOW PIC 9.",
        "10 HIGH PIC 9.",
    ))


def pair_rules(quantifier: str) -> str:
    return f"""version: 1
name: pair
record_rules:
  - name: pair-limit
    kind: cross_field
    left: LOW
    op: "<="
    right: HIGH
    quantifier: {quantifier}
"""


def decode(layout, raw, *, record_no: int = 1, byte_offset: int = 0):
    return decode_record(layout, raw, record_no=record_no, byte_offset=byte_offset, charset="utf-8")


def load(tmp_path, layout, text):
    return load_rules(write_rules(tmp_path, text), layout)


# --------------------------------------------------------------------------- #
# Minimal reproduction from the defect report: PAIR OCCURS 2, raw 1293.
# --------------------------------------------------------------------------- #

def test_pair_occurs_minimal_repro_flat_paths():
    layout = pair_layout()
    assert layout.valid
    record = decode(layout, b"1293")
    assert record.flat == {
        "PAIR[0].LOW": Decimal("1"), "PAIR[0].HIGH": Decimal("2"),
        "PAIR[1].LOW": Decimal("9"), "PAIR[1].HIGH": Decimal("3"),
    }


def test_quantifiers_give_different_decisions_on_same_record(tmp_path):
    layout, record = pair_layout(), decode(pair_layout(), b"1293")
    all_rules = load(tmp_path, layout, pair_rules("all_aligned"))
    any_rules = load(tmp_path, layout, pair_rules("any_aligned"))
    all_binding = all_rules.cross_bindings["pair-limit"]
    any_binding = any_rules.cross_bindings["pair-limit"]

    all_result = evaluate_cross(record, all_rules.record_rules[0], all_binding)
    assert all_result.accepted is False
    assert all_result.witness.coord == (1,)
    assert all_result.witness.left_path == "PAIR[1].LOW"
    assert all_result.witness.right_path == "PAIR[1].HIGH"
    assert (all_result.witness.left, all_result.witness.right) == (Decimal("9"), Decimal("3"))

    any_result = evaluate_cross(record, any_rules.record_rules[0], any_binding)
    assert any_result.accepted is True
    assert any_result.witness.coord == (0,)
    assert (any_result.witness.left, any_result.witness.right) == (Decimal("1"), Decimal("2"))

    all_hit = cross_hit(record, all_rules.record_rules[0], all_binding)
    assert all_hit is not None and all_hit.code == "R009_CROSS_FIELD"
    assert cross_hit(record, any_rules.record_rules[0], any_binding) is None


def test_binding_aligns_same_indexes_and_never_zips_flat_order(tmp_path):
    layout = pair_layout()
    rules = load(tmp_path, layout, pair_rules("all_aligned"))
    binding = rules.cross_bindings["pair-limit"]
    assert binding.coordinates() == [(0,), (1,)]
    assert binding.left.paths == ("PAIR[0].LOW", "PAIR[1].LOW")
    assert binding.right.paths == ("PAIR[0].HIGH", "PAIR[1].HIGH")
    # Reversing the HIGH bytes still pairs by coordinate: [0] vs [0], [1] vs [1].
    record = decode(layout, b"1392")  # (1<=3 holds), (9<=2 fails)
    result = evaluate_cross(record, rules.record_rules[0], binding)
    assert [item.coord for item in result.comparisons] == [(0,), (1,)]
    assert result.accepted is False and result.witness.coord == (1,)
    # A naive flat-list zip would have paired LOW[0] with HIGH[1] and stopped
    # at the first element; the aligned engine reports both comparisons.
    assert len(result.comparisons) == 2


def test_legacy_bare_name_rule_on_occurs_is_rejected_at_load(tmp_path):
    layout = pair_layout()
    legacy = """version: 1
name: pair
record_rules:
  - name: pair-limit
    kind: cross_field
    left: LOW
    op: "<="
    right: HIGH
"""
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, legacy)
    assert caught.value.code == "RC003" and "quantifier" in caught.value.message


# --------------------------------------------------------------------------- #
# Two nested OCCURS levels: full coordinate tuples define the pairing.
# --------------------------------------------------------------------------- #

def nested_layout():
    return compile_copybook(cpy(
        "01 R.",
        "05 BATCH OCCURS 2 TIMES.",
        "10 ITEM OCCURS 2 TIMES.",
        "15 LOW PIC 9.",
        "15 HIGH PIC 9.",
    ))


def nested_rules(quantifier: str) -> str:
    return f"""version: 1
name: nested
record_rules:
  - name: nested-limit
    kind: cross_field
    left: LOW
    op: "<="
    right: HIGH
    quantifier: {quantifier}
"""


def test_nested_occurs_pairs_by_full_coordinate_tuple(tmp_path):
    layout = nested_layout()
    assert layout.valid and layout.record_length == 8
    # batch0: (1<=2),(3<=4) hold; batch1: (9<=3) fails at [1][0], (1<=1) holds.
    record = decode(layout, b"12349311")
    rules = load(tmp_path, layout, nested_rules("all_aligned"))
    binding = rules.cross_bindings["nested-limit"]
    assert binding.coordinates() == [(0, 0), (0, 1), (1, 0), (1, 1)]
    result = evaluate_cross(record, rules.record_rules[0], binding)
    assert result.accepted is False
    assert result.witness.coord == (1, 0)
    assert result.witness.left_path == "BATCH[1].ITEM[0].LOW"
    assert result.witness.right_path == "BATCH[1].ITEM[0].HIGH"
    # Explicit full-coordinate witness: outer[0].inner[1] must never pair with
    # outer[1].inner[0].
    paired = {(c.left_path, c.right_path) for c in result.comparisons}
    assert ("BATCH[0].ITEM[1].LOW", "BATCH[0].ITEM[1].HIGH") in paired
    assert ("BATCH[0].ITEM[1].LOW", "BATCH[1].ITEM[0].HIGH") not in paired


def test_nested_naive_zip_demonstrates_the_mispairing_being_prevented(tmp_path):
    """Document why order-based zipping is wrong on nested OCCURS."""
    layout = nested_layout()
    # Every coordinate holds under true alignment (1<=2, 3<=4, 5<=6, 7<=8).
    record = decode(layout, b"12345678")
    rules = load(tmp_path, layout, nested_rules("all_aligned"))
    result = evaluate_cross(record, rules.record_rules[0],
                            rules.cross_bindings["nested-limit"])
    assert result.accepted is True
    # A flat-order zip after independently shuffling one side's storage order
    # would fabricate failures; the coordinate-keyed pairing cannot.
    lows = sorted((p, v) for p, v in record.flat.items() if p.endswith(".LOW"))
    highs = sorted((p, v) for p, v in record.flat.items() if p.endswith(".HIGH"))
    naive = list(zip((v for _, v in lows), (v for _, v in reversed(highs))))
    assert any(left > right for left, right in naive)  # naive zip "fails"
    assert all(item.held for item in result.comparisons)  # aligned result holds


def test_nested_quantifiers_diverge(tmp_path):
    layout = nested_layout()
    record = decode(layout, b"12349311")
    any_rules = load(tmp_path, layout, nested_rules("any_aligned"))
    result = evaluate_cross(record, any_rules.record_rules[0],
                            any_rules.cross_bindings["nested-limit"])
    assert result.accepted is True and result.witness.coord == (0, 0)


# --------------------------------------------------------------------------- #
# Scalar broadcast to every installment of a repeated leaf.
# --------------------------------------------------------------------------- #

def broadcast_layout():
    return compile_copybook(cpy(
        "01 R.",
        "05 CAP PIC 9.",
        "05 PAIR OCCURS 2 TIMES.",
        "10 LOW PIC 9.",
    ))


def test_scalar_broadcasts_to_every_occurrence_index(tmp_path):
    layout = broadcast_layout()
    assert layout.valid and layout.record_length == 3
    record = decode(layout, b"519")  # CAP=5; LOW[0]=1 holds, LOW[1]=9 fails
    text = """version: 1
name: broadcast
record_rules:
  - name: cap-low
    kind: cross_field
    left: LOW
    op: "<="
    right: CAP
    quantifier: all_aligned
"""
    rules = load(tmp_path, layout, text)
    binding = rules.cross_bindings["cap-low"]
    assert binding.coordinates() == [(0,), (1,)]
    assert binding.right.paths == ("CAP",) and binding.left.paths == (
        "PAIR[0].LOW", "PAIR[1].LOW")
    result = evaluate_cross(record, rules.record_rules[0], binding)
    assert result.accepted is False and result.witness.coord == (1,)
    assert result.witness.left_path == "PAIR[1].LOW" and result.witness.right_path == "CAP"
    # A first-value/zip shortcut would only see LOW[0] and wrongly accept.
    assert len(result.comparisons) == 2
    assert [(c.left_path, c.right_path) for c in result.comparisons] == [
        ("PAIR[0].LOW", "CAP"), ("PAIR[1].LOW", "CAP")]


def test_broadcast_any_aligned_accepts_with_one_holding_installment(tmp_path):
    layout = broadcast_layout()
    record = decode(layout, b"519")
    text = """version: 1
name: broadcast
record_rules:
  - name: cap-low
    kind: cross_field
    left: LOW
    op: "<="
    right: CAP
    quantifier: any_aligned
"""
    rules = load(tmp_path, layout, text)
    result = evaluate_cross(record, rules.record_rules[0],
                            rules.cross_bindings["cap-low"])
    assert result.accepted is True and result.witness.coord == (0,)


def inner_broadcast_layout():
    # CAP spans only the outer axis; V spans both axes, so CAP broadcasts to
    # every inner occurrence of the outer group it belongs to.
    return compile_copybook(cpy(
        "01 R.",
        "05 OUT OCCURS 2 TIMES.",
        "10 CAP PIC 9.",
        "10 IN OCCURS 2 TIMES.",
        "15 V PIC 9.",
    ))


def test_outer_scalar_broadcasts_over_inner_occurrences(tmp_path):
    layout = inner_broadcast_layout()
    assert layout.valid and layout.record_length == 6
    record = decode(layout, b"112334")  # CAP=1/3; V=1,2 / 3,4
    text = """version: 1
name: inner
record_rules:
  - name: cap-v
    kind: cross_field
    left: CAP
    op: "<="
    right: V
    quantifier: all_aligned
"""
    rules = load(tmp_path, layout, text)
    binding = rules.cross_bindings["cap-v"]
    assert binding.coordinates() == [(0, 0), (0, 1), (1, 0), (1, 1)]
    result = evaluate_cross(record, rules.record_rules[0], binding)
    assert result.accepted is True
    assert [(c.coord, c.left_path) for c in result.comparisons] == [
        ((0, 0), "OUT[0].CAP"), ((0, 1), "OUT[0].CAP"),
        ((1, 0), "OUT[1].CAP"), ((1, 1), "OUT[1].CAP")]
    # CAP[0] must never be paired with an inner value of OUT[1].
    assert all(not (c.left_path == "OUT[0].CAP" and c.right_path.startswith("OUT[1]"))
               for c in result.comparisons)


def test_literal_index_selector_pins_one_installment(tmp_path):
    layout = pair_layout()
    record = decode(layout, b"1293")  # pair[0] holds; pair[1] fails
    text = """version: 1
name: pin
record_rules:
  - name: only-first
    kind: cross_field
    left: LOW
    left_path: PAIR[0].LOW
    op: "<="
    right: HIGH
    right_path: PAIR[0].HIGH
    quantifier: all_aligned
"""
    rules = load(tmp_path, layout, text)
    binding = rules.cross_bindings["only-first"]
    assert binding.coordinates() == [(0,)]
    result = evaluate_cross(record, rules.record_rules[0], binding)
    assert result.accepted is True and len(result.comparisons) == 1


def test_unbalanced_index_pin_across_axes_is_rejected(tmp_path):
    layout = inner_broadcast_layout()
    text = """version: 1
name: inner
record_rules:
  - name: cap-v
    kind: cross_field
    left: CAP
    left_path: OUT[0].CAP
    op: "<="
    right: V
    quantifier: all_aligned
"""
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, text)
    assert caught.value.code == "RC003"


# --------------------------------------------------------------------------- #
# Same leaf name under distinct OCCURS ancestors: reject before running.
# --------------------------------------------------------------------------- #

def distinct_ancestor_layout():
    return compile_copybook(cpy(
        "01 R.",
        "05 A OCCURS 2 TIMES.",
        "10 V PIC 9.",
        "05 B OCCURS 2 TIMES.",
        "10 V PIC 9.",
    ))


def test_distinct_ancestor_same_name_is_ambiguous_without_selector(tmp_path):
    layout = distinct_ancestor_layout()
    assert layout.valid
    text = """version: 1
name: distinct
record_rules:
  - name: distinct-v
    kind: cross_field
    left: V
    op: "<="
    right: V
    quantifier: all_aligned
"""
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, text)
    assert caught.value.code == "RC003"
    message = caught.value.message
    assert "ambiguous" in message
    assert "A[0].V" in message and "B[0].V" in message  # conflicting paths listed


def test_distinct_ancestor_remains_rejected_even_with_explicit_selectors(tmp_path):
    layout = distinct_ancestor_layout()
    text = """version: 1
name: distinct
record_rules:
  - name: distinct-v
    kind: cross_field
    left: V
    op: "<="
    right: V
    left_path: A[*].V
    right_path: B[*].V
    quantifier: all_aligned
"""
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, text)
    assert caught.value.code == "RC003"
    assert "distinct OCCURS ancestors" in caught.value.message


# --------------------------------------------------------------------------- #
# REDEFINES same-name conflict: two physical paths over the same bytes.
# --------------------------------------------------------------------------- #

def redefines_layout():
    return compile_copybook(cpy(
        "01 R.",
        "05 GRP.",
        "10 V PIC 9(2).",
        "05 GRP2 REDEFINES GRP.",
        "10 V PIC 9(2).",
        "05 W PIC 9(2).",
    ))


def test_redefines_same_name_conflict_lists_both_paths(tmp_path):
    layout = redefines_layout()
    assert layout.valid
    text = """version: 1
name: redef
record_rules:
  - name: redef-v
    kind: cross_field
    left: V
    op: "<="
    right: W
"""
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, text)
    assert caught.value.code == "RC003"
    message = caught.value.message
    assert "ambiguous" in message
    assert "GRP.V" in message and "GRP2.V" in message


def test_redefines_conflict_resolved_by_explicit_selector(tmp_path):
    layout = redefines_layout()
    text = """version: 1
name: redef
record_rules:
  - name: redef-v
    kind: cross_field
    left: V
    left_path: GRP2.V
    op: "<="
    right: W
"""
    rules = load(tmp_path, layout, text)
    binding = rules.cross_bindings["redef-v"]
    assert binding.left.paths == ("GRP2.V",) and binding.right.paths == ("W",)
    record = decode(layout, b"1234")
    assert cross_hit(record, rules.record_rules[0], binding) is None  # 12 <= 34


def test_selector_that_matches_nothing_is_rc004(tmp_path):
    layout = pair_layout()
    text = pair_rules("all_aligned").replace(
        "    right: HIGH\n", '    right: HIGH\n    left_path: "OTHER[*].LOW"\n')
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, text)
    assert caught.value.code == "RC004"


@pytest.mark.parametrize("bad_selector", ["PAIR[x].LOW", "pair[*].low", "PAIR[*].", ".LOW"])
def test_malformed_selectors_are_rc003(tmp_path, bad_selector):
    layout = pair_layout()
    text = pair_rules("all_aligned").replace(
        "    right: HIGH\n", f"    left_path: \"{bad_selector}\"\n")
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, text)
    assert caught.value.code == "RC003"


def test_quantifier_value_and_selector_keys_are_validated(tmp_path):
    layout = pair_layout()
    bad_quantifier = pair_rules("every_pair")
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, bad_quantifier)
    assert caught.value.code == "RC003"
    bad_key = pair_rules("all_aligned").replace(
        "    quantifier: all_aligned\n", "    quantifier: all_aligned\n    bogus: true\n")
    with pytest.raises(RulesError):
        load(tmp_path, layout, bad_key)


def test_quantifier_on_plain_scalar_rule_is_rejected(tmp_path):
    layout = compile_copybook(cpy("01 R.", "05 A PIC 9.", "05 B PIC 9."))
    text = """version: 1
name: scalar
record_rules:
  - name: s
    kind: cross_field
    left: A
    op: "<="
    right: B
    quantifier: all_aligned
"""
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, text)
    assert caught.value.code == "RC003"


def test_duplicate_record_rule_names_are_rejected(tmp_path):
    layout = pair_layout()
    text = pair_rules("all_aligned").replace(
        "record_rules:\n",
        "record_rules:\n"
        "  - {name: pair-limit, kind: unique_key, fields: [LOW]}\n",
    )
    with pytest.raises(RulesError) as caught:
        load(tmp_path, layout, text)
    assert caught.value.code == "RC003" and "unique" in caught.value.message


# --------------------------------------------------------------------------- #
# COMP-3 decode failure: missing values never count as a successful comparison.
# --------------------------------------------------------------------------- #

def comp3_layout():
    return compile_copybook(cpy(
        "01 R.",
        "05 PAIR OCCURS 2 TIMES.",
        "10 LOW PIC 9.",
        "10 HIGH PIC S9(2) USAGE COMP-3.",
    ))


def comp3_rules(quantifier: str) -> str:
    return f"""version: 1
name: comp3
record_rules:
  - name: pair-limit
    kind: cross_field
    left: LOW
    op: "<="
    right: HIGH
    quantifier: {quantifier}
"""


def test_comp3_decode_failure_is_a_reported_missing_pair(tmp_path):
    layout = comp3_layout()
    assert layout.valid and layout.record_length == 6
    raw = b"1" + bytes.fromhex("020C") + b"9" + bytes.fromhex("000A")
    record = decode(layout, raw, record_no=4, byte_offset=100)
    assert [error.code for error in record.errors] == ["DE002"]
    assert record.flat["PAIR[1].HIGH"] is None

    all_rules = load(tmp_path, layout, comp3_rules("all_aligned"))
    result = evaluate_cross(record, all_rules.record_rules[0],
                            all_rules.cross_bindings["pair-limit"])
    assert result.accepted is False
    assert len(result.comparisons) == 1 and result.comparisons[0].coord == (0,)
    assert len(result.missing) == 1
    missing = result.missing[0]
    assert missing.coord == (1,) and missing.missing_side == "right"
    assert missing.decode_error == "DE002"
    assert missing.right_path == "PAIR[1].HIGH"
    # Physical byte position and raw-byte evidence survive the failed decode.
    assert missing.right_byte_offset == 4
    assert missing.right_evidence == record.evidence["PAIR[1].HIGH"]

    any_rules = load(tmp_path, layout, comp3_rules("any_aligned"))
    any_result = evaluate_cross(record, any_rules.record_rules[0],
                                any_rules.cross_bindings["pair-limit"])
    assert any_result.accepted is True and any_result.witness.coord == (0,)
    assert len(any_result.missing) == 1  # missing installment still reported


def test_all_missing_installments_reject_both_quantifiers(tmp_path):
    layout = comp3_layout()
    raw = b"1" + bytes.fromhex("000A") + b"9" + bytes.fromhex("000A")
    record = decode(layout, raw)
    assert len(record.errors) == 2
    for quantifier in ("all_aligned", "any_aligned"):
        rules = load(tmp_path, layout, comp3_rules(quantifier))
        result = evaluate_cross(record, rules.record_rules[0],
                                rules.cross_bindings["pair-limit"])
        # Explicit policy: no defined comparison means rejection, never the
        # truthy default of any([]) / all([]).
        assert result.accepted is False
        assert result.comparisons == () and len(result.missing) == 2
        hit = cross_hit(record, rules.record_rules[0],
                        rules.cross_bindings["pair-limit"])
        assert hit.code == "R009_CROSS_FIELD"
        assert hit.detail["witness"]["kind"] == "missing"


# --------------------------------------------------------------------------- #
# R009 evidence: paths, absolute byte offsets, rule name; unique-key isolation.
# --------------------------------------------------------------------------- #

def test_r009_evidence_keeps_paths_absolute_offsets_and_rule_name(tmp_path):
    layout = pair_layout()
    record = decode(layout, b"1293", record_no=3, byte_offset=24)
    rules = load(tmp_path, layout, pair_rules("all_aligned"))
    hit = cross_hit(record, rules.record_rules[0], rules.cross_bindings["pair-limit"])
    assert hit is not None and hit.code == "R009_CROSS_FIELD" and hit.scope == "record"
    detail = hit.detail
    assert detail["rule"] == "pair-limit" and detail["quantifier"] == "all_aligned"
    witness = detail["witness"]
    assert witness["left_path"] == "PAIR[1].LOW"
    assert witness["right_path"] == "PAIR[1].HIGH"
    # Absolute file offsets: record starts at 24; LOW[1] is at +2, HIGH[1] +3.
    assert witness["left_byte_offset"] == 26 and witness["right_byte_offset"] == 27
    assert witness["left_evidence"] == record.evidence["PAIR[1].LOW"]
    assert witness["right_evidence"] == record.evidence["PAIR[1].HIGH"]
    assert [entry["coord"] for entry in detail["comparisons"]] == [[0], [1]]
    assert detail["comparisons"][1]["held"] is False
    # The payload is JSON-serializable for rejects.jsonl and the review desk.
    import json
    from copybook_kit.util import json_safe
    json.dumps(json_safe(detail))


def test_cross_field_rejection_does_not_poison_unique_key_state(tmp_path):
    layout = compile_copybook(cpy(
        "01 R.",
        "05 KEY PIC X(1).",
        "05 PAIR OCCURS 2 TIMES.",
        "10 LOW PIC 9.",
        "10 HIGH PIC 9.",
    ))
    text = """version: 1
name: isolated
record_rules:
  - name: key-unique
    kind: unique_key
    fields: [KEY]
  - name: pair-limit
    kind: cross_field
    left: LOW
    op: "<="
    right: HIGH
    quantifier: all_aligned
"""
    rules = load(tmp_path, layout, text)
    engine, state = RuleEngine(rules), ValidationState()
    rejected = decode(layout, b"X1293")
    hits = engine.validate_record(rejected, state)
    assert [hit.code for hit in hits] == ["R009_CROSS_FIELD"]
    assert state.unique == {}  # rejected record never commits its key
    # A later record reusing KEY=X is therefore not a duplicate.
    accepted = decode(layout, b"X1234")  # (1<=2),(3<=4) both hold
    assert engine.validate_record(accepted, state) == []
    duplicate = decode(layout, b"X1234")
    dup_hits = engine.validate_record(duplicate, state)
    assert [hit.code for hit in dup_hits] == ["R008_UNIQUE"]


# --------------------------------------------------------------------------- #
# Legacy single-value rules keep their original conclusions.
# --------------------------------------------------------------------------- #

def test_legacy_scalar_cross_field_rule_regression(tmp_path):
    layout = compile_copybook(cpy("01 R.", "05 A PIC 9.", "05 B PIC 9."))
    text = """version: 1
name: legacy
record_rules:
  - name: order
    kind: cross_field
    left: A
    op: "<="
    right: B
"""
    rules = load(tmp_path, layout, text)
    assert rules.cross_bindings["order"].is_scalar_pair
    record_ok = decode(layout, b"12")
    record_bad = decode(layout, b"21")
    assert cross_hit(record_ok, rules.record_rules[0],
                     rules.cross_bindings["order"]) is None
    hit = cross_hit(record_bad, rules.record_rules[0], rules.cross_bindings["order"])
    assert hit is not None and hit.code == "R009_CROSS_FIELD"
    assert hit.detail["quantifier"] is None
    # Missing side preserves the old pass-through behavior for scalar rules.
    missing = decode(layout, b"  ")
    assert cross_hit(missing, rules.record_rules[0],
                     rules.cross_bindings["order"]) is None


def test_layout_free_contract_keeps_legacy_first_value_semantics():
    # Loading without a Layout (contract lint mode) stays backward compatible.
    from copybook_kit.rules.schema import RecordRule, RuleSet
    from copybook_kit.model import Record
    rule = RecordRule("order", "cross_field", left="A", op="<=", right="B")
    ruleset = RuleSet(name="test", version=1, record_rules=(rule,))
    ok = Record(record_no=1, byte_offset=0, raw=b"", flat={"A": Decimal("1"), "B": Decimal("2")})
    bad = Record(record_no=1, byte_offset=0, raw=b"", flat={"A": Decimal("2"), "B": Decimal("1")})
    assert cross_hit(ok, rule, ruleset.cross_bindings.get("order")) is None
    assert cross_hit(bad, rule, ruleset.cross_bindings.get("order")).code == "R009_CROSS_FIELD"


def test_quantified_rule_without_binding_fails_loudly():
    from copybook_kit.rules.schema import RecordRule
    from copybook_kit.model import Record
    rule = RecordRule("q", "cross_field", left="LOW", op="<=", right="HIGH",
                      quantifier="all_aligned")
    record = Record(record_no=1, byte_offset=0, raw=b"",
                    flat={"PAIR[0].LOW": Decimal("1"), "PAIR[0].HIGH": Decimal("2")})
    with pytest.raises(RuntimeError):
        cross_hit(record, rule, None)


# --------------------------------------------------------------------------- #
# End-to-end pipeline: rejects.jsonl evidence, unique-key isolation, reprocess.
# --------------------------------------------------------------------------- #

def _write_pipeline_fixtures(tmp_path):
    copybook = cpy(
        "01 R.",
        "05 KEY PIC X(1).",
        "05 PAIR OCCURS 2 TIMES.",
        "10 LOW PIC 9.",
        "10 HIGH PIC 9.",
    )
    rules_all = """version: 1
name: pair-e2e
record_rules:
  - name: key-unique
    kind: unique_key
    fields: [KEY]
  - name: pair-limit
    kind: cross_field
    left: LOW
    op: "<="
    right: HIGH
    quantifier: all_aligned
"""
    rules_any = rules_all.replace("all_aligned", "any_aligned")
    cpy_path = tmp_path / "record.cpy"
    all_path = tmp_path / "rules_all.yaml"
    any_path = tmp_path / "rules_any.yaml"
    data_path = tmp_path / "data.dat"
    cpy_path.write_text(copybook, encoding="utf-8")
    all_path.write_text(rules_all, encoding="utf-8")
    any_path.write_text(rules_any, encoding="utf-8")
    # Record 1: (1<=2) holds, (9<=3) fails. Record 2: both hold. Record 3 reuses
    # KEY=X of record 1; under all_aligned record 1 must not commit its key.
    data_path.write_bytes(b"X1293" + b"Y1234" + b"X1234")
    return cpy_path, all_path, any_path, data_path


def test_pipeline_r009_reject_evidence_and_unique_key_isolation(tmp_path):
    cpy_path, all_path, _, data_path = _write_pipeline_fixtures(tmp_path)
    options = RunOptions(input_path=data_path, copybook_path=cpy_path,
                         rules_path=all_path, out_root=tmp_path / "runs",
                         db_path=tmp_path / "ledger.sqlite", now=NOW)
    result = run(options)
    assert result.status == "completed_with_rejects"
    assert result.reject_codes == {"R009_CROSS_FIELD": 1}
    run_dir = tmp_path / "runs" / result.run_id
    entries = [json.loads(line) for line in
               (run_dir / "rejects.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1 and entries[0]["record_no"] == 1
    entry = entries[0]
    # Formal decoded paths, absolute byte offsets and the rule name survive.
    hit = next(item for item in entry["rule_hits"] if item["code"] == "R009_CROSS_FIELD")
    witness = hit["detail"]["witness"]
    assert witness["left_path"] == "PAIR[1].LOW"
    assert witness["right_path"] == "PAIR[1].HIGH"
    # Record 1 starts at file offset 0; PAIR[1].LOW/HIGH sit at relative
    # offsets 3 and 4 (KEY=1 byte + two bytes for installment zero).
    assert witness["left_byte_offset"] == 3 and witness["right_byte_offset"] == 4
    assert hit["detail"]["rule"] == "pair-limit"
    assert hit["detail"]["quantifier"] == "all_aligned"
    # Raw bytes are quarantined with the same offsets the witness references.
    raw = (run_dir / entry["raw_file"]).read_bytes()
    assert raw == b"X1293"
    source = data_path.read_bytes()
    assert raw[witness["left_byte_offset"] - entry["byte_offset"]:][:1] == b"9"
    assert source[witness["left_byte_offset"]: witness["left_byte_offset"] + 1] == b"9"
    accepted = [json.loads(line) for line in
                (run_dir / "accepted.jsonl").read_text(encoding="utf-8").splitlines()]
    # Record 3 reuses KEY=X: only record 1's rejection kept it out of unique
    # state, so record 3 is accepted (no R008 raised by the rejected record).
    assert [item["record_no"] for item in accepted] == [2, 3]


def test_pipeline_any_aligned_accepts_the_same_input(tmp_path):
    cpy_path, _, any_path, data_path = _write_pipeline_fixtures(tmp_path)
    options = RunOptions(input_path=data_path, copybook_path=cpy_path,
                         rules_path=any_path, out_root=tmp_path / "runs-any",
                         db_path=tmp_path / "ledger-any.sqlite", now=NOW)
    result = run(options)
    # any_aligned accepts record 1; its KEY=X then commits, so record 3 is the
    # only rejection, on the unique-key rule rather than the cross-field rule.
    assert result.status == "completed_with_rejects"
    assert result.reject_codes == {"R008_UNIQUE": 1}
    run_dir = tmp_path / "runs-any" / result.run_id
    rejects = [json.loads(line) for line in
               (run_dir / "rejects.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [entry["record_no"] for entry in rejects] == [3]
    assert rejects[0]["codes"] == ["R008_UNIQUE"]
    accepted = [json.loads(line) for line in
                (run_dir / "accepted.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [entry["record_no"] for entry in accepted] == [1, 2]


def test_pipeline_rerun_is_byte_stable_for_review_and_reprocess(tmp_path):
    cpy_path, all_path, _, data_path = _write_pipeline_fixtures(tmp_path)
    common = dict(input_path=data_path, copybook_path=cpy_path, rules_path=all_path,
                  out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite", now=NOW)
    first = run(RunOptions(**common))
    run_dir = tmp_path / "runs" / first.run_id
    before = {name: (run_dir / filename).read_bytes()
              for name, filename in (("rejects", "rejects.jsonl"),
                                     ("accepted", "accepted.jsonl"),
                                     ("summary", "summary.json"))}
    second = run(RunOptions(**common))
    assert second.idempotent and second.run_id == first.run_id
    assert {name: (run_dir / filename).read_bytes()
            for name, filename in (("rejects", "rejects.jsonl"),
                                   ("accepted", "accepted.jsonl"),
                                   ("summary", "summary.json"))} == before
