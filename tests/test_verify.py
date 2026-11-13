import json
import pathlib
import subprocess

from click.testing import CliRunner

from sqlite_diffable import cli
from sqlite_diffable.verify import render_text, verify_restore


COLUMNS = ["customer_id", "name", "tier", "newsletter", "theme"]
MASTER_COLUMNS = ["customer_id", "name", "tier"]
PREF_COLUMNS = ["customer_id", "newsletter", "theme"]

OLD_ROWS = [
    [1, "Ada", "gold", 1, "dark"],
    [1, "Ada", "gold", 0, "light"],
    [2, "Lin", "silver", None, None],
    [3, "Moe", "bronze", 1, "dark"],
]

MASTER_ROWS = [
    [1, "Ada", "gold"],
    [2, "Lin", "silver"],
    [3, "Moe", "bronze"],
]

PREF_ROWS = [
    [1, 1, "dark"],
    [1, 0, "light"],
    [3, 1, "dark"],
]


def write_export(directory, table, columns, rows):
    directory = pathlib.Path(str(directory))
    directory.mkdir(exist_ok=True)
    (directory / "{}.metadata.json".format(table)).write_text(
        json.dumps(
            {"name": table, "columns": columns, "schema": "CREATE TABLE x"},
            indent=4,
        ),
        encoding="utf-8",
    )
    with (directory / "{}.ndjson".format(table)).open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row) + "\n")


def make_pair(tmpdir, master_rows=MASTER_ROWS, pref_rows=PREF_ROWS, old_rows=OLD_ROWS):
    base = pathlib.Path(str(tmpdir))
    old_dir = base / "old"
    new_dir = base / "new"
    write_export(old_dir, "customers", COLUMNS, old_rows)
    write_export(new_dir, "customers_master", MASTER_COLUMNS, master_rows)
    write_export(new_dir, "customers_pref", PREF_COLUMNS, pref_rows)
    return old_dir, new_dir


def run_verify(old_dir, new_dir, keys=("customer_id",)):
    return verify_restore(
        str(old_dir),
        str(new_dir),
        "customers",
        "customers_master",
        "customers_pref",
        keys,
    )


def test_exact_restore_with_duplicate_keys_and_null_preferences(tmpdir):
    old_dir, new_dir = make_pair(tmpdir)
    report = run_verify(old_dir, new_dir)
    assert report.status == "ok"
    assert report.order_preserved is True
    assert report.missing == []
    assert report.extra == []
    assert report.orphan_preferences == []
    assert report.old_count == 4
    assert report.reconstructed_count == 4
    # customer_id=2 has no preference row at all and must reconnect to nulls
    text = render_text(report)
    assert "OK" in text


def test_reorder_only_is_reported_separately(tmpdir):
    reordered_master = [list(r) for r in reversed(MASTER_ROWS)]
    old_dir, new_dir = make_pair(tmpdir, master_rows=reordered_master)
    report = run_verify(old_dir, new_dir)
    assert report.status == "reordered"
    assert report.order_preserved is False
    assert report.missing == []
    assert report.extra == []
    text = render_text(report)
    assert "仅行序变化" in text

    result = CliRunner().invoke(
        cli.cli,
        [
            "verify",
            str(old_dir),
            str(new_dir),
            "customers",
            "customers_master",
            "customers_pref",
            "--key",
            "customer_id",
        ],
    )
    assert result.exit_code == 0


