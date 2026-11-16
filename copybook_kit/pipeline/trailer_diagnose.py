"""Read-only offline diagnosis of an FL002 trailer-amount mismatch.

The diagnosis never mutates the original run, its input or its evidence
artifacts.  It re-derives every conclusion from the stored inputs:

1. the run registration, its inputs/copybook/rules and every stored raw byte
   are cross-checked against the current files (``evidence`` block);
2. data rows, the trailer row and the FL002 issue are replayed with the same
   decoder and :class:`~copybook_kit.rules.engine.RuleEngine`;
3. when the evidence is trustworthy, two honest business routes are offered,
   each conditional on an explicit authority decision -- the trailer is
   trusted (single COMP-3 sign-nibble flips on data rows) or the data rows are
   trusted (the trailer value that *should* have been signed).

An unexplained difference is never attributed to a record: when any premise
cannot be verified, the conclusion is ``insufficient_evidence``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..copybook.layout import compile_copybook
from ..decode.record import decode_record
from ..model import Layout, Record
from ..rules.checks import ValidationState, first_value
from ..rules.engine import RuleEngine, assign_record_type
from ..rules.loader import load_rules
from ..util import decimal_text, sha256_bytes
from .reader import read_records

DEFAULT_NODE_BUDGET = 200_000
TRAILER_TRUSTED = "trailer_trusted"
DATA_TRUSTED = "data_trusted"
ROUTES = (TRAILER_TRUSTED, DATA_TRUSTED)


@dataclass(frozen=True)
class _Contract:
    layout: Layout
    rules: Any
    amount_field: str
    amount_leaf_offset: int
    amount_leaf_length: int
    amount_digits: int
    amount_scale: int
    amount_usage: str
    charset: str
    record_length: int
    recfm: str
    max_records: int


@dataclass(frozen=True)
class _Candidate:
    record_no: int
    byte_offset: int
    sign_byte_relative: int
    sign_byte_absolute: int
    sign_nibble: str
    candidate_sign_nibble: str
    original_amount: Decimal
    candidate_amount: Decimal
    delta_scaled: int
    original_sha256: str
    candidate_sha256: str
    candidate_raw: bytes


def _insufficient(reason_code: str, message: str, *, evidence: dict[str, Any] | None = None,
                  replay: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "conclusion": "insufficient_evidence",
        "reason_code": reason_code,
        "message": message,
    }
    if evidence:
        payload["evidence"] = evidence
    if replay:
        payload["replay"] = replay
    return payload


def _rule_summary(rules: Any) -> dict[str, Any]:
    trailer = rules.trailer
    return {
        "name": rules.name,
        "version": rules.version,
        "record_type": None if rules.record_type is None else {
            "field": rules.record_type.field,
            "data": rules.record_type.data,
            "header": rules.record_type.header,
            "trailer": rules.record_type.trailer,
        },
        "trailer": None if trailer is None else {
            "count_field": trailer.count_field,
            "amount_field": trailer.amount_field,
        },
        "field_rules": sorted(rules.fields),
        "record_rules": [rule.name for rule in rules.record_rules],
        "limits": {"min_records": rules.min_records, "max_records": rules.max_records},
    }


def _load_contract(run: dict[str, Any]) -> _Contract | dict[str, Any]:
    copybook_path, rules_path = Path(run["copybook_path"]), Path(run["rules_path"])
    try:
        copybook_bytes = copybook_path.read_bytes()
        rules_bytes = rules_path.read_bytes()
    except OSError as exc:
        return _insufficient("CONTRACT_UNREADABLE",
                             f"contract file cannot be read: {exc}")
    try:
        layout = compile_copybook(copybook_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return _insufficient("CONTRACT_UNREADABLE", f"copybook is not UTF-8: {exc}")
    if not layout.valid:
        return _insufficient("CONTRACT_INVALID", "stored copybook no longer compiles cleanly")
    try:
        rules = load_rules(rules_path, layout)
    except Exception as exc:  # rules contracts carry their own stable codes
        return _insufficient("CONTRACT_INVALID", f"stored rules no longer load: {exc}")
    if rules.record_type is None or rules.trailer is None or not rules.trailer.amount_field:
        return _insufficient("NO_TRAILER_AMOUNT_RULE",
                             "the stored rules do not declare a trailer amount_field")
    amount_name = rules.trailer.amount_field
    field = layout.find_field(amount_name)
    if field is None:
        return _insufficient("AMOUNT_FIELD_MISSING",
                             f"amount field {amount_name!r} is not in the compiled layout")
    # The engine's first_value/values_for semantics use the first matching leaf.
    first_leaf = next(leaf for leaf in layout.leaves() if leaf.field.name == amount_name)
    return _Contract(
        layout=layout, rules=rules, amount_field=amount_name,
        amount_leaf_offset=first_leaf.offset, amount_leaf_length=first_leaf.length,
        amount_digits=field.digits, amount_scale=field.scale, amount_usage=field.usage,
        charset=run["charset"], record_length=layout.record_length,
        recfm=run["recfm"], max_records=int(run["max_records"]),
    )


def _verify_evidence(run: dict[str, Any], input_data: bytes,
                     contract: _Contract) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return (insufficient payload or None, the collected evidence block)."""
    evidence: dict[str, Any] = {"records": []}
    actual_input_sha = sha256_bytes(input_data)
    evidence["input"] = {
        "path": run["input_path"],
        "stored_sha256": run["input_sha256"],
        "actual_sha256": actual_input_sha,
        "stored_size": int(run["input_size"]),
        "actual_size": len(input_data),
        "matches": actual_input_sha == run["input_sha256"]
        and len(input_data) == int(run["input_size"]),
    }
    if not evidence["input"]["matches"]:
        return _insufficient("INPUT_CHANGED",
                             "the input file bytes differ from the bytes of the recorded run",
                             evidence=evidence), evidence
    for label, key, path_text in (
        ("copybook", "copybook_sha256", run["copybook_path"]),
        ("rules", "rules_sha256", run["rules_path"]),
    ):
        try:
            raw = Path(path_text).read_bytes()
        except OSError as exc:
            return _insufficient("CONTRACT_UNREADABLE", f"{label} cannot be read: {exc}",
                                 evidence=evidence), evidence
        actual = sha256_bytes(raw)
        evidence[label] = {"path": path_text, "stored_sha256": run[key],
                           "actual_sha256": actual, "matches": actual == run[key]}
        if actual != run[key]:
            return _insufficient("CONTRACT_CHANGED",
                                 f"the {label} bytes differ from the recorded run",
                                 evidence=evidence), evidence

    read = read_records(input_data, contract.record_length,
                        max_records=contract.max_records, recfm=contract.recfm)
    blocking = [issue.to_dict() for issue in read.file_issues
                if issue.code in {"RD002", "RD003", "RD004"}]
    evidence["read_issues"] = [issue.to_dict() for issue in read.file_issues]
    if blocking:
        return _insufficient("INPUT_NOT_FULLY_READABLE",
                             "the stored input is empty, truncated or was read under a max-records guard",
                             evidence=evidence), evidence

    run_dir = Path(run["out_dir"])
    try:
        accepted_lines = [json.loads(line) for line in
                          (run_dir / "accepted.jsonl").read_text(encoding="utf-8").splitlines()
                          if line]
        rejects_lines = [json.loads(line) for line in
                         (run_dir / "rejects.jsonl").read_text(encoding="utf-8").splitlines()
                         if line]
    except OSError as exc:
        return _insufficient("EVIDENCE_ARTIFACT_MISSING",
                             f"run evidence artifact cannot be read: {exc}",
                             evidence=evidence), evidence
    stored = {item["record_no"]: item for item in accepted_lines + rejects_lines}

    engine = RuleEngine(contract.rules)
    state = ValidationState()
    records: list[Record] = []
    for raw in read.records:
        record = decode_record(contract.layout, raw.data, record_no=raw.record_no,
                               byte_offset=raw.byte_offset, charset=contract.charset,
                               blank_policies=contract.rules.blank_policies)
        assign_record_type(record, contract.rules)
        if not record.errors:
            record.hits = engine.validate_record(record, state)
        records.append(record)

        entry = stored.get(record.record_no)
        if entry is None:
            return _insufficient("EVIDENCE_ARTIFACT_MISSING",
                                 f"record {record.record_no} has no stored evidence entry",
                                 evidence=evidence), evidence
        actual_sha = sha256_bytes(record.raw)
        record_evidence = {
            "record_no": record.record_no,
            "byte_offset": record.byte_offset,
            "stored_sha256": entry.get("raw_sha256"),
            "actual_sha256": actual_sha,
            "matches": entry.get("raw_sha256") == actual_sha,
            "record_type": record.record_type,
            "has_decode_errors": bool(record.errors),
            "has_rule_hits": bool(record.hits),
        }
        evidence["records"].append(record_evidence)
        if not record_evidence["matches"]:
            return _insufficient("SOURCE_BYTES_CHANGED",
                                 f"record {record.record_no} raw bytes differ from the stored evidence",
                                 evidence=evidence), evidence
        # Rejected records additionally keep the quarantined raw bytes on disk.
        if not record.accepted:
            raw_file = entry.get("raw_file")
            if not raw_file:
                return _insufficient("EVIDENCE_ARTIFACT_MISSING",
                                     f"rejected record {record.record_no} has no raw_file evidence",
                                     evidence=evidence), evidence
            try:
                quarantined = (run_dir / raw_file).read_bytes()
            except OSError as exc:
                return _insufficient("EVIDENCE_ARTIFACT_MISSING",
                                     f"quarantined bytes for record {record.record_no} unreadable: {exc}",
                                     evidence=evidence), evidence
            if sha256_bytes(quarantined) != actual_sha:
                return _insufficient("SOURCE_BYTES_CHANGED",
                                     f"quarantined bytes for record {record.record_no} differ from the input",
                                     evidence=evidence), evidence

    if set(stored) != {record.record_no for record in records}:
        return _insufficient("EVIDENCE_ARTIFACT_MISSING",
                             "stored evidence entries do not match the records replayed from the input",
                             evidence=evidence), evidence
    evidence["record_count"] = len(records)
    evidence["all_record_bytes_match"] = True
    return None, evidence


