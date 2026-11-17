"""可恢复发布：force 重跑的故障切点、保留策略与恢复判定。

同一 run_key 的“当前可复核轮次”由台账 publications 指针决定，与单个文件写入无关。
重跑在隔离目录构建新轮；只有产物完整、摘要可回读且台账拒绝/文件问题/产物引用与
新一轮对应后，指针才切换。三个切点（产物写一半、产物齐但台账未切换、台账已切换但
旧文件未清理）分别用子进程注入故障并以 os._exit 模拟进程被终止，再重新进入验证。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import FIXTURES, NOW, ROOT, load_json
from copybook_kit.errors import LedgerError
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.pipeline.runner import resume_run, run
from copybook_kit.store.ledger import Ledger
from copybook_kit.web.app import create_app

LATER = datetime(2026, 2, 2, tzinfo=timezone.utc)
DATA = FIXTURES / "data" / "customer_rejects.dat"
COPYBOOK = FIXTURES / "copybooks" / "customer.cpy"
RULES = FIXTURES / "rules" / "customer_rules.yaml"
ARTIFACT_NAMES = ("summary.json", "layout.json", "accepted.jsonl", "rejects.jsonl",
                  "profile.json", "report.md", "report.html")

# 在子进程中注入切点故障：写完部分产物 / 切换台账指针前 / 清理旧轮前，
# 然后 os._exit(42) 模拟进程被终止（不执行任何清理或异常处理）。
CRASH_SCRIPT = """
import os
import sys
from datetime import datetime
from pathlib import Path

from copybook_kit.pipeline import runner
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.store.ledger import Ledger

cut, input_path, copybook_path, rules_path, out_root, db_path, now_text, retention, force = (
    sys.argv[1:10])

if cut == "artifacts":
    def boom(run_dir, **kwargs):
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "layout.json").write_text('{"record_length": 86', encoding="utf-8")
        (run_dir / "accepted.jsonl").write_text('{"record_no": 1', encoding="utf-8")
        os._exit(42)
    runner.write_artifacts = boom
elif cut == "publish":
    def boom(self, *args, **kwargs):
        os._exit(42)
    Ledger.publish_run = boom
elif cut == "cleanup":
    def boom(*args, **kwargs):
        if kwargs.get("retention") == "clean":  # 只在发布后的已授权清理处崩溃
            os._exit(42)
    runner._cleanup_superseded = boom

options = RunOptions(input_path=Path(input_path), copybook_path=Path(copybook_path),
                     rules_path=Path(rules_path), out_root=Path(out_root),
                     db_path=Path(db_path), now=datetime.fromisoformat(now_text),
                     force=force == "1", retention=retention)
