import json
import pathlib
import sqlite3

import pytest
from click.testing import CliRunner

from sqlite_diffable import cli


SCHEMA = """CREATE TABLE [cells] (
   [id] INTEGER PRIMARY KEY,
   [empty_blob] BLOB,
   [empty_text] TEXT,
   [null_value] TEXT,
   [blob_value] BLOB,
   [text_that_looks_like_blob] TEXT
)"""


@pytest.fixture
def cell_db(tmpdir):
    path = str(tmpdir / "cells.db")
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    conn.execute(
        "INSERT INTO cells VALUES (1, ?, ?, ?, ?, ?)",
        (
            sqlite3.Binary(b""),
            "",
            None,
            sqlite3.Binary(b"\x00\xff"),
            "b''",
        ),
    )
    conn.execute(
        "INSERT INTO cells VALUES (2, ?, ?, ?, ?, ?)",
        (
            sqlite3.Binary(b"abc"),
            "abc",
            None,
            sqlite3.Binary(b"\x00"),
            "b'\\x00'",
        ),
    )
    conn.commit()
    conn.close()
    return path


def _classes(dbpath):
    conn = sqlite3.connect(dbpath)
    try:
        return conn.execute(
            "SELECT typeof(empty_blob), typeof(empty_text), typeof(null_value), "
            "typeof(blob_value), typeof(text_that_looks_like_blob) FROM cells"
        ).fetchall()
    finally:
        conn.close()


def test_dump_preserves_empty_blob_empty_text_and_null(cell_db, tmpdir):
    output_dir = tmpdir / "out"
    result = CliRunner().invoke(cli.cli, ["dump", cell_db, str(output_dir), "cells"])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in (output_dir / "cells.ndjson").open()]
    assert rows[0][1] == {"$sqlite": "blob", "hex": ""}
    assert rows[0][2] == ""
    assert rows[0][3] is None
    assert rows[0][5] == "b''"
    manifest = json.loads((output_dir / ".sqlite-diffable.json").read_text("utf-8"))
    assert manifest["tables"] == {"cells": 2}


def test_round_trip_distinguishes_storage_classes(cell_db, tmpdir):
    output_dir = tmpdir / "out"
    restored = str(tmpdir / "restored.db")
    assert (
        CliRunner()
        .invoke(cli.cli, ["dump", cell_db, str(output_dir), "cells"])
        .exit_code
        == 0
    )
    result = CliRunner().invoke(cli.cli, ["load", restored, str(output_dir)])
    assert result.exit_code == 0, result.output
    assert _classes(restored) == [
        ("blob", "text", "null", "blob", "text"),
        ("blob", "text", "null", "blob", "text"),
    ]
    conn = sqlite3.connect(restored)
    try:
        assert conn.execute(
            "SELECT hex(empty_blob), length(empty_blob), empty_text, "
            "null_value IS NULL FROM cells"
        ).fetchall() == [
            ("", 0, "", 1),
            ("616263", 3, "abc", 1),
        ]
    finally:
        conn.close()


def _legacy_dump(tmpdir, source_db):
    "Write the files the old exporter would have produced for cells.db."
    directory = tmpdir / "legacy"
    directory.mkdir()
    conn = sqlite3.connect(source_db)
    rows = conn.execute(
        "SELECT id, empty_blob, empty_text, null_value, blob_value, "
        "text_that_looks_like_blob FROM cells"
    ).fetchall()
    conn.close()
    ndjson = "\n".join(json.dumps(list(row), default=repr) for row in rows) + "\n"
    (directory / "cells.ndjson").write_text(ndjson, "utf-8")
    (directory / "cells.metadata.json").write_text(
        json.dumps(
            {
                "name": "cells",
                "columns": [
                    "id",
                    "empty_blob",
                    "empty_text",
                    "null_value",
                    "blob_value",
                    "text_that_looks_like_blob",
                ],
                "schema": SCHEMA,
            },
            indent=4,
        ),
        "utf-8",
    )
    return directory


def test_legacy_migration_uses_read_only_source(cell_db, tmpdir):
    directory = _legacy_dump(tmpdir, cell_db)
    restored = str(tmpdir / "restored.db")
    result = CliRunner().invoke(
        cli.cli, ["load", restored, str(directory), "--source", cell_db]
    )
    assert result.exit_code == 0, result.output
    assert _classes(restored) == [
        ("blob", "text", "null", "blob", "text"),
        ("blob", "text", "null", "blob", "text"),
    ]
    # The upgraded file must contain no pending markers.
    assert "pending" not in (directory / "cells.ndjson").read_text("utf-8")


