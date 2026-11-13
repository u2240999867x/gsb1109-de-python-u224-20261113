import click
import json
import pathlib
import sqlite_utils
import sqlite3
import sys

from . import schema_check


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


def _render_human(report):
    lines = []
    verdict = report["verdict"]
    if verdict == "different":
        sequence = report["distinguishing_sequence"]
        lines.append(
            "DIFFERENT - %d operation(s) distinguish the versions:" % sequence["length"]
        )
        for number, step in enumerate(sequence["steps"], 1):
            params = ""
            if step["params"]:
                params = "  params=%s" % json.dumps(step["params"])
            lines.append("  %d. %s%s" % (number, step["sql"], params))
        lines.append("  A: %s" % json.dumps(sequence["a"]))
        lines.append("  B: %s" % json.dumps(sequence["b"]))
    elif verdict == "equivalent":
        budget = report["search_budget"]
        lines.append(
            "EQUIVALENT - no sequence of up to %d operation(s) from the %d "
            "allowed operations distinguished the versions."
            % (budget["max_length"], budget["operations"])
        )
        lines.append(
            "  Scoped to the supplied operation set; operations outside it "
            "were not attempted."
        )
    else:
        lines.append("UNDECIDED - no verdict can be claimed.")
        if report.get("search_error"):
            lines.append("  Search stopped early: %s" % report["search_error"])

    suspect = report.get("distinguishing_sequence")
    if suspect is not None and not suspect.get("conclusive", True):
        lines.append("")
        lines.append(
            "A sequence looked different but is not conclusive here:"
        )
        for number, step in enumerate(suspect["steps"], 1):
            lines.append("  %d. %s" % (number, step["sql"]))
        lines.append("  %s" % suspect.get("caveat", ""))

    cosmetic = report["statement_diff"]["cosmetic"]
    if cosmetic:
        lines.append("")
        lines.append("Equivalent spelling differences (ignored): %d" % len(cosmetic))
        for entry in cosmetic:
            lines.append("  A: %s" % entry["a"])
            lines.append("  B: %s" % entry["b"])

    only_a = report["statement_diff"]["only_a"]
    only_b = report["statement_diff"]["only_b"]
    if only_a or only_b:
        lines.append("")
        lines.append("Statements present in only one version:")
        for statement in only_a:
            lines.append("  A only: %s" % statement)
        for statement in only_b:
            lines.append("  B only: %s" % statement)

    unsupported = report.get("unsupported") or []
    if unsupported:
        lines.append("")
        lines.append("Outside the checkable range (reported, not judged):")
        for entry in unsupported:
            lines.append("  [%s] %s %s" % (
                entry["side"].upper(), entry["object"], entry["statement"]
            ))
            lines.append("      %s: %s" % (entry["reason"], entry["explanation"]))
    if report.get("blocking"):
        lines.append("")
        lines.append("Blocking gaps:")
        for entry in report["blocking"]:
            lines.append("  [%s] %s - %s" % (
                entry["side"].upper(), entry["statement"], entry["detail"]
            ))
    return "\n".join(lines)


@cli.command("check")
@click.argument(
    "definition_a",
    type=click.Path(exists=False, file_okay=True, dir_okay=True),
)
@click.argument(
    "definition_b",
    type=click.Path(exists=False, file_okay=True, dir_okay=True),
)
@click.option(
    "--operations",
    "operations_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    help="JSON or one-statement-per-line list of allowed operations",
)
@click.option(
    "--operation",
    "inline_operations",
    multiple=True,
    help="An allowed operation, repeatable (used when --operations is omitted)",
)
@click.option(
    "--max-length",
    type=click.IntRange(1, 20),
    default=3,
    show_default=True,
    help="Longest operation sequence to try",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the full report as JSON",
)
def check(
    definition_a,
    definition_b,
    operations_path,
    inline_operations,
    max_length,
    as_json,
):
    """
    Compare two exported SQLite definitions under an allowed operation set

    Each definition may be a directory of sqlite-diffable dump output
    (*.metadata.json files), a .sql file, a JSON metadata file, or an
    inline SQL string prefixed with sql:.

    Operations are read from --operations (a JSON array, or a file with
    one statement per line) or given with repeated --operation options.
    """
    try:
        sql_a = _load_definition_argument(definition_a)
        sql_b = _load_definition_argument(definition_b)
        if operations_path:
            operations = schema_check.load_operations(operations_path)
        else:
            operations = list(inline_operations)
        if not operations:
            raise click.ClickException(
                "Pass --operations FILE or at least one --operation"
            )
        report = schema_check.check(sql_a, sql_b, operations, max_length=max_length)
    except (schema_check.DefinitionError, schema_check.OperationError) as error:
        raise click.ClickException(str(error))
    if as_json:
        click.echo(json.dumps(report, indent=2, default=repr))
    else:
        click.echo(_render_human(report))
    if report["verdict"] == "different":
        raise click.exceptions.Exit(1)


def _load_definition_argument(value):
    if value.startswith("sql:"):
        return value[len("sql:"):]
    return schema_check.load_definition(value)
