"""Offline rule-certification verdicts: SAT witnesses, UNSAT chains and UNKNOWN.

The certify command reasons about what raw bytes a copybook can actually encode.
A schema-valid YAML contract may still admit no record; these tests pin the
honest three-way verdict contract:

* SAT is only reported for a record-length byte witness that survives the real
  ``decode_record`` + ``RuleEngine.validate_record`` round trip.
* UNSAT needs an auditable chain built from encodable domains and supported
  clauses; random samples missing a value can never produce UNSAT.
* Everything outside the reasoned fragment, and every exhausted witness
  budget, is UNKNOWN.
"""

from __future__ import annotations

import json

from conftest import FIXTURES, cpy
from copybook_kit.copybook.layout import compile_file
from copybook_kit.model import Diagnostic
from copybook_kit.rules.certify import (REASON_BUDGET, REASON_OCCURS, REASON_PATTERN_OPEN,
                                        REASON_REDEFINES, REASON_WITNESS_UNSAT, REASON_VERIFY,
                                        certify, verify_witness)
from copybook_kit.rules.loader import load_rules
from copybook_kit.util import canonical_json, sha256_bytes
import copybook_kit.cli as cli

CUSTOMER_CPY = FIXTURES / "copybooks" / "customer.cpy"
CUSTOMER_RULES = FIXTURES / "rules" / "customer_rules.yaml"


def write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def cert(tmp_path, copybook_text: str | None, rules_text: str, *, mode: str = "proof", **kwargs):
    rules_path = write(tmp_path, "rules.yaml", rules_text)
    if copybook_text is None:
        layout = compile_file(str(CUSTOMER_CPY))
    else:
        cpy_path = write(tmp_path, "layout.cpy", copybook_text)
        layout = compile_file(str(cpy_path))
    rules = load_rules(rules_path, layout)
    return layout, rules, certify(layout, rules, mode=mode,
                                  copybook_sha256=sha256_bytes(b"cpy"),
                                  rules_sha256=sha256_bytes(rules_text.encode("utf-8")), **kwargs)


# ---------------------------------------------------------------------------
# UNSAT family 1: enum + pattern conflict on a finite character domain
# ---------------------------------------------------------------------------
STATUS_CONFLICT = """version: 1
name: status-conflict
record_type: {field: REC-TYPE, data: D}
fields:
  REC-TYPE: {required: true, enum: ["D"]}
  STATUS:
    required: true
    enum: ["A", "I"]
    pattern: "^Z$"
"""


def test_proof_enum_pattern_conflict_is_unsat_with_chain(tmp_path):
    layout, rules, report = cert(tmp_path, None, STATUS_CONFLICT)
    assert report["verdict"] == "UNSAT"
    sources = [step["source"] for step in report["contradiction_chain"]]
    assert sources == ["layout.STATUS", "fields.STATUS.enum", "fields.STATUS.pattern"]
    assert any("full-matches none" in step["detail"] for step in report["contradiction_chain"])
    assert report["unknown_reasons"] == []


def test_witness_mode_reports_unknown_for_same_enum_pattern_conflict(tmp_path):
    # Same input, different honest scope: witness mode must never certify UNSAT.
    _, _, report = cert(tmp_path, None, STATUS_CONFLICT, mode="witness", budget=5)
    assert report["verdict"] == "UNKNOWN"
    assert REASON_WITNESS_UNSAT in report["unknown_reasons"]
    assert report["search"]["exhausted"] is True
    chain_sources = [step["source"] for step in report["proof_contradiction_chain"]]
    assert "fields.STATUS.pattern" in chain_sources


# ---------------------------------------------------------------------------
# UNSAT family 2: cross-field <= relation over disjoint date windows
# ---------------------------------------------------------------------------
DATE_CONFLICT = """version: 1
name: date-conflict
record_type: {field: REC-TYPE, data: D}
fields:
  REC-TYPE: {required: true, enum: ["D"]}
  OPEN-DATE:
    required: true
    date_format: "%Y%m%d"
    range: {min: "20270101", max: "20271231"}
  RENEW-DATE:
    required: true
    date_format: "%Y%m%d"
    range: {min: "20200101", max: "20201231"}
record_rules:
  - name: open-before-renew
    kind: cross_field
    left: OPEN-DATE
    op: "<="
    right: RENEW-DATE
"""


