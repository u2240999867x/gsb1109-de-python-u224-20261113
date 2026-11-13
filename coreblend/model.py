"""数据模型：分层（来自 LAS 界面）与取芯筒次。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Layer:
    """一个分层段 [top, base)，深度单位与 LAS 一致。"""

    name: str
    top: float
    base: float

    def __post_init__(self) -> None:
        if not self.base > self.top:
            raise ValueError(f"层 {self.name!r} 的底界必须大于顶界")


@dataclass
class Barrel:
    """一筒取芯。

    top/base        取芯井段（钻进的筒顶、筒底）
    concentration   整筒化验浓度（均值）
    error           浓度绝对误差（与 concentration 同单位），用于相容性判据
    recovered_length 实收长度；缺省认为满筒取到（扣除失芯后）
    lost_intervals  已知/可能的失芯区间 [(top, base), ...]，必须落在取芯段内
    """

    name: str
    top: float
    base: float
    concentration: float
    error: float = 0.0
    recovered_length: float | None = None
    lost_intervals: list[tuple[float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.base > self.top:
            raise ValueError(f"岩芯 {self.name!r} 的筒底必须大于筒顶")