def test_missing_row_is_detected_with_witness_and_repro(tmpdir):
    # Drop the preference row that backed the second customer_id=1 record
    pref_rows = [[1, 1, "dark"], [3, 1, "dark"]]
    old_dir, new_dir = make_pair(tmpdir, pref_rows=pref_rows)
    report = run_verify(old_dir, new_dir)
    assert report.status == "mismatch"
    assert len(report.missing) == 1
    missing_values, copies = report.missing[0]
    assert copies == 1
    assert dict(zip(COLUMNS, missing_values)) == {
        "customer_id": 1,
        "name": "Ada",
        "tier": "gold",
        "newsletter": 0,
        "theme": "light",
    }
    assert report.extra == []
    witness = report.witnesses[0]
    assert witness["kind"] == "missing"
    assert witness["join_key"] == {"customer_id": 1}
    # Evidence points at concrete lines in the original export files
    original_lines = [item["line"] for item in witness["evidence"]["original"]]
    assert original_lines == [2]
    assert witness["evidence"]["preference_total_matches"] == 1

    result = CliRunner().invoke(
        cli.cli,
        [
            "verify",
            str(old_dir),
            str(new_dir),
            "customers",
            "customers_master",
            "customers_pref",
            "--key",
            "customer_id",
        ],
    )
    assert result.exit_code == 1
    assert "丢失 MISSING" in result.output

    # The embedded repro script must run against the raw exports alone
    completed = subprocess.run(
        ["bash", "-c", witness["repro"]],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "旧导出中该完整记录份数: 1" in completed.stdout
    assert "重连生成该完整记录份数: 0" in completed.stdout
    assert "旧导出同键行数: 2" in completed.stdout


def test_phantom_combination_from_duplicate_preference(tmpdir):
    # An extra preference row for the duplicated key customer_id=1 creates
    # a cross-product combination that never existed in the old table.
    pref_rows = [
        [1, 1, "dark"],
        [1, 1, "dark"],
        [1, 0, "light"],
        [3, 1, "dark"],
    ]
    old_dir, new_dir = make_pair(tmpdir, pref_rows=pref_rows)
    report = run_verify(old_dir, new_dir)
    assert report.status == "mismatch"
    assert report.missing == []
    assert len(report.extra) == 1
    extra_values, copies = report.extra[0]
    assert copies == 1
    assert dict(zip(COLUMNS, extra_values)) == {
        "customer_id": 1,
        "name": "Ada",
        "tier": "gold",
        "newsletter": 1,
        "theme": "dark",
    }
    witness = report.witnesses[0]
    assert witness["kind"] == "extra"
    assert witness["evidence"]["preference"], "preference source line must be cited"
    assert witness["evidence"]["original_total_matches"] == 2

    completed = subprocess.run(
        ["bash", "-c", witness["repro"]],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "旧导出中该完整记录份数: 1" in completed.stdout
    assert "重连生成该完整记录份数: 2" in completed.stdout


def test_duplicated_master_rows_inflate_combinations(tmpdir):
    # Master itself is not guaranteed unique either: two master rows with
    # the same key times two preference rows yield four combinations,
    # while the original table only had two.
    old_rows = [
        [5, "Vera", "silver", 1, "dark"],
        [5, "Vera", "silver", 0, "light"],
    ]
    master_rows = [
        [5, "Vera", "silver"],
        [5, "Vera", "silver"],
    ]
    pref_rows = [
        [5, 1, "dark"],
        [5, 0, "light"],
    ]
    old_dir, new_dir = make_pair(
        tmpdir, master_rows=master_rows, pref_rows=pref_rows, old_rows=old_rows
    )
    report = run_verify(old_dir, new_dir)
    assert report.status == "mismatch"
    assert report.missing == []
    assert sum(copies for _, copies in report.extra) == 2
    assert report.reconstructed_count == 4


def test_duplicate_full_rows_are_counted_not_collapsed(tmpdir):
    # Identical rows with a non-unique key must be compared as a multiset
    old_rows = [
        [7, "Pat", "gold", 1, "dark"],
        [7, "Pat", "gold", 1, "dark"],
    ]
    master_rows = [[7, "Pat", "gold"]]
    pref_rows = [[7, 1, "dark"], [7, 1, "dark"]]
    old_dir, new_dir = make_pair(
        tmpdir, master_rows=master_rows, pref_rows=pref_rows, old_rows=old_rows
    )
    report = run_verify(old_dir, new_dir)
    assert report.status == "ok"

    # Removing one duplicate copy must be detected even though a set
    # comparison would consider the tables equal
    old_dir2, new_dir2 = make_pair(
        tmpdir,
        master_rows=master_rows,
        pref_rows=[[7, 1, "dark"]],
        old_rows=old_rows,
    )
    report2 = run_verify(old_dir2, new_dir2)
    assert report2.status == "mismatch"
    assert len(report2.missing) == 1
    assert report2.missing[0][1] == 1


def test_orphan_preference_row_is_reported(tmpdir):
    pref_rows = PREF_ROWS + [[99, 1, "dark"]]
    old_dir, new_dir = make_pair(tmpdir, pref_rows=pref_rows)
    report = run_verify(old_dir, new_dir)
    assert report.status == "mismatch"
    assert len(report.orphan_preferences) == 1
    orphan = report.orphan_preferences[0]
    assert orphan["table"] == "customers_pref"
    assert orphan["line"] == 4
    assert orphan["join_key"] == {"customer_id": 99}
    text = render_text(report)
    assert "悬空偏好行" in text


def test_composite_join_keys(tmpdir):
    columns = ["customer_id", "region", "name", "newsletter"]
    master_columns = ["customer_id", "region", "name"]
    pref_columns = ["customer_id", "region", "newsletter"]
    old_rows = [
        [1, "eu", "Ada", 1],
        [1, "ap", "Ada", 0],
        [2, "eu", "Lin", None],
    ]
    master_rows = [[1, "eu", "Ada"], [1, "ap", "Ada"], [2, "eu", "Lin"]]
    pref_rows = [[1, "eu", 1], [1, "ap", 0]]
    old_dir = tmpdir / "old"
    new_dir = tmpdir / "new"
    write_export(old_dir, "customers", columns, old_rows)
    write_export(new_dir, "customers_master", master_columns, master_rows)
    write_export(new_dir, "customers_pref", pref_columns, pref_rows)
    report = verify_restore(
        str(old_dir),
        str(new_dir),
        "customers",
        "customers_master",
        "customers_pref",
        ("customer_id", "region"),
    )
    assert report.status == "ok"
    assert report.join_keys == ["customer_id", "region"]


def test_json_report_and_missing_key_column(tmpdir):
    old_dir, new_dir = make_pair(tmpdir)
    result = CliRunner().invoke(
        cli.cli,
        [
            "verify",
            str(old_dir),
            str(new_dir),
            "customers",
            "customers_master",
            "customers_pref",
            "--key",
            "customer_id",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["counts"] == {
        "original": 4,
        "master": 3,
        "preference": 3,
        "reconnected": 4,
        "orphan_preferences": 0,
    }

    result = CliRunner().invoke(
        cli.cli,
        [
            "verify",
            str(old_dir),
            str(new_dir),
            "customers",
            "customers_master",
            "customers_pref",
            "--key",
            "nonexistent",
        ],
    )
    assert result.exit_code == 1
    assert "nonexistent" in result.output


def test_output_file(tmpdir):
    old_dir, new_dir = make_pair(tmpdir)
    target = tmpdir / "report.txt"
    result = CliRunner().invoke(
        cli.cli,
        [
            "verify",
            str(old_dir),
            str(new_dir),
            "customers",
            "customers_master",
            "customers_pref",
            "--key",
            "customer_id",
            "-o",
            str(target),
        ],
    )
    assert result.exit_code == 0
    assert target.exists()
    assert "还原核查报告" in target.read_text(encoding="utf-8")
