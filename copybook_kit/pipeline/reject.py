"""Rejected-record quarantine and traceable raw-byte evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..model import FileIssue, Record
from ..util import json_safe, sha256_bytes, write_bytes_atomic

def reject_filename(record_no: int) -> str:
    return f"record-{record_no:08d}.bin"

def build_reject_entry(record: Record, run_dir: Path) -> dict[str, Any]:
    raw_file = Path("rejects") / reject_filename(record.record_no)
    write_bytes_atomic(run_dir / raw_file, record.raw)
    errors = [item.to_dict() for item in record.errors]
    hits = [item.to_dict() for item in record.hits]
    codes = sorted({item["code"] for item in errors + hits})
    payload = {
        "record_no": record.record_no,
        "byte_offset": record.byte_offset,
        "record_type": record.record_type,
        "raw_length": len(record.raw),
        "raw_sha256": sha256_bytes(record.raw),
        "raw_file": raw_file.as_posix(),
        "codes": codes,
        "errors": errors,
        "rule_hits": hits,
        "partial": json_safe(record.values),
    }
    return payload

def reject_code_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        for code in entry["codes"]:
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))

def quarantine_trailing_bytes(
    run_dir: Path, data: bytes, issue: FileIssue
) -> str | None:
    """Write a truncated tail as evidence and annotate the file issue detail."""

    if not data:
        return None
    raw_file = Path("rejects") / "truncated-tail.bin"
    write_bytes_atomic(run_dir / raw_file, data)
    issue.detail["raw_file"] = raw_file.as_posix()
    issue.detail["raw_sha256"] = sha256_bytes(data)
    issue.detail["raw_length"] = len(data)
    return raw_file.as_posix()
