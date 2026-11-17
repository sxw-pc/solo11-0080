"""SQLite run ledger: idempotency, checkpoints, rejects and lineage."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..errors import LedgerError
from .queries import LedgerQueries
from .schema import RUN_COLUMNS, SCHEMA, SCHEMA_VERSION, dump

RESET_SQL = (
    "UPDATE runs SET status = 'running', updated_at = ?, exit_code = 0, total_records = 0, "
    "data_records = 0, accepted_records = 0, accepted_data_records = 0, rejected_records = 0, "
    "file_issue_count = 0, reject_codes_json = '{}' WHERE run_id = ?")
CHECKPOINT_SQL = (
    "INSERT INTO checkpoints (run_id, next_record_no, byte_offset, accepted_bytes, rejected_bytes, "
    "state_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
    "next_record_no=excluded.next_record_no, byte_offset=excluded.byte_offset, "
    "accepted_bytes=excluded.accepted_bytes, rejected_bytes=excluded.rejected_bytes, "
    "state_json=excluded.state_json, updated_at=excluded.updated_at")
FINISH_SQL = (
    "UPDATE runs SET status = ?, exit_code = ?, updated_at = ?, total_records = ?, data_records = ?, "
    "accepted_records = ?, accepted_data_records = ?, rejected_records = ?, file_issue_count = ?, "
    "reject_codes_json = ?, summary_path = ?, report_path = ?, html_path = ?, profile_path = ?, "
    "accepted_path = ?, rejects_path = ? WHERE run_id = ?")
PUBLISH_SQL = (
    "INSERT INTO publications (run_key, run_id, round_no, retention, published_at) "
    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_key) DO UPDATE SET run_id=excluded.run_id, "
    "round_no=excluded.round_no, retention=excluded.retention, "
    "published_at=excluded.published_at")

class Ledger(LedgerQueries):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _write(self, statements: list[tuple[str, tuple]], error: str, code: str = "IO001") -> None:
        try:
            with self._connection() as connection:
                for sql, params in statements:
                    connection.execute(sql, params)
        except sqlite3.Error as exc:
            raise LedgerError(f"{error}: {exc}", code=code) from exc

    def initialize(self) -> None:
        try:
            with self._connection() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, SCHEMA_VERSION):
                    raise LedgerError(
                        f"ledger {self.path} uses unsupported schema version {version} "
                        f"(expected {SCHEMA_VERSION}); archive or remove the file and rebuild",
                        code="LD005")
                connection.executescript(SCHEMA)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise LedgerError(f"cannot initialize ledger {self.path}: {exc}", code="IO001") from exc

    def insert_run(self, spec: dict[str, Any]) -> None:
        columns = [item.strip() for item in RUN_COLUMNS.split(",")]
        missing = [item for item in columns if item not in spec]
        if missing:
            raise LedgerError(f"run registration missing columns: {missing}", code="LD003")
        placeholders = ", ".join("?" for _ in columns)
        self._write([(f"INSERT INTO runs ({RUN_COLUMNS}) VALUES ({placeholders})",
                      tuple(spec[item] for item in columns))], "run key or id already exists", "LD003")

    def delete_run(self, run_id: str) -> None:
        self._write([("DELETE FROM runs WHERE run_id = ?", (run_id,))],
                    f"cannot delete run {run_id}", "LD003")

    def next_round_no(self, run_key: str) -> int:
        """Next round number for a run key; never reuses a published number."""
        row = self._one(
            "SELECT COALESCE(MAX(round_no), 0) AS top FROM ("
            "SELECT round_no FROM runs WHERE run_key = ? "
            "UNION ALL SELECT round_no FROM publications WHERE run_key = ?)",
            (run_key, run_key))
        return int(row["top"]) + 1

    def publish_run(self, run_key: str, *, run_id: str, round_no: int, retention: str,
                    published_at: str) -> None:
        """Atomically switch the reviewable round of a run key (single statement)."""
        self._write([(PUBLISH_SQL, (run_key, run_id, round_no, retention, published_at))],
                    "cannot publish run")

    def reset_for_resume(self, run_id: str, updated_at: str) -> None:
        self._write([
            ("DELETE FROM rejects WHERE run_id = ?", (run_id,)),
            ("DELETE FROM file_issues WHERE run_id = ?", (run_id,)),
            ("DELETE FROM checkpoints WHERE run_id = ?", (run_id,)),
            (RESET_SQL, (updated_at, run_id)),
        ], f"cannot reset run {run_id}", "LD003")

    def mark_failed(self, run_id: str, updated_at: str, message: str) -> None:
        self._write([("UPDATE runs SET status = 'failed', exit_code = 3, updated_at = ?, "
                      "notes = ? WHERE run_id = ?", (updated_at, message[:2000], run_id))],
                    "cannot mark run failed")

    def add_event(self, run_id: str, event: str, detail: dict[str, Any], created_at: str) -> None:
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events "
                                         "WHERE run_id = ?", (run_id,)).fetchone()
                connection.execute("INSERT INTO run_events (run_id, seq, event, detail_json, "
                                   "created_at) VALUES (?, ?, ?, ?, ?)",
                                   (run_id, int(row[0]), event, dump(detail), created_at))
        except sqlite3.Error as exc:
            raise LedgerError(f"cannot append run event: {exc}", code="IO001") from exc

    def checkpoint(self, run_id: str, *, next_record_no: int, byte_offset: int,
                   accepted_bytes: int, rejected_bytes: int, state: dict[str, Any],
                   updated_at: str) -> None:
        self._write([(CHECKPOINT_SQL, (run_id, next_record_no, byte_offset, accepted_bytes,
                                       rejected_bytes, dump(state), updated_at))],
                    "cannot write checkpoint")

    def add_reject(self, run_id: str, reject_no: int, entry: dict[str, Any]) -> None:
        self._write([(
            "INSERT INTO rejects (run_id, reject_no, record_no, byte_offset, record_type, codes_json, "
            "raw_file, raw_sha256, raw_length, partial_json, errors_json, hits_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, reject_no, entry["record_no"], entry["byte_offset"], entry.get("record_type"),
             dump(entry["codes"]), entry["raw_file"], entry["raw_sha256"], entry["raw_length"],
             dump(entry["partial"]), dump(entry["errors"]), dump(entry["rule_hits"])))],
            "cannot persist reject")

    def add_file_issue(self, run_id: str, issue_no: int, issue: dict[str, Any]) -> None:
        self._write([(
            "INSERT INTO file_issues (run_id, issue_no, code, severity, message, byte_offset, "
            "record_no, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, issue_no, issue["code"], issue["severity"], issue["message"],
             issue.get("byte_offset"), issue.get("record_no"), dump(issue.get("detail", {}))))],
            "cannot persist file issue")

    def finish_run(self, run_id: str, *, status: str, exit_code: int, counts: dict[str, int],
                   reject_codes: dict[str, int], outputs: dict[str, str], updated_at: str) -> None:
        params = (status, exit_code, updated_at, counts.get("total_records", 0),
                  counts.get("data_records", 0), counts.get("accepted_records", 0),
                  counts.get("accepted_data_records", 0), counts.get("rejected_records", 0),
                  counts.get("file_issues", 0), dump(reject_codes), outputs.get("summary"),
                  outputs.get("report"), outputs.get("html"), outputs.get("profile"),
                  outputs.get("accepted"), outputs.get("rejects"), run_id)
        self._write([(FINISH_SQL, params)], "cannot finish run")