def _replay(contract: _Contract, input_data: bytes) -> dict[str, Any]:
    """Decode and validate the stored input exactly as the pipeline would."""
    read = read_records(input_data, contract.record_length,
                        max_records=contract.max_records, recfm=contract.recfm)
    engine, state = RuleEngine(contract.rules), ValidationState()
    records: list[Record] = []
    for raw in read.records:
        record = decode_record(contract.layout, raw.data, record_no=raw.record_no,
                               byte_offset=raw.byte_offset, charset=contract.charset,
                               blank_policies=contract.rules.blank_policies)
        assign_record_type(record, contract.rules)
        if not record.errors:
            record.hits = engine.validate_record(record, state)
        records.append(record)
    issues = list(read.file_issues)
    if not any(issue.code == "RD004" for issue in read.file_issues):
        issues.extend(engine.validate_file(records))
    marker = contract.rules.record_type
    data_records = [record for record in records if record.record_type == marker.data]
    trailers = [record for record in records if record.record_type == marker.trailer]
    return {
        "records": records,
        "data_records": data_records,
        "trailer": trailers[-1] if trailers else None,
        "issues": issues,
        "read_issue_codes": sorted(issue.code for issue in read.file_issues),
    }


def _amount_value(contract: _Contract, record: Record) -> Decimal | None:
    value = first_value(record, contract.amount_field)
    return value if isinstance(value, Decimal) else None


