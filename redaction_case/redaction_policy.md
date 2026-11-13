# 退件复现脱敏策略（订单记录 ORD-104282）

## 场景与结论

- 原校验入口：`order_validation.py` 的 `validate_order()`（基于本仓库 `schema` 库）。
- 原始退件记录：`original_record.json`，含客户值，**仅内部留存，不外发**。
- 失败规则：**R2 数量小计**（`sum(items[].quantity) != summary.total_quantity`，3+2=5 ≠ 6）。
- 可分享替身：`sanitized_record.json`，已经原校验入口实跑，失败路径与原记录**逐字相同**（见 `verify_reproduction.py`，退出码 0）。

## 必须保留的公开字段（逐值保留，不得改动）

| 字段 | 保留理由 |
|---|---|
| `order_id` | 退件单号，供应商定位问题所需，非客户数据 |
| `currency` | 公开枚举值，非敏感 |
| `items[].sku` / `items[].unit_price` | 公开目录数据，非敏感 |
| `items[].quantity` | **R2 失败规则的输入**，改动即改变失败路径 |
| `summary.total_quantity` | **R2 失败规则的输入**，必须保留 5≠6 的差值 |
| `summary.total_amount` | 与数量小计一致的公开上下文，非敏感 |

## 允许替换的字段与替代值域

| 字段 | 替代值域（公开约束） | 联动要求 | 本例替代值 |
|---|---|---|---|
| `customer.name` | 任意非空字符串（`And(str, len)`） | 无 | `REDACTED-CUSTOMER` |
| `customer.email` | 匹配 `^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$` 的任意字符串 | **必须与 `confirm_email` 替换为同一值**（R1 跨字段相等） | `redacted@example.com` |
| `confirm_email` | 同上邮箱正则 | **必须与 `customer.email` 相等** | `redacted@example.com` |
| `customer.tax_id` | 匹配 `^[A-Z0-9]{10,18}$` 的任意字符串 | 无 | `XX00000000` |

替换原则：替代值必须落在各字段的公开值域内，且保持所有跨字段关系的原判定结果（R1 相等关系继续成立），使校验按原顺序走到 R2 才失败。

## 不可行替身：卡在哪组公开约束

- **逐格打星（`***`）不可行**：`customer.email` / `confirm_email` 受公开邮箱正则约束，`***` 在**字段级格式校验**阶段即被拒（实测报错 `Key 'confirm_email' error: '***' does not match ...`），校验根本走不到 R2，失败路径改变。`customer.tax_id` 同理受 `^[A-Z0-9]{10,18}$` 约束。
- **只替换邮箱的一侧不可行**：R1 跨字段相等（`customer.email == confirm_email`）会先失败，记录停在 R1 而非 R2。
- **改动数量或小计不可行**：`items[].quantity` 与 `summary.total_quantity` 是目标失败规则 R2 本身的输入，改动后失败消失或差值改变，供应商无法复现同一退件。

## 交付清单

- `sanitized_record.json` — 可外发替身记录（唯一需要交给供应商的数据文件）
- `redaction_policy.md` — 本策略
- `verify_reproduction.py` — 复现证明：用原校验入口实跑 original / sanitized / naive 三条记录并比对失败路径
- `order_validation.py` — 校验入口（如供应商需本地复跑可一并提供，不含任何客户值）
