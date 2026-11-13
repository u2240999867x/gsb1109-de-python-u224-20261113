import json
import pathlib
from click.testing import CliRunner
from sqlite_diffable import cli
import sqlite_utils
import sqlite3


SCHEMA = "CREATE TABLE chairs (model TEXT, color TEXT, qty INTEGER, payload BLOB)"


def make_db(path, rows):
    db = sqlite_utils.Database(str(path))
    db.conn.execute(SCHEMA)
    db.conn.executemany(
        "INSERT INTO chairs (model, color, qty, payload) VALUES (?, ?, ?, ?)", rows
    )
    db.conn.commit()
    return str(path)


def counts(path):
    db = sqlite_utils.Database(str(path))
    return {
        (row["model"], row["color"]): row["n"]
        for row in db.query(
            "select model, color, count(*) as n from chairs group by model, color"
        )
    }


def dump(dbpath, outdir, *args):
    result = CliRunner().invoke(
        cli.cli, ["dump", str(dbpath), str(outdir), "chairs"] + list(args)
    )
    assert result.exit_code == 0, result.output
    return pathlib.Path(str(outdir))


OLD_ROWS = [
    ("A", "red", 1, b"\x00\xff"),
    ("A", "red", 1, b"\x00\xff"),
    ("A", "blue", 2, b""),
    ("B", "red", 3, None),
]
NEW_ROWS = [
    ("A", "red", 1, b"\x00\xff"),
    ("A", "blue", 2, b""),
    ("A", "blue", 2, b""),
    ("B", "red", 3, None),
    ("B", "red", 4, "b'\\x00'"),
]


