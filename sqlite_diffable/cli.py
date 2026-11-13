import click
import json
import pathlib
import re
import sqlite_utils
import sqlite3
import sys


INTERNAL_TABLES = {"sqlite_sequence"}
AUTOINCREMENT_RE = re.compile(r"\bAUTOINCREMENT\b", re.IGNORECASE)


def _table_uses_autoincrement(schema):
    return bool(AUTOINCREMENT_RE.search(schema))


def _read_sequences(conn):
    try:
        rows = conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {name: seq for name, seq in rows}


@click.group()
@click.version_option()
def cli():
    "Tools for dumping/loading a SQLite database to diffable directory structure"
    pass


@cli.command()
@click.argument(
    "dbpath",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    required=True,
)
@click.argument(
    "output", type=click.Path(file_okay=False, dir_okay=True), required=True
)
@click.argument("tables", nargs=-1, required=False)
@click.option("--all", is_flag=True, help="Dump all tables")
@click.option("--exclude", multiple=True, help="Tables to exclude from the dump")
def dump(dbpath, output, tables, all, exclude):
    """
    Dump a SQLite database out as flat files in the directory

    Usage:

        sqlite-diffable dump my.db output/ --all

    --all dumps every table. Or specify tables like this:

        sqlite-diffable dump my.db output/ entries tags

    Exclude specific tables:

        sqlite-diffable dump my.db output/ --all --exclude table1 --exclude table2
    """
    if not tables and not all:
        raise click.ClickException("You must pass --all or specify some tables")
    output = pathlib.Path(output)
    output.mkdir(exist_ok=True)
    conn = sqlite_utils.Database(dbpath)
    if all:
        tables = set(conn.table_names()) - set(exclude)
    else:
        tables = set(tables) - set(exclude)
    sequences = _read_sequences(conn)
    for table in sorted(tables):
        if table in INTERNAL_TABLES:
            # Internal tables such as sqlite_sequence are archived per-table
            # inside the metadata files instead of being dumped as rows.
            click.echo(
                "Skipping internal table '{}'; AUTOINCREMENT state is "
                "archived in each table's metadata instead.".format(table),
                err=True,
            )
            continue
        tablename = table.replace("/", "")
        filepath = output / "{}.ndjson".format(tablename)
        metapath = output / "{}.metadata.json".format(tablename)
        schema = conn[table].schema
        # Dump rows to filepath
        with filepath.open("w") as fp:
            for row in conn[table].rows:
                fp.write(json.dumps(list(row.values()), default=repr) + "\n")
            fp.close()
        # Archive the AUTOINCREMENT high-water mark so that restored tables
        # keep allocating ids past numbers that were used (and possibly voided)
        # in the original database, even when the table has been emptied.
        metadata = {
            "name": table,
            "columns": [c.name for c in conn[table].columns],
            "schema": schema,
        }
        if _table_uses_autoincrement(schema):
            metadata["autoincrement"] = True
            metadata["sequence"] = sequences.get(table)
        # Dump out metadata
        metapath.open("w").write(json.dumps(metadata, indent=4))


def _quote_ident(name):
    return '"{}"'.format(name.replace('"', '""'))


def _read_legacy_sequences(directory):
    # Dumps written by older versions of this tool exported sqlite_sequence
    # as an ordinary table. Those files cannot be loaded directly (the name
    # is reserved) but the values still describe the old high-water marks.
    legacy_path = directory / "sqlite_sequence.ndjson"
    if not legacy_path.exists():
        return {}
    sequences = {}
    for line in legacy_path.open():
        if not line.strip():
            continue
        name, seq = json.loads(line)
        sequences[name] = seq
    return sequences


def _restore_sequence(db, table, target_seq):
    # sqlite_sequence only exists once an AUTOINCREMENT table has held a row.
    # Upsert the high-water mark without ever lowering it below the rows just
    # restored, so voided numbers of emptied tables are not reissued.
    if target_seq is None:
        return
    current = db.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
    ).fetchone()
    if current is not None and current[0] >= target_seq:
        return
    if current is None:
        db.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
            (table, target_seq),
        )
    else:
        db.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
            (target_seq, table),
        )


