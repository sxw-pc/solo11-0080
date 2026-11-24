"""离线多日余额计息与修订权威（原账/重述）。

本模块只读地消费既有运行的台账状态（``store/queries.LedgerQueries``）与
``accepted.jsonl`` / ``rejects.jsonl`` 产物（即 ``pipeline/outputs.py`` 的写出格式），
绝不重新执行或改写原运行及其产物。版本权威只来自显式的本地修订清单：同账户同日期
的两个独立 run 若没有可核对的替代链即视为冲突，不按文件名、运行创建时间或清单顺序
猜测版本。

计息规则：半开日区间 ``[start, end)``；某日快照自该日起生效至下一快照前一日；
每日金额为 ``AMOUNT * RATE / 365``，全程 ``Decimal`` 全精度累加，仅在报告最终
按 half-even 舍入到分，绝不逐日舍入。任何未知段或修订链分叉/成环都输出分段未知，
且不产生整期确定金额；无冲突的账户仍可独立结算。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Callable

from ..errors import InputError
from ..util import canonical_json, sha256_bytes, sha256_text
from ..store.queries import LedgerQueries

UTC = timezone.utc
CENT = Decimal("0.01")
YEAR_DAYS = Decimal(365)
PRECISION = 50
REQUIRED_FIELDS = ("ACCOUNT-ID", "AMOUNT", "RATE", "BOOK-DATE")
TERMINAL_STATUSES = {"completed", "completed_with_rejects", "failed"}
FileIssueQuery = Callable[[str], list[dict[str, Any]]]


# ---------------------------------------------------------------------------
# 清单数据结构与解析
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManifestEntry:
    run_id: str
    business_date: date
    expected_accounts: tuple[str, ...]
    supersedes_run_id: str | None
    authorized_at: datetime
    close_at: datetime


@dataclass
class EntryEvidence:
    """一次清单条目核对后的逐账户证据结论。"""

    entry: ManifestEntry
    reason: str | None = None
    detail: str | None = None
    # account -> 已验证的日状态载荷（携带源 run、record_no、raw_sha256、规则摘要）
    states: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    # account -> 未知原因
    unknown_accounts: dict[str, str] = dc_field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.unknown_accounts


def _parse_iso(text: Any, label: str) -> datetime:
    if not isinstance(text, str) or not text:
        raise InputError(f"manifest {label} must be a non-empty ISO-8601 string", code="CLI001")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise InputError(f"manifest {label} is not valid ISO-8601: {text!r}", code="CLI001") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def _parse_yyyymmdd(text: Any, label: str) -> date:
    if not isinstance(text, str) or len(text) != 8 or not text.isdigit():
        raise InputError(f"manifest {label} must use YYYYMMDD, got {text!r}", code="CLI001")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise InputError(f"manifest {label} is not a valid calendar date: {text!r}",
                         code="CLI001") from exc


def _parse_decimal(value: Any, label: str) -> Decimal:
    # 显式拒绝 float：RATE/AMOUNT 必须按十进制比例读取。
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (str, int)):
        raise InputError(f"{label} must be a decimal string, not float", code="CLI001")
    try:
        return Decimal(str(value).strip())
    except Exception as exc:  # InvalidOperation
        raise InputError(f"{label} is not a valid decimal: {value!r}", code="CLI001") from exc


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read manifest {path}: {exc}", code="IO001") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"manifest {path} must be UTF-8 JSON: {exc}", code="CLI001") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list) or not data["entries"]:
        raise InputError("manifest must be a JSON object with a non-empty entries list",
                         code="CLI001")
    return data


def parse_manifest(manifest: dict[str, Any]) -> tuple[list[ManifestEntry], datetime | None]:
    default_close = (_parse_iso(manifest["close_at"], "close_at")
                     if manifest.get("close_at") else None)
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(manifest["entries"]):
        label = f"entries[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"manifest {label} must be an object", code="CLI001")
        run_id = item.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise InputError(f"manifest {label}.run_id is required", code="CLI001")
        if run_id in seen:
            raise InputError(f"manifest run_id {run_id!r} appears more than once", code="CLI001")
        seen.add(run_id)
        business_date = _parse_yyyymmdd(item.get("business_date"), f"{label}.business_date")
        accounts = item.get("expected_accounts")
        if not isinstance(accounts, list) or not accounts or any(
                not isinstance(account, str) or not account for account in accounts):
            raise InputError(f"manifest {label}.expected_accounts must be a non-empty string list",
                             code="CLI001")
        if len(set(accounts)) != len(accounts):
            raise InputError(f"manifest {label}.expected_accounts contains duplicates", code="CLI001")
        supersedes = item.get("supersedes_run_id")
        if supersedes is not None and (not isinstance(supersedes, str) or not supersedes):
            raise InputError(f"manifest {label}.supersedes_run_id must be a string or null",
                             code="CLI001")
        authorized_at = _parse_iso(item.get("authorized_at"), f"{label}.authorized_at")
        close_at = (_parse_iso(item["close_at"], f"{label}.close_at")
                    if item.get("close_at") else default_close)
        if close_at is None:
            raise InputError(f"manifest {label} has no close_at and no top-level default",
                             code="CLI001")
        entries.append(ManifestEntry(run_id, business_date, tuple(accounts), supersedes,
                                     authorized_at, close_at))
    by_id = {entry.run_id: entry for entry in entries}
    for entry in entries:
        if entry.supersedes_run_id is not None:
            if entry.supersedes_run_id == entry.run_id:
                raise InputError(f"manifest entry {entry.run_id} cannot supersede itself",
                                 code="CLI001")
            if entry.supersedes_run_id not in by_id:
                raise InputError(
                    f"manifest entry {entry.run_id} supersedes unknown run "
                    f"{entry.supersedes_run_id!r}; revision links must resolve inside the manifest",
                    code="CLI001")
    return entries, default_close


# ---------------------------------------------------------------------------
# 运行证据核对（只读）
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _identifiable_account(account: Any) -> bool:
    return (isinstance(account, str) and len(account) == 12
            and account.startswith("ACCT") and account[4:].isdigit())


def _blank_evidence(entry: ManifestEntry, reason: str, detail: str | None = None) -> EntryEvidence:
    return EntryEvidence(
        entry=entry, reason=reason, detail=detail,
        unknown_accounts={account: reason for account in entry.expected_accounts})


def evaluate_entry(entry: ManifestEntry, run: dict[str, Any] | None,
                   copybook_sha256: str | None,
                   file_issue_query: FileIssueQuery) -> EntryEvidence:
    """核对单个清单条目的台账状态、全部摘要与产物证据，返回逐账户结论。"""

    if run is None:
        return _blank_evidence(entry, "run_not_found", "run_id absent from ledger")
    expected = set(entry.expected_accounts)

    if run["status"] not in TERMINAL_STATUSES:
        return _blank_evidence(entry, "run_not_terminal", f"status={run['status']}")
    if copybook_sha256 is not None and run.get("copybook_sha256") != copybook_sha256:
        return _blank_evidence(entry, "copybook_digest_mismatch",
                               f"run={run.get('copybook_sha256')} expected={copybook_sha256}")

    # 输入 / 规则摘要：重读磁盘原文件并与台账指纹比对。
    try:
        input_bytes = Path(run["input_path"]).read_bytes()
    except OSError as exc:
        return _blank_evidence(entry, "input_unreadable", str(exc))
    if sha256_bytes(input_bytes) != run.get("input_sha256"):
        return _blank_evidence(entry, "input_digest_mismatch",
                               "ledger fingerprint does not match input bytes")
    try:
        rules_digest = sha256_bytes(Path(run["rules_path"]).read_bytes())
    except OSError as exc:
        return _blank_evidence(entry, "rules_unreadable", str(exc))
    if rules_digest != run.get("rules_sha256"):
        return _blank_evidence(entry, "rules_digest_mismatch",
                               "ledger fingerprint does not match rules bytes")

    accepted_path = Path(run["accepted_path"]) if run.get("accepted_path") else None
    rejects_path = Path(run["rejects_path"]) if run.get("rejects_path") else None
    if not accepted_path or not accepted_path.is_file():
        return _blank_evidence(entry, "accepted_artifact_missing", str(accepted_path))
    try:
        accepted_rows = _load_jsonl(accepted_path)
    except (OSError, json.JSONDecodeError) as exc:
        return _blank_evidence(entry, "accepted_artifact_unreadable", str(exc))
    if len(accepted_rows) != int(run.get("accepted_records", 0)):
        return _blank_evidence(entry, "accepted_count_mismatch",
                               f"rows={len(accepted_rows)} ledger={run.get('accepted_records')}")

    # accepted.jsonl 摘要：逐行 record_no/byte_offset/raw_sha256 与原输入字节切片核对。
    seen_record_nos: set[int] = set()
    parsed_lines: list[dict[str, Any]] = []
    malformed_line = False
    for row in accepted_rows:
        record_no, offset, raw_sha = row.get("record_no"), row.get("byte_offset"), \
            row.get("raw_sha256")
        if not isinstance(record_no, int) or record_no in seen_record_nos:
            return _blank_evidence(entry, "accepted_record_no_invalid", f"row={record_no!r}")
        seen_record_nos.add(record_no)
        length = row.get("raw_length")
        if not isinstance(offset, int) or not isinstance(length, int) or not isinstance(raw_sha, str):
            return _blank_evidence(entry, "accepted_line_malformed", f"record_no={record_no}")
        if length <= 0 or offset < 0 or offset + length > len(input_bytes) or \
                sha256_bytes(input_bytes[offset:offset + length]) != raw_sha:
            return _blank_evidence(entry, "accepted_digest_mismatch", f"record_no={record_no}")
        values = row.get("values")
        if not isinstance(values, dict) or any(not isinstance(values.get(name), str)
                                               for name in REQUIRED_FIELDS):
            malformed_line = True
            continue
        parsed_lines.append({"row": row, "values": values})

    # 文件级失败 / max-records 前缀：该文件全部预期账户在该日期未知。
    issues = file_issue_query(entry.run_id)
    issue_codes = {item["code"] for item in issues}
    if "RD004" in issue_codes:
        return _blank_evidence(entry, "max_records_prefix",
                               "max-records guard stopped before end of file")
    if run.get("status") == "failed" or any(item.get("severity") == "error" for item in issues):
        return _blank_evidence(entry, "file_level_failure",
                               ",".join(sorted(issue_codes)) or "run failed")
    if malformed_line:
        return _blank_evidence(entry, "accepted_line_malformed",
                               "accepted row missing ACCOUNT-ID/AMOUNT/RATE/BOOK-DATE")

    # 拒绝记录：可识别 ACCOUNT-ID 则该账户未知；不可识别则全部预期账户未知。
    try:
        reject_rows = _load_jsonl(rejects_path) if rejects_path and rejects_path.is_file() else []
    except (OSError, json.JSONDecodeError) as exc:
        return _blank_evidence(entry, "rejects_artifact_unreadable", str(exc))
    evidence = EntryEvidence(entry=entry)
    for row in reject_rows:
        partial = row.get("partial") if isinstance(row.get("partial"), dict) else {}
        account = partial.get("ACCOUNT-ID")
        if not _identifiable_account(account):
            return _blank_evidence(entry, "reject_account_unidentifiable",
                                   f"record_no={row.get('record_no')} cannot identify ACCOUNT-ID")
        if account in expected:
            evidence.unknown_accounts[account] = "record_rejected"

    chosen: dict[str, dict[str, Any]] = {}
    for item in parsed_lines:
        values, row = item["values"], item["row"]
        account = values["ACCOUNT-ID"]
        if not _identifiable_account(account):
            return _blank_evidence(entry, "accepted_account_unidentifiable",
                                   f"record_no={row.get('record_no')}")
        if account not in expected:
            # 非本条目预期账户不参与该文件的日状态建立。
            continue
        if values["BOOK-DATE"] != entry.business_date.strftime("%Y%m%d"):
            return _blank_evidence(entry, "book_date_mismatch",
                                   f"record_no={row.get('record_no')} book-date "
                                   f"{values['BOOK-DATE']} != {entry.business_date:%Y%m%d}")
        try:
            amount = _parse_decimal(values["AMOUNT"], "AMOUNT")
            rate = _parse_decimal(values["RATE"], "RATE")
        except InputError as exc:
            return _blank_evidence(entry, "accepted_value_invalid", exc.message)
        if account in chosen:
            # 同一文件同账户多行无法可靠选择：该账户未知，缺行也不得解释为零余额。
            evidence.unknown_accounts[account] = "duplicate_accepted_account"
            chosen.pop(account, None)
            continue
        chosen[account] = {
            "business_date": entry.business_date.isoformat(),
            "source_run": entry.run_id,
            "record_no": row["record_no"],
            "raw_sha256": row["raw_sha256"],
            "rules_sha256": rules_digest,
            "amount": format(amount, "f"),
            "rate": format(rate, "f"),
            "authorized_at": entry.authorized_at.isoformat().replace("+00:00", "Z"),
        }

    for account, state in chosen.items():
        # 该账户存在被拒记录（例如同账户重复行的第二条）时保持未知，
        # 不得因另有一条接收行就把拒绝结论覆盖掉。
        if account not in evidence.unknown_accounts:
            evidence.states[account] = state
    for account in entry.expected_accounts:
        if account not in evidence.states and account not in evidence.unknown_accounts:
            evidence.unknown_accounts[account] = "missing_accepted_record"
    if not evidence.ok and evidence.reason is None:
        evidence.reason = "account_level_unknown"
    return evidence


# ---------------------------------------------------------------------------
# 修订图：链、分叉、成环与无链冲突
# ---------------------------------------------------------------------------

@dataclass
class ChainGroup:
    accounts: tuple[str, ...]
    business_date: date
    entries: list[ManifestEntry]  # 旧 -> 新，沿 supersedes 边
    structural_reason: str | None = None

    @property
    def structural_unknown(self) -> bool:
        return self.structural_reason is not None


def build_groups(entries: list[ManifestEntry]) -> list[ChainGroup]:
    # 替代边：dict[被替代 run_id, 替代条目列表]；同一目标多条边即分叉。
    outgoing: dict[str, list[ManifestEntry]] = {}
    for entry in entries:
        if entry.supersedes_run_id is not None:
            outgoing.setdefault(entry.supersedes_run_id, []).append(entry)
    roots = [entry for entry in entries if entry.supersedes_run_id is None]

    groups: list[ChainGroup] = []
    assigned: set[str] = set()
    for root in roots:
        chain: list[ManifestEntry] = []
        reason: str | None = None
        queue: list[ManifestEntry] = [root]
        seen_local: set[str] = set()
        while queue:
            node = queue.pop(0)
            if node.run_id in seen_local:
                reason = "revision_cycle"
                continue
            seen_local.add(node.run_id)
            chain.append(node)
            next_items = outgoing.get(node.run_id, [])
            if len(next_items) > 1:
                reason = "revision_fork"
            queue.extend(next_items)
        for item in chain:
            assigned.add(item.run_id)
        # 线性链按替代边方向（旧 -> 新）稳定排序；分叉时该组已整体未知，
        # 排序仅保证产物确定性，不参与版本选择。
        if reason is None and len(chain) > 1:
            ordered = [root]
            cursor = root
            while outgoing.get(cursor.run_id):
                nxt = outgoing[cursor.run_id][0]
                ordered.append(nxt)
                cursor = nxt
            if len(ordered) == len(chain):
                chain = ordered
            else:
                chain = sorted(chain, key=lambda item: (item.authorized_at, item.run_id))
        elif reason is not None:
            chain = sorted(chain, key=lambda item: (item.authorized_at, item.run_id))
        dates = {item.business_date for item in chain}
        if reason is None and len(dates) != 1:
            reason = "chain_date_mismatch"
        accounts = tuple(sorted({account for item in chain for account in item.expected_accounts}))
        business_date = next(iter(dates)) if len(dates) == 1 else root.business_date
        groups.append(ChainGroup(accounts, business_date, chain, reason))

    # 成环且无根的组件（环上每个节点都声明了 supersedes）：沿双向边收集整个
    # 连通分量并标记成环，而不是把环上节点拆成互相冲突的独立组。
    incoming: dict[str, list[ManifestEntry]] = {}
    for entry in entries:
        if entry.supersedes_run_id is not None:
            incoming.setdefault(entry.supersedes_run_id, []).append(entry)
    for entry in entries:
        if entry.run_id in assigned:
            continue
        component: list[ManifestEntry] = []
        seen_ids: set[str] = set()
        stack = [entry]
        while stack:
            node = stack.pop()
            if node.run_id in seen_ids:
                continue
            seen_ids.add(node.run_id)
            component.append(node)
            stack.extend(outgoing.get(node.run_id, []))
            stack.extend(incoming.get(node.run_id, []))
        for item in component:
            assigned.add(item.run_id)
        dates = {item.business_date for item in component}
        accounts = tuple(sorted({account for item in component
                                 for account in item.expected_accounts}))
        business_date = next(iter(dates)) if len(dates) == 1 else entry.business_date
        groups.append(ChainGroup(
            accounts, business_date,
            sorted(component, key=lambda item: (item.authorized_at, item.run_id)),
            "revision_cycle"))

    # 同账户同日期存在两个互不相连的根 => 没有可核对替代链的冲突。
    ownership: dict[tuple[str, date], list[ChainGroup]] = {}
    for group in groups:
        for account in group.accounts:
            ownership.setdefault((account, group.business_date), []).append(group)
    for owners in ownership.values():
        if len({id(group) for group in owners}) > 1:
            for group in owners:
                group.structural_reason = "unlinked_version_conflict"
    return groups


# ---------------------------------------------------------------------------
# 版本选择
# ---------------------------------------------------------------------------

def select_version(group: ChainGroup, evidence_by_run: dict[str, EntryEvidence],
                   route: str, cutoff: datetime) -> tuple[ManifestEntry | None, str | None]:
    """在授权截止时刻内沿修订链选择权威版本。"""

    if group.structural_unknown:
        return None, group.structural_reason
    if route == "historical":
        root_close = group.entries[0].close_at
        if any(item.close_at != root_close for item in group.entries):
            return None, "close_time_mismatch"
    eligible = [item for item in group.entries if item.authorized_at <= cutoff]
    if not eligible:
        return None, "no_authorized_version"
    return eligible[-1], None


# ---------------------------------------------------------------------------
# 时间线、分段与全精度计息
# ---------------------------------------------------------------------------

@dataclass
class TimelinePoint:
    day: date
    state: dict[str, Any] | None
    unknown_reason: str | None


def _daterange(start: date, end: date):
    cursor = start
    while cursor < end:
        yield cursor
        cursor += timedelta(days=1)


def _build_timeline(account: str, start: date, end: date,
                    selected: dict[date, tuple[ManifestEntry | None, str | None]],
                    evidence_by_run: dict[str, EntryEvidence]) -> list[TimelinePoint]:
    """逐日时间线。

    某日存在快照时按当日选择的版本取状态；无快照日向前携带最近一个已知快照
    （半开 [start, end)：快照自当日生效至下一快照前一日）。开始日之前没有任何
    已授权状态时，前段保持未知；未知不会被后续已知快照反向填补。
    """

    points: list[TimelinePoint] = []
    carried_state: dict[str, Any] | None = None
    carried_reason: str | None = None
    for day in _daterange(start, end):
        if day in selected:
            entry_item, reason = selected[day]
            if reason is not None or entry_item is None:
                # 当日权威解析失败：记录未知，但不覆盖此前携带的已知快照。
                points.append(TimelinePoint(day, None, reason or "no_authorized_version"))
                carried_state, carried_reason = None, reason or "no_authorized_version"
                continue
            evidence = evidence_by_run[entry_item.run_id]
            if account in evidence.unknown_accounts:
                reason = evidence.unknown_accounts[account]
                points.append(TimelinePoint(day, None, reason))
                carried_state, carried_reason = None, reason
                continue
            state = evidence.states.get(account)
            if state is None:
                points.append(TimelinePoint(day, None, "missing_accepted_record"))
                carried_state, carried_reason = None, "missing_accepted_record"
            else:
                points.append(TimelinePoint(day, state, None))
                carried_state, carried_reason = state, None
        elif carried_state is not None:
            points.append(TimelinePoint(day, carried_state, None))
        else:
            points.append(TimelinePoint(day, None,
                                        carried_reason or "no_snapshot_for_date"))
    return points


def _make_segment(start: date, end: date, state: dict[str, Any] | None,
                  reason: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days,
        "kind": "known" if state is not None else "unknown",
    }
    if state is not None:
        payload.update({
            "source_run": state["source_run"], "record_no": state["record_no"],
            "raw_sha256": state["raw_sha256"], "rules_sha256": state["rules_sha256"],
            "amount": state["amount"], "rate": state["rate"],
        })
    else:
        payload["reason"] = reason or "unknown"
    return payload


def build_segments(points: list[TimelinePoint]) -> list[dict[str, Any]]:
    """把逐日点压缩为半开 [start, end) 分段；快照状态向前携带至下一快照前一日。"""

    if not points:
        return []
    segments: list[dict[str, Any]] = []
    seg_start = points[0].day
    cur_state, cur_reason = points[0].state, points[0].unknown_reason

    def identity(state: dict[str, Any] | None) -> tuple[str, str] | None:
        return None if state is None else (state["source_run"], state["raw_sha256"])

    for point in points[1:]:
        boundary = (point.state is None) != (cur_state is None) or \
            (point.state is not None and cur_state is not None and
             identity(point.state) != identity(cur_state))
        if boundary:
            segments.append(_make_segment(seg_start, point.day, cur_state, cur_reason))
            seg_start, cur_state, cur_reason = point.day, point.state, point.unknown_reason
        elif point.state is not None:
            cur_state = point.state
        elif cur_reason is None:
            cur_reason = point.unknown_reason
    segments.append(_make_segment(seg_start, points[-1].day + timedelta(days=1),
                                  cur_state, cur_reason))
    return segments


def accrue_segments(segments: list[dict[str, Any]]) -> Decimal | None:
    """全精度计息；存在未知段时返回 None，阻止整期金额冒充已确定。"""

    if any(item["kind"] == "unknown" for item in segments):
        return None
    total = Decimal(0)
    with localcontext() as context:
        context.prec = PRECISION
        for segment in segments:
            total += (Decimal(segment["amount"]) * Decimal(segment["rate"])
                      * Decimal(segment["days"]) / YEAR_DAYS)
    return total


def round_cents(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(context.prec, 28)
        return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# 报告装配
# ---------------------------------------------------------------------------

def _account_payload(account: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    exact = accrue_segments(segments)
    return {
        "account_id": account,
        "status": "determined" if exact is not None else "unknown_segments",
        "segments": segments,
        "interest_exact": format(exact, "f") if exact is not None else None,
        "interest_cents": format(round_cents(exact), "f") if exact is not None else None,
    }


def _assemble_block(route: str, accounts: list[str], start: date, end: date,
                    groups: list[ChainGroup], evidence_by_run: dict[str, EntryEvidence],
                    cutoff_for: Callable[[ChainGroup], datetime]) -> dict[str, Any]:
    # (date, account) -> (选中条目, 结构/授权原因)
    selections: dict[tuple[date, str], tuple[ManifestEntry | None, str | None]] = {}
    cutoffs: set[datetime] = set()
    for group in groups:
        cutoff = cutoff_for(group)
        cutoffs.add(cutoff)
        entry, reason = select_version(group, evidence_by_run, route, cutoff)
        for account in group.accounts:
            selections[(group.business_date, account)] = (entry, reason)

    payload_accounts: list[dict[str, Any]] = []
    for account in accounts:
        selected = {day: choice for (day, acc), choice in selections.items() if acc == account}
        points = _build_timeline(account, start, end, selected, evidence_by_run)
        payload_accounts.append(_account_payload(account, build_segments(points)))
    return {
        "route": route,
        "cutoffs": [item.isoformat().replace("+00:00", "Z")
                    for item in sorted(cutoffs)],
        "fully_known": all(item["status"] == "determined" for item in payload_accounts),
        "accounts": payload_accounts,
    }


def _diff_block(historical: dict[str, Any], restated: dict[str, Any]) -> dict[str, Any]:
    old_by = {item["account_id"]: item for item in historical["accounts"]}
    new_by = {item["account_id"]: item for item in restated["accounts"]}
    rows: list[dict[str, Any]] = []
    for account in sorted(set(old_by) | set(new_by)):
        old, new = old_by.get(account), new_by.get(account)
        old_cents = old["interest_cents"] if old else None
        new_cents = new["interest_cents"] if new else None
        if old_cents is not None and new_cents is not None:
            delta = round_cents(Decimal(new_cents) - Decimal(old_cents))
            row_status = "changed" if delta != 0 else "unchanged"
            delta_text = format(delta, "f")
        else:
            row_status, delta_text = "not_computable", None
        rows.append({
            "account_id": account,
            "historical_cents": old_cents,
            "restated_cents": new_cents,
            "delta_cents": delta_text,
            "status": row_status,
            "historical_source_runs": sorted({seg["source_run"] for seg in old["segments"]
                                              if seg["kind"] == "known"}) if old else [],
            "restated_source_runs": sorted({seg["source_run"] for seg in new["segments"]
                                            if seg["kind"] == "known"}) if new else [],
        })
    return {"fully_computable": all(row["status"] != "not_computable" for row in rows),
            "accounts": rows}


def build_accrual_report(*, manifest_path: Path, db_path: Path, start: date, end: date,
                         route: str, as_of: datetime | None = None,
                         copybook_sha256: str | None = None,
                         queries: LedgerQueries | None = None) -> dict[str, Any]:
    """基于已完成运行与本地修订清单计算账户期间利息。全程只读。"""

    if route not in {"historical", "restatement"}:
        raise InputError(f"unknown accrual route {route!r}", code="CLI001")
    if start >= end:
        raise InputError("accrual period must be half-open [start, end) with start < end",
                         code="CLI001")
    manifest = load_manifest(manifest_path)
    entries, default_close = parse_manifest(manifest)

    if queries is None:
        queries = LedgerQueries()
        queries.connect = lambda: _connect_readonly(db_path)  # type: ignore[method-assign]

    def _safe_get_run(run_id: str) -> dict[str, Any] | None:
        try:
            return queries.get_run(run_id)
        except Exception as exc:  # 数据库或表缺失：该 run 按未知处理而非中断整份报告。
            if isinstance(exc, sqlite3.OperationalError):
                return None
            raise

    evidence_by_run = {
        entry.run_id: evaluate_entry(
            entry, _safe_get_run(entry.run_id), copybook_sha256,
            lambda run_id: _safe_file_issues(queries, run_id))
        for entry in entries}
    groups = build_groups(entries)
    accounts = sorted({account for entry in entries for account in entry.expected_accounts})

    historical = _assemble_block(
        "historical", accounts, start, end, groups, evidence_by_run,
        lambda group: group.entries[0].close_at)
    report: dict[str, Any] = {
        "ok": True,
        "schema_version": 1,
        "manifest": {
            "path": str(manifest_path),
            "sha256": sha256_text(canonical_json(manifest)),
            "entries": len(entries),
            "default_close_at": default_close.isoformat().replace("+00:00", "Z")
            if default_close else None,
        },
        "period": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": (end - start).days},
        "routes": {"historical": historical},
    }
    if route == "restatement":
        if as_of is None:
            raise InputError("restatement route requires an --as-of authorization cutoff",
                             code="CLI001")
        restated = _assemble_block(
            "restatement", accounts, start, end, groups, evidence_by_run,
            lambda _group: as_of)
        report["routes"]["restatement"] = restated
        report["restatement_diff"] = _diff_block(historical, restated)
    return report


def _connect_readonly(db_path: Path):
    # 普通连接即可满足只读边界：本模块只执行 LedgerQueries 的 SELECT，
    # 且不实例化 Ledger、不调用任何写接口。URI 只读模式在数据库文件尚不存在时
    # 无法打开，这里交由调用方对缺失 run 产出 run_not_found 的未知结论。
    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _safe_file_issues(queries: LedgerQueries, run_id: str) -> list[dict[str, Any]]:
    try:
        return queries.get_file_issues(run_id)
    except sqlite3.OperationalError:
        return []
