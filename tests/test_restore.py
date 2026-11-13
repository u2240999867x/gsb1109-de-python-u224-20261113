from click.testing import CliRunner
from sqlite_diffable import cli
from sqlite_diffable.restore import restore, RestoreError
import json
import pytest
import sqlite_utils


SCHEMA = """
CREATE TABLE branch (
    branch_id TEXT PRIMARY KEY,
    name TEXT
);
CREATE TABLE supplier (
    supplier_id TEXT PRIMARY KEY,
    name TEXT
);
CREATE TABLE contract (
    supplier_id TEXT NOT NULL,
    contract_no TEXT NOT NULL,
    terms TEXT,
    PRIMARY KEY (supplier_id, contract_no),
    FOREIGN KEY (supplier_id) REFERENCES supplier(supplier_id)
);
CREATE TABLE cycle_a (
    cycle_a_id TEXT PRIMARY KEY,
    cycle_b_id TEXT REFERENCES cycle_b(cycle_b_id)
);
CREATE TABLE cycle_b (
    cycle_b_id TEXT PRIMARY KEY,
    cycle_a_id TEXT REFERENCES cycle_a(cycle_a_id)
);
CREATE TABLE receipt (
    receipt_id TEXT PRIMARY KEY,
    branch_id TEXT REFERENCES branch(branch_id),
    supplier_id TEXT,
    contract_no TEXT,
    note TEXT,
    cycle_a_id TEXT REFERENCES cycle_a(cycle_a_id),
    FOREIGN KEY (supplier_id, contract_no)
        REFERENCES contract(supplier_id, contract_no)
);
CREATE TABLE receipt_line (
    line_id TEXT PRIMARY KEY,
    receipt_id TEXT REFERENCES receipt(receipt_id),
    item TEXT,
    qty INTEGER
);
"""


def _build_source_db(path):
    db = sqlite_utils.Database(path)
    db.conn.executescript(SCHEMA)
    db.conn.commit()
    db["branch"].insert_all(
        [
            {"branch_id": "B1", "name": "Downtown"},
            {"branch_id": "B2", "name": "Uptown"},
        ]
    )
    db["supplier"].insert_all(
        [
            {"supplier_id": "S1", "name": "Acme"},
            {"supplier_id": "S2", "name": "Beta"},
            {"supplier_id": "S3", "name": "Gamma"},
            {"supplier_id": "S6", "name": "Hexa"},
        ]
    )
    db["contract"].insert_all(
        [
            {"supplier_id": "S1", "contract_no": "C1", "terms": "net-30"},
            {"supplier_id": "S2", "contract_no": "C2", "terms": "arch-terms"},
            {"supplier_id": "S3", "contract_no": "C3", "terms": "uptown-only"},
            {"supplier_id": "S6", "contract_no": "C6", "terms": "hex-terms"},
        ]
    )
    db["cycle_b"].insert({"cycle_b_id": "CYB1", "cycle_a_id": "CYA1"})
    db["cycle_a"].insert({"cycle_a_id": "CYA1", "cycle_b_id": "CYB1"})
    db["receipt"].insert_all(
        [
            {"receipt_id": "R1", "branch_id": "B1", "supplier_id": "S1",
             "contract_no": "C1", "note": "ok", "cycle_a_id": None},
            {"receipt_id": "R2", "branch_id": "B1", "supplier_id": "S2",
             "contract_no": "C2", "note": "contract-conflict", "cycle_a_id": None},
            {"receipt_id": "R3", "branch_id": "B1", "supplier_id": "S1",
             "contract_no": "C1", "note": "cyclic", "cycle_a_id": "CYA1"},
            {"receipt_id": "R5", "branch_id": "B1", "supplier_id": "S9",
             "contract_no": "C9", "note": "missing", "cycle_a_id": None},
            {"receipt_id": "R6", "branch_id": "B1", "supplier_id": "S6",
             "contract_no": "C6", "note": "composite-key", "cycle_a_id": None},
            {"receipt_id": "R4", "branch_id": "B2", "supplier_id": "S3",
             "contract_no": "C3", "note": "other-branch", "cycle_a_id": None},
        ]
    )
    db["receipt_line"].insert_all(
        [
            {"line_id": "L1", "receipt_id": "R1", "item": "paper", "qty": 2},
            {"line_id": "L4", "receipt_id": "R4", "item": "stapler", "qty": 1},
        ]
    )
    db.conn.commit()
    return db


