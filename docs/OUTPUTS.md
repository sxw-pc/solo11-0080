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

## 离线拒绝复核抽样（plan-reject-audit）

`copybook-kit plan-reject-audit --request <plan.json>` 从一个已完成 run
（`completed` / `completed_with_rejects`）的拒绝台账生成只读人工复核样本清单，
不写回台账、run 目录或任何拒绝字节。请求字段：

| 字段 | 含义 |
| --- | --- |
| `run_id` | 已完成 run 的 ID |
| `quotas` | 每个错误码需要的**独立记录数**，如 `{"R002_ENUM": 2}` |
| `budget` | 总审阅记录上限（正整数） |
| `exclusion_groups` | 可选，`record_no` 列表的列表；同组至多入选一条（不可同时披露） |
| `contract` | `exact`（默认）或 `bounded` |
| `node_budget` | 可选搜索节点预算，覆盖合同默认值 |

一条拒绝可凭多个 `codes` 同时满足多个配额，但同一码下一条记录只计一次。计划只引用
台账真实字段 `reject_no` / `record_no` / `byte_offset` / `raw_sha256` / `raw_file`；
生成前对每个 `rejects/record-*.bin` 重新计算 SHA-256 并核对长度，与台账不符报 `AU001`，
`partial` 解码值永不参与证据重建。`FL*` 文件级问题没有拒绝行，标记为
`file_level_not_sampleable`，不会被当成可抽样码。

- `exact`：候选数与预算各 ≤ 24 时启用，分支限界穷举，`status="optimal"` 并附
  `optimality_proof`（最小记录数、可采纳下界、搜索节点）；不可行时
  `status="infeasible"` 并附 `infeasibility_certificate`（候选不足 `insufficient_candidates`、
  预算下界 `budget_lower_bound` 或穷尽无可行选择 `no_satisfiable_selection`）。
- `bounded`：固定节点预算内寻找任意可行集合，`status="feasible"` 但不含最优证明；
  预算耗尽且无结论时 `status="undecided"`，附 `undecided_certificate`（已证实覆盖与
  未满足配额），区别于已证明的 `infeasible`。

输出还包含输入/规则指纹、台账码集合与计数、每个配额的 `covered_by` 与状态、证据校验方法。
选择与平局按稳定的 `(record_no, raw_sha256)` 排序，重复执行逐字节稳定。
