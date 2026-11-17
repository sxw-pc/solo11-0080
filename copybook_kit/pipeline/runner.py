"""End-to-end orchestration: prepare, process, report, persist artifacts.

Rerunning an existing run key with ``force`` is a recoverable publish flow, not an
in-place replacement. Each attempt (round) builds in its own isolated directory
under ``<out>/<run-id>/``; the old round stays published and fully readable until
the new round passes the publish gate and the ``publications`` pointer switches in
a single ledger statement. Recovery after a crash at any cut point is determined
by that pointer plus the staged round's state:

1. artifacts half written  -> staged round is not terminal: reset and replay it;
2. artifacts complete, ledger not switched -> staged round is terminal: verify and publish;
3. ledger switched, old files not cleaned -> reconcile finishes the authorized cleanup.

Repeated ``--force`` and ``resume --run-id`` only ever continue the staged round;
the published round is never treated as an unfinished task.
"""

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
from ..store.queries import ARTIFACT_PATH_FIELDS, run_is_terminal
from ..util import utc_text
from .options import PreparedRun, RunOptions, prepare_run, result_from_summary, run_spec
from .outputs import OUTPUT_NAMES, write_artifacts
from .process import process_run
from .profile import build_profile

COUNT_COLUMNS = {"total_records": "total_records", "data_records": "data_records",
                 "accepted_records": "accepted_records",
                 "accepted_data_records": "accepted_data_records",
                 "rejected_records": "rejected_records", "file_issues": "file_issue_count"}

def _load_summary(run: dict) -> RunResult:
    path_text = run.get("summary_path")
    if not path_text:
        raise LedgerError(f"run {run['run_id']} has no summary artifact; use --force or resume", code="LD001")
    try:
        summary = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read summary for run {run['run_id']}: {exc}", code="LD001") from exc
    return result_from_summary(summary, idempotent=True)

def _summary_result(prepared: PreparedRun, processed, generated_at: str, run_id: str) -> RunResult:
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
    return RunResult(run_id, prepared.run_key, processed.status, generated_at,
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
        result = _summary_result(prepared, processed, now, run_id)
        result.outputs = {name: str((run_dir / filename).resolve())
                          for name, filename in OUTPUT_NAMES.items()}
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

def _verify_publishable(ledger: Ledger, run_row: dict) -> None:
    """Publish gate: every artifact exists, the summary re-reads, and the ledger's
    rejects, file issues and artifact references all belong to this round."""
    run_id = run_row["run_id"]
    if not run_is_terminal(run_row):
        raise LedgerError(f"run {run_id} is not complete and cannot be published", code="LD004")
    problems: list[str] = []
    paths: dict[str, Path] = {}
    for name, field in ARTIFACT_PATH_FIELDS.items():
        text = run_row.get(field)
        if not text or not Path(text).is_file():
            problems.append(f"{name} artifact missing")
        else:
            paths[name] = Path(text)
    if not (Path(run_row["out_dir"]) / "layout.json").is_file():
        problems.append("layout artifact missing")
    summary = None
    if "summary" in paths:
        try:
            summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"summary unreadable: {exc}")
    if summary is not None:
        if summary.get("run_id") != run_id:
            problems.append("summary run_id does not match the ledger row")
        if summary.get("status") != run_row["status"]:
            problems.append("summary status does not match the ledger row")
        counts = summary.get("counts", {})
        for key, column in COUNT_COLUMNS.items():
            if int(counts.get(key, -1)) != int(run_row[column]):
                problems.append(f"count {key} does not match the ledger row")
        rejects = ledger.get_rejects(run_id)
        if len(rejects) != int(counts.get("rejected_records", -1)):
            problems.append("ledger rejects do not match the summary")
        if len(ledger.get_file_issues(run_id)) != int(counts.get("file_issues", -1)):
            problems.append("ledger file issues do not match the summary")
        out_dir = Path(run_row["out_dir"])
        for reject in rejects:
            if not (out_dir / reject["raw_file"]).is_file():
                problems.append(f"reject raw file {reject['raw_file']} missing")
    if problems:
        raise LedgerError(f"run {run_id} failed publish verification: " + "; ".join(problems),
                          code="LD004")

def _cleanup_superseded(ledger: Ledger, run_key: str, *, keep_run_id: str, keep_round_no: int,
                        retention: str) -> None:
    """``clean`` policy: remove older rounds of this key (ledger row first, then the
    directory) so the ledger never points at a deleted directory. Lineage parents
    and newer staged rounds are never touched."""
    if retention != "clean":
        return
    for row in ledger.get_rounds(run_key):
        if row["run_id"] == keep_run_id or row["round_no"] > keep_round_no:
            continue
        if ledger.list_children(row["run_id"]):
            continue
        ledger.delete_run(row["run_id"])
        shutil.rmtree(row["out_dir"], ignore_errors=True)

def _reconcile(ledger: Ledger, run_key: str) -> None:
    """Finish cleanup that was authorized at publish time but interrupted."""
    publication = ledger.get_publication(run_key)
    if publication is None:
        return
    _cleanup_superseded(ledger, run_key, keep_run_id=publication["run_id"],
                        keep_round_no=publication["round_no"], retention=publication["retention"])