def _build_target_db(path):
    db = sqlite_utils.Database(path)
    db.conn.executescript(SCHEMA)
    db.conn.commit()
    # Reference data already living in the target database.
    db["branch"].insert({"branch_id": "B1", "name": "Downtown"})
    db["supplier"].insert_all(
        [
            {"supplier_id": "S1", "name": "Acme"},
            {"supplier_id": "S2", "name": "Beta"},
        ]
    )
    db["contract"].insert_all(
        [
            # Same composite key as archive row (S2, C2), different content.
            {"supplier_id": "S2", "contract_no": "C2", "terms": "target-terms"},
            # Same supplier as the archived (S6, C6) but a different contract:
            # a partial, single-column match must not satisfy the composite FK.
            {"supplier_id": "S6", "contract_no": "COTHER", "terms": "other"},
        ]
    )
    db.conn.commit()
    return db


def _ids(rows, key):
    return sorted(row[key] for row in rows)


def test_restore_filtered_closure_conflicts_and_cycles(tmpdir):
    source_db = str(tmpdir / "source.db")
    target_db = str(tmpdir / "target.db")
    archive = str(tmpdir / "archive")
    _build_source_db(source_db)
    _build_target_db(target_db)

    result = CliRunner().invoke(
        cli.cli, ["dump", source_db, archive, "--all"]
    )
    assert result.exit_code == 0, result.output

    report = restore(target_db, archive, "receipt", {"branch_id": "B1"})

    db = sqlite_utils.Database(target_db)

    # No foreign key violations, including the closed cyclic group.
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # Cyclic group referenced by R3 restored as a closed set.
    assert _ids(db["cycle_a"].rows, "cycle_a_id") == ["CYA1"]
    assert _ids(db["cycle_b"].rows, "cycle_b_id") == ["CYB1"]

    # Receipts that could be restored; blocked ones absent.
    assert _ids(db["receipt"].rows, "receipt_id") == ["R1", "R3", "R6"]
    assert _ids(db["receipt_line"].rows, "line_id") == ["L1"]

    # Only suppliers/contracts actually referenced by B1 receipts came over.
    assert _ids(db["supplier"].rows, "supplier_id") == ["S1", "S2", "S6"]
    contract_keys = sorted(
        (row["supplier_id"], row["contract_no"]) for row in db["contract"].rows
    )
    assert contract_keys == [
        ("S1", "C1"),
        ("S2", "C2"),
        ("S6", "C6"),
        ("S6", "COTHER"),
    ]

    # Conflicting contract must not be overwritten.
    conflict = db.conn.execute(
        "SELECT terms FROM contract WHERE supplier_id='S2' AND contract_no='C2'"
    ).fetchone()
    assert conflict[0] == "target-terms"

    # Other branch's data never leaked into the target.
    assert _ids(db["branch"].rows, "branch_id") == ["B1"]
    assert db.conn.execute(
        "SELECT 1 FROM supplier WHERE supplier_id='S3'"
    ).fetchone() is None
    assert db.conn.execute(
        "SELECT 1 FROM contract WHERE supplier_id='S3'"
    ).fetchone() is None
    assert db.conn.execute(
        "SELECT 1 FROM receipt WHERE receipt_id='R4'"
    ).fetchone() is None

    blocked = {(item["table"], tuple(item["key"])): item for item in report["blocked"]}
    assert set(blocked) == {("receipt", ("R2",)), ("receipt", ("R5",))}

    r2_reasons = blocked[("receipt", ("R2",))]["reasons"]
    assert any(
        reason["kind"] == "content_mismatch"
        and reason["table"] == "contract"
        and reason["key"] == ["S2", "C2"]
        and reason["differing_columns"] == ["terms"]
        for reason in r2_reasons
    )
    r5_reasons = blocked[("receipt", ("R5",))]["reasons"]
    assert any(
        reason["kind"] == "missing_dependency"
        and reason["table"] == "contract"
        and reason["key"] == ["S9", "C9"]
        for reason in r5_reasons
    )

    # R6 proves composite FKs are judged as a whole group, not column by column.
    assert ("receipt", ("R6",)) not in {
        (item["table"], tuple(item["key"])) for item in report["blocked"]
    }
    assert ("contract", ("S6", "C6")) in {
        (table, tuple(key))
        for table, keys in report["imported"].items()
        for key in keys
    }


