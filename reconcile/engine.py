from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field, asdict
from typing import Any

CONTINUE = "可确认延续"
REPLACED = "可确认更替"
PENDING = "待确认"

ORDER_OK = "归属一致"
ORDER_CUST_CHANGED = "订单客户编号变更"
ORDER_OWNER_MISMATCH = "订单账户归属不一致"
ORDER_IDENTITY_SPLIT = "同一订单指向两个自然人身份"


def read_table(export_dir: pathlib.Path, table: str) -> list[dict[str, Any]]:
    meta = json.loads((export_dir / f"{table}.metadata.json").read_text())
    columns = meta["columns"]
    path = export_dir / f"{table}.ndjson"
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(dict(zip(columns, json.loads(line))))
    return rows


@dataclass
class Snapshot:
    directory: pathlib.Path
    customers: dict[int, dict[str, Any]]
    accounts: dict[str, dict[str, Any]]
    identity: list[dict[str, Any]]
    orders: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, directory: pathlib.Path) -> "Snapshot":
        customers = {row["cust_id"]: row for row in read_table(directory, "customers")}
        accounts = {row["account_no"]: row for row in read_table(directory, "accounts")}
        identity = read_table(directory, "identity_map")
        orders = {row["order_no"]: row for row in read_table(directory, "orders")}
        return cls(directory, customers, accounts, identity, orders)

    def accounts_for(self, cust_id: int) -> list[dict[str, Any]]:
        return [a for a in self.accounts.values() if a["cust_id"] == cust_id]

    def identity_for(self, cust_id: int | None) -> list[dict[str, Any]]:
        return [m for m in self.identity if m["cust_id"] == cust_id]

    def active_id_numbers_for(self, cust_id: int) -> set[str]:
        """证件号仅取活跃锚点: 客户主表 + 未关闭账户 + 仍绑定本编号的映射。"""
        ids: set[str] = set()
        if cust_id in self.customers and self.customers[cust_id]["id_no"] is not None:
            ids.add(self.customers[cust_id]["id_no"])
        for account in self.accounts_for(cust_id):
            if account["status"] != "closed" and account["id_no"] is not None:
                ids.add(account["id_no"])
        for mapping in self.identity_for(cust_id):
            if mapping["id_no"] is not None:
                ids.add(mapping["id_no"])
        return ids


@dataclass
class CustomerVerdict:
    cust_id: int
    classification: str
    reasons: list[str] = field(default_factory=list)
    old_identity: dict[str, Any] | None = None
    new_identity: dict[str, Any] | None = None
    viable_explanations: list[str] = field(default_factory=list)
    order_numbers: list[str] = field(default_factory=list)


@dataclass
class OrderVerdict:
    order_no: str
    status: str
    detail: str
    v1_cust_id: int | None = None
    v2_cust_id: int | None = None
    v1_account: str | None = None
    v2_account: str | None = None
    evidenced_id_numbers: list[str] = field(default_factory=list)
    account_anchor_id: str | None = None
    recommendation: str = ""