def _amount_sign_nibble(contract: _Contract, record: Record) -> int | None:
    if contract.amount_usage != "COMP-3":
        return None
    start = contract.amount_leaf_offset
    field_bytes = record.raw[start:start + contract.amount_leaf_length]
    if len(field_bytes) != contract.amount_leaf_length:
        return None
    return field_bytes[-1] & 0x0F


def _flipped_raw(contract: _Contract, record: Record) -> bytes:
    start = contract.amount_leaf_offset
    index = start + contract.amount_leaf_length - 1
    sign = record.raw[index] & 0x0F
    flipped = 0x0D if sign == 0x0C else 0x0C
    raw = bytearray(record.raw)
    raw[index] = (raw[index] & 0xF0) | flipped
    return bytes(raw)


def _replay_with_flips(contract: _Contract, input_data: bytes,
                       flips: dict[int, bytes]) -> dict[str, Any]:
    """Replay the whole file after replacing selected data rows' raw bytes."""
    read = read_records(input_data, contract.record_length,
                        max_records=contract.max_records, recfm=contract.recfm)
    engine, state = RuleEngine(contract.rules), ValidationState()
    records: list[Record] = []
    for raw in read.records:
        data = flips.get(raw.record_no, raw.data)
        record = decode_record(contract.layout, data, record_no=raw.record_no,
                               byte_offset=raw.byte_offset, charset=contract.charset,
                               blank_policies=contract.rules.blank_policies)
        assign_record_type(record, contract.rules)
        if not record.errors:
            record.hits = engine.validate_record(record, state)
        records.append(record)
    issues = list(read.file_issues)
    if not any(issue.code == "RD004" for issue in read.file_issues):
        issues.extend(engine.validate_file(records))
    marker = contract.rules.record_type
    data_records = [record for record in records if record.record_type == marker.data]
    total = Decimal(0)
    missing = False
    for record in data_records:
        value = _amount_value(contract, record)
        if value is None:
            missing = True
        else:
            total += value
    return {"records": records, "issues": issues, "computed_total": total,
            "missing_amount": missing}


