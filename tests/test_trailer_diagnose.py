"""Synthetic regressions for the read-only FL002 trailer diagnosis.

Every scenario is built from :mod:`fixtures.generator` customer records; the
diagnosis only ever reads the stored run and its evidence artifacts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import FIXTURES
from copybook_kit.cli import main as cli_main
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.pipeline.runner import run
from copybook_kit.pipeline.trailer_diagnose import (
    DATA_TRUSTED,
    TRAILER_TRUSTED,
    diagnose_run,
)
from copybook_kit.store.ledger import Ledger
from copybook_kit.util import pretty_json
from fixtures.generator import customer_file, customer_record

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
COPYBOOK = FIXTURES / "copybooks" / "customer.cpy"
RULES = FIXTURES / "rules" / "customer_rules.yaml"
RECORD_LENGTH = 86
# BALANCE PIC S9(7)V9(2) COMP-3 lives at offsets 41..45; the sign nibble is
# the low nibble of byte 45 of each 86-byte record.
SIGN_RELATIVE_OFFSET = 45


def _two_clean_records(balance0: str = "50.00", balance1: str = "50.00"):
    r0 = customer_record(0, name="CUSTOMER ALPHA", open_date="20260105", status="A",
                         balance=balance0, renew_date="20261005", tag_count=1)
    r1 = customer_record(1, name="CUSTOMER BETA", open_date="20260210", status="A",
                         balance=balance1, renew_date="20261110", tag_count=1,
                         customer_id="CUST200002")
    assert len(r0) == len(r1) == RECORD_LENGTH
    return r0, r1


def _execute(tmp_path, data, *, rules_path=RULES):
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "input.dat"
    input_path.write_bytes(data)
    result = run(RunOptions(
        input_path=input_path, copybook_path=COPYBOOK, rules_path=rules_path,
        out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite", now=NOW))
    ledger = Ledger(tmp_path / "ledger.sqlite")
    return result, ledger.get_run(result.run_id), ledger


def test_two_equal_amounts_trailer_zero_yields_two_minimum_sets_without_necessary_row(tmp_path):
    r0, r1 = _two_clean_records("50.00", "50.00")
    data = customer_file([r0, r1], ["0.00", "0.00"])  # trailer deliberately wrong: 0.00
    result, row, _ = _execute(tmp_path, data)
    assert result.status == "failed"
    assert [issue.code for issue in result.file_issues] == ["FL002"]

    report = diagnose_run(row)
    assert report["conclusion"] == "conditional_routes_requiring_authority_decision"
    assert report["replay"]["data_record_count"] == 2
    assert report["replay"]["trailer_record_no"] == 4
    assert report["trailer_amounts"] == {
        "declared": "0.00", "computed": "100.00", "difference": "-100.00",
        "trailer_record_no": 4, "trailer_byte_offset": 3 * RECORD_LENGTH,
    }

    trusted = report[TRAILER_TRUSTED]
    assert trusted["conclusion"] == "multiple_minimum_sets"
    assert trusted["search"]["status"] == "exhaustive"
    sets = trusted["minimum_sets"]
    assert [item["record_nos"] for item in sets] == [[2], [3]]
    assert all(item["size"] == 1 and item["matches_trailer"] is True
               and item["recomputed_total"] == "0.00"
               and item["remaining_issue_codes"] == [] for item in sets)
    # Flipping both rows gives -100.00 and is therefore not a feasible cause.
    assert all(item["record_nos"] != [2, 3] for item in sets)
    assert trusted["necessary_records"] == []
    assert trusted["partial_records"] == [2, 3]
    assert trusted["minimality_proof"] == {
        "searched_cardinalities_in_order": True,
        "minimum_cardinality": 1,
        "all_smaller_cardinalities_exhausted": True,
        "all_minimum_sets_reported": True,
    }

    candidates = {(item["record_no"]): item for item in trusted["candidates"]}
    for record_no, byte_offset in ((2, RECORD_LENGTH), (3, 2 * RECORD_LENGTH)):
        candidate = candidates[record_no]
        assert candidate["sign_byte_relative_offset"] == SIGN_RELATIVE_OFFSET
        assert candidate["sign_byte_absolute_offset"] == byte_offset + SIGN_RELATIVE_OFFSET
        assert candidate["original_sign_nibble"] == "C"
        assert candidate["candidate_sign_nibble"] == "D"
        assert candidate["original_amount"] == "50.00"
        assert candidate["candidate_amount"] == "-50.00"
        assert len(candidate["original_record_sha256"]) == 64
        assert len(candidate["candidate_record_sha256"]) == 64
        assert candidate["original_record_sha256"] != candidate["candidate_record_sha256"]

    data_trusted = report[DATA_TRUSTED]
    assert data_trusted["correct_trailer_amount"] == "100.00"
    assert data_trusted["data_row_sign_flips_suggested"] == []
    assert data_trusted["encodable_at_current_width"]["encodable"] is True


def test_data_trusted_route_names_exact_trailer_value(tmp_path):
    r0, r1 = _two_clean_records("50.00", "50.00")
    _, row, _ = _execute(tmp_path, customer_file([r0, r1], ["0.00", "0.00"]))
    report = diagnose_run(row, routes=(DATA_TRUSTED,))
    assert report["conclusion"] == "data_trusted_correction"
    route = report[DATA_TRUSTED]
    assert route["declared_trailer_amount"] == "0.00"
    assert route["correct_trailer_amount"] == "100.00"
    assert route["amount_field"] == "BALANCE"
    assert route["pic_usage"] == "COMP-3" and route["pic_digits"] == 9 and route["pic_scale"] == 2
    assert route["encodable_at_current_width"] == {
        "encodable": True, "scaled_integer": 10000, "packed_byte_length": 5,
        "positive_sign_nibble": "C", "negative_sign_nibble": "D",
    }
    assert route["data_row_sign_flips_suggested"] == []
    assert TRAILER_TRUSTED not in report


def test_unique_minimum_set_when_only_one_row_can_close_the_gap(tmp_path):
    # +100.00 and +50.00 against a +50.00 trailer: the declared total requires a
    # -100.00 change. Flipping the +50.00 row changes the total by -100.00,
    # while flipping the +100.00 row changes it by -200.00, so the second row is
    # the unique minimum set.
    r0, r1 = _two_clean_records("100.00", "50.00")
    _, row, _ = _execute(tmp_path, customer_file([r0, r1], ["0.00", "50.00"]))
    report = diagnose_run(row, routes=(TRAILER_TRUSTED,))
    trusted = report[TRAILER_TRUSTED]
    assert trusted["conclusion"] == "unique_minimum_set"
    assert [item["record_nos"] for item in trusted["minimum_sets"]] == [[3]]
    assert trusted["necessary_records"] == [3]
    assert trusted["partial_records"] == []
    flip = trusted["minimum_sets"][0]["flipped_records"][0]
    assert flip["original_amount"] == "50.00" and flip["candidate_amount"] == "-50.00"
    assert trusted["minimum_sets"][0]["recomputed_total"] == "50.00"
    # The +100 row remains a legal candidate but cannot solve the gap alone.
    assert {item["record_no"] for item in trusted["candidates"]} == {2, 3}


def test_f_sign_nibble_is_never_a_reversible_candidate(tmp_path):
    r0, r1 = _two_clean_records("50.00", "50.00")
    r0_f = bytearray(r0)
    r0_f[SIGN_RELATIVE_OFFSET] = 0x0F  # F positive sign, original C/F provenance ambiguous
    _, row, _ = _execute(tmp_path, customer_file([bytes(r0_f), r1], ["0.00", "0.00"]))
    report = diagnose_run(row, routes=(TRAILER_TRUSTED,))
    trusted = report[TRAILER_TRUSTED]
    excluded = {item["record_no"]: item for item in trusted["excluded_data_records"]}
    assert excluded[2]["reason"] == "ambiguous_f_sign_nibble"
    assert excluded[2]["sign_nibble"] == "F"
    # Only the C-signed second row remains, and its flip alone explains 0.00.
    assert trusted["conclusion"] == "unique_minimum_set"
    assert [item["record_nos"] for item in trusted["minimum_sets"]] == [[3]]

    # When every sign nibble is F there is no reversible candidate at all.
    r1_f = bytearray(r1)
    r1_f[SIGN_RELATIVE_OFFSET] = 0x0F
    _, row_all_f, _ = _execute(tmp_path, customer_file([bytes(r0_f), bytes(r1_f)],
                                                       ["0.00", "0.00"]))
    all_f = diagnose_run(row_all_f, routes=(TRAILER_TRUSTED,))[TRAILER_TRUSTED]
    assert all_f["conclusion"] == "no_feasible_set"
    assert all_f["minimum_sets"] == []
    assert all_f["search"]["status"] == "exhaustive"


def test_flip_that_would_break_range_rules_is_excluded(tmp_path):
    strict_rules = tmp_path / "strict_rules.yaml"
    strict_rules.write_text(
        RULES.read_text(encoding="utf-8").replace('min: "-9999999.99"', 'min: "0.00"'),
        encoding="utf-8")
    r0, r1 = _two_clean_records("50.00", "50.00")
    _, row, _ = _execute(tmp_path, customer_file([r0, r1], ["0.00", "0.00"]),
                         rules_path=strict_rules)
    trusted = diagnose_run(row, routes=(TRAILER_TRUSTED,))[TRAILER_TRUSTED]
    assert trusted["conclusion"] == "no_feasible_set"
    assert {item["reason"] for item in trusted["excluded_data_records"]} == {"flip_breaks_rules"}
    assert all(item["codes"] == ["R003_RANGE"] for item in trusted["excluded_data_records"])
    assert trusted["candidates"] == [] and trusted["necessary_records"] == []


def test_field_level_rejected_rows_are_not_candidates_and_do_not_disappear(tmp_path):
    r0, r1 = _two_clean_records("100.00", "50.00")
    rejected = customer_record(2, name="CUSTOMER GAMMA", open_date="20260315", status="X",
                               balance="0.00", renew_date="20261215", tag_count=1,
                               customer_id="CUST300003")
    # Declared +50.00 against clean rows +100.00/+50.00 (the rejected row carries
    # 0.00): flipping the +50.00 row changes the clean total by -100.00 and closes
    # the gap. The rejected row stays in the file and in the engine's total.
    data = customer_file([r0, r1, rejected], ["0.00", "50.00", "0.00"])
    result, row, _ = _execute(tmp_path, data)
    # The FL002 file-level failure coexists with a field-level rejection.
    assert result.status == "failed"
    assert result.counts["rejected_records"] == 1
    report = diagnose_run(row, routes=(TRAILER_TRUSTED,))
    trusted = report[TRAILER_TRUSTED]
    outcomes = {item["record_no"]: item for item in report["replay"]["data_record_outcomes"]}
    assert outcomes[4]["accepted"] is False
    assert outcomes[4]["rule_hit_codes"] == ["R002_ENUM"]
    assert outcomes[4]["amount"] == "0.00"  # decoded evidence is retained
    excluded = {item["record_no"]: item["reason"]
                for item in trusted["excluded_data_records"]}
    assert excluded == {4: "field_or_record_rejection"}
    # The rejected row is never offered as a flip candidate; the clean row still solves it.
    assert [item["record_nos"] for item in trusted["minimum_sets"]] == [[3]]


def test_insufficient_evidence_when_source_bytes_or_premises_change(tmp_path):
    r0, r1 = _two_clean_records("50.00", "50.00")
    data = customer_file([r0, r1], ["0.00", "0.00"])
    result, row, ledger = _execute(tmp_path, data)
    run_id = row["run_id"]

    # 1. tamper with the recorded input file -> INPUT_CHANGED
    input_path = Path(row["input_path"])
    original = input_path.read_bytes()
    input_path.write_bytes(original[:-1] + bytes([original[-1] ^ 0x01]))
    report = diagnose_run(ledger.get_run(run_id))
    assert report["conclusion"] == "insufficient_evidence"
    assert report["reason_code"] == "INPUT_CHANGED"
    assert report["evidence"]["input"]["matches"] is False
    input_path.write_bytes(original)

    # 2. remove a stored evidence artifact -> EVIDENCE_ARTIFACT_MISSING
    accepted = Path(row["out_dir"]) / "accepted.jsonl"
    backup = accepted.read_bytes()
    accepted.unlink()
    report = diagnose_run(ledger.get_run(run_id))
    assert report["conclusion"] == "insufficient_evidence"
    assert report["reason_code"] == "EVIDENCE_ARTIFACT_MISSING"
    accepted.write_bytes(backup)

    # 3. quarantined reject bytes no longer match the input -> SOURCE_BYTES_CHANGED
    rejected = customer_record(2, name="CUSTOMER GAMMA", open_date="20260315", status="X",
                               balance="70.00", renew_date="20261215", tag_count=1,
                               customer_id="CUST300003")
    reject_data = customer_file([r0, r1, rejected], ["50.00", "50.00", "70.00"])
    _, reject_row, reject_ledger = _execute(tmp_path / "reject", reject_data)
    quarantine = next((Path(reject_row["out_dir"]) / "rejects").glob("record-*.bin"))
    raw = quarantine.read_bytes()
    quarantine.write_bytes(raw[:-1] + bytes([raw[-1] ^ 0xFF]))
    report = diagnose_run(reject_ledger.get_run(reject_row["run_id"]))
    assert report["conclusion"] == "insufficient_evidence"
    assert report["reason_code"] == "SOURCE_BYTES_CHANGED"

    # 4. invalid COMP-3 sign nibble on a data row -> amount cannot be decoded
    bad = bytearray(r0)
    bad[SIGN_RELATIVE_OFFSET] = 0x05
    _, bad_row, _ = _execute(tmp_path / "bad", customer_file([bytes(bad), r1],
                                                             ["0.00", "0.00"]))
    report = diagnose_run(bad_row)
    assert report["conclusion"] == "insufficient_evidence"
    assert report["reason_code"] == "DATA_AMOUNT_UNDECODABLE"

    # 5. trailer record missing entirely
    _, no_trailer_row, _ = _execute(tmp_path / "notrailer", data[:-RECORD_LENGTH])
    report = diagnose_run(no_trailer_row)
    assert report["conclusion"] == "insufficient_evidence"
    assert report["reason_code"] == "TRAILER_MISSING"

    # 6. trailer count disagrees with the number of data records
    wrong_count = bytearray(data)
    trailer_start = 3 * RECORD_LENGTH
    wrong_count[trailer_start + 31:trailer_start + 39] = b"00000009"
    _, count_row, _ = _execute(tmp_path / "count", bytes(wrong_count))
    report = diagnose_run(count_row)
    assert report["conclusion"] == "insufficient_evidence"
    assert report["reason_code"] == "TRAILER_COUNT_MISMATCH"


def test_budget_exhaustion_is_distinct_from_a_proven_no_solution(tmp_path):
    r0, r1 = _two_clean_records("100.00", "50.00")
    _, row, _ = _execute(tmp_path, customer_file([r0, r1], ["0.00", "50.00"]))
    exhausted = diagnose_run(row, routes=(TRAILER_TRUSTED,), node_budget=1)[TRAILER_TRUSTED]
    assert exhausted["conclusion"] == "budget_exhausted"
    assert exhausted["search"]["status"] == "budget_exhausted"
    assert exhausted["search"]["nodes_visited"] > 1
    assert exhausted["minimum_sets"] == []
    # The same input with a proper budget yields the proven unique answer.
    proven = diagnose_run(row, routes=(TRAILER_TRUSTED,))[TRAILER_TRUSTED]
    assert proven["conclusion"] == "unique_minimum_set"
    assert [item["record_nos"] for item in proven["minimum_sets"]] == [[3]]


def test_diagnosis_is_read_only_and_byte_stable(tmp_path):
    r0, r1 = _two_clean_records("50.00", "50.00")
    data = customer_file([r0, r1], ["0.00", "0.00"])
    result, row, ledger = _execute(tmp_path / "run", data)
    run_dir = Path(row["out_dir"])
    before = {path.name: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    input_before = Path(row["input_path"]).read_bytes()
    db_path = tmp_path / "run" / "ledger.sqlite"
    db_bytes = db_path.read_bytes()

    first = pretty_json(diagnose_run(ledger.get_run(result.run_id)))
    second = pretty_json(diagnose_run(ledger.get_run(result.run_id)))
    assert first == second  # byte-stable report for identical inputs
    assert json.loads(first)  # valid JSON

    # The successful report carries the verified evidence for every record.
    report = json.loads(first)
    evidence = report["evidence"]
    assert evidence["input"]["matches"] is True
    assert evidence["copybook"]["matches"] is True and evidence["rules"]["matches"] is True
    assert evidence["record_count"] == 4 and evidence["all_record_bytes_match"] is True
    assert all(item["matches"] is True for item in evidence["records"])
    assert {item["record_type"] for item in evidence["records"]} == {"H", "D", "T"}

    after = {path.name: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    assert after == before  # no run artifact changed
    assert Path(row["input_path"]).read_bytes() == input_before
    assert db_path.read_bytes() == db_bytes  # ledger untouched


def test_cli_diagnose_trailer_outputs_json_and_keeps_run_read_only(tmp_path, capsys):
    r0, r1 = _two_clean_records("50.00", "50.00")
    data = customer_file([r0, r1], ["0.00", "0.00"])
    result, row, _ = _execute(tmp_path / "run", data)
    db = tmp_path / "run" / "ledger.sqlite"
    output_file = tmp_path / "diagnosis.json"
    run_dir = Path(row["out_dir"])
    before = {path.name: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}

    code = cli_main(["diagnose-trailer", "--run-id", result.run_id, "--db", str(db),
                     "--output", str(output_file)])
    assert code == 0
    assert capsys.readouterr().out == ""
    report = json.loads(output_file.read_text(encoding="utf-8"))
    assert report["run_id"] == result.run_id
    assert report[TRAILER_TRUSTED]["conclusion"] == "multiple_minimum_sets"
    assert report[DATA_TRUSTED]["correct_trailer_amount"] == "100.00"

    # Single-route selection commits the conclusion naming and drops the other route.
    code = cli_main(["diagnose-trailer", "--run-id", result.run_id, "--db", str(db),
                     "--route", "data-trusted"])
    assert code == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["conclusion"] == "data_trusted_correction"
    assert DATA_TRUSTED in payload and TRAILER_TRUSTED not in payload

    after = {path.name: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    assert after == before

    # Unknown run id still follows the stable error contract.
    code = cli_main(["diagnose-trailer", "--run-id", "run-missing", "--db", str(db)])
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False and error["code"] == "LD002"


def test_healthy_run_reports_no_fl002_and_remains_read_only(tmp_path):
    r0, r1 = _two_clean_records("50.00", "50.00")
    data = customer_file([r0, r1], ["50.00", "50.00"])  # honest trailer
    result, row, ledger = _execute(tmp_path, data)
    assert result.status == "completed" and result.file_issues == []
    report = diagnose_run(ledger.get_run(result.run_id))
    assert report["conclusion"] == "no_fl002_observed"
    assert TRAILER_TRUSTED not in report and DATA_TRUSTED not in report
    assert report["replay"]["issue_codes"] == []
