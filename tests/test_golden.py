"""Golden scenario metrics, reject evidence and byte-stable reruns."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import FIXTURES, NOW, load_json
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.pipeline.runner import run

GOLDEN = load_json(FIXTURES / "expected" / "demo.json")

def options(case, out, db):
    return RunOptions(FIXTURES / "data" / case["data"], FIXTURES / "copybooks" / case["copybook"],
                      FIXTURES / "rules" / case["rules"], out, db, NOW)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_scenario(name, tmp_path):
    case = GOLDEN[name]
    result = run(options(case, tmp_path / "runs", tmp_path / "ledger.sqlite"))
    assert result.status == case["status"]
    assert result.counts == case["counts"]
    assert result.reject_codes == case["reject_codes"]
    assert [issue.code for issue in result.file_issues] == case["issues"]
    assert result.exit_code == (3 if case["status"] == "failed" else 0)
    assert result.generated_at == "2026-01-01T00:00:00Z"
    assert result.contract_meta["copybook"]["sha256"] == sha(FIXTURES / "copybooks" / case["copybook"])
    assert result.contract_meta["rules"]["sha256"] == sha(FIXTURES / "rules" / case["rules"])
    summary = load_json(Path(result.outputs["summary"]))
    assert summary["generated_at"] == result.generated_at and summary["counts"] == result.counts

def test_rerun_is_idempotent_and_byte_stable(tmp_path):
    case = GOLDEN["customer_normal"]
    first = run(options(case, tmp_path / "runs", tmp_path / "ledger.sqlite"))
    before = {name: Path(path).read_bytes() for name, path in first.outputs.items()}
    second = run(options(case, tmp_path / "runs", tmp_path / "ledger.sqlite"))
    assert second.idempotent and second.run_id == first.run_id
    assert {name: Path(path).read_bytes() for name, path in second.outputs.items()} == before

def test_reject_evidence_matches_input_offsets(tmp_path):
    case = GOLDEN["customer_rejects"]
    result = run(options(case, tmp_path / "runs", tmp_path / "ledger.sqlite"))
    source = (FIXTURES / "data" / case["data"]).read_bytes()
    entries = [json.loads(line) for line in Path(result.outputs["rejects"]).read_text(encoding="utf-8").splitlines()]
    assert len(entries) == result.counts["rejected_records"]
    for entry in entries:
        start, end = entry["byte_offset"], entry["byte_offset"] + entry["raw_length"]
        assert entry["raw_sha256"] == hashlib.sha256(source[start:end]).hexdigest()
        raw = Path(result.outputs["rejects"]).parent / entry["raw_file"]
        assert raw.read_bytes() == source[start:end]

def test_two_fresh_runs_produce_identical_artifacts(tmp_path):
    case = GOLDEN["customer_truncated"]
    left = run(options(case, tmp_path / "a", tmp_path / "a.sqlite"))
    right = run(options(case, tmp_path / "b", tmp_path / "b.sqlite"))
    for name in ("accepted", "rejects", "layout", "profile", "report", "html"):
        assert Path(left.outputs[name]).read_bytes() == Path(right.outputs[name]).read_bytes()