def test_restore_blocks_anchor_that_conflicts_in_target(tmpdir):
    source_db = str(tmpdir / "source.db")
    target_db = str(tmpdir / "target.db")
    archive = str(tmpdir / "archive")
    _build_source_db(source_db)
    db = _build_target_db(target_db)
    # R1 already exists in the target with the same key but different content.
    db["contract"].insert(
        {"supplier_id": "S1", "contract_no": "C1", "terms": "net-30"}
    )
    db["receipt"].insert(
        {
            "receipt_id": "R1", "branch_id": "B1", "supplier_id": "S1",
            "contract_no": "C1", "note": "tampered", "cycle_a_id": None,
        }
    )
    db.conn.commit()
    CliRunner().invoke(cli.cli, ["dump", source_db, archive, "--all"])

    report = restore(target_db, archive, "receipt", {"branch_id": "B1"})

    db = sqlite_utils.Database(target_db)
    # The conflicting anchor row is never overwritten.
    row = db.conn.execute(
        "SELECT note FROM receipt WHERE receipt_id='R1'"
    ).fetchone()
    assert row[0] == "tampered"
    blocked = {tuple(item["key"]): item for item in report["blocked"]}
    assert ("R1",) in blocked
    assert any(
        reason["kind"] == "content_mismatch"
        and reason["table"] == "receipt"
        and reason["differing_columns"] == ["note"]
        for reason in blocked[("R1",)]["reasons"]
    )
    # Unrelated receipts still restore normally.
    assert _ids(db["receipt"].rows, "receipt_id") == ["R1", "R3", "R6"]


def test_restore_filtered_cli_report(tmpdir):
    source_db = str(tmpdir / "source.db")
    target_db = str(tmpdir / "target.db")
    archive = str(tmpdir / "archive")
    _build_source_db(source_db)
    _build_target_db(target_db)
    CliRunner().invoke(cli.cli, ["dump", source_db, archive, "--all"])

    result = CliRunner().invoke(
        cli.cli,
        [
            "restore-filtered", target_db, archive,
            "--table", "receipt", "--filter", "branch_id=B1",
        ],
    )
    # Exit code 2 because at least one document was blocked.
    assert result.exit_code == 2
    assert "R2" in result.output
    assert "R5" in result.output
    assert "content_mismatch" in result.output
    assert "missing_dependency" in result.output

    result_json = CliRunner().invoke(
        cli.cli,
        [
            "restore-filtered", target_db, archive,
            "--table", "receipt", "--filter", "branch_id=B1", "--json",
        ],
    )
    # Conflicts/missing deps are never resolved, so the same documents stay
    # blocked on every run; --json only changes the report format, not the code.
    assert result_json.exit_code == 2, result_json.output
    report = json.loads(result_json.output)
    assert report["anchor_table"] == "receipt"
    assert {tuple(item["key"]) for item in report["blocked"]} == {
        ("R2",),
        ("R5",),
    }
    # The second run imports nothing new: restorable rows already matched.
    assert report["imported"] == {}


def test_restore_unknown_filter_column_raises(tmpdir):
    source_db = str(tmpdir / "source.db")
    target_db = str(tmpdir / "target.db")
    archive = str(tmpdir / "archive")
    _build_source_db(source_db)
    _build_target_db(target_db)
    CliRunner().invoke(cli.cli, ["dump", source_db, archive, "--all"])

    with pytest.raises(RestoreError) as excinfo:
        restore(target_db, archive, "receipt", {"nope": "B1"})
    assert "Unknown filter column" in str(excinfo.value)
    assert "nope" in str(excinfo.value)


def test_restore_filter_matching_nothing_imports_nothing(tmpdir):
    source_db = str(tmpdir / "source.db")
    target_db = str(tmpdir / "target.db")
    archive = str(tmpdir / "archive")
    _build_source_db(source_db)
    target = _build_target_db(target_db)
    CliRunner().invoke(cli.cli, ["dump", source_db, archive, "--all"])
    # The fixture deliberately contains a pre-existing dangling contract.
    baseline = target.conn.execute("PRAGMA foreign_key_check").fetchall()

    report = restore(target_db, archive, "receipt", {"branch_id": "NOPE"})

    assert report["blocked"] == []
    assert report["imported"] == {}
    db = sqlite_utils.Database(target_db)
    assert db["receipt"].count == 0
    assert db["supplier"].count == 2  # only the pre-existing target suppliers
    # The restore introduces no new violations beyond the pre-existing one.
    after = db.conn.execute("PRAGMA foreign_key_check").fetchall()
    assert after == baseline