def test_legacy_migration_without_source_leaves_pending(cell_db, tmpdir):
    directory = _legacy_dump(tmpdir, cell_db)
    restored = str(tmpdir / "restored.db")
    result = CliRunner().invoke(cli.cli, ["load", restored, str(directory)])
    assert result.exit_code == 1
    assert "pending" in result.output
    assert not pathlib.Path(restored).exists()
    rows = [json.loads(line) for line in (directory / "cells.ndjson").open()]
    assert rows[0][1]["$sqlite"] == "pending"
    assert rows[0][1]["candidates"][0] == {
        "$sqlite": "blob",
        "hex": "",
    }
    assert rows[0][1]["candidates"][1] == {"$sqlite": "text", "value": "b''"}
    # An unambiguous value is never marked pending.
    assert rows[0][2] == ""


def test_pending_can_be_resolved_later_by_migrate(cell_db, tmpdir):
    directory = _legacy_dump(tmpdir, cell_db)
    result = CliRunner().invoke(cli.cli, ["migrate", str(directory)])
    assert result.exit_code == 0, result.output
    assert "6 cell(s) are pending confirmation" in result.output

    result = CliRunner().invoke(
        cli.cli, ["migrate", str(directory), "--source", cell_db]
    )
    assert result.exit_code == 0, result.output
    assert "pending" not in (directory / "cells.ndjson").read_text("utf-8")

    restored = str(tmpdir / "restored.db")
    result = CliRunner().invoke(cli.cli, ["load", restored, str(directory)])
    assert result.exit_code == 0, result.output
    assert _classes(restored) == [
        ("blob", "text", "null", "blob", "text"),
        ("blob", "text", "null", "blob", "text"),
    ]


def test_pending_can_be_confirmed_by_editing_the_file(tmpdir):
    directory = tmpdir / "hand"
    directory.mkdir()
    (directory / "cells.metadata.json").write_text(
        json.dumps(
            {
                "name": "cells",
                "columns": ["value"],
                "schema": "CREATE TABLE [cells] ([value] BLOB)",
            }
        ),
        "utf-8",
    )
    pending = {
        "$sqlite": "pending",
        "status": "needs-review",
        "legacy": "b'abc'",
        "candidates": [
            {"$sqlite": "blob", "hex": "616263"},
            {"$sqlite": "text", "value": "b'abc'"},
        ],
    }
    (directory / "cells.ndjson").write_text(
        json.dumps([pending]) + "\n",
        "utf-8",
    )
    restored = str(tmpdir / "restored.db")
    result = CliRunner().invoke(cli.cli, ["load", restored, str(directory)])
    assert result.exit_code == 1

    # A human confirms it was a BLOB by replacing the pending marker.
    (directory / "cells.ndjson").write_text(
        json.dumps([{"$sqlite": "blob", "hex": "616263"}]) + "\n",
        "utf-8",
    )
    result = CliRunner().invoke(cli.cli, ["load", restored, str(directory)])
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(restored)
    try:
        assert conn.execute(
            "SELECT typeof(value), hex(value) FROM cells"
        ).fetchone() == (
            "blob",
            "616263",
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "options,expected",
    (
        ([], {"$sqlite": "blob", "hex": ""}),
        (["--legacy"], "b''"),
    ),
)
def test_objects_shows_typed_and_legacy_views(cell_db, tmpdir, options, expected):
    output_dir = tmpdir / "out"
    assert (
        CliRunner()
        .invoke(cli.cli, ["dump", cell_db, str(output_dir), "cells"])
        .exit_code
        == 0
    )
    ndjson = str(output_dir / "cells.ndjson")
    result = CliRunner().invoke(cli.cli, ["objects", ndjson] + options)
    assert result.exit_code == 0, result.output
    first = json.loads(result.output.splitlines()[1])
    assert first["empty_blob"] == expected
    assert first["empty_text"] == ""
    assert first["null_value"] is None
    # A text value that looks like a BLOB stays text in both views.
    assert first["text_that_looks_like_blob"] == "b''"