def test_proof_disjoint_date_windows_violate_cross_relation(tmp_path):
    layout, rules, report = cert(tmp_path, None, DATE_CONFLICT)
    assert report["verdict"] == "UNSAT"
    chain = report["contradiction_chain"]
    assert [step["source"] for step in chain][-1] == "record_rules:open-before-renew"
    joined = " ".join(step["detail"] for step in chain)
    assert "20270101" in joined and "20201231" in joined
    assert "OPEN-DATE <= RENEW-DATE" in joined


# ---------------------------------------------------------------------------
# UNSAT family 3: check digit derived from the encodable domain, not sampling
# ---------------------------------------------------------------------------
CHECK_DIGIT_CONFLICT = """version: 1
name: control-conflict
record_type: {field: REC-TYPE, data: D}
fields:
  REC-TYPE: {required: true, enum: ["D"]}
  CONTROL-NO:
    required: true
    enum: [100000]
    check_digit: {weights: [2, 3, 4, 5, 6], modulus: 11}
"""


def test_proof_check_digit_conflict_derives_expected_digit_nine(tmp_path):
    layout, rules, report = cert(tmp_path, None, CHECK_DIGIT_CONFLICT)
    assert report["verdict"] == "UNSAT"
    detail = " ".join(step["detail"] for step in report["contradiction_chain"])
    # 1*2 + 0*3 + 0*4 + 0*5 + 0*6 = 2; (11 - 2) mod 11 = 9, candidate ends in 0.
    assert "prefix '10000' requires check digit 9" in detail
    assert "ends in '0'" in detail
    assert any("modulus=11" in step["detail"] for step in report["contradiction_chain"])


def test_witness_check_digit_conflict_is_unknown_not_unsat(tmp_path):
    _, _, report = cert(tmp_path, None, CHECK_DIGIT_CONFLICT, mode="witness", budget=10)
    assert report["verdict"] == "UNKNOWN"
    assert report["unknown_reasons"] == [REASON_WITNESS_UNSAT]


# ---------------------------------------------------------------------------
# SAT: full supported fragment including COMP-3, scale, dates and a relation
# ---------------------------------------------------------------------------
SAT_CPY = cpy("01 R.",
              "05 REC-TYPE PIC X(1).",
              "05 REGION PIC X(2).",
              "05 AMOUNT PIC S9(7)V9(2) USAGE COMP-3.",
              "05 RATE PIC 9(3)V9(4).",
              "05 BOOK-DATE PIC 9(8).",
              "05 CONTROL-NO PIC 9(6).")
SAT_RULES = """version: 1
name: sat-case
record_type: {field: REC-TYPE, data: D}
fields:
  REC-TYPE: {required: true, enum: ["D"]}
  REGION: {required: true, enum: ["NA", "EU"]}
  AMOUNT:
    required: true
    range: {min: "-99999.99", max: "99999.99"}
    scale: 2
  RATE:
    required: true
    range: {min: "0", max: "999.9999"}
    scale: 4
  BOOK-DATE:
    required: true
    date_format: "%Y%m%d"
    range: {min: "20260101", max: "20261231"}
  CONTROL-NO:
    required: true
    check_digit: {weights: [2, 3, 4, 5, 6], modulus: 11}
record_rules:
  - name: rate-within-control
    kind: cross_field
    left: RATE
    op: "<="
    right: CONTROL-NO
"""


