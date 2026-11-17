"""Configurable unique-key conflict adjudication.

Two business contracts are pinned here:

* ``sequential`` (default) keeps the historical "first accepted record wins"
  acceptance semantics byte for byte.
* ``prefer_combination`` is explicitly authorized, scores contended records by a
  decoded ``SCORE`` field, and publishes a component only after an exhaustive
  proof that no compatible set has a higher total.  Unknown proofs (component too
  large, budget exhausted) stay undetermined and never masquerade as optimal.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import FIXTURES, NOW, load_json
from copybook_kit.model import Record
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.pipeline.runner import run
from copybook_kit.rules.checks import canonical_unique_keys
from copybook_kit.rules.engine import (PREFER_COMBINATION_POLICY, resolve_unique_selection)
from copybook_kit.rules.schema import FieldRule, RecordRule, RuleSet

def _load_generator():
    spec = importlib.util.spec_from_file_location("fixture_generator",
                                                  FIXTURES / "generator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_generator = _load_generator()
ACCOUNT_COPYBOOK = FIXTURES / "copybooks" / "account.cpy"

RULES_YAML = """\
version: 1
name: account-unique-two-keys
fields:
  REGION:
    required: true
    enum: ["NA", "EU", "AP"]
  ACCOUNT-NO:
    required: true
    pattern: "^[0-9]{8}$"
  SCORE:
    required: true
    range: {min: "0", max: "100"}
  AMOUNT:
    required: true
    range: {min: "-99999.99", max: "99999.99"}
    scale: 2
record_rules:
  - name: region-account
    kind: unique_key
    fields: [REGION, ACCOUNT-NO]
  - name: contact-phone
    kind: unique_key
    fields: [CONTACT-PHONE]
"""


def account_row(sequence: int, *, region: str = "NA", account_no: str | None = None,
                phone: str | None = None, score: int = 50, amount: str = "10.00",
                history: tuple[str, str] = ("0100", "0200"), name: str | None = None) -> bytes:
    """Generate one valid 50-byte D record and patch the layout's byte ranges."""

    buffer = bytearray(_generator.account_record(sequence, region, score, amount, list(history)))
    buffer[3:11] = (account_no or f"{sequence:08d}").encode("ascii")
    buffer[27:35] = (phone or f"5550{sequence:04d}").encode("ascii")
    buffer[35:38] = f"{score:03d}".encode("ascii")
    if name is not None:
        buffer[11:27] = name.encode("ascii")
    return bytes(buffer)


def write_bytes(tmp_path: Path, name: str, rows: list[bytes]) -> Path:
    path = tmp_path / name
    path.write_bytes(b"".join(rows))
    return path


def write_rules(tmp_path: Path, name: str = "rules.yaml", text: str = RULES_YAML) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def run_policy(tmp_path: Path, data: Path, rules: Path, *, tag: str = "",
               **kwargs) -> object:
    return run(RunOptions(data, ACCOUNT_COPYBOOK, rules, tmp_path / f"runs{tag}",
                          tmp_path / f"ledger{tag}.sqlite", NOW, **kwargs))