def _changed_fields(old: dict[str, Any], new: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    changed = {}
    for key in old:
        if old.get(key) != new.get(key):
            changed[key] = (old.get(key), new.get(key))
    return changed


def classify_customer(cust_id: int, v1: Snapshot, v2: Snapshot) -> CustomerVerdict:
    old = v1.customers.get(cust_id)
    new = v2.customers.get(cust_id)
    verdict = CustomerVerdict(cust_id=cust_id, classification=PENDING)
    verdict.old_identity = old
    verdict.new_identity = new
    verdict.order_numbers = sorted(
        {o["order_no"] for snap in (v1, v2) for o in snap.orders.values() if o["cust_id"] == cust_id}
    )

    if old is None and new is not None:
        verdict.classification = REPLACED
        verdict.reasons.append("v1 无此编号, v2 新建, 属新增编号而非旧号延续")
        return verdict

    if old is not None and new is None:
        verdict.classification = REPLACED
        verdict.reasons.append("v2 中该编号消失, 旧客户身份终止(删除/离任可确认, 编号未见重用)")
        return verdict

    changed = _changed_fields(old, new)
    old_active_ids = v1.active_id_numbers_for(cust_id)
    new_active_ids = v2.active_id_numbers_for(cust_id)
    shared_active = old_active_ids & new_active_ids

    if "dept" in changed:
        verdict.reasons.append(
            f"部门由 {changed['dept'][0]} 调整为 {changed['dept'][1]}(部门变动本身不改变自然人身份)"
        )
    for key in ("phone", "name", "status"):
        if key in changed:
            verdict.reasons.append(f"{key} 发生变化: {changed[key][0]!r} -> {changed[key][1]!r}")

    new_customer_id = new["id_no"]
    old_customer_id = old["id_no"]

    # 同一自然人: 客户主表证件号保持一致, 且活跃锚点仍指向同一证件号
    if (
        new_customer_id is not None
        and new_customer_id == old_customer_id
        and (not new_active_ids or new_customer_id in new_active_ids or shared_active)
    ):
        verdict.classification = CONTINUE
        verdict.reasons.append(f"客户主表证件号一致({new_customer_id}), 自然人身份延续可确认")
        if shared_active:
            verdict.reasons.append(f"活跃账户/业务身份映射仍锚定同一证件号: {sorted(shared_active)}")
        return verdict

    # 明确换人: 主表证件号变为另一个非空值, 活跃锚点与旧证件号完全断裂
    if (
        new_customer_id is not None
        and old_customer_id is not None
        and new_customer_id != old_customer_id
        and old_customer_id not in new_active_ids
    ):
        verdict.classification = REPLACED
        verdict.reasons.append(
            f"客户主表证件号由 {old_customer_id} 变为 {new_customer_id}, "
            "活跃账户与业务身份映射均不再锚定旧证件号"
        )
        closed = [a["account_no"] for a in v2.accounts_for(cust_id) if a["status"] == "closed"]
        if closed:
            verdict.reasons.append(f"旧证件号仅残留在已关闭账户 {closed}(残留不作延续依据)")
        reopened = [a["account_no"] for a in v2.accounts_for(cust_id) if a["id_no"] == new_customer_id]
        if reopened:
            verdict.reasons.append(f"已按新证件号 {new_customer_id} 重新开户: {reopened}")
        detached = [
            m["biz_id"]
            for m in v2.identity
            if m["id_no"] == old_customer_id and m["cust_id"] is None
        ]
        if detached:
            verdict.reasons.append(f"业务身份映射中旧证件号已与编号 {cust_id} 解绑: {detached}")
        rebound = [
            m["biz_id"]
            for m in v2.identity
            if m["id_no"] == new_customer_id and m["cust_id"] == cust_id
        ]
        if rebound:
            verdict.reasons.append(f"新证件号已建立业务身份映射: {rebound}")
        return verdict

    # 活跃身份锚点断裂(主表证件号缺失等), 又没有第二个自然人的直接证据 -> 待确认
    verdict.classification = PENDING
    if new_customer_id is None:
        verdict.reasons.append("v2 客户主表证件号为空, 无法在主表层面锁定自然人")

    v2_accounts = v2.accounts_for(cust_id)
    closed_with_old = sorted(
        a["account_no"] for a in v2_accounts if a["status"] == "closed" and a["id_no"] == old_customer_id
    )
    open_with_old = sorted(
        a["account_no"]
        for a in v2_accounts
        if a["status"] != "closed" and a["id_no"] == old_customer_id
    )
    blank_accounts = sorted(a["account_no"] for a in v2_accounts if a["id_no"] is None)
    legacy_mappings = sorted(
        m["biz_id"] for m in v2.identity_for(cust_id) if m["id_no"] == old_customer_id
    )
    blank_mappings = sorted(m["biz_id"] for m in v2.identity_for(cust_id) if m["id_no"] is None)

    if old_customer_id is not None:
        legacy_parts = []
        if closed_with_old:
            legacy_parts.append(f"已关闭账户 {closed_with_old}")
        if legacy_mappings:
            legacy_parts.append(f"历史业务身份映射 {legacy_mappings}")
        if legacy_parts:
            verdict.reasons.append(
                f"旧证件号 {old_customer_id} 仅见于{'; '.join(legacy_parts)}, "
                "均为关闭/历史态, 不能单独证明当前仍为同一自然人"
            )
        if open_with_old:
            verdict.reasons.append(
                f"未关闭账户 {open_with_old} 仍登记旧证件号 {old_customer_id}(倾向延续, 但主表证件缺失, 需补件)"
            )
        if blank_accounts:
            verdict.reasons.append(
                f"边界时间新开证件号为空的账户 {blank_accounts}, 不能证明与旧客户为同一人"
            )
        if blank_mappings:
            verdict.reasons.append(f"新业务身份映射 {blank_mappings} 证件号为空, 身份尚未登记")

    verdict.viable_explanations = [
        (
            f"解释一(部门调动/同一人延续): 编号 {cust_id} 仍是同一自然人, 仅由原部门调整部门, "
            "旧账户关停后重开, 历史订单应归本人; 需补齐 v2 证件号, 使其与旧证件号一致方可确认"
        ),
        (
            f"解释二(删号重用/新旧更替): 旧客户删除后编号 {cust_id} 被分配给新自然人, "
            "旧证件号只留在已关闭账户与历史映射, 新账户/新映射尚未登记证件; "
            "需提供新客户开户证件, 证明与旧客户为不同人"
        ),
    ]
    return verdict


def _account_owner(snap: Snapshot, account_no: str | None) -> int | None:
    if account_no is None or account_no not in snap.accounts:
        return None
    return snap.accounts[account_no]["cust_id"]


def _account_id(snap: Snapshot, account_no: str | None) -> str | None:
    if account_no is None or account_no not in snap.accounts:
        return None
    return snap.accounts[account_no]["id_no"]


def check_orders(v1: Snapshot, v2: Snapshot) -> list[OrderVerdict]:
    verdicts: list[OrderVerdict] = []
    for order_no in sorted(set(v1.orders) | set(v2.orders)):
        o1 = v1.orders.get(order_no)
        o2 = v2.orders.get(order_no)
        c1 = o1["cust_id"] if o1 else None
        c2 = o2["cust_id"] if o2 else None
        a1 = o1["account_no"] if o1 else None
        a2 = o2["account_no"] if o2 else None

        owner1 = _account_owner(v1, a1)
        owner2 = _account_owner(v2, a2)
        id1 = _account_id(v1, a1)
        id2 = _account_id(v2, a2)
        cust_id1 = v1.customers.get(c1, {}).get("id_no") if c1 is not None else None
        cust_id2 = v2.customers.get(c2, {}).get("id_no") if c2 is not None else None

        evidenced = sorted({x for x in (id1, id2, cust_id1, cust_id2) if x})

        status = ORDER_OK
        details: list[str] = []

        if owner1 is not None and owner1 != c1:
            status = ORDER_OWNER_MISMATCH
            details.append(f"v1 订单挂在客户 {c1}, 但账户 {a1} 实际属于客户 {owner1}")
        if owner2 is not None and owner2 != c2:
            status = ORDER_OWNER_MISMATCH
            details.append(f"v2 订单挂在客户 {c2}, 但账户 {a2} 实际属于客户 {owner2}")

        if o1 and o2 and c1 != c2:
            status = ORDER_CUST_CHANGED
            details.append(f"同一订单客户编号由 {c1} 改为 {c2}, 两侧归属不同")

        if len(set(evidenced)) > 1:
            if status == ORDER_OK:
                status = ORDER_IDENTITY_SPLIT
            details.append(
                f"该订单经账户/客户链路指向多个证件号 {evidenced}, 不能同时用来给两个客户证明身份"
            )

        # 账户锚定: 订单所挂账户登记的证件号, 是该订单可追溯的最强身份锚点
        anchors = {x for x in (id1, id2) if x}
        if len(anchors) == 1:
            account_anchor = next(iter(anchors))
        else:
            account_anchor = None

        recommendation = ""
        if status == ORDER_OK:
            if account_anchor:
                details.append(f"账户锚定证件号 {account_anchor}, 身份链单一")
                recommendation = f"可按账户锚定证件号 {account_anchor} 出具归属"
            else:
                details.append("两版客户编号与账户归属一致, 但缺有效证件锚点")
                recommendation = "归属编号一致, 但证件缺失, 出具身份证明前需补证件"
        elif status == ORDER_IDENTITY_SPLIT and account_anchor:
            others = sorted(set(evidenced) - {account_anchor})
            recommendation = (
                f"订单实际挂在锚定 {account_anchor} 的账户上, 应归 {account_anchor} 对应客户; "
                f"不得据此向 {others} 对应的新身份作证, 需先剥离错误的客户链路"
            )
        elif status == ORDER_CUST_CHANGED:
            recommendation = (
                "两版挂账账户本身已换人, 数据无法判定归属, 冻结该订单的对外证明, "
                "凭原始开户/下单凭证人工裁定后再归位"
            )
        else:
            recommendation = "存在归属冲突, 确认前不得用于向任何客户证明身份"

        verdicts.append(
            OrderVerdict(
                order_no=order_no,
                status=status,
                detail="; ".join(details),
                v1_cust_id=c1,
                v2_cust_id=c2,
                v1_account=a1,
                v2_account=a2,
                evidenced_id_numbers=evidenced,
                account_anchor_id=account_anchor,
                recommendation=recommendation,
            )
        )
    return verdicts


def reconcile(v1_dir: pathlib.Path, v2_dir: pathlib.Path) -> dict[str, Any]:
    v1 = Snapshot.load(v1_dir)
    v2 = Snapshot.load(v2_dir)
    all_cust = sorted(set(v1.customers) | set(v2.customers))
    customer_verdicts = [classify_customer(cust_id, v1, v2) for cust_id in all_cust]
    order_verdicts = check_orders(v1, v2)

    buckets: dict[str, list[CustomerVerdict]] = {CONTINUE: [], REPLACED: [], PENDING: []}
    for verdict in customer_verdicts:
        buckets[verdict.classification].append(verdict)

    return {
        "v1_dir": str(v1_dir),
        "v2_dir": str(v2_dir),
        "buckets": {key: [asdict(v) for v in value] for key, value in buckets.items()},
        "customer_verdicts": [asdict(v) for v in customer_verdicts],
        "order_verdicts": [asdict(v) for v in order_verdicts],
    }
