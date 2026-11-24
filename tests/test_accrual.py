"""离线多日余额计息（accrue-balances）合成回归。

覆盖：跨月/闰日、负本金、同日多版授权前后、修订链分叉/成环/无链冲突、
被拒记录与缺日（缺行不等于零余额）、原账与重述差额、重复输入字节稳定、
RATE 变化驱动区间切分与重算，以及 max-records 前缀和摘要不匹配。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from conftest import FIXTURES
from copybook_kit.cli import main as cli_main
from copybook_kit.errors import InputError
from copybook_kit.pipeline.accrual import build_accrual_report
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.pipeline.runner import run
from copybook_kit.util import pretty_json, sha256_bytes

COPYBOOK = FIXTURES / "copybooks" / "balance.cpy"
RULES = FIXTURES / "rules" / "balance_rules.yaml"
UTC = timezone.utc
ACCT1, ACCT2, ACCT3 = "ACCT00000001", "ACCT00000002", "ACCT00000003"


def exact_daily(amount: str, rate: str, days: int) -> Decimal:
    """与 accrual.py 相同的 50 位全精度期望值。"""
    with localcontext() as context:
        context.prec = 50
        return Decimal(amount) * Decimal(rate) * Decimal(days) / Decimal(365)


# ---------------------------------------------------------------------------
# 合成余额文件构造（64 字节，与 fixtures/copybooks/balance.cpy 对齐）
# ---------------------------------------------------------------------------

def pack_comp3(value: str, digits: int = 11, scale: int = 2) -> bytes:
    scaled = int(Decimal(value).scaleb(scale))
    sign = 0xD if scaled < 0 else 0xC
    text = str(abs(scaled)).zfill(digits)
    nibbles = [int(char) for char in text] + [sign]
    if len(nibbles) % 2:
        nibbles.insert(0, 0)
    return bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles), 2))


def put(buffer: bytearray, offset: int, text: str, width: int) -> None:
    raw = text.encode("ascii")
    buffer[offset:offset + width] = raw.ljust(width, b" ")


def balance_record(account: str, amount: str, rate: str, book_date: str,
                   memo: str = "SYNTHETIC BALANCE", rec_type: str = "D") -> bytes:
    buffer = bytearray(b" " * 64)
    put(buffer, 0, rec_type, 1)
    put(buffer, 1, account, 12)
    buffer[13:19] = pack_comp3(amount)
    put(buffer, 19, rate.replace(".", "").zfill(7), 7)
    put(buffer, 26, book_date, 8)
    put(buffer, 34, memo, 30)
    return bytes(buffer)


def write_balance_file(tmp_path, name: str, rows: list[bytes]) -> Path:
    path = tmp_path / name
    path.write_bytes(b"".join(rows))
    return path


def run_balance(tmp_path, name: str, rows: list[bytes], *, max_records: int = 0):
    input_path = write_balance_file(tmp_path, name, rows)
    result = run(RunOptions(
        input_path=input_path, copybook_path=COPYBOOK, rules_path=RULES,
        out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite",
        now=datetime(2026, 1, 1, tzinfo=UTC), max_records=max_records))
    return result, input_path


def write_manifest(tmp_path, entries: list[dict], *, close_at=None) -> Path:
    payload: dict = {"entries": entries}
    if close_at is not None:
        payload["close_at"] = close_at
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def entry(run_id, business_date, accounts, *, supersedes=None, authorized_at, close_at=None):
    item = {"run_id": run_id, "business_date": business_date,
            "expected_accounts": accounts, "supersedes_run_id": supersedes,
            "authorized_at": authorized_at}
    if close_at is not None:
        item["close_at"] = close_at
    return item


def account_block(report: dict, route: str, account: str) -> dict:
    return next(item for item in report["routes"][route]["accounts"]
                if item["account_id"] == account)


def find_segment(segments: list[dict], day: str) -> dict:
    return next(seg for seg in segments if seg["start"] <= day < seg["end"])


# ---------------------------------------------------------------------------
# 规范示例：BOOK-DATE=20260228 原账 vs 次日才授权的替代文件
# ---------------------------------------------------------------------------

def test_historical_vs_restatement_canonical_revision(tmp_path):
    original, _ = run_balance(tmp_path, "original.dat", [
        balance_record(ACCT1, "1000.00", "0.0500", "20260228")])
    replacement, _ = run_balance(tmp_path, "replacement.dat", [
        balance_record(ACCT1, "1200.00", "0.0600", "20260228")])
    manifest = write_manifest(tmp_path, [
        entry(original.run_id, "20260228", [ACCT1],
              authorized_at="2026-02-27T00:00:00Z", close_at="2026-03-01T00:00:00Z"),
        entry(replacement.run_id, "20260228", [ACCT1], supersedes=original.run_id,
              authorized_at="2026-03-02T00:00:00Z", close_at="2026-03-01T00:00:00Z"),
    ])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 2, 28), end=date(2026, 3, 5),
        route="restatement", as_of=datetime(2026, 3, 3, tzinfo=UTC),
        copybook_sha256=__import__("copybook_kit.util", fromlist=["sha256_bytes"])
        .sha256_bytes(COPYBOOK.read_bytes()))

    historical = account_block(report, "historical", ACCT1)
    restated = account_block(report, "restatement", ACCT1)
    # 关账当日替代版本尚未授权：原账冻结 1000.00 / 0.0500。
    assert historical["status"] == "determined"
    assert historical["segments"][0]["source_run"] == original.run_id
    assert historical["segments"][0]["amount"] == "1000.00"
    assert historical["segments"][0]["rate"] == "0.0500"
    assert historical["interest_exact"] == format(exact_daily("1000.00", "0.0500", 5), "f")
    assert historical["interest_cents"] == "0.68"
    # 重述沿修订链采用 1200.00 / 0.0600。
    assert restated["segments"][0]["source_run"] == replacement.run_id
    assert restated["interest_cents"] == "0.99"
    # 两个结果不得共用来源标签，重述不得覆盖原账。
    assert report["routes"]["historical"]["route"] == "historical"
    assert report["routes"]["restatement"]["route"] == "restatement"
    assert historical["segments"][0]["source_run"] != restated["segments"][0]["source_run"]
    diff = next(item for item in report["restatement_diff"]["accounts"]
                if item["account_id"] == ACCT1)
    assert diff["historical_cents"] == "0.68" and diff["restated_cents"] == "0.99"
    assert diff["delta_cents"] == "0.31" and diff["status"] == "changed"
    assert diff["historical_source_runs"] == [original.run_id]
    assert diff["restated_source_runs"] == [replacement.run_id]


def test_historical_route_alone_does_not_require_as_of(tmp_path):
    original, _ = run_balance(tmp_path, "original.dat", [
        balance_record(ACCT1, "1000.00", "0.0500", "20260228")])
    manifest = write_manifest(tmp_path, [
        entry(original.run_id, "20260228", [ACCT1],
              authorized_at="2026-02-27T00:00:00Z", close_at="2026-03-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 2, 28), end=date(2026, 3, 1), route="historical")
    assert "restatement" not in report["routes"]
    assert account_block(report, "historical", ACCT1)["interest_cents"] == "0.14"


def test_restatement_without_as_of_is_cli_error(tmp_path):
    original, _ = run_balance(tmp_path, "original.dat", [
        balance_record(ACCT1, "1000.00", "0.0500", "20260228")])
    manifest = write_manifest(tmp_path, [
        entry(original.run_id, "20260228", [ACCT1],
              authorized_at="2026-02-27T00:00:00Z", close_at="2026-03-01T00:00:00Z")])
    with pytest.raises(InputError) as caught:
        build_accrual_report(manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
                             start=date(2026, 2, 28), end=date(2026, 3, 1),
                             route="restatement")
    assert caught.value.code == "CLI001"


# ---------------------------------------------------------------------------
# 跨月 / 闰日 / half-even 最终舍入 / 不逐日舍入
# ---------------------------------------------------------------------------

def test_leap_day_cross_month_segments_and_half_even_rounding(tmp_path):
    feb, _ = run_balance(tmp_path, "feb.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20240228")])
    mar, _ = run_balance(tmp_path, "mar.dat", [
        balance_record(ACCT1, "730.00", "0.5000", "20240301")])
    manifest = write_manifest(tmp_path, [
        entry(feb.run_id, "20240228", [ACCT1],
              authorized_at="2024-02-27T00:00:00Z", close_at="2024-04-01T00:00:00Z"),
        entry(mar.run_id, "20240301", [ACCT1],
              authorized_at="2024-02-29T00:00:00Z", close_at="2024-04-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2024, 2, 28), end=date(2024, 3, 2), route="historical")
    block = account_block(report, "historical", ACCT1)
    segments = block["segments"]
    # 2024 为闰年：首段覆盖 02-28、02-29 共 2 天，跨月后 03-01 另起一段。
    assert [(seg["start"], seg["end"], seg["days"]) for seg in segments] == [
        ("2024-02-28", "2024-03-01", 2), ("2024-03-01", "2024-03-02", 1)]
    # 365*0.125/365 = 0.125/天；两日合计 0.25（逐日 half-even 会错误得到 0.24）。
    with localcontext() as context:
        context.prec = 50
        expected = (Decimal("365.00") * Decimal("0.1250") * Decimal(2)
                    + Decimal("730.00") * Decimal("0.5000") * Decimal(1)) / Decimal(365)
    assert block["interest_exact"] == format(expected, "f")
    assert block["interest_cents"] == "1.25"


def test_half_even_rounding_at_final_report_only(tmp_path):
    # 单日精确 0.125 => half-even 舍到 0.12；0.375 => 舍到 0.38。
    run_a, _ = run_balance(tmp_path, "a.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260101")])
    run_b, _ = run_balance(tmp_path, "b.dat", [
        balance_record(ACCT2, "365.00", "0.3750", "20260101")])
    manifest = write_manifest(tmp_path, [
        entry(run_a.run_id, "20260101", [ACCT1],
              authorized_at="2025-12-31T00:00:00Z", close_at="2026-02-01T00:00:00Z"),
        entry(run_b.run_id, "20260101", [ACCT2],
              authorized_at="2025-12-31T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 1), end=date(2026, 1, 2), route="historical")
    assert account_block(report, "historical", ACCT1)["interest_cents"] == "0.12"
    assert account_block(report, "historical", ACCT2)["interest_cents"] == "0.38"


def test_negative_principal_accrues_negative_interest(tmp_path):
    result, _ = run_balance(tmp_path, "neg.dat", [
        balance_record(ACCT3, "-200.00", "0.1000", "20260110")])
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260110", [ACCT3],
              authorized_at="2026-01-09T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 10), end=date(2026, 1, 13), route="historical")
    block = account_block(report, "historical", ACCT3)
    assert block["segments"][0]["amount"] == "-200.00"
    assert block["interest_exact"] == format(exact_daily("-200.00", "0.1000", 3), "f")
    assert block["interest_cents"] == "-0.16"


# ---------------------------------------------------------------------------
# 同日多版：授权时刻决定原账/重述可见性
# ---------------------------------------------------------------------------

def test_versions_authorized_after_close_are_invisible_to_historical(tmp_path):
    early, _ = run_balance(tmp_path, "early.dat", [
        balance_record(ACCT1, "100.00", "0.0100", "20260105")])
    late, _ = run_balance(tmp_path, "late.dat", [
        balance_record(ACCT1, "200.00", "0.0200", "20260105")])
    manifest = write_manifest(tmp_path, [
        entry(early.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-01-06T00:00:00Z"),
        entry(late.run_id, "20260105", [ACCT1], supersedes=early.run_id,
              authorized_at="2026-01-07T00:00:00Z", close_at="2026-01-06T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 8),
        route="restatement", as_of=datetime(2026, 1, 10, tzinfo=UTC))
    historical = account_block(report, "historical", ACCT1)
    restated = account_block(report, "restatement", ACCT1)
    assert historical["segments"][0]["source_run"] == early.run_id
    assert restated["segments"][0]["source_run"] == late.run_id
    # 重述截止时刻早于替代授权：链上只有原版本可用，仍可确定。
    early_cut = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 8),
        route="restatement", as_of=datetime(2026, 1, 5, tzinfo=UTC))
    block = account_block(early_cut, "restatement", ACCT1)
    assert block["status"] == "determined"
    assert block["segments"][0]["source_run"] == early.run_id

    # 截止时刻早于链上任何授权：该日期无已授权状态 => 未知，而非猜测版本。
    none_cut = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 8),
        route="restatement", as_of=datetime(2026, 1, 3, tzinfo=UTC))
    none_block = account_block(none_cut, "restatement", ACCT1)
    assert none_block["status"] == "unknown_segments"
    assert none_block["segments"][0]["reason"] == "no_authorized_version"


# ---------------------------------------------------------------------------
# 修订链分叉 / 成环 / 无链冲突：分段未知，无冲突账户独立结算
# ---------------------------------------------------------------------------

def test_revision_fork_marks_account_unknown_but_other_account_settles(tmp_path):
    r1, _ = run_balance(tmp_path, "r1.dat", [
        balance_record(ACCT1, "100.00", "0.0100", "20260105")])
    r2, _ = run_balance(tmp_path, "r2.dat", [
        balance_record(ACCT1, "110.00", "0.0100", "20260105")])
    r3, _ = run_balance(tmp_path, "r3.dat", [
        balance_record(ACCT1, "120.00", "0.0100", "20260105")])
    clean, _ = run_balance(tmp_path, "clean.dat", [
        balance_record(ACCT2, "365.00", "0.1250", "20260105")])
    manifest = write_manifest(tmp_path, [
        entry(r1.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z"),
        entry(r2.run_id, "20260105", [ACCT1], supersedes=r1.run_id,
              authorized_at="2026-01-05T00:00:00Z", close_at="2026-02-01T00:00:00Z"),
        entry(r3.run_id, "20260105", [ACCT1], supersedes=r1.run_id,
              authorized_at="2026-01-05T12:00:00Z", close_at="2026-02-01T00:00:00Z"),
        entry(clean.run_id, "20260105", [ACCT2],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 7), route="historical")
    blocked = account_block(report, "historical", ACCT1)
    settled = account_block(report, "historical", ACCT2)
    assert blocked["status"] == "unknown_segments"
    assert {seg["reason"] for seg in blocked["segments"]} == {"revision_fork"}
    assert blocked["interest_exact"] is None and blocked["interest_cents"] is None
    assert settled["status"] == "determined"
    assert settled["interest_cents"] == "0.25"
    assert report["routes"]["historical"]["fully_known"] is False


def test_revision_cycle_is_unknown(tmp_path):
    r1, _ = run_balance(tmp_path, "c1.dat", [
        balance_record(ACCT1, "100.00", "0.0100", "20260105")])
    r2, _ = run_balance(tmp_path, "c2.dat", [
        balance_record(ACCT1, "200.00", "0.0100", "20260105")])
    manifest = write_manifest(tmp_path, [
        entry(r1.run_id, "20260105", [ACCT1], supersedes=r2.run_id,
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z"),
        entry(r2.run_id, "20260105", [ACCT1], supersedes=r1.run_id,
              authorized_at="2026-01-05T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    blocked = account_block(report, "historical", ACCT1)
    assert blocked["status"] == "unknown_segments"
    assert {seg["reason"] for seg in blocked["segments"]} == {"revision_cycle"}


def test_unlinked_same_day_versions_are_conflicts_not_guessed(tmp_path):
    r1, _ = run_balance(tmp_path, "u1.dat", [
        balance_record(ACCT1, "100.00", "0.0100", "20260105")])
    r2, _ = run_balance(tmp_path, "u2.dat", [
        balance_record(ACCT1, "200.00", "0.0100", "20260105")])
    # 两个独立根，没有替代链；清单顺序/文件名/创建时间都不能用来猜版本。
    manifest = write_manifest(tmp_path, [
        entry(r1.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z"),
        entry(r2.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    blocked = account_block(report, "historical", ACCT1)
    assert blocked["status"] == "unknown_segments"
    assert {seg["reason"] for seg in blocked["segments"]} == {"unlinked_version_conflict"}


def test_manifest_revision_link_must_resolve(tmp_path):
    r1, _ = run_balance(tmp_path, "x.dat", [
        balance_record(ACCT1, "100.00", "0.0100", "20260105")])
    manifest = write_manifest(tmp_path, [
        entry(r1.run_id, "20260105", [ACCT1], supersedes="run-doesnotexist",
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    with pytest.raises(InputError) as caught:
        build_accrual_report(manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
                             start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    assert caught.value.code == "CLI001"


def test_top_level_close_at_default_and_bad_manifest_shapes(tmp_path):
    result, _ = run_balance(tmp_path, "d.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105")])
    # 条目不带 close_at，回退到顶层默认。
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z")], close_at="2026-02-01T00:00:00Z")
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    assert account_block(report, "historical", ACCT1)["interest_cents"] == "0.12"
    assert report["manifest"]["default_close_at"] == "2026-02-01T00:00:00Z"

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"entries": []}), encoding="utf-8")
    with pytest.raises(InputError):
        build_accrual_report(manifest_path=bad, db_path=tmp_path / "ledger.sqlite",
                             start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    bad.write_text(json.dumps({"entries": [{"run_id": result.run_id}]}), encoding="utf-8")
    with pytest.raises(InputError):
        build_accrual_report(manifest_path=bad, db_path=tmp_path / "ledger.sqlite",
                             start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")


def test_restatement_diff_marks_not_computable_when_unknown(tmp_path):
    # 链上两个版本在重述截止时刻都未授权 => 重述未知；原账仍可确定。
    r1, _ = run_balance(tmp_path, "h.dat", [
        balance_record(ACCT1, "1000.00", "0.0500", "20260105")])
    r2, _ = run_balance(tmp_path, "r.dat", [
        balance_record(ACCT1, "1200.00", "0.0600", "20260105")])
    manifest = write_manifest(tmp_path, [
        entry(r1.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-01-10T00:00:00Z"),
        entry(r2.run_id, "20260105", [ACCT1], supersedes=r1.run_id,
              authorized_at="2026-01-12T00:00:00Z", close_at="2026-01-10T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 8),
        route="restatement", as_of=datetime(2026, 1, 3, tzinfo=UTC))
    historical = account_block(report, "historical", ACCT1)
    restated = account_block(report, "restatement", ACCT1)
    assert historical["status"] == "determined"
    assert restated["status"] == "unknown_segments"
    diff = next(item for item in report["restatement_diff"]["accounts"]
                if item["account_id"] == ACCT1)
    assert diff["status"] == "not_computable" and diff["delta_cents"] is None
    assert diff["historical_cents"] is not None and diff["restated_cents"] is None
    assert report["restatement_diff"]["fully_computable"] is False


# ---------------------------------------------------------------------------
# 被拒记录、缺日、max-records 前缀、文件级失败、摘要不匹配
# ---------------------------------------------------------------------------

def test_rejected_identifiable_account_is_unknown_others_settle(tmp_path):
    result, _ = run_balance(tmp_path, "mix.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105"),
        balance_record("ACCT00000009", "10.00", "0.0100", "20261301")])  # R004_DATE
    assert result.status == "completed_with_rejects"
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1, "ACCT00000009"],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    ok = account_block(report, "historical", ACCT1)
    bad = account_block(report, "historical", "ACCT00000009")
    assert ok["status"] == "determined" and ok["interest_cents"] == "0.12"
    assert bad["status"] == "unknown_segments"
    assert bad["segments"][0]["reason"] == "record_rejected"


def test_unidentifiable_reject_blanks_all_expected_accounts(tmp_path):
    result, _ = run_balance(tmp_path, "blind.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105"),
        balance_record("BADACCOUNT01", "10.00", "0.0100", "20260105", rec_type="X")])
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1, ACCT2],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    for account in (ACCT1, ACCT2):
        block = account_block(report, "historical", account)
        assert block["status"] == "unknown_segments"
        assert block["segments"][0]["reason"] == "reject_account_unidentifiable"


def test_missing_accepted_line_is_unknown_not_zero_balance(tmp_path):
    # 文件只含 ACCT2，但清单预期 ACCT1：缺行不得解释为零余额。
    result, _ = run_balance(tmp_path, "other.dat", [
        balance_record(ACCT2, "365.00", "0.1250", "20260105")])
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 8), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert block["status"] == "unknown_segments"
    assert block["interest_exact"] is None
    assert {seg["reason"] for seg in block["segments"]} == {"missing_accepted_record"}
    assert all(seg["kind"] == "unknown" for seg in block["segments"])


def test_missing_day_before_first_snapshot_is_unknown_then_snapshot_carries(tmp_path):
    result, _ = run_balance(tmp_path, "snap.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260103")])
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260103", [ACCT1],
              authorized_at="2026-01-02T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 1), end=date(2026, 1, 5), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert block["status"] == "unknown_segments"
    assert [(seg["start"], seg["end"], seg["kind"]) for seg in block["segments"]] == [
        ("2026-01-01", "2026-01-03", "unknown"), ("2026-01-03", "2026-01-05", "known")]
    assert block["segments"][0]["reason"] == "no_snapshot_for_date"
    # 存在未知段时不得给出任何整期金额。
    assert block["interest_exact"] is None and block["interest_cents"] is None


def test_mid_period_unknown_blocks_full_period_and_unknown_carries_forward(tmp_path):
    # 01-01 有正常快照；01-03 的文件被拒（账户未知），01-03 起该日期状态未知，
    # 之后没有新快照，未知向前携带到区间结束；同期干净账户独立结算。
    good, _ = run_balance(tmp_path, "good.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260101"),
        balance_record(ACCT2, "365.00", "0.1250", "20260101")])
    broken, _ = run_balance(tmp_path, "broken.dat", [
        balance_record("BADACCOUNT01", "10.00", "0.0100", "20260103", rec_type="X")])
    manifest = write_manifest(tmp_path, [
        entry(good.run_id, "20260101", [ACCT1, ACCT2],
              authorized_at="2025-12-31T00:00:00Z", close_at="2026-02-01T00:00:00Z"),
        entry(broken.run_id, "20260103", [ACCT1],
              authorized_at="2026-01-02T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 1), end=date(2026, 1, 5), route="historical")
    blocked = account_block(report, "historical", ACCT1)
    clean = account_block(report, "historical", ACCT2)
    assert blocked["status"] == "unknown_segments"
    assert blocked["interest_exact"] is None
    kinds = [(seg["start"], seg["end"], seg["kind"], seg.get("reason"))
             for seg in blocked["segments"]]
    assert kinds == [
        ("2026-01-01", "2026-01-03", "known", None),
        ("2026-01-03", "2026-01-05", "unknown", "reject_account_unidentifiable")]
    # 干净账户不受牵连，快照携带整段并独立结算。
    assert clean["status"] == "determined"
    assert len(clean["segments"]) == 1 and clean["segments"][0]["days"] == 4
    assert clean["interest_cents"] == "0.50"
    assert report["routes"]["historical"]["fully_known"] is False


def test_max_records_prefix_blanks_expected_accounts(tmp_path):
    result, _ = run_balance(tmp_path, "prefix.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105"),
        balance_record(ACCT2, "365.00", "0.1250", "20260105")], max_records=1)
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1, ACCT2],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    for account in (ACCT1, ACCT2):
        block = account_block(report, "historical", account)
        assert block["segments"][0]["reason"] == "max_records_prefix"


def test_file_level_failure_blanks_expected_accounts(tmp_path):
    input_path = write_balance_file(tmp_path, "empty.dat", [])
    result = run(RunOptions(
        input_path=input_path, copybook_path=COPYBOOK, rules_path=RULES,
        out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite",
        now=datetime(2026, 1, 1, tzinfo=UTC)))
    assert result.status == "failed"
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert block["segments"][0]["reason"] == "file_level_failure"


def test_input_digest_mismatch_blanks_expected_accounts(tmp_path):
    result, input_path = run_balance(tmp_path, "tamper.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105")])
    # 运行完成后篡改磁盘原输入：台账指纹与现字节不再一致。
    input_path.write_bytes(b"x" * 64)
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert block["segments"][0]["reason"] == "input_digest_mismatch"


def test_rules_digest_mismatch_blanks_expected_accounts(tmp_path):
    result, input_path = run_balance(tmp_path, "rules.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105")])
    # 运行完成后替换磁盘上的规则文件：台账 rules_sha256 与现字节不再一致。
    rules_copy = tmp_path / "mutated_rules.yaml"
    rules_copy.write_text(
        RULES.read_text(encoding="utf-8") + "\n# mutated after run completed\n",
        encoding="utf-8")
    import sqlite3
    connection = sqlite3.connect(tmp_path / "ledger.sqlite")
    connection.execute("UPDATE runs SET rules_path = ? WHERE run_id = ?",
                       (str(rules_copy), result.run_id))
    connection.commit()
    connection.close()
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert block["segments"][0]["reason"] == "rules_digest_mismatch"


def test_rejected_account_plus_missing_sibling_account(tmp_path):
    # ACCT1 正常接收；ACCT2 既无接收行也无拒绝行 => missing，而不是零余额。
    result, _ = run_balance(tmp_path, "partial.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105")])
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1, ACCT2],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 7), route="historical")
    ok = account_block(report, "historical", ACCT1)
    missing = account_block(report, "historical", ACCT2)
    assert ok["status"] == "determined"
    assert {seg["reason"] for seg in missing["segments"]} == {"missing_accepted_record"}
    assert missing["interest_cents"] is None


def test_accepted_jsonl_tampering_is_detected_via_digest(tmp_path):
    result, input_path = run_balance(tmp_path, "acc.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105")])
    run_dir = tmp_path / "runs" / result.run_id
    accepted = run_dir / "accepted.jsonl"
    rows = [json.loads(line) for line in accepted.read_text(encoding="utf-8").splitlines()]
    # 篡改 accepted.jsonl 的 raw_sha256：与原输入字节切片重算的摘要必须对不上。
    rows[0]["raw_sha256"] = "0" * 64
    accepted.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
                        encoding="utf-8")
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert block["segments"][0]["reason"] == "accepted_digest_mismatch"


def test_copybook_digest_mismatch_blanks_expected_accounts(tmp_path):
    result, _ = run_balance(tmp_path, "cb.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105")])
    other_copybook = FIXTURES / "copybooks" / "customer.cpy"
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical",
        copybook_sha256=sha256_bytes(other_copybook.read_bytes()))
    block = account_block(report, "historical", ACCT1)
    assert block["segments"][0]["reason"] == "copybook_digest_mismatch"


def test_unknown_run_id_is_unknown_not_an_exception(tmp_path):
    manifest = write_manifest(tmp_path, [
        entry("run-ghostghostghostgho", "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 6), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert block["segments"][0]["reason"] == "run_not_found"


def test_book_date_must_match_manifest_business_date(tmp_path):
    result, _ = run_balance(tmp_path, "dated.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260106")])
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 7), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert {seg["reason"] for seg in block["segments"]} == {"book_date_mismatch"}


# ---------------------------------------------------------------------------
# RATE 变化驱动区间切分与重算
# ---------------------------------------------------------------------------

def test_identical_daily_snapshots_keep_one_carry_segment(tmp_path):
    # 两个不同日期的文件，记录原字节完全相同（同金额/利率/账户，仅 BOOK-DATE 不同会改变字节，
    # 因此这里用相同 BOOK-DATE 的两个独立日文件，靠清单 business_date 区分授权快照）。
    rows = [balance_record(ACCT1, "365.00", "0.1250", "20260101")]
    r1, _ = run_balance(tmp_path, "d1.dat", rows)
    r2, _ = run_balance(tmp_path, "d2.dat", rows)  # 相同字节 => 相同 run_key 幂等
    assert r1.run_id == r2.run_id
    manifest = write_manifest(tmp_path, [
        entry(r1.run_id, "20260101", [ACCT1],
              authorized_at="2025-12-31T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 1), end=date(2026, 1, 6), route="historical")
    block = account_block(report, "historical", ACCT1)
    # 单一快照携带整段，不产生多余切分。
    assert [(seg["start"], seg["end"], seg["days"]) for seg in block["segments"]] == [
        ("2026-01-01", "2026-01-06", 5)]
    assert block["interest_cents"] == "0.62"  # 0.125 * 5


def test_duplicate_accepted_lines_for_account_make_it_unknown(tmp_path):
    result, _ = run_balance(tmp_path, "dup.dat", [
        balance_record(ACCT1, "100.00", "0.0100", "20260105"),
        balance_record(ACCT1, "200.00", "0.0200", "20260105")])
    # 同账户重复行触发唯一键规则 => 第二行被拒，第一行接收。
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 5), end=date(2026, 1, 7), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert block["status"] == "unknown_segments"
    assert block["segments"][0]["reason"] == "record_rejected"


def test_rate_change_on_later_snapshot_splits_segments(tmp_path):
    first, _ = run_balance(tmp_path, "rate1.dat", [
        balance_record(ACCT1, "1000.00", "0.0500", "20260101")])
    second, _ = run_balance(tmp_path, "rate2.dat", [
        balance_record(ACCT1, "1000.00", "0.0800", "20260103")])
    manifest = write_manifest(tmp_path, [
        entry(first.run_id, "20260101", [ACCT1],
              authorized_at="2025-12-31T00:00:00Z", close_at="2026-02-01T00:00:00Z"),
        entry(second.run_id, "20260103", [ACCT1],
              authorized_at="2026-01-02T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 1), end=date(2026, 1, 5), route="historical")
    block = account_block(report, "historical", ACCT1)
    assert [(seg["start"], seg["end"], seg["days"], seg["rate"]) for seg in block["segments"]] == [
        ("2026-01-01", "2026-01-03", 2, "0.0500"),
        ("2026-01-03", "2026-01-05", 2, "0.0800")]
    with localcontext() as context:
        context.prec = 50
        expected = (Decimal("1000.00") * Decimal("0.0500") * Decimal(2)
                    + Decimal("1000.00") * Decimal("0.0800") * Decimal(2)) / Decimal(365)
    assert block["interest_exact"] == format(expected, "f")
    assert block["segments"][0]["source_run"] == first.run_id
    assert block["segments"][1]["source_run"] == second.run_id


def test_rate_change_via_revision_recalculates_from_affected_date(tmp_path):
    old, _ = run_balance(tmp_path, "oldrate.dat", [
        balance_record(ACCT1, "1000.00", "0.0500", "20260101")])
    new, _ = run_balance(tmp_path, "newrate.dat", [
        balance_record(ACCT1, "1000.00", "0.0600", "20260101")])
    manifest = write_manifest(tmp_path, [
        entry(old.run_id, "20260101", [ACCT1],
              authorized_at="2025-12-31T00:00:00Z", close_at="2026-01-10T00:00:00Z"),
        entry(new.run_id, "20260101", [ACCT1], supersedes=old.run_id,
              authorized_at="2026-01-12T00:00:00Z", close_at="2026-01-10T00:00:00Z")])
    report = build_accrual_report(
        manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
        start=date(2026, 1, 1), end=date(2026, 1, 4),
        route="restatement", as_of=datetime(2026, 1, 20, tzinfo=UTC))
    historical = account_block(report, "historical", ACCT1)
    restated = account_block(report, "restatement", ACCT1)
    assert historical["segments"][0]["rate"] == "0.0500"
    assert restated["segments"][0]["rate"] == "0.0600"
    assert historical["interest_cents"] == "0.41"   # 50*3/365
    assert restated["interest_cents"] == "0.49"     # 60*3/365
    diff = next(item for item in report["restatement_diff"]["accounts"]
                if item["account_id"] == ACCT1)
    assert diff["delta_cents"] == "0.08"


# ---------------------------------------------------------------------------
# 字节级稳定与证据载荷
# ---------------------------------------------------------------------------

def test_repeated_invocation_is_byte_stable(tmp_path):
    result, _ = run_balance(tmp_path, "stable.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105")])
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    kwargs = dict(manifest_path=manifest, db_path=tmp_path / "ledger.sqlite",
                  start=date(2026, 1, 5), end=date(2026, 1, 8), route="historical")
    first = pretty_json(build_accrual_report(**kwargs))
    second = pretty_json(build_accrual_report(**kwargs))
    assert first == second
    payload = json.loads(first)
    segment = payload["routes"]["historical"]["accounts"][0]["segments"][0]
    assert segment["source_run"] == result.run_id
    assert segment["record_no"] == 1
    assert len(segment["raw_sha256"]) == 64
    assert len(segment["rules_sha256"]) == 64


def test_cli_accrue_balances_end_to_end(tmp_path, capsys):
    result, _ = run_balance(tmp_path, "cli.dat", [
        balance_record(ACCT1, "1000.00", "0.0500", "20260228")])
    replacement, _ = run_balance(tmp_path, "cli2.dat", [
        balance_record(ACCT1, "1200.00", "0.0600", "20260228")])
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260228", [ACCT1],
              authorized_at="2026-02-27T00:00:00Z", close_at="2026-03-01T00:00:00Z"),
        entry(replacement.run_id, "20260228", [ACCT1], supersedes=result.run_id,
              authorized_at="2026-03-02T00:00:00Z", close_at="2026-03-01T00:00:00Z")])
    out_file = tmp_path / "report.json"
    code = cli_main([
        "accrue-balances", "--manifest", str(manifest), "--db", str(tmp_path / "ledger.sqlite"),
        "--start", "2026-02-28", "--end", "2026-03-05", "--route", "restatement",
        "--as-of", "2026-03-03T00:00:00Z", "--copybook", str(COPYBOOK),
        "--output", str(out_file)])
    assert code == 0 and capsys.readouterr().out == ""
    report = json.loads(out_file.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert account_block(report, "historical", ACCT1)["interest_cents"] == "0.68"
    assert account_block(report, "restatement", ACCT1)["interest_cents"] == "0.99"
    # stdout 模式与文件模式内容一致（字节稳定）。
    code = cli_main([
        "accrue-balances", "--manifest", str(manifest), "--db", str(tmp_path / "ledger.sqlite"),
        "--start", "2026-02-28", "--end", "2026-03-05", "--route", "historical"])
    assert code == 0
    stdout_report = json.loads(capsys.readouterr().out)
    assert stdout_report["routes"]["historical"] == report["routes"]["historical"]


def test_cli_rejects_bad_period_and_missing_as_of(tmp_path, capsys):
    result, _ = run_balance(tmp_path, "z.dat", [
        balance_record(ACCT1, "365.00", "0.1250", "20260105")])
    manifest = write_manifest(tmp_path, [
        entry(result.run_id, "20260105", [ACCT1],
              authorized_at="2026-01-04T00:00:00Z", close_at="2026-02-01T00:00:00Z")])
    code = cli_main([
        "accrue-balances", "--manifest", str(manifest), "--db", str(tmp_path / "ledger.sqlite"),
        "--start", "2026-01-10", "--end", "2026-01-05", "--route", "historical"])
    assert code == 2
    assert json.loads(capsys.readouterr().err)["code"] == "CLI001"
    code = cli_main([
        "accrue-balances", "--manifest", str(manifest), "--db", str(tmp_path / "ledger.sqlite"),
        "--start", "2026-01-05", "--end", "2026-01-06", "--route", "restatement"])
    assert code == 2
    assert json.loads(capsys.readouterr().err)["code"] == "CLI001"
