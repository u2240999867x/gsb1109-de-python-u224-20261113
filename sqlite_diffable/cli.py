import json
import math
import pathlib
import sqlite3
import sys
import urllib.parse

import click
import sqlite_utils

from . import storage


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
    for table in tables:
        tablename = table.replace("/", "")
        filepath = output / "{}.ndjson".format(tablename)
        metapath = output / "{}.metadata.json".format(tablename)
        # Dump rows to filepath, preserving every cell's SQLite storage class.
        with filepath.open("w") as fp:
            for row in conn[table].rows:
                fp.write(
                    json.dumps([storage.encode_cell(value) for value in row.values()])
                    + "\n"
                )
        # Dump out metadata
        metapath.open("w").write(
            json.dumps(
                {
                    "name": table,
                    "columns": [c.name for c in conn[table].columns],
                    "schema": conn[table].schema,
                },
                indent=4,
            )
        )
        # Record each table as soon as its files are complete: a table
        # containing only scalar cells cannot otherwise be identified as
        # typed if a later table fails.
        storage.write_manifest(output, [table])


def _quote(identifier):
    return '"{}"'.format(str(identifier).replace('"', '""'))


def _read_only_connection(dbpath):
    if dbpath is None:
        return None
    path = pathlib.Path(dbpath).resolve()
    uri = "file:{}?mode=ro".format(urllib.parse.quote(str(path)))
    return sqlite3.connect(uri, uri=True)


def _evidence_rows(conn, table, columns):
    """Read original rows in natural order, with each cell's storage class."""
    sql = "SELECT {} FROM {}".format(
        ", ".join(
            "typeof({}), {}".format(_quote(column), _quote(column))
            for column in columns
        ),
        _quote(table),
    )
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.DatabaseError as ex:
        raise click.ClickException(
            "Could not read table {!r} from the source database: {}".format(table, ex)
        )
    result = []
    for row in rows:
        cells = []
        for index in range(0, len(row), 2):
            type_name = row[index]
            value = row[index + 1]
            if type_name == "blob":
                value = bytes(value)
            cells.append((type_name, value))
        result.append(cells)
    return result


def _same_value(left, right):
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        # repr() distinguishes -0.0 from 0.0, as the old file did.
        return repr(left) == repr(right)
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return type(left) is type(right) and left == right


def _legacy_signature(row):
    # repr() keeps int 1 and float 1.0 distinct while matching exactly the
    # values that json.dumps wrote to the old file.
    return tuple(repr(value) for value in row)


def _evidence_legacy_row(evidence_row):
    return [storage.legacy_value(type_name, value) for type_name, value in evidence_row]


def _rows_match(evidence_rows, legacy_rows):
    if len(evidence_rows) != len(legacy_rows):
        return False
    for evidence_row, legacy_row in zip(evidence_rows, legacy_rows):
        expected = _evidence_legacy_row(evidence_row)
        if len(expected) != len(legacy_row):
            return False
        if not all(_same_value(a, b) for a, b in zip(expected, legacy_row)):
            return False
    return True


def _align_evidence(evidence, legacy_rows):
    """Map each legacy row to an evidence row without relying on rowid.

    The old exporter used the table's natural order.  Usually that is enough.
    Matching the exact legacy representation also copes with an evidence
    database whose rows were reordered or partly deleted.
    """
    if _rows_match(evidence, legacy_rows):
        return list(range(len(evidence)))

    pools = {}
    for index, evidence_row in enumerate(evidence):
        signature = _legacy_signature(_evidence_legacy_row(evidence_row))
        pools.setdefault(signature, []).append(index)

    mapping = []
    for legacy_row in legacy_rows:
        signature = _legacy_signature(legacy_row)
        candidates = pools.get(signature)
        if not candidates:
            return None
        mapping.append(candidates.pop(0))
    return mapping


def _read_legacy_rows(ndjson, columns):
    legacy_rows = []
    with ndjson.open() as fp:
        for line_number, line in enumerate(fp, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, list):
                raise click.ClickException(
                    "Row {} in {} is not a JSON array".format(line_number, ndjson)
                )
            if len(row) != len(columns):
                raise click.ClickException(
                    "Row {} in {} has {} values, expected {}".format(
                        line_number, ndjson, len(row), len(columns)
                    )
                )
            legacy_rows.append(row)
    return legacy_rows


