# 规则契约

规则文件是 UTF-8 YAML，最大 512 KiB。顶层只允许
`version, name, record_type, trailer, limits, fields, record_rules`；重复键、未知键、
浮点数字面量都会被拒绝（`RC001` / `RC003`），避免隐式精度和静默拼写错误。
`fields` 中的键必须是布局里存在的 elementary 字段名，否则 `RC004`。

## fields

```yaml
fields:
  CUSTOMER-ID:
    required: true              # 空值 -> R001_REQUIRED
    enum: ["A", "I"]            # -> R002_ENUM
    range: {min: "1", max: "9"} # 数值或日期区间 -> R003_RANGE
    date_format: "%Y%m%d"       # -> R004_DATE
    pattern: "^CUST[0-9]{6}$"   # 整串匹配 -> R005_PATTERN
    check_digit: {weights: [2,3,4,5,6], modulus: 11}  # -> R006_CHECK_DIGIT
    scale: 2                    # 实际 Decimal 小数位超过 2 -> R007_SCALE
    blank: null                 # null | zero | error（空值策略，默认 null）
```

检查顺序固定为：required → 日期格式 → enum → range → pattern → check digit → scale；
空值仅执行 required/blank 语义，避免重复报错。日期区间边界既可用同一 `date_format`，
也可用 ISO `YYYY-MM-DD`。正则最大 200 字符，禁止反向引用、环视与嵌套量词（`RC002`）。
校验和权重为 1–9 的整数，模数 2–97，值长度必须等于权重数 + 1。

## record_rules

```yaml
record_rules:
  - name: customer-id-unique
    kind: unique_key
    fields: [CUSTOMER-ID]       # 复合键按字段顺序拼接；重复/缺失 -> R008_UNIQUE
  - name: renewal-not-before-open
    kind: cross_field
    left: OPEN-DATE
    op: "<="                    # <= | < | == | !=
    right: RENEW-DATE           # 失败 -> R009_CROSS_FIELD
```

唯一键状态只在整条记录没有其他命中时提交；一条记录同时触发其他规则时，不会污染后续
文件的唯一性判断。比较两侧能解析为 `Decimal` 时按数值比较，否则按字符串比较。

### 唯一键冲突裁决（两种业务合同）

默认合同 `sequential`（历史语义）：

- 按文件顺序，先通过解码、字段与 cross_field 检查的记录先占用其全部唯一键；
- 之后任意一条 `unique_key` 同键的记录以 `R008_UNIQUE` 拒绝；
- 记录集、拒绝理由与画像完全沿用旧行为，运行键也不含裁决参数。

显式授权的合同 `prefer_combination`（需同时给出质量分字段）：

```bash
copybook-kit run ... --unique-policy prefer_combination --unique-score-field SCORE
```

- 只把通过解码、字段与 cross_field 检查的数据记录纳入冲突图；字段失败的记录不参与，
  也不会污染其他记录。
- 每条 `unique_key` 用完整规范键（字段顺序 + 规范文本）建立边；任意一条 `unique_key`
  同键即互斥，两个键上的冲突共同连成连通分量。
- 在每个连通分量内求“兼容集合”的 SCORE 总和最大者。SCORE 取 copybook 解码后的
  `Decimal`/整数；缺失或非法（文本、NaN/Inf）的受争记录以 `R011_UNIQUE_SELECTION`
  自身拒绝，未知分数绝不按 0 计。
- 并列总分按记录号序列字典序取最小者，确定性可复现。
- 每个未选行输出冲突键、与哪些胜出行冲突（及具体键）、该分量所选集与总分、以及
  分支定界搜索节点数/剪枝数/上界等证据。
- 分量超过 `--unique-max-component` 或搜索超过 `--unique-proof-budget` 时状态为
  `undetermined`：不发布任何胜者，该分量中受争记录全部以 `R011_UNIQUE_SELECTION`
  拒绝；此时文件级结论以 `FL006` 显式标注为“未定”，不沿用裁剪前的数量/尾账结论。
- 证明完成后，文件级 `FL003`/`FL004`/trailer 仍按物理数据记录（`file_data_records`）
  解释，因为 tailer 声明的是文件内容而不是接收子集；`detail.basis` 会写明依据。

`sequential` 与 `prefer_combination` 写入不同的 `run_key`：裁决模式、SCORE 字段名、
分量上限与证明预算都计入运行身份，同一输入在不同合同下不会复用运行。


## record_type / trailer / limits

```yaml
record_type: {field: REC-TYPE, data: D, header: H, trailer: T}
trailer: {count_field: OPEN-DATE, amount_field: BALANCE}
limits: {min_records: 1, max_records: 100}
```

- 未声明的记录类型 -> `R010_RECORD_TYPE`；header/trailer 不参与业务字段校验。
- 首条不是 header、中间出现 header/trailer、末条不是 trailer -> `FL005`。
- trailer 缺失或数量不符 -> `FL001`；金额合计不符 -> `FL002`。
- 数据记录数低于 `min_records` -> `FL003`，高于 `max_records` -> `FL004`。

## 码表

| 类别 | 码 |
| --- | --- |
| 字段规则 | `R001_REQUIRED` `R002_ENUM` `R003_RANGE` `R004_DATE` `R005_PATTERN` `R006_CHECK_DIGIT` `R007_SCALE` |
| 记录规则 | `R008_UNIQUE` `R009_CROSS_FIELD` `R010_RECORD_TYPE` `R011_UNIQUE_SELECTION` |
| 文件规则 | `FL001` 数量/缺失、`FL002` 金额、`FL003` 下限、`FL004` 上限、`FL005` 位置、`FL006` 裁决未定 |
| 规则契约 | `RC001` schema、`RC002` 正则、`RC003` 值类型/范围、`RC004` 未知字段 |
| 读取/解码 | `RD001` 字符集、`RD002` 截断、`RD003` 空文件、`RD004` 记录上限；`DE001` DISPLAY、`DE002` COMP-3、`DE003` 空值策略 |
| 台账/IO/CLI | `LD001` 未完成同键运行、`LD002` 运行不存在、`LD003` 键冲突；`IO001`；`CLI001` |
