"""Read-only ledger queries shared by the CLI and web console."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

class LedgerQueries:
    def connect(self):  # pragma: no cover - implemented by Ledger
        raise NotImplementedError

    @contextmanager
    def _connection(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _many(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_run_by_key(self, run_key: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM runs WHERE run_key = ?", (run_key,))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))

    def get_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        result = self._one("SELECT * FROM checkpoints WHERE run_id = ?", (run_id,))
        if result:
            result["state"] = json.loads(result.pop("state_json"))
        return result

    def list_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._many("SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit,))

    def list_children(self, run_id: str) -> list[dict[str, Any]]:
        return self._many("SELECT * FROM runs WHERE parent_run_id = ? ORDER BY created_at, run_id",
                          (run_id,))

    def get_rejects(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._many("SELECT * FROM rejects WHERE run_id = ? ORDER BY reject_no", (run_id,))
        for row in rows:
            row["codes"] = json.loads(row.pop("codes_json"))
            row["partial"] = json.loads(row.pop("partial_json"))
            row["errors"] = json.loads(row.pop("errors_json"))
            row["rule_hits"] = json.loads(row.pop("hits_json"))
        return rows

    def get_file_issues(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._many("SELECT * FROM file_issues WHERE run_id = ? ORDER BY issue_no", (run_id,))
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json"))
        return rows

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._many("SELECT * FROM run_events WHERE run_id = ? ORDER BY seq", (run_id,))
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json"))
        return rows

    def _batch_row(self, row: dict[str, Any]) -> dict[str, Any]:
        row["manifest"] = json.loads(row.pop("manifest_json"))
        row["diff_ranges"] = json.loads(row.pop("diff_json"))
        row["report"] = json.loads(row.pop("report_json"))
        return row

    def get_correction_batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM correction_batches WHERE batch_id = ?", (batch_id,))
        return self._batch_row(row) if row else None

    def list_correction_batches(self, source_run_id: str | None = None,
                                limit: int = 200) -> list[dict[str, Any]]:
        if source_run_id is None:
            rows = self._many("SELECT * FROM correction_batches ORDER BY created_at DESC, "
                              "batch_id DESC LIMIT ?", (limit,))
        else:
            rows = self._many("SELECT * FROM correction_batches WHERE source_run_id = ? "
                              "ORDER BY created_at DESC, batch_id DESC LIMIT ?",
                              (source_run_id, limit))
        return [self._batch_row(row) for row in rows]
