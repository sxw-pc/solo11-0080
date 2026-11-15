"""Run options, input preparation, fingerprints and summary reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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

def _read(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read {label} {path}: {exc}", code="IO001") from exc

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
    params = {
        "schema_version": 1, "input_sha256": input_sha, "copybook_sha256": copybook_sha,
        "rules_sha256": rules_sha, "charset": charset, "recfm": options.recfm,
        "max_records": options.max_records, "parent_run_id": options.parent_run_id or "",
        "kind": options.kind,
    }
    run_key = sha256_text(canonical_json(params))
    return PreparedRun(options, layout, rules, input_data, input_sha, copybook_sha,
                       rules_sha, charset, options.recfm, params, run_key, f"run-{run_key[:20]}")

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
