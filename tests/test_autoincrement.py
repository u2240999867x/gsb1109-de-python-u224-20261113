from click.testing import CliRunner
from sqlite_diffable import cli
import json
import sqlite3
import sqlite_utils


def build_invoice_db(path):
    db = sqlite_utils.Database(str(path))
    db.conn.executescript(
        """
        CREATE TABLE plain_book (id INTEGER PRIMARY KEY, no TEXT);
        CREATE TABLE big_book (id INTEGER PRIMARY KEY, no TEXT);
        CREATE TABLE invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, no TEXT);
        CREATE TABLE voided_book (id INTEGER PRIMARY KEY AUTOINCREMENT, no TEXT);
        """
    )
    # Ordinary integer primary key, never explicitly assigned.
    db.conn.executemany(
        "INSERT INTO plain_book(no) VALUES (?)", [("P-1",), ("P-2",)]
    )
    # Explicitly written large number, later removed. Without AUTOINCREMENT
    # SQLite itself does not remember the gap, so neither can a dump/restore.
    db.conn.execute("INSERT INTO big_book(id, no) VALUES (9000, 'B-BIG')")
    db.conn.execute("DELETE FROM big_book")
    # Issued invoices; the latest one (id 3) was voided. Number 3 must never
    # be handed out again after a restore.
    db.conn.executemany(
        "INSERT INTO invoices(no) VALUES (?)",
        [("I-1",), ("I-2",), ("I-3",)],
    )
    db.conn.execute("DELETE FROM invoices WHERE id = 3")
    # A whole AUTOINCREMENT book emptied: every previously issued number was
    # voided and the table contains no row to infer a high-water mark from.
    db.conn.executemany(
        "INSERT INTO voided_book(no) VALUES (?)",
        [("V-1",), ("V-2",), ("V-3",)],
    )
    db.conn.execute("DELETE FROM voided_book")
    db.conn.commit()
    return db


def continue_invoicing(path):
    # Open a separate database and perform the same next-invoice operation on
    # every book; the successfully allocated numbers are what we compare.
    conn = sqlite3.connect(str(path))
    allocated = {}
    for table in ("plain_book", "big_book", "invoices", "voided_book"):
        conn.execute("INSERT INTO {}(no) VALUES ('NEXT')".format(table))
        allocated[table] = conn.execute(
            "SELECT id FROM {} WHERE no = 'NEXT'".format(table)
        ).fetchone()[0]
    conn.commit()
    conn.close()
    return allocated


def dump_and_load(src_path, tmp_path, extra_load_options=()):
    dump_dir = tmp_path / "dump"
    restored = tmp_path / "restored.db"
    runner = CliRunner()
    result = runner.invoke(
        cli.cli, ["dump", str(src_path), str(dump_dir), "--all"]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli.cli,
        ["load", str(restored), str(dump_dir), *extra_load_options],
    )
    assert result.exit_code == 0, result.output
    return dump_dir, restored, result.output


def test_dump_archives_autoincrement_metadata_not_internal_table(
    tmp_path,
):
    src = tmp_path / "src.db"
    build_invoice_db(src)
    dump_dir, _, _ = dump_and_load(src, tmp_path)

    assert not (dump_dir / "sqlite_sequence.ndjson").exists()
    assert not (dump_dir / "sqlite_sequence.metadata.json").exists()

    invoices_meta = json.loads(
        (dump_dir / "invoices.metadata.json").read_text()
    )
    assert invoices_meta["autoincrement"] is True
    # seq stays at 3 even though row id 3 was voided
    assert invoices_meta["sequence"] == 3

    voided_meta = json.loads(
        (dump_dir / "voided_book.metadata.json").read_text()
    )
    assert voided_meta["autoincrement"] is True
    assert voided_meta["sequence"] == 3

    plain_meta = json.loads(
        (dump_dir / "plain_book.metadata.json").read_text()
    )
    assert "autoincrement" not in plain_meta
    assert "sequence" not in plain_meta

    big_meta = json.loads((dump_dir / "big_book.metadata.json").read_text())
    assert "autoincrement" not in big_meta


def test_restored_database_allocates_identical_next_numbers(tmp_path):
    src = tmp_path / "src.db"
    build_invoice_db(src)

    expected = continue_invoicing(src)
    # Sanity check of the documented SQLite semantics in the source database.
    assert expected == {
        "plain_book": 3,
        "big_book": 1,  # plain PK: deleted explicit number is forgotten
        "invoices": 4,  # voided number 3 is skipped
        "voided_book": 4,  # emptied AUTOINCREMENT book continues after 3
    }

    _, restored, output = dump_and_load(src, tmp_path)
    assert "Warning" not in output

    # Rows are identical line for line before any new invoice is issued.
    src_db = sqlite_utils.Database(str(src))
    restored_db = sqlite_utils.Database(str(restored))
    for table in ("plain_book", "big_book", "invoices", "voided_book"):
        assert list(restored_db[table].rows) == list(src_db[table].rows)

    allocated = continue_invoicing(restored)
    assert allocated == expected


