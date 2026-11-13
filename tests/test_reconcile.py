import pathlib

import pytest

from reconcile import fixture
from reconcile.engine import (
    CONTINUE,
    REPLACED,
    PENDING,
    ORDER_OK,
    ORDER_CUST_CHANGED,
    ORDER_IDENTITY_SPLIT,
    reconcile,
)


@pytest.fixture
def exports(tmp_path: pathlib.Path):
    v1_dir, v2_dir = fixture.build_all(
        v1_db=tmp_path / "v1.db",
        v2_db=tmp_path / "v2.db",
        v1_dir=tmp_path / "export_v1",
        v2_dir=tmp_path / "export_v2",
    )
    return v1_dir, v2_dir


def _by_customer(result):
    return {v["cust_id"]: v for v in result["customer_verdicts"]}


def _by_order(result):
    return {v["order_no"]: v for v in result["order_verdicts"]}


def test_export_directories_contain_all_tables(exports):
    v1_dir, v2_dir = exports
    for directory in (v1_dir, v2_dir):
        names = {p.name for p in directory.iterdir()}
        for table in fixture.TABLES:
            assert f"{table}.ndjson" in names
            assert f"{table}.metadata.json" in names


def test_customer_classification_buckets(exports):
    result = reconcile(*exports)
    buckets = result["buckets"]
    assert sorted(v["cust_id"] for v in buckets[CONTINUE]) == [1, 2, 40, 41]
    assert sorted(v["cust_id"] for v in buckets[REPLACED]) == [30, 50, 52]
    assert sorted(v["cust_id"] for v in buckets[PENDING]) == [17]


def test_department_move_with_same_id_is_continue(exports):
    customer = _by_customer(reconcile(*exports))[1]
    assert customer["classification"] == CONTINUE
    assert any("部门由 A 调整为 B" in r for r in customer["reasons"])


def test_id_change_with_detached_identity_is_replaced(exports):
    customer = _by_customer(reconcile(*exports))[30]
    assert customer["classification"] == REPLACED
    assert any("ID30A 变为 ID30B" in r for r in customer["reasons"])
    assert any("B30-OLD" in r for r in customer["reasons"])
    assert any("A3001" in r and "关闭" in r for r in customer["reasons"])


def test_pending_keeps_two_viable_explanations(exports):
    customer = _by_customer(reconcile(*exports))[17]
    assert customer["classification"] == PENDING
    assert len(customer["viable_explanations"]) == 2
    joined = " ".join(customer["viable_explanations"])
    assert "部门调动" in joined
    assert "删号重用" in joined
    assert "编号 17" in joined


def test_order_identity_split_for_reused_number(exports):
    order = _by_order(reconcile(*exports))["O7002"]
    assert order["status"] == ORDER_IDENTITY_SPLIT
    assert set(order["evidenced_id_numbers"]) == {"ID30A", "ID30B"}
    assert order["account_anchor_id"] == "ID30A"
    assert "ID30A 对应客户" in order["recommendation"]


def test_order_customer_number_change_is_frozen(exports):
    order = _by_order(reconcile(*exports))["O6001"]
    assert order["status"] == ORDER_CUST_CHANGED
    assert order["v1_cust_id"] == 40
    assert order["v2_cust_id"] == 41
    assert "冻结" in order["recommendation"]


def test_clean_orders_have_single_identity(exports):
    orders = _by_order(reconcile(*exports))
    for order_no in ("O1001", "O2001", "O5001", "O7003", "O8001"):
        assert orders[order_no]["status"] == ORDER_OK
        assert len(orders[order_no]["evidenced_id_numbers"]) <= 1


def test_blank_id_order_is_not_evidenced(exports):
    order = _by_order(reconcile(*exports))["O5002"]
    assert order["status"] == ORDER_OK
    assert order["evidenced_id_numbers"] == []
    assert order["account_anchor_id"] is None
    assert "补证件" in order["recommendation"]
