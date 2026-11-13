"""命令行入口。

用法:
    python -m coreblend.cli spec.json [--las well.las --curve ZONE] [--json]

spec.json 格式:
    {
      "layers":  [{"name": "L1", "top": 1000.0, "base": 1005.0}, ...],   // 给了 --las 可省略
      "barrels": [{"name": "B1", "top": 999.0, "base": 1009.0,
                   "concentration": 12.3, "error": 0.5,
                   "recovered_length": 8.5,
                   "lost_intervals": [[1003.0, 1004.5]]}, ...]
    }
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

from .model import Barrel, Layer
from .solver import analyze, format_report


def _load_barrels(spec: dict) -> list[Barrel]:
    barrels = []
    for d in spec["barrels"]:
        barrels.append(
            Barrel(
                name=d["name"],
                top=float(d["top"]),
                base=float(d["base"]),
                concentration=float(d["concentration"]),
                error=float(d.get("error", 0.0)),
                recovered_length=(
                    None if d.get("recovered_length") is None else float(d["recovered_length"])
                ),
                lost_intervals=[tuple(map(float, iv)) for iv in d.get("lost_intervals", [])],
            )
        )
    return barrels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coreblend",
        description="整筒浓度反演分层浓度：可辨识性分析、歧义解释、冲突岩芯定位",
    )
    parser.add_argument("spec", help="岩芯（及可选分层）JSON 文件")
    parser.add_argument("--las", help="提供分层界面的 LAS 文件")
    parser.add_argument("--curve", help="LAS 中的分层编码曲线名（默认自动识别）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出机器可读报告")
    args = parser.parse_args(argv)
    logging.getLogger("lasio").setLevel(logging.ERROR)

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)

    if args.las:
        from .lasio_io import layers_from_las

        layers = layers_from_las(args.las, args.curve)
    elif "layers" in spec:
        layers = [Layer(name=d["name"], top=float(d["top"]), base=float(d["base"])) for d in spec["layers"]]
    else:
        parser.error("spec 中没有 layers，且未用 --las 提供分层")

    report = analyze(layers, _load_barrels(spec))
    if args.json:
        print(json.dumps(dataclasses.asdict(report), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report.consistent else 1


if __name__ == "__main__":
    sys.exit(main())