def _convert_legacy_rows(info, ndjson, legacy_rows, evidence_rows):
    """Convert legacy cells to typed cells, leaving ambiguities pending.

    ``evidence_rows`` must already be aligned with ``legacy_rows`` or be None.
    Returns (typed_rows, pending_count).
    """
    columns = info["columns"]
    typed_rows = []
    pending_count = 0
    for row_index, legacy_row in enumerate(legacy_rows):
        evidence_row = evidence_rows[row_index] if evidence_rows is not None else None
        typed_row = []
        for column_index, legacy_value in enumerate(legacy_row):
            blob_bytes = storage.ambiguous_bytes(legacy_value)
            if blob_bytes is None:
                typed_row.append(legacy_value)
                continue
            if evidence_row is None:
                typed_row.append(storage.pending_cell(legacy_value, blob_bytes))
                pending_count += 1
                continue
            type_name, value = evidence_row[column_index]
            expected = storage.legacy_value(type_name, value)
            if not _same_value(expected, legacy_value):
                raise click.ClickException(
                    "{} row {} column {!r} does not match the source "
                    "database; refusing to guess".format(
                        ndjson, row_index + 1, columns[column_index]
                    )
                )
            typed_row.append(
                storage.encode_cell(value) if type_name == "blob" else value
            )
        typed_rows.append(typed_row)
    return typed_rows, pending_count


def _load_evidence(info, source, legacy_rows=None):
    evidence_rows = _evidence_rows(source, info["name"], info["columns"])
    if legacy_rows is not None:
        mapping = _align_evidence(evidence_rows, legacy_rows)
        if mapping is None:
            click.echo(
                "Warning: could not align table {!r} with the source "
                "database; ambiguous cells will be left pending".format(info["name"]),
                err=True,
            )
            return None
        evidence_rows = [evidence_rows[index] for index in mapping]
    return evidence_rows


def _typed_row_as_legacy(row):
    "Reconstruct the row an old exporter would have written for this row."
    legacy_row = []
    for cell in row:
        if storage.is_pending(cell):
            legacy_row.append(storage.pending_legacy_value(cell))
        elif storage.is_blob(cell):
            legacy_row.append(repr(storage.decode_cell(cell)))
        else:
            legacy_row.append(cell)
    return legacy_row


def _align_typed_evidence(info, rows, source):
    legacy_rows = [_typed_row_as_legacy(row) for row in rows]
    return _load_evidence(info, source, legacy_rows)


def _resolve_pending_rows(info, ndjson, typed_rows, evidence_rows):
    """Resolve already-typed pending cells using aligned evidence rows."""
    if evidence_rows is None:
        return sum(1 for row in typed_rows for cell in row if storage.is_pending(cell))
    remaining = 0
    for row_index, row in enumerate(typed_rows):
        evidence_row = evidence_rows[row_index]
        for column_index, cell in enumerate(row):
            if not storage.is_pending(cell):
                continue
            legacy_value = storage.pending_legacy_value(cell)
            type_name, value = evidence_row[column_index]
            expected = storage.legacy_value(type_name, value)
            if not _same_value(expected, legacy_value):
                remaining += 1
                continue
            row[column_index] = (
                storage.encode_cell(value) if type_name == "blob" else value
            )
    return remaining


def _write_rows(ndjson, rows):
    tmp = ndjson.with_name(ndjson.name + ".tmp")
    with tmp.open("w") as fp:
        for row in rows:
            fp.write(json.dumps(row) + "\n")
    tmp.replace(ndjson)


def _migrate_table(metadata, info, source):
    ndjson = storage.ndjson_path(metadata.parent, info["name"])
    legacy_rows = _read_legacy_rows(ndjson, info["columns"])
    evidence_rows = None
    if source is not None:
        evidence_rows = _load_evidence(info, source, legacy_rows)
    typed_rows, pending = _convert_legacy_rows(info, ndjson, legacy_rows, evidence_rows)
    _write_rows(ndjson, typed_rows)
    return pending


