"""Correction manifest loading and schema validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import CorrectionError, InputError
from ..util import sha256_bytes

PATH_RE = re.compile(r"^[A-Z][A-Z0-9-]*(\[\d+\])?(\.[A-Z][A-Z0-9-]*(\[\d+\])?)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOP_KEYS = {"schema_version", "name", "redefines", "corrections"}
ITEM_KEYS = {"run", "record_no", "path", "expected_sha256", "value"}
CHOICE_KEYS = {"group", "use"}
MAX_CORRECTIONS = 10000

@dataclass(frozen=True)
class CorrectionItem:
    run: str
    record_no: int
    path: str
    expected_sha256: str
    value: str

@dataclass(frozen=True)
class RedefinesChoice:
    group: str
    use: str

@dataclass(frozen=True)
class Manifest:
    name: str
    run_id: str
    items: tuple[CorrectionItem, ...]
    redefines: tuple[RedefinesChoice, ...]
    sha256: str
    path: str

def _fail(message: str) -> None:
    raise CorrectionError(message, code="CE001")

def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate manifest key {key!r}")
        result[key] = value
    return result

def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    return value

def _item(raw: Any, index: int) -> CorrectionItem:
    if not isinstance(raw, dict):
        _fail(f"corrections[{index}] must be an object")
    if unknown := set(raw) - ITEM_KEYS:
        _fail(f"corrections[{index}] has unknown keys: {sorted(unknown)}")
    if missing := ITEM_KEYS - set(raw):
        _fail(f"corrections[{index}] is missing keys: {sorted(missing)}")
    run = _string(raw["run"], f"corrections[{index}].run")
    record_no = raw["record_no"]
    if isinstance(record_no, bool) or not isinstance(record_no, int) or record_no < 1:
        _fail(f"corrections[{index}].record_no must be a positive integer")
    path = _string(raw["path"], f"corrections[{index}].path")
    if not PATH_RE.fullmatch(path):
        _fail(f"corrections[{index}].path {path!r} is not a copybook leaf path")
    expected = raw["expected_sha256"]
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        _fail(f"corrections[{index}].expected_sha256 must be 64 lowercase hex characters")
    if not isinstance(raw["value"], str):
        _fail(f"corrections[{index}].value must be a string")
    return CorrectionItem(run, record_no, path, expected, raw["value"])

def _choice(raw: Any, index: int) -> RedefinesChoice:
    if not isinstance(raw, dict) or set(raw) != CHOICE_KEYS:
        _fail(f"redefines[{index}] must be an object with exactly {sorted(CHOICE_KEYS)}")
    return RedefinesChoice(_string(raw["group"], f"redefines[{index}].group"),
                           _string(raw["use"], f"redefines[{index}].use"))

def parse_manifest(data: Any, *, sha256: str, path: str) -> Manifest:
    if not isinstance(data, dict):
        _fail("manifest must be a JSON object")
    if unknown := set(data) - TOP_KEYS:
        _fail(f"unknown top-level keys: {sorted(unknown)}")
    if data.get("schema_version") != 1:
        _fail("schema_version must be 1")
    name = data.get("name", "")
    if not isinstance(name, str):
        _fail("name must be a string")
    raw_items = data.get("corrections")
    if not isinstance(raw_items, list) or not raw_items:
        _fail("corrections must be a non-empty list")
    if len(raw_items) > MAX_CORRECTIONS:
        _fail(f"corrections exceeds {MAX_CORRECTIONS} items")
    items = tuple(_item(raw, index) for index, raw in enumerate(raw_items))
    run_ids = {item.run for item in items}
    if len(run_ids) != 1:
        _fail("all corrections must name the same original run")
    raw_choices = data.get("redefines", [])
    if not isinstance(raw_choices, list):
        _fail("redefines must be a list")
    choices = tuple(_choice(raw, index) for index, raw in enumerate(raw_choices))
    if len({choice.group for choice in choices}) != len(choices):
        _fail("redefines declares the same group twice")
    return Manifest(name, run_ids.pop(), items, choices, sha256, path)

def load_manifest(path: Path) -> Manifest:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read manifest {path}: {exc}", code="IO001") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorrectionError(f"manifest must be UTF-8 text: {exc}", code="CE001") from exc
    try:
        data = json.loads(text, object_pairs_hook=_object)
    except json.JSONDecodeError as exc:
        raise CorrectionError(f"manifest is not valid JSON: {exc}", code="CE001") from exc
    return parse_manifest(data, sha256=sha256_bytes(raw), path=str(path))