def test_load_replace_keeps_restored_sequence(tmp_path):
    src = tmp_path / "src.db"
    build_invoice_db(src)
    dump_dir, restored, _ = dump_and_load(src, tmp_path)

    result = CliRunner().invoke(
        cli.cli,
        ["load", str(restored), str(dump_dir), "--replace"],
    )
    assert result.exit_code == 0, result.output
    assert continue_invoicing(restored) == {
        "plain_book": 3,
        "big_book": 1,
        "invoices": 4,
        "voided_book": 4,
    }


def test_legacy_dump_with_sqlite_sequence_files_is_recovered(tmp_path):
    # Dumps written by older tool versions exported sqlite_sequence as an
    # ordinary pair of files. Loading them must neither crash on the reserved
    # name nor lose the high-water marks they recorded.
    src = tmp_path / "src.db"
    build_invoice_db(src)
    dump_dir, restored, output = dump_and_load(src, tmp_path)

    legacy_ndjson = dump_dir / "sqlite_sequence.ndjson"
    legacy_meta = dump_dir / "sqlite_sequence.metadata.json"
    legacy_ndjson.write_text('["invoices", 3]\n["voided_book", 3]\n')
    legacy_meta.write_text(
        json.dumps(
            {
                "name": "sqlite_sequence",
                "columns": ["name", "seq"],
                "schema": "CREATE TABLE sqlite_sequence(name,seq)",
            }
        )
    )
    # Strip the new per-table sequence fields to mimic the old metadata.
    for table in ("invoices", "voided_book"):
        meta_path = dump_dir / "{}.metadata.json".format(table)
        info = json.loads(meta_path.read_text())
        info.pop("autoincrement", None)
        info.pop("sequence", None)
        meta_path.write_text(json.dumps(info, indent=4))

    result = CliRunner().invoke(
        cli.cli, ["load", str(restored), str(dump_dir), "--replace"]
    )
    assert result.exit_code == 0, result.output
    assert "Warning" not in result.output
    assert continue_invoicing(restored) == {
        "plain_book": 3,
        "big_book": 1,
        "invoices": 4,
        "voided_book": 4,
    }


def test_dump_skips_named_internal_table_with_message(tmp_path):
    src = tmp_path / "src.db"
    build_invoice_db(src)
    dump_dir = tmp_path / "dump"
    result = CliRunner().invoke(
        cli.cli,
        ["dump", str(src), str(dump_dir), "invoices", "sqlite_sequence"],
    )
    assert result.exit_code == 0, result.output
    assert "Skipping internal table 'sqlite_sequence'" in result.output
    assert (dump_dir / "invoices.ndjson").exists()
    assert not (dump_dir / "sqlite_sequence.ndjson").exists()
    assert not (dump_dir / "sqlite_sequence.metadata.json").exists()


def test_autoincrement_table_that_never_held_rows_starts_at_one(tmp_path):
    src = tmp_path / "src.db"
    db = sqlite_utils.Database(str(src))
    db.conn.execute(
        "CREATE TABLE fresh_book (id INTEGER PRIMARY KEY AUTOINCREMENT, no TEXT)"
    )
    db.conn.commit()

    dump_dir, restored, output = dump_and_load(src, tmp_path)
    meta = json.loads((dump_dir / "fresh_book.metadata.json").read_text())
    assert meta["autoincrement"] is True
    assert meta["sequence"] is None
    assert "Warning" not in output

    conn = sqlite3.connect(str(restored))
    conn.execute("INSERT INTO fresh_book(no) VALUES ('NEXT')")
    assert conn.execute("SELECT id FROM fresh_book").fetchone()[0] == 1
    conn.close()


def test_bare_legacy_directory_only_recovers_surviving_rows(tmp_path):
    # A directory that contains nothing but row files and basic metadata:
    # rows come back exactly, but voided numbers cannot be reconstructed.
    src = tmp_path / "src.db"
    build_invoice_db(src)
    dump_dir, restored, _ = dump_and_load(src, tmp_path)

    for table in ("invoices", "voided_book"):
        meta_path = dump_dir / "{}.metadata.json".format(table)
        info = json.loads(meta_path.read_text())
        info.pop("autoincrement", None)
        info.pop("sequence", None)
        meta_path.write_text(json.dumps(info, indent=4))

    result = CliRunner().invoke(
        cli.cli, ["load", str(restored), str(dump_dir), "--replace"]
    )
    assert result.exit_code == 0, result.output
    assert "no archived AUTOINCREMENT sequence" in result.output
    assert "voided_book" in result.output
    assert "invoices" in result.output

    restored_db = sqlite_utils.Database(str(restored))
    src_db = sqlite_utils.Database(str(src))
    for table in ("plain_book", "big_book", "invoices", "voided_book"):
        assert list(restored_db[table].rows) == list(src_db[table].rows)

    # Document the hard boundary: surviving rows alone give max(rowid), so
    # the emptied book restarts at 1 and the voided invoice number 3 returns.
    assert continue_invoicing(restored) == {
        "plain_book": 3,
        "big_book": 1,
        "invoices": 3,
        "voided_book": 1,
    }
