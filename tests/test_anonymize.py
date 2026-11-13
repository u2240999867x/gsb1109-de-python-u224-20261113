from click.testing import CliRunner
from sqlite_diffable import cli
from sqlite_diffable.anonymize import (
    DECLARATION_FILENAME,
    ISOLATED_PREFIX,
    SURROGATE_PREFIX,
)
import json
import pathlib
import sqlite_utils
import pytest


SALT_A = "aa" * 32
SALT_B = "bb" * 32

SPEC = {
    "tables": {
        "contracts": {
            "keys": [
                ["customer_id"],
                ["region_code", "contract_no"],
            ],
            "identity": ["customer_name", "contact_phone"],
        },
        "settlements": {
            "keys": [
                ["customer_id"],
                ["region_code", "contract_no"],
            ]
        },
    }
}


@pytest.fixture
def contracts_db(tmpdir):
    path = str(tmpdir / "contracts.db")
    db = sqlite_utils.Database(path)
    db["contracts"].insert_all(
        [
            {
                "id": 1,
                "customer_id": "C001",
                "customer_name": "Acme",
                "contact_phone": "111",
                "region_code": "R1",
                "contract_no": "K10",
                "amount": 100,
            },
            {
                "id": 2,
                "customer_id": "C002",
                "customer_name": "Acme",
                "contact_phone": "222",
                "region_code": "R1",
                "contract_no": "K11",
                "amount": 200,
            },
            {
                # Same raw value in two different isolated columns
                "id": 3,
                "customer_id": "C003",
                "customer_name": "Globex",
                "contact_phone": "Globex",
                "region_code": "R2",
                "contract_no": "K12",
                "amount": 300,
            },
        ],
        pk="id",
    )
    db["settlements"].insert_all(
        [
            # Contract 1 is matched twice (per-order match count > 1)
            {
                "id": 1,
                "customer_id": "C001",
                "region_code": "R1",
                "contract_no": "K10",
                "amount": 100,
                "settled_at": "2026-01-01",
            },
            {
                "id": 2,
                "customer_id": "C001",
                "region_code": "R1",
                "contract_no": "K10",
                "amount": 50,
                "settled_at": "2026-02-01",
            },
            {
                "id": 3,
                "customer_id": "C002",
                "region_code": "R1",
                "contract_no": "K11",
                "amount": 200,
                "settled_at": "2026-01-05",
            },
            # Partially NULL composite foreign key: must never match
            {
                "id": 4,
                "customer_id": "C003",
                "region_code": None,
                "contract_no": "K12",
                "amount": 300,
                "settled_at": "2026-01-06",
            },
            # Dangling foreign key: no contract row
            {
                "id": 5,
                "customer_id": "C999",
                "region_code": "R9",
                "contract_no": "K99",
                "amount": 9,
                "settled_at": "2026-01-07",
            },
        ],
        pk="id",
    )
    return path


@pytest.fixture
def spec_path(tmpdir):
    path = tmpdir / "spec.json"
    path.write_text(json.dumps(SPEC), "utf-8")
    return str(path)


def _read_ndjson(directory, table):
    path = pathlib.Path(str(directory)) / "{}.ndjson".format(table)
    return [json.loads(line) for line in path.open() if line.strip()]


def _columns(directory, table):
    path = pathlib.Path(str(directory)) / "{}.metadata.json".format(table)
    return json.loads(path.read_text())["columns"]


def _row_map(directory, table):
    columns = _columns(directory, table)
    return [dict(zip(columns, row)) for row in _read_ndjson(directory, table)]


def _run_anonymize(db, output, spec, partner_id, salt):
    return CliRunner().invoke(
        cli.cli,
        [
            "anonymize",
            db,
            str(output),
            "--spec",
            spec,
            "--partner-id",
            partner_id,
            "--salt",
            salt,
        ],
    )


