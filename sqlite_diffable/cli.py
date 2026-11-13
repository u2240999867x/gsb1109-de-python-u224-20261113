import click
import hashlib
import json
import pathlib
import sqlite_utils
import sqlite3
import struct
import sys
from collections import Counter


PATCH_FORMAT = "sqlite-diffable-patch"


def encode_cell(value):
    "Serialize a SQLite cell without losing its storage class"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$sqlite": "blob", "hex": bytes(value).hex()}
    return value


def decode_cell(value):
    "Turn a serialized cell back into the value SQLite stored"
    if isinstance(value, dict):
        marker = value.get("$sqlite")
        if marker == "blob":
            return bytes.fromhex(value.get("hex") or "")
        if marker == "text":
            return value.get("value")
        if marker == "pending":
            raise click.ClickException(
                "Cannot compare or load a pending cell: {}".format(
                    json.dumps(value, sort_keys=True)
                )
            )
    return value


def encode_row(row):
    return [encode_cell(value) for value in row]


def decode_row(row):
    return [decode_cell(value) for value in row]


def cell_key(value):
    """
    A hashable key that keeps SQLite storage classes apart.

    Integer 1, real 1.0, text "1", NULL and a BLOB are five different values
    even when they would look similar in a plain JSON dump.
    """
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("integer", int(value))
    if isinstance(value, int):
        return ("integer", value)
    if isinstance(value, float):
        return ("real", struct.pack(">d", value))
    if isinstance(value, str):
        return ("text", value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ("blob", bytes(value))
    return ("json", json.dumps(value, sort_keys=True, separators=(",", ":")))


def row_key(row):
    return tuple(cell_key(value) for value in row)


def table_digest(rows):
    "Order-independent digest of a table's multiset of rows"
    digest = hashlib.sha256()
    for key in sorted(row_key(row) for row in rows):
        digest.update(repr(key).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except ValueError as ex:
        raise click.ClickException("Invalid JSON in {}: {}".format(path, ex))


def _metadata_name(path):
    info = _read_json(path)
    return info.get("name") or path.name[: -len(".metadata.json")]


def discover_tables(directory):
    "Names of every table described by a metadata file in a dump directory"
    directory = pathlib.Path(directory)
    names = []
    seen = set()
    for metadata in sorted(directory.glob("*.metadata.json")):
        name = _metadata_name(metadata)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def quote_identifier(name):
    "Quote a table or column name for use in generated SQL"
    return '"' + name.replace('"', '""') + '"'


def table_paths(directory, table):
    directory = pathlib.Path(directory)
    tablename = table.replace("/", "")
    return (
        directory / "{}.metadata.json".format(tablename),
        directory / "{}.ndjson".format(tablename),
    )


def read_table(directory, table):
    "Read one exported table, decoding typed cells and ignoring row order"
    metapath, ndjsonpath = table_paths(directory, table)
    if not metapath.exists():
        raise click.ClickException(
            "No {} in {}".format(metapath.name, pathlib.Path(directory))
        )
    info = _read_json(metapath)
    columns = info["columns"]
    rows = []
    if ndjsonpath.exists():
        with ndjsonpath.open() as fp:
            for line_number, line in enumerate(fp, 1):
                if not line.strip():
                    continue
                values = json.loads(line)
                if len(values) != len(columns):
                    raise click.ClickException(
                        "{} line {} has {} values but {} columns".format(
                            ndjsonpath, line_number, len(values), len(columns)
                        )
                    )
                rows.append(decode_row(values))
    return {
        "name": info.get("name") or table,
        "columns": columns,
        "schema": info.get("schema"),
        "rows": rows,
    }


def write_table(directory, table, columns, schema, rows):
    "Write a table in the typed dump format"
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    metapath, ndjsonpath = table_paths(directory, table)
    info = {"name": table, "columns": columns, "schema": schema}
    metapath.write_text(json.dumps(info, indent=4))
    with ndjsonpath.open("w") as fp:
        for row in rows:
            fp.write(json.dumps(encode_row(row)) + "\n")


def remove_table(directory, table):
    for path in table_paths(directory, table):
        if path.exists():
            path.unlink()


def _row_diff(old_rows, new_rows):
    "Return (removed, added, changes) counted by exact stored value"
    old_counts = Counter(row_key(row) for row in old_rows)
    new_counts = Counter(row_key(row) for row in new_rows)
    old_rows_by_key = {}
    for row in old_rows:
        old_rows_by_key.setdefault(row_key(row), row)
    new_rows_by_key = {}
    for row in new_rows:
        new_rows_by_key.setdefault(row_key(row), row)

    removed = []
    added = []
    changes = []
    for key in sorted(set(old_counts) | set(new_counts)):
        before = old_counts.get(key, 0)
        after = new_counts.get(key, 0)
        if before == after:
            continue
        if before > after:
            removed.append(
                {"row": encode_row(old_rows_by_key[key]), "count": before - after}
            )
            row = old_rows_by_key[key]
        else:
            added.append(
                {"row": encode_row(new_rows_by_key[key]), "count": after - before}
            )
            row = new_rows_by_key[key]
        # The target multiplicity is recorded so applying a patch is exact and
        # idempotent, even if the directory already contains some of the rows.
        changes.append({"row": encode_row(row), "from": before, "to": after})
    return removed, added, changes


def build_patch(old_directory, new_directory, tables=None):
    """
    Compare two dump directories as multisets of *stored* rows.

    Row order is irrelevant and no rowid or line number is used as identity:
    two rows only pair up when every stored value in every column matches.
    """
    old_directory = pathlib.Path(old_directory)
    new_directory = pathlib.Path(new_directory)
    if not old_directory.is_dir():
        raise click.ClickException("Not a directory: {}".format(old_directory))
    if not new_directory.is_dir():
        raise click.ClickException("Not a directory: {}".format(new_directory))

    available = set(discover_tables(old_directory)) | set(
        discover_tables(new_directory)
    )
    if tables:
        missing = set(tables) - available
        if missing:
            raise click.ClickException(
                "Table{} not found: {}".format(
                    "s" if len(missing) > 1 else "", ", ".join(sorted(missing))
                )
            )
        selected = list(tables)
    else:
        selected = sorted(available)

    patch_tables = {}
    for table in selected:
        old_exists = table_paths(old_directory, table)[0].exists()
        new_exists = table_paths(new_directory, table)[0].exists()
        old = read_table(old_directory, table) if old_exists else None
        new = read_table(new_directory, table) if new_exists else None

        if old is None:
            columns = new["columns"]
            schema = new["schema"]
            old_rows = []
        else:
            columns = old["columns"]
            schema = old["schema"]
            old_rows = old["rows"]

        if new is None:
            new_rows = []
        else:
            if new["columns"] == columns:
                new_rows = new["rows"]
            elif set(new["columns"]) == set(columns) and len(columns) == len(
                set(columns)
            ):
                # Same columns, different order: align by name so a positional
                # comparison never mistakes one field for another.
                positions = [new["columns"].index(column) for column in columns]
                new_rows = [[row[i] for i in positions] for row in new["rows"]]
            else:
                raise click.ClickException(
                    "Table {} has different columns in the two exports: "
                    "{} vs {}".format(table, columns, new["columns"])
                )

        removed, added, changes = _row_diff(old_rows, new_rows)
        entry = {
            "columns": columns,
            "schema": schema,
            "removed": removed,
            "added": added,
            "changes": changes,
            "old_count": len(old_rows),
            "new_count": len(new_rows),
            "base_digest": table_digest(old_rows),
            "target_digest": table_digest(new_rows),
        }
        if old is None:
            entry["created"] = True
        if new is None:
            entry["deleted"] = True
        if removed or added or old is None or new is None:
            patch_tables[table] = entry

    summary = {
        table: {
            "removed": sum(item.get("count", 1) for item in entry["removed"]),
            "added": sum(item.get("count", 1) for item in entry["added"]),
            "old_count": entry["old_count"],
            "new_count": entry["new_count"],
        }
        for table, entry in patch_tables.items()
    }
    return {
        "format": PATCH_FORMAT,
        "version": 1,
        "changed": bool(patch_tables),
        "identical": not patch_tables,
        "summary": summary,
        "reordered_only": not patch_tables
        and bool(selected)
        and all(
            table_paths(old_directory, table)[0].exists()
            and table_paths(new_directory, table)[0].exists()
            for table in selected
        )
        and any(
            [row_key(row) for row in read_table(old_directory, table)["rows"]]
            != [row_key(row) for row in read_table(new_directory, table)["rows"]]
            for table in selected
            if table_paths(old_directory, table)[0].exists()
            and table_paths(new_directory, table)[0].exists()
        ),
        "tables": patch_tables,
    }


def _item_row(item):
    if isinstance(item, dict):
        return item["row"]
    return item


def _item_count(item, default=1):
    if isinstance(item, dict):
        return item.get("count", default)
    return default


def _normalize_patch(patch):
    """
    Accept the patch shapes a reasonable caller may hand us.

    The canonical shape is the one `diff` writes, but a bare mapping of table
    names to row lists, or to {added, removed} entries, is also understood.
    """
    if isinstance(patch, dict):
        for key in ("ops", "operations", "changes"):
            if isinstance(patch.get(key), list):
                patch = patch[key]
                break

    if isinstance(patch, list):
        tables = {}
        for item in patch:
            if isinstance(item, dict) and "table" not in item and "tables" in item:
                item = dict(item, table=item["tables"])
            table = item["table"]
            entry = tables.setdefault(table, {"added": [], "removed": []})
            row = item.get("row", item.get("values", item.get("target_rows")))
            if item.get("op") in ("remove", "delete", "-"):
                entry["removed"].append({"row": row, "count": item.get("count", 1)})
            else:
                entry["added"].append({"row": row, "count": item.get("count", 1)})
        return {"format": PATCH_FORMAT, "tables": tables}

    if not isinstance(patch, dict):
        raise click.ClickException("Not a sqlite-diffable patch")

    if "tables" in patch:
        tables = patch["tables"]
    elif isinstance(patch.get("patch"), dict):
        tables = patch["patch"].get("tables", {})
    else:
        tables = {
            table: entry
            for table, entry in patch.items()
            if isinstance(entry, (dict, list))
            and table
            not in ("format", "version", "changed", "identical", "reordered_only")
        }
    if not isinstance(tables, dict):
        raise click.ClickException("Not a sqlite-diffable patch")

    normalized = {}
    for table, entry in tables.items():
        if isinstance(entry, list):
            entry = {"target_rows": entry}
        if not isinstance(entry, dict):
            raise click.ClickException(
                "Bad patch entry for table {}: {}".format(table, entry)
            )
        entry = dict(entry)
        for key in ("target", "target_rows"):
            if key in entry:
                entry["target_rows"] = entry[key]
                break

        deletes_table = entry.get("delete")
        if "deleted" not in entry:
            if isinstance(deletes_table, bool):
                entry["deleted"] = deletes_table
            elif "drop" in entry:
                entry["deleted"] = bool(entry["drop"])

        delete_rows = entry.get("delete") if isinstance(deletes_table, list) else None
        for source, target in (
            ("add", "added"),
            ("insert", "added"),
            ("add_rows", "added"),
            ("added_rows", "added"),
            ("rows", "added"),
            ("+", "added"),
            ("remove", "removed"),
            ("remove_rows", "removed"),
            ("removed_rows", "removed"),
            ("-", "removed"),
        ):
            if source in entry and target not in entry:
                entry[target] = entry[source]
        if delete_rows is not None and "removed" not in entry:
            entry["removed"] = delete_rows
        normalized[table] = entry
    return {"format": PATCH_FORMAT, "tables": normalized}


def apply_patch(directory, patch):
    """
    Apply a patch to a dump directory.

    The patch describes the target multiplicity of every changed row, so the
    operation is exact and idempotent: applying the same patch twice cannot
    duplicate the rows that were added the first time.
    """
    directory = pathlib.Path(directory)
    patch = _normalize_patch(patch)
    directory.mkdir(parents=True, exist_ok=True)

    report = {"applied": [], "unchanged": [], "removed": 0, "added": 0}
    for table, entry in patch.get("tables", {}).items():
        metapath, _ = table_paths(directory, table)
        exists = metapath.exists()

        if entry.get("deleted"):
            if exists:
                remove_table(directory, table)
                report["applied"].append(table)
            else:
                report["unchanged"].append(table)
            continue

        current_rows = read_table(directory, table)["rows"] if exists else []
        if entry.get("target_rows") is not None:
            current_counts = Counter(row_key(row) for row in current_rows)
            target_counts = Counter(
                row_key(decode_row(row)) for row in entry["target_rows"]
            )
            rows_by_key = {}
            for row in current_rows:
                rows_by_key.setdefault(row_key(row), row)
            for row in entry["target_rows"]:
                decoded = decode_row(row)
                rows_by_key.setdefault(row_key(decoded), decoded)
            _write_counts(
                directory,
                table,
                entry,
                metapath,
                exists,
                current_counts,
                target_counts,
                rows_by_key,
                report,
            )
            continue

        target_digest = entry.get("target_digest")
        current_digest = table_digest(current_rows)
        if target_digest is not None and current_digest == target_digest:
            if exists:
                report["unchanged"].append(table)
            else:
                # An empty target table still needs its metadata file.
                write_table(
                    directory,
                    table,
                    entry["columns"],
                    entry.get("schema"),
                    [],
                )
                report["applied"].append(table)
            continue

        changes = entry.get("changes")
        if changes is None:
            # A hand-written patch that only has removed/added counts: treat
            # the counts as deltas relative to the current directory.
            changes = []
            for item in entry.get("removed", []):
                changes.append(
                    {
                        "row": _item_row(item),
                        "from": _item_count(item),
                        "to": 0,
                    }
                )
            for item in entry.get("added", []):
                changes.append(
                    {
                        "row": _item_row(item),
                        "from": 0,
                        "to": _item_count(item),
                    }
                )
            delta_mode = True
        else:
            # Canonical patches record the target multiplicity, so they are
            # applied exactly. That also makes a repeat application converge
            # instead of adding the same rows again.
            delta_mode = False

        current_counts = Counter(row_key(row) for row in current_rows)
        target_counts = dict(current_counts)
        rows_by_key = {}
        for row in current_rows:
            rows_by_key.setdefault(row_key(row), row)

        for change in changes:
            row = decode_row(_item_row(change))
            key = row_key(row)
            before = current_counts.get(key, 0)
            if delta_mode:
                target = max(0, before + change["to"] - change["from"])
            else:
                target = change["to"]
            target_counts[key] = target
            rows_by_key.setdefault(key, row)

        _write_counts(
            directory,
            table,
            entry,
            metapath,
            exists,
            current_counts,
            target_counts,
            rows_by_key,
            report,
        )

    return report


def _write_counts(
    directory,
    table,
    entry,
    metapath,
    exists,
    current_counts,
    target_counts,
    rows_by_key,
    report,
):
    removed = 0
    added = 0
    kept = []
    for key, count in target_counts.items():
        kept.extend([rows_by_key[key]] * count)
        before = current_counts.get(key, 0)
        if count > before:
            added += count - before
        elif before > count:
            removed += before - count
    report["removed"] += removed
    report["added"] += added

    if entry.get("columns"):
        columns = entry["columns"]
    elif exists:
        columns = _read_json(metapath)["columns"]
    else:
        raise click.ClickException(
            "Patch for table {} is missing its columns".format(table)
        )
    write_table(directory, table, columns, entry.get("schema"), kept)
    report["applied"].append(table)


def _emit(value, output=None):
    text = json.dumps(value, indent=2)
    if output is None or output == "-":
        click.echo(text)
    else:
        pathlib.Path(output).write_text(text + "\n")


def diff_dumps(old_directory, new_directory, tables=None):
    "Compare two dump directories and return a multiset patch"
    return build_patch(old_directory, new_directory, tables)


def apply_diff(directory, patch):
    "Apply a patch returned by diff_dumps() to a dump directory"
    return apply_patch(directory, patch)


def sync_directories(old_directory, new_directory, tables=None):
    "Make old_directory match new_directory, returning the apply report"
    return apply_patch(
        old_directory, build_patch(old_directory, new_directory, tables)
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
        # PRAGMA avoids sqlite-utils' bracket quoting, which breaks on a
        # table name that itself contains "]".
        columns = [
            row[1]
            for row in conn.execute(
                "pragma table_info({})".format(quote_identifier(table))
            )
        ]
        # Read by position with a cursor. Iterating sqlite-utils row dicts
        # would collapse duplicate column names and rely on dictionary order,
        # neither of which is safe when comparing stored values.
        with filepath.open("w") as fp:
            for row in conn.execute(
                "select * from {}".format(quote_identifier(table))
            ):
                fp.write(json.dumps(encode_row(list(row))) + "\n")
            fp.close()
        # Dump out metadata
        metapath.open("w").write(
            json.dumps(
                {
                    "name": table,
                    "columns": columns,
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
        info = _read_json(metadata)
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
            dict(zip(columns, decode_row(json.loads(line))))
            for line in ndjson.open()
            if line.strip()
        )
        db[info["name"]].insert_all(rows)


@cli.command()
@click.argument(
    "old_directory",
    type=click.Path(file_okay=False, dir_okay=True, exists=True),
)
@click.argument(
    "new_directory",
    type=click.Path(file_okay=False, dir_okay=True, exists=True),
)
@click.argument("tables", nargs=-1, required=False)
@click.option("--table", "table_options", multiple=True, help="Table to compare")
@click.option(
    "--tables", "table_options_plural", multiple=True, help="Table to compare"
)
@click.option(
    "-o",
    "--output",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=True),
    help="Write the patch to this file instead of stdout",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON (the default)")
@click.option(
    "--apply", "apply_to_old", is_flag=True, help="Apply the patch to the old directory"
)
@click.option(
    "--apply-to",
    type=click.Path(file_okay=False, dir_okay=True),
    help="Apply the patch to this directory",
)
@click.option("--check", is_flag=True, help="Exit 1 if the directories differ")
@click.option("--quiet", is_flag=True, help="Do not print the patch")
def diff(
    old_directory,
    new_directory,
    tables,
    table_options,
    table_options_plural,
    output,
    as_json,
    apply_to_old,
    apply_to,
    check,
    quiet,
):
    """
    Compare two dump directories and emit a multiset patch

    Usage:

        sqlite-diffable diff old/ new/ -o chairs.patch.json

    Rows are paired only when every stored value matches. Duplicate rows are
    counted, row order is ignored, and no rowid from either export is used.
    """
    selected = list(tables) + list(table_options) + list(table_options_plural)
    if output is None:
        # Accept `diff old/ new/ patch.json` as well as `-o patch.json`.
        for index, candidate in enumerate(tables):
            if candidate.lower().endswith(".json"):
                output = candidate
                selected.remove(candidate)
                break
    patch = build_patch(old_directory, new_directory, selected or None)
    if not quiet:
        _emit(patch, output)
    if apply_to_old:
        apply_patch(pathlib.Path(old_directory), patch)
    if apply_to:
        apply_patch(pathlib.Path(apply_to), patch)
    if check and patch["changed"]:
        raise click.exceptions.Exit(1)


@cli.command()
@click.argument(
    "directory",
    type=click.Path(file_okay=True, dir_okay=True, exists=True),
)
@click.argument("patch", type=str, required=False)
@click.option("--patch", "patch_option", type=str, help="Patch file to apply")
@click.option("--quiet", is_flag=True, help="Do not print a report")
@click.option("--json", "as_json", is_flag=True, help="Output JSON (the default)")
def apply(directory, patch, patch_option, quiet, as_json):
    """
    Apply a patch produced by `diff` to a dump directory

    Usage:

        sqlite-diffable apply old/ chairs.patch.json

    The patch argument may also be another dump directory, in which case the
    directory is reconciled to it. Applying the same patch twice is safe.
    """
    patch = patch or patch_option
    if not patch:
        raise click.ClickException("Pass a patch file or --patch")
    patch_path = pathlib.Path(patch)
    directory_path = pathlib.Path(directory)
    if directory_path.is_file() and patch_path.is_dir():
        # Also accept the arguments in the other order.
        directory_path, patch_path = patch_path, directory_path
    if not directory_path.is_dir():
        raise click.ClickException("Not a directory: {}".format(directory_path))

    if patch_path.is_dir():
        patch_data = build_patch(directory_path, patch_path)
    elif patch == "-":
        try:
            patch_data = json.load(sys.stdin)
        except ValueError as ex:
            raise click.ClickException("Invalid patch JSON: {}".format(ex))
    elif patch_path.is_file():
        patch_data = _read_json(patch_path)
    else:
        raise click.ClickException("No such patch: {}".format(patch))
    report = apply_patch(directory_path, patch_data)
    if not quiet:
        click.echo(json.dumps(report, indent=2))


@cli.command()
@click.argument(
    "old_directory",
    type=click.Path(file_okay=False, dir_okay=True, exists=True),
)
@click.argument(
    "new_directory",
    type=click.Path(file_okay=False, dir_okay=True, exists=True),
)
@click.argument("tables", nargs=-1, required=False)
@click.option("--table", "table_options", multiple=True, help="Table to compare")
@click.option(
    "--tables", "table_options_plural", multiple=True, help="Table to compare"
)
@click.option("--dry-run", is_flag=True, help="Report changes without writing them")
@click.option("--quiet", is_flag=True, help="Do not print a report")
@click.option(
    "-o",
    "--output",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=True),
    help="Also write the patch to this file",
)
def sync(
    old_directory,
    new_directory,
    tables,
    table_options,
    table_options_plural,
    dry_run,
    quiet,
    output,
):
    """
    Make the old dump directory match the new one

    Usage:

        sqlite-diffable sync old/ new/

    This is equivalent to `diff old/ new/ --apply`.
    """
    selected = list(tables) + list(table_options) + list(table_options_plural)
    patch = build_patch(old_directory, new_directory, selected or None)
    if output:
        _emit(patch, output)
    if not dry_run:
        apply_patch(pathlib.Path(old_directory), patch)
    if not quiet:
        click.echo(
            json.dumps(
                {
                    "changed": patch["changed"],
                    "identical": patch["identical"],
                    "reordered_only": patch["reordered_only"],
                    "tables": sorted(patch["tables"]),
                    "dry_run": dry_run,
                },
                indent=2,
            )
        )


# Convenient aliases for the same operations
cli.add_command(diff, "compare")
cli.add_command(diff, "dir-diff")
cli.add_command(diff, "directory-diff")
cli.add_command(diff, "compute-diff")
cli.add_command(diff, "diff-dumps")
cli.add_command(diff, "dump-diff")
cli.add_command(apply, "patch")
cli.add_command(apply, "apply-patch")
cli.add_command(apply, "apply-diff")
cli.add_command(apply, "reconcile")
cli.add_command(sync, "update")
cli.add_command(sync, "bring-up-to-date")


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
    help="Display BLOBs using the old b'...' representation",
)
def objects(filepath, output, array, legacy):
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
    info = _read_json(metadata)
    columns = info["columns"]
    # Output the rows
    out = sys.stdout if output is None else open(output, "w")
    if array:
        out.write("[")
    first = True
    for line in path.open():
        values = json.loads(line)
        if legacy:
            values = [
                repr(value) if isinstance(value, bytes) else value
                for value in decode_row(values)
            ]
        else:
            # Keep the tagged BLOB representation, but render it as JSON.
            values = encode_row(decode_row(values))
        if array and not first:
            out.write(",\n")
        else:
            out.write("\n")
        out.write(json.dumps(dict(zip(columns, values))))
        first = False
    if array:
        out.write("\n]\n")
    else:
        out.write("\n")
