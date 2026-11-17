"""Run options, input preparation, fingerprints and summary reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..certify import (
    LEVEL_FILE_REUSE,
    LEVEL_UNIVERSAL,
    CertificateError,
    certificate_summary,
    load_certificate,
    require_level,
    verify_certificate,
)
from ..copybook.layout import compile_copybook
from ..decode.charset import normalize_charset
from ..errors import CompileError, InputError
from ..model import FileIssue, Layout, RunResult
from ..rules.loader import load_rules
from ..rules.schema import RuleSet
from ..util import canonical_json, sha256_bytes, sha256_text, utc_text

@dataclass
class RunOptions:
    input_path: Path
    copybook_path: Path
    rules_path: Path
    out_root: Path
    db_path: Path
    now: datetime
    charset: str = "utf-8"
    recfm: str = "F"
    max_records: int = 0
    force: bool = False
    parent_run_id: str | None = None
    kind: str = "ingest"
    notes: str | None = None
    # New/old contract selection entry point. A run without a certificate keeps
    # the historical behavior exactly; a certified child run is gated and traced.
    certificate_path: Path | None = None
    required_level: str | None = None
    old_copybook_path: Path | None = None

@dataclass
class PreparedRun:
    options: RunOptions
    layout: Layout
    rules: RuleSet
    input_data: bytes
    input_sha256: str
    copybook_sha256: str
    rules_sha256: str
    charset: str
    recfm: str
    params: dict[str, Any]
    run_key: str
    run_id: str
    certificate: dict[str, Any] | None = None
    certificate_summary: dict[str, Any] | None = None

def _read(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read {label} {path}: {exc}", code="IO001") from exc

def _gate_certificate(options: RunOptions, *, input_sha: str, new_copybook_sha: str,
                      rules_sha: str) -> dict[str, Any]:
    """Validate the replacement credential before any new-copybook child run starts."""
    if options.required_level not in (None, LEVEL_FILE_REUSE, LEVEL_UNIVERSAL):
        raise InputError(f"unknown certification level {options.required_level!r}", code="CLI001")
    if options.certificate_path is None:
        if options.required_level is not None or options.old_copybook_path is not None:
            raise CertificateError("a certified new-copybook run needs --certificate and "
                                   "--old-copybook", code="CF002")
        raise CertificateError("certificate gating invoked without a certificate", code="CF003")
    if options.required_level is None:
        raise CertificateError("--certificate requires --required-level file-reuse|universal",
                               code="CF002")
    if options.old_copybook_path is None:
        raise CertificateError("--certificate requires --old-copybook", code="CF002")
    certificate = load_certificate(options.certificate_path)
    verify_certificate(
        certificate,
        input_path=options.input_path, old_copybook_path=options.old_copybook_path,
        new_copybook_path=options.copybook_path, rules_path=options.rules_path)
    materials = certificate["materials"]
    if (materials["input"]["sha256"] != input_sha
            or materials["new_copybook"]["sha256"] != new_copybook_sha
            or materials["rules"]["sha256"] != rules_sha):
        raise CertificateError(
            "certificate material hashes do not match this run's input, copybook or rules",
            code="CF001")
    require_level(certificate, options.required_level)
    return certificate


def prepare_run(options: RunOptions) -> PreparedRun:
    try:
        charset = normalize_charset(options.charset)
    except ValueError as exc:
        raise InputError(str(exc), code="CLI001") from exc
    if options.recfm not in {"F", "FB"}:
        raise InputError("--recfm must be F or FB", code="CLI001")
    if options.max_records < 0:
        raise InputError("--max-records must be non-negative", code="CLI001")
    copybook_bytes = _read(options.copybook_path, "copybook")
    try:
        copybook_source = copybook_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"copybook must be UTF-8 text: {exc}", code="CB008") from exc
    layout = compile_copybook(copybook_source)
    if not layout.valid:
        raise CompileError(layout.errors)
    rules = load_rules(options.rules_path, layout)
    input_data = _read(options.input_path, "input file")
    rules_bytes = _read(options.rules_path, "rules")
    input_sha, copybook_sha, rules_sha = map(
        sha256_bytes, (input_data, copybook_bytes, rules_bytes))
    certificate = None
    cert_summary = None
    old_copybook_sha = ""
    if options.certificate_path is not None or options.required_level is not None:
        certificate = _gate_certificate(
            options, input_sha=input_sha, new_copybook_sha=copybook_sha, rules_sha=rules_sha)
        cert_summary = certificate_summary(certificate)
        old_copybook_sha = cert_summary["old_copybook_sha256"]
    params = {
        "schema_version": 1, "input_sha256": input_sha, "copybook_sha256": copybook_sha,
        "rules_sha256": rules_sha, "charset": charset, "recfm": options.recfm,
        "max_records": options.max_records, "parent_run_id": options.parent_run_id or "",
        "kind": options.kind,
        "certificate_digest": cert_summary["digest"] if cert_summary else "",
        "required_level": options.required_level or "",
        "old_copybook_sha256": old_copybook_sha,
    }
    run_key = sha256_text(canonical_json(params))
    return PreparedRun(options, layout, rules, input_data, input_sha, copybook_sha,
                       rules_sha, charset, options.recfm, params, run_key,
                       f"run-{run_key[:20]}", certificate, cert_summary)

def run_spec(prepared: PreparedRun, out_dir: Path) -> dict[str, Any]:
    options, now_text = prepared.options, utc_text(prepared.options.now)
    return {
        "run_key": prepared.run_key, "run_id": prepared.run_id, "kind": options.kind,
        "parent_run_id": options.parent_run_id, "created_at": now_text, "updated_at": now_text,
        "status": "running", "exit_code": 0, "input_path": str(options.input_path.resolve()),
        "input_name": options.input_path.name, "input_sha256": prepared.input_sha256,
        "input_size": len(prepared.input_data),
        "copybook_path": str(options.copybook_path.resolve()),
        "copybook_name": options.copybook_path.name, "copybook_sha256": prepared.copybook_sha256,
        "rules_path": str(options.rules_path.resolve()), "rules_name": options.rules_path.name,
        "rules_sha256": prepared.rules_sha256, "params_json": canonical_json(prepared.params),
        "params_sha256": sha256_text(canonical_json(prepared.params)),
        "record_length": prepared.layout.record_length, "charset": prepared.charset,
        "recfm": prepared.recfm, "max_records": options.max_records,
        "out_dir": str(out_dir.resolve()), "summary_path": None, "report_path": None,
        "html_path": None, "profile_path": None, "accepted_path": None, "rejects_path": None,
        "total_records": 0, "data_records": 0, "accepted_records": 0,
        "accepted_data_records": 0, "rejected_records": 0, "file_issue_count": 0,
        "reject_codes_json": "{}", "notes": options.notes,
    }

def result_from_summary(summary: dict[str, Any], *, idempotent: bool) -> RunResult:
    issues = [FileIssue(item["code"], item["message"], item.get("severity", "error"),
                        item.get("byte_offset"), item.get("record_no"), item.get("detail", {}))
              for item in summary.get("file_issues", [])]
    return RunResult(
        summary["run_id"], summary["run_key"], summary["status"], summary["generated_at"],
        {key: int(value) for key, value in summary.get("counts", {}).items()},
        summary.get("outputs", {}), issues, summary.get("reject_codes", {}),
        summary.get("input", {}), summary.get("contracts", {}), summary.get("parent_run_id"),
        int(summary.get("exit_code", 0)), idempotent,
    )
