"""Record/file rule orchestration and configurable unique-key conflict resolution."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from typing import Any

from ..model import FileIssue, Record, RuleHit
from ..util import decimal_text
from .checks import (ValidationState, canonical_unique_keys, cross_hit, first_value,
                     scalar_text, unique_checks, validate_field, values_for)
from .schema import RuleSet

SEQUENTIAL_POLICY = "sequential"
PREFER_COMBINATION_POLICY = "prefer_combination"
UNIQUE_POLICIES = (SEQUENTIAL_POLICY, PREFER_COMBINATION_POLICY)
DEFAULT_SCORE_FIELD = "SCORE"
# Exhaustive clause-space search is exponential.  These budgets are deliberately
# small and configurable so an exhausted proof can be reported as unresolved.
DEFAULT_MAX_COMPONENT_RECORDS = 18
DEFAULT_PROOF_BUDGET = 2_000_000

def assign_record_type(record: Record, rules: RuleSet) -> None:
    if rules.record_type is None:
        record.record_type = None
        return
    value = first_value(record, rules.record_type.field)
    record.record_type = scalar_text(value) if value is not None else None

class RuleEngine:
    def __init__(self, rules: RuleSet) -> None:
        self.rules = rules

    def _declared_types(self) -> set[str]:
        if self.rules.record_type is None:
            return set()
        return {value for value in (self.rules.record_type.data, self.rules.record_type.header,
                                    self.rules.record_type.trailer) if value is not None}

    def validate_record(self, record: Record, state: ValidationState) -> list[RuleHit]:
        if self.rules.record_type is not None:
            if record.record_type not in self._declared_types():
                return [RuleHit(record.record_no, "R010_RECORD_TYPE",
                                f"undeclared record type {record.record_type!r}", "record")]
            if record.record_type != self.rules.record_type.data:
                return []
        hits: list[RuleHit] = []
        for rule in self.rules.fields.values():
            for path, value in values_for(record, rule.name):
                hits.extend(validate_field(record, path, value, rule))
        for rule in self.rules.record_rules:
            if rule.kind == "cross_field":
                hit = cross_hit(record, rule)
                if hit is not None:
                    hits.append(hit)
        return hits or unique_checks(record, self.rules, state)

    def validate_data_record(self, record: Record) -> list[RuleHit]:
        """Field, cross-field and record-type checks; never touches unique state.

        This is the gate for preference selection: only records that clear every
        non-unique rule may enter the conflict graph.
        """

        if self.rules.record_type is not None:
            if record.record_type not in self._declared_types():
                return [RuleHit(record.record_no, "R010_RECORD_TYPE",
                                f"undeclared record type {record.record_type!r}", "record")]
            if record.record_type != self.rules.record_type.data:
                return []
        hits: list[RuleHit] = []
        for rule in self.rules.fields.values():
            for path, value in values_for(record, rule.name):
                hits.extend(validate_field(record, path, value, rule))
        for rule in self.rules.record_rules:
            if rule.kind == "cross_field":
                hit = cross_hit(record, rule)
                if hit is not None:
                    hits.append(hit)
        return hits

    def _check_positions(self, records: list[Record]) -> list[FileIssue]:
        issues: list[FileIssue] = []
        if self.rules.record_type is None:
            return issues
        marker = self.rules.record_type
        if marker.header:
            if not records or records[0].record_type != marker.header:
                issues.append(FileIssue("FL005", f"first record is not the declared header {marker.header!r}",
                                        byte_offset=records[0].byte_offset if records else 0))
            for record in records[1:]:
                if record.record_type == marker.header:
                    issues.append(FileIssue("FL005", "header record appears after the first record",
                                            byte_offset=record.byte_offset, record_no=record.record_no))
        if marker.trailer:
            if not records or records[-1].record_type != marker.trailer:
                issues.append(FileIssue("FL001", f"last record is not the declared trailer {marker.trailer!r}",
                                        byte_offset=records[-1].byte_offset if records else 0))
            for record in records[:-1]:
                if record.record_type == marker.trailer:
                    issues.append(FileIssue("FL005", "trailer record appears before the last record",
                                            byte_offset=record.byte_offset, record_no=record.record_no))
        return issues

    def _check_trailer(self, records: list[Record], data_records: list[Record],
                       basis: str = "file", selection: str | None = None) -> list[FileIssue]:
        if self.rules.trailer is None or self.rules.record_type is None or self.rules.record_type.trailer is None:
            return []
        trailers = [record for record in records if record.record_type == self.rules.record_type.trailer]
        trailer = trailers[-1] if trailers else None
        if trailer is None:
            return []
        issues: list[FileIssue] = []
        marker = {"basis": basis} if selection is None else {"basis": basis, "selection": selection}
        if self.rules.trailer.count_field:
            try:
                declared_count = int(scalar_text(first_value(trailer, self.rules.trailer.count_field)))
            except (TypeError, ValueError):
                declared_count = None
            if declared_count != len(data_records):
                issues.append(FileIssue("FL001", f"trailer count {declared_count!r} != {len(data_records)} "
                                        f"data records", byte_offset=trailer.byte_offset,
                                        record_no=trailer.record_no, detail=dict(marker)))
        if self.rules.trailer.amount_field:
            total, missing = Decimal(0), False
            for record in data_records:
                value = first_value(record, self.rules.trailer.amount_field)
                if isinstance(value, Decimal):
                    total += value
                else:
                    missing = True
            declared = first_value(trailer, self.rules.trailer.amount_field)
            if missing or not isinstance(declared, Decimal) or total != declared:
                detail = {"computed": decimal_text(total), "missing_amount_values": missing, **marker}
                issues.append(FileIssue(
                    "FL002", f"trailer amount {scalar_text(declared)!r} != computed {decimal_text(total)}",
                    byte_offset=trailer.byte_offset, record_no=trailer.record_no, detail=detail))
        return issues

    def validate_file(self, records: list[Record], selection: dict[str, Any] | None = None) -> list[FileIssue]:
        rules = self.rules
        data_records = [record for record in records
                        if rules.record_type is None or record.record_type == rules.record_type.data]
        issues = self._check_positions(records)
        if selection is not None and not selection.get("final", True):
            # The accepted set is not final; reusing the pre-selection counts or
            # trailer conclusion would misstate the outcome.  Say so explicitly.
            issues.append(FileIssue(
                "FL006", "unique selection is undetermined for "
                f"{selection.get('unresolved_component_count', 0)} component(s); "
                "file-level limits and trailer totals are not evaluated on a final set",
                severity="warning", detail={"basis": "unknown", "reason": "selection_undetermined",
                                            "unresolved_components": [
                                                item.get("component") for item in selection.get("components", [])
                                                if item.get("status") != "proven"]}))
            return issues
        # Limits and trailer accounting describe the physical data records of the
        # file; preference selection changes which rows are accepted, not what the
        # trailer declares, so the basis stays the file and never silently adopts
        # the accepted subset.
        basis = "file_data_records"
        note: dict[str, Any] = {"basis": basis}
        if selection is not None:
            note["selection"] = "proven"
        if rules.min_records is not None and len(data_records) < rules.min_records:
            issues.append(FileIssue("FL003", f"{len(data_records)} data records is below min_records "
                                    f"{rules.min_records}", detail=dict(note)))
        if rules.max_records is not None and len(data_records) > rules.max_records:
            issues.append(FileIssue("FL004", f"{len(data_records)} data records is above max_records "
                                    f"{rules.max_records}", detail=dict(note)))
        issues.extend(self._check_trailer(records, data_records, basis,
                                          None if selection is None else "proven"))
        return issues
# ---------------------------------------------------------------------------
# Configurable unique-key conflict adjudication.
#
# Sequential mode preserves the historical "first accepted record wins" rule.
# Preference mode treats every unique_key as an edge generator: two records that
# share *any* complete canonical key can never both be accepted.  Each connected
# component is then an exact maximum-weight independent-set problem where the
# weight is the decoded SCORE.  Only an exhaustive proof may declare winners.
# ---------------------------------------------------------------------------

class _ProofBudgetExceeded(Exception):
    def __init__(self, nodes: int) -> None:
        super().__init__(nodes)
        self.nodes = nodes


@dataclass
class _Candidate:
    record: Record
    keys: list[tuple[str, tuple[str, ...]]]
    score: Decimal | None
    score_text: str | None
    score_reason: str | None


@dataclass
class UniqueSelection:
    report: dict[str, Any]
    hits: dict[int, RuleHit] = dc_field(default_factory=dict)
    selected: set[int] = dc_field(default_factory=set)

    @property
    def final(self) -> bool:
        return bool(self.report.get("final"))


def _parse_score(record: Record, field: str) -> tuple[Decimal | None, str | None, str | None]:
    """Read the decoded score without inventing a value.

    ``account.cpy`` decodes ``SCORE PIC 9(3)`` to ``Decimal``; hand-built records
    may use ``int``.  ``None`` is a missing score and any other type (text,
    booleans, NaN/Inf) is invalid.  Unknown is never coerced to zero.
    """

    found = values_for(record, field)
    if not found or found[0][1] is None:
        return None, None, "score_missing"
    value = found[0][1]
    if isinstance(value, bool):
        return None, scalar_text(value), "score_invalid"
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, Decimal):
        number = value
    else:
        return None, scalar_text(value), "score_invalid"
    if not number.is_finite():
        return None, scalar_text(value), "score_invalid"
    return number, decimal_text(number), None


def _conflict_key_payload(keys: list[tuple[str, tuple[str, ...]]] | None) -> list[dict[str, Any]]:
    return [{"rule": rule_name, "key": list(key)} for rule_name, key in (keys or [])]


def _pair_edges(candidates: list[_Candidate]) -> tuple[
        dict[int, set[int]], dict[tuple[int, int], list[tuple[str, tuple[str, ...]]]]]:
    """Build the conflict graph: same complete key on any unique_key is an edge."""

    owners: dict[tuple[str, tuple[str, ...]], list[int]] = {}
    for candidate in candidates:
        for rule_name, key in candidate.keys:
            owners.setdefault((rule_name, key), []).append(candidate.record.record_no)
    neighbors: dict[int, set[int]] = {item.record.record_no: set() for item in candidates}
    shared: dict[tuple[int, int], list[tuple[str, tuple[str, ...]]]] = {}
    for (rule_name, key), record_nos in owners.items():
        ordered = sorted(record_nos)
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1:]:
                neighbors[left].add(right)
                neighbors[right].add(left)
                shared.setdefault((left, right), []).append((rule_name, key))
    return neighbors, shared


def _components(nodes: list[int], neighbors: dict[int, set[int]]) -> list[list[int]]:
    """Connected components induced by ``nodes`` only.

    Unscored records are rejected before this point and must not bridge scored
    records into a shared component: their edges disappear with them.
    """

    allowed = set(nodes)
    seen: set[int] = set()
    components: list[list[int]] = []
    for start in sorted(nodes):
        if start in seen:
            continue
        stack, group = [start], []
        seen.add(start)
        while stack:
            current = stack.pop()
            group.append(current)
            for other in sorted(neighbors.get(current, ())):
                if other in allowed and other not in seen:
                    seen.add(other)
                    stack.append(other)
        components.append(sorted(group))
    return components


def _better(total: Decimal, chosen: tuple[int, ...], best_total: Decimal,
            best_chosen: tuple[int, ...]) -> bool:
    """Larger total wins; a tie is broken by the lowest record-number sequence."""

    return total > best_total or (total == best_total and chosen < best_chosen)


@dataclass
class _SearchProof:
    status: str
    selected: list[int]
    total: Decimal | None
    nodes: int
    pruned: int
    root_upper_bound: Decimal
    reason: str | None = None


def _search_component(record_nos: list[int], score_by_record: dict[int, Decimal],
                      neighbors: dict[int, set[int]], proof_budget: int) -> _SearchProof:
    """Exact branch-and-bound over all non-empty independent sets.

    A component always keeps at least one record: the business contract is to
    publish a usable winner set, not to discard a whole conflict component.  The
    initial incumbent is the best single record, which is always compatible.
    """

    ordered = sorted(record_nos)
    index_of = {record_no: index for index, record_no in enumerate(ordered)}
    weights = [score_by_record[record_no] for record_no in ordered]
    conflict_masks = [0] * len(ordered)
    for record_no in ordered:
        for other in neighbors.get(record_no, ()):
            if other in index_of:
                conflict_masks[index_of[record_no]] |= 1 << index_of[other]
    root_bound = sum((weight for weight in weights if weight > 0), Decimal(0))
    initial = max(range(len(ordered)), key=lambda index: (weights[index], -ordered[index]))
    best_total = weights[initial]
    best_chosen: tuple[int, ...] = (ordered[initial],)
    nodes = pruned = 0

    def choose(candidates: list[int]) -> int:
        return max(candidates, key=lambda index: (weights[index], -ordered[index]))

    def search(candidates: list[int], chosen: list[int], total: Decimal) -> None:
        nonlocal nodes, pruned, best_total, best_chosen
        nodes += 1
        if nodes > proof_budget:
            raise _ProofBudgetExceeded(nodes)
        if not candidates:
            candidate_choice = tuple(sorted(ordered[index] for index in chosen))
            if chosen and _better(total, candidate_choice, best_total, best_chosen):
                best_total, best_chosen = total, candidate_choice
            return
        # Positive-only bound: including a non-positive score can never improve a
        # maximum, and this keeps the bound admissible for negative weights.
        bound = total + sum((weights[index] for index in candidates
                             if weights[index] > 0), Decimal(0))
        if bound < best_total:
            pruned += 1
            return
        vertex = choose(candidates)
        remaining = [index for index in candidates if index != vertex]
        # Branch 1: accept this record; all of its conflict neighbours drop out.
        free = [index for index in remaining if not (conflict_masks[vertex] >> index) & 1]
        search(free, chosen + [vertex], total + weights[vertex])
        # Branch 2: reject this record and try the rest.
        search(remaining, chosen, total)

    try:
        search(list(range(len(ordered))), [], Decimal(0))
    except _ProofBudgetExceeded as exc:
        return _SearchProof("undetermined", [], None, exc.nodes, pruned, root_bound,
                            "proof_budget_exhausted")
    return _SearchProof("proven", list(best_chosen), best_total, nodes, pruned, root_bound)


def _hit(record_no: int, detail: dict[str, Any], message: str) -> RuleHit:
    return RuleHit(record_no, "R011_UNIQUE_SELECTION", message, "record", None, detail)


def resolve_unique_selection(records: list[Record], rules: RuleSet, *,
                             score_field: str = DEFAULT_SCORE_FIELD,
                             max_component_records: int = DEFAULT_MAX_COMPONENT_RECORDS,
                             proof_budget: int = DEFAULT_PROOF_BUDGET) -> UniqueSelection:
    """Adjudicate unique-key conflicts by maximum total SCORE per component.

    ``records`` must already have passed decoding plus field/cross-field rules.
    Records with a missing unique-key component keep ``R008_UNIQUE`` and never
    enter the graph.  Contended records whose SCORE is missing/invalid are
    rejected with ``R011_UNIQUE_SELECTION`` rather than scored as zero.
    """

    candidates: list[_Candidate] = []
    hits: dict[int, RuleHit] = {}
    for record in sorted(records, key=lambda item: item.record_no):
        keys, problems = canonical_unique_keys(record, rules)
        if problems:
            hits[record.record_no] = problems[0]
            continue
        score, score_text, reason = _parse_score(record, score_field)
        candidates.append(_Candidate(record, keys, score, score_text, reason))

    neighbors, shared = _pair_edges(candidates)
    contended = {record_no for record_no, peers in neighbors.items() if peers}
    by_record = {item.record.record_no: item for item in candidates}
    score_rejections: list[dict[str, Any]] = []
    # An unscoreable record cannot be ranked at all in this contract.  Reject it
    # on its own merits instead of treating an unknown score as zero; only the
    # rows that passed field/cross-field checks appear in ``candidates``.
    for record_no in sorted(neighbors):
        candidate = by_record[record_no]
        if candidate.score is not None:
            continue
        conflicts = sorted(neighbors[record_no])
        detail = {
            "reason": candidate.score_reason,
            "conflict_keys": _conflict_key_payload(candidate.keys),
            "conflicts_with": [
                {"record_no": other, "selected": False,
                 "keys": _conflict_key_payload(shared.get((min(record_no, other),
                                                          max(record_no, other))))}
                for other in conflicts
            ],
            "selected_record_nos": [],
            "selected_total_score": None,
            "optimality": {"status": "undetermined", "reason": candidate.score_reason,
                           "complete": False},
        }
        hits[record_no] = _hit(
            record_no, detail,
            f"record has {candidate.score_reason.replace('_', ' ')} and cannot compete "
            f"on {score_field}")
        score_rejections.append({"record_no": record_no, "reason": candidate.score_reason,
                                 "score": candidate.score_text,
                                 "conflict_keys": detail["conflict_keys"]})

    # Removing unscored records can shrink or split the remaining graph.  Every
    # scored record keeps a component, even a singleton, so the report accounts
    # for all records the selection could have accepted.
    remaining = sorted(record_no for record_no in neighbors
                       if by_record[record_no].score is not None)
    components = _components(remaining, neighbors)
    component_reports: list[dict[str, Any]] = []
    selected_all: set[int] = set()
    proven_total = Decimal(0)
    unresolved = 0
    for component_index, component in enumerate(components, start=1):
        if len(component) > max_component_records:
            positive_bound = sum((by_record[no].score for no in component
                                  if by_record[no].score and by_record[no].score > 0),
                                 Decimal(0))
            proof = _SearchProof("undetermined", [], None, 0, 0, positive_bound,
                                 "component_too_large")
        else:
            proof = _search_component(
                component, {no: by_record[no].score for no in component}, neighbors, proof_budget)
        evidence: dict[str, Any] = {
            "method": "exhaustive_branch_and_bound",
            "objective": "max_total_score_over_nonempty_compatible_sets",
            "status": proof.status,
            "complete": proof.status == "proven",
            "search_nodes": proof.nodes,
            "bound_prunes": proof.pruned,
            "root_upper_bound": decimal_text(proof.root_upper_bound),
            "proof_budget": proof_budget,
            "max_component_records": max_component_records,
            "tie_break": "lexicographically_lowest_record_number_sequence",
        }
        if proof.status != "proven":
            evidence["reason"] = proof.reason
        chosen = set(proof.selected)
        edges = []
        for (left, right), keys in sorted(shared.items()):
            if left in component and right in component:
                edges.append({"a": left, "b": right, "keys": _conflict_key_payload(keys)})
        if proof.status == "proven":
            selected_all |= chosen
            proven_total += proof.total or Decimal(0)
        else:
            unresolved += 1
        rejected_rows: list[dict[str, Any]] = []
        for record_no in component:
            candidate = by_record[record_no]
            if record_no in chosen:
                continue
            conflicts_with = []
            for other in sorted(neighbors.get(record_no, ())):
                if other not in component:
                    continue
                if proof.status == "proven" and other not in chosen:
                    continue
                pair = (min(record_no, other), max(record_no, other))
                conflicts_with.append({"record_no": other, "selected": other in chosen,
                                       "keys": _conflict_key_payload(shared.get(pair))})
            row = {
                "record_no": record_no,
                "score": candidate.score_text,
                "conflict_keys": _conflict_key_payload(candidate.keys),
                "conflicts_with": conflicts_with,
                "selected_record_nos": sorted(chosen),
                "selected_total_score": (decimal_text(proof.total)
                                         if proof.status == "proven" else None),
                "reason": ("not_in_optimal_compatible_set" if proof.status == "proven"
                           else "selection_undetermined"),
                "optimality": evidence,
            }
            rejected_rows.append(row)
            if proof.status == "proven":
                message = (f"record conflicts with the optimal compatible set for component "
                           f"{component_index}")
            else:
                message = (f"unique selection for component {component_index} is undetermined "
                           f"({proof.reason}); no winner is published")
            hits[record_no] = _hit(record_no, row, message)
        component_reports.append({
            "component": component_index,
            "record_nos": component,
            "status": proof.status,
            "selected": sorted(chosen),
            "selected_total_score": decimal_text(proof.total) if proof.status == "proven" else None,
            "rejected": rejected_rows,
            "conflict_edges": edges,
            "evidence": evidence,
        })

    singletons = sorted(set(neighbors) - contended)
    final = unresolved == 0
    report = {
        "policy": PREFER_COMBINATION_POLICY,
        "score_field": score_field,
        "final": final,
        "status": "proven" if final else "undetermined",
        "component_count": len(component_reports),
        "unresolved_component_count": unresolved,
        "selected_record_nos": sorted(selected_all),
        "selected_total_score": decimal_text(proven_total),
        "score_rejections": score_rejections,
        "singleton_record_nos": singletons,
        "components": component_reports,
    }
    return UniqueSelection(report, hits, selected_all)
