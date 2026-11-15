"""Fixed-width reading, run outcomes, reject evidence and deterministic artifacts."""

from __future__ import annotations

import json

from conftest import FIXTURES, load_json
from copybook_kit.pipeline.reader import read_records

def codes(items):
    return {item.code for item in items}

def test_reader_split_offsets_truncation_empty_and_guard():
    data = (FIXTURES / "data" / "customer_normal.dat").read_bytes()
    result = read_records(data, 86)
    assert len(result.records) == 7 and result.total_bytes == 602
    assert result.records[1].byte_offset == 86 and len(result.records[1].data) == 86
    assert result.trailing_bytes == b"" and result.file_issues == []
    truncated = read_records((FIXTURES / "data" / "customer_truncated.dat").read_bytes(), 86)
    assert truncated.full_record_count == 6 and len(truncated.records) == 6
    assert len(truncated.trailing_bytes) == 74
    assert truncated.file_issues[0].code == "RD002" and truncated.file_issues[0].byte_offset == 516
    empty = read_records(b"", 86)
    assert empty.records == [] and empty.file_issues[0].code == "RD003"
    guarded = read_records(data, 86, max_records=3)
    assert len(guarded.records) == 3 and guarded.file_issues[0].code == "RD004"
    assert guarded.file_issues[0].detail["remaining_records"] == 4

def test_normal_run_outputs_counts_and_artifacts(run_fixture, tmp_path):
    result = run_fixture()
    assert result.status == "completed" and result.exit_code == 0 and not result.idempotent
    assert result.counts == {"total_records": 7, "data_records": 5, "accepted_records": 7,
                             "accepted_data_records": 5, "rejected_records": 0, "file_issues": 0}
    assert result.reject_codes == {} and result.file_issues == []
    run_dir = tmp_path / "runs" / result.run_id
    assert set(result.outputs) == {"accepted", "rejects", "report", "html", "profile", "layout", "summary"}
    assert all((run_dir / name).is_file() for name in result.outputs.values())
    assert len((run_dir / "accepted.jsonl").read_text(encoding="utf-8").splitlines()) == 7
    assert (run_dir / "rejects.jsonl").read_text(encoding="utf-8") == ""
    summary = load_json(run_dir / "summary.json")
    assert summary["counts"] == result.counts and summary["status"] == "completed"
    assert summary["outputs"] == result.outputs
    assert load_json(run_dir / "layout.json")["record_length"] == 86
    profile = load_json(run_dir / "profile.json")
    assert profile["record_count"] == 5
    assert [item["path"] for item in profile["fields"]] == sorted(item["path"] for item in profile["fields"])
    assert '<meta charset="utf-8">' in (run_dir / "report.html").read_text(encoding="utf-8")

def test_rejecting_run_has_traceable_raw_bytes(run_fixture, tmp_path):
    result = run_fixture("customer_rejects.dat")
    assert result.status == "completed_with_rejects" and result.exit_code == 0
    assert result.counts["rejected_records"] == 4 and result.counts["accepted_records"] == 3
    assert result.reject_codes == {"R001_REQUIRED": 1, "R002_ENUM": 1, "R004_DATE": 1,
                                   "R008_UNIQUE": 1, "R009_CROSS_FIELD": 1}
    run_dir = tmp_path / "runs" / result.run_id
    data = (FIXTURES / "data" / "customer_rejects.dat").read_bytes()
    entries = [json.loads(line) for line in
               (run_dir / "rejects.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [item["record_no"] for item in entries] == [3, 4, 5, 6]
    assert [item["byte_offset"] for item in entries] == [172, 258, 344, 430]
    for entry in entries:
        raw_path = run_dir / entry["raw_file"]
        assert raw_path.read_bytes() == data[entry["byte_offset"]:entry["byte_offset"] + 86]
        assert len(entry["raw_sha256"]) == 64
    assert entries[2]["partial"]["CUSTOMER-NAME"] is None
    assert {"R004_DATE", "R009_CROSS_FIELD"} <= set(entries[3]["codes"])

def test_truncated_and_negative_and_empty_runs_fail_with_codes(run_fixture, tmp_path):
    truncated = run_fixture("customer_truncated.dat")
    assert truncated.status == "failed" and truncated.exit_code == 3
    assert truncated.counts["accepted_records"] == 6
    assert {"RD002", "FL001"} <= codes(truncated.file_issues)
    tail = tmp_path / "runs" / truncated.run_id / "rejects" / "truncated-tail.bin"
    assert tail.is_file() and len(tail.read_bytes()) == 74
    assert truncated.file_issues[0].detail["raw_file"] == "rejects/truncated-tail.bin"
    negative = run_fixture("customer_normal.dat", rules="negative_rules.yaml")
    assert negative.status == "failed" and {"FL003"} <= codes(negative.file_issues)
    empty = run_fixture("empty.dat")
    assert empty.status == "failed" and {"RD003", "FL001", "FL003"} <= codes(empty.file_issues)

def test_max_records_guard_commits_prefix_without_false_trailer_failure(run_fixture):
    result = run_fixture(max_records=3)
    assert result.status == "completed" and result.exit_code == 0
    assert result.counts["total_records"] == 3 and result.counts["data_records"] == 2
    assert codes(result.file_issues) == {"RD004"} and result.file_issues[0].severity == "warning"

def test_idempotent_and_force_reruns_are_byte_stable(run_fixture, tmp_path):
    first = run_fixture()
    run_dir = tmp_path / "runs" / first.run_id
    names = ("summary.json", "accepted.jsonl", "report.html", "profile.json")
    before = {name: (run_dir / name).read_bytes() for name in names}
    second = run_fixture()
    assert second.idempotent and second.run_id == first.run_id and second.counts == first.counts
    forced = run_fixture(force=True)
    assert not forced.idempotent and forced.run_id == first.run_id
    assert before == {name: (run_dir / name).read_bytes() for name in names}
