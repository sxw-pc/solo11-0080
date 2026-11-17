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
| 记录规则 | `R008_UNIQUE` `R009_CROSS_FIELD` `R010_RECORD_TYPE` |
| 文件规则 | `FL001` 数量/缺失、`FL002` 金额、`FL003` 下限、`FL004` 上限、`FL005` 位置 |
| 规则契约 | `RC001` schema、`RC002` 正则、`RC003` 值类型/范围、`RC004` 未知字段 |
| 读取/解码 | `RD001` 字符集、`RD002` 截断、`RD003` 空文件、`RD004` 记录上限；`DE001` DISPLAY、`DE002` COMP-3、`DE003` 空值策略 |
| 台账/IO/CLI | `LD001` 未完成同键运行、`LD002` 运行不存在、`LD003` 键冲突；`IO001`；`CLI001` |

## certify-rules 离线可满足性

`copybook-kit certify-rules --copybook ... --rules ... --mode proof|witness` 不读数据文件、
不打开台账、不写 run 产物，只判断**一条 data 记录**是否可满足契约，结论只有三种：

- `SAT`：报告内 witness 是按布局编码、长度恰为 `record_length` 的原始字节；必须通过正式
  `decode_record`（零解码错误）与 `RuleEngine.validate_record`（零命中）复核，缺失字段的
  blank 策略与不可编码的字符/数值都不能被当作 SAT。
- `UNSAT`：`contradiction_chain` 的每一步都来自可编码字段域（PIC/USAGE/字段宽度）、
  受支持规则子句（enum、数值/日期 range、date_format、required、scale、check_digit）或
  `<=`/`<` 跨字段关系；证明模式专用，禁止用随机样本未命中宣布无解。
- `UNKNOWN`：开放 domain 上的任意 pattern、REDEFINES 重叠分支、OCCURS 多值引用、
  `==`/`!=` 关系等放弃推理的约束（见 `coverage.abandoned`），或 witness 模式预算耗尽
  （`unknown_reasons=search_budget_exhausted`）。已构成独立矛盾时仍报 UNSAT。

witness 模式只负责找测试数据，任何情况下都不报 UNSAT；proof 模式负责契约审查，
预算参数对其无效。报告固定含 copybook/规则 SHA-256、`coverage.{reasoned,abandoned,
out_of_scope}`、witness 的 `raw_hex/raw_sha256/fields(offset,length,evidence_sha8)` 或
矛盾链来源路径，同输入序列化字节稳定。退出码：SAT=0、UNSAT=1、UNKNOWN=2。