def _publish_round(ledger: Ledger, run_row: dict, *, now: str) -> None:
    _verify_publishable(ledger, run_row)
    previous = ledger.get_publication(run_row["run_key"])
    ledger.publish_run(run_row["run_key"], run_id=run_row["run_id"],
                       round_no=run_row["round_no"], retention=run_row["retention"],
                       published_at=now)
    ledger.add_event(run_row["run_id"], "run_published",
                     {"round_no": run_row["round_no"], "retention": run_row["retention"],
                      "supersedes": previous["run_id"] if previous else None}, now)
    _cleanup_superseded(ledger, run_row["run_key"], keep_run_id=run_row["run_id"],
                        keep_round_no=run_row["round_no"], retention=run_row["retention"])

def _latest_staged(ledger: Ledger, run_key: str) -> dict | None:
    staged = [row for row in ledger.get_rounds(run_key) if row["publish_state"] == "staged"]
    return staged[-1] if staged else None

def _restart_round(prepared: PreparedRun, ledger: Ledger, run_row: dict, *, now: str) -> RunResult:
    publication = ledger.get_publication(run_row["run_key"])
    if publication is not None and publication["run_id"] == run_row["run_id"]:
        raise LedgerError(f"run {run_row['run_id']} is the published round; refusing to rebuild",
                          code="LD003")
    ledger.reset_for_resume(run_row["run_id"], now)
    run_dir = Path(run_row["out_dir"])
    if run_dir.exists():
        shutil.rmtree(run_dir)
    return _execute(prepared, ledger, run_id=run_row["run_id"], run_dir=run_dir,
                    created_at=run_row["created_at"])

def _complete_staged(prepared: PreparedRun, ledger: Ledger, run_row: dict, *, now: str) -> RunResult:
    """Bring a staged round to published state, replaying it only if it is not
    already terminal with a verifiable artifact set."""
    if run_is_terminal(run_row):
        try:
            _verify_publishable(ledger, run_row)
        except LedgerError:
            pass  # artifacts or ledger rows are inconsistent: replay the round below
        else:
            _publish_round(ledger, run_row, now=now)
            return _load_summary(ledger.get_run(run_row["run_id"]))
    result = _restart_round(prepared, ledger, run_row, now=now)
    _publish_round(ledger, ledger.get_run(run_row["run_id"]), now=now)
    return result

def _start_round(prepared: PreparedRun, ledger: Ledger, *, round_no: int, now: str) -> RunResult:
    options = prepared.options
    run_id = prepared.run_id if round_no == 1 else f"{prepared.run_id}-r{round_no}"
    if ledger.get_run(run_id) is not None:
        raise LedgerError(f"run id {run_id} already belongs to a different run key", code="LD003")
    run_dir = options.out_root / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)  # leftover directory with no ledger row behind it
    spec = run_spec(prepared, run_dir, run_id=run_id, round_no=round_no,
                    retention=options.retention)
    ledger.insert_run(spec)
    ledger.add_event(run_id, "run_started",
                     {"kind": options.kind, "parent_run_id": options.parent_run_id,
                      "round_no": round_no}, spec["created_at"])
    result = _execute(prepared, ledger, run_id=run_id, run_dir=run_dir,
                      created_at=spec["created_at"])
    _publish_round(ledger, ledger.get_run(run_id), now=now)
    return result

def run(options: RunOptions) -> RunResult:
    prepared = prepare_run(options)
    ledger = Ledger(options.db_path)
    now = utc_text(options.now)
    _reconcile(ledger, prepared.run_key)
    publication = ledger.get_publication(prepared.run_key)
    published = ledger.get_run(publication["run_id"]) if publication else None
    staged = _latest_staged(ledger, prepared.run_key)
    if not options.force:
        if published is not None and run_is_terminal(published):
            return _load_summary(published)
        if staged is None:
            return _start_round(prepared, ledger, round_no=ledger.next_round_no(prepared.run_key),
                                now=now)
        if run_is_terminal(staged):
            return _complete_staged(prepared, ledger, staged, now=now)
        raise LedgerError(f"run {staged['run_id']} is incomplete; use 'resume --run-id "
                          f"{staged['run_id']}'", code="LD001")
    if staged is not None:
        return _complete_staged(prepared, ledger, staged, now=now)
    return _start_round(prepared, ledger, round_no=ledger.next_round_no(prepared.run_key), now=now)

def resume_run(run_id: str, db_path: Path, now: datetime) -> RunResult:
    ledger = Ledger(db_path)
    existing = ledger.get_run(run_id)
    if existing is None:
        raise LedgerError(f"run {run_id} not found", code="LD002")
    now_text = utc_text(now)
    _reconcile(ledger, existing["run_key"])
    if existing["publish_state"] != "staged":
        return _load_summary(existing)  # published round or retained history: idempotent read
    options = RunOptions(
        input_path=Path(existing["input_path"]), copybook_path=Path(existing["copybook_path"]),
        rules_path=Path(existing["rules_path"]), out_root=Path(existing["out_dir"]).parent,
        db_path=db_path, now=now, charset=existing["charset"], recfm=existing["recfm"],
        max_records=int(existing["max_records"]), parent_run_id=existing["parent_run_id"],
        kind=existing["kind"], notes=existing.get("notes"), retention=existing["retention"])
    prepared = prepare_run(options)
    if prepared.run_key != existing["run_key"]:
        raise LedgerError("inputs or rules changed since the interrupted run; start a new run",
                          code="LD003")
    ledger.add_event(run_id, "run_resumed", {"round_no": existing["round_no"]}, now_text)
    return _complete_staged(prepared, ledger, existing, now=now_text)