def _scaled_int(value: Decimal, scale: int) -> int | None:
    """Exact integer representation at ``scale`` places; None when it needs more places."""
    try:
        return int(value.scaleb(scale))
    except (ValueError, OverflowError):
        return None


def _all_minimal_subsets(candidates: list[_Candidate], target: int,
                         node_budget: int) -> tuple[list[tuple[int, ...]], str, int]:
    """Enumerate every minimum-cardinality subset whose scaled deltas hit target.

    Returns (subsets as tuples of candidate indices, status, nodes_visited).
    ``status`` is ``exhaustive`` when the whole search tree was visited within
    ``node_budget`` nodes, otherwise ``budget_exhausted``.
    """
    order = sorted(range(len(candidates)),
                   key=lambda index: (-abs(candidates[index].delta_scaled),
                                      candidates[index].record_no))
    deltas = [candidates[index].delta_scaled for index in order]
    suffix_sum: list[int] = [0] * (len(deltas) + 1)
    for index in range(len(deltas) - 1, -1, -1):
        suffix_sum[index] = suffix_sum[index + 1] + deltas[index]

    nodes = 0
    exhausted = True
    solutions: list[tuple[int, ...]] = []

    def level_dfs(start: int, remaining: int, chosen: list[int], depth_left: int) -> None:
        nonlocal nodes, exhausted
        nodes += 1
        if nodes > node_budget:
            exhausted = False
            return
        if depth_left == 0:
            if remaining == 0:
                solutions.append(tuple(chosen))
            return
        if start >= len(deltas):
            return
        # With negative deltas present, suffix sums are not a sound prune.
        if all(delta >= 0 for delta in deltas[start:]) and remaining > suffix_sum[start]:
            return
        upper = len(deltas) - depth_left
        for index in range(start, upper + 1):
            if not exhausted:
                return
            chosen.append(index)
            level_dfs(index + 1, remaining - deltas[index], chosen, depth_left - 1)
            chosen.pop()

    for cardinality in range(1, len(deltas) + 1):
        solutions_before = len(solutions)
        level_dfs(0, target, [], cardinality)
        if not exhausted:
            break
        if len(solutions) > solutions_before:
            return solutions, "exhaustive", nodes
    return solutions, ("exhaustive" if exhausted else "budget_exhausted"), nodes