def test_sat_witness_is_record_length_and_passes_official_pipeline(tmp_path):
    layout, rules, report = cert(tmp_path, SAT_CPY, SAT_RULES)
    assert report["verdict"] == "SAT"
    witness = report["witness"]
    raw = bytes.fromhex(witness["raw_hex"])
    assert len(raw) == layout.record_length == witness["length"]
    assert witness["raw_sha256"] == sha256_bytes(raw)
    # The SAT evidence is the official decoder plus the rule engine, not a dict.
    assert witness["decode_record"] == {"errors": []}
    assert witness["rule_engine"] == {"hits": [], "accepted": True}
    check = verify_witness(layout, rules, "utf-8", raw)
    assert check["ok"] and check["record_type"] == "D"
    # COMP-3 and decimal scale really round-trip through the layout.
    values = witness["decoded_values"]
    assert values["AMOUNT"] == "-99999.99"
    assert values["RATE"].split(".")[1] == "0000"
    assert values["BOOK-DATE"] == "20260101"
    # Check digit of the chosen control number is genuinely valid mod 11.
    control = values["CONTROL-NO"]
    total = sum(int(d) * w for d, w in zip(control[:-1], (2, 3, 4, 5, 6)))
    assert int(control[-1]) == (11 - total % 11) % 11
    # Offsets and per-field evidence are part of the report.
    rows = {(row["path"]): row for row in witness["fields"]}
    assert rows["R.AMOUNT"]["offset"] == 3 and rows["R.AMOUNT"]["length"] == 5
    assert rows["R.AMOUNT"]["evidence_sha8"] == sha8_of(raw, rows["R.AMOUNT"])
    assert report["coverage"]["abandoned"] == []


def sha8_of(raw: bytes, row) -> str:
    from copybook_kit.util import sha8
    return sha8(raw[row["offset"]:row["offset"] + row["length"]])


# ---------------------------------------------------------------------------
# UNKNOWN: arbitrary pattern over an open domain cannot be fully reasoned
# ---------------------------------------------------------------------------
OPEN_PATTERN_RULES = """version: 1
name: open-pattern
record_type: {field: REC-TYPE, data: D}
fields:
  REC-TYPE: {required: true, enum: ["D"]}
  CUSTOMER-ID:
    required: true
    pattern: "^CUST[0-9]{6}$"
"""


def test_open_pattern_is_unknown_in_proof_and_budget_unknown_in_witness(tmp_path):
    _, _, proof = cert(tmp_path, None, OPEN_PATTERN_RULES, mode="proof")
    assert proof["verdict"] == "UNKNOWN"
    assert proof["unknown_reasons"] == [REASON_PATTERN_OPEN]
    abandoned = {item["source"]: item["reason"] for item in proof["coverage"]["abandoned"]}
    assert abandoned["fields.CUSTOMER-ID.pattern"] == REASON_PATTERN_OPEN
    _, _, witness = cert(tmp_path, None, OPEN_PATTERN_RULES, mode="witness", budget=4)
    assert witness["verdict"] == "UNKNOWN"
    assert witness["unknown_reasons"] == [REASON_BUDGET]
    assert witness["search"] == {"budget": 4, "attempts": 4, "exhausted": True}


def test_customer_fixture_contract_is_unknown_never_unsat():
    layout = compile_file(str(CUSTOMER_CPY))
    rules = load_rules(CUSTOMER_RULES, layout)
    report = certify(layout, rules, mode="proof")
    assert report["verdict"] == "UNKNOWN"
    reasons = set(report["unknown_reasons"])
    assert REASON_PATTERN_OPEN in reasons
    assert report.get("contradiction_chain") is None


# ---------------------------------------------------------------------------
# UNKNOWN: REDEFINES overlap and OCCURS multi-value references
# ---------------------------------------------------------------------------
def test_redefines_and_occurs_are_conservatively_unknown():
    layout = compile_file(str(FIXTURES / "copybooks" / "account.cpy"))
    rules = load_rules(FIXTURES / "rules" / "account_rules.yaml", layout)
    report = certify(layout, rules, mode="proof")
    assert report["verdict"] == "UNKNOWN"
    reasons = {item["reason"] for item in report["coverage"]["abandoned"]}
    assert REASON_REDEFINES in reasons and REASON_OCCURS in reasons


def test_independent_supported_contradiction_still_wins_over_redefines(tmp_path):
    layout = compile_file(str(FIXTURES / "copybooks" / "account.cpy"))
    path = write(tmp_path, "rules.yaml", """version: 1
name: acct-unsat
fields:
  SCORE:
    required: true
    enum: ["NOT-A-NUMBER"]
""")
    report = certify(layout, load_rules(path, layout), mode="proof")
    assert report["verdict"] == "UNSAT"
    assert any(step["source"] == "fields.SCORE.enum" for step in report["contradiction_chain"])


