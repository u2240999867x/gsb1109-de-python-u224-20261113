# 采集器分配规划（allocation）

在升级部分采集器之前，根据各采集器**当前声明**的 NpmSpec、并发容量和可读
来源清单，计算最多还能覆盖多少来源，并给出实际可用的分配与不可避免的停采
清单。

## 输入契约（JSON）

```json
{
  "sources": [
    {"id": "src-a", "contract_version": "1.4.0"},
    {"id": "src-d", "contract_version": "2.1.0-rc.3", "locked_collector": "col-beta"}
  ],
  "collectors": [
    {
      "id": "col-beta",
      "npm_spec": ">=2.1.0-rc.1 <3.0.0",
      "capacity": 2,
      "can_read": ["src-d"]
    }
  ]
}
```

- `sources[].contract_version`：来源上报的数据合同版本，保留预发布标识
  （如 `2.1.0-rc.3`）。
- `sources[].locked_collector`：可选；锁定分配，规划时不可移动。
- `collectors[].npm_spec`：采集器声明的 npm 风格兼容范围。
- `collectors[].capacity`：并发容量，即最多同时采集的来源数。
- `collectors[].can_read`：采集器能够读取的来源 ID 清单。

## 判定规则

- 一个来源能分给某采集器，当且仅当来源同时满足：在该采集器 `can_read`
  清单中，且合同版本被其 `NpmSpec` 接受。
- 每个来源最多分配给一个采集器；采集器负载不得超过 `capacity`。
- 锁定来源固定在原采集器上，并预先占用其容量；锁定本身不兼容（采集器不
  存在、不在可读清单、NpmSpec 不匹配或容量被锁定来源撑爆）时直接报
  `InvalidLockError`，不会静默破坏锁。
- 预发布合同严格按 npm 语义处理：不带预发布边界的范围（如 `^2.0.0`）
  **不会**匹配 `2.1.0-rc.3`；只有显式预发布范围（如
  `>=2.1.0-rc.1 <3.0.0`）才能匹配，且预发布比较器只接受同一
  `major.minor.patch` 元组下的预发布版本。因此预发布合同不会被先当作
  正式版试配。

## 算法

先固定全部锁定分配并扣除容量，再对剩余来源构建
`源 → 采集器（容量边）→ 汇` 的网络，用 Dinic 最大流求最大覆盖；最大流的
流量值就是最多可覆盖来源数，未获流量的来源进入停采清单，并区分原因：

- 无任何兼容采集器（含预发布合同无人声明对应范围）；
- 兼容采集器全部达到容量上限。

## 用法

```bash
# JSON 输出
python -m allocation examples/upgrade_plan.json

# 可读报告
python -m allocation examples/upgrade_plan.json --pretty

# 标准输入
cat plan.json | python -m allocation
```

编程接口：

```python
from allocation import load_json, plan_allocation

sources, collectors = load_json(document_text)
plan = plan_allocation(sources, collectors)
print(plan.covered_count, plan.stopped_count)
```
