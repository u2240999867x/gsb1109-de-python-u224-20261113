import json

import pytest
from click.testing import CliRunner

from sqlite_diffable import cli, schema_check


BASE = (
    "CREATE TABLE entries ("
    "id INTEGER PRIMARY KEY, "
    "name TEXT NOT NULL, "
    "score INTEGER DEFAULT 0"
    ");"
)


def test_cosmetic_spellings_are_equivalent():
    a = (
        "CREATE TABLE [entries] ([id] INTEGER PRIMARY KEY, "
        "[name] TEXT NOT NULL, [score] INTEGER DEFAULT 0);"
    )
    b = (
        'create table "entries"(id integer primary key, '
        "name text not null, score integer default 0)"
    )
    report = schema_check.check(a, b, ["INSERT INTO entries VALUES (1, 'Ada', 1)"])
    assert report["verdict"] == "equivalent"
    assert report["unsupported"] == []
    assert len(report["statement_diff"]["cosmetic"]) == 1


@pytest.mark.parametrize(
    "definition_a, definition_b, operations",
    [
        # Partial unique index.
        (
            BASE + " CREATE UNIQUE INDEX u ON entries(name) WHERE score > 0;",
            BASE + " CREATE UNIQUE INDEX u ON entries(name);",
            [
                "INSERT INTO entries VALUES (1, 'Ada', 0)",
                {"steps": [
                    "INSERT INTO entries VALUES (1, 'Ada', 0)",
                    "INSERT INTO entries VALUES (2, 'Ada', 0)",
                ]},
            ],
        ),
        # Collation.
        (
            "CREATE TABLE t (name TEXT COLLATE NOCASE);"
            " CREATE UNIQUE INDEX u ON t(name);",
            "CREATE TABLE t (name TEXT);"
            " CREATE UNIQUE INDEX u ON t(name);",
            ["INSERT INTO t VALUES ('A')", "INSERT INTO t VALUES ('a')"],
        ),
        # Trigger WHEN clause.
        (
            "CREATE TABLE t (id INTEGER PRIMARY KEY, qty INTEGER);"
            " CREATE TABLE log (qty INTEGER);"
            " CREATE TRIGGER tr AFTER INSERT ON t WHEN NEW.qty > 0"
            " BEGIN INSERT INTO log VALUES (NEW.qty); END;",
            "CREATE TABLE t (id INTEGER PRIMARY KEY, qty INTEGER);"
            " CREATE TABLE log (qty INTEGER);"
            " CREATE TRIGGER tr AFTER INSERT ON t"
            " BEGIN INSERT INTO log VALUES (NEW.qty); END;",
            ["INSERT INTO t VALUES (1, 0)", "INSERT INTO t VALUES (2, 5)"],
        ),
        # Check constraint.
        (
            "CREATE TABLE t (n INTEGER CHECK (n > 0));",
            "CREATE TABLE t (n INTEGER);",
            ["INSERT INTO t VALUES (-1)"],
        ),
    ],
)
def test_short_sequence_distinguishes(definition_a, definition_b, operations):
    report = schema_check.check(definition_a, definition_b, operations)
    assert report["verdict"] == "different"
    sequence = report["distinguishing_sequence"]
    assert 1 <= sequence["length"] <= 3
    assert sequence["a"] != sequence["b"]
    # The sequence can be replayed verbatim against fresh databases.
    for step in sequence["steps"]:
        assert step["sql"]


def test_trigger_when_sequence_is_minimal():
    definition_a = (
        "CREATE TABLE t (id INTEGER PRIMARY KEY, qty INTEGER);"
        " CREATE TABLE log (qty INTEGER);"
        " CREATE TRIGGER tr AFTER INSERT ON t WHEN NEW.qty > 0"
        " BEGIN INSERT INTO log VALUES (NEW.qty); END;"
    )
    definition_b = (
        "CREATE TABLE t (id INTEGER PRIMARY KEY, qty INTEGER);"
        " CREATE TABLE log (qty INTEGER);"
        " CREATE TRIGGER tr AFTER INSERT ON t"
        " BEGIN INSERT INTO log VALUES (NEW.qty); END;"
    )
    report = schema_check.check(
        definition_a, definition_b, ["INSERT INTO t VALUES (1, 0)"]
    )
    assert report["verdict"] == "different"
    assert report["distinguishing_sequence"]["length"] == 1


