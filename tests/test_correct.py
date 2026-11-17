"""Offline correction exports: manifests, encoding, both contracts, conflicts and lineage."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

import copybook_kit.cli as cli
from conftest import FIXTURES, NOW, cpy
from copybook_kit.copybook.layout import compile_copybook
from copybook_kit.correct.encode import EncodeError, encode_field
from copybook_kit.correct.export import CorrectOptions, correct
from copybook_kit.errors import CorrectionError, LedgerError
from copybook_kit.pipeline.options import RunOptions, prepare_run, run_spec
from copybook_kit.pipeline.runner import run
from copybook_kit.store.ledger import Ledger

ACCOUNT_CPY = FIXTURES / "copybooks" / "account.cpy"
ACCOUNT_RULES = FIXTURES / "rules" / "account_rules.yaml"
LENGTH = 50  # account.cpy record length

def pack_comp3(value, digits=7, scale=2, positive_sign=0x0C):
    scaled = int(Decimal(str(value)).scaleb(scale))
    nibble = 0x0D if scaled < 0 else positive_sign
    nibbles = [int(char) for char in str(abs(scaled)).zfill(digits)] + [nibble]
    if len(nibbles) % 2:
        nibbles.insert(0, 0)
    return bytes((nibbles[index] << 4) | nibbles[index + 1]
                 for index in range(0, len(nibbles), 2))

def account_record(seq, *, region="NA", score=85, amount="100.00", history=("0100", "0200"),
                   rec_type="D", name=None, amount_bytes=None, contact_bytes=None):
    buffer = bytearray(b" " * LENGTH)
    buffer[0:1] = rec_type.encode("ascii")
    buffer[1:3] = region.encode("ascii")
    buffer[3:11] = f"{seq:08d}".encode("ascii")
    if contact_bytes is not None:
        buffer[11:35] = contact_bytes
    else:
        buffer[11:27] = (name or f"DEMO ACCOUNT {seq:03d}").encode("ascii").ljust(16, b" ")
        buffer[27:35] = f"55501{seq:03d}".encode("ascii")
    buffer[35:38] = f"{score:03d}".encode("ascii")
    buffer[38:42] = amount_bytes if amount_bytes is not None else pack_comp3(amount)
    buffer[42:46] = history[0].encode("ascii")
    buffer[46:50] = history[1].encode("ascii")
    return bytes(buffer)

def source_run(tmp_path, records, rules=ACCOUNT_RULES):
    input_path = tmp_path / "source-input.dat"
    input_path.write_bytes(b"".join(records))
    result = run(RunOptions(input_path=input_path, copybook_path=ACCOUNT_CPY,
                            rules_path=rules, out_root=tmp_path / "runs",
                            db_path=tmp_path / "ledger.sqlite", now=NOW))
    return result, input_path

def record_sha(input_path, record_no):
    data = input_path.read_bytes()
    chunk = data[(record_no - 1) * LENGTH:record_no * LENGTH]
    return hashlib.sha256(chunk).hexdigest()

def correction(input_path, record_no, path, value):
    return {"record_no": record_no, "expected_sha256": record_sha(input_path, record_no),
            "path": path, "value": value}

def manifest_file(tmp_path, source, corrections, *, contract="evidence", redefines=None,
                  name="batch-1"):
    document = {"version": 1, "name": name, "source_run_id": source.run_id,
                "contract": contract, "corrections": corrections}
    if redefines is not None:
        document["redefines"] = redefines
    path = tmp_path / f"manifest-{name}.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path

def run_correct(tmp_path, manifest_path):
    return correct(CorrectOptions(manifest_path=manifest_path,
                                  db_path=tmp_path / "ledger.sqlite", now=NOW))

def failing_correct(tmp_path, manifest_path):
    with pytest.raises(CorrectionError) as caught:
        run_correct(tmp_path, manifest_path)
    return caught.value.diagnostics

def codes_of(diagnostics):
    return {item.code for item in diagnostics}

def test_evidence_export_corrects_reject_and_preserves_everything_else(tmp_path):
    records = [account_record(1), account_record(2, score=150), account_record(3, region="EU")]
    source, input_path = source_run(tmp_path, records)
    assert source.status == "completed_with_rejects" and source.counts["rejected_records"] == 1
    input_sha = hashlib.sha256(input_path.read_bytes()).hexdigest()
    reject_bins = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (tmp_path / "runs" / source.run_id / "rejects").glob("*.bin")}

    manifest = manifest_file(tmp_path, source, [correction(input_path, 2, "SCORE", "80")])
    report = run_correct(tmp_path, manifest)
    payload = report.to_dict()
    assert report.exit_code == 0 and payload["ok"] is True and payload["idempotent"] is False
    assert payload["contract"] == "evidence" and payload["source_run_id"] == source.run_id
    assert payload["input"]["sha256"] == input_sha
    assert payload["output"]["sha256"] != input_sha
    assert payload["unchanged_bytes_verified"] is True

    entry, = payload["corrections"]
    assert entry["record_no"] == 2 and entry["path"] == "SCORE"
    assert entry["record_offset"] == 35 and entry["byte_offset"] == 50 + 35
    assert entry["old_bytes"] == b"150".hex() and entry["new_bytes"] == b"080".hex()
    assert entry["old_value"] == "150" and entry["new_value"] == "80"
    assert entry["old_sha8"] != entry["new_sha8"]
    assert payload["diff_ranges"] == [
        {"record_no": 2, "byte_offset": 85, "length": 3, "reason": "correction",
         "path": "SCORE", "old_sha8": entry["old_sha8"], "new_sha8": entry["new_sha8"]}]

    original = input_path.read_bytes()
    exported = Path(payload["output"]["path"]).read_bytes()
    assert len(exported) == len(original)
    assert exported[:85] == original[:85] and exported[85:88] == b"080"
    assert exported[88:] == original[88:]
    assert exported[:50] == original[:50] and exported[100:] == original[100:]

    review = payload["review"]
    assert review["status"] == "completed" and review["exit_code"] == 0
    assert review["counts"]["accepted_records"] == 3 and review["counts"]["rejected_records"] == 0

    ledger = Ledger(tmp_path / "ledger.sqlite")
    child = ledger.get_run(payload["run_id"])
    assert child["kind"] == "correct" and child["parent_run_id"] == source.run_id
    batch, = ledger.list_correction_batches()
    assert batch["contract"] == "evidence" and batch["status"] == "published"
    assert batch["diff_ranges"] == payload["diff_ranges"]
    correction_json = tmp_path / "runs" / payload["run_id"] / "correction.json"
    assert json.loads(correction_json.read_text(encoding="utf-8"))["batch_id"] == payload["batch_id"]

    # the source input and the source run's reject evidence stay untouched
    assert hashlib.sha256(input_path.read_bytes()).hexdigest() == input_sha
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in reject_bins} == reject_bins

def test_occurs_comp3_and_declared_scale_encoding(tmp_path):
    records = [account_record(1), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records)
    items = [correction(input_path, 1, "SCORE-HISTORY[1]", "0123"),
             correction(input_path, 1, "AMOUNT", "-123.45"),
             correction(input_path, 2, "AMOUNT", "1.5")]
    report = run_correct(tmp_path, manifest_file(tmp_path, source, items))
    payload = report.to_dict()
    assert report.exit_code == 0
    by_path = {(entry["record_no"], entry["path"]): entry for entry in payload["corrections"]}
    assert by_path[(1, "SCORE-HISTORY[1]")]["new_bytes"] == b"0123".hex()
    assert by_path[(1, "AMOUNT")]["new_bytes"] == bytes.fromhex("0012345d").hex()
    assert by_path[(1, "AMOUNT")]["new_value"] == "-123.45"
    # the declared scale is preserved through Decimal: 1.5 is stored and read back as 1.50
    assert by_path[(2, "AMOUNT")]["new_value"] == "1.50"
    exported = Path(payload["output"]["path"]).read_bytes()
    assert exported[46:50] == b"0123" and exported[38:42] == bytes.fromhex("0012345d")
    accepted = (tmp_path / "runs" / payload["run_id"] / "accepted.jsonl").read_text(encoding="utf-8")
    second = [json.loads(line) for line in accepted.splitlines()][1]
    assert second["values"]["AMOUNT"] == "1.50"

def test_redefines_branch_export_and_declaration_validation(tmp_path):
    records = [account_record(1), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records)
    email = correction(input_path, 1, "CONTACT-V2.EMAIL", "OPS AT EXAMPLE DOT COM")

    selected = manifest_file(tmp_path, source, [email], name="selected",
                             redefines={"CONTACT": "CONTACT-V2"})
    payload = run_correct(tmp_path, selected).to_dict()
    entry, = payload["corrections"]
    assert entry["byte_offset"] == 11 and entry["length"] == 24
    assert entry["new_bytes"] == b"OPS AT EXAMPLE DOT COM  ".hex()
    exported = Path(payload["output"]["path"]).read_bytes()
    assert exported[11:35] == b"OPS AT EXAMPLE DOT COM  "
    assert exported[:11] == input_path.read_bytes()[:11] and exported[35:] == input_path.read_bytes()[35:]
    assert payload["redefines"] == {"CONTACT": "CONTACT-V2"}
    assert payload["review"]["status"] == "completed"

    missing = manifest_file(tmp_path, source, [email], name="missing")
    assert codes_of(failing_correct(tmp_path, missing)) == {"CR006"}

    other_branch = manifest_file(tmp_path, source, [email], name="other",
                                 redefines={"CONTACT": "CONTACT"})
    assert codes_of(failing_correct(tmp_path, other_branch)) == {"CR006"}

    name_item = correction(input_path, 1, "CONTACT.CONTACT-NAME", "RENAMED ACCOUNT")
    wrong_side = manifest_file(tmp_path, source, [name_item], name="wrong-side",
                               redefines={"CONTACT": "CONTACT-V2"})
    assert codes_of(failing_correct(tmp_path, wrong_side)) == {"CR006"}

    untouched = manifest_file(tmp_path, source, [correction(input_path, 1, "SCORE", "090")],
                              name="untouched", redefines={"CONTACT": "CONTACT-V2"})
    assert codes_of(failing_correct(tmp_path, untouched)) == {"CR006"}

    unknown_group = manifest_file(tmp_path, source, [email], name="unknown-group",
                                  redefines={"NOPE": "CONTACT-V2"})
    assert codes_of(failing_correct(tmp_path, unknown_group)) == {"CR006"}

    unknown_branch = manifest_file(tmp_path, source, [email], name="unknown-branch",
                                   redefines={"CONTACT": "NOPE"})
    assert "CR006" in codes_of(failing_correct(tmp_path, unknown_branch))

def test_conflicting_writes_and_duplicate_paths_reject_the_whole_batch(tmp_path):
    records = [account_record(1), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records)
    both_branches = [
        correction(input_path, 1, "CONTACT.CONTACT-NAME", "RENAMED ACCOUNT"),
        correction(input_path, 1, "CONTACT-V2.EMAIL", "OPS AT EXAMPLE DOT COM"),
    ]
    manifest = manifest_file(tmp_path, source, both_branches,
                             redefines={"CONTACT": "CONTACT-V2"})
    diagnostics = failing_correct(tmp_path, manifest)
    assert "CR005" in codes_of(diagnostics)
    conflict = next(item for item in diagnostics if item.code == "CR005")
    assert conflict.record_no == 1 and conflict.byte_offset == 11
    assert conflict.detail["reason"] == "redefines_branches"
    assert conflict.detail["first"]["offset"] == 11 and conflict.detail["second"]["offset"] == 11

    duplicate = [correction(input_path, 1, "SCORE", "080"),
                 correction(input_path, 1, "SCORE", "090")]
    diagnostics = failing_correct(tmp_path, manifest_file(tmp_path, source, duplicate,
                                                          name="dup"))
    conflict = next(item for item in diagnostics if item.code == "CR005")
    assert conflict.detail["reason"] == "duplicate_path"

    # nothing was published: no child run, no batch row, no export file
    ledger = Ledger(tmp_path / "ledger.sqlite")
    assert [row["kind"] for row in ledger.list_runs()] == ["ingest"]
    assert ledger.list_correction_batches() == []
    assert not (tmp_path / "runs" / "exports").exists()

def test_overflow_unrepresentable_and_too_long_values_are_rejected(tmp_path):
    records = [account_record(1)]
    source, input_path = source_run(tmp_path, records)
    bad_values = [
        ("SCORE", "1000"),        # overflows PIC 9(3)
        ("SCORE", "-1"),          # unsigned field cannot be negative
        ("AMOUNT", "123456.78"),  # overflows S9(5)V9(2)
        ("AMOUNT", "1.234"),      # finer than the declared scale 2
        ("AMOUNT", None),         # blank cannot be represented in COMP-3
        ("CONTACT.CONTACT-PHONE", "123456789"),  # longer than X(8)
        ("CONTACT.CONTACT-NAME", "TAIL "),       # trailing spaces are not representable
        ("SCORE-HISTORY[0]", "12.5"),            # scale 0 field
    ]
    items = [correction(input_path, 1, path, value) for path, value in bad_values]
    diagnostics = failing_correct(tmp_path, manifest_file(
        tmp_path, source, items, redefines={"CONTACT": "CONTACT"}))
    assert codes_of(diagnostics) == {"CR004"}
    assert len(diagnostics) == len(bad_values)
    for item in diagnostics:
        assert item.record_no == 1 and item.field and item.byte_offset is not None
    assert Ledger(tmp_path / "ledger.sqlite").list_correction_batches() == []

def test_digest_path_and_record_validation(tmp_path):
    records = [account_record(1), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records)

    stale = {"record_no": 1, "expected_sha256": "0" * 64, "path": "SCORE", "value": "80"}
    assert codes_of(failing_correct(tmp_path, manifest_file(tmp_path, source, [stale],
                                                            name="stale"))) == {"CR002"}
    for path in ("NOPE", "SCORE-HISTORY[2]", "CONTACT", "ACCOUNT-KEY"):
        bad = correction(input_path, 1, path, "X")
        assert codes_of(failing_correct(tmp_path, manifest_file(tmp_path, source, [bad],
                                                                name=f"path-{path}"))) == {"CR003"}
    out_of_range = correction(input_path, 99, "SCORE", "80")
    out_of_range["expected_sha256"] = "0" * 64
    assert codes_of(failing_correct(tmp_path, manifest_file(tmp_path, source, [out_of_range],
                                                            name="range"))) == {"CR001"}

    # drift of the source input or of the original contract forbids any export
    drifted = correction(input_path, 1, "SCORE", "80")
    manifest = manifest_file(tmp_path, source, [drifted], name="drift-input")
    input_path.write_bytes(input_path.read_bytes()[:-1] + b"X")
    assert codes_of(failing_correct(tmp_path, manifest)) == {"CR002"}

    # a changed copybook or rules file means the original contract is gone
    local_rules = tmp_path / "local-rules.yaml"
    local_rules.write_bytes(ACCOUNT_RULES.read_bytes() + b"# local\n")
    input_path.write_bytes(b"".join(records))
    source = run(RunOptions(input_path=input_path, copybook_path=ACCOUNT_CPY,
                            rules_path=local_rules, out_root=tmp_path / "runs",
                            db_path=tmp_path / "ledger.sqlite", now=NOW))
    manifest = manifest_file(tmp_path, source, [correction(input_path, 1, "SCORE", "80")],
                             name="drift-rules")
    local_rules.write_bytes(local_rules.read_bytes() + b"# drifted\n")
    assert codes_of(failing_correct(tmp_path, manifest)) == {"CR002"}

def test_canonical_export_recodes_and_preserves_business_values(tmp_path):
    odd_amount = pack_comp3("123.45", positive_sign=0x0F)  # legal but non-canonical sign
    records = [account_record(1, amount_bytes=odd_amount), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records)
    assert source.status == "completed"
    items = [correction(input_path, 1, "SCORE", "090")]

    canonical = run_correct(tmp_path, manifest_file(tmp_path, source, items,
                                                    contract="canonical", name="canon"))
    canon = canonical.to_dict()
    reasons = {(diff["path"], diff["reason"]) for diff in canon["diff_ranges"]}
    assert reasons == {("SCORE", "correction"), ("AMOUNT", "recanonicalize")}
    exported = Path(canon["output"]["path"]).read_bytes()
    assert exported[38:42] == pack_comp3("123.45")  # re-encoded with the canonical C sign
    assert exported[38:42] != odd_amount
    accepted = (tmp_path / "runs" / canon["run_id"] / "accepted.jsonl").read_text(encoding="utf-8")
    first = json.loads(accepted.splitlines()[0])
    assert first["values"]["AMOUNT"] == "123.45"  # business value unchanged

    evidence = run_correct(tmp_path, manifest_file(tmp_path, source, items, name="evid"))
    evid = evidence.to_dict()
    kept = Path(evid["output"]["path"]).read_bytes()
    assert kept[38:42] == odd_amount  # evidence export keeps the legal non-canonical bytes
    assert evid["output"]["sha256"] != canon["output"]["sha256"]

    batches = Ledger(tmp_path / "ledger.sqlite").list_correction_batches()
    by_contract = {batch["contract"]: batch for batch in batches}
    assert set(by_contract) == {"evidence", "canonical"}
    assert [diff["reason"] for diff in by_contract["canonical"]["diff_ranges"]] == [
        "correction", "recanonicalize"]
    assert [diff["reason"] for diff in by_contract["evidence"]["diff_ranges"]] == ["correction"]

def test_canonical_preserves_undeclared_redefines_coverage(tmp_path):
    broken_contact = bytearray(account_record(1))
    broken_contact[11:14] = b"\xff\xfe\xfd"  # undecodable UTF-8 inside the CONTACT group
    records = [bytes(broken_contact), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records)
    assert source.status == "completed_with_rejects"

    items = [correction(input_path, 1, "SCORE", "090")]
    payload = run_correct(tmp_path, manifest_file(tmp_path, source, items,
                                                  contract="canonical")).to_dict()
    exported = Path(payload["output"]["path"]).read_bytes()
    assert exported[11:35] == bytes(broken_contact)[11:35]  # untouched group keeps raw bytes
    assert [diff["path"] for diff in payload["diff_ranges"]] == ["SCORE"]
    assert payload["review"]["status"] == "completed_with_rejects"
    assert payload["review"]["counts"]["rejected_records"] == 1

def test_review_outcomes_are_published_record_reject_and_file_failure(tmp_path):
    records = [account_record(1), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records)
    bad_region = [correction(input_path, 1, "ACCOUNT-KEY.REGION", "XX")]
    payload = run_correct(tmp_path, manifest_file(tmp_path, source, bad_region)).to_dict()
    assert payload["review"]["status"] == "completed_with_rejects"
    assert payload["review"]["counts"]["rejected_records"] == 1
    assert payload["review"]["reject_codes"] == {"R002_ENUM": 1}
    assert payload["review"]["exit_code"] == 0

    trailer_rules = tmp_path / "trailer-rules.yaml"
    trailer_rules.write_text(
        "version: 1\nname: trailer-rules\n"
        "record_type: {field: REC-TYPE, data: D, trailer: T}\n"
        "trailer: {amount_field: AMOUNT}\n"
        "fields:\n  REGION:\n    enum: [\"NA\", \"EU\", \"AP\"]\n", encoding="utf-8")
    trailer = account_record(9, rec_type="T", amount="30.00")
    source2, input2 = source_run(tmp_path, [account_record(1, amount="10.00"),
                                            account_record(2, region="EU", amount="20.00"),
                                            trailer], rules=trailer_rules)
    assert source2.status == "completed"
    change_amount = [correction(input2, 1, "AMOUNT", "15.00")]
    payload = run_correct(tmp_path, manifest_file(tmp_path, source2, change_amount,
                                                  name="trailer")).to_dict()
    assert payload["review"]["status"] == "failed" and payload["review"]["exit_code"] == 3
    batch = Ledger(tmp_path / "ledger.sqlite").get_correction_batch(payload["batch_id"])
    assert batch["status"] == "published"  # a failed review is a result, not a process failure

def test_idempotent_resubmission_and_export_immutability(tmp_path):
    records = [account_record(1), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records)
    manifest = manifest_file(tmp_path, source, [correction(input_path, 1, "SCORE", "090")])
    first = run_correct(tmp_path, manifest).to_dict()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    runs_before = len(ledger.list_runs())
    export_path = Path(first["output"]["path"])
    inode_before = os.stat(export_path).st_ino

    second = run_correct(tmp_path, manifest)
    assert second.idempotent and second.exit_code == 0
    assert second.to_dict()["batch_id"] == first["batch_id"]
    assert second.to_dict()["run_id"] == first["run_id"]
    assert len(ledger.list_runs()) == runs_before
    assert len(ledger.list_correction_batches()) == 1
    assert os.stat(export_path).st_ino == inode_before

    export_path.write_bytes(b"tampered" + export_path.read_bytes()[8:])
    with pytest.raises(LedgerError) as caught:
        run_correct(tmp_path, manifest)
    assert caught.value.code == "IO001"
    assert export_path.read_bytes().startswith(b"tampered")  # never overwritten

def test_manifest_schema_errors_are_cr001(tmp_path):
    records = [account_record(1)]
    source, input_path = source_run(tmp_path, records)
    good = correction(input_path, 1, "SCORE", "80")

    def write(name, text):
        path = tmp_path / f"schema-{name}.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    base = (f"version: 1\nsource_run_id: {source.run_id}\ncontract: evidence\n"
            f"corrections:\n  - record_no: 1\n    expected_sha256: {good['expected_sha256']}\n"
            f"    path: SCORE\n    value: ")
    assert codes_of(failing_correct(tmp_path, write("float", base + "8.5\n"))) == {"CR001"}
    assert codes_of(failing_correct(tmp_path, write("bool", base + "true\n"))) == {"CR001"}
    assert codes_of(failing_correct(tmp_path, write(
        "contract", base.replace("contract: evidence", "contract: sideways") + "80\n"))) == {"CR001"}
    assert codes_of(failing_correct(tmp_path, write(
        "unknown", base + "80\nextra: 1\n"))) == {"CR001"}
    duplicated = base + "80\n" + "corrections: []\n"
    assert codes_of(failing_correct(tmp_path, write("dup", duplicated))) == {"CR001"}

def test_canonical_recodes_blank_padding_under_blank_zero_policy(tmp_path):
    rules = tmp_path / "blank-zero-rules.yaml"
    rules.write_text(
        "version: 1\nname: blank-zero\n"
        "fields:\n  SCORE:\n    blank: zero\n    range: {min: \"0\", max: \"100\"}\n",
        encoding="utf-8")
    blank_score = bytearray(account_record(1))
    blank_score[35:38] = b"   "  # legal blank padding, decodes to 0 under blank=zero
    records = [bytes(blank_score), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records, rules=rules)
    assert source.status == "completed"
    items = [correction(input_path, 1, "SCORE-HISTORY[0]", "0555")]

    canonical = run_correct(tmp_path, manifest_file(tmp_path, source, items,
                                                    contract="canonical", name="canon"))
    canon = canonical.to_dict()
    assert {(diff["record_no"], diff["path"], diff["reason"]) for diff in canon["diff_ranges"]
            } == {(1, "SCORE-HISTORY[0]", "correction"), (1, "SCORE", "recanonicalize")}
    exported = Path(canon["output"]["path"]).read_bytes()
    assert exported[35:38] == b"000"  # canonical zero-fill for the same business value 0
    accepted = (tmp_path / "runs" / canon["run_id"] / "accepted.jsonl").read_text(encoding="utf-8")
    assert json.loads(accepted.splitlines()[0])["values"]["SCORE"] == "0"

    evidence = run_correct(tmp_path, manifest_file(tmp_path, source, items, name="evid"))
    evid_path = Path(evidence.to_dict()["output"]["path"])
    assert evid_path.read_bytes()[35:38] == b"   "  # evidence keeps the original padding
    assert evidence.to_dict()["output"]["sha256"] != canon["output"]["sha256"]

def test_non_terminal_source_run_is_rejected(tmp_path):
    records = [account_record(1)]
    source, input_path = source_run(tmp_path, records)
    options = RunOptions(input_path=input_path, copybook_path=ACCOUNT_CPY,
                         rules_path=ACCOUNT_RULES, out_root=tmp_path / "runs",
                         db_path=tmp_path / "ledger.sqlite", now=NOW)
    prepared = prepare_run(options)
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.delete_run(source.run_id)
    ledger.insert_run(run_spec(prepared, tmp_path / "runs" / prepared.run_id))
    manifest = manifest_file(tmp_path, source, [correction(input_path, 1, "SCORE", "80")])
    assert codes_of(failing_correct(tmp_path, manifest)) == {"CR001"}

def test_interrupted_publishing_batch_self_heals_on_resubmission(tmp_path):
    records = [account_record(1), account_record(2, region="EU")]
    source, input_path = source_run(tmp_path, records)
    manifest = manifest_file(tmp_path, source, [correction(input_path, 1, "SCORE", "090")])
    first = run_correct(tmp_path, manifest).to_dict()
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger._write([("UPDATE correction_batches SET status = 'publishing' WHERE batch_id = ?",
                    (first["batch_id"],))], "test setup")

    healed = run_correct(tmp_path, manifest)
    assert healed.idempotent is False and healed.exit_code == 0
    assert healed.to_dict()["batch_id"] == first["batch_id"]
    assert healed.to_dict()["run_id"] == first["run_id"]  # child run reused, not duplicated
    assert len(ledger.list_runs()) == 2 and len(ledger.list_correction_batches()) == 1
    assert ledger.get_correction_batch(first["batch_id"])["status"] == "published"

def test_signed_separate_and_overpunch_encoding():
    layout = compile_copybook(cpy(
        "01 R.",
        "05 A PIC S9(3) SIGN IS LEADING SEPARATE.",
        "05 B PIC S9(2)V9(1) SIGN IS TRAILING SEPARATE.",
        "05 C PIC S9(3).",
        "05 D PIC X(2).",
    ))
    assert layout.valid
    field_a, field_b = layout.find_field("A"), layout.find_field("B")
    assert encode_field(field_a, "-12", "utf-8") == b"-012"
    assert encode_field(field_a, "12", "utf-8") == b"+012"
    assert encode_field(field_b, "-1.5", "utf-8") == b"015-"
    with pytest.raises(EncodeError):
        encode_field(layout.find_field("C"), "-1", "utf-8")  # overpunch is not supported
    with pytest.raises(EncodeError):
        encode_field(layout.find_field("D"), "TOOLONG", "utf-8")
    with pytest.raises(EncodeError):
        encode_field(layout.find_field("D"), 5, "utf-8")

def test_encoding_honours_the_run_charset():
    layout = compile_copybook(cpy("01 R.", "05 N PIC 9(3).", "05 T PIC X(2)."))
    assert layout.valid
    assert encode_field(layout.find_field("N"), "7", "cp037") == b"\xf0\xf0\xf7"
    assert encode_field(layout.find_field("T"), "AB", "cp037") == b"\xc1\xc2"
    assert encode_field(layout.find_field("N"), "7", "utf-8") == b"007"

def test_cli_correct_json_exit_codes_and_idempotency(tmp_path, capsys):
    records = [account_record(1), account_record(2, score=150)]
    source, input_path = source_run(tmp_path, records)
    manifest = manifest_file(tmp_path, source, [correction(input_path, 2, "SCORE", "80")])
    db = str(tmp_path / "ledger.sqlite")

    code = cli.main(["correct", "--manifest", str(manifest), "--db", db,
                     "--now", "2026-01-01T00:00:00Z"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0 and payload["ok"] is True and payload["idempotent"] is False
    assert payload["review"]["counts"]["accepted_records"] == 2

    code = cli.main(["correct", "--manifest", str(manifest), "--db", db,
                     "--now", "2026-01-01T00:00:00Z"])
    assert code == 0 and json.loads(capsys.readouterr().out)["idempotent"] is True

    code = cli.main(["correct", "--manifest", str(tmp_path / "missing.yaml"), "--db", db])
    err = json.loads(capsys.readouterr().err)
    assert code == 2 and err["ok"] is False and err["errors"][0]["code"] == "CR001"

    unknown = tmp_path / "unknown-run.yaml"
    unknown.write_text(yaml.safe_dump({
        "version": 1, "source_run_id": "run-missing", "contract": "evidence",
        "corrections": [{"record_no": 1, "expected_sha256": "0" * 64,
                         "path": "SCORE", "value": "1"}]}), encoding="utf-8")
    code = cli.main(["correct", "--manifest", str(unknown), "--db", db])
    assert code == 2 and json.loads(capsys.readouterr().err)["code"] == "LD002"
