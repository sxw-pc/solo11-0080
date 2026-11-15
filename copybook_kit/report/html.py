"""Self-contained HTML report with no external assets or scripts."""

from __future__ import annotations

from html import escape
from typing import Any

CSS = """
:root { --ink:#17212b; --muted:#607080; --line:#dce3ea; --paper:#f7f9fb; --accent:#0f6b5d; --bad:#9b2c2c; }
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink);
  font:15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1040px; margin:0 auto; padding:32px 20px 56px; }
h1 { font-size:27px; margin:0 0 6px; letter-spacing:-.02em; }
h2 { font-size:18px; margin:32px 0 10px; }
p.lede { color:var(--muted); margin:0 0 22px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }
.card { background:white; border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.card .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }
.card .value { font-size:22px; font-weight:650; margin-top:4px; }
table { width:100%; border-collapse:collapse; background:white; border:1px solid var(--line); }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { background:#eef3f6; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
tr:last-child td { border-bottom:0; }
code, .mono { font-family:ui-monospace, SFMono-Regular, Consolas, monospace; font-size:13px; }
.hash { word-break:break-all; color:var(--muted); }
.ok { color:var(--accent); font-weight:650; }
.bad { color:var(--bad); font-weight:650; }
.empty { color:var(--muted); font-style:italic; }
footer { color:var(--muted); margin-top:32px; font-size:12px; }
"""

def _e(value: Any) -> str:
    return escape("" if value is None else str(value))

def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(item)}</td>" for item in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

def render_html(summary: dict[str, Any], profile: dict[str, Any] | None) -> str:
    counts = summary.get("counts", {})
    input_meta = summary.get("input", {})
    contracts = summary.get("contracts", {})
    status = str(summary.get("status", ""))
    status_class = "ok" if status == "completed" else "bad"
    cards = [
        ("接收记录", counts.get("accepted_records", 0)),
        ("拒绝记录", counts.get("rejected_records", 0)),
        ("业务记录", counts.get("data_records", 0)),
        ("文件级问题", counts.get("file_issues", 0)),
    ]
    card_html = "".join(
        f'<div class="card"><div class="label">{_e(label)}</div>'
        f'<div class="value">{_e(value)}</div></div>'
        for label, value in cards
    )
    issues = summary.get("file_issues", [])
    issue_rows = [[item.get("code"), item.get("severity"), item.get("byte_offset", ""),
                   item.get("record_no", ""), item.get("message")] for item in issues]
    issue_html = (_table(["错误码", "级别", "字节偏移", "记录号", "说明"], issue_rows)
                  if issues else '<p class="empty">无文件级问题。</p>')
    reject_codes = summary.get("reject_codes", {})
    reject_html = (_table(["错误码", "次数"], [[code, count] for code, count in reject_codes.items()])
                   if reject_codes else '<p class="empty">无拒绝记录。</p>')
    fields = (profile or {}).get("fields", [])
    field_rows = [[field.get("path"), field.get("field_type"), field.get("present_count"),
                   field.get("null_count"), field.get("distinct_count"), field.get("minimum", ""),
                   field.get("maximum", "")] for field in fields[:100]]
    profile_html = (_table(["字段", "类型", "非空", "空值", "基数", "最小", "最大"], field_rows)
                    if fields else '<p class="empty">无接收业务记录，画像为空。</p>')
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>定长契勘台运行报告 {_e(summary.get('run_id', ''))}</title>
<style>{CSS}</style>
</head>
<body>
<main>
<h1>定长契勘台运行报告</h1>
<p class="lede">声明式定长布局 · 字段解码 · 规则核验 · 可复现台账</p>
<p>状态 <span class="{status_class}">{_e(status)}</span>
 · 运行编号 <code>{_e(summary.get('run_id', ''))}</code>
 · 生成时间 {_e(summary.get('generated_at', ''))}</p>
<div class="grid">{card_html}</div>
<h2>输入与契约</h2>
{_table(["项目", "值"], [
    ["输入文件", input_meta.get("name", "")],
    ["输入 SHA-256", input_meta.get("sha256", "")],
    ["输入字节数", input_meta.get("size", "")],
    ["记录长度", input_meta.get("record_length", "")],
    ["字符集 / RECFM", f"{input_meta.get('charset', '')} / {input_meta.get('recfm', '')}"],
    ["copybook", (contracts.get("copybook") or {}).get("name", "")],
    ["copybook SHA-256", (contracts.get("copybook") or {}).get("sha256", "")],
    ["rules", (contracts.get("rules") or {}).get("name", "")],
    ["rules SHA-256", (contracts.get("rules") or {}).get("sha256", "")],
    ["父运行", summary.get("parent_run_id") or "-"],
])}
<h2>文件级问题</h2>
{issue_html}
<h2>拒绝码分布</h2>
{reject_html}
<h2>字段画像</h2>
{profile_html}
<footer>运行键 <span class="mono hash">{_e(summary.get('run_key', ''))}</span><br>
本页面由 copybook-kit 生成，样式与内容均内嵌，不包含外部资源。</footer>
</main>
</body>
</html>
"""
