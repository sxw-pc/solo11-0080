"""Read-only ledger queries shared by the CLI and web console."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

TERMINAL_STATUSES = {"completed", "completed_with_rejects"}

ARTIFACT_PATH_FIELDS = {"summary": "summary_path", "report": "report_path", "html": "html_path",
                        "profile": "profile_path", "accepted": "accepted_path",
                        "rejects": "rejects_path"}

# Every run row is annotated with the publication state of its run_key:
# ``published`` (the currently reviewable round), ``superseded`` (a retained older
# round) or ``staged`` (built in isolation, not yet reviewable).
ANNOTATED_RUNS = (
    "SELECT r.*, p.run_id AS published_run_id, "
    "CASE WHEN p.run_id = r.run_id THEN 'published' "
    "WHEN p.run_id IS NOT NULL AND r.round_no < p.round_no THEN 'superseded' "
    "ELSE 'staged' END AS publish_state "
    "FROM runs r LEFT JOIN publications p ON p.run_key = r.run_key")

def run_is_terminal(run: dict[str, Any]) -> bool:
    return (run["status"] in TERMINAL_STATUSES
            or (run["status"] == "failed" and bool(run.get("summary_path"))))

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
        """The currently published (reviewable) round for a run key, if any."""
        return self._one(f"{ANNOTATED_RUNS} WHERE r.run_key = ? AND p.run_id = r.run_id",
                         (run_key,))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._one(f"{ANNOTATED_RUNS} WHERE r.run_id = ?", (run_id,))

    def get_rounds(self, run_key: str) -> list[dict[str, Any]]:
        return self._many(f"{ANNOTATED_RUNS} WHERE r.run_key = ? ORDER BY r.round_no",
                          (run_key,))

    def get_publication(self, run_key: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM publications WHERE run_key = ?", (run_key,))

    def get_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        result = self._one("SELECT * FROM checkpoints WHERE run_id = ?", (run_id,))
        if result:
            result["state"] = json.loads(result.pop("state_json"))
        return result

    def list_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._many(f"{ANNOTATED_RUNS} ORDER BY r.created_at DESC, r.run_id DESC LIMIT ?",
                          (limit,))

    def list_children(self, run_id: str) -> list[dict[str, Any]]:
        return self._many(f"{ANNOTATED_RUNS} WHERE r.parent_run_id = ? "
                          "ORDER BY r.created_at, r.run_id", (run_id,))

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
