import click
import json
import pathlib
import sqlite3
import sys

from .views import (
    CyclicDependencyError,
    MissingDependencyError,
    dependency_closure,
    index_from_connection,
    index_from_directory,
    objects_for_restore,
    restore_table,
    restore_trigger,
    restore_view,
    write_closure_dump,
    write_table_dump,
    write_trigger_dump,
    write_view_dump,
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
@click.option(
    "--all",
    is_flag=True,
    help="Dump all tables (and views, which are stored as schemas only)",
)
@click.option("--exclude", multiple=True, help="Tables to exclude from the dump")
@click.option(
    "--triggers",
    is_flag=True,
    help="Also dump trigger definitions (never needed to query a view)",
)
def dump(dbpath, output, tables, all, exclude, triggers):
    """
    Dump a SQLite database out as flat files in the directory

    Usage:

        sqlite-diffable dump my.db output/ --all

    --all dumps every table and view. Or specify tables like this:

        sqlite-diffable dump my.db output/ entries tags

    A view is dumped as its CREATE VIEW statement without a .ndjson file,
    because its rows always come from its base tables.

    Exclude specific tables:

        sqlite-diffable dump my.db output/ --all --exclude table1 --exclude table2
    """
    if not tables and not all:
        raise click.ClickException("You must pass --all or specify some tables")
    output = pathlib.Path(output)
    output.mkdir(exist_ok=True)
    sqlite_conn = sqlite3.connect(dbpath)
    try:
        known = {
            name.lower(): kind
            for name, kind in sqlite_conn.execute(
                "SELECT name, type FROM main.sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        trigger_names = []
        if triggers:
            trigger_names = [
                name
                for (name,) in sqlite_conn.execute(
                    "SELECT name FROM main.sqlite_master "
                    "WHERE type = 'trigger' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                ).fetchall()
            ]
        excluded = {value.lower() for value in exclude}
        if all:
            names = [
                name for name, kind in sorted(known.items()) if name not in excluded
            ]
        else:
            names = []
            missing = []
            for table in tables:
                if table.lower() in known:
                    names.append(table.lower())
                elif table.lower() not in excluded:
                    missing.append(table)
            if missing:
                raise click.ClickException(
                    "No such table or view: {}".format(", ".join(missing))
                )
        if all:
            selected = list(names)
        else:
            # A named view is only useful with everything needed to recompute
            # it, so expand it to its transitive table/view closure.
            objects = index_from_connection(sqlite_conn)
            selected = []
            for name in names:
                kind = known[name]
                object_name = sqlite_conn.execute(
                    "SELECT name FROM main.sqlite_master WHERE lower(name) = ?",
                    (name,),
                ).fetchone()[0]
                if kind == "view":
                    try:
                        closure = dependency_closure(objects, object_name)
                    except MissingDependencyError as ex:
                        raise click.ClickException(str(ex))
                    except CyclicDependencyError as ex:
                        raise click.ClickException(str(ex))
                    selected.extend(
                        obj.name for obj in closure if obj.name.lower() not in excluded
                    )
                else:
                    selected.append(object_name)
        seen = set()
        for name in selected:
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            kind = known[name.lower()]
            object_name = sqlite_conn.execute(
                "SELECT name FROM main.sqlite_master WHERE lower(name) = ?",
                (name.lower(),),
            ).fetchone()[0]
            if kind == "view":
                write_view_dump(sqlite_conn, output, object_name)
            else:
                write_table_dump(sqlite_conn, output, object_name)
        for name in trigger_names:
            write_trigger_dump(sqlite_conn, output, name)
    finally:
        sqlite_conn.close()


def _restore(dbpath, directory, replace, views, triggers, objects):
    directory = pathlib.Path(directory)
    try:
        available = index_from_directory(directory)
    except ValueError as ex:
        raise click.ClickException(str(ex))
    requested = list(views) + list(objects)
    conn = sqlite3.connect(dbpath)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        existing = {
            name.lower()
            for (name,) in conn.execute(
                "SELECT name FROM main.sqlite_master"
            ).fetchall()
        }
        try:
            ordered = objects_for_restore(
                available,
                requested=requested or None,
                # Objects already in the destination database satisfy
                # dependencies that need not be present in this directory.
                satisfied_names=existing
                - {obj.name.lower() for obj in available.values()},
            )
        except MissingDependencyError as ex:
            raise click.ClickException(str(ex))
        except CyclicDependencyError as ex:
            raise click.ClickException(str(ex))
        for obj in ordered:
            try:
                if obj.is_view:
                    restore_view(conn, obj, replace)
                else:
                    restore_table(conn, obj, replace)
            except sqlite3.OperationalError as ex:
                msg = str(ex)
                if "already exists" in msg:
                    msg += "\n\nUse the --replace option to over-write existing tables"
                raise click.ClickException(msg)
            conn.commit()
        if not triggers:
            return
        for obj in sorted(
            (obj for obj in available.values() if obj.is_trigger),
            key=lambda obj: obj.name.lower(),
        ):
            try:
                restore_trigger(conn, obj, replace)
            except sqlite3.OperationalError as ex:
                msg = str(ex)
                if "already exists" in msg:
                    msg += "\n\nUse the --replace option to over-write existing tables"
                raise click.ClickException(msg)
            conn.commit()
    finally:
        conn.close()


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
    "--view",
    "views",
    multiple=True,
    help="Restore only this view and the objects it depends on (repeatable)",
)
@click.option(
    "--triggers",
    is_flag=True,
    help="Also restore triggers (not needed to query views)",
)
@click.argument("objects", nargs=-1, required=False)
def load(dbpath, directory, replace, views, triggers, objects):
    """
    Load flat files from a directory into a SQLite database

    Usage:

        sqlite-diffable load my.db dump-location/

    Tables are created and populated before views, and views are created in
    dependency order.  Triggers in the directory are applied last only when
    ``--triggers`` is passed.

    With ``--view`` only that view's transitive table/view dependencies are
    restored.  Positional object names select specific tables or views, and a
    selected view still pulls in its whole dependency closure.  This is how a
    view name alone is turned into a runnable query against an empty database.
    """
    _restore(dbpath, directory, replace, views, triggers, objects)


@cli.command("restore-view")
@click.argument(
    "dbpath",
    type=click.Path(file_okay=True, allow_dash=False, dir_okay=False),
)
@click.argument(
    "directory",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.argument("views", nargs=-1, required=True)
@click.option("--replace", is_flag=True, help="Replace objects if they exist already")
@click.option(
    "--triggers",
    is_flag=True,
    help="Also restore triggers (not needed to query views)",
)
def restore_view_command(dbpath, directory, views, replace, triggers):
    """
    Restore only the objects needed to recompute named views

    Usage:

        sqlite-diffable restore-view my.db dump-location/ my_view

    The view itself is recreated from its CREATE VIEW statement; the base
    tables in its dependency closure are recreated with their dumped rows.
    """
    _restore(dbpath, directory, replace, views, triggers, ())


@cli.command("dump-view")
@click.argument(
    "dbpath",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    required=True,
)
@click.argument(
    "output", type=click.Path(file_okay=False, dir_okay=True), required=True
)
@click.argument("views", nargs=-1, required=True)
def dump_view(dbpath, output, views):
    """
    Dump one or more views and everything needed to recompute them

    Usage:

        sqlite-diffable dump-view my.db output/ my_view

    The view is written as a CREATE VIEW schema, never as a snapshot of its
    current rows.  Base tables in its dependency closure are dumped as normal
    .ndjson files, so the view keeps following later table changes.
    """
    output = pathlib.Path(output)
    output.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dbpath)
    try:
        available = {
            name.lower(): (name, kind)
            for name, kind in conn.execute(
                "SELECT name, type FROM main.sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        missing = [name for name in views if name.lower() not in available]
        if missing:
            raise click.ClickException("No such view: {}".format(", ".join(missing)))
        not_views = [name for name in views if available[name.lower()][1] != "view"]
        if not_views:
            raise click.ClickException("Not a view: {}".format(", ".join(not_views)))
        for view in views:
            try:
                write_closure_dump(conn, output, view)
            except MissingDependencyError as ex:
                raise click.ClickException(str(ex))
            except CyclicDependencyError as ex:
                raise click.ClickException(str(ex))
    finally:
        conn.close()


@cli.command("view-dependencies")
@click.argument(
    "directory",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.argument("views", nargs=-1, required=True)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output the closure as JSON instead of one name per line",
)
def view_dependencies(directory, views, as_json):
    """
    Show the objects needed to restore views from a diffable directory

    Usage:

        sqlite-diffable view-dependencies dump/ my_view
    """
    directory = pathlib.Path(directory)
    try:
        available = index_from_directory(directory)
    except ValueError as ex:
        raise click.ClickException(str(ex))
    selected = []
    for view in views:
        obj = available.get(view.lower())
        if obj is None:
            raise click.ClickException("No such view: {}".format(view))
        if not obj.is_view:
            raise click.ClickException("Not a view: {}".format(view))
        try:
            selected.extend(dependency_closure(available, obj.name))
        except MissingDependencyError as ex:
            raise click.ClickException(str(ex))
        except CyclicDependencyError as ex:
            raise click.ClickException(str(ex))
    seen = set()
    ordered = []
    for obj in selected:
        if obj.name.lower() in seen:
            continue
        seen.add(obj.name.lower())
        ordered.append(obj)
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "name": obj.name,
                        "type": obj.type,
                        "has_rows": bool(obj.ndjson_path and obj.ndjson_path.exists()),
                    }
                    for obj in ordered
                ],
                indent=2,
            )
        )
    else:
        for obj in ordered:
            click.echo(obj.name)


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
