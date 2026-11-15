"""Artifact rendering and atomic writes for a run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..model import Layout, Record
from ..util import (
    canonical_json,
    json_safe,
    sha256_bytes,
    write_json_atomic,
    write_text_atomic,
)

OUTPUT_NAMES = {
    "accepted": "accepted.jsonl",
    "rejects": "rejects.jsonl",
    "report": "report.md",
    "html": "report.html",
    "profile": "profile.json",
    "layout": "layout.json",
    "summary": "summary.json",
}

def accepted_payload(record: Record) -> dict[str, Any]:
    return {
        "record_no": record.record_no,
        "byte_offset": record.byte_offset,
        "record_type": record.record_type,
        "raw_length": len(record.raw),
        "raw_sha256": sha256_bytes(record.raw),
        "values": json_safe(record.values),
        "evidence": dict(sorted(record.evidence.items())),
    }

def write_artifacts(
    run_dir: Path,
    *,
    layout: Layout,
    accepted_records: list[Record],
    reject_entries: list[dict[str, Any]],
    profile: dict[str, Any],
    summary: dict[str, Any],
    markdown: str,
    html: str,
) -> dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {name: run_dir / filename for name, filename in OUTPUT_NAMES.items()}
    write_json_atomic(paths["layout"], layout.to_dict())
    accepted_text = "".join(canonical_json(accepted_payload(record)) + "\n" for record in accepted_records)
    reject_text = "".join(canonical_json(entry) + "\n" for entry in reject_entries)
    write_text_atomic(paths["accepted"], accepted_text)
    write_text_atomic(paths["rejects"], reject_text)
    write_json_atomic(paths["profile"], profile)
    write_text_atomic(paths["report"], markdown)
    write_text_atomic(paths["html"], html)
    write_json_atomic(paths["summary"], summary)
    return paths
