"""Staging, isolated re-review and atomic publication of a corrected export.

A correction batch never mutates the source input or the source run's reject
evidence.  The new file is encoded in an isolated staging directory and
re-reviewed there under the original contract; only when the new file, the
review outcome and the correction lineage are all readable is the batch
published as one child run.  Previously exported files are never overwritten.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..copybook.layout import compile_copybook
from ..errors import CompileError, CorrectionError, Diagnostic, InputError, LedgerError
from ..pipeline.options import RunOptions, prepare_run
from ..pipeline.process import process_run
from ..pipeline.runner import TERMINAL_STATUSES, run
from ..rules.loader import load_rules
from ..store.ledger import Ledger
from ..util import (
    canonical_json,
    json_safe,
    sha8,
    sha256_bytes,
    sha256_text,
    utc_text,
    write_bytes_atomic,
    write_json_atomic,
)
from .manifest import Manifest, load_manifest
from .plan import CorrectionPlan, build_plan

@dataclass
class CorrectOptions:
    manifest_path: Path
    db_path: Path
    now: datetime
    out_root: Path | None = None

@dataclass
class CorrectionReport:
    payload: dict[str, Any]
    exit_code: int
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload, "idempotent": self.idempotent}

def _read(path_text: str, label: str) -> bytes:
    try:
        return Path(path_text).read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read {label} {path_text}: {exc}", code="IO001") from exc

def _is_terminal(source: dict[str, Any]) -> bool:
    return (source["status"] in TERMINAL_STATUSES
            or (source["status"] == "failed" and bool(source.get("summary_path"))))

def _source_run(ledger: Ledger, manifest: Manifest) -> dict[str, Any]:
    source = ledger.get_run(manifest.source_run_id)
    if source is None:
        raise LedgerError(f"source run {manifest.source_run_id} not found", code="LD002")
    if not _is_terminal(source):
        raise CorrectionError([Diagnostic(
            "CR001", f"source run {manifest.source_run_id} is not terminal "
            f"(status={source['status']}); corrections require a finished run")])
    return source

def _check_digests(source: dict[str, Any]) -> tuple[bytes, bytes, bytes]:
    diagnostics: list[Diagnostic] = []
    input_data = _read(source["input_path"], "source input")
    if sha256_bytes(input_data) != source["input_sha256"]:
        diagnostics.append(Diagnostic(
            "CR002", f"source input {source['input_path']} no longer matches the digest "
            f"recorded for run {source['run_id']}"))
    copybook_bytes = _read(source["copybook_path"], "copybook")
    if sha256_bytes(copybook_bytes) != source["copybook_sha256"]:
        diagnostics.append(Diagnostic(
            "CR002", f"copybook {source['copybook_path']} changed since run "
            f"{source['run_id']}; the original contract cannot be re-reviewed"))
    rules_bytes = _read(source["rules_path"], "rules")
    if sha256_bytes(rules_bytes) != source["rules_sha256"]:
        diagnostics.append(Diagnostic(
            "CR002", f"rules {source['rules_path']} changed since run {source['run_id']}; "
            f"the original contract cannot be re-reviewed"))
    if diagnostics:
        raise CorrectionError(diagnostics)
    return input_data, copybook_bytes, rules_bytes

def _batch_id(manifest: Manifest, source: dict[str, Any]) -> str:
    fingerprint = {
        "schema_version": 1,
        "manifest_sha256": manifest.sha256,
        "source_run_id": source["run_id"],
        "source_input_sha256": source["input_sha256"],
        "copybook_sha256": source["copybook_sha256"],
        "rules_sha256": source["rules_sha256"],
    }
    return f"batch-{sha256_text(canonical_json(fingerprint))[:20]}"

def _evidence_payload(plan: CorrectionPlan) -> list[dict[str, Any]]:
    entries = []
    for item in plan.evidence:
        entry = {
            "record_no": item.record_no, "path": item.path,
            "record_offset": item.record_offset, "byte_offset": item.byte_offset,
            "length": item.length, "old_bytes": item.old_bytes.hex(),
            "new_bytes": item.new_bytes.hex(),
            "old_sha8": sha8(item.old_bytes),
            "new_sha8": sha8(item.new_bytes),
            "old_value": item.old_value, "new_value": item.new_value,
        }
        if item.old_decode_error:
            entry["old_decode_error"] = item.old_decode_error
        entries.append(json_safe(entry))
    return entries

def _report_payload(manifest: Manifest, source: dict[str, Any], batch_id: str,
                    plan: CorrectionPlan, export_path: Path, new_sha: str, new_size: int,
                    diffs: list[dict[str, Any]], verified: bool, result,
                    correction_path: Path, now_text: str) -> dict[str, Any]:
    return json_safe({
        "schema_version": 1, "ok": True, "batch_id": batch_id, "name": manifest.name,
        "contract": manifest.contract, "source_run_id": source["run_id"],
        "run_id": result.run_id, "generated_at": now_text,
        "input": {"path": source["input_path"], "sha256": source["input_sha256"]},
        "output": {"path": str(export_path), "sha256": new_sha, "size": new_size},
        "redefines": dict(sorted(plan.redefines.items())),
        "corrections": _evidence_payload(plan),
        "diff_ranges": diffs,
        "unchanged_bytes_verified": verified,
        "review": {"run_id": result.run_id, "status": result.status,
                   "exit_code": result.exit_code, "counts": result.counts,
                   "reject_codes": result.reject_codes},
        "outputs": {"export": str(export_path), "correction": str(correction_path)},
    })

def _published_report(batch: dict[str, Any]) -> CorrectionReport:
    output_path = Path(batch["output_path"])
    if not output_path.is_file() or sha256_bytes(output_path.read_bytes()) != batch["output_sha256"]:
        raise LedgerError(
            f"previously exported file {output_path} is missing or altered; "
            f"batch {batch['batch_id']} cannot be replayed", code="IO001")
    report = batch["report"]
    return CorrectionReport(report, int(report["review"]["exit_code"]), idempotent=True)

def correct(options: CorrectOptions) -> CorrectionReport:
    manifest = load_manifest(options.manifest_path)
    ledger = Ledger(options.db_path)
    source = _source_run(ledger, manifest)
    input_data, copybook_bytes, _rules_bytes = _check_digests(source)
    out_root = options.out_root or Path(source["out_dir"]).parent
    try:
        layout = compile_copybook(copybook_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise InputError(f"copybook must be UTF-8 text: {exc}", code="CB008") from exc
    if not layout.valid:
        raise CompileError(layout.errors)
    rules = load_rules(source["rules_path"], layout)
    plan = build_plan(manifest, layout, input_data, charset=source["charset"],
                      blank_policies=rules.blank_policies)
    new_data, diffs, verified = plan.apply(input_data)
    new_sha = sha256_bytes(new_data)
    batch_id = _batch_id(manifest, source)

    existing = ledger.get_correction_batch(batch_id)
    if existing is not None and existing["status"] == "published":
        return _published_report(existing)
    if existing is not None:
        ledger.delete_correction_batch(batch_id)

    now_text = utc_text(options.now)
    staging = out_root / f".staging-{batch_id}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        staging_input = staging / "corrected.dat"
        write_bytes_atomic(staging_input, new_data)
        child = RunOptions(
            input_path=staging_input, copybook_path=Path(source["copybook_path"]),
            rules_path=Path(source["rules_path"]), out_root=out_root, db_path=options.db_path,
            now=options.now, charset=source["charset"], recfm=source["recfm"],
            max_records=int(source["max_records"]), parent_run_id=source["run_id"],
            kind="correct", notes=f"correction batch {batch_id} of {source['run_id']}")
        prepared = prepare_run(child)
        review = process_run(prepared, staging / "review")
        export_path = out_root / "exports" / f"corrected-{prepared.run_id}.dat"
        if export_path.exists():
            if sha256_bytes(export_path.read_bytes()) != new_sha:
                raise InputError(f"refusing to overwrite previously exported file "
                                 f"{export_path}", code="IO001")
        else:
            write_bytes_atomic(export_path, new_data)
        ledger.insert_correction_batch({
            "batch_id": batch_id, "run_id": prepared.run_id,
            "source_run_id": source["run_id"], "contract": manifest.contract,
            "status": "publishing", "manifest_sha256": manifest.sha256,
            "manifest": {"name": manifest.name, "source_run_id": manifest.source_run_id,
                         "contract": manifest.contract, "redefines": plan.redefines,
                         "corrections": [item.__dict__ for item in manifest.corrections]},
            "diff_ranges": diffs, "report": {},
            "output_path": str(export_path), "output_sha256": new_sha,
            "created_at": now_text, "updated_at": now_text})
        try:
            result = run(replace(child, input_path=export_path))
        except Exception:
            try:
                ledger.update_correction_batch(batch_id, status="failed", updated_at=now_text)
            finally:
                raise
        if review.counts != result.counts or review.status != result.status:
            raise LedgerError("isolated review and published run diverge; "
                              "the batch is left unpublished", code="LD003")
        correction_path = out_root / prepared.run_id / "correction.json"
        payload = _report_payload(manifest, source, batch_id, plan, export_path, new_sha,
                                  len(new_data), diffs, verified, result, correction_path,
                                  now_text)
        ledger.update_correction_batch(batch_id, status="published", report=payload,
                                       updated_at=now_text)
        write_json_atomic(correction_path, payload)
        return CorrectionReport(payload, result.exit_code)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