runner.run(options)
"""

def make_options(tmp_path, **kwargs):
    base = dict(input_path=DATA, copybook_path=COPYBOOK, rules_path=RULES,
                out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite", now=NOW)
    base.update(kwargs)
    return RunOptions(**base)

def crash_run(tmp_path, cut, *, retention="keep", force=True, now=NOW):
    args = [sys.executable, "-c", CRASH_SCRIPT, cut, str(DATA), str(COPYBOOK), str(RULES),
            str(tmp_path / "runs"), str(tmp_path / "ledger.sqlite"), now.isoformat(),
            retention, "1" if force else "0"]
    completed = subprocess.run(args, capture_output=True, text=True, cwd=ROOT)
    assert completed.returncode == 42, completed.stderr

def digest(run_dir: Path) -> dict[str, str]:
    return {name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
            for name in ARTIFACT_NAMES}

def client_for(tmp_path):
    app = create_app(tmp_path / "ledger.sqlite")
    app.config.update(TESTING=True)
    return app.test_client()

def assert_visible_round(tmp_path, run_key, expected_run_id):
    """当前可见轮判定：指针、摘要、每份产物、拒绝条目与台账互相一致。"""
    ledger = Ledger(tmp_path / "ledger.sqlite")
    publication = ledger.get_publication(run_key)
    assert publication is not None and publication["run_id"] == expected_run_id
    row = ledger.get_run(expected_run_id)
    assert row["publish_state"] == "published"
    assert ledger.get_run_by_key(run_key)["run_id"] == expected_run_id
    out_dir = Path(row["out_dir"])
    for name in ARTIFACT_NAMES:
        assert (out_dir / name).is_file(), name
    summary = load_json(out_dir / "summary.json")
    assert summary["run_id"] == expected_run_id and summary["status"] == row["status"]
    assert summary["counts"]["rejected_records"] == row["rejected_records"]
    rejects = ledger.get_rejects(expected_run_id)
    assert len(rejects) == summary["counts"]["rejected_records"]
    lines = [json.loads(line) for line in
             (out_dir / "rejects.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [item["record_no"] for item in lines] == [item["record_no"] for item in rejects]
    for entry in rejects:
        assert (out_dir / entry["raw_file"]).is_file()
    return row, summary, rejects

def test_cut1_artifacts_half_written_old_round_stays_visible(tmp_path):
    first = run(make_options(tmp_path))
    run_key, dir_a = first.run_key, tmp_path / "runs" / first.run_id
    digest_a = digest(dir_a)
    crash_run(tmp_path, "artifacts")

    # 重新进入：当前仍是旧轮 A；新轮 B 只是 staged，普通 run/台账/控制台都看到 A
    ledger = Ledger(tmp_path / "ledger.sqlite")
    staged = ledger.get_run(f"{first.run_id}-r2")
    assert staged["status"] == "running" and staged["publish_state"] == "staged"
    assert digest(dir_a) == digest_a and len(ledger.get_rejects(first.run_id)) == 4
    repeat = run(make_options(tmp_path))
    assert repeat.idempotent and repeat.run_id == first.run_id
    resumed_old = resume_run(first.run_id, tmp_path / "ledger.sqlite", NOW)
    assert resumed_old.idempotent and resumed_old.run_id == first.run_id
    assert digest(dir_a) == digest_a  # 已发布轮从未被当作未完成任务删除或覆写
    client = client_for(tmp_path)
    index = client.get("/")
    assert first.run_id.encode() in index.data and staged["run_id"].encode() in index.data
    detail_a = client.get(f"/runs/{first.run_id}")
    assert detail_a.status_code == 200 and f"/runs/{first.run_id}/artifacts/".encode() in detail_a.data
    assert client.get(f"/runs/{first.run_id}/artifacts/summary").status_code == 200
    assert b"R008_UNIQUE" in client.get(f"/runs/{first.run_id}/rejects").data
    detail_b = client.get(f"/runs/{staged['run_id']}")
    assert detail_b.status_code == 200  # staged 轮可见但没有任何产物链接（无悬空引用）
    assert f"/runs/{staged['run_id']}/artifacts/".encode() not in detail_b.data
    assert client.get(f"/runs/{staged['run_id']}/artifacts/summary").status_code == 404

    # 重复 --force：继续同一个 staged 轮（不开第三轮），发布后切换到 B
    forced = run(make_options(tmp_path, force=True))
    assert forced.run_id == staged["run_id"] and not forced.idempotent
    assert ledger.get_run(f"{first.run_id}-r3") is None
    row, summary, rejects = assert_visible_round(tmp_path, run_key, forced.run_id)
    assert summary["counts"] == first.counts and len(rejects) == 4
    dir_b = Path(row["out_dir"])
    assert load_json(dir_b / "layout.json")["record_length"] == 86  # 半成品已被隔离重建
    assert digest(dir_a) == digest_a  # keep 策略：旧轮完整保留
    assert {name: digest(dir_a)[name] for name in ("accepted.jsonl", "rejects.jsonl",
            "profile.json", "layout.json")} == \
           {name: digest(dir_b)[name] for name in ("accepted.jsonl", "rejects.jsonl",
            "profile.json", "layout.json")}

def test_cut2_artifacts_complete_ledger_not_switched(tmp_path):
    first = run(make_options(tmp_path))
    crash_run(tmp_path, "publish")

    # 重新进入：产物已齐但台账未切换，当前仍是旧轮 A
    ledger = Ledger(tmp_path / "ledger.sqlite")
    staged_id = f"{first.run_id}-r2"
    staged = ledger.get_run(staged_id)
    assert staged["status"] == "completed_with_rejects" and staged["publish_state"] == "staged"
    dir_b = tmp_path / "runs" / staged_id
    summary_before = (dir_b / "summary.json").read_bytes()
    assert load_json(dir_b / "summary.json")["run_id"] == staged_id
    repeat = run(make_options(tmp_path))
    assert repeat.idempotent and repeat.run_id == first.run_id

    # resume 新轮：校验通过后直接补切换，不重新执行（摘要字节不变、generated_at 不变）
    resumed = resume_run(staged_id, tmp_path / "ledger.sqlite", LATER)
    assert resumed.idempotent and resumed.run_id == staged_id
    assert (dir_b / "summary.json").read_bytes() == summary_before
    row, summary, rejects = assert_visible_round(tmp_path, first.run_key, staged_id)
    assert summary["generated_at"] == "2026-01-01T00:00:00Z"
    assert len(rejects) == 4 and ledger.get_publication(first.run_key)["round_no"] == 2
    assert "run_published" in [event["event"] for event in ledger.get_events(staged_id)]

def test_cut3_ledger_switched_old_files_not_cleaned(tmp_path):
    first = run(make_options(tmp_path))
    dir_a = tmp_path / "runs" / first.run_id
    crash_run(tmp_path, "cleanup", retention="clean")

    # 重新进入：指针已切换，当前就是新轮 B；旧轮 A 行与目录尚未清理
    ledger = Ledger(tmp_path / "ledger.sqlite")
    staged_id = f"{first.run_id}-r2"
    publication = ledger.get_publication(first.run_key)
    assert publication["run_id"] == staged_id and publication["retention"] == "clean"
    assert ledger.get_run(first.run_id) is not None and dir_a.is_dir()

    # 再次进入即完成已授权的清理：A 从台账与磁盘消失，不留悬空引用
    repeat = run(make_options(tmp_path))
    assert repeat.idempotent and repeat.run_id == staged_id
    ledger = Ledger(tmp_path / "ledger.sqlite")
    assert ledger.get_run(first.run_id) is None and not dir_a.exists()
    assert [row["run_id"] for row in ledger.list_runs()] == [staged_id]
    assert_visible_round(tmp_path, first.run_key, staged_id)
    with pytest.raises(LedgerError) as caught:
        resume_run(first.run_id, tmp_path / "ledger.sqlite", NOW)
    assert caught.value.code == "LD002"
    client = client_for(tmp_path)
    assert client.get(f"/runs/{first.run_id}").status_code == 404
    assert client.get(f"/runs/{first.run_id}/artifacts/summary").status_code == 404
    assert f'/runs/{first.run_id}"'.encode() not in client.get("/").data  # 无悬空链接

def test_retention_keep_preserves_readable_history_chain(tmp_path):
    first = run(make_options(tmp_path))
    second = run(make_options(tmp_path, force=True, retention="keep"))
    third = run(make_options(tmp_path, force=True, retention="keep", now=LATER))
    assert (first.run_id, second.run_id, third.run_id) == (
        first.run_id, f"{first.run_id}-r2", f"{first.run_id}-r3")
    ledger = Ledger(tmp_path / "ledger.sqlite")
    states = {row["run_id"]: row["publish_state"] for row in ledger.list_runs()}
    assert states == {first.run_id: "superseded", second.run_id: "superseded",
                      third.run_id: "published"}
    assert_visible_round(tmp_path, first.run_key, third.run_id)
    # 历史轮次可回读（人工回退依据），且磁盘上三轮目录并存
    for old in (first, second):
        assert (tmp_path / "runs" / old.run_id / "summary.json").is_file()
        assert resume_run(old.run_id, tmp_path / "ledger.sqlite", NOW).counts == first.counts
    client = client_for(tmp_path)
    detail_a = client.get(f"/runs/{first.run_id}")
    assert detail_a.status_code == 200 and "历史轮次".encode() in detail_a.data
    assert third.run_id.encode() in detail_a.data  # 指向当前可复核轮次，无悬空引用
    assert client.get(f"/runs/{first.run_id}/artifacts/summary").status_code == 200
    # 源输入与 copybook 全程只读
    for path in (DATA, COPYBOOK, RULES):
        assert hashlib.sha256(path.read_bytes()).hexdigest()

def test_retention_clean_removes_old_round_after_switch(tmp_path):
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (DATA, COPYBOOK, RULES)}
    first = run(make_options(tmp_path))
    dir_a = tmp_path / "runs" / first.run_id
    forced = run(make_options(tmp_path, force=True, retention="clean"))
    assert forced.run_id == f"{first.run_id}-r2"
    ledger = Ledger(tmp_path / "ledger.sqlite")
    assert ledger.get_run(first.run_id) is None and not dir_a.exists()
    assert [row["run_id"] for row in ledger.list_runs()] == [forced.run_id]
    assert ledger.get_publication(first.run_key)["retention"] == "clean"
    assert_visible_round(tmp_path, first.run_key, forced.run_id)
    client = client_for(tmp_path)
    assert client.get(f"/runs/{first.run_id}").status_code == 404
    assert f'/runs/{first.run_id}"'.encode() not in client.get("/").data  # 无悬空链接
    # 轮次号单调：旧轮已删也不会复用其 run_id
    third = run(make_options(tmp_path, force=True, retention="clean"))
    assert third.run_id == f"{first.run_id}-r3"
    assert [row["run_id"] for row in Ledger(tmp_path / "ledger.sqlite").list_runs()] == [third.run_id]
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in before} == before  # 源输入与 copybook 只读

def test_clean_retention_keeps_lineage_parent_round(tmp_path):
    first = run(make_options(tmp_path))
    child = run(make_options(tmp_path, rules_path=FIXTURES / "rules" / "negative_rules.yaml",
                             parent_run_id=first.run_id, kind="reprocess"))
    assert child.parent_run_id == first.run_id
    forced = run(make_options(tmp_path, force=True, retention="clean"))
    ledger = Ledger(tmp_path / "ledger.sqlite")
    assert_visible_round(tmp_path, first.run_key, forced.run_id)
    # 血缘父轮不随 clean 清理：子运行的 parent_run_id 始终可解析
    parent = ledger.get_run(first.run_id)
    assert parent["publish_state"] == "superseded"
    assert (tmp_path / "runs" / first.run_id / "summary.json").is_file()
    assert ledger.get_run(child.run_id)["parent_run_id"] == first.run_id

def test_corrupted_staged_artifacts_are_rebuilt_not_published(tmp_path):
    first = run(make_options(tmp_path))
    crash_run(tmp_path, "publish")  # 产物齐、台账未切换
    dir_b = tmp_path / "runs" / f"{first.run_id}-r2"
    (dir_b / "summary.json").unlink()  # 切点之后产物被破坏：发布门控必须拒绝
    forced = run(make_options(tmp_path, force=True))
    assert forced.run_id == f"{first.run_id}-r2" and not forced.idempotent
    row, summary, _ = assert_visible_round(tmp_path, first.run_key, forced.run_id)
    assert summary["counts"] == first.counts  # 重建后完整发布，而非带缺失产物切换

def test_outdated_v1_ledger_is_rejected(tmp_path):
    import sqlite3
    db_path = tmp_path / "ledger.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE runs (run_key TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(LedgerError) as caught:
        Ledger(db_path)
    assert caught.value.code == "LD005"

def test_idempotent_reads_without_force_track_current_round(tmp_path):
    first = run(make_options(tmp_path))
    again = run(make_options(tmp_path))
    assert again.idempotent and again.run_id == first.run_id
    forced = run(make_options(tmp_path, force=True))
    moved = run(make_options(tmp_path))
    assert moved.idempotent and moved.run_id == forced.run_id != first.run_id
    summary = load_json(tmp_path / "runs" / forced.run_id / "summary.json")
    assert moved.counts == summary["counts"] and moved.outputs == summary["outputs"]

def test_first_round_crash_before_publish_recovers_without_old_round(tmp_path):
    crash_run(tmp_path, "publish", force=False)
    ledger = Ledger(tmp_path / "ledger.sqlite")
    assert ledger.list_runs()[0]["publish_state"] == "staged"
    result = run(make_options(tmp_path))  # 无旧轮：普通 run 直接完成发布判定
    assert result.status == "completed_with_rejects"
    assert_visible_round(tmp_path, result.run_key, result.run_id)

def test_first_round_half_written_requires_resume_then_publishes(tmp_path):
    crash_run(tmp_path, "artifacts", force=False)
    with pytest.raises(LedgerError) as caught:  # 无已发布轮时的旧版行为：提示 resume
        run(make_options(tmp_path))
    assert caught.value.code == "LD001"
    ledger = Ledger(tmp_path / "ledger.sqlite")
    run_id = ledger.list_runs()[0]["run_id"]
    resumed = resume_run(run_id, tmp_path / "ledger.sqlite", NOW)
    assert resumed.status == "completed_with_rejects" and not resumed.idempotent
    assert_visible_round(tmp_path, resumed.run_key, run_id)

def test_reports_and_summary_do_not_expose_temporary_paths(tmp_path):
    run(make_options(tmp_path))
    forced = run(make_options(tmp_path, force=True))
    run_dir = tmp_path / "runs" / forced.run_id
    summary = load_json(run_dir / "summary.json")
    assert all(Path(path).parent == run_dir.resolve()
               for path in summary["outputs"].values())
    for name in ("summary.json", "report.md", "report.html", "accepted.jsonl",
                 "rejects.jsonl", "profile.json", "layout.json"):
        text = (run_dir / name).read_text(encoding="utf-8")
        assert ".tmp" not in text and ".staging" not in text
    entries = [json.loads(line) for line in
               (run_dir / "rejects.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(not Path(entry["raw_file"]).is_absolute() for entry in entries)