def test_blob_null_and_text_are_storage_classes(tmpdir):
    old_db = make_db(tmpdir / "old.db", OLD_ROWS)
    new_dir = dump(make_db(tmpdir / "new.db", NEW_ROWS), tmpdir / "new")
    old_dir = dump(old_db, tmpdir / "old")

    result = CliRunner().invoke(cli.cli, ["diff", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    patch = json.loads(result.output)
    table = patch["tables"]["chairs"]
    assert table["removed"] == [
        {"row": ["A", "red", 1, {"$sqlite": "blob", "hex": "00ff"}], "count": 1}
    ]
    assert sorted(item["count"] for item in table["added"]) == [1, 1]
    empty_blob = {"row": ["A", "blue", 2, {"$sqlite": "blob", "hex": ""}], "count": 1}
    assert empty_blob in table["added"]
    assert any(item["row"][3] == "b'\\x00'" for item in table["added"])


def test_reordering_is_not_a_change(tmpdir):
    old_db = make_db(tmpdir / "old.db", OLD_ROWS)
    new_db = make_db(tmpdir / "new.db", list(reversed(OLD_ROWS)))
    old_dir = dump(old_db, tmpdir / "old")
    new_dir = dump(new_db, tmpdir / "new")
    result = CliRunner().invoke(cli.cli, ["diff", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    patch = json.loads(result.output)
    assert patch["changed"] is False, patch
    assert patch["tables"] == {}
    assert patch["reordered_only"] is True


def test_diff_does_not_pair_by_rowid(tmpdir):
    # Same rows, but inserted in a different order so rowids are permuted.
    old = [("A", "red", 1, b"\x01"), ("B", "red", 2, None)]
    new = [("B", "red", 2, None), ("A", "red", 1, b"\x01")]
    old_dir = dump(make_db(tmpdir / "old.db", old), tmpdir / "old")
    new_dir = dump(make_db(tmpdir / "new.db", new), tmpdir / "new")
    result = CliRunner().invoke(cli.cli, ["diff", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["changed"] is False


def test_apply_then_load_matches_new_counts(tmpdir):
    old_db = make_db(tmpdir / "old.db", OLD_ROWS)
    new_db = make_db(tmpdir / "new.db", NEW_ROWS)
    expected = counts(new_db)
    old_dir = dump(old_db, tmpdir / "old")
    new_dir = dump(new_db, tmpdir / "new")

    patch_path = tmpdir / "chairs.patch.json"
    result = CliRunner().invoke(
        cli.cli, ["diff", str(old_dir), str(new_dir), "-o", str(patch_path)]
    )
    assert result.exit_code == 0, result.output
    patch = json.loads(pathlib.Path(str(patch_path)).read_text())
    result = CliRunner().invoke(cli.cli, ["apply", str(old_dir), str(patch_path)])
    assert result.exit_code == 0, result.output

    restored = tmpdir / "restored.db"
    result = CliRunner().invoke(cli.cli, ["load", str(restored), str(old_dir)])
    assert result.exit_code == 0, result.output
    assert counts(restored) == expected

    db = sqlite_utils.Database(str(restored))
    assert [row["typeof"] for row in db.query(
        "select typeof(payload) as typeof from chairs order by rowid"
    )].count("blob") == 3


def test_apply_is_idempotent(tmpdir):
    old_dir = dump(make_db(tmpdir / "old.db", OLD_ROWS), tmpdir / "old")
    new_dir = dump(make_db(tmpdir / "new.db", NEW_ROWS), tmpdir / "new")
    result = CliRunner().invoke(cli.cli, ["sync", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    before = (old_dir / "chairs.ndjson").read_text()
    result = CliRunner().invoke(cli.cli, ["sync", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    assert (old_dir / "chairs.ndjson").read_text() == before


def test_sync_makes_counts_equal(tmpdir):
    old_dir = dump(make_db(tmpdir / "old.db", OLD_ROWS), tmpdir / "old")
    new_db = make_db(tmpdir / "new.db", NEW_ROWS)
    new_dir = dump(new_db, tmpdir / "new")
    result = CliRunner().invoke(cli.cli, ["sync", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    restored = tmpdir / "restored.db"
    result = CliRunner().invoke(cli.cli, ["load", str(restored), str(old_dir)])
    assert result.exit_code == 0, result.output
    assert counts(restored) == counts(new_db)


def test_load_roundtrips_typed_values(tmpdir):
    rows = [
        ("A", "red", 1, b""),
        ("A", "red", 1, None),
        ("A", "red", 1, ""),
        ("A", "red", 1, "b''"),
    ]
    src = make_db(tmpdir / "src.db", rows)
    out = dump(src, tmpdir / "out")
    restored = tmpdir / "restored.db"
    result = CliRunner().invoke(cli.cli, ["load", str(restored), str(out)])
    assert result.exit_code == 0, result.output
    db = sqlite_utils.Database(str(restored))
    assert [
        tuple(row)
        for row in db.conn.execute("select typeof(payload), quote(payload) from chairs")
    ] == [
        ("blob", "X''"),
        ("null", "NULL"),
        ("text", "''"),
        ("text", "'b'''''"),
    ]


def test_apply_from_directory_is_idempotent(tmpdir):
    old_dir = dump(make_db(tmpdir / "old.db", OLD_ROWS), tmpdir / "old")
    new_dir = dump(make_db(tmpdir / "new.db", NEW_ROWS), tmpdir / "new")
    result = CliRunner().invoke(cli.cli, ["apply", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    first = sorted((old_dir / "chairs.ndjson").read_text().splitlines())
    result = CliRunner().invoke(cli.cli, ["apply", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    assert sorted((old_dir / "chairs.ndjson").read_text().splitlines()) == first


def test_diff_check_exits_one_on_change(tmpdir):
    old_dir = dump(make_db(tmpdir / "old.db", OLD_ROWS), tmpdir / "old")
    new_dir = dump(make_db(tmpdir / "new.db", NEW_ROWS), tmpdir / "new")
    result = CliRunner().invoke(cli.cli, ["diff", str(old_dir), str(new_dir), "--check"])
    assert result.exit_code == 1, result.output


def test_new_table_and_deleted_table(tmpdir):
    from click.testing import CliRunner
    import sqlite_utils

    def table_db(path, tables):
        db = sqlite_utils.Database(str(path))
        for name, rows in tables.items():
            db.conn.execute("CREATE TABLE [{}] (x TEXT)".format(name))
            db.conn.executemany(
                "INSERT INTO [{}] VALUES (?)".format(name), [(row,) for row in rows]
            )
        db.conn.commit()
        return str(path)

    old_dir = tmpdir / "old"
    new_dir = tmpdir / "new"
    CliRunner().invoke(cli.cli, ["dump", table_db(tmpdir / "o.db", {"gone": ["a"]}), str(old_dir), "--all"])
    CliRunner().invoke(cli.cli, ["dump", table_db(tmpdir / "n.db", {"new": ["b", "b"]}), str(new_dir), "--all"])
    result = CliRunner().invoke(cli.cli, ["sync", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    restore_db = tmpdir / "restore.db"
    result = CliRunner().invoke(cli.cli, ["load", str(restore_db), str(old_dir)])
    assert result.exit_code == 0, result.output
    db = sqlite_utils.Database(str(restore_db))
    assert db.table_names() == ["new"]
    assert db["new"].count == 2


def test_integer_and_real_are_distinct(tmpdir):
    def make(path, values):
        import sqlite_utils

        db = sqlite_utils.Database(str(path))
        db.conn.execute("CREATE TABLE nums (v)")
        db.conn.executemany("INSERT INTO nums VALUES (?)", [(v,) for v in values])
        db.conn.commit()
        return str(path)

    old_dir = tmpdir / "old"
    new_dir = tmpdir / "new"
    CliRunner().invoke(cli.cli, ["dump", make(tmpdir / "o.db", [1]), str(old_dir), "--all"])
    CliRunner().invoke(cli.cli, ["dump", make(tmpdir / "n.db", [1.0]), str(new_dir), "--all"])
    result = CliRunner().invoke(cli.cli, ["diff", str(old_dir), str(new_dir)])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)["summary"]["nums"]
    assert summary["removed"] == 1 and summary["added"] == 1
