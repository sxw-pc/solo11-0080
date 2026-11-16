"""Deterministic Markdown report renderer."""

from __future__ import annotations

from typing import Any

def _cell(value: Any) -> str:
    return ("" if value is None else str(value)).replace("|", "\\|").replace("\n", " ")

def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    return (["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"] +
            ["| " + " | ".join(_cell(item) for item in row) + " |" for row in rows])

def _top_value(field: dict[str, Any]) -> str:
    return ", ".join(f"{item['value']}×{item['count']}" for item in (field.get("top_values") or [])[:3])

def render_markdown(summary: dict[str, Any], profile: dict[str, Any] | None) -> str:
    counts, input_meta = summary.get("counts", {}), summary.get("input", {})
    contracts = summary.get("contracts", {})
    copybook, rules = contracts.get("copybook") or {}, contracts.get("rules") or {}
    lines = [
        "# 定长契勘台运行报告", "",
        f"- 运行状态：`{summary.get('status', '')}`",
        f"- 运行编号：`{summary.get('run_id', '')}`",
        f"- 运行键：`{summary.get('run_key', '')}`",
        f"- 生成时间（注入）：{summary.get('generated_at', '')}",
        f"- 父运行：`{summary.get('parent_run_id') or '-'}`", "", "## 输入与契约", "",
    ]
    lines.extend(_table(["项目", "值"], [
        ["输入文件", input_meta.get("name", "")], ["输入 SHA-256", input_meta.get("sha256", "")],
        ["输入字节数", input_meta.get("size", "")], ["记录长度", input_meta.get("record_length", "")],
        ["字符集", input_meta.get("charset", "")], ["RECFM", input_meta.get("recfm", "")],
        ["copybook", copybook.get("name", "")], ["copybook SHA-256", copybook.get("sha256", "")],
        ["rules", rules.get("name", "")], ["rules SHA-256", rules.get("sha256", "")]]))
    lines.extend(["", "## 运行摘要", ""])
    labels = [("total_records", "读取记录"), ("data_records", "业务记录"),
              ("accepted_records", "接收记录"), ("accepted_data_records", "接收业务记录"),
              ("rejected_records", "拒绝记录"), ("file_issues", "文件级问题")]
    lines.extend(_table(["指标", "数量"], [[label, counts.get(key, 0)] for key, label in labels]))
    lines.extend(["", "## 文件级问题", ""])
    issues = summary.get("file_issues", [])
    if issues:
        lines.extend(_table(["错误码", "字节偏移", "记录号", "说明"], [
            [item.get("code"), item.get("byte_offset", ""), item.get("record_no", ""), item.get("message")]
            for item in issues]))
    else:
        lines.append("无。")
    lines.extend(["", "## 拒绝码分布", ""])
    reject_codes = summary.get("reject_codes", {})
    lines.extend(_table(["错误码", "次数"], [[code, count] for code, count in reject_codes.items()])
                 if reject_codes else ["无拒绝记录。"])
    lines.extend(["", "## 字段画像（前 20 项）", ""])
    fields = (profile or {}).get("fields", [])
    if fields:
        rows = [[field.get("path"), field.get("field_type"), field.get("present_count"),
                 field.get("null_count"), field.get("distinct_count"), field.get("minimum", ""),
                 field.get("maximum", ""), _top_value(field)] for field in fields[:20]]
        lines.extend(_table(["字段", "类型", "非空", "空值", "基数", "最小", "最大", "高频值"], rows))
    else:
        lines.append("无接收业务记录，画像为空。")
    lines.extend(["", "## 复现", "",
                  "使用相同输入、copybook、规则与参数重新运行时，运行键保持不变。",
                  "报告时间均来自 `--now` 注入，JSON 使用排序键与规范化 Decimal 文本。", ""])
    return "\n".join(lines)
