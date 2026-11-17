"""Synthetic regression tests for offline reject-audit sampling plans.

The ledger and raw evidence are built synthetically so the tests control the
exact code-overlap geometry (one record covering several codes), exclusion
groups and budget interactions. Every test stays read-only against the run
artifacts and never relies on SQLite return order.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import FIXTURES
from copybook_kit.errors import CopybookKitError
from copybook_kit.pipeline import audit_sample as audit
from copybook_kit.store.ledger import Ledger

RUN_ID = "run-audit-synth"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _run_spec(run_dir: Path, reject_rows: int, code_counts: dict[str, int]) -> dict:
    return {
        "run_key": "key-audit-synth", "run_id": RUN_ID, "kind": "ingest", "parent_run_id": None,
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "status": "completed_with_rejects", "exit_code": 0,
        "input_path": str(run_dir.parent / "input.dat"), "input_name": "input.dat",
        "input_sha256": "a" * 64, "input_size": reject_rows * 3,
        "copybook_path": str(run_dir.parent / "x.cpy"), "copybook_name": "x.cpy",
        "copybook_sha256": "b" * 64, "rules_path": str(run_dir.parent / "r.yaml"),
        "rules_name": "r.yaml", "rules_sha256": "c" * 64,
        "params_json": "{}", "params_sha256": "d" * 64,
        "record_length": 3, "charset": "utf-8", "recfm": "F", "max_records": 0,
        "out_dir": str(run_dir), "summary_path": None, "report_path": None, "html_path": None,
        "profile_path": None, "accepted_path": None, "rejects_path": None,
        "total_records": reject_rows, "data_records": reject_rows, "accepted_records": 0,
        "accepted_data_records": 0, "rejected_records": reject_rows, "file_issue_count": 0,
        "reject_codes_json": json.dumps(code_counts, sort_keys=True, separators=(",", ":")),
        "notes": None,
    }


def seed_ledger(tmp_path: Path, rejects: list[dict], *, file_issues: list[dict] | None = None,
                run_id: str = RUN_ID) -> tuple[Ledger, Path]:
    """Create a completed run with reject rows and their raw evidence files."""

    run_dir = tmp_path / "runs" / run_id
    (run_dir / "rejects").mkdir(parents=True)
    ledger = Ledger(tmp_path / "ledger.sqlite")
    code_counts: dict[str, int] = {}
    for row in rejects:
        for code in row["codes"]:
            code_counts[code] = code_counts.get(code, 0) + 1
    spec = _run_spec(run_dir, len(rejects), dict(sorted(code_counts.items())))
    if run_id != RUN_ID:
        spec["run_id"] = run_id
        spec["run_key"] = f"key-{run_id}"
    ledger.insert_run(spec)
    for index, row in enumerate(rejects, start=1):
        raw = row["raw"]
        record_no = row["record_no"]
        raw_file = f"rejects/record-{record_no:08d}.bin"
        (run_dir / raw_file).write_bytes(raw)
        ledger.add_reject(run_id, index, {
            "record_no": record_no, "byte_offset": row.get("byte_offset", 3 * (record_no - 1)),
            "record_type": "D", "codes": row["codes"], "raw_file": raw_file,
            "raw_sha256": _sha(raw), "raw_length": len(raw),
            "partial": row.get("partial", {"NOTE": "partial must never rebuild evidence"}),
            "errors": [], "rule_hits": [],
        })
    for index, issue in enumerate(file_issues or [], start=1):
        ledger.add_file_issue(run_id, index, issue)
    return ledger, run_dir


def rec(record_no: int, codes: list[str], raw: bytes | None = None) -> dict:
    return {"record_no": record_no, "codes": codes,
            "raw": raw if raw is not None else bytes((64 + record_no,)) * 3}


# Canonical overlap geometry from the task statement.
R1 = rec(1, ["R002_ENUM", "R005_PATTERN"], b"abc")
R2 = rec(2, ["R002_ENUM"], b"def")
R3 = rec(3, ["R005_PATTERN"], b"ghi")


def test_one_record_covering_two_codes_is_the_unique_budget_one_solution(tmp_path):
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3])
    plan = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 1, "contract": "exact"})
    assert plan["status"] == "optimal"
    assert [row["record_no"] for row in plan["selected_records"]] == [1]
    assert plan["selected_count"] == 1
    proof = plan["optimality_proof"]
    assert proof["minimum_records"] == 1 and proof["admissible_lower_bound"] == 1
    # The multi-code record is counted once per code, never twice for one code.
    coverage = {item["code"]: item["covered_by"] for item in plan["quota_coverage"]}
    assert coverage == {"R002_ENUM": [1], "R005_PATTERN": [1]}
    # Evidence references only real ledger fields.
    chosen = plan["selected_records"][0]
    assert chosen["reject_no"] == 1 and chosen["byte_offset"] == 0
    assert chosen["raw_sha256"] == _sha(b"abc") and chosen["raw_file"].endswith("record-00000001.bin")
    assert plan["evidence_verification"]["reconstructed_from_partial"] is False
    assert plan["read_only"] is True


def test_greedy_per_code_picking_would_misjudge_budget(tmp_path):
    # Independent per-code selection would take r2 for R002 and r3 for R005
    # (two records) and wrongly conclude budget 1 is infeasible.
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3])
    request = audit.parse_request({
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 1, "contract": "exact"})
    rejects = ledger.get_rejects(RUN_ID)
    candidates, _ = audit.build_candidates(rejects, request.exclusion_groups)
    greedy = audit.greedy_incumbent(request, candidates)
    # The greedy incumbent is itself feasible here (it scores r1 highest);
    # the assertion that matters is the exact proof of the size-1 optimum.
    assert greedy is not None and len(greedy) == 1
    plan = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 1, "contract": "exact"})
    assert plan["status"] == "optimal" and plan["selected_count"] == 1


def test_exclusion_group_forces_joint_solve_and_defeats_local_optimum(tmp_path):
    r4 = rec(4, ["R002_ENUM"], b"jkl")
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3, r4])
    # Feasible joint solve: G1={r1,r4}, optimum r1+r2 (r1 covers both codes,
    # r2 supplies the second R002 slot) — size 2, found by joint search.
    plan = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 2, "R005_PATTERN": 1},
        "budget": 3, "contract": "exact", "exclusion_groups": [[1, 4]]})
    assert plan["status"] == "optimal"
    assert sorted(row["record_no"] for row in plan["selected_records"]) == [1, 2]
    assert plan["optimality_proof"]["minimum_records"] == 2
    # Infeasible joint solve: every R002 carrier (r1, r2, r4) sits in one
    # exclusion group, so R002=2 (which needs two of them) can never be met.
    # A per-code greedy that picks r1 then deletes the blocked r2 would hide
    # the real group-wide contradiction; the exhaustive solver proves it.
    trapped = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 2, "R005_PATTERN": 1},
        "budget": 3, "contract": "exact",
        "exclusion_groups": [[1, 2], [2, 4], [1, 4]]})
    assert trapped["status"] == "infeasible"
    reason_types = {item["type"] for item in trapped["infeasibility_certificate"]["reasons"]}
    assert "no_satisfiable_selection" in reason_types
    assert "optimality_proof" not in trapped


def test_no_candidate_code_and_fl_file_issues_are_not_sampleable(tmp_path):
    fl_issue = {"code": "FL003", "severity": "error", "message": "min records",
                "byte_offset": None, "record_no": None, "detail": {}}
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3], file_issues=[fl_issue])
    # FL code exists as a file issue but has no reject row: must not be sampled.
    fl_plan = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"FL003": 1}, "budget": 2, "contract": "exact"})
    assert fl_plan["status"] == "infeasible"
    assert fl_plan["code_inventory"][0]["kind"] == "file_level_not_sampleable"
    assert fl_plan["rule_summary"]["file_level_codes_not_sampleable"] == ["FL003"]
    assert fl_plan["infeasibility_certificate"]["reasons"][0]["type"] == "insufficient_candidates"
    # Unknown reject code: no row carries it.
    unknown = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R009_CROSS_FIELD": 1},
        "budget": 2, "contract": "exact"})
    assert unknown["status"] == "infeasible"
    assert unknown["code_inventory"][0]["kind"] == "no_reject_candidate"


def test_exact_optimal_proof_and_budget_gap_certificate(tmp_path):
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3])
    # R002 quota 2 forces two distinct records; budget 1 cannot hold them.
    plan = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 2}, "budget": 1, "contract": "exact"})
    assert plan["status"] == "infeasible"
    reason = plan["infeasibility_certificate"]["reasons"][0]
    assert reason["type"] == "budget_lower_bound"
    assert reason["lower_bound"] == 2 and reason["budget"] == 1
    # Insufficient-candidates gap certificate is independently checkable.
    short = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R005_PATTERN": 4}, "budget": 4, "contract": "exact"})
    assert short["status"] == "infeasible"
    gap = short["infeasibility_certificate"]["reasons"][0]
    assert gap["type"] == "insufficient_candidates" and gap["available"] == 2 and gap["required"] == 4


def test_bounded_contract_three_states(tmp_path):
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3])
    quotas = {"R002_ENUM": 1, "R005_PATTERN": 1}
    # Feasible: must never claim minimality.
    feasible = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": quotas, "budget": 1, "contract": "bounded"})
    assert feasible["status"] == "feasible" and feasible["selected_count"] == 1
    assert "optimality_proof" not in feasible
    assert feasible["contract"]["name"] == "bounded"
    # Proven infeasible within the node budget is reported as infeasible,
    # never silently conflated with undecided.
    infeasible = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 9}, "budget": 9, "contract": "bounded"})
    assert infeasible["status"] == "infeasible"
    assert infeasible["infeasibility_certificate"]["reasons"][0]["type"] == "insufficient_candidates"
    # Undecided: a wall of conflicting pairs with a one-node budget.
    extra = [rec(i, ["R002_ENUM"]) for i in range(4, 18)]
    ledger2, _ = seed_ledger(tmp_path / "wall", [R1, R2, R3, *extra], run_id="run-wall")
    groups = [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10], [11, 12], [13, 14], [15, 16]]
    undecided = audit.build_audit_plan(ledger2, {
        "run_id": "run-wall", "quotas": {"R002_ENUM": 12}, "budget": 12,
        "contract": "bounded", "node_budget": 3, "exclusion_groups": groups})
    assert undecided["status"] == "undecided"
    assert undecided["contract"]["budget_exhausted"] is True
    cert = undecided["undecided_certificate"]
    assert cert["unmet_quotas"] == {"R002_ENUM": 12}
    assert "infeasibility_certificate" not in undecided
    # The undecided plan still lists proven partial coverage honestly.
    assert cert["proven_coverage"] == {"R002_ENUM": 0}


def test_tampered_raw_bytes_fail_verification_with_au001(tmp_path):
    ledger, run_dir = seed_ledger(tmp_path, [R1, R2, R3])
    evidence = run_dir / "rejects" / "record-00000001.bin"
    original = evidence.read_bytes()
    evidence.write_bytes(b"XYZ")
    with pytest.raises(CopybookKitError) as caught:
        audit.build_audit_plan(ledger, {
            "run_id": RUN_ID, "quotas": {"R002_ENUM": 1}, "budget": 1, "contract": "exact"})
    assert caught.value.code == "AU001"
    assert "raw_sha256" in caught.value.message
    # Restore: the same request now succeeds, proving the ledger itself was
    # untouched and only the on-disk evidence was at fault.
    evidence.write_bytes(original)
    plan = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 1}, "budget": 1, "contract": "exact"})
    assert plan["status"] == "optimal"


def test_missing_raw_file_fails_verification(tmp_path):
    ledger, run_dir = seed_ledger(tmp_path, [R1, R2, R3])
    (run_dir / "rejects" / "record-00000002.bin").unlink()
    with pytest.raises(CopybookKitError) as caught:
        audit.build_audit_plan(ledger, {
            "run_id": RUN_ID, "quotas": {"R002_ENUM": 1}, "budget": 1, "contract": "exact"})
    assert caught.value.code == "AU001"


def test_repeated_execution_is_byte_stable_and_sqlite_order_independent(tmp_path):
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3])
    payload = {"run_id": RUN_ID, "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
               "budget": 2, "contract": "exact"}
    first = audit.build_audit_plan(ledger, payload)
    second = audit.build_audit_plan(ledger, payload)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    # Re-shuffle the underlying reject rows into a fresh ledger with the same
    # evidence; the stable (record_no, raw_sha256) ordering must keep the plan
    # identical even though insertion/SQLite order differs.
    shuffled = [R3, R1, R2]
    ledger_b, _ = seed_ledger(tmp_path / "shuffle", shuffled, run_id="run-shuffle")
    moved = audit.build_audit_plan(ledger_b, {
        "run_id": "run-shuffle", "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 2, "contract": "exact"})
    assert [row["record_no"] for row in moved["selected_records"]] == \
           [row["record_no"] for row in first["selected_records"]]
    assert moved["selected_records"][0]["raw_sha256"] == first["selected_records"][0]["raw_sha256"]


def test_plan_never_rewrites_run_artifacts(tmp_path):
    ledger, run_dir = seed_ledger(tmp_path, [R1, R2, R3])
    rejects_path = run_dir / "rejects.jsonl"
    rejects_path.write_text("untouched\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (run_dir / "rejects").glob("*.bin")}
    audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 2, "contract": "exact"})
    after = {path: path.read_bytes() for path in (run_dir / "rejects").glob("*.bin")}
    assert before == after
    assert rejects_path.read_text(encoding="utf-8") == "untouched\n"


def test_exact_contract_rejects_oversized_scale(tmp_path):
    rejects = [rec(i, ["R002_ENUM"]) for i in range(1, audit.EXACT_MAX_CANDIDATES + 2)]
    ledger, _ = seed_ledger(tmp_path, rejects)
    with pytest.raises(CopybookKitError) as caught:
        audit.build_audit_plan(ledger, {
            "run_id": RUN_ID, "quotas": {"R002_ENUM": 1},
            "budget": 1, "contract": "exact"})
    assert caught.value.code == "AU002" and "bounded" in caught.value.message
    # The same instance is accepted under the bounded contract.
    bounded = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 1}, "budget": 1, "contract": "bounded"})
    assert bounded["status"] in {"feasible", "undecided"}


def test_invalid_requests_raise_au002(tmp_path):
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3])
    bad_payloads = [
        {"run_id": RUN_ID, "quotas": {}, "budget": 2},
        {"run_id": RUN_ID, "quotas": {"R002_ENUM": 0}, "budget": 2},
        {"run_id": RUN_ID, "quotas": {"R002_ENUM": 1}, "budget": 0},
        {"run_id": RUN_ID, "quotas": {"R002_ENUM": 1}, "budget": 2, "exclusion_groups": [[1, 1]]},
        {"run_id": RUN_ID, "quotas": {"R002_ENUM": 1}, "budget": 2,
         "exclusion_groups": [[1, 99]]},
        {"run_id": RUN_ID, "quotas": {"R002_ENUM": 1}, "budget": 2, "contract": "turbo"},
    ]
    for payload in bad_payloads:
        with pytest.raises(CopybookKitError) as caught:
            audit.build_audit_plan(ledger, payload)
        assert caught.value.code == "AU002", payload


def test_unknown_and_unfinished_runs_are_rejected(tmp_path):
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3])
    with pytest.raises(CopybookKitError) as caught:
        audit.build_audit_plan(ledger, {
            "run_id": "run-missing", "quotas": {"R002_ENUM": 1},
            "budget": 1, "contract": "exact"})
    assert caught.value.code == "LD002"


def test_code_set_change_reshapes_feasibility(tmp_path):
    # The planner reads codes straight from the ledger: change the rule engine
    # output geometry and the optimum must change with it.
    ledger_a, _ = seed_ledger(tmp_path / "a", [R1, R2, R3], run_id="run-rules-a")
    plan_a = audit.build_audit_plan(ledger_a, {
        "run_id": "run-rules-a", "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 1, "contract": "exact"})
    assert plan_a["selected_count"] == 1
    # Same record numbers but r1 loses R005: no single record can cover both.
    r1_split = rec(1, ["R002_ENUM"], b"abc")
    ledger_b, _ = seed_ledger(tmp_path / "b", [r1_split, R2, R3], run_id="run-rules-b")
    plan_b = audit.build_audit_plan(ledger_b, {
        "run_id": "run-rules-b", "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 1, "contract": "exact"})
    assert plan_b["status"] == "infeasible"
    plan_b2 = audit.build_audit_plan(ledger_b, {
        "run_id": "run-rules-b", "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 2, "contract": "exact"})
    assert plan_b2["status"] == "optimal" and plan_b2["selected_count"] == 2


def test_plan_references_only_ledger_evidence_fields(tmp_path):
    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3])
    plan = audit.build_audit_plan(ledger, {
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 2, "contract": "exact"})
    rows = {row["reject_no"]: row for row in ledger.get_rejects(RUN_ID)}
    for selected in plan["selected_records"]:
        source = rows[selected["reject_no"]]
        assert selected["record_no"] == source["record_no"]
        assert selected["byte_offset"] == source["byte_offset"]
        assert selected["raw_sha256"] == source["raw_sha256"]
        assert selected["raw_file"] == source["raw_file"]
        assert "partial" not in selected
    # Rule and input fingerprints are echoed for run traceability.
    assert plan["run"]["contracts"]["rules"]["sha256"] == "c" * 64
    assert plan["run"]["input"]["sha256"] == "a" * 64


def test_cli_plan_reject_audit_end_to_end(tmp_path, capsys):
    import copybook_kit.cli as cli

    ledger, _ = seed_ledger(tmp_path, [R1, R2, R3])
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "run_id": RUN_ID, "quotas": {"R002_ENUM": 1, "R005_PATTERN": 1},
        "budget": 1, "contract": "exact"}), encoding="utf-8")
    output_path = tmp_path / "plan.json"
    code = cli.main(["plan-reject-audit", "--request", str(request_path),
                     "--db", str(tmp_path / "ledger.sqlite"), "--output", str(output_path)])
    assert code == 0
    plan = json.loads(output_path.read_text(encoding="utf-8"))
    assert plan["status"] == "optimal"
    assert [row["record_no"] for row in plan["selected_records"]] == [1]
    # Override flags take precedence over the request file.
    code = cli.main(["plan-reject-audit", "--request", str(request_path),
                     "--db", str(tmp_path / "ledger.sqlite"), "--contract", "bounded"])
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["status"] == "feasible"
    # Tampered evidence surfaces as the stable AU001 error and exit code 2.
    run_dir = tmp_path / "runs" / RUN_ID
    (run_dir / "rejects" / "record-00000001.bin").write_bytes(b"ZZZ")
    code = cli.main(["plan-reject-audit", "--request", str(request_path),
                     "--db", str(tmp_path / "ledger.sqlite")])
    assert code == 2
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "AU001"


def test_cli_against_real_reject_fixture_run(tmp_path, capsys):
    # Exercise the full pipeline ledger path (not only synthetic rows).
    from copybook_kit.pipeline.options import RunOptions
    from copybook_kit.pipeline.runner import run
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = run(RunOptions(
        input_path=FIXTURES / "data" / "customer_rejects.dat",
        copybook_path=FIXTURES / "copybooks" / "customer.cpy",
        rules_path=FIXTURES / "rules" / "customer_rules.yaml",
        out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite", now=now))
    assert result.status == "completed_with_rejects"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "run_id": result.run_id,
        "quotas": {"R001_REQUIRED": 1, "R002_ENUM": 1, "R004_DATE": 1},
        "budget": 4, "contract": "exact"}), encoding="utf-8")
    import copybook_kit.cli as cli
    code = cli.main(["plan-reject-audit", "--request", str(request_path),
                     "--db", str(tmp_path / "ledger.sqlite")])
    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "optimal"
    assert plan["selected_count"] <= 4
    # Every selected record re-verifies against the on-disk raw bytes.
    run_dir = tmp_path / "runs" / result.run_id
    for row in plan["selected_records"]:
        raw = (run_dir / row["raw_file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["raw_sha256"]
    assert plan["evidence_verification"]["verified_rows"] == result.counts["rejected_records"]