def test_anonymize_replaces_identity_and_writes_declaration(
    contracts_db, spec_path, tmpdir
):
    output = tmpdir / "export_a"
    result = _run_anonymize(
        contracts_db, output, spec_path, "partner-a", SALT_A
    )
    assert result.exit_code == 0, result.output

    contracts = _row_map(output, "contracts")
    settlements = _row_map(output, "settlements")
    raw_blob = "".join(
        path.read_text()
        for path in pathlib.Path(str(output)).glob("*.ndjson")
    )
    # No direct customer identity may survive anywhere in the export
    for secret in ("C001", "C002", "C003", "Acme", "Globex", "111", "222"):
        assert secret not in raw_blob

    # Surrogates keep the original equi-join: same source id, same token,
    # across the contracts and settlements tables
    contract_by_id = {row["id"]: row for row in contracts}
    settlement_by_id = {row["id"]: row for row in settlements}
    assert contract_by_id[1]["customer_id"] == settlement_by_id[1]["customer_id"]
    assert contract_by_id[1]["customer_id"] == settlement_by_id[2]["customer_id"]
    assert contract_by_id[1]["customer_id"].startswith(SURROGATE_PREFIX)
    # Different source values map to different surrogates
    assert (
        contract_by_id[1]["customer_id"]
        != contract_by_id[2]["customer_id"]
    )

    # Composite key surrogates align component by component across tables
    for column in ("region_code", "contract_no"):
        assert contract_by_id[1][column] == settlement_by_id[1][column]
    assert ":0" in settlement_by_id[1]["region_code"]
    assert ":1" in settlement_by_id[1]["contract_no"]

    # Partially NULL composite key is exported entirely as NULL
    assert settlement_by_id[4]["region_code"] is None
    assert settlement_by_id[4]["contract_no"] is None

    # Isolated columns: stable within one column, but separated per column
    # even when the source values are identical
    acme_name = contract_by_id[1]["customer_name"]
    assert acme_name == contract_by_id[2]["customer_name"]
    assert acme_name.startswith(ISOLATED_PREFIX)
    globex = contract_by_id[3]
    assert globex["customer_name"] != globex["contact_phone"]
    # An isolated value must never collide with a join surrogate
    assert acme_name != contract_by_id[1]["customer_id"]

    # Non-identifying business data passes through unchanged
    assert contract_by_id[1]["amount"] == 100
    assert settlement_by_id[1]["settled_at"] == "2026-01-01"

    declaration = json.loads(
        (pathlib.Path(str(output)) / DECLARATION_FILENAME).read_text()
    )
    assert declaration["partner_id"] == "partner-a"
    surrogate_targets = {
        (item["table"], tuple(item["columns"]))
        for item in declaration["surrogate_fields"]
    }
    assert surrogate_targets == {
        ("contracts", ("customer_id",)),
        ("contracts", ("region_code", "contract_no")),
        ("settlements", ("customer_id",)),
        ("settlements", ("region_code", "contract_no")),
    }
    isolated_targets = {
        (item["table"], item["column"])
        for item in declaration["isolated_fields"]
    }
    assert isolated_targets == {
        ("contracts", "customer_name"),
        ("contracts", "contact_phone"),
    }
    assert declaration["rules"]


def test_partner_directories_cannot_cross_recognise(
    contracts_db, spec_path, tmpdir
):
    export_a = tmpdir / "export_a"
    export_b = tmpdir / "export_b"
    result_a = _run_anonymize(
        contracts_db, export_a, spec_path, "partner-a", SALT_A
    )
    result_b = _run_anonymize(
        contracts_db, export_b, spec_path, "partner-b", SALT_B
    )
    assert result_a.exit_code == 0, result_a.output
    assert result_b.exit_code == 0, result_b.output

    a_contracts = _row_map(export_a, "contracts")
    b_contracts = _row_map(export_b, "contracts")
    for row_a, row_b in zip(a_contracts, b_contracts):
        assert row_a["customer_id"] != row_b["customer_id"]
        assert row_a["region_code"] != row_b["region_code"]
        assert row_a["contract_no"] != row_b["contract_no"]
        assert row_a["customer_name"] != row_b["customer_name"]

    # Re-exporting for the same partner with the same salt is stable
    export_a2 = tmpdir / "export_a2"
    result_a2 = _run_anonymize(
        contracts_db, export_a2, spec_path, "partner-a", SALT_A
    )
    assert result_a2.exit_code == 0, result_a2.output
    assert _read_ndjson(export_a2, "contracts") == _read_ndjson(
        export_a, "contracts"
    )


def test_verify_joins_single_key(contracts_db, spec_path, tmpdir):
    output = tmpdir / "export_a"
    result = _run_anonymize(
        contracts_db, output, spec_path, "partner-a", SALT_A
    )
    assert result.exit_code == 0, result.output
    verify = CliRunner().invoke(
        cli.cli,
        [
            "verify-joins",
            contracts_db,
            str(output),
            "--spec",
            spec_path,
            "--left-table",
            "settlements",
            "--right-table",
            "contracts",
            "--on",
            "customer_id=customer_id",
        ],
    )
    assert verify.exit_code == 0, verify.output
    payload = json.loads(verify.output.split("OK:")[0])
    assert payload["joined_rows"] == 4
    assert payload["left_rows"] == 5
    assert payload["matched_left_rows"] == 4
    assert payload["unmatched_left_rows"] == 1
    assert payload["match_count_histogram"] == {"0": 1, "1": 4}


def test_verify_joins_composite_key_with_nulls(
    contracts_db, spec_path, tmpdir
):
    output = tmpdir / "export_a"
    result = _run_anonymize(
        contracts_db, output, spec_path, "partner-a", SALT_A
    )
    assert result.exit_code == 0, result.output
    verify = CliRunner().invoke(
        cli.cli,
        [
            "verify-joins",
            contracts_db,
            str(output),
            "--spec",
            spec_path,
            "--left-table",
            "settlements",
            "--right-table",
            "contracts",
            "--on",
            "region_code=region_code",
            "--on",
            "contract_no=contract_no",
        ],
    )
    assert verify.exit_code == 0, verify.output
    payload = json.loads(verify.output.split("OK:")[0])
    # The partially-NULL row and the dangling row both stay unmatched,
    # exactly as in the source database
    assert payload["joined_rows"] == 3
    assert payload["matched_left_rows"] == 3
    assert payload["unmatched_left_rows"] == 2
    assert payload["match_count_histogram"] == {"0": 2, "1": 3}