def test_virtual_tables_are_compared_when_replayable():
    a = "CREATE VIRTUAL TABLE docs USING fts5(body);"
    b = "CREATE TABLE docs (body TEXT);"
    report = schema_check.check(
        a,
        b,
        [
            "INSERT INTO docs VALUES ('hello world')",
            "SELECT * FROM docs WHERE docs MATCH 'hello'",
        ],
    )
    assert report["verdict"] == "different"
    assert report["unsupported"] == []


def test_clock_dependent_definition_is_listed_and_blocks_equivalence():
    report = schema_check.check(
        "CREATE TABLE t (created TEXT DEFAULT CURRENT_TIMESTAMP);",
        "CREATE TABLE t (created TEXT);",
        ["INSERT INTO t DEFAULT VALUES"],
    )
    assert report["verdict"] == "undecided"
    assert report["unsupported"]
    assert report["unsupported"][0]["reason"] == "UNSTABLE EXPRESSION"
    assert report["blocking"]


def test_temp_object_is_listed_as_out_of_range():
    report = schema_check.check(
        "CREATE TEMP TABLE x (a INTEGER);",
        "CREATE TABLE x (a INTEGER);",
        ["SELECT * FROM x"],
    )
    assert report["verdict"] == "undecided"
    assert {item["reason"] for item in report["unsupported"]} == {"TEMP OBJECT"}


def test_real_difference_is_reported_despite_unsupported_statement():
    # The TEMP statement cannot be replayed, but the UNIQUE constraint
    # difference is demonstrated by a replayed operation and is conclusive.
    report = schema_check.check(
        "CREATE TEMP TABLE scratch (a INTEGER);"
        " CREATE TABLE t (a INTEGER);",
        "CREATE TEMP TABLE scratch (a INTEGER);"
        " CREATE TABLE t (a INTEGER UNIQUE);",
        ["INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (1)"],
    )
    assert report["verdict"] == "different"
    assert report["distinguishing_sequence"]["conclusive"] is True
    assert any(item["reason"] == "TEMP OBJECT" for item in report["unsupported"])


def test_gap_on_one_side_only_does_not_produce_a_false_difference():
    report = schema_check.check(
        "CREATE TABLE t (a INTEGER);",
        "CREATE TEMP TABLE t_shadow (a INTEGER);"
        " CREATE TABLE t (a INTEGER);",
        ["SELECT * FROM t", "SELECT * FROM t_shadow"],
    )
    assert report["verdict"] in {"undecided", "equivalent"}
    if report["distinguishing_sequence"] is not None:
        assert report["distinguishing_sequence"]["conclusive"] is False


def test_metadata_directory_is_read_in_dependency_order(tmp_path):
    dump = tmp_path / "dump"
    dump.mkdir()
    (dump / "z_index.metadata.json").write_text(
        json.dumps({"schema": "CREATE INDEX i ON child(name);"}), "utf-8"
    )
    (dump / "a_child.metadata.json").write_text(
        json.dumps(
            {
                "schema": (
                    "CREATE TABLE child (id INTEGER PRIMARY KEY, name TEXT,"
                    " parent_id INTEGER REFERENCES parent(id));"
                )
            }
        ),
        "utf-8",
    )
    (dump / "m_parent.metadata.json").write_text(
        json.dumps(
            {"schema": "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);"}
        ),
        "utf-8",
    )
    definition = schema_check.load_definition(dump)
    report = schema_check.check(
        definition,
        definition,
        ["INSERT INTO parent (id, name) VALUES (1, 'root')"],
        max_length=1,
    )
    assert report["verdict"] == "equivalent"
    assert report["unsupported"] == []


def test_identical_definitions_are_equivalent():
    report = schema_check.check(
        BASE,
        BASE,
        [
            "INSERT INTO entries VALUES (1, 'Ada', 10)",
            "SELECT * FROM entries",
            "UPDATE entries SET score = score + 1",
        ],
    )
    assert report["verdict"] == "equivalent"
    assert report["distinguishing_sequence"] is None


