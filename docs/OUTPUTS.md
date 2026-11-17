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

## 幂等键、发布与台账

`run_key = SHA-256(canonical_json(params))`，params 包含输入、copybook、规则三份内容的
SHA-256，以及字符集、RECFM、记录上限、父运行 ID、运行类型。首轮 `run_id = "run-" + run_key[:20]`；
同一 run_key 第 N 次 `--force` 重跑使用 `run-…-rN`（轮次号单调递增，不复用），每轮在
`<out>/<run-id>/` 独立构建，不覆写旧轮路径。

**当前可复核轮次**由 `publications` 表指针（run_key → run_id）唯一决定，与单个文件写入
无关。重跑流程：

1. 新轮以 `staged` 状态登记并在隔离目录构建。此间普通 `run`、台账查询与只读控制台仍
   返回上一轮（`published`）的完整结果：summary、accepted、rejects、profile、报告与
   对应拒绝记录。
2. 新轮全部产物写完、摘要可回读，且台账中的拒绝、文件问题与产物引用均与新一轮一致
   （发布门控，失败码 `LD004`）后，`publications` 指针在单条 SQLite 语句中原子切换。
3. 指针切换后按发布时授权的策略处理旧轮：`--retention keep`（默认）把旧轮保留为可回读
   历史（`superseded`），便于人工回退；`--retention clean` 删除旧轮台账行与目录以控制
   磁盘（有子运行的血缘父轮除外）。清理先删台账行再删目录，不留悬空引用；清理授权记录
   在 `publications.retention`，进程在切换后、清理前崩溃时，再次进入会先完成已授权的
   待清理工作。

恢复判定：再次进入时，`publications` 指向的轮次即当前轮；`staged` 轮未完成则重置重放，
已终止但未发布则通过发布门控后直接补切换。重复 `--force` 与 `resume --run-id` 都只继续
未完成的 staged 轮，绝不把已发布轮当作未完成任务删除。同一组合再次提交（无 `--force`）
返回当前发布轮的 `summary.json`，结果标记 `idempotent=true`。

SQLite 表（schema version 2；v1 台账拒绝打开，报 `LD005`）：

- `runs`：每轮一行（`run_id` 主键），含 `round_no`、`retention`、指纹、路径、计数、状态、
  输出路径、父运行、备注。
- `publications`：run_key → 当前可复核 `run_id`、轮次、保留策略、发布时间。
- `rejects`：记录号、字节偏移、原始字节文件、错误/命中 JSON。
- `file_issues`：文件级问题及偏移。
- `checkpoints`：每 25 条记录的进度与唯一键快照，用于审计和恢复。
- `run_events`：`run_started`、`run_resumed`、`run_finished`、`run_published` 等事件。

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
