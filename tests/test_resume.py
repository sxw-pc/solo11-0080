"""Resume semantics, incomplete-run guards and reprocess lineage."""

from __future__ import annotations

import pytest

from conftest import FIXTURES, NOW
from copybook_kit.errors import LedgerError
from copybook_kit.pipeline.options import RunOptions, prepare_run, run_spec
from copybook_kit.pipeline.runner import resume_run, run
from copybook_kit.store.ledger import Ledger

def options_for(tmp_path, **kwargs):
    return RunOptions(input_path=FIXTURES / "data" / "customer_normal.dat",
                      copybook_path=FIXTURES / "copybooks" / "customer.cpy",
                      rules_path=FIXTURES / "rules" / "customer_rules.yaml",
                      out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite",
                      now=NOW, **kwargs)

def test_resume_terminal_run_is_idempotent(run_fixture, tmp_path):
    first = run_fixture()
    resumed = resume_run(first.run_id, tmp_path / "ledger.sqlite", NOW)
    assert resumed.idempotent and resumed.run_id == first.run_id
    assert resumed.counts == first.counts and resumed.status == "completed"
    with pytest.raises(LedgerError) as caught:
        resume_run("run-missing", tmp_path / "ledger.sqlite", NOW)
    assert caught.value.code == "LD002"

def test_incomplete_run_requires_resume_and_resume_replays_to_completion(tmp_path):
    options = options_for(tmp_path)
    prepared, ledger = prepare_run(options), Ledger(options.db_path)
    run_dir = options.out_root / prepared.run_id
    ledger.insert_run(run_spec(prepared, run_dir))
    ledger.mark_failed(prepared.run_id, "2026-01-01T00:00:00Z", "simulated interruption")
    with pytest.raises(LedgerError) as caught:
        run(options)
    assert caught.value.code == "LD001"
    result = resume_run(prepared.run_id, options.db_path, NOW)
    assert result.status == "completed" and result.counts["accepted_records"] == 7
    assert (run_dir / "summary.json").is_file() and ledger.get_run(prepared.run_id)["status"] == "completed"
    assert "run_resumed" in [event["event"] for event in ledger.get_events(prepared.run_id)]

def test_reprocess_creates_child_run_with_parent_lineage(run_fixture, tmp_path):
    parent = run_fixture()
    child = run_fixture(rules="negative_rules.yaml", parent_run_id=parent.run_id, kind="reprocess")
    assert child.run_id != parent.run_id and child.parent_run_id == parent.run_id
    assert child.status == "failed" and child.exit_code == 3
    ledger = Ledger(tmp_path / "ledger.sqlite")
    assert [row["run_id"] for row in ledger.list_children(parent.run_id)] == [child.run_id]
    assert ledger.get_run(child.run_id)["parent_run_id"] == parent.run_id
    assert ledger.get_run(parent.run_id)["status"] == "completed"