def test_statement_diff_reports_cosmetic_and_structural():
    a = "CREATE TABLE [t] (a INTEGER);"
    b = 'CREATE TABLE "t" (a INTEGER); CREATE INDEX i ON t(a);'
    report = schema_check.check(a, b, ["SELECT * FROM t"])
    diff = report["statement_diff"]
    assert diff["cosmetic"] == [
        {"a": a, "b": 'CREATE TABLE "t" (a INTEGER);'}
    ]
    assert diff["only_b"] == ["CREATE INDEX i ON t(a);"]


def test_directory_input_reads_metadata_files(tmp_path):
    dump = tmp_path / "dump"
    dump.mkdir()
    (dump / "entries.metadata.json").write_text(
        json.dumps(
            {
                "name": "entries",
                "columns": ["id", "name"],
                "schema": (
                    "CREATE TABLE [entries] "
                    "([id] INTEGER PRIMARY KEY, [name] TEXT)"
                ),
            }
        ),
        "utf-8",
    )
    (dump / "ignored.ndjson").write_text("[1, \"Ada\"]\n", "utf-8")
    definition = schema_check.load_definition(dump)
    assert "[entries]" in definition
    assert "CREATE TABLE" in definition


def test_operations_file_and_param_steps(tmp_path):
    operations = tmp_path / "operations.json"
    operations.write_text(
        json.dumps(
            [
                {"sql": "INSERT INTO t VALUES (?, ?)", "params": [1, "a"]},
                {"steps": [
                    "BEGIN",
                    "INSERT INTO t VALUES (2, 'b')",
                    "COMMIT",
                ]},
            ]
        ),
        "utf-8",
    )
    loaded = schema_check.load_operations(operations)
    normalised = schema_check.normalise_operations(loaded)
    assert normalised[0]["steps"] == [
        {"sql": "INSERT INTO t VALUES (?, ?);", "params": [1, "a"]}
    ]
    assert len(normalised[1]["steps"]) == 3


def test_trigger_body_does_not_break_statement_splitting():
    sql = (
        "CREATE TABLE t (id INTEGER PRIMARY KEY);"
        " CREATE TRIGGER tr AFTER INSERT ON t BEGIN"
        " INSERT INTO t VALUES (NEW.id + 1);"
        " INSERT INTO t VALUES (NEW.id + 2);"
        " END;"
        " CREATE INDEX i ON t(id);"
    )
    statements = schema_check.split_statements(sql)
    assert len(statements) == 3
    assert statements[1].count(";") == 3


def test_cli_returns_one_when_different(tmp_path):
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "a.sql").write_text(BASE, "utf-8")
    (definitions / "b.sql").write_text(
        "CREATE TABLE entries ("
        "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
        "score INTEGER DEFAULT 0);",
        "utf-8",
    )
    result = CliRunner().invoke(
        cli.cli,
        [
            "check",
            str(definitions / "a.sql"),
            str(definitions / "b.sql"),
            "--operation",
            "INSERT INTO entries VALUES (1, 'Ada', 0)",
            "--operation",
            "INSERT INTO entries VALUES (2, 'Ada', 0)",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report["verdict"] == "different"
    assert report["distinguishing_sequence"]["length"] >= 1


def test_cli_returns_zero_for_equivalent(tmp_path):
    path = tmp_path / "schema.sql"
    path.write_text(BASE, "utf-8")
    result = CliRunner().invoke(
        cli.cli,
        [
            "check",
            "sql:CREATE TABLE [entries] ([id] INTEGER PRIMARY KEY, "
            "[name] TEXT NOT NULL, [score] INTEGER DEFAULT 0)",
            str(path),
            "--operation",
            "SELECT * FROM entries",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "EQUIVALENT" in result.output


def test_empty_operation_set_is_rejected_by_cli(tmp_path):
    path = tmp_path / "schema.sql"
    path.write_text(BASE, "utf-8")
    result = CliRunner().invoke(cli.cli, ["check", str(path), str(path)])
    assert result.exit_code != 0
    assert "operation" in result.output.lower()
