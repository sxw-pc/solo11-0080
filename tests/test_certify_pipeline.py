"""Certificate-driven new/old contract selection in the run pipeline."""

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
    CertificateError,
    build_and_certify,
)
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.pipeline.runner import resume_run, run
from copybook_kit.store.ledger import Ledger
from copybook_kit.util import write_json_atomic

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RULES = "version: 1\nname: gate-rules\nfields: {}\n"


@pytest.fixture
def contract(tmp_path):
    old_cpy = tmp_path / "old.cpy"
    new_cpy = tmp_path / "new.cpy"
    rules = tmp_path / "rules.yaml"
    data = tmp_path / "data.dat"
    old_cpy.write_text(cpy("01 R.", "05 CODE PIC X(4)."), encoding="utf-8")
    new_cpy.write_text(cpy("01 R.", "05 CODE PIC 9(4)."), encoding="utf-8")
    rules.write_text(RULES, encoding="utf-8")
    data.write_bytes(b"1234" * 3)
    certificate = build_and_certify(
        old_copybook_path=old_cpy, new_copybook_path=new_cpy, rules_path=rules,
        input_path=data, charset="utf-8", recfm="F", field_paths=None,
        requested_level=LEVEL_FILE_REUSE, now=NOW)
    cert_path = tmp_path / "certificate.json"
    write_json_atomic(cert_path, certificate)
    return tmp_path, old_cpy, new_cpy, rules, data, cert_path, certificate


def _options(contract, tmp_path, *, level, **overrides):
    root, old_cpy, new_cpy, rules, data, cert_path, _ = contract
    params = dict(input_path=data, copybook_path=new_cpy, rules_path=rules,
                  out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite", now=NOW,
                  certificate_path=cert_path, required_level=level, old_copybook_path=old_cpy)
    params.update(overrides)
    return RunOptions(**params)


def test_universal_request_blocks_new_child_run(contract, tmp_path):
    with pytest.raises(CertificateError) as caught:
        run(_options(contract, tmp_path, level=LEVEL_UNIVERSAL))
    assert caught.value.code == "CF002"
    # Nothing was registered: the block happens before the ledger insert.
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").glob("*"))


def test_file_reuse_request_runs_and_records_certificate_everywhere(contract, tmp_path):
    _, _, _, _, _, _, certificate = contract
    result = run(_options(contract, tmp_path, level=LEVEL_FILE_REUSE))
    assert result.status == "completed" and result.exit_code == 0
    summary = result.contract_meta["certificate"]
    assert summary["digest"] == certificate["digest"]
    assert summary["file_reuse"] == VERDICT_PASS and summary["universal"] == VERDICT_REJECT
    run_dir = Path(result.outputs["summary"]).parent
    # The credential and the old contract bytes are kept with the child run.
    stored = json.loads((run_dir / "certificate.json").read_text(encoding="utf-8"))
    assert stored["digest"] == certificate["digest"]
    assert (run_dir / "old_copybook.cpy").read_bytes() == contract[1].read_bytes()
    assert "certificate" in result.outputs and "old_copybook" in result.outputs
    events = Ledger(tmp_path / "ledger.sqlite").get_events(result.run_id)
    attached = next(event for event in events if event["event"] == "certificate_attached")
    assert attached["detail"]["digest"] == certificate["digest"]
    # The run key embeds the certificate digest, distinct from an uncertified run.
    assert "certificate" not in run(RunOptions(
        input_path=contract[4], copybook_path=contract[2], rules_path=contract[3],
        out_root=tmp_path / "plain", db_path=tmp_path / "plain.sqlite", now=NOW)).contract_meta


def test_certified_and_plain_runs_have_distinct_run_keys(contract, tmp_path):
    certified = run(_options(contract, tmp_path, level=LEVEL_FILE_REUSE))
    plain = run(RunOptions(
        input_path=contract[4], copybook_path=contract[2], rules_path=contract[3],
        out_root=tmp_path / "plain", db_path=tmp_path / "plain.sqlite", now=NOW))
    assert certified.run_key != plain.run_key and certified.run_id != plain.run_id


def test_input_byte_change_invalidates_certificate(contract, tmp_path):
    root, old_cpy, new_cpy, rules, data, _, _ = contract
    changed = root / "changed.dat"
    changed.write_bytes(b"1234" * 2 + b"9999")
    with pytest.raises(CertificateError) as caught:
        run(_options(contract, tmp_path, level=LEVEL_FILE_REUSE, input_path=changed))
    assert caught.value.code == "CF001"


