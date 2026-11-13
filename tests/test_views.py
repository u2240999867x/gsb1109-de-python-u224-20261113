import json
import pathlib
import sqlite3

from click.testing import CliRunner

from sqlite_diffable import cli


def _rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _columns(path, name):
    conn = sqlite3.connect(path)
    try:
        return [
            row[1]
            for row in conn.execute(
                'PRAGMA table_info("{}")'.format(name.replace('"', '""'))
            ).fetchall()
        ]
    finally:
        conn.close()


def _make_fixture(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, active INTEGER);
        CREATE TABLE visits (id INTEGER PRIMARY KEY, person_id INTEGER, page TEXT);
        CREATE TABLE ledger (id INTEGER PRIMARY KEY, event TEXT);
        INSERT INTO people VALUES (1, 'Ada', 1), (2, 'Grace', 0), (3, 'Linus', 1);
        INSERT INTO visits VALUES
            (1, 1, '/a'), (2, 1, '/a'), (3, 1, '/b'),
            (4, 2, '/a'), (5, 3, '/c');
        INSERT INTO ledger VALUES (1, 'seed');
        CREATE VIEW active_people AS
            SELECT id, name FROM people WHERE active = 1;
        CREATE VIEW active_visits AS
            SELECT p.id AS person_id, p.name, v.page
            FROM active_people p JOIN visits v ON p.id = v.person_id;
        CREATE VIEW page_totals AS
            SELECT page, count(*) AS hits FROM active_visits GROUP BY page;
        -- This CTE alias deliberately has the same name as the real ledger
        -- table; it must not become a dependency.
        CREATE VIEW event_alias AS
            WITH ledger AS (SELECT * FROM visits)
            SELECT id, page FROM ledger;
        CREATE TRIGGER people_audit AFTER INSERT ON people
        BEGIN
            INSERT INTO ledger VALUES (new.id, 'person_inserted');
        END;
        """
    )
    conn.commit()
    conn.close()


def test_dump_and_restore_view_closure_without_snapshot(tmpdir):
    source = str(tmpdir / "source.db")
    output = tmpdir / "dump"
    restored = str(tmpdir / "restored.db")
    _make_fixture(source)

    result = CliRunner().invoke(cli.cli, ["dump", source, str(output), "--all"])
    assert result.exit_code == 0, result.output

    # A view is stored as schema metadata only, never as stored rows.
    assert (output / "page_totals.metadata.json").exists()
    assert not (output / "page_totals.ndjson").exists()
    metadata = json.loads((output / "page_totals.metadata.json").read_text("utf-8"))
    assert metadata["type"] == "view"
    assert metadata["schema"].startswith("CREATE VIEW page_totals")

    result = CliRunner().invoke(
        cli.cli, ["load", restored, str(output), "--view", "page_totals"]
    )
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(restored)
    try:
        objects = {
            row[0]: row[1]
            for row in conn.execute("SELECT name, type FROM sqlite_master").fetchall()
        }
    finally:
        conn.close()
    assert objects == {
        "people": "table",
        "visits": "table",
        "active_people": "view",
        "active_visits": "view",
        "page_totals": "view",
    }
    # The maintenance trigger and the unrelated ledger table are not part of
    # a read-only view query.
    assert "ledger" not in objects
    assert "people_audit" not in objects

    expected = _rows(
        source,
        "SELECT page, hits FROM page_totals ORDER BY page",
    )
    assert expected == [("/a", 2), ("/b", 1), ("/c", 1)]
    assert (
        _rows(restored, "SELECT page, hits FROM page_totals ORDER BY page") == expected
    )
    assert _columns(restored, "page_totals") == _columns(source, "page_totals")


def test_restored_view_recomputes_after_base_table_change(tmpdir):
    source = str(tmpdir / "source.db")
    output = tmpdir / "dump"
    restored = str(tmpdir / "restored.db")
    _make_fixture(source)
    assert (
        CliRunner().invoke(cli.cli, ["dump", source, str(output), "--all"]).exit_code
        == 0
    )
    result = CliRunner().invoke(
        cli.cli, ["load", restored, str(output), "--view", "page_totals"]
    )
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(restored)
    try:
        conn.execute("INSERT INTO visits VALUES (6, 3, '/d')")
        conn.commit()
        rows = conn.execute(
            "SELECT page, hits FROM page_totals ORDER BY page"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("/a", 2), ("/b", 1), ("/c", 1), ("/d", 1)]


def test_cte_alias_shadowing_real_table_is_not_a_dependency(tmpdir):
    source = str(tmpdir / "source.db")
    output = tmpdir / "dump"
    restored = str(tmpdir / "restored.db")
    _make_fixture(source)
    assert (
        CliRunner().invoke(cli.cli, ["dump", source, str(output), "--all"]).exit_code
        == 0
    )
    result = CliRunner().invoke(
        cli.cli, ["load", restored, str(output), "--view", "event_alias"]
    )
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(restored)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
    finally:
        conn.close()
    assert names == {"visits", "event_alias"}
    assert _rows(restored, "SELECT id, page FROM event_alias ORDER BY id") == _rows(
        source, "SELECT id, page FROM event_alias ORDER BY id"
    )


def test_missing_base_table_reports_the_reference_chain(tmpdir):
    output = tmpdir / "dump"
    output.mkdir()
    # Only a fragment of the dependency chain is available: short_base is
    # missing below top_view -> middle_view.
    (output / "middle_view.metadata.json").write_text(
        json.dumps(
            {
                "name": "middle_view",
                "columns": ["id"],
                "type": "view",
                "schema": "CREATE VIEW middle_view AS SELECT id FROM short_base",
            }
        ),
        "utf-8",
    )
    (output / "top_view.metadata.json").write_text(
        json.dumps(
            {
                "name": "top_view",
                "columns": ["id"],
                "type": "view",
                "schema": "CREATE VIEW top_view AS SELECT id FROM middle_view",
            }
        ),
        "utf-8",
    )
    restored = str(tmpdir / "restored.db")
    result = CliRunner().invoke(
        cli.cli, ["load", restored, str(output), "--view", "top_view"]
    )
    assert result.exit_code == 1
    assert "top_view -> middle_view -> short_base" in result.output


def test_load_without_view_selector_restores_everything(tmpdir):
    source = str(tmpdir / "source.db")
    output = tmpdir / "dump"
    restored = str(tmpdir / "restored.db")
    _make_fixture(source)
    assert (
        CliRunner().invoke(cli.cli, ["dump", source, str(output), "--all"]).exit_code
        == 0
    )
    result = CliRunner().invoke(cli.cli, ["load", restored, str(output)])
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(restored)
    try:
        kinds = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert kinds == {
        "people": "table",
        "visits": "table",
        "ledger": "table",
        "active_people": "view",
        "active_visits": "view",
        "page_totals": "view",
        "event_alias": "view",
    }
    # Optional maintenance triggers stay out unless explicitly requested.
    assert "people_audit" not in kinds


def test_dump_view_dumps_only_its_closure(tmpdir):
    source = str(tmpdir / "source.db")
    output = tmpdir / "dump"
    _make_fixture(source)
    result = CliRunner().invoke(
        cli.cli, ["dump-view", source, str(output), "page_totals"]
    )
    assert result.exit_code == 0, result.output
    names = {pathlib.Path(path).name for path in output.listdir()}
    assert names == {
        "people.metadata.json",
        "people.ndjson",
        "visits.metadata.json",
        "visits.ndjson",
        "active_people.metadata.json",
        "active_visits.metadata.json",
        "page_totals.metadata.json",
    }

    result = CliRunner().invoke(
        cli.cli, ["view-dependencies", str(output), "page_totals"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "people",
        "active_people",
        "visits",
        "active_visits",
        "page_totals",
    ]


def test_dump_view_rejects_tables_and_load_accepts_positional_view(tmpdir):
    source = str(tmpdir / "source.db")
    output = tmpdir / "dump"
    _make_fixture(source)
    result = CliRunner().invoke(cli.cli, ["dump-view", source, str(output), "people"])
    assert result.exit_code == 1
    assert "Not a view: people" in result.output

    CliRunner().invoke(cli.cli, ["dump", source, str(output), "--all"])
    restored = str(tmpdir / "restored.db")
    result = CliRunner().invoke(cli.cli, ["load", restored, str(output), "page_totals"])
    assert result.exit_code == 0, result.output
    assert _rows(restored, "SELECT page, hits FROM page_totals ORDER BY page") == [
        ("/a", 2),
        ("/b", 1),
        ("/c", 1),
    ]


def test_triggers_are_dumped_but_not_part_of_view_restore(tmpdir):
    source = str(tmpdir / "source.db")
    output = tmpdir / "dump"
    restored = str(tmpdir / "restored.db")
    _make_fixture(source)
    result = CliRunner().invoke(
        cli.cli, ["dump", source, str(output), "--all", "--triggers"]
    )
    assert result.exit_code == 0, result.output
    trigger_metadata = json.loads(
        (output / "people_audit.metadata.json").read_text("utf-8")
    )
    assert trigger_metadata["type"] == "trigger"
    assert "CREATE TRIGGER" in trigger_metadata["schema"]

    result = CliRunner().invoke(
        cli.cli, ["load", restored, str(output), "--view", "page_totals"]
    )
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(restored)
    try:
        triggers = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'trigger'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert triggers == 0

    restored_with_triggers = str(tmpdir / "restored_triggers.db")
    result = CliRunner().invoke(
        cli.cli,
        [
            "load",
            restored_with_triggers,
            str(output),
            "--view",
            "page_totals",
            "--triggers",
        ],
    )
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(restored_with_triggers)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert names == {"people_audit"}


def test_restore_view_command_without_dump_view(tmpdir):
    source = str(tmpdir / "source.db")
    output = tmpdir / "dump"
    restored = str(tmpdir / "restored.db")
    _make_fixture(source)
    assert (
        CliRunner().invoke(cli.cli, ["dump", source, str(output), "--all"]).exit_code
        == 0
    )
    result = CliRunner().invoke(
        cli.cli, ["restore-view", restored, str(output), "page_totals"]
    )
    assert result.exit_code == 0, result.output
    assert _rows(restored, "SELECT page, hits FROM page_totals ORDER BY page") == [
        ("/a", 2),
        ("/b", 1),
        ("/c", 1),
    ]
