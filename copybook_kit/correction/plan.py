"""Resolve a correction manifest against a compiled layout and the original bytes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..encode.failure import FieldEncodeError
from ..encode.field import encode_manifest_value
from ..errors import CorrectionError
from ..model import Field, Layout
from ..util import sha256_bytes
from .manifest import Manifest

@dataclass(frozen=True)
class ResolvedLeaf:
    field: Field
    path: str
    offset: int  # record-relative
    length: int
    groups: tuple  # ((redefines target name, branch name), ...) crossed by this leaf

@dataclass(frozen=True)
class PlannedWrite:
    record_no: int
    path: str
    offset: int  # record-relative
    length: int
    encoded: bytes
    value: str
    field: Field

@dataclass(frozen=True)
class CorrectionPlan:
    writes: tuple
    declared: dict  # redefines target name -> chosen branch name
    scope_paths: dict  # record_no -> tuple[ResolvedLeaf, ...] re-encoded by the canonical contract

def _resolve_leaves(layout: Layout):
    """Map every leaf path to its bytes and the REDEFINES branches it sits in."""
    leaves: dict[str, ResolvedLeaf] = {}
    duplicates: set[str] = set()
    registry: dict[str, set] = {}

    def walk(fields: list[Field], prefix: str, base_shift: int, context: tuple) -> None:
        targets = {child.redefines for child in fields if child.redefines}
        for target in sorted(targets):
            branches = (target,) + tuple(child.name for child in fields
                                         if child.redefines == target)
            registry.setdefault(target, set()).add(branches)
        for item in fields:
            for index in range(item.occurs):
                path = f"{prefix}{item.name}{f'[{index}]' if item.occurs > 1 else ''}"
                shift = base_shift + index * item.length
                item_context = context
                if item.redefines is not None:
                    item_context = context + ((item.redefines, item.name),)
                elif item.name in targets:
                    item_context = context + ((item.name, item.name),)
                if item.is_group:
                    walk(item.children, f"{path}.", shift, item_context)
                    continue
                leaf = ResolvedLeaf(item, path, item.offset + shift, item.length, item_context)
                if path in leaves:
                    duplicates.add(path)
                leaves[path] = leaf

    walk(layout.fields, "", 0, ())
    return leaves, duplicates, registry

def _fail(code: str, message: str, **detail: Any) -> None:
    raise CorrectionError(message, code=code, detail=detail)

def _check_declarations(manifest: Manifest, registry: dict) -> dict:
    declared: dict[str, str] = {}
    for choice in manifest.redefines:
        branches = registry.get(choice.group)
        if branches is None:
            _fail("CE006", f"REDEFINES group {choice.group!r} does not exist in the layout",
                  group=choice.group)
        if len(branches) > 1:
            _fail("CE006", f"REDEFINES group name {choice.group!r} is ambiguous in the layout",
                  group=choice.group)
        (branch_tuple,) = branches
        if choice.use not in branch_tuple:
            _fail("CE006", f"branch {choice.use!r} is not part of REDEFINES group "
                  f"{choice.group!r} {list(branch_tuple)}", group=choice.group, use=choice.use)
        declared[choice.group] = choice.use
    return declared

def _check_writes(writes: list[PlannedWrite], leaves: dict, declared: dict,
                  record_length: int) -> None:
    touched: dict[str, set] = {}
    for write in writes:
        for target, branch in leaves[write.path].groups:
            touched.setdefault(target, set()).add(branch)
    for target, branches in sorted(touched.items()):
        record_no = next(write.record_no for write in writes
                         if any(group == target for group, _ in leaves[write.path].groups))
        if len(branches) > 1:
            _fail("CE005", f"corrections modify both branches {sorted(branches)} of REDEFINES "
                  f"group {target!r}", group=target, record_no=record_no)
        if target not in declared:
            _fail("CE006", f"manifest must declare which branch of REDEFINES group {target!r} "
                  "interprets the corrected bytes", group=target, record_no=record_no)
        (branch,) = branches
        if declared[target] != branch:
            _fail("CE006", f"corrections touch branch {branch!r} of REDEFINES group {target!r} "
                  f"but the manifest declares {declared[target]!r}",
                  group=target, use=declared[target], record_no=record_no)
    per_record: dict[int, list[PlannedWrite]] = {}
    for write in writes:
        per_record.setdefault(write.record_no, []).append(write)
    for record_no, items in sorted(per_record.items()):
        ordered = sorted(items, key=lambda write: (write.offset, write.path))
        for previous, current in zip(ordered, ordered[1:]):
            if current.offset < previous.offset + previous.length:
                base = (record_no - 1) * record_length
                _fail("CE005", f"corrections {previous.path!r} and {current.path!r} write "
                      f"overlapping bytes of record {record_no}",
                      record_no=record_no, path=f"{previous.path},{current.path}",
                      byte_offset=base + current.offset)

def build_plan(layout: Layout, manifest: Manifest, input_data: bytes, *,
               max_records: int, charset: str) -> CorrectionPlan:
    leaves, duplicates, registry = _resolve_leaves(layout)
    declared = _check_declarations(manifest, registry)
    record_length = layout.record_length
    full_records = len(input_data) // record_length
    reviewable = min(full_records, max_records) if max_records else full_records
    writes: list[PlannedWrite] = []
    for item in manifest.items:
        if item.record_no > reviewable:
            _fail("CE002", f"record {item.record_no} is beyond the {reviewable} records the "
                  "original run can review", record_no=item.record_no, path=item.path)
        base = (item.record_no - 1) * record_length
        actual = sha256_bytes(input_data[base:base + record_length])
        if actual != item.expected_sha256:
            _fail("CE002", f"record {item.record_no} digest mismatch: manifest expects "
                  f"{item.expected_sha256} but the original record hashes {actual}",
                  record_no=item.record_no, path=item.path)
        if item.path in duplicates:
            _fail("CE003", f"leaf path {item.path!r} is not unique in the layout",
                  record_no=item.record_no, path=item.path)
        leaf = leaves.get(item.path)
        if leaf is None:
            _fail("CE003", f"leaf path {item.path!r} does not exist in the layout",
                  record_no=item.record_no, path=item.path)
        try:
            encoded = encode_manifest_value(leaf.field, item.value, charset)
        except FieldEncodeError as exc:
            detail = {**exc.detail, "record_no": item.record_no, "path": item.path,
                      "byte_offset": base + leaf.offset}
            _fail("CE004", f"record {item.record_no} {item.path}: {exc.message}", **detail)
        writes.append(PlannedWrite(item.record_no, item.path, leaf.offset, leaf.length,
                                   encoded, item.value, leaf.field))
    _check_writes(writes, leaves, declared, record_length)
    in_scope = [leaf for leaf in leaves.values()
                if leaf.path not in duplicates
                and all(declared.get(target) == branch for target, branch in leaf.groups)]
    ordered = tuple(sorted(in_scope, key=lambda leaf: (leaf.offset, leaf.path)))
    scope = {record_no: ordered for record_no in {write.record_no for write in writes}}
    return CorrectionPlan(tuple(writes), declared, scope)
