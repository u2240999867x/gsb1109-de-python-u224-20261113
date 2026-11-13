from __future__ import annotations

import json
import pathlib

from .engine import (
    CONTINUE,
    REPLACED,
    PENDING,
    ORDER_OK,
    reconcile,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORT_DIR = ROOT / "report"


def _format_customer(block: list[dict]) -> list[str]:
    lines = []
    for item in block:
        lines.append(f"- 客户 {item['cust_id']}:")
        for reason in item["reasons"]:
            lines.append(f"    * {reason}")
        if item["order_numbers"]:
            lines.append(f"    * 关联订单: {', '.join(item['order_numbers'])}")
        if item["viable_explanations"]:
            lines.append("    * 旧资料不足, 以下两个解释仍都说得通:")
            for explanation in item["viable_explanations"]:
                lines.append(f"        - {explanation}")
    return lines


def build_report(v1_dir: pathlib.Path, v2_dir: pathlib.Path) -> tuple[str, dict]:
    result = reconcile(v1_dir, v2_dir)
    buckets = result["buckets"]

    lines: list[str] = []
    lines.append("# 两次导出目录对账报告")
    lines.append("")
    lines.append(f"- 旧导出: {result['v1_dir']}")
    lines.append(f"- 新导出: {result['v2_dir']}")
    lines.append("")

    lines.append(f"## 一、{CONTINUE}")
    lines.append("")
    block = buckets[CONTINUE]
    lines.append(f"共 {len(block)} 个编号, 证件号等身份锚点在两版保持一致。")
    lines.extend(_format_customer(block))
    lines.append("")

    lines.append(f"## 二、{REPLACED}")
    lines.append("")
    block = buckets[REPLACED]
    lines.append(f"共 {len(block)} 个编号, 有直接证据表明编号背后的自然人已终止或被替换。")
    lines.extend(_format_customer(block))
    lines.append("")

    lines.append(f"## 三、{PENDING}")
    lines.append("")
    block = buckets[PENDING]
    lines.append(f"共 {len(block)} 个编号, 活跃资料不足以唯一判定, 需客服补件。")
    lines.extend(_format_customer(block))
    lines.append("")

    lines.append("## 四、跨表订单归属核对")
    lines.append("")
    lines.append("规则: 同一笔订单只能锚定单一个自然人证件号; 出现多个证件号即身份冲突。")
    lines.append("")
    for order in result["order_verdicts"]:
        mark = "OK " if order["status"] == ORDER_OK else "!! "
        lines.append(
            f"- {mark}订单 {order['order_no']} [{order['status']}] "
            f"v1客户={order['v1_cust_id']} 账户={order['v1_account']} -> "
            f"v2客户={order['v2_cust_id']} 账户={order['v2_account']}"
        )
        lines.append(f"      证件链: {order['evidenced_id_numbers'] or '无有效证件锚点'}")
        if order["account_anchor_id"]:
            lines.append(f"      账户锚定证件号: {order['account_anchor_id']}")
        lines.append(f"      判定: {order['detail']}")
        lines.append(f"      处置: {order['recommendation']}")
    lines.append("")

    conflict_orders = [o for o in result["order_verdicts"] if o["status"] != ORDER_OK]
    lines.append("## 五、给客服的处置提示")
    lines.append("")
    for item in buckets[PENDING]:
        lines.append(
            f"- 客户 {item['cust_id']} 暂不可定身份, 其订单 {', '.join(item['order_numbers'])} "
            "在确认前不得用于向任何一方出具身份证明。"
        )
    if conflict_orders:
        for order in conflict_orders:
            lines.append(
                f"- 订单 {order['order_no']} 存在 {order['status']}, "
                "不能同时挂在两个客户名下作证。"
            )
            lines.append(f"    建议: {order['recommendation']}")
    if not buckets[PENDING] and not conflict_orders:
        lines.append("- 无待确认身份, 无订单归属冲突。")
    lines.append("")

    return "\n".join(lines), result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="对两次 sqlite-diffable 导出目录做身份与订单对账")
    parser.add_argument("v1_dir", nargs="?", type=pathlib.Path, default=DATA / "export_v1")
    parser.add_argument("v2_dir", nargs="?", type=pathlib.Path, default=DATA / "export_v2")
    parser.add_argument("-o", "--out-dir", type=pathlib.Path, default=REPORT_DIR)
    args = parser.parse_args()

    text, result = build_report(args.v1_dir, args.v2_dir)
    args.out_dir.mkdir(exist_ok=True)
    (args.out_dir / "reconciliation_report.md").write_text(text, encoding="utf-8")
    (args.out_dir / "reconciliation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(text)


if __name__ == "__main__":
    main()
