import click
import json
import pathlib
import sqlite_utils
import sqlite3
import sys
import tempfile

from .anonymize import (
    DECLARATION_FILENAME,
    Pseudonymizer,
    SpecError,
    build_declaration,
    load_spec,
    normalize_key_group,
    random_salt,
    table_rules,
    transform_row,
    validate_columns,
    write_declaration,
)


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
        # Dump rows to filepath
        with filepath.open("w") as fp:
            for row in conn[table].rows:
                fp.write(json.dumps(list(row.values()), default=repr) + "\n")
            fp.close()
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


def load_directory(db, directory, replace=False):
    directory = pathlib.Path(directory)
    metadatas = directory.glob("*.metadata.json")
    for metadata in metadatas:
        info = json.loads(metadata.read_text())
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
        # Now insert the rows
        ndjson = metadata.parent / metadata.stem.replace(".metadata", ".ndjson")
        rows = (
            dict(zip(columns, json.loads(line)))
            for line in ndjson.open()
            if line.strip()
        )
        db[info["name"]].insert_all(rows)


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
    load_directory(db, directory, replace)


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


@cli.command()
@click.argument(
    "dbpath",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    required=True,
)
@click.argument(
    "output", type=click.Path(file_okay=False, dir_okay=True), required=True
)
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    required=True,
    help="JSON spec describing keys (surrogate identities) and identity columns",
)
@click.option(
    "--partner-id",
    required=True,
    help="Identifier of the partner receiving this export",
)
@click.option(
    "--salt",
    envvar="SQLITE_DIFFABLE_SALT",
    help="Hex-encoded per-partner secret salt (default: random; reuse to "
    "re-export for the same partner)",
)
def anonymize(dbpath, output, spec_path, partner_id, salt):
    """
    Dump an anonymized, partner-specific copy of the database.

    Key columns listed in the spec receive replacement identities that stay
    consistent inside this export, so the original equi-joins keep working.
    Direct identity columns are pseudonymized per partner and per column and
    must never be used for matching. See anonymization-declaration.json in
    the output directory for the full statement.

    Spec example:

    \b
    {
      "tables": {
        "contracts": {
          "keys": [["customer_id"], ["region_code", "contract_no"]],
          "identity": ["customer_name", "contact_phone"]
        },
        "settlements": {
          "keys": [["customer_id"], ["region_code", "contract_no"]]
        }
      }
    }
    """
    spec = load_spec(spec_path)
    if salt is None:
        salt = random_salt()
    try:
        bytes.fromhex(salt)
    except ValueError:
        raise click.ClickException("--salt must be hex-encoded bytes")
    pseudonymizer = Pseudonymizer(partner_id, salt)
    output = pathlib.Path(output)
    output.mkdir(exist_ok=True)
    conn = sqlite_utils.Database(dbpath)
    unknown = set(spec["tables"]) - set(conn.table_names())
    if unknown:
        raise click.ClickException(
            "spec references tables not in the database: {}".format(
                ", ".join(sorted(unknown))
            )
        )
    for table, table_spec in spec["tables"].items():
        rules = table_rules(table_spec)
        table_obj = conn[table]
        columns = [column.name for column in table_obj.columns]
        try:
            validate_columns(table, columns, rules)
        except SpecError as ex:
            raise click.ClickException(str(ex))
        tablename = table.replace("/", "")
        filepath = output / "{}.ndjson".format(tablename)
        metapath = output / "{}.metadata.json".format(tablename)
        with filepath.open("w") as fp:
            for row in table_obj.rows:
                values = transform_row(
                    list(row.values()),
                    columns,
                    rules,
                    pseudonymizer,
                    table,
                )
                fp.write(json.dumps(values, default=repr) + "\n")
        metapath.open("w").write(
            json.dumps(
                {
                    "name": table,
                    "columns": columns,
                    "schema": table_obj.schema,
                },
                indent=4,
            )
        )
    declaration = build_declaration(partner_id, spec)
    declaration["salt_fingerprint"] = salt[:8]
    write_declaration(output, declaration)
    click.echo(
        "Wrote anonymized export for partner {!r} to {}".format(
            partner_id, output
        )
    )
    click.echo("Declaration: {}".format(output / DECLARATION_FILENAME))