def _build_candidates(contract: _Contract, replay: dict[str, Any],
                      input_data: bytes) -> tuple[list[_Candidate], list[dict[str, Any]]]:
    """Only originally clean data rows with a reversible C/D nibble are candidates."""
    candidates: list[_Candidate] = []
    excluded: list[dict[str, Any]] = []
    for record in replay["data_records"]:
        base = {
            "record_no": record.record_no,
            "byte_offset": record.byte_offset,
            "record_type": record.record_type,
        }
        if record.errors:
            excluded.append({**base, "reason": "decode_error",
                             "codes": sorted(item.code for item in record.errors)})
            continue
        if record.hits:
            excluded.append({**base, "reason": "field_or_record_rejection",
                             "codes": sorted(hit.code for hit in record.hits)})
            continue
        if contract.amount_usage != "COMP-3":
            excluded.append({**base, "reason": "amount_not_comp3",
                             "usage": contract.amount_usage})
            continue
        sign = _amount_sign_nibble(contract, record)
        if sign is None:
            excluded.append({**base, "reason": "amount_not_readable"})
            continue
        if sign == 0x0F:
            # F is positive but its original C/F provenance cannot be restored
            # unambiguously, so an F->D change is not a reversible candidate.
            excluded.append({**base, "reason": "ambiguous_f_sign_nibble",
                             "sign_nibble": "F"})
            continue
        if sign not in {0x0C, 0x0D}:
            excluded.append({**base, "reason": "invalid_sign_nibble",
                             "sign_nibble": f"{sign:X}"})
            continue
        original_amount = _amount_value(contract, record)
        if original_amount is None:
            excluded.append({**base, "reason": "amount_not_readable"})
            continue
        candidate_raw = _flipped_raw(contract, record)
        candidate_replay = _replay_with_flips(contract, input_data,
                                               {record.record_no: candidate_raw})
        candidate_record = next(item for item in candidate_replay["records"]
                                if item.record_no == record.record_no)
        if candidate_record.errors or candidate_record.hits:
            excluded.append({
                **base,
                "reason": "flip_breaks_rules",
                "codes": sorted({item.code for item in candidate_record.errors}
                                | {hit.code for hit in candidate_record.hits}),
            })
            continue
        candidate_amount = _amount_value(contract, candidate_record)
        if candidate_amount is None:
            excluded.append({**base, "reason": "amount_not_readable"})
            continue
        sign_relative = contract.amount_leaf_offset + contract.amount_leaf_length - 1
        delta = _scaled_int(candidate_amount - original_amount, contract.amount_scale)
        if delta is None or delta == 0:
            excluded.append({**base, "reason": "flip_not_reversible"})
            continue
        candidates.append(_Candidate(
            record_no=record.record_no,
            byte_offset=record.byte_offset,
            sign_byte_relative=sign_relative,
            sign_byte_absolute=record.byte_offset + sign_relative,
            sign_nibble=f"{sign:X}",
            candidate_sign_nibble="D" if sign == 0x0C else "C",
            original_amount=original_amount,
            candidate_amount=candidate_amount,
            delta_scaled=delta,
            original_sha256=sha256_bytes(record.raw),
            candidate_sha256=sha256_bytes(candidate_raw),
            candidate_raw=candidate_raw,
        ))
    candidates.sort(key=lambda item: item.record_no)
    excluded.sort(key=lambda item: item["record_no"])
    return candidates, excluded


def _candidate_payload(candidate: _Candidate, contract: _Contract) -> dict[str, Any]:
    return {
        "record_no": candidate.record_no,
        "byte_offset": candidate.byte_offset,
        "amount_field": contract.amount_field,
        "sign_byte_relative_offset": candidate.sign_byte_relative,
        "sign_byte_absolute_offset": candidate.sign_byte_absolute,
        "original_sign_nibble": candidate.sign_nibble,
        "candidate_sign_nibble": candidate.candidate_sign_nibble,
        "original_amount": decimal_text(candidate.original_amount),
        "candidate_amount": decimal_text(candidate.candidate_amount),
        "original_record_sha256": candidate.original_sha256,
        "candidate_record_sha256": candidate.candidate_sha256,
    }


