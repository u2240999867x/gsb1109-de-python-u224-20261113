from __future__ import annotations

import pathlib
import sqlite_utils

from click.testing import CliRunner
from sqlite_diffable import cli

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
V1_DB = DATA / "snapshot_v1.db"
V2_DB = DATA / "snapshot_v2.db"
V1_DIR = DATA / "export_v1"
V2_DIR = DATA / "export_v2"

TABLES = ("customers", "accounts", "identity_map", "orders")

T_EARLY = "2026-09-01T09:00:00"
T_BOUNDARY = "2026-10-15T12:00:00"
T_LATE = "2026-11-01T09:00:00"


def _fresh(path: pathlib.Path) -> sqlite_utils.Database:
    if path.exists():
        path.unlink()
    return sqlite_utils.Database(str(path))


def _seed_customers(db: sqlite_utils.Database, rows):
    db["customers"].insert_all(rows, pk="cust_id")


def _seed_accounts(db: sqlite_utils.Database, rows):
    db["accounts"].insert_all(rows, pk="account_no")


def _seed_identity(db: sqlite_utils.Database, rows):
    db["identity_map"].insert_all(rows, pk="biz_id")


def _seed_orders(db: sqlite_utils.Database, rows):
    db["orders"].insert_all(rows, pk="order_no")


def build_v1(db_path: pathlib.Path = V1_DB) -> None:
    db = _fresh(db_path)
    _seed_customers(
        db,
        [
            {"cust_id": 1, "name": "陈安", "id_no": "ID01", "phone": "138-0001", "dept": "A", "status": "active"},
            {"cust_id": 2, "name": "白璐", "id_no": "ID02", "phone": "138-0002", "dept": "A", "status": "active"},
            {"cust_id": 17, "name": "韩冰", "id_no": "ID17", "phone": "137-0001", "dept": "A", "status": "active"},
            {"cust_id": 30, "name": "麦琪", "id_no": "ID30A", "phone": "136-0030", "dept": "C", "status": "active"},
            {"cust_id": 40, "name": "孟宁", "id_no": "ID40", "phone": "135-0040", "dept": "B", "status": "active"},
            {"cust_id": 41, "name": "祁镇", "id_no": "ID41", "phone": "135-0041", "dept": "B", "status": "active"},
            {"cust_id": 50, "name": "离任老客户", "id_no": "ID50", "phone": "135-0050", "dept": "C", "status": "active"},
        ],
    )
    _seed_accounts(
        db,
        [
            {"account_no": "A1001", "cust_id": 1, "id_no": "ID01", "opened_at": T_EARLY, "status": "open"},
            {"account_no": "A2001", "cust_id": 2, "id_no": "ID02", "opened_at": T_EARLY, "status": "open"},
            {"account_no": "A1701", "cust_id": 17, "id_no": "ID17", "opened_at": T_EARLY, "status": "open"},
            {"account_no": "A3001", "cust_id": 30, "id_no": "ID30A", "opened_at": T_EARLY, "status": "open"},
            {"account_no": "A4001", "cust_id": 40, "id_no": "ID40", "opened_at": T_EARLY, "status": "open"},
            {"account_no": "A4101", "cust_id": 41, "id_no": "ID41", "opened_at": T_EARLY, "status": "open"},
            {"account_no": "A5001", "cust_id": 50, "id_no": "ID50", "opened_at": T_EARLY, "status": "open"},
        ],
    )
    _seed_identity(
        db,
        [
            {"biz_id": "B01", "id_no": "ID01", "cust_id": 1, "note": "初始映射"},
            {"biz_id": "B02", "id_no": "ID02", "cust_id": 2, "note": "初始映射"},
            {"biz_id": "B17", "id_no": "ID17", "cust_id": 17, "note": "初始映射"},
            {"biz_id": "B30", "id_no": "ID30A", "cust_id": 30, "note": "初始映射"},
            {"biz_id": "B40", "id_no": "ID40", "cust_id": 40, "note": "初始映射"},
            {"biz_id": "B41", "id_no": "ID41", "cust_id": 41, "note": "初始映射"},
            {"biz_id": "B50", "id_no": "ID50", "cust_id": 50, "note": "初始映射"},
        ],
    )
    _seed_orders(
        db,
        [
            {"order_no": "O1001", "cust_id": 1, "account_no": "A1001", "amount": 100.0, "created_at": T_EARLY},
            {"order_no": "O2001", "cust_id": 2, "account_no": "A2001", "amount": 200.0, "created_at": T_EARLY},
            {"order_no": "O5001", "cust_id": 17, "account_no": "A1701", "amount": 500.0, "created_at": T_EARLY},
            {"order_no": "O7002", "cust_id": 30, "account_no": "A3001", "amount": 700.0, "created_at": T_EARLY},
            {"order_no": "O6001", "cust_id": 40, "account_no": "A4001", "amount": 600.0, "created_at": T_EARLY},
            {"order_no": "O8001", "cust_id": 50, "account_no": "A5001", "amount": 800.0, "created_at": T_EARLY},
        ],
    )
    db.close()


