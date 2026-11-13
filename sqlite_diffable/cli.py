import click
import json
import pathlib
import sqlite_utils
import sqlite3
import sys

from .lineage import Lineage, LineageError


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


@cli.group()
def lineage():
    "Reconstruct one unified table across versioned positional dumps"
    pass


def _load_changes(path):
    if path is None:
        return []
    text = pathlib.Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        raise click.ClickException("Invalid JSON in {}: {}".format(path, ex))
    if not isinstance(data, list):
        raise click.ClickException("Changes file must contain a JSON list")
    return data


def _build_lineage(versions, changes):
    try:
        return Lineage.from_paths(versions, changes)
    except LineageError as ex:
        raise click.ClickException(str(ex))


def _emit(payload, output):
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=repr)
    if output is None:
        click.echo(text)
    else:
        pathlib.Path(output).write_text(text + "\n", encoding="utf-8")


@lineage.command("describe")
@click.argument("versions", nargs=-1, required=True, type=click.Path(exists=True))
@click.option(
    "-c", "--changes", "changes_path", type=click.Path(exists=True), default=None,
    help="JSON file of proven rename/split changes",
)
@click.option("-o", "--output", type=click.Path(dir_okay=False), default=None)
def lineage_describe(versions, changes_path, output):
    """Show versions, proven changes and field lineage as JSON."""
    lineage_obj = _build_lineage(versions, _load_changes(changes_path))
    _emit(lineage_obj.describe(), output)


@lineage.command("reconstruct")
@click.argument("versions", nargs=-1, required=True, type=click.Path(exists=True))
@click.option(
    "-c", "--changes", "changes_path", type=click.Path(exists=True), default=None,
    help="JSON file of proven rename/split changes",
)
@click.option(
    "--version", "only_version", default=None,
    help="Restrict output to one version (label or path)",
)
@click.option(
    "--no-provenance", is_flag=True,
    help="Omit _version/_source_directory from each row",
)
@click.option("-o", "--output", type=click.Path(dir_okay=False), default=None)
def lineage_reconstruct(versions, changes_path, only_version, no_provenance, output):
    """Write the unified historical table as a JSON array of row objects."""
    lineage_obj = _build_lineage(versions, _load_changes(changes_path))
    rows = lineage_obj.reconstruct(
        version=only_version, provenance=not no_provenance
    )
    _emit(rows, output)


@lineage.command("history")
@click.argument("versions", nargs=-1, required=True, type=click.Path(exists=True))
@click.option(
    "-c", "--changes", "changes_path", type=click.Path(exists=True), default=None,
    help="JSON file of proven rename/split changes",
)
@click.option("--name", required=True, help="Column name to trace")
@click.option("-o", "--output", type=click.Path(dir_okay=False), default=None)
def lineage_history(versions, changes_path, name, output):
    """Trace every field that ever used a name (reused names stay separate)."""
    lineage_obj = _build_lineage(versions, _load_changes(changes_path))
    _emit(lineage_obj.history(name), output)
