"""End-to-end orchestration: prepare, process, report, persist artifacts."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from ..errors import LedgerError
from ..model import RunResult
from ..report.html import render_html
from ..report.markdown import render_markdown
from ..store.ledger import Ledger
from ..util import utc_text, write_json_atomic
from .options import PreparedRun, RunOptions, prepare_run, result_from_summary, run_spec
from .outputs import OUTPUT_NAMES, write_artifacts
from .process import process_run
from .profile import build_profile

TERMINAL_STATUSES = {"completed", "completed_with_rejects"}
CERTIFICATE_ARTIFACT = "certificate.json"
OLD_COPYBOOK_ARTIFACT = "old_copybook.cpy"

def _load_summary(run: dict) -> RunResult:
    path_text = run.get("summary_path")
    if not path_text:
        raise LedgerError(f"run {run['run_id']} has no summary artifact; use --force or resume", code="LD001")
    try:
        summary = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read summary for run {run['run_id']}: {exc}", code="LD001") from exc
    return result_from_summary(summary, idempotent=True)

def _is_terminal(run: dict) -> bool:
    return run["status"] in TERMINAL_STATUSES or (run["status"] == "failed" and bool(run.get("summary_path")))

def _summary_result(prepared: PreparedRun, processed, generated_at: str) -> RunResult:
    options = prepared.options
    input_meta = {
        "name": options.input_path.name, "sha256": prepared.input_sha256,
        "size": len(prepared.input_data), "record_length": prepared.layout.record_length,
        "charset": prepared.charset, "recfm": prepared.recfm, "max_records": options.max_records,
    }
    contracts = {
        "copybook": {"name": options.copybook_path.name, "sha256": prepared.copybook_sha256},
        "rules": {"name": options.rules_path.name, "sha256": prepared.rules_sha256},
    }
    if prepared.certificate_summary is not None:
        # The ledger-facing proof that this child run was authorized by a certificate.
        contracts["certificate"] = prepared.certificate_summary
    return RunResult(prepared.run_id, prepared.run_key, processed.status, generated_at,
                     processed.counts, dict(OUTPUT_NAMES), processed.file_issues,
                     processed.reject_codes, input_meta, contracts, options.parent_run_id,
                     processed.exit_code)

def _execute(prepared: PreparedRun, ledger: Ledger, *, run_id: str, run_dir: Path,
             created_at: str) -> RunResult:
    now = utc_text(prepared.options.now)
    run_dir.mkdir(parents=True, exist_ok=True)

    def checkpoint(next_record_no: int, byte_offset: int, state: dict) -> None:
        ledger.checkpoint(run_id, next_record_no=next_record_no, byte_offset=byte_offset,
                          accepted_bytes=0, rejected_bytes=0, state=state, updated_at=now)

    try:
        processed = process_run(prepared, run_dir, checkpoint=checkpoint)
        profile = build_profile(processed.accepted_data_records, now)
        result = _summary_result(prepared, processed, now)
        result.outputs = {name: str((run_dir / filename).resolve())
                          for name, filename in OUTPUT_NAMES.items()}
        if prepared.certificate is not None:
            # Keep the authorizing credential and the old contract bytes beside
            # the artifacts; the original input and the parent/old run remain
            # independently readable and re-runnable. On resume the artifacts
            # already exist, so never copy a path onto itself.
            cert_path = run_dir / CERTIFICATE_ARTIFACT
            if not cert_path.exists():
                write_json_atomic(cert_path, prepared.certificate)
            result.outputs["certificate"] = str(cert_path.resolve())
            old_copy_path = run_dir / OLD_COPYBOOK_ARTIFACT
            if not old_copy_path.exists():
                shutil.copyfile(prepared.options.old_copybook_path, old_copy_path)
            result.outputs["old_copybook"] = str(old_copy_path.resolve())
            known_events = {event["event"] for event in ledger.get_events(run_id)}
            if "certificate_attached" not in known_events:
                ledger.add_event(prepared.run_id, "certificate_attached",
                                 prepared.certificate_summary, now)
        summary = result.to_dict()
        paths = write_artifacts(
            run_dir, layout=prepared.layout, accepted_records=processed.accepted_records,
            reject_entries=processed.reject_entries, profile=profile, summary=summary,
            markdown=render_markdown(summary, profile), html=render_html(summary, profile))
        for index, entry in enumerate(processed.reject_entries, start=1):
            ledger.add_reject(run_id, index, entry)
        for index, issue in enumerate(processed.file_issues, start=1):
            ledger.add_file_issue(run_id, index, issue.to_dict())
        ledger.checkpoint(
            run_id, next_record_no=len(processed.records) + 1, byte_offset=len(prepared.input_data),
            accepted_bytes=paths["accepted"].stat().st_size,
            rejected_bytes=paths["rejects"].stat().st_size,
            state={"counts": processed.counts, "reject_codes": processed.reject_codes}, updated_at=now)
        outputs_abs = {name: str(path.resolve()) for name, path in paths.items()}
        # Certificate/old-copybook artifacts are written above and must reach
        # the ledger outputs exactly as the summary recorded them.
        if prepared.certificate is not None:
            outputs_abs.update(result.outputs)
        ledger.finish_run(run_id, status=processed.status, exit_code=processed.exit_code,
                          counts=processed.counts, reject_codes=processed.reject_codes,
                          outputs=outputs_abs, updated_at=now)
        ledger.add_event(run_id, "run_finished",
                         {"status": processed.status, "counts": processed.counts,
                          "created_at": created_at}, now)
        return result
    except Exception as exc:
        try:
            ledger.mark_failed(run_id, now, f"{type(exc).__name__}: {exc}")
        finally:
            raise

def run(options: RunOptions) -> RunResult:
    prepared = prepare_run(options)
    ledger = Ledger(options.db_path)
    existing = ledger.get_run_by_key(prepared.run_key)
    if existing is not None and not options.force:
        if existing["run_id"] != prepared.run_id:
            raise LedgerError("run id prefix collision", code="LD003")
        if _is_terminal(existing):
            return _load_summary(existing)
        raise LedgerError(f"run {prepared.run_id} is incomplete; use 'resume --run-id "
                          f"{prepared.run_id}'", code="LD001")
    if existing is not None:
        ledger.delete_run(existing["run_id"])
        shutil.rmtree(existing["out_dir"], ignore_errors=True)
    collision = ledger.get_run(prepared.run_id)
    if collision is not None:
        raise LedgerError(f"run id {prepared.run_id} already belongs to a different run key", code="LD003")
    run_dir = options.out_root / prepared.run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    spec = run_spec(prepared, run_dir)
    ledger.insert_run(spec)
    ledger.add_event(prepared.run_id, "run_started",
                     {"kind": options.kind, "parent_run_id": options.parent_run_id}, spec["created_at"])
    return _execute(prepared, ledger, run_id=prepared.run_id, run_dir=run_dir,
                    created_at=spec["created_at"])

def resume_run(run_id: str, db_path: Path, now: datetime) -> RunResult:
    ledger = Ledger(db_path)
    existing = ledger.get_run(run_id)
    if existing is None:
        raise LedgerError(f"run {run_id} not found", code="LD002")
    if _is_terminal(existing):
        return _load_summary(existing)
    params = json.loads(existing["params_json"])
    certificate_path = None
    required_level = params.get("required_level") or None
    old_copybook_path = None
    if required_level is not None:
        # Re-attach the exact credential the run started with; the gate re-runs
        # and a tampered or missing certificate blocks the resume.
        certificate_path = Path(existing["out_dir"]) / CERTIFICATE_ARTIFACT
        if not certificate_path.is_file():
            raise LedgerError(f"run {run_id} lost its certificate artifact; cannot resume the "
                              "certified child run", code="LD001")
        old_copybook_path = Path(existing["out_dir"]) / OLD_COPYBOOK_ARTIFACT
        if not old_copybook_path.is_file():
            raise LedgerError(f"run {run_id} lost its old-copybook artifact; cannot resume the "
                              "certified child run", code="LD001")
    options = RunOptions(
        input_path=Path(existing["input_path"]), copybook_path=Path(existing["copybook_path"]),
        rules_path=Path(existing["rules_path"]), out_root=Path(existing["out_dir"]).parent,
        db_path=db_path, now=now, charset=existing["charset"], recfm=existing["recfm"],
        max_records=int(existing["max_records"]), parent_run_id=existing["parent_run_id"],
        kind=existing["kind"], notes=existing.get("notes"),
        certificate_path=certificate_path, required_level=required_level,
        old_copybook_path=old_copybook_path)
    prepared = prepare_run(options)
    if prepared.run_key != existing["run_key"]:
        raise LedgerError("inputs or rules changed since the interrupted run; start a new run", code="LD003")
    ledger.reset_for_resume(run_id, utc_text(now))
    ledger.add_event(run_id, "run_resumed", {}, utc_text(now))
    run_dir = Path(existing["out_dir"])
    preserved: dict[str, bytes] = {}
    if required_level is not None:
        # The run directory is wiped below; preserve the sealed credential and
        # old contract so the gate artifacts survive the replay.
        for name in (CERTIFICATE_ARTIFACT, OLD_COPYBOOK_ARTIFACT):
            preserved[name] = (run_dir / name).read_bytes()
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, content in preserved.items():
        (run_dir / name).write_bytes(content)
    return _execute(prepared, ledger, run_id=run_id, run_dir=run_dir,
                    created_at=existing["created_at"])
