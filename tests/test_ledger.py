"""SQLite ledger persistence, queries, checkpoints, rejects and cascade."""

from __future__ import annotations

import pytest

from copybook_kit.errors import LedgerError
from copybook_kit.store.ledger import Ledger

def test_ledger_records_terminal_run_events_checkpoint_and_queries(run_fixture, tmp_path):
    result = run_fixture()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    row = ledger.get_run(result.run_id)
    assert row["status"] == "completed" and row["run_key"] == result.run_key
    assert row["input_name"] == "customer_normal.dat" and row["record_length"] == 86
    assert ledger.get_run_by_key(result.run_key)["run_id"] == result.run_id
    assert ledger.list_runs()[0]["run_id"] == result.run_id
    assert [event["event"] for event in ledger.get_events(result.run_id)] == ["run_started", "run_finished"]
    checkpoint = ledger.get_checkpoint(result.run_id)
    assert checkpoint["next_record_no"] == 8
    assert checkpoint["state"]["counts"]["accepted_records"] == 7
    assert ledger.list_children(result.run_id) == [] and ledger.get_rejects(result.run_id) == []
    assert ledger.get_file_issues(result.run_id) == [] and ledger.get_run("run-missing") is None

def test_ledger_reject_and_file_issue_rows_are_structured(run_fixture, tmp_path):
    rejected = run_fixture("customer_rejects.dat")
    ledger = Ledger(tmp_path / "ledger.sqlite")
    rows = ledger.get_rejects(rejected.run_id)
    assert len(rows) == 4 and rows[0]["record_no"] == 3
    assert rows[0]["codes"] and isinstance(rows[0]["partial"], dict)
    assert rows[0]["raw_sha256"] == rows[0]["raw_sha256"].lower()
    issues = ledger.get_file_issues(run_fixture("customer_truncated.dat").run_id)
    assert {"RD002", "FL001"} <= {item["code"] for item in issues}
    assert all(isinstance(item["detail"], dict) for item in issues)

def test_ledger_delete_cascades_and_contract_errors_are_stable(run_fixture, tmp_path):
    result = run_fixture("customer_rejects.dat")
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.delete_run(result.run_id)
    assert ledger.get_run(result.run_id) is None and ledger.get_rejects(result.run_id) == []
    assert ledger.get_events(result.run_id) == []
    with pytest.raises(LedgerError) as caught:
        ledger.insert_run({})
    assert caught.value.code == "LD003"