# ---------------------------------------------------------------------------
# Anti-forgery: a witness that fails official re-decode can never be SAT
# ---------------------------------------------------------------------------
def test_witness_redecode_failure_is_unknown_not_sat(tmp_path, monkeypatch):
    layout, rules, report = cert(tmp_path, SAT_CPY, SAT_RULES)
    assert report["verdict"] == "SAT"

    import copybook_kit.rules.certify as certify_mod

    def forged_verify(a_layout, a_rules, charset, raw):
        result = verify_witness(a_layout, a_rules, charset, raw)
        result["ok"] = False
        result["errors"] = [Diagnostic("RD002", "forged re-decode failure").to_dict()]
        return result

    monkeypatch.setattr(certify_mod, "verify_witness", forged_verify)
    report2 = certify(layout, rules, mode="proof")
    assert report2["verdict"] == "UNKNOWN"
    assert report2["unknown_reasons"] == [REASON_VERIFY]
    assert report2["unknown_detail"]["stage"] == "decode+rules"
    assert report2["unknown_detail"]["decode_errors"][0]["code"] == "RD002"


# ---------------------------------------------------------------------------
# Blank policy and unencodable values cannot be faked into SAT
# ---------------------------------------------------------------------------
BLANK_CPY = cpy("01 R.", "05 N PIC 9(2).", "05 K PIC X(2).")
BLANK_RULES = """version: 1
name: blank-policy
fields:
  N: {blank: error}
  K: {enum: ["OK"]}
"""


def test_blank_error_policy_still_produces_decodable_sat_witness(tmp_path):
    layout, rules, report = cert(tmp_path, BLANK_CPY, BLANK_RULES)
    assert report["verdict"] == "SAT"
    raw = bytes.fromhex(report["witness"]["raw_hex"])
    assert len(raw) == layout.record_length
    check = verify_witness(layout, rules, "utf-8", raw)
    assert check["ok"] and check["values"]["N"] == "0" and check["values"]["K"] == "OK"


WIDE_RULES = """version: 1
name: unencodable
fields:
  K:
    required: true
    enum: ["長"]
"""
WIDE_CPY = cpy("01 R.", "05 K PIC X(1).")


def test_unencodable_enum_is_unsat_under_utf8(tmp_path):
    layout, rules, report = cert(tmp_path, WIDE_CPY, WIDE_RULES)
    assert report["verdict"] == "UNSAT"
    assert any(step["source"] == "fields.K.enum" for step in report["contradiction_chain"])


# ---------------------------------------------------------------------------
# DISPLAY decimal grid, scale and strict < cross relation
# ---------------------------------------------------------------------------
DECIMAL_CPY = cpy("01 R.", "05 A PIC 9(3)V9(2).", "05 B PIC 9(3)V9(2).")


def test_display_decimal_strict_relation_sat_and_unsat(tmp_path):
    sat_rules = """version: 1
name: dec
fields:
  A: {required: true, range: {min: "1.00", max: "9.99"}, scale: 2}
  B: {required: true, range: {min: "1.00", max: "9.99"}, scale: 2}
record_rules:
  - name: a-lt-b
    kind: cross_field
    left: A
    op: "<"
    right: B
"""
    layout, rules, report = cert(tmp_path, DECIMAL_CPY, sat_rules)
    assert report["verdict"] == "SAT"
    raw = bytes.fromhex(report["witness"]["raw_hex"])
    check = verify_witness(layout, rules, "utf-8", raw)
    assert check["ok"] and check["values"] == {"A": "1.00", "B": "1.01"}

    unsat_rules = """version: 1
name: dec2
fields:
  A: {required: true, range: {min: "8.00", max: "9.99"}, scale: 2}
  B: {required: true, range: {min: "1.00", max: "7.99"}, scale: 2}
record_rules:
  - name: a-lt-b
    kind: cross_field
    left: A
    op: "<"
    right: B
"""
    _, _, report2 = cert(tmp_path, DECIMAL_CPY, unsat_rules)
    assert report2["verdict"] == "UNSAT"
    assert report2["contradiction_chain"][-1]["source"] == "record_rules:a-lt-b"