@cli.command()
@click.argument("source_db", type=click.Path(exists=True, dir_okay=False))
@click.argument("export_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    required=True,
    help="The same JSON spec used to produce the anonymized export",
)
@click.option(
    "--left-table",
    required=True,
    help="Child table, e.g. the settlement statements table",
)
@click.option(
    "--right-table",
    required=True,
    help="Parent table, e.g. the contracts table",
)
@click.option(
    "--on",
    "join_columns",
    multiple=True,
    required=True,
    help="Join column pair as LEFT=RIGHT; repeat for composite keys",
)
def verify_joins(
    source_db, export_dir, spec_path, left_table, right_table, join_columns
):
    """
    Check that an anonymized export preserves join semantics.

    Compares the source database against the export loaded into a temporary
    database for the given join: the total number of rows returned by the
    join and how many times each left-table row matches must be identical
    before and after export. NULL-bearing composite keys follow SQL matching
    semantics on both sides.
    """
    spec = load_spec(spec_path)
    pairs = []
    for item in join_columns:
        if "=" not in item:
            raise click.ClickException(
                "--on must look like LEFT_COLUMN=RIGHT_COLUMN"
            )
        left_column, right_column = item.split("=", 1)
        pairs.append((left_column.strip(), right_column.strip()))
    left_keys = [pair[0] for pair in pairs]
    right_keys = [pair[1] for pair in pairs]
    for table, columns in (
        (left_table, left_keys),
        (right_table, right_keys),
    ):
        if table not in spec["tables"]:
            raise click.ClickException(
                "table {!r} is not covered by the spec".format(table)
            )
        known = set()
        for group in spec["tables"][table].get("keys", []):
            _, group_columns = normalize_key_group(group)
            known.update(group_columns)
        for column in columns:
            if column not in known:
                raise click.ClickException(
                    "join column {!r} of table {!r} is not a surrogate key "
                    "in the spec".format(column, table)
                )

    source = sqlite_utils.Database(source_db)
    temp_dir = tempfile.TemporaryDirectory(prefix="sqlite-diffable-verify-")
    restored_path = pathlib.Path(temp_dir.name) / "verify-joins.db"
    restored = sqlite_utils.Database(str(restored_path))
    try:
        load_directory(restored, export_dir, replace=True)
        source_stats = _join_stats(
            source, left_table, right_table, left_keys, right_keys
        )
        export_stats = _join_stats(
            restored, left_table, right_table, left_keys, right_keys
        )
    finally:
        restored.close()
        temp_dir.cleanup()

    if source_stats != export_stats:
        raise click.ClickException(
            "Join statistics differ between source and export:\n"
            "source: {}\nexport: {}".format(source_stats, export_stats)
        )
    click.echo(
        json.dumps(
            {
                "join": "{} JOIN {} ON {}".format(
                    left_table,
                    right_table,
                    ", ".join(
                        "{}={}".format(left, right)
                        for left, right in pairs
                    ),
                ),
                "joined_rows": source_stats["joined_rows"],
                "left_rows": source_stats["left_rows"],
                "matched_left_rows": source_stats["matched_left_rows"],
                "unmatched_left_rows": source_stats["unmatched_left_rows"],
                "match_count_histogram": source_stats[
                    "match_count_histogram"
                ],
            },
            indent=4,
        )
    )
    click.echo("OK: join row counts and per-row match counts are preserved")


def _join_stats(db, left_table, right_table, left_keys, right_keys):
    left_pk = db[left_table].pks
    if not left_pk:
        raise click.ClickException(
            "table {!r} needs a primary key to count per-row matches".format(
                left_table
            )
        )
    conditions = " AND ".join(
        "l.[{}] = r.[{}]".format(left, right)
        for left, right in zip(left_keys, right_keys)
    )
    joined_rows = db.execute(
        "SELECT COUNT(*) FROM [{}] l JOIN [{}] r ON {}".format(
            left_table, right_table, conditions
        )
    ).fetchone()[0]
    pk_select = ", ".join("l.[{}]".format(pk) for pk in left_pk)
    counts = db.execute(
        "SELECT {}, COUNT(r.[{}]) FROM [{}] l LEFT JOIN [{}] r ON {} "
        "GROUP BY {}".format(
            pk_select,
            right_keys[0],
            left_table,
            right_table,
            conditions,
            pk_select,
        )
    ).fetchall()
    histogram = {}
    matched = 0
    for row in counts:
        match_count = row[-1]
        histogram[match_count] = histogram.get(match_count, 0) + 1
        if match_count:
            matched += 1
    total_left = db.execute(
        "SELECT COUNT(*) FROM [{}]".format(left_table)
    ).fetchone()[0]
    histogram = {str(key): value for key, value in sorted(histogram.items())}
    return {
        "joined_rows": joined_rows,
        "left_rows": total_left,
        "matched_left_rows": matched,
        "unmatched_left_rows": total_left - matched,
        "match_count_histogram": histogram,
    }