def test_verify_joins_matches_source_per_order_counts(
    contracts_db, spec_path, tmpdir
):
    output = tmpdir / "export_a"
    result = _run_anonymize(
        contracts_db, output, spec_path, "partner-a", SALT_A
    )
    assert result.exit_code == 0, result.output

    def per_order_counts(db_path):
        db = sqlite_utils.Database(db_path)
        rows = db.execute(
            "SELECT s.id, COUNT(c.id) FROM settlements s LEFT JOIN "
            "contracts c ON s.customer_id = c.customer_id GROUP BY s.id "
            "ORDER BY s.id"
        ).fetchall()
        db.close()
        return rows

    source_counts = per_order_counts(contracts_db)
    restored_path = str(tmpdir / "restored.db")
    restored = sqlite_utils.Database(restored_path)
    cli.load_directory(restored, output, replace=True)
    restored.close()
    assert per_order_counts(restored_path) == source_counts
    assert source_counts == [
        (1, 1),
        (2, 1),
        (3, 1),
        (4, 1),
        (5, 0),
    ]


def test_verify_joins_detects_tampered_export(
    contracts_db, spec_path, tmpdir
):
    output = tmpdir / "export_a"
    result = _run_anonymize(
        contracts_db, output, spec_path, "partner-a", SALT_A
    )
    assert result.exit_code == 0, result.output
    ndjson = pathlib.Path(str(output)) / "settlements.ndjson"
    rows = _read_ndjson(output, "settlements")
    columns = _columns(output, "settlements")
    customer_index = columns.index("customer_id")
    rows[0][customer_index] = SURROGATE_PREFIX + "tampered"
    ndjson.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), "utf-8"
    )
    verify = CliRunner().invoke(
        cli.cli,
        [
            "verify-joins",
            contracts_db,
            str(output),
            "--spec",
            spec_path,
            "--left-table",
            "settlements",
            "--right-table",
            "contracts",
            "--on",
            "customer_id=customer_id",
        ],
    )
    assert verify.exit_code == 1
    assert "Join statistics differ" in verify.output


def test_foreign_key_with_different_column_names(tmpdir):
    # Parent key and child foreign key have different column names; the
    # shared explicit group name aligns their surrogate namespaces.
    db_path = str(tmpdir / "different_names.db")
    db = sqlite_utils.Database(db_path)
    db["contracts"].insert_all(
        [{"id": 1, "customer_id": "C001"}], pk="id"
    )
    db["settlements"].insert_all(
        [
            {"id": 1, "cust_id": "C001"},
            {"id": 2, "cust_id": "C001"},
        ],
        pk="id",
    )
    db.close()
    spec = {
        "tables": {
            "contracts": {
                "keys": [
                    {"name": "customer_ref", "columns": ["customer_id"]}
                ]
            },
            "settlements": {
                "keys": [
                    {"name": "customer_ref", "columns": ["cust_id"]}
                ]
            },
        }
    }
    spec_path = tmpdir / "spec.json"
    spec_path.write_text(json.dumps(spec), "utf-8")
    output = tmpdir / "export"
    result = _run_anonymize(
        db_path, output, str(spec_path), "partner-a", SALT_A
    )
    assert result.exit_code == 0, result.output
    contracts = _row_map(output, "contracts")
    settlements = _row_map(output, "settlements")
    assert contracts[0]["customer_id"] == settlements[0]["cust_id"]
    assert contracts[0]["customer_id"] == settlements[1]["cust_id"]
    verify = CliRunner().invoke(
        cli.cli,
        [
            "verify-joins",
            db_path,
            str(output),
            "--spec",
            str(spec_path),
            "--left-table",
            "settlements",
            "--right-table",
            "contracts",
            "--on",
            "cust_id=customer_id",
        ],
    )
    assert verify.exit_code == 0, verify.output
    payload = json.loads(verify.output.split("OK:")[0])
    assert payload["joined_rows"] == 2


def test_anonymize_rejects_unknown_column(contracts_db, tmpdir):
    bad_spec = tmpdir / "bad.json"
    bad_spec.write_text(
        json.dumps(
            {
                "tables": {
                    "contracts": {"keys": [["nonexistent_column"]]}
                }
            }
        ),
        "utf-8",
    )
    result = _run_anonymize(
        contracts_db,
        tmpdir / "out",
        str(bad_spec),
        "partner-a",
        SALT_A,
    )
    assert result.exit_code == 1
    assert "has no key column" in result.output


def test_anonymize_rejects_non_hex_salt(contracts_db, spec_path, tmpdir):
    result = CliRunner().invoke(
        cli.cli,
        [
            "anonymize",
            contracts_db,
            str(tmpdir / "out"),
            "--spec",
            spec_path,
            "--partner-id",
            "partner-a",
            "--salt",
            "not-hex",
        ],
    )
    assert result.exit_code == 1
    assert "hex-encoded" in result.output