def payload(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def three_row_scenario(tmp_path: Path) -> tuple[Path, Path]:
    """r1 (ACCT 1, phone 1, 9) vs r2 (ACCT 1, phone 2, 6) and r3 (ACCT 2, phone 1, 6).

    r1 conflicts with both r2 and r3 on two independent keys; r2 and r3 are
    compatible.  Sequential accepts r1 (total 9); preference accepts r2+r3 (12).
    Picking the best record per key and merging would wrongly yield r1 alone.
    """

    rows = [account_row(1, account_no="00000001", phone="55500001", score=9),
            account_row(2, account_no="00000001", phone="55500002", score=6),
            account_row(3, account_no="00000002", phone="55500001", score=6)]
    return write_bytes(tmp_path, "three.dat", rows), write_rules(tmp_path)


def test_three_row_contracts_are_both_legal_and_different(tmp_path):
    data, rules = three_row_scenario(tmp_path)
    sequential = run_policy(tmp_path, data, rules, tag="-seq")
    preferred = run_policy(tmp_path, data, rules, tag="-pref", unique_policy=PREFER_COMBINATION_POLICY,
                           unique_score_field="SCORE")

    # Default contract keeps the historical first-come-wins acceptance order.
    assert (sequential.counts["accepted_records"], sequential.counts["accepted_data_records"]) == (1, 1)
    assert sequential.reject_codes == {"R008_UNIQUE": 2}
    assert [row["record_no"] for row in payload(sequential.outputs["accepted"])] == [1]
    assert [row["record_no"] for row in payload(sequential.outputs["rejects"])] == [2, 3]
    assert sequential.unique_selection is None
    assert sequential.contract_meta["rules"]["sha256"] == hashlib.sha256(rules.read_bytes()).hexdigest()

    # Authorized contract accepts the higher-total compatible set after proof.
    assert (preferred.counts["accepted_records"], preferred.counts["accepted_data_records"]) == (2, 2)
    assert preferred.reject_codes == {"R011_UNIQUE_SELECTION": 1}
    assert [row["record_no"] for row in payload(preferred.outputs["accepted"])] == [2, 3]
    rejects = payload(preferred.outputs["rejects"])
    assert [row["record_no"] for row in rejects] == [1]
    hit = rejects[0]["rule_hits"][0]
    assert hit["code"] == "R011_UNIQUE_SELECTION"
    assert hit["detail"]["conflict_keys"] == [
        {"key": ["NA", "00000001"], "rule": "region-account"},
        {"key": ["55500001"], "rule": "contact-phone"},
    ]
    assert [item["record_no"] for item in hit["detail"]["conflicts_with"]] == [2, 3]
    assert all(item["selected"] is True for item in hit["detail"]["conflicts_with"])
    assert hit["detail"]["selected_record_nos"] == [2, 3]
    assert hit["detail"]["selected_total_score"] == "12"
    assert hit["detail"]["optimality"]["complete"] is True
    assert hit["detail"]["optimality"]["method"] == "exhaustive_branch_and_bound"

    selection = preferred.unique_selection
    assert selection["final"] is True and selection["status"] == "proven"
    assert selection["selected_record_nos"] == [2, 3]
    assert selection["selected_total_score"] == "12"
    assert selection["components"][0]["selected_total_score"] == "12"

    # The two contracts are distinct runs, not one reused key.
    assert sequential.run_key != preferred.run_key
    assert sequential.contract_meta.get("unique_selection") is None
    assert preferred.contract_meta["unique_selection"] == {
        "policy": "prefer_combination", "score_field": "SCORE"}

    # Raw bytes of the unselected row are preserved and its format was not at fault.
    raw = data.read_bytes()
    reject = rejects[0]
    assert reject["raw_sha256"] == hashlib.sha256(raw[0:50]).hexdigest()
    assert not reject["errors"] and {item["code"] for item in reject["rule_hits"]} == {
        "R011_UNIQUE_SELECTION"}
    assert (Path(preferred.outputs["rejects"]).parent / reject["raw_file"]).read_bytes() == raw[0:50]

    # Profiles follow the accepted set, and reports stay deterministic.
    profile = load_json(Path(preferred.outputs["profile"]))
    assert profile["record_count"] == 2
    score_field = next(item for item in profile["fields"] if item["name"] == "SCORE")
    assert score_field["distinct_count"] == 1 and score_field["top_values"] == [{"count": 2, "value": "6"}]
    markdown = Path(preferred.outputs["report"]).read_text(encoding="utf-8")
    assert "R011_UNIQUE_SELECTION" in markdown


def test_preference_profiles_and_reject_files_agree(tmp_path):
    data, rules = three_row_scenario(tmp_path)
    preferred = run_policy(tmp_path, data, rules, unique_policy=PREFER_COMBINATION_POLICY,
                           unique_score_field="SCORE")
    accepted = payload(preferred.outputs["accepted"])
    rejected = payload(preferred.outputs["rejects"])
    profile = load_json(Path(preferred.outputs["profile"]))
    assert profile["record_count"] == len(accepted) == preferred.counts["accepted_data_records"]
    assert {row["record_no"] for row in accepted} | {row["record_no"] for row in rejected} == {1, 2, 3}
    assert not ({row["record_no"] for row in accepted} & {row["record_no"] for row in rejected})


def test_long_conflict_chain_from_two_independent_keys(tmp_path):
    """Two independent keys form a genuine long chain, not a clique.

    Phones pair rows (1,2), (3,4), (5,6), (7,8); account numbers bridge the pairs
    (2,3), (4,5), (6,7), so the eight records form one 8-node path.  Every record
    is compatible with all but its immediate neighbours.
    """

    bridges = {2: "00000003", 3: "00000003", 4: "00000004", 5: "00000004",
               6: "00000005", 7: "00000005"}
    rows = []
    for index in range(1, 9):
        account_no = bridges.get(index, f"{index:08d}")
        phone = f"5550{(index + 1) // 2:04d}"
        rows.append(account_row(index, account_no=account_no, phone=phone, score=20 - index))
    data = write_bytes(tmp_path, "chain.dat", rows)
    rules = write_rules(tmp_path)
    preferred = run_policy(tmp_path, data, rules, unique_policy=PREFER_COMBINATION_POLICY,
                           unique_score_field="SCORE")
    selection = preferred.unique_selection
    assert selection["final"] is True and selection["status"] == "proven"
    assert selection["component_count"] == 1 and selection["unresolved_component_count"] == 0
    component = selection["components"][0]
    assert component["record_nos"] == list(range(1, 9))
    assert {(edge["a"], edge["b"]) for edge in component["conflict_edges"]} == {
        (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8)}
    # Highest-total independent set: 1, 3, 5, 7 (19+17+15+13 = 64).
    assert component["selected"] == [1, 3, 5, 7]
    assert selection["selected_record_nos"] == [1, 3, 5, 7]
    assert selection["selected_total_score"] == "64"
    assert component["evidence"]["complete"] is True
    assert component["evidence"]["root_upper_bound"] == "124"


def test_component_picks_a_compatible_pair_over_a_higher_single_record(tmp_path):
    # r1 alone scores 9 but blocks r2+r3 (6+6=12); sequential would keep r1.
    data, rules = three_row_scenario(tmp_path)
    preferred = run_policy(tmp_path, data, rules, unique_policy=PREFER_COMBINATION_POLICY,
                           unique_score_field="SCORE")
    edges = preferred.unique_selection["components"][0]["conflict_edges"]
    assert {(item["a"], item["b"]) for item in edges} == {(1, 2), (1, 3)}
    assert preferred.unique_selection["components"][0]["selected"] == [2, 3]
    assert preferred.unique_selection["selected_total_score"] == "12"


def test_field_reject_does_not_enter_graph_and_does_not_poison_others(tmp_path):
    rows = [account_row(1, account_no="00000001", phone="55500001", score=9),
            account_row(2, account_no="00000002", phone="55500001", score=6),
            # Invalid region trips a field rule before unique adjudication.
            account_row(3, account_no="00000003", phone="55500001", score=99, region="ZZ")]
    data = write_bytes(tmp_path, "field-reject.dat", rows)
    rules = write_rules(tmp_path)
    preferred = run_policy(tmp_path, data, rules, unique_policy=PREFER_COMBINATION_POLICY,
                           unique_score_field="SCORE")
    rejected = payload(preferred.outputs["rejects"])
    # The field-rejected row is not in the graph, so it does not split the pair;
    # the remaining conflict is settled on score alone.
    assert [row["record_no"] for row in rejected] == [2, 3]
    assert rejected[0]["codes"] == ["R011_UNIQUE_SELECTION"]
    assert rejected[1]["codes"] == ["R002_ENUM"]
    assert rejected[1]["rule_hits"][0]["code"] == "R002_ENUM"
    assert preferred.unique_selection["selected_record_nos"] == [1]
    assert preferred.unique_selection["selected_total_score"] == "9"
    assert preferred.unique_selection["component_count"] == 1
    assert all(3 not in component["record_nos"] for component in preferred.unique_selection["components"])


def test_missing_unique_component_and_missing_score_are_not_zero(tmp_path):
    rules = RuleSet(name="u", version=1,
                    fields={"REGION": FieldRule("REGION", required=True),
                            "SCORE": FieldRule("SCORE")},
                    record_rules=(RecordRule("key", "unique_key", fields=("REGION", "MISSING-FIELD")),))
    good = Record(record_no=1, byte_offset=0, raw=b"", flat={"REGION": "NA", "SCORE": Decimal(5)})
    keys, hits = canonical_unique_keys(good, rules)
    assert keys == [] and [hit.code for hit in hits] == ["R008_UNIQUE"]

    # A contended record with an unknown score must be rejected, not scored zero.
    unique = RecordRule("key", "unique_key", fields=("REGION",))
    simple = RuleSet(name="u", version=1,
                     fields={"SCORE": FieldRule("SCORE")}, record_rules=(unique,))
    low = Record(record_no=1, byte_offset=0, raw=b"", flat={"REGION": "NA", "SCORE": Decimal(5)})
    unknown = Record(record_no=2, byte_offset=0, raw=b"", flat={"REGION": "NA", "SCORE": None})
    selection = resolve_unique_selection([low, unknown], simple, score_field="SCORE")
    assert selection.report["score_rejections"] == [
        {"record_no": 2, "reason": "score_missing", "score": None,
         "conflict_keys": [{"rule": "key", "key": ["NA"]}]}]
    assert "R011_UNIQUE_SELECTION" in [hit.code for hit in selection.hits.values()]
    assert selection.hits[2].detail["selected_total_score"] is None
    assert selection.hits[2].detail["optimality"]["status"] == "undetermined"
    assert selection.selected == {1} and selection.report["selected_total_score"] == "5"

    # Text or non-finite scores are invalid, again never coerced to zero.
    for bad in ("abc", Decimal("NaN"), Decimal("Infinity")):
        record = Record(record_no=2, byte_offset=0, raw=b"", flat={"REGION": "NA", "SCORE": bad})
        result = resolve_unique_selection([low, record], simple, score_field="SCORE")
        assert result.report["score_rejections"][0]["reason"] == "score_invalid"

    # Integer decoded scores are accepted and kept exact.
    integer = Record(record_no=2, byte_offset=0, raw=b"", flat={"REGION": "NA", "SCORE": 7})
    result = resolve_unique_selection([low, integer], simple, score_field="SCORE")
    assert result.selected == {2} and result.report["selected_total_score"] == "7"


def test_tie_breaks_by_lowest_record_number_sequence(tmp_path):
    # Two records with the same score and the same key: tie -> lowest record_no.
    a = Record(record_no=5, byte_offset=0, raw=b"", flat={"REGION": "NA", "SCORE": Decimal(6)})
    b = Record(record_no=2, byte_offset=0, raw=b"", flat={"REGION": "NA", "SCORE": Decimal(6)})
    unique = RecordRule("key", "unique_key", fields=("REGION",))
    rules = RuleSet(name="u", version=1, record_rules=(unique,))
    result = resolve_unique_selection([a, b], rules)
    assert result.selected == {2}
    assert result.report["components"][0]["evidence"]["tie_break"] == (
        "lexicographically_lowest_record_number_sequence")

    # Equal totals from different sets: the lexicographically lower sequence wins.
    records = [
        Record(record_no=1, byte_offset=0, raw=b"", flat={"REGION": "A", "SCORE": Decimal(3)}),
        Record(record_no=2, byte_offset=0, raw=b"", flat={"REGION": "A", "SCORE": Decimal(2)}),
        Record(record_no=3, byte_offset=0, raw=b"", flat={"REGION": "B", "SCORE": Decimal(1)}),
        Record(record_no=4, byte_offset=0, raw=b"", flat={"REGION": "B", "SCORE": Decimal(2)}),
    ]
    pair_rule = RuleSet(name="u", version=1, record_rules=(
        RecordRule("region", "unique_key", fields=("REGION",)),
        RecordRule("pair", "unique_key", fields=("PAIR",)),))
    for index, record in enumerate(records):
        record.flat["PAIR"] = ("X", "Y", "X", "Y")[index]
    result = resolve_unique_selection(records, pair_rule)
    # {1, 4} -> 5 and {2, 3} -> 3.  Only the first is optimal.
    assert result.selected == {1, 4} and result.report["selected_total_score"] == "5"


def test_component_too_large_and_budget_exhausted_are_undetermined(tmp_path):
    rules = RuleSet(name="u", version=1, record_rules=(
        RecordRule("key", "unique_key", fields=("REGION",)),))
    records = [Record(record_no=index, byte_offset=0, raw=b"",
                      flat={"REGION": "NA", "SCORE": Decimal(10 + index)})
               for index in range(1, 6)]
    large = resolve_unique_selection(records, rules, max_component_records=2)
    assert large.report["final"] is False and large.report["status"] == "undetermined"
    assert large.report["unresolved_component_count"] == 1
    component = large.report["components"][0]
    assert component["status"] == "undetermined" and component["selected"] == []
    assert component["evidence"]["reason"] == "component_too_large"
    assert component["evidence"]["complete"] is False
    assert large.selected == set()
    for record_no in range(1, 6):
        detail = large.hits[record_no].detail
        assert detail["reason"] == "selection_undetermined"
        assert detail["selected_record_nos"] == []
        assert detail["selected_total_score"] is None
        assert detail["optimality"]["status"] == "undetermined"

    tiny = resolve_unique_selection(records, rules, proof_budget=1)
    assert tiny.report["final"] is False
    assert tiny.report["components"][0]["evidence"]["reason"] == "proof_budget_exhausted"

    proven = resolve_unique_selection(records, rules)
    assert proven.report["final"] is True
    assert proven.selected == {5}
    assert proven.report["selected_total_score"] == "15"


def test_undetermined_selection_blocks_final_file_conclusions(tmp_path):
    # A trailer-bearing contract: with an undetermined component we must emit an
    # explicit FL006 and must not reuse a pre-selection trailer/count conclusion.
    rules_text = RULES_YAML + """\
record_type:
  field: REC-TYPE
  data: D
  trailer: T
trailer:
  count_field: ACCOUNT-NO
limits:
  min_records: 1
"""
    path = write_rules(tmp_path, "trailer.yaml", rules_text)
    rows = [account_row(index, account_no=f"{index:08d}", phone="55500001", score=10 - index)
            for index in range(1, 5)]
    trailer = bytearray(b" " * 50)
    trailer[0:1] = b"T"
    trailer[3:11] = b"00000004"  # declares the four physical data records
    data = write_bytes(tmp_path, "trailer.dat", rows + [bytes(trailer)])
    undetermined = run_policy(tmp_path, data, path, tag="-und",
                              unique_policy=PREFER_COMBINATION_POLICY,
                              unique_score_field="SCORE", unique_max_component=2)
    assert undetermined.unique_selection["final"] is False
    codes = [issue.code for issue in undetermined.file_issues]
    assert "FL006" in codes
    assert "FL001" not in codes and "FL002" not in codes
    issue = next(item for item in undetermined.file_issues if item.code == "FL006")
    assert issue.severity == "warning" and issue.detail["basis"] == "unknown"
    assert undetermined.status == "completed_with_rejects"

    proven = run_policy(tmp_path, data, path, tag="-proven",
                        unique_policy=PREFER_COMBINATION_POLICY, unique_score_field="SCORE")
    assert proven.unique_selection["final"] is True
    proven_codes = {issue.code for issue in proven.file_issues}
    assert "FL006" not in proven_codes
    # The trailer declares 4 physical data records and the proof selected 1; the
    # conclusion must be stated on the file-data-record basis, never silently
    # re-derived from the accepted subset.
    assert "FL001" not in proven_codes
    assert proven.counts["accepted_data_records"] == 1


def test_preference_mode_outputs_are_stable_and_idempotent(tmp_path):
    data, rules = three_row_scenario(tmp_path)
    first = run_policy(tmp_path, data, rules, tag="-a", unique_policy=PREFER_COMBINATION_POLICY,
                       unique_score_field="SCORE")
    second = run_policy(tmp_path, data, rules, tag="-a", unique_policy=PREFER_COMBINATION_POLICY,
                        unique_score_field="SCORE")
    assert second.idempotent and second.run_id == first.run_id
    for name in ("accepted", "rejects", "profile", "report", "summary"):
        assert Path(first.outputs[name]).read_bytes() == Path(second.outputs[name]).read_bytes()

    other = run_policy(tmp_path, data, rules, tag="-b", unique_policy=PREFER_COMBINATION_POLICY,
                       unique_score_field="SCORE")
    for name in ("accepted", "rejects", "profile", "report"):
        assert Path(first.outputs[name]).read_bytes() == Path(other.outputs[name]).read_bytes()


def test_sequential_mode_is_byte_identical_to_the_pinned_baseline(tmp_path):
    """Old-mode regression: run key, counts and artifacts must not drift."""

    data, rules = three_row_scenario(tmp_path)
    sequential = run_policy(tmp_path, data, rules)
    assert sequential.unique_selection is None
    assert sequential.counts == {"total_records": 3, "data_records": 3, "accepted_records": 1,
                                 "accepted_data_records": 1, "rejected_records": 2,
                                 "file_issues": 0}
    assert sequential.reject_codes == {"R008_UNIQUE": 2}
    assert [row["record_no"] for row in payload(sequential.outputs["accepted"])] == [1]
    details = [row["rule_hits"][0]["detail"] for row in payload(sequential.outputs["rejects"])]
    # File order: r2 collides on [REGION, ACCOUNT-NO]; r3 collides on the phone key.
    assert details[0]["key"] == ["NA", "00000001"]
    assert details[1]["key"] == ["55500001"]

    # Fixture-level regression: account demo artifacts and run key are unchanged.
    result = run(RunOptions(FIXTURES / "data" / "account_normal.dat", ACCOUNT_COPYBOOK,
                            FIXTURES / "rules" / "account_rules.yaml", tmp_path / "acct-runs",
                            tmp_path / "acct.sqlite", NOW))
    assert result.run_key == "4e2126e9decb73a0e569191a5de370bafaa6f25e8f1fc07b1ce5e72de994b4b3"
    assert result.counts["accepted_records"] == 3 and result.unique_selection is None
    raw = (FIXTURES / "data" / "account_normal.dat").read_bytes()
    accepted = payload(result.outputs["accepted"])
    assert [row["raw_sha256"] for row in accepted] == [
        hashlib.sha256(raw[index * 50:(index + 1) * 50]).hexdigest() for index in range(3)]
    # Byte-stable across two fresh runs in the same output root.
    profile_before = Path(result.outputs["profile"]).read_bytes()
    forced = run(RunOptions(FIXTURES / "data" / "account_normal.dat", ACCOUNT_COPYBOOK,
                            FIXTURES / "rules" / "account_rules.yaml", tmp_path / "acct-runs",
                            tmp_path / "acct.sqlite", NOW, force=True))
    assert Path(forced.outputs["profile"]).read_bytes() == profile_before


def test_policy_requires_explicit_score_field_and_rejects_bad_configuration(tmp_path):
    data, rules = three_row_scenario(tmp_path)
    from copybook_kit.errors import InputError

    with pytest.raises(InputError) as missing:
        run_policy(tmp_path, data, rules, unique_policy=PREFER_COMBINATION_POLICY)
    assert missing.value.code == "CLI001"
    with pytest.raises(InputError):
        run_policy(tmp_path, data, rules, unique_policy=PREFER_COMBINATION_POLICY,
                   unique_score_field="NOT-A-FIELD")
    with pytest.raises(InputError):
        run_policy(tmp_path, data, rules, unique_policy="majority")
    with pytest.raises(InputError):
        run_policy(tmp_path, data, rules, unique_score_field="SCORE")

    # Run identity includes the selection contract and its budgets.
    keys = set()
    for policy in ("sequential", "prefer_combination"):
        result = run_policy(tmp_path, data, rules, tag=f"-{policy}",
                            unique_policy=policy,
                            **({"unique_score_field": "SCORE"}
                               if policy != "sequential" else {}))
        keys.add(result.run_key)
    assert len(keys) == 2


def test_preference_mode_resumes_with_the_same_contract(tmp_path):
    """Resume must rebuild the selection contract, not fall back to sequential."""

    from copybook_kit.pipeline.options import prepare_run, run_spec
    from copybook_kit.pipeline.runner import resume_run
    from copybook_kit.store.ledger import Ledger

    data, rules = three_row_scenario(tmp_path)
    options = RunOptions(data, ACCOUNT_COPYBOOK, rules, tmp_path / "runs",
                         tmp_path / "ledger.sqlite", NOW,
                         unique_policy=PREFER_COMBINATION_POLICY, unique_score_field="SCORE")
    prepared, ledger = prepare_run(options), Ledger(options.db_path)
    ledger.insert_run(run_spec(prepared, options.out_root / prepared.run_id))
    ledger.mark_failed(prepared.run_id, "2026-01-01T00:00:00Z", "simulated interruption")
    result = resume_run(prepared.run_id, options.db_path, NOW)
    assert result.status == "completed_with_rejects"
    assert [row["record_no"] for row in payload(result.outputs["accepted"])] == [2, 3]
    assert result.unique_selection["selected_total_score"] == "12"
    row = ledger.get_run(prepared.run_id)
    assert row["unique_policy"] == PREFER_COMBINATION_POLICY and row["unique_score_field"] == "SCORE"


def test_proven_selection_states_the_file_basis_for_trailer_checks(tmp_path):
    rules_text = RULES_YAML + """\
record_type:
  field: REC-TYPE
  data: D
  trailer: T
trailer:
  count_field: ACCOUNT-NO
"""
    path = write_rules(tmp_path, "trailer-basis.yaml", rules_text)
    # The trailer declares 3 records but a declared count of 4 triggers FL001 on
    # the file's physical data records; basis must be explicit.
    rows = [account_row(index, account_no=f"{index:08d}", phone="55500001", score=10 - index)
            for index in range(1, 4)]
    trailer = bytearray(b" " * 50)
    trailer[0:1] = b"T"
    trailer[3:11] = b"00000009"
    data = write_bytes(tmp_path, "basis.dat", rows + [bytes(trailer)])
    result = run_policy(tmp_path, data, path, unique_policy=PREFER_COMBINATION_POLICY,
                        unique_score_field="SCORE")
    issue = next(item for item in result.file_issues if item.code == "FL001")
    assert issue.detail["basis"] == "file_data_records"
    assert issue.detail["selection"] == "proven"
    assert result.counts["accepted_data_records"] == 1