def test_new_copybook_byte_change_invalidates_certificate(contract, tmp_path):
    root, old_cpy, _, rules, data, cert_path, _ = contract
    swapped = root / "swapped.cpy"
    swapped.write_text(cpy("01 R.", "05 CODE PIC X(4)."), encoding="utf-8")
    with pytest.raises(CertificateError) as caught:
        run(_options(contract, tmp_path, level=LEVEL_FILE_REUSE, copybook_path=swapped))
    assert caught.value.code == "CF001"


def test_tampered_certificate_digest_blocks_run(contract, tmp_path):
    root, *_ = contract
    tampered = json.loads(contract[5].read_text(encoding="utf-8"))
    tampered["digest"] = "f" * 64
    bad_path = root / "bad.json"
    write_json_atomic(bad_path, tampered)
    with pytest.raises(CertificateError) as caught:
        run(_options(contract, tmp_path, level=LEVEL_FILE_REUSE, certificate_path=bad_path))
    assert caught.value.code == "CF003"


def test_missing_certificate_arguments_are_input_errors(contract, tmp_path):
    root, old_cpy, new_cpy, rules, data, cert_path, _ = contract
    base = dict(input_path=data, copybook_path=new_cpy, rules_path=rules,
                out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite", now=NOW)
    # Level requested without a certificate.
    with pytest.raises(CertificateError) as caught:
        run(RunOptions(**base, required_level=LEVEL_FILE_REUSE))
    assert caught.value.code == "CF002"
    # Certificate supplied without a level.
    with pytest.raises(CertificateError) as caught:
        run(RunOptions(**base, certificate_path=cert_path, old_copybook_path=old_cpy))
    assert caught.value.code == "CF002"


def test_certified_resume_replays_gate_and_keeps_lineage(contract, tmp_path):
    result = run(_options(contract, tmp_path, level=LEVEL_FILE_REUSE))
    resumed = resume_run(result.run_id, tmp_path / "ledger.sqlite", NOW)
    assert resumed.idempotent and resumed.run_id == result.run_id
    assert resumed.contract_meta["certificate"]["digest"] == contract[6]["digest"]


def test_interrupted_certified_run_resumes_with_preserved_artifacts(contract, tmp_path):
    from copybook_kit.pipeline.options import prepare_run, run_spec
    options = _options(contract, tmp_path, level=LEVEL_FILE_REUSE)
    prepared, ledger = prepare_run(options), Ledger(options.db_path)
    run_dir = options.out_root / prepared.run_id
    ledger.insert_run(run_spec(prepared, run_dir))
    ledger.mark_failed(prepared.run_id, "2026-01-01T00:00:00Z", "simulated interruption")

    # Without the gate artifacts resume refuses rather than silently dropping the gate.
    with pytest.raises(Exception) as caught:
        resume_run(prepared.run_id, options.db_path, NOW)
    assert getattr(caught.value, "code", None) == "LD001"

    # Simulate _execute having persisted its gate artifacts before interruption.
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(run_dir / "certificate.json", contract[6])
    (run_dir / "old_copybook.cpy").write_bytes(contract[1].read_bytes())
    resumed = resume_run(prepared.run_id, options.db_path, NOW)
    assert resumed.status == "completed"
    assert resumed.contract_meta["certificate"]["digest"] == contract[6]["digest"]
    # Artifacts survive the run-directory wipe and the event is recorded exactly once.
    assert (run_dir / "certificate.json").is_file()
    assert (run_dir / "old_copybook.cpy").read_bytes() == contract[1].read_bytes()
    events = [event["event"] for event in ledger.get_events(prepared.run_id)]
    assert events.count("certificate_attached") == 1


def test_plain_run_and_reprocess_keep_historical_behavior(contract, tmp_path):
    # No certificate anywhere: params stay byte-identical to the legacy shape.
    plain = run(RunOptions(
        input_path=contract[4], copybook_path=contract[2], rules_path=contract[3],
        out_root=tmp_path / "legacy", db_path=tmp_path / "legacy.sqlite", now=NOW,
        kind="reprocess", parent_run_id=None))
    assert "certificate" not in plain.contract_meta
    row = Ledger(tmp_path / "legacy.sqlite").get_run(plain.run_id)
    params = json.loads(row["params_json"])
    assert params["certificate_digest"] == "" and params["required_level"] == ""
    assert params["old_copybook_sha256"] == ""
