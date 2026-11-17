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

### cross_field 与 OCCURS 对齐

定长合同里一个 `OCCURS` 组常表示同一行的多个分期。旧契约只用叶子名取第一个同名值，
在重复组上会静默漏掉失败分期。现在 `cross_field` 按编译后的 Layout 把两侧叶子绑定到
明确的 OCCURS 坐标，比较只在同坐标之间发生，绝不按扁平列表的碰巧顺序 zip。

- 两侧都是 OCCURS 外的单值叶子时，保持旧的单值规则结论（缺值放行，无需量词）。
- 任一侧位于 OCCURS 祖先下时，必须声明 `quantifier`，并在有歧义时给出显式路径：

```yaml
record_rules:
  - name: installment-limit
    kind: cross_field
    left: LOW                   # 叶子名；同一 OCCURS 祖先下可直接按索引对齐
    op: "<="
    right: HIGH
    quantifier: all_aligned     # all_aligned | any_aligned
    # 当同名叶子存在多个物理路径（不同 OCCURS 祖先 / REDEFINES）时必须显式消歧：
    left_path: "PAIR[*].LOW"    # [*] 匹配该轴全部索引；PAIR[0].LOW 只钉住第 0 期
    right_path: "PAIR[*].HIGH"
```

对齐与广播规则：

- **同祖先按索引配对**：`PAIR[0].LOW` 只与 `PAIR[0].HIGH` 比较；两层 OCCURS 按完整
  坐标元组对齐，外层 0 的内层 1 永远不会配到外层 1 的内层 0。
- **标量广播**：一侧是该 OCCURS 祖先之外的标量（或只覆盖外层轴）时，同一个值广播到
  每个对应索引，例如 `CAP <= V` 中的 `CAP` 会与同组的每个 `V` 比较。
- **装载期消歧**：两侧来自互不相同的重复祖先、同名叶子有多个物理/REDEFINES 路径、
  或路径投影后出现一对多歧义时，`load_rules` 在运行前抛出稳定的 `RC003`/`RC004`
  规则错误并列出冲突路径；不会取第一个值，也不会按扁平顺序 zip。
- **完整矩形**：带 `[*]` 的投影必须覆盖该轴的完整 OCCURS 矩形；只钉单侧外轴会被
  `RC003` 拒绝，钉住两侧同一外轴的子矩形是合法的。

量词政策（两者都是完整、明确授权的业务政策）：

- `all_aligned`：每个**有定义**的对应分期都必须满足关系；任一已定义分期失败即拒绝，
  见证坐标指向第一个失败分期；存在缺值但已定义分期全部满足时同样拒绝并报告缺值坐标。
- `any_aligned`：至少一个有定义的对应分期满足关系即接受，见证坐标指向第一个满足分期；
  没有任何有定义分期时拒绝（不使用 Python `any([])` 的默认真值）。
- 缺失值或解码失败（如 COMP-3 的 `DE002`）不算一次成功比较：该坐标进入 `missing`
  列表，注明 `missing_side`、解码错误码、两侧字段路径与字节偏移，两个量词都不会由
  空列表的 `all`/`any` 默认值决定记录是否可接收。

R009 拒绝证据（写入 `rejects.jsonl`，供 reprocess 与只读复核台一致读取）在 `detail`
中保留：规则名、量词、全部参与坐标、每个比较的正式解码字段路径、**文件绝对字节偏移**、
原始字节证据哈希（`record.evidence` 的 sha8）与布尔结论，以及 `witness` 见证位置；
记录级 R009 只使用本行数据，不写入 `unique_key` 的跨行状态。

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