def test_decimal_grid_bound_between_representable_points_is_unsat(tmp_path):
    grid_rules = """version: 1
name: grid
fields:
  A: {required: true, range: {min: "1.005", max: "1.005"}, scale: 2}
"""
    _, _, report = cert(tmp_path, DECIMAL_CPY, grid_rules)
    assert report["verdict"] == "UNSAT"
    assert any("intersects it at [101, 100]" in step["detail"]
               for step in report["contradiction_chain"])


def test_overpunch_field_with_blank_error_policy_is_unsat(tmp_path):
    overpunch = cpy("01 R.", "05 S PIC S9(3).", "05 N PIC 9(2).")
    rules = """version: 1
name: overpunch
fields:
  S: {blank: error}
"""
    _, _, report = cert(tmp_path, overpunch, rules)
    assert report["verdict"] == "UNSAT"
    assert any(step["source"] == "fields.S.blank" for step in report["contradiction_chain"])


def test_check_digit_interval_enumeration_finds_sat_witness(tmp_path):
    cpy_text = cpy("01 R.", "05 CONTROL-NO PIC 9(6).")
    rules = """version: 1
name: cd
fields:
  CONTROL-NO:
    required: true
    check_digit: {weights: [2, 3, 4, 5, 6], modulus: 11}
"""
    layout, ruleset, report = cert(tmp_path, cpy_text, rules)
    assert report["verdict"] == "SAT"
    raw = bytes.fromhex(report["witness"]["raw_hex"])
    check = verify_witness(layout, ruleset, "utf-8", raw)
    assert check["ok"] and len(check["values"]["CONTROL-NO"]) == 6


# ---------------------------------------------------------------------------
# Byte-stable report and CLI wiring
# ---------------------------------------------------------------------------
def test_report_is_byte_stable_for_identical_inputs(tmp_path):
    layout, rules, first = cert(tmp_path, SAT_CPY, SAT_RULES, seed=7, budget=13)
    second = certify(layout, rules, mode="proof", seed=7, budget=13,
                     copybook_sha256=first["copybook_sha256"], rules_sha256=first["rules_sha256"])
    assert canonical_json(first) == canonical_json(second)


def test_cli_certify_rules_exit_codes_and_report_hashes(tmp_path, capsys):
    cpy_path = write(tmp_path, "sat.cpy", SAT_CPY)
    rules_path = write(tmp_path, "sat.yaml", SAT_RULES)
    code = cli.main(["certify-rules", "--copybook", str(cpy_path),
                     "--rules", str(rules_path), "--mode", "proof"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["verdict"] == "SAT" and payload["ok"] is True
    assert payload["copybook_sha256"] == sha256_bytes(cpy_path.read_bytes())
    assert payload["rules_sha256"] == sha256_bytes(rules_path.read_bytes())

    bad_rules = write(tmp_path, "bad.yaml", STATUS_CONFLICT)
    code = cli.main(["certify-rules", "--copybook", str(CUSTOMER_CPY),
                     "--rules", str(bad_rules), "--mode", "proof"])
    assert code == 1 and json.loads(capsys.readouterr().out)["verdict"] == "UNSAT"

    code = cli.main(["certify-rules", "--copybook", str(CUSTOMER_CPY),
                     "--rules", str(CUSTOMER_RULES), "--mode", "witness", "--budget", "2"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and payload["verdict"] == "UNKNOWN" and payload["search"]["exhausted"] is True


def test_cli_certify_rules_validates_budget(tmp_path, capsys):
    rules_path = write(tmp_path, "sat.yaml", SAT_RULES)
    cpy_path = write(tmp_path, "sat.cpy", SAT_CPY)
    code = cli.main(["certify-rules", "--copybook", str(cpy_path), "--rules", str(rules_path),
                     "--budget", "0"])
    assert code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "CLI001" and "budget" in payload["message"]