def build_v2(db_path: pathlib.Path = V2_DB) -> None:
    db = _fresh(db_path)
    _seed_customers(
        db,
        [
            {"cust_id": 1, "name": "陈安", "id_no": "ID01", "phone": "138-0001", "dept": "B", "status": "active"},
            {"cust_id": 2, "name": "白璐", "id_no": "ID02", "phone": "138-9002", "dept": "A", "status": "active"},
            {"cust_id": 17, "name": "韩冰", "id_no": None, "phone": "137-9002", "dept": "B", "status": "active"},
            {"cust_id": 30, "name": "卓然", "id_no": "ID30B", "phone": "136-9030", "dept": "C", "status": "active"},
            {"cust_id": 40, "name": "孟宁", "id_no": "ID40", "phone": "135-0040", "dept": "B", "status": "active"},
            {"cust_id": 41, "name": "祁镇", "id_no": "ID41", "phone": "135-0041", "dept": "B", "status": "active"},
            {"cust_id": 52, "name": "全新客户", "id_no": "ID52", "phone": "135-0052", "dept": "A", "status": "active"},
        ],
    )
    _seed_accounts(
        db,
        [
            {"account_no": "A1001", "cust_id": 1, "id_no": "ID01", "opened_at": T_EARLY, "status": "open"},
            {"account_no": "A2001", "cust_id": 2, "id_no": "ID02", "opened_at": T_EARLY, "status": "open"},
            {"account_no": "A1701", "cust_id": 17, "id_no": "ID17", "opened_at": T_EARLY, "status": "closed"},
            {"account_no": "A1799", "cust_id": 17, "id_no": None, "opened_at": T_BOUNDARY, "status": "open"},
            {"account_no": "A3001", "cust_id": 30, "id_no": "ID30A", "opened_at": T_EARLY, "status": "closed"},
            {"account_no": "A3099", "cust_id": 30, "id_no": "ID30B", "opened_at": T_BOUNDARY, "status": "open"},
            {"account_no": "A4001", "cust_id": 40, "id_no": "ID40", "opened_at": T_EARLY, "status": "open"},
            {"account_no": "A4101", "cust_id": 41, "id_no": "ID41", "opened_at": T_EARLY, "status": "open"},
        ],
    )
    _seed_identity(
        db,
        [
            {"biz_id": "B01", "id_no": "ID01", "cust_id": 1, "note": "持续映射"},
            {"biz_id": "B02", "id_no": "ID02", "cust_id": 2, "note": "持续映射"},
            {"biz_id": "B17-OLD", "id_no": "ID17", "cust_id": 17, "note": "旧档案, 账户A1701已关"},
            {"biz_id": "B17-NEW", "id_no": None, "cust_id": 17, "note": "待补证件"},
            {"biz_id": "B30-OLD", "id_no": "ID30A", "cust_id": None, "note": "已注销(麦琪)"},
            {"biz_id": "B30-NEW", "id_no": "ID30B", "cust_id": 30, "note": "新开户(卓然)"},
            {"biz_id": "B40", "id_no": "ID40", "cust_id": 40, "note": "持续映射"},
            {"biz_id": "B41", "id_no": "ID41", "cust_id": 41, "note": "持续映射"},
            {"biz_id": "B52", "id_no": "ID52", "cust_id": 52, "note": "新客映射"},
        ],
    )
    _seed_orders(
        db,
        [
            {"order_no": "O1001", "cust_id": 1, "account_no": "A1001", "amount": 100.0, "created_at": T_EARLY},
            {"order_no": "O2001", "cust_id": 2, "account_no": "A2001", "amount": 200.0, "created_at": T_EARLY},
            {"order_no": "O5001", "cust_id": 17, "account_no": "A1701", "amount": 500.0, "created_at": T_EARLY},
            {"order_no": "O5002", "cust_id": 17, "account_no": "A1799", "amount": 510.0, "created_at": T_LATE},
            {"order_no": "O7002", "cust_id": 30, "account_no": "A3001", "amount": 700.0, "created_at": T_EARLY},
            {"order_no": "O7003", "cust_id": 30, "account_no": "A3099", "amount": 710.0, "created_at": T_LATE},
            {"order_no": "O6001", "cust_id": 41, "account_no": "A4101", "amount": 600.0, "created_at": T_EARLY},
        ],
    )
    db.close()


def _dump(db_path: pathlib.Path, out_dir: pathlib.Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["dump", str(db_path), str(out_dir), "--all"])
    if result.exit_code != 0:
        raise RuntimeError(result.output)


def build_all(
    v1_db: pathlib.Path = V1_DB,
    v2_db: pathlib.Path = V2_DB,
    v1_dir: pathlib.Path = V1_DIR,
    v2_dir: pathlib.Path = V2_DIR,
) -> tuple[pathlib.Path, pathlib.Path]:
    build_v1(v1_db)
    build_v2(v2_db)
    for out in (v1_dir, v2_dir):
        out.mkdir(parents=True, exist_ok=True)
        for child in out.iterdir():
            child.unlink()
    _dump(v1_db, v1_dir)
    _dump(v2_db, v2_dir)
    return v1_dir, v2_dir


if __name__ == "__main__":
    d1, d2 = build_all()
    print("built", d1, "and", d2)
