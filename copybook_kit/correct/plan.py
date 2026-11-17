"""Resolve a manifest against the layout and source bytes into a verified write plan."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any

from ..decode.display import decode_field
from ..decode.failure import FieldDecodeError
from ..errors import CorrectionError, Diagnostic
from ..model import Field, Layout, LeafOccurrence, walk_leaves
from ..util import sha256_bytes, sha8
from .encode import EncodeError, encode_field, expected_value, proof_decode
from .manifest import CorrectionItem, Manifest

@dataclass(frozen=True)
class RedefinesGroup:
    key: str
    target: Field
    redefiners: tuple[Field, ...]

    @property
    def branches(self) -> tuple[Field, ...]:
        return (self.target, *self.redefiners)

    def branch_named(self, name: str) -> Field | None:
        return next((branch for branch in self.branches if branch.name == name), None)

def find_redefines_groups(layout: Layout) -> list[RedefinesGroup]:
    collected: dict[int, dict[str, Any]] = {}

    def visit(siblings: list[Field]) -> None:
        seen: dict[str, Field] = {}
        for item in siblings:
            if item.redefines and item.redefines in seen:
                target = seen[item.redefines]
                collected.setdefault(id(target), {"target": target, "redefiners": []})
                collected[id(target)]["redefiners"].append(item)
            seen[item.name] = item
            visit(item.children)

    visit(layout.fields)
    return [RedefinesGroup(entry["target"].name, entry["target"], tuple(entry["redefiners"]))
            for entry in collected.values()]

def layout_leaves(layout: Layout) -> list[LeafOccurrence]:
    """Leaf occurrences with the same paths decode_record produces (no 01-root prefix)."""
    leaves: list[LeafOccurrence] = []
    for root in layout.fields:
        leaves.extend(walk_leaves(root.children if root.is_group else [root]))
    return leaves

def _branch_membership(groups: list[RedefinesGroup]) -> dict[int, tuple[RedefinesGroup, Field]]:
    membership: dict[int, tuple[RedefinesGroup, Field]] = {}
    for group in groups:
        for branch in group.branches:
            for leaf in walk_leaves([branch]):
                membership[id(leaf.field)] = (group, branch)
    return membership

@dataclass(frozen=True)
class WriteOp:
    record_no: int
    record_offset: int
    length: int
    data: bytes
    reason: str
    path: str | None

@dataclass(frozen=True)
class CorrectionEvidence:
    record_no: int
    path: str
    record_offset: int
    byte_offset: int
    length: int
    old_bytes: bytes
    new_bytes: bytes
    old_value: Any
    new_value: Any
    old_decode_error: str | None

@dataclass(frozen=True)
class _PreparedItem:
    item: CorrectionItem
    leaf: LeafOccurrence
    encoded: bytes
    old_bytes: bytes
    old_value: Any
    old_decode_error: str | None
    new_value: Any

@dataclass
class CorrectionPlan:
    record_length: int
    writes: list[WriteOp]
    evidence: list[CorrectionEvidence]
    redefines: dict[str, str]

    def apply(self, source: bytes) -> tuple[bytes, list[dict[str, Any]], bool]:
        """Apply writes and return (new bytes, diff ranges, unchanged-verified)."""
        buffer = bytearray(source)
        diffs: list[dict[str, Any]] = []
        for write in self.writes:
            start = (write.record_no - 1) * self.record_length + write.record_offset
            old = bytes(buffer[start:start + write.length])
            if old == write.data:
                continue
            buffer[start:start + write.length] = write.data
            diffs.append({"record_no": write.record_no, "byte_offset": start,
                          "length": write.length, "reason": write.reason, "path": write.path,
                          "old_sha8": sha8(old), "new_sha8": sha8(write.data)})
        new = bytes(buffer)
        differing = {index for index, pair in enumerate(zip(source, new)) if pair[0] != pair[1]}
        covered = set()
        for diff in diffs:
            covered.update(range(diff["byte_offset"], diff["byte_offset"] + diff["length"]))
        verified = len(new) == len(source) and differing <= covered
        return new, diffs, verified

def _decode_old(field: Field, raw: bytes, charset: str, policy: str) -> tuple[Any, str | None]:
    try:
        return decode_field(field, raw, charset, policy), None
    except FieldDecodeError as exc:
        return None, exc.code

def _prepare_item(item: CorrectionItem, leaf: LeafOccurrence, record: bytes, absolute: int,
                  charset: str, policies: dict[str, str],
                  diagnostics: list[Diagnostic]) -> _PreparedItem | None:
    offset = absolute + leaf.offset
    old_bytes = record[leaf.offset:leaf.offset + leaf.length]
    policy = policies.get(leaf.field.name, "null")
    try:
        encoded = encode_field(leaf.field, item.value, charset)
        expected = expected_value(leaf.field, item.value, policy)
        decoded = proof_decode(leaf.field, encoded, charset, policy)
    except EncodeError as exc:
        diagnostics.append(Diagnostic("CR004", exc.message, record_no=item.record_no,
                                      field=item.path, byte_offset=offset, detail=exc.detail))
        return None
    if decoded != expected:
        diagnostics.append(Diagnostic(
            "CR004", f"encoded bytes decode to {decoded!r}, not the approved value {expected!r}",
            record_no=item.record_no, field=item.path, byte_offset=offset))
        return None
    old_value, old_error = _decode_old(leaf.field, old_bytes, charset, policy)
    return _PreparedItem(item, leaf, encoded, old_bytes, old_value, old_error, decoded)

def _check_conflicts(record_no: int, items: list[_PreparedItem], absolute: int,
                     membership: dict[int, tuple[RedefinesGroup, Field]],
                     diagnostics: list[Diagnostic]) -> None:
    for first, second in combinations(items, 2):
        a, b = first.leaf, second.leaf
        low = max(a.offset, b.offset)
        high = min(a.offset + a.length, b.offset + b.length)
        if low >= high:
            continue
        if first.item.path == second.item.path:
            reason = "duplicate_path"
        else:
            first_member = membership.get(id(a.field))
            second_member = membership.get(id(b.field))
            reason = ("redefines_branches" if first_member and second_member
                      and first_member[0] is second_member[0]
                      and first_member[1] is not second_member[1] else "overlap")
        diagnostics.append(Diagnostic(
            "CR005", f"corrections {first.item.path} and {second.item.path} both write "
            f"record bytes [{low}, {high}) of record {record_no}",
            record_no=record_no, field=second.item.path, byte_offset=absolute + low,
            detail={"reason": reason,
                    "first": {"path": first.item.path, "offset": a.offset, "length": a.length},
                    "second": {"path": second.item.path, "offset": b.offset, "length": b.length}}))

def _canonical_writes(record_no: int, record: bytes, absolute: int,
                      corrected: dict[str, _PreparedItem], layout: Layout,
                      declared: dict[str, str],
                      membership: dict[int, tuple[RedefinesGroup, Field]],
                      charset: str, policies: dict[str, str],
                      diagnostics: list[Diagnostic]) -> list[WriteOp]:
    writes: list[WriteOp] = []
    for leaf in layout_leaves(layout):
        if leaf.path in corrected:
            continue
        member = membership.get(id(leaf.field))
        if member is not None:
            group, branch = member
            if declared.get(group.key) != branch.name:
                continue  # unselected or undeclared branch coverage keeps original bytes
        old = record[leaf.offset:leaf.offset + leaf.length]
        policy = policies.get(leaf.field.name, "null")
        try:
            value = decode_field(leaf.field, old, charset, policy)
        except FieldDecodeError:
            continue  # undecodable bytes cannot be proven value-preserving; keep them
        try:
            encoded = encode_field(leaf.field, value, charset)
        except EncodeError as exc:
            diagnostics.append(Diagnostic("CR007", f"cannot re-encode {leaf.path} "
                                          f"canonically: {exc.message}",
                                          record_no=record_no, field=leaf.path,
                                          byte_offset=absolute + leaf.offset))
            continue
        try:
            decoded = decode_field(leaf.field, encoded, charset, policy)
        except FieldDecodeError as exc:
            diagnostics.append(Diagnostic("CR007", f"re-encoded {leaf.path} fails to decode: "
                                          f"{exc.message}", record_no=record_no, field=leaf.path,
                                          byte_offset=absolute + leaf.offset))
            continue
        if decoded != value:
            diagnostics.append(Diagnostic(
                "CR007", f"re-encoding {leaf.path} would change its business value "
                f"from {value!r} to {decoded!r}",
                record_no=record_no, field=leaf.path, byte_offset=absolute + leaf.offset))
            continue
        if encoded != old:
            writes.append(WriteOp(record_no, leaf.offset, leaf.length, encoded,
                                  "recanonicalize", leaf.path))
    return writes

def build_plan(manifest: Manifest, layout: Layout, source: bytes, *, charset: str,
               blank_policies: dict[str, str]) -> CorrectionPlan:
    """Validate the whole batch and return the write plan; reject atomically on any error."""
    diagnostics: list[Diagnostic] = []
    record_length = layout.record_length
    full_records = len(source) // record_length
    groups = find_redefines_groups(layout)
    groups_by_key: dict[str, list[RedefinesGroup]] = {}
    for group in groups:
        groups_by_key.setdefault(group.key, []).append(group)
    membership = _branch_membership(groups)
    leaf_index: dict[str, list[LeafOccurrence]] = {}
    for leaf in layout_leaves(layout):
        leaf_index.setdefault(leaf.path, []).append(leaf)

    declared = dict(manifest.redefines)
    for key, branch_name in sorted(declared.items()):
        matches = groups_by_key.get(key, [])
        if len(matches) != 1:
            diagnostics.append(Diagnostic(
                "CR006", f"redefines group {key!r} does not exist uniquely in the layout "
                f"({len(matches)} matches)"))
        elif matches[0].branch_named(branch_name) is None:
            diagnostics.append(Diagnostic(
                "CR006", f"branch {branch_name!r} is not part of redefines group {key!r}; "
                f"choose one of {[branch.name for branch in matches[0].branches]}"))

    prepared: list[_PreparedItem] = []
    touched: set[str] = set()
    for item in manifest.corrections:
        if item.record_no > full_records:
            diagnostics.append(Diagnostic(
                "CR001", f"record_no {item.record_no} exceeds the {full_records} complete "
                f"records of the source input", record_no=item.record_no, field=item.path))
            continue
        absolute = (item.record_no - 1) * record_length
        record = source[absolute:absolute + record_length]
        actual = sha256_bytes(record)
        if actual != item.expected_sha256:
            diagnostics.append(Diagnostic(
                "CR002", f"record {item.record_no} digest {actual} no longer matches the "
                f"approved {item.expected_sha256}",
                record_no=item.record_no, field=item.path, byte_offset=absolute))
            continue
        matches = leaf_index.get(item.path, [])
        if len(matches) != 1:
            diagnostics.append(Diagnostic(
                "CR003", f"path {item.path!r} resolves to {len(matches)} leaves in the "
                f"current layout; it must resolve to exactly one",
                record_no=item.record_no, field=item.path))
            continue
        leaf = matches[0]
        member = membership.get(id(leaf.field))
        if member is not None:
            group, branch = member
            touched.add(group.key)
            selected = declared.get(group.key)
            if selected is None:
                diagnostics.append(Diagnostic(
                    "CR006", f"path {item.path} belongs to redefines group {group.key}; the "
                    f"manifest must declare which branch interprets the result",
                    record_no=item.record_no, field=item.path,
                    byte_offset=absolute + leaf.offset))
            elif selected != branch.name:
                diagnostics.append(Diagnostic(
                    "CR006", f"path {item.path} is stored in branch {branch.name} but the "
                    f"manifest selects {selected} for group {group.key}",
                    record_no=item.record_no, field=item.path,
                    byte_offset=absolute + leaf.offset))
        prepared_item = _prepare_item(item, leaf, record, absolute, charset,
                                      blank_policies, diagnostics)
        if prepared_item is not None:
            prepared.append(prepared_item)

    by_record: dict[int, list[_PreparedItem]] = {}
    for prepared_item in prepared:
        by_record.setdefault(prepared_item.item.record_no, []).append(prepared_item)
    for record_no, items in sorted(by_record.items()):
        _check_conflicts(record_no, items, (record_no - 1) * record_length,
                         membership, diagnostics)

    for key in sorted(declared):
        if key in groups_by_key and len(groups_by_key[key]) == 1 and key not in touched:
            diagnostics.append(Diagnostic(
                "CR006", f"redefines group {key!r} is declared but no correction touches it"))

    if diagnostics:
        raise CorrectionError(diagnostics)

    writes: list[WriteOp] = []
    evidence: list[CorrectionEvidence] = []
    for record_no, items in sorted(by_record.items()):
        absolute = (record_no - 1) * record_length
        record = source[absolute:absolute + record_length]
        corrected = {entry.item.path: entry for entry in items}
        for entry in items:
            writes.append(WriteOp(record_no, entry.leaf.offset, entry.leaf.length,
                                  entry.encoded, "correction", entry.item.path))
            evidence.append(CorrectionEvidence(
                record_no, entry.item.path, entry.leaf.offset, absolute + entry.leaf.offset,
                entry.leaf.length, entry.old_bytes, entry.encoded, entry.old_value,
                entry.new_value, entry.old_decode_error))
        if manifest.contract == "canonical":
            writes.extend(_canonical_writes(
                record_no, record, absolute, corrected, layout, declared, membership,
                charset, blank_policies, diagnostics))
    if diagnostics:
        raise CorrectionError(diagnostics)
    writes.sort(key=lambda write: (write.record_no, write.record_offset))
    return CorrectionPlan(record_length, writes, evidence, declared)