def _migrate_directory(directory, source=None):
    directory = pathlib.Path(directory)
    migrated = []
    pending = 0
    for metadata in sorted(directory.glob("*.metadata.json")):
        info = json.loads(metadata.read_text())
        table = info["name"]
        if storage.table_is_typed(directory, table):
            # Record the version even if the manifest was lost, so a table
            # containing only scalar values is not mistaken for a legacy file.
            storage.write_manifest(directory, [table])
            if source is not None:
                pending += _resolve_typed_table(metadata, info, source)
            continue
        pending += _migrate_table(metadata, info, source)
        migrated.append(table)
        storage.write_manifest(directory, [table])
    return migrated, pending


def _resolve_typed_table(metadata, info, source):
    ndjson = storage.ndjson_path(metadata.parent, info["name"])
    with ndjson.open() as fp:
        rows = [json.loads(line) for line in fp if line.strip()]
    pending_before = sum(1 for row in rows for cell in row if storage.is_pending(cell))
    if not pending_before:
        return 0
    evidence_rows = _align_typed_evidence(info, rows, source)
    remaining = _resolve_pending_rows(info, ndjson, rows, evidence_rows)
    if remaining != pending_before:
        _write_rows(ndjson, rows)
    return remaining


def _read_typed_rows(info, ndjson, source, allow_pending):
    with ndjson.open() as fp:
        rows = [json.loads(line) for line in fp if line.strip()]
    if source is not None and any(
        storage.is_pending(cell) for row in rows for cell in row
    ):
        # Resolve in memory only: `load` must not modify the archive.
        evidence_rows = _align_typed_evidence(info, rows, source)
        _resolve_pending_rows(info, ndjson, rows, evidence_rows)
    return _decode_typed_rows(info, rows, allow_pending)


def _decode_typed_rows(info, rows, allow_pending):
    pending_cells = [
        (row_index, column_index)
        for row_index, row in enumerate(rows)
        for column_index, cell in enumerate(row)
        if storage.is_pending(cell)
    ]
    if pending_cells and not allow_pending:
        row_index, column_index = pending_cells[0]
        column = (
            info["columns"][column_index]
            if column_index < len(info["columns"])
            else column_index
        )
        raise click.ClickException(
            "Table {!r} row {} column {!r} is pending confirmation; confirm "
            "the cell, pass --source with the original database, or use "
            "--allow-pending".format(info["name"], row_index + 1, column)
        )

    decoded = []
    for row_index, row in enumerate(rows):
        decoded_row = []
        for column_index, cell in enumerate(row):
            column = (
                info["columns"][column_index]
                if column_index < len(info["columns"])
                else column_index
            )
            try:
                decoded_row.append(
                    storage.decode_cell(
                        cell,
                        table=info["name"],
                        line_number=row_index + 1,
                        column=column,
                    )
                )
            except storage.PendingCellError:
                decoded_row.append(None)
            except storage.UnknownCellError as ex:
                raise click.ClickException(
                    "Unable to decode table {!r} row {} column {!r}: {}".format(
                        info["name"], row_index + 1, column, ex
                    )
                )
        decoded.append(decoded_row)
    return decoded


