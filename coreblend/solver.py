"""整筒浓度 -> 分层浓度 的反演与可辨识性分析。

数学模型
--------
对每筒 i，实收岩芯与各层 j 的重叠长度 w_ij 满足质量平衡：

    sum_j (w_ij / L_i) * c_j  =  C_i        (L_i = sum_j w_ij)

其中 C_i 为整筒化验浓度，c_j 为待求的分层浓度。化验误差给出逐行容差：

    |sum_j (w_ij / L_i) * c_j - C_i| <= error_i

分析内容
--------
1. 相容性：加权最小二乘 + IRLS 逼近 L∞，检查是否存在满足全部容差的解；
   不相容时通过留一法/贪心剔除定位冲突岩芯。
2. 可辨识性：对系数矩阵做 SVD 求零空间，零空间在某层分量为零 <=> 该层
   浓度可被唯一确定。
3. 歧义组：列向量成比例的层在所有样本中总以相同比例出现，无法区分；
   对每组给出两个都能复现化验值的不同解释（沿零空间方向推到非负边界）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .model import Barrel, Layer

LENGTH_TOL = 1e-6
CONSISTENT_TOL = 1.0 + 1e-6  # 归一化残差 <= 1 视为相容
_NULL_TOL = 1e-8


@dataclass
class LayerResult:
    name: str
    top: float
    base: float
    status: str  # "determined" | "undetermined" | "unsampled"
    value: float | None  # determined 时给出浓度
    sampled_length: float  # 被所有岩芯采到的总长度


@dataclass
class AmbiguityGroup:
    layers: list[str]
    explanation_a: dict[str, float]
    explanation_b: dict[str, float]


@dataclass
class Report:
    consistent: bool
    max_normalized_residual: float
    layers: list[LayerResult]
    groups: list[AmbiguityGroup]
    suspect_barrels: list[str]
    residuals: dict[str, float]  # 每筒的归一化残差
    solution: dict[str, float]  # 一个相容（或最接近相容）的解
    warnings: list[str] = field(default_factory=list)


def _overlap(a_top: float, a_base: float, b_top: float, b_base: float) -> float:
    return max(0.0, min(a_base, b_base) - max(a_top, b_top))


def recovered_intervals(barrel: Barrel, warnings: list[str]) -> list[tuple[float, float]]:
    """实收岩芯占据的井段：取芯段扣除失芯区间。

    若给了实收长度但未给失芯区间，则按现场常用假定：从筒顶连续取心。
    """
    drilled = barrel.base - barrel.top
    intervals: list[tuple[float, float]] = [(barrel.top, barrel.base)]
    for lost_top, lost_base in sorted(barrel.lost_intervals):
        lo, hi = max(lost_top, barrel.top), min(lost_base, barrel.base)
        if hi <= lo:
            warnings.append(
                f"{barrel.name}: 失芯区间 ({lost_top}, {lost_base}) 不在取芯段内，已忽略"
            )
            continue
        shrunk: list[tuple[float, float]] = []
        for start, end in intervals:
            if end <= lo or start >= hi:
                shrunk.append((start, end))
                continue
            if start < lo:
                shrunk.append((start, lo))
            if end > hi:
                shrunk.append((hi, end))
        intervals = shrunk
    present = sum(end - start for start, end in intervals)

    if barrel.recovered_length is not None:
        rec = min(barrel.recovered_length, drilled)
        if barrel.recovered_length > drilled + LENGTH_TOL:
            warnings.append(
                f"{barrel.name}: 实收长度 {barrel.recovered_length} 超过进尺 {drilled}，已按进尺截断"
            )
        if not barrel.lost_intervals and rec < drilled - LENGTH_TOL:
            intervals = [(barrel.top, barrel.top + rec)]
            warnings.append(
                f"{barrel.name}: 未给失芯区间，假定从筒顶连续实收 {rec:g}"
            )
        elif barrel.lost_intervals and abs(present - rec) > max(0.05, 0.01 * drilled):
            warnings.append(
                f"{barrel.name}: 实收长度 {rec:g} 与扣除失芯后的 {present:g} 不一致，按失芯区间计算"
            )
    return intervals


def _build_system(
    layers: list[Layer], barrels: list[Barrel], warnings: list[str]
):
    """返回归一化方程组 A x = b、逐行容差 tol、原始重叠长度矩阵及参与筒次索引。"""
    rows, rhs, tol, kept, raw = [], [], [], [], []
    for i, br in enumerate(barrels):
        intervals = recovered_intervals(br, warnings)
        w = np.array(
            [sum(_overlap(s, e, ly.top, ly.base) for s, e in intervals) for ly in layers]
        )
        total = w.sum()
        if total <= LENGTH_TOL:
            warnings.append(f"{br.name}: 实收岩芯未与任何分层重叠，该筒不参与反演")
            continue
        rows.append(w / total)
        rhs.append(br.concentration)
        tol.append(max(br.error, 0.0) + 1e-9)
        kept.append(i)
        raw.append(w)
    if not rows:
        return (
            np.zeros((0, len(layers))),
            np.zeros(0),
            np.zeros(0),
            np.zeros((0, len(layers))),
            kept,
        )
    return np.array(rows), np.array(rhs), np.array(tol), np.array(raw), kept


def _solve_min_residual(A: np.ndarray, b: np.ndarray, tol: np.ndarray):
    """最小化 max |(Ax-b)/tol| 的近似解（加权最小二乘 + IRLS 升幂）。"""
    n = A.shape[1]
    if A.shape[0] == 0:
        return np.zeros(n), 0.0
    W = 1.0 / tol
    Aw, bw = A * W[:, None], b * W
    x, *_ = np.linalg.lstsq(Aw, bw, rcond=None)

    def max_residual(sol: np.ndarray) -> float:
        return float(np.max(np.abs(A @ sol - b) / tol))

    # 已相容则不必迭代；否则 IRLS 升幂逼近 L∞（权重归一并加下限，避免塌缩）
    for p in (4.0, 8.0, 16.0):
        if max_residual(x) <= CONSISTENT_TOL:
            break
        r = np.abs(A @ x - b) / tol
        r = r / max(r.max(), 1e-12)
        w = np.maximum(r, 1e-3) ** ((p - 2.0) / 2.0)
        x_new, *_ = np.linalg.lstsq(Aw * w[:, None], bw * w, rcond=None)
        if max_residual(x_new) <= max_residual(x) + 1e-9:
            x = x_new
    return x, max_residual(x)


def _null_space(A: np.ndarray) -> np.ndarray:
    """A 的零空间正交基，形状 (n_layers, k)。"""
    n = A.shape[1]
    if A.shape[0] == 0:
        return np.eye(n)
    _, s, Vh = np.linalg.svd(A)
    if s.size == 0:
        return np.eye(n)
    cutoff = s.max() * max(A.shape) * np.finfo(float).eps * 10
    rank = int((s > cutoff).sum())
    return Vh[rank:].T


def _column_groups(A: np.ndarray) -> list[list[int]]:
    """把列向量成比例（在所有样本中同比例出现）的层归为一组。"""
    groups: list[list[int]] = []
    directions: list[np.ndarray] = []
    for j in range(A.shape[1]):
        col = A[:, j]
        norm = np.linalg.norm(col)
        if norm < 1e-12:
            continue  # 未被采样，单列处理
        v = col / norm
        for g, d in enumerate(directions):
            if np.linalg.norm(v - d) < 1e-9:
                groups[g].append(j)
                break
        else:
            directions.append(v)
            groups.append([j])
    return groups


def _alternatives(
    x: np.ndarray, group: list[int], A: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """对一个歧义组给出两个都满足化验值的解释。

    解释一：当前相容解 x；解释二：沿组内零空间方向推到某层浓度为 0 的边界。
    """
    j0, j1 = group[0], group[1]
    a0 = np.linalg.norm(A[:, j0])
    a1 = np.linalg.norm(A[:, j1])
    direction = np.zeros_like(x)
    direction[j0] = 1.0 / a0
    direction[j1] = -1.0 / a1  # sum_j w_ij * direction_j = 0，化验值不变

    t_lo, t_hi = -np.inf, np.inf
    for j in (j0, j1):
        d = direction[j]
        if d > 0:
            t_lo = max(t_lo, -x[j] / d)
        else:
            t_hi = min(t_hi, -x[j] / d)
    candidates = [t for t in (t_lo, t_hi) if np.isfinite(t)]
    t = max(candidates, key=abs) if candidates else 0.0
    x_b = x + t * direction
    x_b[np.abs(x_b) < 1e-9] = 0.0
    return x, x_b


def _diagnose_inconsistency(
    A: np.ndarray, b: np.ndarray, tol: np.ndarray, names: list[str]
) -> list[str]:
    """定位冲突岩芯：先做留一法，失败则按残差贪心剔除。"""
    suspects = []
    for k in range(A.shape[0]):
        mask = np.arange(A.shape[0]) != k
        _, max_res = _solve_min_residual(A[mask], b[mask], tol[mask])
        if max_res <= CONSISTENT_TOL:
            suspects.append(names[k])
    if suspects:
        return suspects
    # 贪心：反复剔除归一化残差最大的筒次，直到剩余相容
    removed: list[str] = []
    idx = list(range(A.shape[0]))
    while idx:
        x, max_res = _solve_min_residual(A[idx], b[idx], tol[idx])
        if max_res <= CONSISTENT_TOL:
            break
        residuals = np.abs(A[idx] @ x - b[idx]) / tol[idx]
        worst = idx[int(np.argmax(residuals))]
        removed.append(names[worst])
        idx.remove(worst)
    return removed


def analyze(layers: list[Layer], barrels: list[Barrel]) -> Report:
    """主入口：返回分层浓度可辨识性报告。"""
    warnings: list[str] = []
    A, b, tol, raw_w, kept = _build_system(layers, barrels, warnings)
    names = [barrels[i].name for i in kept]

    x, max_res = _solve_min_residual(A, b, tol)
    consistent = max_res <= CONSISTENT_TOL
    x_clean = x.copy()
    x_clean[np.abs(x_clean) < 1e-9] = 0.0

    null = _null_space(A)
    determined = np.abs(null).max(axis=1) < _NULL_TOL if null.size else np.ones(len(layers), bool)
    sampled = raw_w.sum(axis=0) if raw_w.shape[0] else np.zeros(len(layers))

    layer_results = []
    for j, ly in enumerate(layers):
        if sampled[j] <= 1e-12:
            status, value = "unsampled", None
        elif determined[j]:
            status, value = "determined", float(x_clean[j])
        else:
            status, value = "undetermined", None
        layer_results.append(
            LayerResult(ly.name, ly.top, ly.base, status, value, float(sampled[j]))
        )

    groups: list[AmbiguityGroup] = []
    for group in _column_groups(A):
        if len(group) < 2 or all(determined[j] for j in group):
            continue
        x_a, x_b = _alternatives(x_clean, group, A)
        groups.append(
            AmbiguityGroup(
                layers=[layers[j].name for j in group],
                explanation_a={layers[j].name: float(x_a[j]) for j in group},
                explanation_b={layers[j].name: float(x_b[j]) for j in group},
            )
        )

    suspects: list[str] = []
    if not consistent and A.shape[0] > 0:
        suspects = _diagnose_inconsistency(A, b, tol, names)

    residuals = (
        dict(zip(names, (np.abs(A @ x - b) / tol).tolist())) if A.shape[0] else {}
    )
    return Report(
        consistent=consistent,
        max_normalized_residual=max_res,
        layers=layer_results,
        groups=groups,
        suspect_barrels=suspects,
        residuals=residuals,
        solution={ly.name: float(x_clean[j]) for j, ly in enumerate(layers)},
        warnings=warnings,
    )


def format_report(report: Report) -> str:
    """把报告渲染成中文文本。"""
    lines: list[str] = []
    if report.consistent:
        lines.append("整批相容性: 相容（存在满足全部化验误差的解）")
    else:
        lines.append(
            f"整批相容性: 不相容（最大归一化残差 {report.max_normalized_residual:.2f} > 1）"
        )
        lines.append("冲突岩芯: " + ("、".join(report.suspect_barrels) or "无法定位到单筒"))

    determined = [r for r in report.layers if r.status == "determined"]
    undetermined = [r for r in report.layers if r.status == "undetermined"]
    unsampled = [r for r in report.layers if r.status == "unsampled"]

    lines.append("")
    lines.append("可唯一确定浓度的层:")
    if determined:
        for r in determined:
            lines.append(f"  {r.name} [{r.top:g}, {r.base:g}): 浓度 = {r.value:.4g}")
    else:
        lines.append("  （无）")

    if undetermined:
        lines.append("不能唯一确定的层:")
        for r in undetermined:
            lines.append(f"  {r.name} [{r.top:g}, {r.base:g})  被采样 {r.sampled_length:g}")
    if unsampled:
        lines.append("未被任何岩芯采样的层:")
        for r in unsampled:
            lines.append(f"  {r.name} [{r.top:g}, {r.base:g})")

    for i, g in enumerate(report.groups, 1):
        lines.append("")
        lines.append(
            f"歧义组 {i}: 层 {'、'.join(g.layers)} 在所有岩芯中比例相同，无法区分"
        )
        fmt = lambda d: "，".join(f"{k}={v:.4g}" for k, v in d.items())
        lines.append(f"  解释一: {fmt(g.explanation_a)}")
        lines.append(f"  解释二: {fmt(g.explanation_b)}")

    if report.warnings:
        lines.append("")
        lines.append("警告:")
        lines.extend(f"  {w}" for w in report.warnings)
    return "\n".join(lines)
