# 运行产物、台账与复现

## 单次运行目录

`<out>/<run-id>/` 下包含：

| 文件 | 内容 |
| --- | --- |
| `summary.json` | 运行摘要：状态、计数、指纹、文件问题、拒绝码 |
| `layout.json` | 编译后的字段树、offset/length/PIC/usage/scale/occurs/redefines |
| `accepted.jsonl` | 每条接收记录一行，含字节偏移、`raw_sha256`、解码值、证据 |
| `rejects.jsonl` | 每条拒绝记录一行，含绝对 `byte_offset`、错误/规则命中、原字节文件 |
| `rejects/record-NNNNNNNN.bin` | 拒绝记录的原始字节；截断尾部另存 `truncated-tail.bin` |
| `profile.json` | 接收的数据记录字段画像（计数、空值、唯一值、极值） |
| `report.md` | 确定性中文摘要报告 |
| `report.html` | 自包含 HTML，无外部脚本、字体或 CDN |

所有文件以原子方式写入（临时文件 + fsync + replace）。JSON 使用排序键、紧凑分隔符，
Decimal 输出为字符串；重复运行同一输入时内容逐字节一致。

## summary.json

关键字段：`schema_version`, `run_id`, `run_key`, `status`, `generated_at`, `input`,
`contracts`, `counts`, `file_issues`, `reject_codes`, `outputs`, `parent_run_id`, `exit_code`。

- `status`：`completed` / `completed_with_rejects` / `failed`。
- `counts`：`total_records`、`data_records`、`accepted_records`、`accepted_data_records`、
  `rejected_records`、`file_issues`。
- `reject_codes`：按码排序的计数，例如 `{"R008_UNIQUE": 1}`。
- 退出码：`0` 正常完成（含隔离拒绝），`3` 文件级失败，`2` CLI/契约/IO 错误。

## 幂等键与台账

`run_key = SHA-256(canonical_json(params))`，params 包含输入、copybook、规则三份内容的
SHA-256，以及字符集、RECFM、记录上限、父运行 ID、运行类型。`run_id = "run-" + run_key[:20]`。
同一组合再次提交会返回既有 `summary.json`，结果标记 `idempotent=true`；`--force` 才会删除旧运行重做。
若运行尚未完成，`run` 报 `LD001` 并提示使用 `resume`。

SQLite 表：

- `runs`：指纹、路径、计数、状态、输出路径、父运行、备注。
- `rejects`：记录号、字节偏移、原始字节文件、错误/命中 JSON。
- `file_issues`：文件级问题及偏移。
- `checkpoints`：每 25 条记录的进度与唯一键快照，用于审计和恢复。
- `run_events`：`run_started`、`run_resumed`、`run_finished` 等事件。

`resume` 会重新校验输入指纹；输入或规则已变化时报 `LD003`。恢复会重置该运行的检查点、
拒绝和问题行，再完整重放，避免部分状态混入最终结果。`reprocess` 新建子运行，继承父运行的
输入与 copybook，换用新规则，并记录 `parent_run_id` 形成血缘。

## 只读控制台

`copybook-kit serve --port 5000` 固定在 `127.0.0.1`，不加载任何外部资源。路由：

- `/`：运行列表、状态、计数、父子关系。
- `/runs/<run-id>`：摘要、文件问题、拒绝、画像、事件。
- `/runs/<run-id>/rejects`：拒绝明细。
- `/runs/<run-id>/artifacts/<name>`：`summary`、`html`、`layout`、`profile`、`accepted`、`rejects`。
- `/runs/<run-id>/rejects/<n>/raw`：下载原始拒绝字节，路径限制在运行目录内。

控制台只读，没有写接口；所有展示内容经 HTML 转义。
