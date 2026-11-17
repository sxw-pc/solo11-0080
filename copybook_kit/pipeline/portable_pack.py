"""Self-contained evidence packs for finished runs and offline verification.

A run pack is a deterministic ``.tar.gz`` holding the original input,
copybook and rules bytes, every run artifact, the referenced raw reject
evidence and the ledger snapshots (run row, rejects, file issues, checkpoint,
events) of one finished run.  ``pack_run`` reads the ledger strictly
read-only and never modifies the run; ``verify_pack`` checks a pack inside an
isolated temporary directory and never touches the operator's ledger or
working directory.

Two acceptance contracts are offered, selected by purpose:

* ``bytes`` — byte-preservation handover.  The pack is checked against an
  independently preserved manifest SHA-256 plus every member hash, ledger
  reference and count.  It proves the handover matches the approved manifest;
  it claims nothing about whether the original business verdicts were right.
* ``replay`` — semantic replay.  After the byte checks pass, the packed
  input, copybook, rules and original run timestamp are reprocessed offline
  through the real pipeline (``copybook_kit.pipeline.runner.run``) and the
  recomputed accepted/rejects/summary evidence is compared record by record,
  reporting the first byte offset and rule-conclusion difference.

The two contracts cannot impersonate each other: a pack that is internally
self-consistent but misjudged a record passes ``bytes`` and fails ``replay``.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import urllib.parse
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import CopybookKitError, InputError, LedgerError
from ..store.queries import LedgerQueries
from ..util import canonical_json, json_safe, parse_now, sha256_bytes
from .options import RunOptions
from .outputs import OUTPUT_NAMES
from .runner import TERMINAL_STATUSES, run

PACK_KIND = "copybook-kit-run-pack"
PACK_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
MEMBERS_ROOT = "members"
CLAIMS_BYTES = "byte_handover_only"
CLAIMS_REPLAY = "semantic_replay"
CONTRACTS = ("bytes", "replay")

DEFAULT_MAX_PACK_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_MEMBERS = 4096
DEFAULT_MAX_MEMBER_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024

ARTIFACT_ROLES = ("summary", "layout", "accepted", "rejects", "profile", "report", "html")
LEDGER_SNAPSHOTS = (
    ("run", "ledger_run", "ledger:runs"),
    ("rejects", "ledger_rejects", "ledger:rejects"),
    ("file_issues", "ledger_file_issues", "ledger:file_issues"),
    ("checkpoint", "ledger_checkpoint", "ledger:checkpoints"),
    ("events", "ledger_events", "ledger:run_events"),
)
ROLE_PREFIXES = {
    "input": "members/input/",
    "copybook": "members/copybook/",
    "rules": "members/rules/",
    "reject_raw": "members/evidence/",
    **{role: "members/artifacts/" for role in ARTIFACT_ROLES},
    **{role: "members/ledger/" for _, role, _ in LEDGER_SNAPSHOTS},
}
SINGLETON_ROLES = frozenset(
    ["input", "copybook", "rules", *ARTIFACT_ROLES]
    + [role for _, role, _ in LEDGER_SNAPSHOTS])
RUN_COUNT_KEYS = (
    "total_records", "data_records", "accepted_records",
    "accepted_data_records", "rejected_records", "file_issue_count",
)
SUMMARY_COMPARE_KEYS = ("status", "counts", "reject_codes", "file_issues", "input", "contracts")
ENTRY_KEYS = ("path", "role", "run_id", "source", "length", "sha256")

LEDGER_DIR = "members/ledger"
ARTIFACT_DIR = "members/artifacts"


class PackError(CopybookKitError):
    code = "PK001"


class _Reject(Exception):
    """Internal: one archive/member rejection reason for the verdict."""


class _ReadOnlyLedger(LedgerQueries):
    """Ledger queries over a ``mode=ro`` connection; packing never writes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        quoted = urllib.parse.quote(str(self.path.absolute()), safe="/")
        connection = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection


def _read_bytes(path: Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read pack source {path}: {exc}", code="IO001") from exc


def _normalize_member_path(name: str) -> str:
    """Canonical relative posix form; rejects anything that could escape."""
    if not isinstance(name, str) or not name:
        raise _Reject("empty member path")
    if "\x00" in name:
        raise _Reject(f"member path {name!r} contains a NUL byte")
    text = name.replace("\\", "/")
    if text.startswith("/"):
        raise _Reject(f"absolute member path {name!r}")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise _Reject(f"member path {name!r} escapes the pack root")
        if ":" in part:
            raise _Reject(f"member path {name!r} contains an unsafe ':'")
        parts.append(part)
    if not parts:
        raise _Reject(f"empty member path {name!r}")
    return "/".join(parts)


def _safe_normalize(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    try:
        return _normalize_member_path(name)
    except _Reject:
        return None


def _is_sha256(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Canonical manifest bytes: sorted members, sorted keys, no wall clock."""
    ordered = dict(manifest)
    ordered["members"] = sorted(manifest["members"], key=lambda item: item["path"])
    return canonical_json(ordered).encode("utf-8")


def _tar_info(path: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o644
    info.size = size
    return info


def _stage_pack(out_path: Path, entries: list[tuple[str, bytes]]) -> tuple[Path, int]:
    """Write the archive to a temporary sibling; caller publishes or drops it."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=str(out_path.parent))
    os.close(descriptor)
    tmp_path = Path(tmp_name)
    try:
        with tmp_path.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                               compresslevel=9, mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w",
                                  format=tarfile.PAX_FORMAT) as tar:
                    for path, data in entries:
                        tar.addfile(_tar_info(path, len(data)), io.BytesIO(data))
        return tmp_path, tmp_path.stat().st_size
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _write_pack(out_path: Path, entries: list[tuple[str, bytes]], *,
                max_pack_bytes: int) -> int:
    """Stage, budget-check and atomically publish a pack archive."""
    ordered = sorted(entries, key=lambda item: item[0])
    tmp_path, size = _stage_pack(Path(out_path), ordered)
    if size > max_pack_bytes:
        tmp_path.unlink(missing_ok=True)
        raise PackError(f"pack is {size} bytes, over the {max_pack_bytes}-byte budget",
                        code="PK003")
    os.replace(tmp_path, out_path)
    return size


def _read_archive(pack_path: Path, *, max_members: int, max_member_bytes: int,
                  max_total_bytes: int) -> dict[str, bytes]:
    """Read an untrusted archive into memory under strict safety budgets."""
    members: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    total = 0
    try:
        with tarfile.open(pack_path, mode="r:gz") as tar:
            for index, info in enumerate(tar, start=1):
                if index > max_members:
                    raise _Reject(f"member count exceeds the budget of {max_members}")
                if not info.isreg():
                    raise _Reject(f"member {info.name!r} is not a regular file "
                                  "(links and devices are rejected)")
                name = _normalize_member_path(info.name)
                if name in members:
                    raise _Reject(f"duplicate normalized member path {name!r}")
                key = name.casefold()
                if key in folded:
                    raise _Reject(f"case-fold conflict between {folded[key]!r} and {name!r}")
                if info.size > max_member_bytes:
                    raise _Reject(f"member {name!r} exceeds the per-member budget of "
                                  f"{max_member_bytes} bytes")
                total += info.size
                if total > max_total_bytes:
                    raise _Reject(f"archive exceeds the total uncompressed budget of "
                                  f"{max_total_bytes} bytes")
                handle = tar.extractfile(info)
                data = handle.read() if handle is not None else b""
                if len(data) != info.size:
                    raise _Reject(f"member {name!r} length does not match its header")
                members[name] = data
                folded[key] = name
    except _Reject:
        raise
    except (OSError, EOFError, ValueError, tarfile.TarError, zlib.error) as exc:
        raise _Reject(f"cannot read pack archive: {exc}") from exc
    return members


def _run_counts(run_row: dict[str, Any]) -> dict[str, int]:
    return {key: int(run_row[key]) for key in RUN_COUNT_KEYS}


def _processed_counts(run_row: dict[str, Any]) -> dict[str, int]:
    counts = _run_counts(run_row)
    counts["file_issues"] = counts.pop("file_issue_count")
    return counts


def _is_terminal(run_row: dict[str, Any]) -> bool:
    return (run_row["status"] in TERMINAL_STATUSES
            or (run_row["status"] == "failed" and bool(run_row.get("summary_path"))))


def _jsonl_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _member(role: str, run_id: str, source: str, path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "role": role, "run_id": run_id, "source": source,
            "length": len(data), "sha256": sha256_bytes(data)}


def _collect(ledger: _ReadOnlyLedger, run_row: dict[str, Any]):
    """First read pass: gather every source, then check ledger consistency."""
    run_id = run_row["run_id"]
    out_dir = Path(run_row["out_dir"])
    rejects_rows = ledger.get_rejects(run_id)
    issue_rows = ledger.get_file_issues(run_id)
    checkpoint = ledger.get_checkpoint(run_id)
    events = ledger.get_events(run_id)
    if checkpoint is None:
        raise PackError(f"run {run_id} has no checkpoint snapshot", code="PK001")

    paths: dict[str, Path] = {
        "input": Path(run_row["input_path"]),
        "copybook": Path(run_row["copybook_path"]),
        "rules": Path(run_row["rules_path"]),
    }
    for role in ARTIFACT_ROLES:
        if role == "layout":
            paths[role] = out_dir / OUTPUT_NAMES["layout"]
            continue
        text = run_row.get(f"{role}_path")
        if not text:
            raise PackError(f"run {run_id} is missing its {role} artifact path", code="PK001")
        paths[role] = Path(text)

    evidence_refs: list[tuple[str, str | None, int | None]] = []
    for row in rejects_rows:
        evidence_refs.append((row["raw_file"], row["raw_sha256"], row["raw_length"]))
    for row in issue_rows:
        detail = row["detail"]
        if detail.get("raw_file"):
            evidence_refs.append(
                (detail["raw_file"], detail.get("raw_sha256"), detail.get("raw_length")))
    evidence_paths: dict[str, Path] = {}
    for raw_file, _, _ in evidence_refs:
        try:
            normalized = _normalize_member_path(raw_file)
        except _Reject as exc:
            raise PackError(f"unsafe raw_file reference {raw_file!r}: {exc}",
                            code="PK001") from exc
        if normalized != raw_file:
            raise PackError(f"raw_file reference {raw_file!r} is not normalized",
                            code="PK001")
        evidence_paths[raw_file] = out_dir.joinpath(*PurePosixPath(raw_file).parts)

    blobs: dict[str, bytes] = {}
    members: list[dict[str, Any]] = []
    recheck: list[tuple[str, Path, str]] = []

    def add(role: str, source: str, member_path: str, absolute: Path) -> bytes:
        data = _read_bytes(absolute)
        blobs[member_path] = data
        members.append(_member(role, run_id, source, member_path, data))
        recheck.append((member_path, Path(absolute), sha256_bytes(data)))
        return data

    input_data = add("input", paths["input"].name, f"members/input/{paths['input'].name}",
                     paths["input"])
    copybook_data = add("copybook", paths["copybook"].name,
                        f"members/copybook/{paths['copybook'].name}", paths["copybook"])
    rules_data = add("rules", paths["rules"].name, f"members/rules/{paths['rules'].name}",
                     paths["rules"])
    for role in ARTIFACT_ROLES:
        add(role, OUTPUT_NAMES[role], f"{ARTIFACT_DIR}/{OUTPUT_NAMES[role]}", paths[role])
    for raw_file, absolute in sorted(evidence_paths.items()):
        add("reject_raw", raw_file, f"members/evidence/{raw_file}", absolute)

    fingerprints = (
        ("input", input_data, run_row["input_sha256"], int(run_row["input_size"])),
        ("copybook", copybook_data, run_row["copybook_sha256"], None),
        ("rules", rules_data, run_row["rules_sha256"], None),
    )
    for label, data, expected_sha, expected_size in fingerprints:
        if sha256_bytes(data) != expected_sha:
            raise PackError(f"{label} bytes no longer match the ledger fingerprint",
                            code="PK001")
        if expected_size is not None and len(data) != expected_size:
            raise PackError(f"{label} size no longer matches the ledger fingerprint",
                            code="PK001")
    for raw_file, expected_sha, expected_len in evidence_refs:
        data = blobs[f"members/evidence/{raw_file}"]
        if expected_sha is not None and sha256_bytes(data) != expected_sha:
            raise PackError(f"reject evidence {raw_file} no longer matches the ledger "
                            "fingerprint", code="PK001")
        if expected_len is not None and len(data) != int(expected_len):
            raise PackError(f"reject evidence {raw_file} length no longer matches the "
                            "ledger", code="PK001")

    snapshots = {"run": run_row, "rejects": rejects_rows, "file_issues": issue_rows,
                 "checkpoint": checkpoint, "events": events}
    for name, role, source in LEDGER_SNAPSHOTS:
        data = (canonical_json(snapshots[name]) + "\n").encode("utf-8")
        blobs[f"{LEDGER_DIR}/{name}.json"] = data
        members.append(_member(role, run_id, source, f"{LEDGER_DIR}/{name}.json", data))

    paths_seen = [member["path"] for member in members]
    if len(set(paths_seen)) != len(paths_seen):
        raise PackError("member path collision while building the pack", code="PK001")
    _check_run_consistency(run_row, rejects_rows, issue_rows, checkpoint, events, blobs)
    return members, blobs, recheck


def _check_run_consistency(run_row, rejects_rows, issue_rows, checkpoint, events, blobs):
    """Ledger counts must match the actual artifact lines and raw evidence."""
    run_id = run_row["run_id"]

    def fail(message: str) -> None:
        raise PackError(f"run {run_id}: {message}", code="PK001")

    try:
        accepted_text = blobs[f"{ARTIFACT_DIR}/accepted.jsonl"].decode("utf-8")
        rejects_text = blobs[f"{ARTIFACT_DIR}/rejects.jsonl"].decode("utf-8")
        summary = json.loads(blobs[f"{ARTIFACT_DIR}/summary.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"artifacts are not valid text/JSON: {exc}")
    accepted_lines = _jsonl_lines(accepted_text)
    reject_lines = _jsonl_lines(rejects_text)
    counts = _run_counts(run_row)
    if len(accepted_lines) != counts["accepted_records"]:
        fail(f"ledger accepted_records={counts['accepted_records']} but accepted.jsonl "
             f"has {len(accepted_lines)} lines")
    if len(reject_lines) != counts["rejected_records"] or len(rejects_rows) != counts["rejected_records"]:
        fail(f"ledger rejected_records={counts['rejected_records']} but rejects.jsonl has "
             f"{len(reject_lines)} lines and the ledger snapshot has {len(rejects_rows)} rows")
    if len(issue_rows) != counts["file_issue_count"]:
        fail(f"ledger file_issue_count={counts['file_issue_count']} but the snapshot has "
             f"{len(issue_rows)} rows")
    if summary.get("run_id") != run_id or summary.get("status") != run_row["status"]:
        fail("summary.json does not match the ledger run row")
    if summary.get("counts") != _processed_counts(run_row):
        fail("summary.json counts do not match the ledger run row")
    state_counts = (checkpoint.get("state") or {}).get("counts")
    if state_counts != _processed_counts(run_row):
        fail("checkpoint counts do not match the ledger run row")
    for entry, row in zip(reject_lines, rejects_rows):
        for key in ("record_no", "byte_offset", "raw_file", "raw_sha256", "raw_length", "codes"):
            if entry.get(key) != row.get(key):
                fail(f"rejects.jsonl and the ledger disagree on {key} for record "
                     f"{row.get('record_no')}")
    if not events or any(event.get("run_id") != run_id for event in events):
        fail("ledger events snapshot is missing or belongs to another run")


def _assert_unchanged(ledger: _ReadOnlyLedger, run_row: dict[str, Any],
                      recheck: list[tuple[str, Path, str]]) -> None:
    """Second read pass: any change since the first pass aborts the pack."""
    for label, path, expected_sha in recheck:
        if sha256_bytes(_read_bytes(path)) != expected_sha:
            raise PackError(f"source {label} changed while packing; no pack published",
                            code="PK001")
    fresh = ledger.get_run(run_row["run_id"])
    watched = ("updated_at", "status", "input_sha256", "copybook_sha256", "rules_sha256",
               *RUN_COUNT_KEYS)
    if fresh is None or any(fresh[key] != run_row[key] for key in watched):
        raise PackError("the ledger row changed while packing; no pack published",
                        code="PK001")


def _build_manifest(run_row: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "kind": PACK_KIND,
        "run_id": run_row["run_id"],
        "run_key": run_row["run_key"],
        "status": run_row["status"],
        "created_at": run_row["created_at"],
        "updated_at": run_row["updated_at"],
        "parameters": {
            "charset": run_row["charset"],
            "recfm": run_row["recfm"],
            "max_records": int(run_row["max_records"]),
            "record_length": int(run_row["record_length"]),
        },
        "counts": _run_counts(run_row),
        "members": members,
    }


def pack_run(run_id: str, db_path: Path, out_path: Path, *,
             max_pack_bytes: int = DEFAULT_MAX_PACK_BYTES) -> dict[str, Any]:
    """Build a self-contained evidence pack for one finished run.

    The ledger is opened read-only; the run and its artifacts are never
    modified.  If any source byte or the ledger row changes between the first
    read and publication, the whole pack fails and nothing is published.
    """
    db_path, out_path = Path(db_path), Path(out_path)
    if max_pack_bytes <= 0:
        raise InputError("--max-pack-bytes must be positive", code="CLI001")
    if not db_path.is_file():
        raise InputError(f"ledger {db_path} not found", code="IO001")
    ledger = _ReadOnlyLedger(db_path)
    run_row = ledger.get_run(run_id)
    if run_row is None:
        raise LedgerError(f"run {run_id} not found", code="LD002")
    if not _is_terminal(run_row):
        raise PackError(f"run {run_id} is not finished (status {run_row['status']!r})",
                        code="PK001")
    members, blobs, recheck = _collect(ledger, run_row)
    manifest = _build_manifest(run_row, members)
    manifest_data = _manifest_bytes(manifest)
    entries = [(MANIFEST_NAME, manifest_data)]
    entries += sorted(blobs.items(), key=lambda item: item[0])
    tmp_path, size = _stage_pack(out_path, entries)
    try:
        _assert_unchanged(ledger, run_row, recheck)
        if size > max_pack_bytes:
            raise PackError(f"pack is {size} bytes, over the {max_pack_bytes}-byte budget",
                            code="PK003")
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, out_path)
    return {
        "ok": True,
        "run_id": run_id,
        "run_key": run_row["run_key"],
        "pack": str(out_path.resolve()),
        "manifest_sha256": sha256_bytes(manifest_data),
        "members": len(members),
        "pack_bytes": size,
        "max_pack_bytes": max_pack_bytes,
    }


def _parse_manifest(raw: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"manifest is not valid JSON: {exc}"
    if not isinstance(manifest, dict):
        return None, "manifest must be a JSON object"
    if manifest.get("kind") != PACK_KIND:
        return None, f"manifest kind must be {PACK_KIND!r}"
    if manifest.get("schema_version") != PACK_SCHEMA_VERSION:
        return None, f"unsupported manifest schema_version {manifest.get('schema_version')!r}"
    if not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]:
        return None, "manifest run_id is missing"
    if not isinstance(manifest.get("members"), list):
        return None, "manifest members must be a list"
    if not isinstance(manifest.get("counts"), dict):
        return None, "manifest counts must be an object"
    return manifest, None


def _check_members(manifest: dict[str, Any], archive: dict[str, bytes]):
    violations: list[str] = []
    entries: dict[str, dict[str, Any]] = {}
    role_counts: dict[str, int] = {}
    for index, entry in enumerate(manifest["members"]):
        label = f"manifest members[{index}]"
        if not isinstance(entry, dict) or sorted(entry) != sorted(ENTRY_KEYS):
            violations.append(f"{label} must have exactly the keys {sorted(ENTRY_KEYS)}")
            continue
        path, role = entry["path"], entry["role"]
        normalized = _safe_normalize(path)
        if normalized is None:
            violations.append(f"{label} has an unsafe path {path!r}")
            continue
        if normalized != path:
            violations.append(f"{label} path {path!r} is not normalized")
        if role not in ROLE_PREFIXES:
            violations.append(f"{label} has unknown role {role!r}")
        elif not path.startswith(ROLE_PREFIXES[role]):
            violations.append(f"{label} path {path!r} does not live under "
                              f"{ROLE_PREFIXES[role]!r}")
        if entry["run_id"] != manifest["run_id"]:
            violations.append(f"{label} run_id does not match the manifest run")
        if not isinstance(entry["source"], str) or not entry["source"]:
            violations.append(f"{label} source must be a non-empty string")
        if not isinstance(entry["length"], int) or entry["length"] < 0:
            violations.append(f"{label} length must be a non-negative integer")
        if not _is_sha256(entry["sha256"]):
            violations.append(f"{label} sha256 must be 64 lowercase hex characters")
        if path in entries:
            violations.append(f"duplicate member path {path!r} in the manifest")
        entries[path] = entry
        role_counts[role] = role_counts.get(role, 0) + 1
    for role in sorted(SINGLETON_ROLES):
        if role_counts.get(role, 0) != 1:
            violations.append(f"manifest must list exactly one {role} member, found "
                              f"{role_counts.get(role, 0)}")
    missing = sorted(set(entries) - set(archive))
    extra = sorted(set(archive) - set(entries))
    for path in missing:
        violations.append(f"missing member listed in the manifest: {path}")
    for path in extra:
        violations.append(f"archive member not listed in the manifest: {path}")
    for path in sorted(set(entries) & set(archive)):
        entry, data = entries[path], archive[path]
        if isinstance(entry.get("length"), int) and len(data) != entry["length"]:
            violations.append(f"member {path} length {len(data)} does not match the "
                              f"manifest length {entry['length']}")
        if _is_sha256(entry.get("sha256")) and sha256_bytes(data) != entry["sha256"]:
            violations.append(f"member {path} sha256 does not match the manifest")
    return violations, entries


def _load_json_member(archive: dict[str, bytes], path: str, label: str,
                      problems: list[str]) -> Any:
    data = archive.get(path)
    if data is None:
        problems.append(f"missing {label} member {path}")
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(f"{label} member {path} is not valid JSON: {exc}")
        return None


def _check_ledger(manifest: dict[str, Any], entries: dict[str, dict[str, Any]],
                  archive: dict[str, bytes]) -> list[str]:
    """Cross-check ledger references and counts against the packed bytes."""
    problems: list[str] = []
    run_id = manifest["run_id"]
    run_row = _load_json_member(archive, f"{LEDGER_DIR}/run.json", "ledger run", problems)
    rejects_rows = _load_json_member(archive, f"{LEDGER_DIR}/rejects.json",
                                     "ledger rejects", problems)
    issue_rows = _load_json_member(archive, f"{LEDGER_DIR}/file_issues.json",
                                   "ledger file issues", problems)
    checkpoint = _load_json_member(archive, f"{LEDGER_DIR}/checkpoint.json",
                                   "ledger checkpoint", problems)
    events = _load_json_member(archive, f"{LEDGER_DIR}/events.json", "ledger events", problems)
    summary = _load_json_member(archive, f"{ARTIFACT_DIR}/summary.json", "run summary", problems)
    if problems:
        return problems
    by_role: dict[str, list[dict[str, Any]]] = {}
    for entry in entries.values():
        by_role.setdefault(entry["role"], []).append(entry)

    def member_path(role: str) -> str:
        return by_role[role][0]["path"]

    try:
        counts = _run_counts(run_row)
        processed = _processed_counts(run_row)
    except (KeyError, TypeError, ValueError):
        return ["ledger run snapshot is malformed"]
    if run_row.get("run_id") != run_id:
        problems.append("ledger run snapshot belongs to a different run")
    if run_row.get("run_key") != manifest.get("run_key"):
        problems.append("ledger run snapshot run_key does not match the manifest")
    if run_row.get("status") != manifest.get("status"):
        problems.append("ledger run snapshot status does not match the manifest")
    if counts != manifest.get("counts"):
        problems.append("manifest counts do not match the ledger run snapshot")
    fingerprints = (
        ("input", "input_sha256", "input_size"),
        ("copybook", "copybook_sha256", None),
        ("rules", "rules_sha256", None),
    )
    for role, sha_key, size_key in fingerprints:
        data = archive[member_path(role)]
        if sha256_bytes(data) != run_row.get(sha_key):
            problems.append(f"{role} member does not match the ledger {sha_key}")
        if size_key is not None and len(data) != run_row.get(size_key):
            problems.append(f"input member length does not match the ledger {size_key}")
    if summary.get("run_id") != run_id or summary.get("status") != run_row.get("status"):
        problems.append("run summary does not match the ledger run snapshot")
    if summary.get("counts") != processed:
        problems.append("run summary counts do not match the ledger run snapshot")
    contracts = summary.get("contracts") or {}
    if (contracts.get("copybook") or {}).get("sha256") != run_row.get("copybook_sha256"):
        problems.append("run summary copybook hash does not match the ledger")
    if (contracts.get("rules") or {}).get("sha256") != run_row.get("rules_sha256"):
        problems.append("run summary rules hash does not match the ledger")
    if (summary.get("input") or {}).get("sha256") != run_row.get("input_sha256"):
        problems.append("run summary input hash does not match the ledger")

    def jsonl(path: str, label: str) -> list[dict[str, Any]] | None:
        try:
            return _jsonl_lines(archive[path].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"{label} member {path} is not valid JSONL: {exc}")
            return None

    accepted_lines = jsonl(f"{ARTIFACT_DIR}/accepted.jsonl", "accepted")
    reject_lines = jsonl(f"{ARTIFACT_DIR}/rejects.jsonl", "rejects")
    if accepted_lines is not None and len(accepted_lines) != counts["accepted_records"]:
        problems.append(f"accepted.jsonl has {len(accepted_lines)} lines but the ledger "
                        f"records {counts['accepted_records']}")
    if reject_lines is not None and len(reject_lines) != counts["rejected_records"]:
        problems.append(f"rejects.jsonl has {len(reject_lines)} lines but the ledger "
                        f"records {counts['rejected_records']}")
    if not isinstance(rejects_rows, list) or len(rejects_rows) != counts["rejected_records"]:
        problems.append("ledger rejects snapshot count does not match rejected_records")
        rejects_rows = rejects_rows if isinstance(rejects_rows, list) else []
    evidence = {entry["source"]: entry for entry in by_role.get("reject_raw", [])}
    for row in rejects_rows:
        if row.get("run_id") != run_id:
            problems.append("ledger reject row belongs to a different run")
        raw_file = row.get("raw_file")
        normalized = _safe_normalize(raw_file)
        if normalized is None or normalized != raw_file:
            problems.append(f"reject raw_file {raw_file!r} is unsafe or not normalized")
            continue
        entry = evidence.get(raw_file)
        if entry is None:
            problems.append(f"reject evidence {raw_file!r} is not in the pack")
            continue
        data = archive[entry["path"]]
        if sha256_bytes(data) != row.get("raw_sha256"):
            problems.append(f"reject evidence {raw_file!r} hash does not match the ledger")
        if len(data) != row.get("raw_length"):
            problems.append(f"reject evidence {raw_file!r} length does not match the ledger")
    if reject_lines is not None and len(reject_lines) == len(rejects_rows):
        for line_entry, row in zip(reject_lines, rejects_rows):
            for key in ("record_no", "byte_offset", "raw_file", "raw_sha256",
                        "raw_length", "codes"):
                if line_entry.get(key) != row.get(key):
                    problems.append(f"rejects.jsonl and the ledger snapshot disagree on "
                                    f"{key} for record {row.get('record_no')}")
    if not isinstance(issue_rows, list) or len(issue_rows) != counts["file_issue_count"]:
        problems.append("ledger file-issues snapshot count does not match file_issue_count")
        issue_rows = issue_rows if isinstance(issue_rows, list) else []
    for row in issue_rows:
        if row.get("run_id") != run_id:
            problems.append("ledger file-issue row belongs to a different run")
        detail = row.get("detail") or {}
        raw_file = detail.get("raw_file")
        if not raw_file:
            continue
        normalized = _safe_normalize(raw_file)
        if normalized is None or normalized != raw_file:
            problems.append(f"file-issue raw_file {raw_file!r} is unsafe or not normalized")
            continue
        entry = evidence.get(raw_file)
        if entry is None:
            problems.append(f"file-issue evidence {raw_file!r} is not in the pack")
            continue
        data = archive[entry["path"]]
        if detail.get("raw_sha256") is not None and sha256_bytes(data) != detail["raw_sha256"]:
            problems.append(f"file-issue evidence {raw_file!r} hash does not match the ledger")
        if detail.get("raw_length") is not None and len(data) != detail["raw_length"]:
            problems.append(f"file-issue evidence {raw_file!r} length does not match the ledger")
    if not isinstance(checkpoint, dict) or checkpoint.get("run_id") != run_id:
        problems.append("ledger checkpoint snapshot belongs to a different run")
    elif (checkpoint.get("state") or {}).get("counts") != processed:
        problems.append("ledger checkpoint counts do not match the run snapshot")
    if not isinstance(events, list) or not events:
        problems.append("ledger events snapshot is missing or empty")
    else:
        for index, event in enumerate(events, start=1):
            if event.get("run_id") != run_id or event.get("seq") != index:
                problems.append("ledger events snapshot is inconsistent with the run")
                break
    return problems


def _record_map(accepted_text: str, rejects_text: str) -> dict[int, dict[str, Any]]:
    mapping: dict[int, dict[str, Any]] = {}
    for text, decision in ((accepted_text, "accepted"), (rejects_text, "rejected")):
        for entry in _jsonl_lines(text):
            mapping[int(entry["record_no"])] = {
                "byte_offset": int(entry["byte_offset"]),
                "decision": decision,
                "codes": sorted(entry.get("codes", [])),
                "raw_sha256": entry.get("raw_sha256"),
            }
    return mapping


def _first_record_difference(pack_map: dict[int, dict[str, Any]],
                             replay_map: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    def sort_key(record_no: int) -> tuple[int, int]:
        spot = pack_map.get(record_no) or replay_map[record_no]
        return (spot["byte_offset"], record_no)

    for record_no in sorted(set(pack_map) | set(replay_map), key=sort_key):
        before, after = pack_map.get(record_no), replay_map.get(record_no)
        if before != after:
            spot = before or after
            return {"record_no": record_no, "byte_offset": spot["byte_offset"],
                    "pack": before, "replay": after}
    return None


def _summary_differences(pack_summary: dict[str, Any],
                         replay_summary: dict[str, Any]) -> list[dict[str, Any]]:
    differences = []
    for key in SUMMARY_COMPARE_KEYS:
        before, after = pack_summary.get(key), replay_summary.get(key)
        if before != after:
            differences.append({"key": key, "pack": json_safe(before),
                                "replay": json_safe(after)})
    return differences


def _replay_and_compare(manifest: dict[str, Any], entries: dict[str, dict[str, Any]],
                        archive: dict[str, bytes], work: Path) -> dict[str, Any]:
    by_role = {entry["role"]: entry for entry in entries.values()}
    try:
        run_row = json.loads(archive[f"{LEDGER_DIR}/run.json"].decode("utf-8"))
        source_dir = work / "src"
        source_dir.mkdir(parents=True)
        paths: dict[str, Path] = {}
        for role in ("input", "copybook", "rules"):
            entry = by_role[role]
            target = source_dir / PurePosixPath(entry["path"]).name
            target.write_bytes(archive[entry["path"]])
            paths[role] = target
        replay_root = work / "replay"
        options = RunOptions(
            input_path=paths["input"], copybook_path=paths["copybook"],
            rules_path=paths["rules"], out_root=replay_root / "runs",
            db_path=replay_root / "ledger.sqlite", now=parse_now(run_row["created_at"]),
            charset=run_row["charset"], recfm=run_row["recfm"],
            max_records=int(run_row["max_records"]))
        result = run(options)
        run_dir = replay_root / "runs" / result.run_id
        replay_accepted = (run_dir / "accepted.jsonl").read_text(encoding="utf-8")
        replay_rejects = (run_dir / "rejects.jsonl").read_text(encoding="utf-8")
        replay_summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        pack_accepted = archive[by_role["accepted"]["path"]].decode("utf-8")
        pack_rejects = archive[by_role["rejects"]["path"]].decode("utf-8")
        pack_summary = json.loads(archive[by_role["summary"]["path"]].decode("utf-8"))
        pack_map = _record_map(pack_accepted, pack_rejects)
        replay_map = _record_map(replay_accepted, replay_rejects)
        return {
            "replay_run_id": result.run_id,
            "records_compared": len(set(pack_map) | set(replay_map)),
            "counts": json_safe(result.counts),
            "first_difference": _first_record_difference(pack_map, replay_map),
            "summary_differences": _summary_differences(pack_summary, replay_summary),
        }
    except CopybookKitError as exc:
        return {"error": f"{exc.code}: {exc.message}"}
    except Exception as exc:  # a hostile pack must not crash the verifier
        return {"error": f"{type(exc).__name__}: {exc}"}


def _failure(contract: str, *, stage: str, violations: list[str],
             manifest_sha256: str | None = None,
             run_id: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "contract": contract,
        "claims": CLAIMS_BYTES if contract == "bytes" else CLAIMS_REPLAY,
        "stage": stage,
        "manifest_sha256": manifest_sha256,
        "run_id": run_id,
        "violations": list(violations),
    }


def verify_pack(pack_path: Path, *, contract: str, manifest_sha256: str | None = None,
                max_members: int = DEFAULT_MAX_MEMBERS,
                max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
                max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> dict[str, Any]:
    """Verify a run pack offline under one of the two acceptance contracts.

    All checks (and the optional replay) run in memory or inside an isolated
    temporary directory; the operator's ledger and working directory are
    never touched, and old-host absolute paths stay inside the pack.
    """
    pack_path = Path(pack_path)
    if contract not in CONTRACTS:
        raise InputError("--contract must be one of: bytes, replay", code="CLI001")
    if contract == "bytes" and not manifest_sha256:
        raise InputError("--manifest-sha256 is required for the bytes contract",
                         code="CLI001")
    for label, value in (("--max-members", max_members),
                         ("--max-member-bytes", max_member_bytes),
                         ("--max-total-bytes", max_total_bytes)):
        if value <= 0:
            raise InputError(f"{label} must be positive", code="CLI001")
    if not pack_path.is_file():
        raise InputError(f"pack {pack_path} not found", code="IO001")
    try:
        archive = _read_archive(pack_path, max_members=max_members,
                                max_member_bytes=max_member_bytes,
                                max_total_bytes=max_total_bytes)
    except _Reject as exc:
        return _failure(contract, stage="archive", violations=[str(exc)])
    manifest_data = archive.pop(MANIFEST_NAME, None)
    if manifest_data is None:
        return _failure(contract, stage="manifest",
                        violations=[f"pack has no {MANIFEST_NAME}"])
    actual_sha = sha256_bytes(manifest_data)
    violations: list[str] = []
    if manifest_sha256:
        expected = manifest_sha256.strip().lower()
        if actual_sha != expected:
            violations.append(f"manifest sha256 {actual_sha} does not match the "
                              f"approved {expected}")
    manifest, error = _parse_manifest(manifest_data)
    if error:
        violations.append(error)
    if violations or manifest is None:
        return _failure(contract, stage="manifest", violations=violations,
                        manifest_sha256=actual_sha)
    member_violations, entries = _check_members(manifest, archive)
    ledger_violations = [] if member_violations else _check_ledger(manifest, entries, archive)
    violations = member_violations + ledger_violations
    if violations:
        return _failure(contract, stage="bytes", violations=violations,
                        manifest_sha256=actual_sha, run_id=manifest["run_id"])
    byte_summary = {
        "ok": True,
        "members_checked": len(archive),
        "checks": ["archive", "manifest", "members", "ledger_refs", "counts"],
    }
    if contract == "bytes":
        return {
            "ok": True,
            "contract": "bytes",
            "claims": CLAIMS_BYTES,
            "manifest_sha256": actual_sha,
            "run_id": manifest["run_id"],
            "violations": [],
            **byte_summary,
        }
    with tempfile.TemporaryDirectory(prefix="copybook-kit-verify-") as work:
        replay = _replay_and_compare(manifest, entries, archive, Path(work))
    if "error" in replay:
        violations = [f"semantic replay failed: {replay['error']}"]
    else:
        problems = []
        if replay["first_difference"] is not None:
            problems.append("first record difference at byte offset "
                            f"{replay['first_difference']['byte_offset']}")
        problems.extend(f"summary {item['key']} differs"
                        for item in replay["summary_differences"])
        violations = [f"semantic replay diverges from the packed evidence: {item}"
                      for item in problems]
    return {
        "ok": not violations,
        "contract": "replay",
        "claims": CLAIMS_REPLAY,
        "manifest_sha256": actual_sha,
        "run_id": manifest["run_id"],
        "bytes": byte_summary,
        "replay": replay,
        "violations": violations,
    }