def _trailer_trusted_route(contract: _Contract, replay: dict[str, Any],
                           input_data: bytes, declared: Decimal, computed: Decimal,
                           node_budget: int) -> dict[str, Any]:
    candidates, excluded = _build_candidates(contract, replay, input_data)
    scale = contract.amount_scale
    required = _scaled_int(declared - computed, scale)
    route: dict[str, Any] = {
        "route": TRAILER_TRUSTED,
        "authority": "operations confirms the trailer was signed by an independent system",
        "declared_trailer_amount": decimal_text(declared),
        "computed_data_total": decimal_text(computed),
        "required_total_change_scaled": required,
        "candidates": [_candidate_payload(item, contract) for item in candidates],
        "excluded_data_records": excluded,
        "search": {"node_budget": node_budget},
    }
    if required is None:
        route["search"].update(status="exhaustive", nodes_visited=0)
        route["conclusion"] = "no_feasible_set"
        route["message"] = ("the trailer difference needs more decimal places than the amount "
                            "scale allows; no COMP-3 sign flip can explain it")
        route["minimum_sets"] = []
        route["necessary_records"] = []
        route["partial_records"] = []
        return route
    if not candidates:
        route["search"].update(status="exhaustive", nodes_visited=0)
        route["conclusion"] = "no_feasible_set"
        route["message"] = "no data row carries a reversible C/D sign nibble"
        route["minimum_sets"] = []
        route["necessary_records"] = []
        route["partial_records"] = []
        return route

    subsets, status, nodes = _all_minimal_subsets(candidates, required, node_budget)
    # Baseline failures exist independently of the trailer mismatch; a candidate
    # set is only rejected when flipping introduces *new* failures or leaves the
    # FL002 mismatch in place. Pre-existing rejected records stay rejected.
    baseline_issue_codes = {issue.code for issue in replay["issues"]} - {"FL002"}
    baseline_record_failures = {
        record.record_no: (sorted(item.code for item in record.errors),
                           sorted(hit.code for hit in record.hits))
        for record in replay["records"]
    }
    # Each arithmetically valid subset must still satisfy the full rule engine
    # on the whole file (unique keys, cross-field rules, FL002 disappearing).
    verified_sets: list[dict[str, Any]] = []
    rejected_sets = 0
    for subset in subsets:
        chosen = [candidates[index] for index in subset]
        flips = {item.record_no: item.candidate_raw for item in chosen}
        full = _replay_with_flips(contract, input_data, flips)
        still_fl002 = any(issue.code == "FL002" for issue in full["issues"])
        new_failures: list[str] = []
        for record in full["records"]:
            current = (sorted(item.code for item in record.errors),
                       sorted(hit.code for hit in record.hits))
            if current != baseline_record_failures.get(record.record_no, ([], [])):
                new_failures.append(record.record_no)
        new_file_issues = {issue.code for issue in full["issues"]} - baseline_issue_codes - {"FL002"}
        if still_fl002 or new_failures or new_file_issues:
            rejected_sets += 1
            continue
        record_nos = sorted(item.record_no for item in chosen)
        verified_sets.append({
            "size": len(chosen),
            "record_nos": record_nos,
            "flipped_records": [
                {
                    "record_no": item.record_no,
                    "sign_byte_absolute_offset": item.sign_byte_absolute,
                    "original_sign_nibble": item.sign_nibble,
                    "candidate_sign_nibble": item.candidate_sign_nibble,
                    "original_amount": decimal_text(item.original_amount),
                    "candidate_amount": decimal_text(item.candidate_amount),
                    "original_record_sha256": item.original_sha256,
                    "candidate_record_sha256": item.candidate_sha256,
                }
                for item in sorted(chosen, key=lambda candidate: candidate.record_no)
            ],
            "recomputed_total": decimal_text(full["computed_total"]),
            "matches_trailer": full["computed_total"] == declared and not full["missing_amount"],
            "remaining_issue_codes": sorted(issue.code for issue in full["issues"]),
        })
    verified_sets.sort(key=lambda item: (item["size"], item["record_nos"]))
    membership: dict[int, int] = {}
    for item in verified_sets:
        for record_no in item["record_nos"]:
            membership[record_no] = membership.get(record_no, 0) + 1
    set_count = len(verified_sets)
    necessary = sorted(record_no for record_no, count in membership.items()
                       if count == set_count and set_count > 0)
    partial = sorted(record_no for record_no, count in membership.items()
                     if count != set_count)
    route["search"].update(status=status, nodes_visited=nodes,
                           arithmetically_valid_sets=len(subsets),
                           rule_verified_sets=set_count,
                           sets_rejected_on_replay=rejected_sets)
    route["minimum_sets"] = verified_sets
    route["necessary_records"] = necessary
    route["partial_records"] = partial
    route["minimality_proof"] = {
        "searched_cardinalities_in_order": True,
        "minimum_cardinality": verified_sets[0]["size"] if verified_sets else None,
        "all_smaller_cardinalities_exhausted": True,
        "all_minimum_sets_reported": status == "exhaustive",
    }
    if not verified_sets:
        if status == "budget_exhausted":
            route["conclusion"] = "budget_exhausted"
            route["message"] = ("the search budget was exhausted before any rule-valid minimum "
                                "set could be confirmed")
        else:
            route["conclusion"] = "no_feasible_set"
            route["message"] = ("no combination of reversible sign-nibble flips both satisfies "
                                "the rules and makes the data total equal the trailer")
    elif status == "budget_exhausted":
        route["conclusion"] = "budget_exhausted_with_candidates"
        route["message"] = ("candidate minimum sets were confirmed, but the search budget was "
                            "exhausted, so further equally-small or smaller sets may exist")
    else:
        route["conclusion"] = "unique_minimum_set" if set_count == 1 else "multiple_minimum_sets"
        route["message"] = ("all minimum-cardinality sign-flip sets are reported; no unexplained "
                            "difference is attributed to any other record")
    return route


