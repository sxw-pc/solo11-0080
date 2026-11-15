"""Decode, validate and split a prepared run into accepted/rejected records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..decode.record import decode_record
from ..errors import FATAL_FILE_CODES
from ..model import FileIssue, Record
from ..rules.checks import ValidationState
from ..rules.engine import RuleEngine, assign_record_type
from ..rules.schema import RuleSet
from .options import PreparedRun
from .reader import read_records
from .reject import build_reject_entry, quarantine_trailing_bytes, reject_code_counts

@dataclass
class ProcessedRun:
    records: list[Record]
    accepted_records: list[Record]
    accepted_data_records: list[Record]
    reject_entries: list[dict]
    file_issues: list[FileIssue]
    counts: dict[str, int]
    reject_codes: dict[str, int]
    status: str
    exit_code: int

CheckpointCallback = Callable[[int, int, dict], None]

def _is_data(record: Record, rules: RuleSet) -> bool:
    return rules.record_type is None or record.record_type == rules.record_type.data

def _snapshot(state: ValidationState) -> dict:
    return {"unique_keys": {name: [list(key) for key in sorted(keys)]
                            for name, keys in sorted(state.unique.items())}}

def process_run(prepared: PreparedRun, run_dir: Path,
                checkpoint: CheckpointCallback | None = None) -> ProcessedRun:
    read = read_records(prepared.input_data, prepared.layout.record_length,
                        max_records=prepared.options.max_records, recfm=prepared.recfm)
    engine, state = RuleEngine(prepared.rules), ValidationState()
    records: list[Record] = []
    for raw in read.records:
        record = decode_record(prepared.layout, raw.data, record_no=raw.record_no,
                               byte_offset=raw.byte_offset, charset=prepared.charset,
                               blank_policies=prepared.rules.blank_policies)
        assign_record_type(record, prepared.rules)
        if not record.errors:
            record.hits = engine.validate_record(record, state)
        records.append(record)
        if checkpoint and record.record_no % 25 == 0:
            checkpoint(record.record_no + 1, raw.byte_offset + prepared.layout.record_length,
                       _snapshot(state))
    issues = list(read.file_issues)
    # The max-records guard reads a prefix; completeness checks on that prefix are false alarms.
    if not any(issue.code == "RD004" for issue in read.file_issues):
        issues.extend(engine.validate_file(records))
    if read.trailing_bytes:
        boundary = read.full_record_count * prepared.layout.record_length
        for issue in issues:
            if issue.code == "RD002" and issue.byte_offset == boundary:
                quarantine_trailing_bytes(run_dir, read.trailing_bytes, issue)
                break
    accepted = [record for record in records if record.accepted]
    accepted_data = [record for record in accepted if _is_data(record, prepared.rules)]
    rejects = [build_reject_entry(record, run_dir) for record in records if not record.accepted]
    data_records = [record for record in records if _is_data(record, prepared.rules)]
    fatal = any(issue.severity == "error" or issue.code in FATAL_FILE_CODES for issue in issues)
    counts = {
        "total_records": len(records), "data_records": len(data_records),
        "accepted_records": len(accepted), "accepted_data_records": len(accepted_data),
        "rejected_records": len(rejects), "file_issues": len(issues),
    }
    return ProcessedRun(records, accepted, accepted_data, rejects, issues, counts,
                        reject_code_counts(rejects), "failed" if fatal else
                        ("completed_with_rejects" if rejects else "completed"), 3 if fatal else 0)
