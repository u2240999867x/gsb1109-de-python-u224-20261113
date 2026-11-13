"""从 LAS 文件读取分层界面（利用 lasio）。"""
from __future__ import annotations

import numpy as np

import lasio

from .model import Layer

# 常见的分层/岩性编码曲线名，未显式指定时按顺序尝试
CANDIDATE_CURVES = ("ZONE", "ZONES", "FACIES", "LAYER", "FORMATION", "FM", "UNIT")


def layers_from_las(path: str, curve: str | None = None) -> list[Layer]:
    """把 LAS 中一条分段常值编码曲线转成 Layer 列表。

    每个相同编码的连续井段成为一个 Layer；层底界取末样点深度加一个采样步长。
    """
    las = lasio.read(path)
    if curve is None:
        for name in CANDIDATE_CURVES:
            if name in las.keys():
                curve = name
                break
        else:
            raise ValueError(
                f"LAS 中未找到分层曲线（尝试过 {CANDIDATE_CURVES}），请显式指定 curve"
            )
    depth = np.asarray(las.index, dtype=float)
    codes = np.asarray(las[curve], dtype=float)

    null_value = getattr(getattr(las.well, "NULL", None), "value", None)
    valid = np.isfinite(codes)
    if null_value is not None:
        valid &= codes != null_value

    step = float(np.median(np.diff(depth))) if len(depth) > 1 else 0.0
    layers: list[Layer] = []
    start = None
    for i in range(len(depth)):
        if valid[i] and (start is None or codes[i] != codes[i - 1] or not valid[i - 1]):
            start = i
        last_of_run = i == len(depth) - 1 or not valid[i + 1] or codes[i + 1] != codes[i]
        if start is not None and valid[i] and last_of_run:
            code = codes[i]
            label = f"{code:g}" if float(code).is_integer() else f"{code:g}"
            layers.append(
                Layer(name=f"{curve}={label}", top=float(depth[start]), base=float(depth[i]) + step)
            )
            start = None
    return layers