def _comp3_encodable(value: Decimal, digits: int, scale: int) -> dict[str, Any]:
    scaled = _scaled_int(value, scale)
    if scaled is None:
        return {"encodable": False, "reason": "value needs more decimal places than the scale allows"}
    limit = 10 ** digits - 1
    if abs(scaled) > limit:
        return {"encodable": False,
                "reason": f"scaled magnitude {abs(scaled)} exceeds {digits}-digit capacity {limit}"}
    nibbles = digits + 1
    padded = nibbles if nibbles % 2 else nibbles + 1
    return {
        "encodable": True,
        "scaled_integer": scaled,
        "packed_byte_length": padded // 2,
        "positive_sign_nibble": "C",
        "negative_sign_nibble": "D",
    }


def _display_encodable(value: Decimal, digits: int, scale: int, field: Any) -> dict[str, Any]:
    scaled = _scaled_int(value, scale)
    if scaled is None:
        return {"encodable": False, "reason": "value needs more decimal places than the scale allows"}
    capacity = 10 ** digits - 1
    if abs(scaled) > capacity:
        return {"encodable": False,
                "reason": f"scaled magnitude {abs(scaled)} exceeds {digits}-digit capacity {capacity}"}
    return {"encodable": True, "scaled_integer": scaled,
            "signed": bool(field.signed), "separate_sign": bool(field.separate)}


def _data_trusted_route(contract: _Contract, replay: dict[str, Any],
                        declared: Decimal, computed: Decimal) -> dict[str, Any]:
    field = contract.layout.find_field(contract.amount_field)
    if contract.amount_usage == "COMP-3":
        encoding = _comp3_encodable(computed, contract.amount_digits, contract.amount_scale)
    else:
        encoding = _display_encodable(computed, contract.amount_digits,
                                      contract.amount_scale, field)
    return {
        "route": DATA_TRUSTED,
        "authority": "operations confirms the data rows are immutable original transactions",
        "declared_trailer_amount": decimal_text(declared),
        "correct_trailer_amount": decimal_text(computed),
        "amount_field": contract.amount_field,
        "pic_usage": contract.amount_usage,
        "pic_digits": contract.amount_digits,
        "pic_scale": contract.amount_scale,
        "encodable_at_current_width": encoding,
        "data_row_sign_flips_suggested": [],
        "message": ("the trailer record should be corrected to the exact computed total; no data "
                    "row sign nibble is implicated"),
    }


