"""Deterministic, self-contained and escaped Markdown/HTML reports."""

from __future__ import annotations

import json

from copybook_kit.report.html import render_html
from copybook_kit.report.markdown import render_markdown

def summary():
    return {
        "run_id": "run-demo", "run_key": "a" * 64, "status": "completed_with_rejects",
        "generated_at": "2026-01-01T00:00:00Z", "parent_run_id": None,
        "input": {"name": "<script>alert(1)</script>", "sha256": "b" * 64, "size": 1},
        "contracts": {"copybook": {"name": "x.cpy"}, "rules": {"name": "x.yaml"}},
        "counts": {"accepted_records": 1, "rejected_records": 1},
        "file_issues": [{"code": "RD002", "message": "<img src=x onerror=alert(1)>", "byte_offset": 0}],
        "reject_codes": {"R002_ENUM|bad": 1}, "outputs": {},
    }

def test_html_is_self_contained_and_escaped():
    html = render_html(summary(), None)
    assert "http://" not in html and "https://" not in html
    assert "<script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<style>" in html and "@import" not in html and "不包含外部资源" in html

def test_html_and_markdown_are_byte_deterministic():
    assert render_html(summary(), None) == render_html(summary(), None)
    markdown = render_markdown(summary(), None)
    assert markdown == render_markdown(summary(), None)
    assert "R002_ENUM\\|bad" in markdown and "| RD002 |" in markdown
    assert "文件级问题" in markdown and "拒绝码分布" in markdown

def test_artifact_jsonl_is_canonical(run_fixture, tmp_path):
    result = run_fixture()
    run_dir = tmp_path / "runs" / result.run_id
    first = (run_dir / "accepted.jsonl").read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(first)
    assert first == json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert list(payload) == sorted(payload)
    layout = json.loads((run_dir / "layout.json").read_text(encoding="utf-8"))
    assert layout["record_length"] == 86 and layout["errors"] == []