@cli.command()
@click.argument(
    "dbpath",
    type=click.Path(file_okay=True, allow_dash=False, dir_okay=False),
)
@click.argument(
    "directory",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option("--replace", is_flag=True, help="Replace tables if they exist already")
def load(dbpath, directory, replace):
    """
    Load flat files from a directory into a SQLite database

    Usage:

        sqlite-diffable load my.db dump-location/
    """
    db = sqlite_utils.Database(dbpath)
    directory = pathlib.Path(directory)
    legacy_sequences = _read_legacy_sequences(directory)
    metadatas = directory.glob("*.metadata.json")
    for metadata in sorted(metadatas):
        info = json.loads(metadata.read_text())
        table = info["name"]
        if table in INTERNAL_TABLES:
            # Restored via the per-table sequence metadata, not as a table.
            continue
        columns = info["columns"]
        schema = info["schema"]
        if db[table].exists() and replace:
            db[table].drop()
        try:
            db.execute(schema)
        except sqlite3.OperationalError as ex:
            msg = str(ex)
            if "already exists" in msg:
                msg += "\n\nUse the --replace option to over-write existing tables"
            raise click.ClickException(msg)
        # Now insert the rows
        ndjson = metadata.parent / metadata.stem.replace(".metadata", ".ndjson")
        rows = (
            dict(zip(columns, json.loads(line)))
            for line in ndjson.open()
            if line.strip()
        )
        db[table].insert_all(rows)
        if not _table_uses_autoincrement(schema):
            continue
        # Restore the AUTOINCREMENT high-water mark. New dumps carry it in
        # the metadata; legacy dumps may carry it in sqlite_sequence.ndjson;
        # otherwise only the largest surviving rowid can be reconstructed.
        archived_seq = info.get("sequence")
        legacy_seq = legacy_sequences.get(table)
        row_max = db.execute(
            "SELECT MAX(rowid) FROM {}".format(_quote_ident(table))
        ).fetchone()[0]
        if "sequence" in info:
            # New archive format: a null value means no sequence existed yet,
            # and the restored rows already determine the starting point.
            target_seq = archived_seq if archived_seq is not None else row_max
        elif legacy_seq is not None:
            target_seq = legacy_seq
        else:
            target_seq = row_max
            click.echo(
                "Warning: no archived AUTOINCREMENT sequence for table "
                "'{}'; restoring only the high-water mark of surviving rows "
                "({}). Numbers assigned to rows that were deleted before the "
                "dump cannot be recovered and may be reissued.".format(
                    table,
                    row_max if row_max is not None else "none",
                ),
                err=True,
            )
        if target_seq is not None and row_max is not None:
            target_seq = max(target_seq, row_max)
        _restore_sequence(db, table, target_seq)
        db.conn.commit()


@cli.command()
@click.argument(
    "filepath",
    type=click.Path(file_okay=True, allow_dash=False, dir_okay=False, exists=True),
)
@click.option(
    "-o",
    "--output",
    type=click.Path(file_okay=True, allow_dash=True, dir_okay=False),
)
@click.option(
    "--array",
    is_flag=True,
    help="Output JSON array instead of newline-delimited objects",
)
def objects(filepath, output, array):
    """
    Output rows from a .ndjson file as newline-delimited JSON objects

    Usage:

        sqlite-diffable objects dump-location/mytable.ndjson

    This will read the column names from the accompanying .metadata.json file.
    """
    if not filepath.endswith(".ndjson"):
        raise click.ClickException("Must be a .ndjson file")
    path = pathlib.Path(filepath)
    metadata = path.parent / (path.stem + ".metadata.json")
    if not metadata.exists():
        raise click.ClickException("No accompanying .metadata.json file")
    # Read the column names
    info = json.loads(metadata.read_text())
    columns = info["columns"]
    # Output the rows
    out = sys.stdout if output is None else open(output, "w")
    if array:
        out.write("[")
    first = True
    for line in path.open():
        row = json.loads(line)
        if array and not first:
            out.write(",\n")
        else:
            out.write("\n")
        out.write(json.dumps(dict(zip(columns, row))))
        first = False
    if array:
        out.write("\n]\n")
    else:
        out.write("\n")
