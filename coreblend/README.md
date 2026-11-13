# coreblend — 整筒浓度反演分层浓度

实验室只给每筒岩芯的整筒浓度，而取芯筒次与 LAS 分层界面并不对齐。本工具按
**质量平衡**（每筒实收岩芯中各层所占长度 × 该层浓度 = 整筒浓度）建立线性方程组，
回答三个问题：

1. 哪些层的浓度能被**唯一确定**（对系数矩阵做零空间分析）；
2. 哪些层**无法区分**——若两层在所有样本中总以相同长度比例出现（列向量成比例），
   给出**两个不同但都能复现全部化验值**的解释；
3. 整批数据是否**相容**（在各自化验误差内是否存在解）；不相容时用留一法/贪心
   剔除**定位冲突岩芯**。

绝不会把整筒均值直接写到所有穿过的层上。

## 输入

- 分层界面：LAS 文件中的分段常值编码曲线（`--las well.las --curve ZONE`，
  缺省自动识别 ZONE/FACIES/LAYER 等），或直接在 JSON 里给 `layers`；
- 每筒：筒顶/筒底、整筒浓度、浓度误差、实收长度、可能的失芯区间。

```json
{
  "barrels": [
    {"name": "B1", "top": 999.5, "base": 1004.0, "concentration": 12.0,
     "error": 0.3, "recovered_length": 4.0, "lost_intervals": [[1002.0, 1002.5]]}
  ]
}
```

失芯处理：实收井段 = 取芯段 − 失芯区间；给了实收长度但未给失芯区间时，
按现场常用假定从筒顶连续取心并给出警告。

## 用法

```bash
python -m coreblend.cli barrels.json --las well.las --curve ZONE
python -m coreblend.cli barrels.json --las well.las --json   # 机器可读报告
```

退出码：0 相容，1 不相容。演示数据见 `examples/coreblend/`。

## Python API

```python
from coreblend import Layer, Barrel, analyze, format_report
from coreblend.lasio_io import layers_from_las

layers = layers_from_las("well.las", "ZONE")
barrels = [Barrel("B1", 999.5, 1004.0, concentration=12.0, error=0.3,
                  recovered_length=4.0, lost_intervals=[(1002.0, 1002.5)])]
report = analyze(layers, barrels)
print(format_report(report))
```

测试：`pytest tests/test_coreblend.py`
