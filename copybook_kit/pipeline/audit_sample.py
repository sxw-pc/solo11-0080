"""Offline human-review sampling plans over a completed run's reject ledger.

A sampling plan selects *real* reject rows (``reject_no`` / ``record_no`` /
``byte_offset`` / ``raw_sha256`` / ``raw_file``) so that a bounded manual review
covers a requested number of independent records per rule code. A single reject
carrying several codes may satisfy several quotas at once, but one record can
never be counted twice for the same code.

Two contracts are offered:

* ``exact`` — exhaustive branch-and-bound search, only accepted for small
  declared scales. The returned set is proven minimum, or a checkable
  infeasibility gap certificate is produced.
* ``bounded`` — the same branch-and-bound search run under a fixed node budget.
  It returns any feasible set without claiming minimality, and distinguishes
  "not decided within budget" from a proven "infeasible".

The planner is strictly read-only: it never rewrites reject bytes, run
artifacts or ledger rows, and it never reconstructs evidence from the
``partial`` decode values. File-level (``FL*``) issues have no reject row and
are therefore not sampleable codes.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import CopybookKitError
from ..util import sha256_bytes

EXACT_CONTRACT = "exact"
BOUNDED_CONTRACT = "bounded"
CONTRACTS = (EXACT_CONTRACT, BOUNDED_CONTRACT)

DEFAULT_EXACT_NODE_BUDGET = 2_000_000
DEFAULT_BOUNDED_NODE_BUDGET = 100_000
EXACT_MAX_CANDIDATES = 24
EXACT_MAX_BUDGET = 24
MAX_PLAN_BUDGET = 1000

OPTIMAL_STATUS = "optimal"
FEASIBLE_STATUS = "feasible"
INFEASIBLE_STATUS = "infeasible"
UNDECIDED_STATUS = "undecided"

sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))


class AuditPlanError(CopybookKitError):
    """Invalid plan request (AU002); AU001 is raised for evidence tampering."""

    code = "AU002"


@dataclass(frozen=True)
class Candidate:
    """One verified reject row, reduced to the evidence the auditor may see."""

    reject_no: int
    record_no: int
    byte_offset: int
    raw_sha256: str
    raw_file: str
    codes: frozenset[str]
    groups: frozenset[str] = frozenset()

    @property
    def sort_key(self) -> tuple[int, str]:
        # Stable, SQLite-order independent tie-breaking.
        return (self.record_no, self.raw_sha256)


@dataclass(frozen=True)
class AuditRequest:
    run_id: str
    quotas: dict[str, int]
    budget: int
    exclusion_groups: tuple[frozenset[int], ...]
    contract: str
    node_budget: int


# --------------------------------------------------------------------------- #
# Request parsing
# --------------------------------------------------------------------------- #

def parse_request(payload: dict[str, Any]) -> AuditRequest:
    if not isinstance(payload, dict):
        raise AuditPlanError("plan request must be a JSON object")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise AuditPlanError("plan request requires a non-empty run_id")
    quotas_raw = payload.get("quotas", {})
    if not isinstance(quotas_raw, dict) or not quotas_raw:
        raise AuditPlanError("plan request requires a non-empty quotas mapping")
    quotas: dict[str, int] = {}
    for code, needed in quotas_raw.items():
        if not isinstance(code, str) or not code:
            raise AuditPlanError("quota codes must be non-empty strings")
        if isinstance(needed, bool) or not isinstance(needed, int) or needed < 0:
            raise AuditPlanError(f"quota for {code} must be a non-negative integer")
        if needed:
            quotas[code] = needed
    if not quotas:
        raise AuditPlanError("plan request requires at least one positive quota")
    budget = payload.get("budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= MAX_PLAN_BUDGET:
        raise AuditPlanError(f"budget must be an integer between 1 and {MAX_PLAN_BUDGET}")
    groups_raw = payload.get("exclusion_groups", [])
    if not isinstance(groups_raw, list):
        raise AuditPlanError("exclusion_groups must be a list of record_no lists")
    groups: list[frozenset[int]] = []
    for group in groups_raw:
        if not isinstance(group, list) or not group or any(
            isinstance(item, bool) or not isinstance(item, int) for item in group
        ):
            raise AuditPlanError("each exclusion group must be a list of integer record_no values")
        if len(set(group)) != len(group):
            raise AuditPlanError("an exclusion group contains a duplicated record_no")
        if len(group) >= 2:
            groups.append(frozenset(group))
    contract = payload.get("contract", EXACT_CONTRACT)
    if contract not in CONTRACTS:
        raise AuditPlanError(f"contract must be one of {sorted(CONTRACTS)}")
    node_budget_raw = payload.get("node_budget")
    default_nodes = (DEFAULT_EXACT_NODE_BUDGET if contract == EXACT_CONTRACT
                     else DEFAULT_BOUNDED_NODE_BUDGET)
    if node_budget_raw is None:
        node_budget = default_nodes
    elif isinstance(node_budget_raw, bool) or not isinstance(node_budget_raw, int) \
            or not 1 <= node_budget_raw <= 100_000_000:
        raise AuditPlanError("node_budget must be a positive integer (<= 100000000)")
    else:
        node_budget = node_budget_raw
    return AuditRequest(run_id, dict(sorted(quotas.items())), budget, tuple(groups),
                        contract, node_budget)


# --------------------------------------------------------------------------- #
# Evidence loading and raw-byte verification (read-only)
# --------------------------------------------------------------------------- #

def verify_evidence(run_dir: Path, rejects: list[dict[str, Any]]) -> None:
    """Hash every persisted raw evidence file and compare it with the ledger.

    Raises AU001 on the first mismatch, missing file, length mismatch or unsafe
    path. The ``partial`` values are deliberately never used as evidence.
    """

    root = run_dir.resolve()
    for row in rejects:
        raw_file = row["raw_file"]
        path = (root / raw_file).resolve()
        if root not in path.parents:
            raise AuditPlanError(f"raw_file escapes the run directory: {raw_file}", code="AU001")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AuditPlanError(f"raw evidence missing or unreadable for reject_no "
                                 f"{row['reject_no']} ({raw_file}): {exc}", code="AU001") from exc
        if sha256_bytes(data) != row["raw_sha256"]:
            raise AuditPlanError(
                f"raw bytes for reject_no {row['reject_no']} ({raw_file}) no longer match "
                "the raw_sha256 recorded in the ledger", code="AU001")
        if len(data) != int(row["raw_length"]):
            raise AuditPlanError(
                f"raw length for reject_no {row['reject_no']} ({raw_file}) disagrees with the "
                "ledger raw_length", code="AU001")


def load_evidence(ledger, run_id: str) -> tuple[dict[str, Any], Path, list[dict[str, Any]]]:
    run = ledger.get_run(run_id)
    if run is None:
        raise AuditPlanError(f"run {run_id} not found", code="LD002")
    if run["status"] not in {"completed", "completed_with_rejects"}:
        raise AuditPlanError(
            f"run {run_id} is not a completed run (status={run['status']}); audit plans only "
            "read finished reject ledgers")
    run_dir = Path(run["out_dir"])
    rejects = ledger.get_rejects(run_id)
    verify_evidence(run_dir, rejects)
    return run, run_dir, rejects


# --------------------------------------------------------------------------- #
# Candidate construction
# --------------------------------------------------------------------------- #

def build_candidates(rejects: list[dict[str, Any]],
                     groups: tuple[frozenset[int], ...]) -> tuple[list[Candidate], dict[str, Any]]:
    by_record = {int(row["record_no"]): row for row in rejects}
    if len(by_record) != len(rejects):
        raise AuditPlanError("reject ledger contains duplicated record_no values")
    unknown = sorted({record for group in groups for record in group} - set(by_record))
    if unknown:
        raise AuditPlanError(
            f"exclusion groups reference record_no values absent from the reject ledger: {unknown}")
    group_of: dict[int, list[str]] = {}
    for index, group in enumerate(groups):
        label = f"G{index + 1}"
        for record_no in group:
            group_of.setdefault(record_no, []).append(label)
    candidates: list[Candidate] = []
    for row in rejects:
        record_no = int(row["record_no"])
        candidates.append(Candidate(
            reject_no=int(row["reject_no"]), record_no=record_no,
            byte_offset=int(row["byte_offset"]), raw_sha256=row["raw_sha256"],
            raw_file=row["raw_file"], codes=frozenset(row["codes"]),
            groups=frozenset(group_of.get(record_no, ()))))
    # Explicit stable ordering: plans must not depend on SQLite return order.
    candidates.sort(key=lambda item: item.sort_key)
    group_labels = {f"G{index + 1}": sorted(group) for index, group in enumerate(groups)}
    return candidates, {"group_labels": group_labels}


# --------------------------------------------------------------------------- #
# Admissible lower bounds and greedy incumbent (branch-and-bound)
# --------------------------------------------------------------------------- #

def admissible_lower_bound(remaining: dict[str, int], candidates: list[Candidate]) -> int:
    """Sound lower bound on the extra records needed for ``remaining``.

    Returns a value no feasible completion can undercut. When a remaining code
    has no carrier among ``candidates`` the instance is uncoverable; the bound
    returns the outstanding demand so branch-and-bound prunes rather than
    chasing an impossible quota.
    """

    if not remaining:
        return 0
    total_need = sum(remaining.values())
    max_cover = max((len(candidate.codes & remaining.keys()) for candidate in candidates),
                    default=0)
    if max_cover == 0:
        # Nothing here can serve any remaining code: every slot still needs a
        # record chosen elsewhere, so demand the full outstanding amount.
        return total_need
    # One record covers at most one slot of any single code.
    per_code = max(remaining.values())
    packing = -(-total_need // max_cover)
    return max(per_code, packing)


def greedy_incumbent(request: AuditRequest, candidates: list[Candidate]) -> list[Candidate] | None:
    """Construct one feasible selection greedily (a valid upper bound).

    Group-aware via concrete group membership: selecting a candidate blocks
    every other member of any exclusion group it belongs to, and a selected
    candidate is never picked again. Failure here never proves infeasibility.
    """

    groups_by_record: dict[int, list[frozenset[int]]] = {}
    for group in request.exclusion_groups:
        for record_no in group:
            groups_by_record.setdefault(record_no, []).append(group)

    def conflicts(record_no: int, chosen_records: frozenset[int]) -> bool:
        return any(record_no in group and chosen_records & (group - {record_no})
                   for group in groups_by_record.get(record_no, ()))

    remaining = dict(request.quotas)
    chosen: list[Candidate] = []
    chosen_records: frozenset[int] = frozenset()
    while remaining:
        options = [candidate for candidate in candidates
                   if candidate.record_no not in chosen_records
                   and candidate.codes & remaining.keys()
                   and not conflicts(candidate.record_no, chosen_records)]
        if not options:
            return None

        def score(candidate: Candidate) -> tuple[int, int, str]:
            gained = sum(1 for code in candidate.codes if code in remaining)
            return (-gained, candidate.record_no, candidate.raw_sha256)

        pick = min(options, key=score)
        chosen.append(pick)
        chosen_records = chosen_records | {pick.record_no}
        if len(chosen) > request.budget:
            return None
        for code in pick.codes:
            if code in remaining:
                remaining[code] -= 1
                if remaining[code] <= 0:
                    del remaining[code]
    return chosen


# --------------------------------------------------------------------------- #
# Branch-and-bound solver
# --------------------------------------------------------------------------- #

@dataclass
class SearchResult:
    best: list[Candidate] | None
    partial: tuple[list[Candidate], dict[str, int]]
    nodes: int
    exhausted: bool


def _solve(request: AuditRequest, candidates: list[Candidate],
           incumbent: list[Candidate] | None) -> SearchResult:
    """Complete branch-and-bound over candidate subsets.

    At every node the smallest-index relevant candidate is either included
    exactly once or excluded forever, so each subset is generated once and no
    candidate can be selected twice. Exclusion-group conflicts and quotas are
    checked jointly inside the recursion rather than by greedy commitment.
    """

    node_limit = request.node_budget
    nodes = 0
    exhausted = False
    best: list[Candidate] | None = list(incumbent) if incumbent else None
    best_partial: tuple[list[Candidate], dict[str, int]] = ([], dict(request.quotas))
    stop_on_first = request.contract == BOUNDED_CONTRACT
    total = len(candidates)
    # Map each record to the concrete exclusion groups (as record sets) it
    # belongs to; blocking uses the group identity, never just label strings.
    groups_by_record: dict[int, list[frozenset[int]]] = {}
    for group in request.exclusion_groups:
        for record_no in group:
            groups_by_record.setdefault(record_no, []).append(group)

    def conflicts(record_no: int, chosen_records: frozenset[int]) -> bool:
        return any(record_no in group and chosen_records & (group - {record_no})
                   for group in groups_by_record.get(record_no, ()))

    def track_partial(chosen: list[Candidate], remaining: dict[str, int]) -> None:
        nonlocal best_partial
        satisfied = sum(request.quotas[code] - need for code, need in remaining.items())
        current = sum(request.quotas[code] - need for code, need in best_partial[1].items())
        if satisfied > current or (satisfied == current and len(chosen) < len(best_partial[0])):
            best_partial = (list(chosen), dict(remaining))

    def visit(index: int, chosen: list[Candidate], remaining: dict[str, int],
              chosen_records: frozenset[int]) -> None:
        nonlocal nodes, best, exhausted
        nodes += 1
        if nodes > node_limit:
            exhausted = True
            return
        if best is not None and len(chosen) >= len(best):
            return
        if not remaining:
            best = list(chosen)
            return
        if len(chosen) >= request.budget or index >= total:
            track_partial(chosen, remaining)
            return
        bound = len(chosen) + admissible_lower_bound(remaining, candidates[index:])
        if bound > request.budget:
            track_partial(chosen, remaining)
            return
        if best is not None and bound >= len(best):
            return
        candidate = candidates[index]
        relevant = bool(candidate.codes & remaining.keys())
        blocked = conflicts(candidate.record_no, chosen_records)
        # Include first (finds small solutions early), but only when useful and
        # permitted by the exclusion groups.
        if relevant and not blocked and len(chosen) < request.budget:
            next_remaining = {code: (need - 1 if code in candidate.codes else need)
                              for code, need in remaining.items()}
            next_remaining = {code: need for code, need in next_remaining.items() if need > 0}
            chosen.append(candidate)
            visit(index + 1, chosen, next_remaining,
                  chosen_records | {candidate.record_no})
            chosen.pop()
            if exhausted or (stop_on_first and best is not None):
                return
        # Exclude candidate forever. An irrelevant or blocked candidate is
        # always excluded, preserving completeness of the include branch.
        visit(index + 1, chosen, remaining, chosen_records)

    visit(0, [], dict(request.quotas), frozenset())
    return SearchResult(best, best_partial, min(nodes, node_limit + 1), exhausted)


# --------------------------------------------------------------------------- #
# Prechecks and checkable infeasibility gap certificates
# --------------------------------------------------------------------------- #

def _precheck(request: AuditRequest, candidates: list[Candidate]) -> list[dict[str, Any]]:
    """Return checkable infeasibility reasons; an empty list means not proven."""

    reasons: list[dict[str, Any]] = []
    availability: dict[str, int] = {}
    for candidate in candidates:
        for code in candidate.codes:
            availability[code] = availability.get(code, 0) + 1
    for code, needed in request.quotas.items():
        pool = availability.get(code, 0)
        if pool < needed:
            reasons.append({
                "type": "insufficient_candidates", "code": code, "required": needed,
                "available": pool,
                "check": f"only {pool} reject rows carry {code}, but the quota asks for {needed} "
                         "distinct records and one record counts at most once per code",
            })
    # Exclusion-group interactions are not reducible to a per-code count: one
    # in-group record may cover several codes, and outside records may replace
    # it. Group-driven infeasibility is therefore proven exhaustively by the
    # solver and reported through the no_satisfiable_selection certificate.
    # Sound budget lower bound: the largest single-code quota forces that many
    # distinct records because one record counts at most once per code.
    trivial_lower = max(request.quotas.values())
    if trivial_lower > request.budget:
        reasons.append({
            "type": "budget_lower_bound", "lower_bound": trivial_lower,
            "budget": request.budget,
            "check": f"a single code requires {trivial_lower} distinct records, above budget "
                     f"{request.budget}",
        })
    return reasons


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def build_audit_plan(ledger, payload: dict[str, Any]) -> dict[str, Any]:
    request = parse_request(payload)
    run, _run_dir, rejects = load_evidence(ledger, request.run_id)
    candidates, group_meta = build_candidates(rejects, request.exclusion_groups)
    all_codes = sorted({code for candidate in candidates for code in candidate.codes})
    file_issues = ledger.get_file_issues(request.run_id)
    file_level_codes = sorted({row["code"] for row in file_issues
                               if str(row["code"]).startswith("FL")})

    code_inventory = []
    for code in sorted(request.quotas):
        pool = sum(1 for candidate in candidates if code in candidate.codes)
        if code.startswith("FL"):
            kind = "file_level_not_sampleable"
        elif pool == 0:
            kind = "no_reject_candidate"
        else:
            kind = "reject_code"
        code_inventory.append({"code": code, "quota": request.quotas[code],
                               "candidate_rows": pool, "kind": kind})

    if request.contract == EXACT_CONTRACT:
        if len(candidates) > EXACT_MAX_CANDIDATES or request.budget > EXACT_MAX_BUDGET:
            raise AuditPlanError(
                f"exact contract is limited to {EXACT_MAX_CANDIDATES} candidates and budget "
                f"{EXACT_MAX_BUDGET}; this run has {len(candidates)} candidates and budget "
                f"{request.budget}; use contract=bounded")

    reasons = _precheck(request, candidates)
    incumbent = None
    if not reasons:
        incumbent = greedy_incumbent(request, candidates)
    result = SearchResult(incumbent, ([], dict(request.quotas)), 0, False)
    if not reasons:
        result = _solve(request, candidates, incumbent)

    selected: list[Candidate] = result.best or []
    if reasons:
        status = INFEASIBLE_STATUS
    elif result.best is not None:
        status = OPTIMAL_STATUS if request.contract == EXACT_CONTRACT else FEASIBLE_STATUS
    elif result.exhausted:
        status = UNDECIDED_STATUS
    else:
        # Exhaustive search finished without a solution and no precheck fired:
        # this is a proven infeasibility (group/budget interaction), never an
        # undecided result.
        status = INFEASIBLE_STATUS
        reasons = [{
            "type": "no_satisfiable_selection",
            "search_nodes": result.nodes,
            "check": "the exhaustive include-branch search completed without any selection "
                     "satisfying every quota, exclusion group and the budget",
        }]

    coverage = {code: [] for code in request.quotas}
    for candidate in selected:
        for code in candidate.codes:
            if code in coverage:
                coverage[code].append(candidate.record_no)
    coverage = {code: sorted(records) for code, records in sorted(coverage.items())}

    selected_records = [{
        "reject_no": candidate.reject_no, "record_no": candidate.record_no,
        "byte_offset": candidate.byte_offset, "raw_sha256": candidate.raw_sha256,
        "raw_file": candidate.raw_file, "codes": sorted(candidate.codes),
        "covers": sorted(code for code in candidate.codes if code in request.quotas),
        "exclusion_groups": sorted(candidate.groups),
    } for candidate in selected]

    quota_report = []
    for code, needed in request.quotas.items():
        covered = coverage.get(code, [])
        state = "satisfied" if len(covered) >= needed else (
            "unknown" if status == UNDECIDED_STATUS else "unmet")
        entry = {"code": code, "required": needed, "covered_by": covered,
                 "covered_count": len(covered), "state": state}
        if state != "satisfied":
            entry["shortfall"] = needed - len(covered)
        quota_report.append(entry)

    plan: dict[str, Any] = {
        "schema_version": 1,
        "run": {
            "run_id": run["run_id"], "run_key": run["run_key"], "status": run["status"],
            "out_dir": run["out_dir"],
            "input": {"name": run["input_name"], "sha256": run["input_sha256"],
                      "record_length": run["record_length"], "charset": run["charset"],
                      "recfm": run["recfm"]},
            "contracts": {
                "copybook": {"name": run["copybook_name"], "sha256": run["copybook_sha256"]},
                "rules": {"name": run["rules_name"], "sha256": run["rules_sha256"]},
            },
        },
        "rule_summary": {
            "reject_code_counts": dict(sorted(json.loads(run["reject_codes_json"]).items())),
            "ledger_reject_codes": all_codes,
            "file_level_codes_not_sampleable": file_level_codes,
            "reject_rows": len(rejects),
        },
        "request": {
            "quotas": request.quotas, "budget": request.budget,
            "exclusion_groups": group_meta["group_labels"],
        },
        "contract": {
            "name": request.contract,
            "node_budget": request.node_budget,
            "nodes_used": result.nodes,
            "budget_exhausted": result.exhausted,
            "exact_scale_limits": (
                {"max_candidates": EXACT_MAX_CANDIDATES, "max_budget": EXACT_MAX_BUDGET}
                if request.contract == EXACT_CONTRACT else None),
        },
        "status": status,
        "selected_count": len(selected_records),
        "selected_records": selected_records,
        "quota_coverage": quota_report,
        "code_inventory": code_inventory,
        "evidence_verification": {
            "verified_rows": len(rejects),
            "method": "sha256(raw_file bytes) == ledger raw_sha256 and len(bytes) == raw_length",
            "reconstructed_from_partial": False,
        },
        "read_only": True,
    }
    if status == OPTIMAL_STATUS:
        plan["optimality_proof"] = {
            "minimum_records": len(selected_records),
            "admissible_lower_bound": max(
                max(request.quotas.values()),
                admissible_lower_bound(dict(request.quotas), candidates)),
            "exhaustive_nodes": result.nodes,
            "check": "branch-and-bound pruned every selection smaller than the returned set "
                     "using admissible lower bounds; no smaller selection satisfies all quotas, "
                     "exclusion groups and the budget",
        }
    if status == INFEASIBLE_STATUS:
        partial_chosen, partial_remaining = result.partial
        plan["infeasibility_certificate"] = {
            "reasons": reasons,
            "best_partial_records": [candidate.record_no for candidate in partial_chosen],
            "unmet_quotas": dict(sorted(partial_remaining.items())),
            "search_nodes": result.nodes,
            "check": "each reason names a concrete constraint that no selection can satisfy",
        }
    if status == UNDECIDED_STATUS:
        partial_chosen, partial_remaining = result.partial
        proven = {code: request.quotas[code] - need
                  for code, need in partial_remaining.items()}
        plan["undecided_certificate"] = {
            "best_partial_records": [candidate.record_no for candidate in partial_chosen],
            "proven_coverage": dict(sorted(proven.items())),
            "unmet_quotas": dict(sorted(partial_remaining.items())),
            "search_nodes": result.nodes, "node_budget": request.node_budget,
            "check": "node budget exhausted before feasibility or infeasibility was proven; "
                     "this must not be read as an infeasibility result",
        }
    return plan
