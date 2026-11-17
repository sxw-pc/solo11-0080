"""Ledger schema and shared serialization helpers."""

from __future__ import annotations

import json
from typing import Any

from ..util import json_safe

RUN_COLUMNS = (
    "run_key, run_id, kind, parent_run_id, created_at, updated_at, status, exit_code, "
    "input_path, input_name, input_sha256, input_size, copybook_path, copybook_name, "
    "copybook_sha256, rules_path, rules_name, rules_sha256, params_json, params_sha256, "
    "record_length, charset, recfm, max_records, out_dir, summary_path, report_path, "
    "html_path, profile_path, accepted_path, rejects_path, total_records, data_records, "
    "accepted_records, accepted_data_records, rejected_records, file_issue_count, "
    "reject_codes_json, notes")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
    run_key TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
    parent_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    status TEXT NOT NULL, exit_code INTEGER NOT NULL DEFAULT 0,
    input_path TEXT NOT NULL, input_name TEXT NOT NULL, input_sha256 TEXT NOT NULL,
    input_size INTEGER NOT NULL, copybook_path TEXT NOT NULL, copybook_name TEXT NOT NULL,
    copybook_sha256 TEXT NOT NULL, rules_path TEXT NOT NULL, rules_name TEXT NOT NULL,
    rules_sha256 TEXT NOT NULL, params_json TEXT NOT NULL, params_sha256 TEXT NOT NULL,
    record_length INTEGER NOT NULL, charset TEXT NOT NULL, recfm TEXT NOT NULL,
    max_records INTEGER NOT NULL, out_dir TEXT NOT NULL, summary_path TEXT, report_path TEXT,
    html_path TEXT, profile_path TEXT, accepted_path TEXT, rejects_path TEXT,
    total_records INTEGER NOT NULL DEFAULT 0, data_records INTEGER NOT NULL DEFAULT 0,
    accepted_records INTEGER NOT NULL DEFAULT 0, accepted_data_records INTEGER NOT NULL DEFAULT 0,
    rejected_records INTEGER NOT NULL DEFAULT 0, file_issue_count INTEGER NOT NULL DEFAULT 0,
    reject_codes_json TEXT NOT NULL DEFAULT '{}', notes TEXT,
    FOREIGN KEY (parent_run_id) REFERENCES runs(run_id));
CREATE TABLE IF NOT EXISTS rejects (
    run_id TEXT NOT NULL, reject_no INTEGER NOT NULL, record_no INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL, record_type TEXT, codes_json TEXT NOT NULL,
    raw_file TEXT NOT NULL, raw_sha256 TEXT NOT NULL, raw_length INTEGER NOT NULL,
    partial_json TEXT NOT NULL, errors_json TEXT NOT NULL, hits_json TEXT NOT NULL,
    PRIMARY KEY (run_id, reject_no),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS file_issues (
    run_id TEXT NOT NULL, issue_no INTEGER NOT NULL, code TEXT NOT NULL,
    severity TEXT NOT NULL, message TEXT NOT NULL, byte_offset INTEGER, record_no INTEGER,
    detail_json TEXT NOT NULL, PRIMARY KEY (run_id, issue_no),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT PRIMARY KEY, next_record_no INTEGER NOT NULL, byte_offset INTEGER NOT NULL,
    accepted_bytes INTEGER NOT NULL, rejected_bytes INTEGER NOT NULL,
    state_json TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL,
    detail_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS correction_batches (
    batch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_run_id TEXT NOT NULL,
    contract TEXT NOT NULL, status TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL, diff_json TEXT NOT NULL DEFAULT '[]',
    report_json TEXT NOT NULL DEFAULT '{}', output_path TEXT, output_sha256 TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rejects_run ON rejects(run_id, reject_no);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_batches_source ON correction_batches(source_run_id);
"""

def dump(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