def _legacy_rows_to_typed(info, ndjson, source):
    legacy_rows = _read_legacy_rows(ndjson, info["columns"])
    evidence_rows = None
    if source is not None:
        evidence_rows = _load_evidence(info, source, legacy_rows)
    return _convert_legacy_rows(info, ndjson, legacy_rows, evidence_rows)


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
@click.option(
    "--source",
    "--source-db",
    "--original",
    "--original-db",
    "--evidence",
    "source",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    help="Read-only original database used to resolve ambiguous legacy cells",
)
@click.option(
    "--allow-pending",
    is_flag=True,
    help="Load pending cells as NULL instead of refusing",
)
def load(dbpath, directory, replace, source, allow_pending):
    """
    Load flat files from a directory into a SQLite database

    Usage:

        sqlite-diffable load my.db dump-location/

    Both the typed NDJSON format and old untyped dumps are accepted. If a
    legacy cell could be either BLOB or TEXT, pass --source pointing at the
    original database (it is opened read-only) to resolve it. Without a source
    such cells are pending and loading fails unless --allow-pending is used.
    """
    directory = pathlib.Path(directory)
    evidence = _read_only_connection(source)
    migrated = []
    tables = []
    try:
        for metadata in sorted(directory.glob("*.metadata.json")):
            info = json.loads(metadata.read_text())
            ndjson = storage.ndjson_path(directory, info["name"])
            if storage.table_is_typed(directory, info["name"]):
                rows = _read_typed_rows(info, ndjson, evidence, allow_pending)
            else:
                rows, pending = _legacy_rows_to_typed(info, ndjson, evidence)
                # Persist the upgraded file, keeping pending markers so the
                # ambiguity remains visible for later confirmation.
                _write_rows(ndjson, rows)
                migrated.append(info["name"])
                if pending and not allow_pending:
                    raise click.ClickException(
                        "{} contains {} pending cell(s); pass --source with "
                        "the original database to resolve them, or use "
                        "--allow-pending".format(ndjson, pending)
                    )
                rows = _decode_rows(info, rows, allow_pending)
            tables.append((info, rows))
    finally:
        if evidence is not None:
            evidence.close()
    if migrated:
        storage.write_manifest(directory, migrated)

    db = sqlite_utils.Database(dbpath)
    for info, rows in tables:
        columns = info["columns"]
        schema = info["schema"]
        if db[info["name"]].exists() and replace:
            db[info["name"]].drop()
        try:
            db.execute(schema)
        except sqlite3.OperationalError as ex:
            msg = str(ex)
            if "already exists" in msg:
                msg += "\n\nUse the --replace option to over-write existing tables"
            raise click.ClickException(msg)
        db[info["name"]].insert_all([dict(zip(columns, row)) for row in rows])


def _decode_rows(info, rows, allow_pending):
    decoded = []
    for row_index, row in enumerate(rows):
        decoded_row = []
        for column_index, cell in enumerate(row):
            column = (
                info["columns"][column_index]
                if column_index < len(info["columns"])
                else column_index
            )
            try:
                decoded_row.append(
                    storage.decode_cell(
                        cell,
                        table=info["name"],
                        line_number=row_index + 1,
                        column=column,
                    )
                )
            except storage.PendingCellError:
                if not allow_pending:
                    raise click.ClickException(
                        "Table {!r} row {} column {!r} is pending; confirm "
                        "the cell or use --allow-pending".format(
                            info["name"], row_index + 1, column
                        )
                    )
                decoded_row.append(None)
        decoded.append(decoded_row)
    return decoded


@cli.command("migrate")
@click.argument(
    "directory",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option(
    "--source",
    "--source-db",
    "--original",
    "--original-db",
    "--evidence",
    "source",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    help="Read-only original database used to resolve ambiguous legacy cells",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit with an error if any cells are left pending",
)
def migrate(directory, source, strict):
    """
    Upgrade legacy untyped .ndjson files to the typed format in place

    Usage:

        sqlite-diffable migrate dump-location/ --source original.db

    Without --source, cells that could be either BLOB or TEXT are left pending
    for confirmation. Those cells block loading until they are resolved.
    """
    directory = pathlib.Path(directory)
    evidence = _read_only_connection(source)
    try:
        migrated, pending = _migrate_directory(directory, evidence)
    finally:
        if evidence is not None:
            evidence.close()
    click.echo("Migrated {} table(s)".format(len(migrated)))
    if pending:
        message = "{} cell(s) are pending confirmation".format(pending)
        if strict:
            raise click.ClickException(message)
        click.echo(message, err=True)


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
@click.option(
    "--legacy",
    is_flag=True,
    help="Render like the old exporter (BLOBs as repr, pending cells as text)",
)
def objects(filepath, output, array, legacy):
    """
    Output rows from a .ndjson file as newline-delimited JSON objects

    Usage:

        sqlite-diffable objects dump-location/mytable.ndjson

    This will read the column names from the accompanying .metadata.json file.
    Use --legacy for the old repr-based display of BLOB values.
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
        if legacy:
            row = [legacy_display(cell) for cell in row]
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


def legacy_display(cell):
    if storage.is_blob(cell):
        return repr(storage.decode_cell(cell))
    if storage.is_pending(cell):
        return storage.pending_legacy_value(cell)
    return cell