def diagnose_run(run: dict[str, Any], *, routes: tuple[str, ...] = ROUTES,
                 node_budget: int = DEFAULT_NODE_BUDGET) -> dict[str, Any]:
    """Diagnose an existing, fully materialized run without writing anything.

    ``run`` is a ledger row (see :meth:`Ledger.get_run`).  The function only
    opens files for reading.  Output is deterministic for identical inputs.
    """
    if node_budget <= 0:
        raise ValueError("node_budget must be positive")
    unknown = set(routes) - set(ROUTES)
    if unknown or not routes:
        raise ValueError(f"unknown routes: {sorted(unknown)!r}")

    contract_or_payload = _load_contract(run)
    if isinstance(contract_or_payload, dict):
        return contract_or_payload
    contract: _Contract = contract_or_payload

    try:
        input_data = Path(run["input_path"]).read_bytes()
    except OSError as exc:
        return _insufficient("INPUT_UNREADABLE", f"input file cannot be read: {exc}")

    evidence_failure, verified_evidence = _verify_evidence(run, input_data, contract)
    if evidence_failure is not None:
        return evidence_failure

    replay = _replay(contract, input_data)
    fl002 = next((issue for issue in replay["issues"] if issue.code == "FL002"), None)
    replay_block = {
        "record_count": len(replay["records"]),
        "data_record_count": len(replay["data_records"]),
        "trailer_record_no": None if replay["trailer"] is None else replay["trailer"].record_no,
        "issue_codes": sorted(issue.code for issue in replay["issues"]),
        "read_issue_codes": replay["read_issue_codes"],
        "data_record_outcomes": [
            {
                "record_no": record.record_no,
                "byte_offset": record.byte_offset,
                "accepted": record.accepted,
                "decode_error_codes": sorted(item.code for item in record.errors),
                "rule_hit_codes": sorted(hit.code for hit in record.hits),
                "amount": None if _amount_value(contract, record) is None
                else decimal_text(_amount_value(contract, record)),
                "raw_sha256": sha256_bytes(record.raw),
            }
            for record in replay["data_records"]
        ],
    }

    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "run_key": run["run_key"],
        "rule_summary": _rule_summary(contract.rules),
        "contract_digests": {
            "input_sha256": run["input_sha256"],
            "copybook_sha256": run["copybook_sha256"],
            "rules_sha256": run["rules_sha256"],
        },
        "evidence": verified_evidence,
        "replay": replay_block,
    }

    if replay["trailer"] is None:
        return _insufficient("TRAILER_MISSING",
                             "no trailer record can be replayed, so a declared trailer amount "
                             "cannot be compared",
                             replay=replay_block)
    trailer = replay["trailer"]
    declared = _amount_value(contract, trailer)
    if declared is None:
        return _insufficient("TRAILER_AMOUNT_UNDECODABLE",
                             "the trailer amount cannot be decoded as a Decimal",
                             replay=replay_block)
    if any(_amount_value(contract, record) is None for record in replay["data_records"]):
        return _insufficient("DATA_AMOUNT_UNDECODABLE",
                             "at least one data row amount cannot be decoded; the difference "
                             "cannot be attributed to sign nibbles",
                             replay=replay_block)

    fl001 = next((issue for issue in replay["issues"] if issue.code == "FL001"), None)
    if fl001 is not None and contract.rules.trailer is not None \
            and contract.rules.trailer.count_field:
        return _insufficient(
            "TRAILER_COUNT_MISMATCH",
            "the trailer record count does not match the number of data records, so the "
            "declared total cannot be tied to the replayed rows without an unexplained premise",
            replay=replay_block)

    if fl002 is None:
        payload["conclusion"] = "no_fl002_observed"
        payload["message"] = ("the replayed run does not produce an FL002 trailer-amount mismatch; "
                              "there is nothing to diagnose")
        return payload

    computed = sum((_amount_value(contract, record) for record in replay["data_records"]),
                   Decimal(0))
    if declared == computed:  # defensive: FL002 would not normally be raised
        payload["conclusion"] = "no_fl002_observed"
        payload["message"] = "the replayed trailer total already equals the data total"
        return payload

    payload["trailer_amounts"] = {
        "declared": decimal_text(declared),
        "computed": decimal_text(computed),
        "difference": decimal_text(declared - computed),
        "trailer_record_no": trailer.record_no,
        "trailer_byte_offset": trailer.byte_offset,
    }
    if TRAILER_TRUSTED in routes:
        payload[TRAILER_TRUSTED] = _trailer_trusted_route(
            contract, replay, input_data, declared, computed, node_budget)
    if DATA_TRUSTED in routes:
        payload[DATA_TRUSTED] = _data_trusted_route(
            contract, replay, declared, computed)

    # Overall conclusion mirrors the chosen route(s); both stay conditional.
    if len(routes) == 1 and routes[0] == TRAILER_TRUSTED:
        payload["conclusion"] = payload[TRAILER_TRUSTED]["conclusion"]
    elif len(routes) == 1 and routes[0] == DATA_TRUSTED:
        payload["conclusion"] = "data_trusted_correction"
    else:
        payload["conclusion"] = "conditional_routes_requiring_authority_decision"
        payload["message"] = (
            "two honest routes are available and neither is a confirmed fact: choose the authority "
            "source -- an independently signed trailer (trailer_trusted) or immutable original data "
            "rows (data_trusted)")
    return payload
