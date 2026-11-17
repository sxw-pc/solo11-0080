"""Approved correction manifest: YAML loading and schema validation (CR001)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..errors import CorrectionError, Diagnostic
from ..util import sha256_bytes

MAX_MANIFEST_BYTES = 512 * 1024
CONTRACTS = ("evidence", "canonical")
TOP_KEYS = {"version", "name", "source_run_id", "contract", "redefines", "corrections"}
ITEM_KEYS = {"record_no", "expected_sha256", "path", "value"}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

@dataclass(frozen=True)
class CorrectionItem:
    record_no: int
    path: str
    expected_sha256: str
    value: Any  # str | int | None; floats and bools are rejected at load time

@dataclass(frozen=True)
class Manifest:
    name: str
    source_run_id: str
    contract: str
    redefines: dict[str, str]
    corrections: tuple[CorrectionItem, ...]
    sha256: str

class _UniqueSafeLoader(yaml.SafeLoader):
    pass

def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"duplicate YAML key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result

_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)

def _fail(diagnostics: list[Diagnostic]) -> None:
    if diagnostics:
        raise CorrectionError(diagnostics)

def _item(index: int, config: Any, diagnostics: list[Diagnostic]) -> CorrectionItem | None:
    label = f"corrections[{index}]"
    if not isinstance(config, dict):
        diagnostics.append(Diagnostic("CR001", f"{label} must be a mapping"))
        return None
    if unknown := set(config) - ITEM_KEYS:
        diagnostics.append(Diagnostic("CR001", f"{label} has unknown keys: {sorted(unknown)}"))
        return None
    if missing := ITEM_KEYS - set(config):
        diagnostics.append(Diagnostic("CR001", f"{label} is missing keys: {sorted(missing)}"))
        return None
    record_no = config["record_no"]
    if isinstance(record_no, bool) or not isinstance(record_no, int) or record_no < 1:
        diagnostics.append(Diagnostic("CR001", f"{label}.record_no must be a positive integer"))
        return None
    digest = config["expected_sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        diagnostics.append(Diagnostic("CR001", f"{label}.expected_sha256 must be 64 hex characters",
                                      record_no=record_no))
        return None
    path = config["path"]
    if not isinstance(path, str) or not path.strip():
        diagnostics.append(Diagnostic("CR001", f"{label}.path must be a non-empty leaf path",
                                      record_no=record_no))
        return None
    value = config["value"]
    if isinstance(value, (bool, float, list, dict)):
        diagnostics.append(Diagnostic(
            "CR001", f"{label}.value must be a string, an integer or null; "
            "floats are forbidden to avoid implicit precision", record_no=record_no, field=path))
        return None
    if isinstance(value, str) and not value:
        value = None
    return CorrectionItem(record_no, path.strip(), digest.lower(), value)

def load_manifest(path: str | Path) -> Manifest:
    path = Path(path)
    diagnostics: list[Diagnostic] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CorrectionError([Diagnostic("CR001", f"cannot read manifest {path}: {exc}")]) from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise CorrectionError([Diagnostic("CR001", f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")])
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=_UniqueSafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        raise CorrectionError([Diagnostic("CR001", f"invalid YAML in {path}: {exc}")]) from exc
    if not isinstance(data, dict):
        raise CorrectionError([Diagnostic("CR001", "correction manifest must be a YAML mapping")])
    if unknown := set(data) - TOP_KEYS:
        diagnostics.append(Diagnostic("CR001", f"unknown top-level keys: {sorted(unknown)}"))
    if data.get("version") != 1:
        diagnostics.append(Diagnostic("CR001", "version must be 1"))
    name = data.get("name", path.stem)
    if not isinstance(name, str) or not name:
        diagnostics.append(Diagnostic("CR001", "name must be a non-empty string"))
        name = path.stem
    source_run_id = data.get("source_run_id")
    if not isinstance(source_run_id, str) or not source_run_id:
        diagnostics.append(Diagnostic("CR001", "source_run_id must be a non-empty string"))
        source_run_id = ""
    contract = data.get("contract")
    if contract not in CONTRACTS:
        diagnostics.append(Diagnostic("CR001", f"contract must be one of {list(CONTRACTS)}"))
        contract = "evidence"
    redefines = data.get("redefines", {})
    if not isinstance(redefines, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not key or not value
        for key, value in redefines.items()
    ):
        diagnostics.append(Diagnostic("CR001", "redefines must map group names to branch names"))
        redefines = {}
    raw_items = data.get("corrections")
    if not isinstance(raw_items, list) or not raw_items:
        diagnostics.append(Diagnostic("CR001", "corrections must be a non-empty list"))
        raw_items = []
    items = [parsed for index, config in enumerate(raw_items)
             if (parsed := _item(index, config, diagnostics)) is not None]
    _fail(diagnostics)
    return Manifest(name, source_run_id, contract, dict(redefines), tuple(items), sha256_bytes(raw))
