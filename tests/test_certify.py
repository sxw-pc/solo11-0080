"""Two-level copybook certification: per-record file proof and static universal proof."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import cpy
from copybook_kit.certify import (
    LEVEL_FILE_REUSE,
    LEVEL_UNIVERSAL,
    VERDICT_PASS,
    VERDICT_REJECT,
    VERDICT_UNKNOWN,
    CertificationInput,
    CertificateError,
    build_and_certify,
    certificate_digest,
    load_certificate,
    meets_level,
    resolve_field_paths,
    verify_certificate,
)
from copybook_kit.copybook.layout import compile_copybook
from copybook_kit.errors import CompileError, InputError
from copybook_kit.rules.loader import load_rules
from copybook_kit.util import sha256_bytes, write_json_atomic

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
MINIMAL_RULES = "version: 1\nname: cert-rules\nfields: {}\n"


@pytest.fixture
def workspace(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(MINIMAL_RULES, encoding="utf-8")
    return tmp_path


def _write_copybooks(workspace: Path, old: str, new: str) -> tuple[Path, Path]:
    old_path, new_path = workspace / "old.cpy", workspace / "new.cpy"
    old_path.write_text(old, encoding="utf-8")
    new_path.write_text(new, encoding="utf-8")
    return old_path, new_path


def _certify(workspace: Path, old: str, new: str, data: bytes, **kwargs):
    old_path, new_path = _write_copybooks(workspace, old, new)
    input_path = workspace / "data.dat"
    input_path.write_bytes(data)
    return build_and_certify(old_copybook_path=old_path, new_copybook_path=new_path,
                             rules_path=workspace / "rules.yaml", input_path=input_path,
                             charset=kwargs.pop("charset", "utf-8"), recfm="F",
                             field_paths=kwargs.pop("field_paths", None),
                             requested_level=kwargs.pop("requested_level", None), now=NOW)


def _material(workspace: Path, old_source: str, new_source: str, data: bytes,
              paths=None, level=None, charset="utf-8") -> CertificationInput:
    old_layout = compile_copybook(old_source)
    assert old_layout.valid, old_layout.to_dict()
    return CertificationInput(
        old_copybook_source=old_source, new_copybook_source=new_source,
        rules=load_rules(workspace / "rules.yaml", old_layout), input_data=data,
        charset=charset, recfm="F",
        field_paths=resolve_field_paths(old_layout, paths), requested_level=level,
        created_at="2026-01-01T00:00:00Z", input_name="data.dat",
        old_copybook_name="old.cpy", new_copybook_name="new.cpy", rules_name="rules.yaml",
        old_copybook_sha256=sha256_bytes(old_source.encode()),
        new_copybook_sha256=sha256_bytes(new_source.encode()),
        input_sha256=sha256_bytes(data),
        rules_sha256=sha256_bytes(MINIMAL_RULES.encode()))


def test_identical_layout_is_universally_replaceable(workspace):
    source = cpy("01 R.", "05 A PIC X(4).", "05 B PIC 9(2)V9(1).")
    certificate = _certify(workspace, source, source, b"AB  123")
    assert certificate["level"] == {"requested": None, LEVEL_FILE_REUSE: VERDICT_PASS,
                                    LEVEL_UNIVERSAL: VERDICT_PASS}
    fields = {item["path"].split(".", 1)[1]: item for item
              in certificate["universal_evaluation"]["fields"]}
    assert all(item["verdict"] == VERDICT_PASS for item in fields.values())
    evaluation = certificate["file_evaluation"]
    assert evaluation["records_compared"] == 1 and evaluation["counterexamples"] == []
    assert evaluation["legacy_rejected_records"] == 0
    # Evidence is real-byte based: field hash and record hash are reproducible.
    evidence = evaluation["field_evidence"]
    assert {item["path"] for item in evidence} == {"R.A", "R.B"}
    assert evidence[0]["field_sha8"] == sha256_bytes(b"AB  ")[:8]
    assert evidence[0]["record_sha256"] == sha256_bytes(b"AB  123")


def test_same_offset_same_value_compatible_across_multiple_records(workspace):
    old = cpy("01 R.", "05 CODE PIC X(4).", "05 NAME PIC X(3).")
    certificate = _certify(workspace, old, old, b"1234ABC" + b"5678DEF")
    assert certificate["level"][LEVEL_FILE_REUSE] == VERDICT_PASS
    assert certificate["level"][LEVEL_UNIVERSAL] == VERDICT_PASS
    assert certificate["file_evaluation"]["records_total"] == 2
    assert len(certificate["file_evaluation"]["field_evidence"]) == 4


def test_x4_to_94_file_passes_but_universal_is_rejected_with_witness(workspace):
    old = cpy("01 R.", "05 CODE PIC X(4).")
    new = cpy("01 R.", "05 CODE PIC 9(4).")
    certificate = _certify(workspace, old, new, b"1234" * 3, requested_level=LEVEL_FILE_REUSE)
    # The file happens to contain only digits: file-level reuse is provable.
    assert certificate["level"][LEVEL_FILE_REUSE] == VERDICT_PASS
    assert certificate["level"]["requested"] == LEVEL_FILE_REUSE
    # But the sample cannot prove universality; the static engine rejects with a witness.
    assert certificate["level"][LEVEL_UNIVERSAL] == VERDICT_REJECT
    field = certificate["universal_evaluation"]["fields"][0]
    assert field["verdict"] == VERDICT_REJECT
    witness = field["counterexample"]
    assert bytes.fromhex(witness["byte_hex"]) == b"AAAA"
    assert witness["old_value"] == "AAAA" and witness["new_value"] is None
    assert witness["new_decode_error"] == "DE001"
    assert not meets_level(certificate, LEVEL_UNIVERSAL)
    assert meets_level(certificate, LEVEL_FILE_REUSE)


def test_x4_to_94_with_alphabetic_record_fails_file_level_with_byte_ranges(workspace):
    old = cpy("01 R.", "05 CODE PIC X(4).")
    new = cpy("01 R.", "05 CODE PIC 9(4).")
    certificate = _certify(workspace, old, new, b"0001" + b"AB34")
    assert certificate["level"][LEVEL_FILE_REUSE] == VERDICT_REJECT
    examples = certificate["file_evaluation"]["counterexamples"]
    failure = next(item for item in examples if item["kind"] == "new_decode_failure")
    assert failure["record_no"] == 2 and failure["path"] == "R.CODE"
    assert failure["old_byte_range"] == {"offset": 0, "length": 4}
    assert failure["new_byte_range"] == {"offset": 0, "length": 4}
    assert failure["field_hex"] == "41423334"
    assert failure["record_sha256"] == sha256_bytes(b"AB34")
    assert failure["error"]["code"] == "DE001"
    # The digit-only first record still changes meaning: text "0001" vs number 1.
    change = next(item for item in examples if item["kind"] == "value_change")
    assert change["record_no"] == 1
    assert change["old_value"] == "0001" and change["new_value"] == "1"


def test_occurs_reposition_misreads_field_values(workspace):
    old = cpy("01 R.", "05 P OCCURS 2 TIMES.", "10 X PIC X(2).", "10 Y PIC 9(2).")
    new = cpy("01 R.", "05 P OCCURS 2 TIMES.", "10 Y PIC 9(2).", "10 X PIC X(2).")
    certificate = _certify(workspace, old, new, b"AB34CD56")
    assert certificate["level"][LEVEL_FILE_REUSE] == VERDICT_REJECT
    paths = {item["path"]: item for item in certificate["file_evaluation"]["counterexamples"]}
    assert "R.P[0].X" in paths and paths["R.P[0].X"]["old_value"] == "AB"
    assert paths["R.P[0].X"]["new_value"] == "34"
    # Static proof independently rejects every moved path.
    assert all(item["verdict"] == VERDICT_REJECT
               for item in certificate["universal_evaluation"]["fields"])
    moved = next(item for item in certificate["universal_evaluation"]["fields"]
                 if item["path"] == "R.P[0].X")
    assert moved["counterexample"]["kind"] == "storage_moved"


def test_occurs_count_change_moves_record_boundary(workspace):
    old = cpy("01 R.", "05 P OCCURS 2 TIMES PIC X(2).", "05 TAIL PIC X(2).")
    new = cpy("01 R.", "05 P OCCURS 3 TIMES PIC X(2).", "05 TAIL PIC X(2).")
    certificate = _certify(workspace, old, new, b"ABCDEF")
    evaluation = certificate["file_evaluation"]
    assert evaluation["record_length"] == {"old": 6, "new": 8, "boundaries_preserved": False}
    assert evaluation["counterexamples"][0]["kind"] == "record_boundary"
    assert certificate["level"][LEVEL_FILE_REUSE] == VERDICT_REJECT
    assert certificate["level"][LEVEL_UNIVERSAL] == VERDICT_REJECT


def test_redefines_shared_storage_is_unknown_not_pass(workspace):
    old = cpy("01 R.", "05 BASE PIC X(4).", "05 ALT REDEFINES BASE PIC X(4).")
    new = cpy("01 R.", "05 BASE PIC X(4).", "05 ALT REDEFINES BASE PIC 9(4).")
    # The single file decodes fine, so file-level reuse passes.
    certificate = _certify(workspace, old, new, b"1234")
    assert certificate["level"][LEVEL_FILE_REUSE] == VERDICT_PASS
    # Shared storage reinterpretation cannot be proved statically -> unknown, never pass.
    assert certificate["level"][LEVEL_UNIVERSAL] == VERDICT_UNKNOWN
    assert all(item["verdict"] == VERDICT_UNKNOWN
               for item in certificate["universal_evaluation"]["fields"])
    assert all("REDEFINES" in item["reason"]
               for item in certificate["universal_evaluation"]["fields"])


def test_decimal_scale_change_rejected_with_decimal_witness(workspace):
    old = cpy("01 R.", "05 N PIC 9(4).")
    new = cpy("01 R.", "05 N PIC 9(3)V9(1).")
    certificate = _certify(workspace, old, new, b"0001")
    field = certificate["universal_evaluation"]["fields"][0]
    assert field["verdict"] == VERDICT_REJECT
    witness = field["counterexample"]
    assert witness["old_value"] == "1" and witness["new_value"] == "0.1"
    change = certificate["file_evaluation"]["counterexamples"][0]
    assert change["kind"] == "value_change"


def test_legacy_rejected_records_are_isolated_evidence(workspace):
    old = cpy("01 R.", "05 N PIC 9(2).", "05 T PIC X(2).")
    certificate = _certify(workspace, old, old, b"ABxx" + b"34yy")
    evaluation = certificate["file_evaluation"]
    assert evaluation["records_total"] == 2
    assert evaluation["records_compared"] == 1
    assert evaluation["legacy_rejected_records"] == 1
    isolated = evaluation["legacy_rejects"][0]
    assert isolated["record_no"] == 1 and isolated["error_codes"] == ["DE001"]
    assert isolated["record_sha256"] == sha256_bytes(b"ABxx")
    # Isolated rejects must not masquerade as a compatibility pass or failure.
    assert evaluation["verdict"] == VERDICT_PASS
    assert evaluation["counterexamples"] == []


def test_charset_change_is_universally_rejected_with_real_byte_witness(workspace):
    from copybook_kit.certify.engine import static_assessment
    source = cpy("01 R.", "05 A PIC X(2).")
    layout = compile_copybook(source)
    for old_charset, new_charset in (("utf-8", "latin-1"), ("utf-8", "cp037"),
                                     ("latin-1", "cp037")):
        assessment = static_assessment("R.A", layout, layout, old_charset, new_charset)
        assert assessment["verdict"] == VERDICT_REJECT, (old_charset, new_charset)
        witness = assessment["counterexample"]
        raw = bytes.fromhex(witness["byte_hex"])
        # The witness must be legal under the old charset and really differ.
        assert raw.decode(old_charset) != raw.decode(new_charset)
        assert witness["byte_range"] == {"offset": 0, "length": 2}


def test_dropped_field_path_is_rejected_at_both_levels(workspace):
    # Keep the record length identical so the per-path check (not the boundary
    # guard) has to prove the removed field loses its meaning.
    old = cpy("01 R.", "05 KEEP PIC X(2).", "05 GONE PIC X(2).")
    new = cpy("01 R.", "05 KEEP PIC X(2).", "05 FILLER PIC X(2).")
    certificate = _certify(workspace, old, new, b"ABCD")
    examples = certificate["file_evaluation"]["counterexamples"]
    assert any(item["kind"] == "missing_path" and item["path"] == "R.GONE" for item in examples)
    gone = next(item for item in certificate["universal_evaluation"]["fields"]
                if item["path"] == "R.GONE")
    assert gone["verdict"] == VERDICT_REJECT and gone["new_byte_range"] is None


def test_unknown_field_path_is_rejected(workspace):
    source = cpy("01 R.", "05 A PIC X(2).")
    layout = compile_copybook(source)
    with pytest.raises(InputError) as caught:
        resolve_field_paths(layout, ["NO-SUCH-FIELD"])
    assert caught.value.code == "CLI001"


def test_invalid_new_copybook_raises_compile_error(workspace):
    good = cpy("01 R.", "05 A PIC X(2).")
    bad = cpy("01 R.", "05 A PIC 9(2)V99.")
    with pytest.raises(CompileError):
        _certify(workspace, good, bad, b"AB")


def test_certificate_digest_covers_every_material_and_claim(workspace):
    old = cpy("01 R.", "05 CODE PIC X(4).")
    new = cpy("01 R.", "05 CODE PIC 9(4).")
    first = _certify(workspace, old, new, b"1234")
    assert first["digest"] == certificate_digest(first)
    assert len(first["digest"]) == 64 and first["certificate_id"].startswith("cert-")

    # Any change to input, old or new copybook moves the digest.
    changed_input = _certify(workspace, old, new, b"1235")
    changed_new = _certify(workspace, old, cpy("01 R.", "05 CODE PIC X(4)."), b"1234")
    changed_old = _certify(workspace, cpy("01 R.", "05 CODE PIC X(5)."),
                           cpy("01 R.", "05 CODE PIC 9(4)."), b"1234 ")
    assert {changed_input["digest"], changed_new["digest"], changed_old["digest"]} == \
        {changed_input["digest"], changed_new["digest"], changed_old["digest"]}
    assert first["digest"] not in {changed_input["digest"], changed_new["digest"],
                                   changed_old["digest"]}


def test_tampered_certificate_fails_self_check_and_material_verification(workspace):
    old = cpy("01 R.", "05 CODE PIC X(4).")
    new = cpy("01 R.", "05 CODE PIC 9(4).")
    old_path, new_path = _write_copybooks(workspace, old, new)
    data = workspace / "data.dat"
    data.write_bytes(b"1234")
    cert_path = workspace / "cert.json"
    certificate = build_and_certify(old_copybook_path=old_path, new_copybook_path=new_path,
                                    rules_path=workspace / "rules.yaml", input_path=data,
                                    charset="utf-8", recfm="F", field_paths=None,
                                    requested_level=LEVEL_FILE_REUSE, now=NOW)
    write_json_atomic(cert_path, certificate)
    assert load_certificate(cert_path)["digest"] == certificate["digest"]

    tampered = json.loads(cert_path.read_text(encoding="utf-8"))
    tampered["digest"] = "0" * 64
    write_json_atomic(workspace / "tampered.json", tampered)
    with pytest.raises(CertificateError) as caught:
        load_certificate(workspace / "tampered.json")
    assert caught.value.code == "CF003"

    # A different input file with the same paths/names fails raw-byte verification.
    other = workspace / "other.dat"
    other.write_bytes(b"5678")
    with pytest.raises(CertificateError) as caught:
        verify_certificate(certificate, input_path=other, old_copybook_path=old_path,
                           new_copybook_path=new_path, rules_path=workspace / "rules.yaml")
    assert caught.value.code == "CF001"

    # The genuine materials verify cleanly on content, regardless of file names.
    renamed = workspace / "copied-input.bin"
    renamed.write_bytes(data.read_bytes())
    verify_certificate(certificate, input_path=renamed, old_copybook_path=old_path,
                       new_copybook_path=new_path, rules_path=workspace / "rules.yaml")


def test_certificate_routes_do_not_claim_each_other(workspace):
    old = cpy("01 R.", "05 CODE PIC X(4).")
    new = cpy("01 R.", "05 CODE PIC 9(4).")
    certificate = _certify(workspace, old, new, b"1234", requested_level=LEVEL_FILE_REUSE)
    authority = certificate["authorization"]
    assert "this input file only" in authority[LEVEL_FILE_REUSE]["scope"]
    assert "every legal record" in authority[LEVEL_UNIVERSAL]["scope"]
    assert authority[LEVEL_FILE_REUSE]["forbids"].startswith("enabling")
    assert certificate["universal_evaluation"]["method"].startswith("static proof")


def test_certificate_document_shape_and_material_hashes(workspace):
    old = cpy("01 R.", "05 CODE PIC X(4).")
    new = cpy("01 R.", "05 CODE PIC 9(4).")
    old_path, new_path = _write_copybooks(workspace, old, new)
    data = workspace / "data.dat"
    data.write_bytes(b"1234")
    certificate = build_and_certify(old_copybook_path=old_path, new_copybook_path=new_path,
                                    rules_path=workspace / "rules.yaml", input_path=data,
                                    charset="utf-8", recfm="F", field_paths=None,
                                    requested_level=None, now=NOW)
    assert certificate["schema_version"] == 1
    assert certificate["materials"]["input"]["sha256"] == sha256_bytes(b"1234")
    assert certificate["materials"]["old_copybook"]["sha256"] == sha256_bytes(old.encode())
    assert certificate["materials"]["new_copybook"]["sha256"] == sha256_bytes(new.encode())
    assert certificate["materials"]["rules"]["sha256"] == sha256_bytes(MINIMAL_RULES.encode())
    assert certificate["materials"]["input"]["size"] == 4
    # Every comparison is byte-based: no file names or mtimes participate.
    assert "mtime" not in certificate["materials"]["input"]
    evaluation = certificate["file_evaluation"]
    assert set(evaluation) >= {"verdict", "record_length", "records_total", "records_compared",
                               "legacy_rejected_records", "counterexamples", "legacy_rejects",
                               "field_evidence", "read_issues"}
    evidence = evaluation["field_evidence"][0]
    assert set(evidence) == {"record_no", "byte_offset", "path", "byte_range", "field_sha8",
                             "record_sha256"}
    # The digest is stable under canonical re-serialization.
    assert certificate_digest(json.loads(json.dumps(certificate))) == certificate["digest"]


def test_explicit_field_paths_restrict_evaluation_scope(workspace):
    old = cpy("01 R.", "05 A PIC X(2).", "05 B PIC 9(2).")
    new = cpy("01 R.", "05 A PIC X(2).", "05 B PIC 9(2).")
    certificate = _certify(workspace, old, new, b"AB12", field_paths=["A"])
    assert certificate["field_paths"] == ["R.A"]
    assert [item["path"] for item in certificate["universal_evaluation"]["fields"]] == ["R.A"]
    assert {item["path"] for item in certificate["file_evaluation"]["field_evidence"]} == {"R.A"}

