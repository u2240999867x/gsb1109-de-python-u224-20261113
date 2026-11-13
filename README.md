# sqlite-diffable

[![PyPI](https://img.shields.io/pypi/v/sqlite-diffable.svg)](https://pypi.org/project/sqlite-diffable/)
[![Changelog](https://img.shields.io/github/v/release/simonw/sqlite-diffable?include_prereleases&label=changelog)](https://github.com/simonw/sqlite-diffable/releases)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/simonw/sqlite-diffable/blob/main/LICENSE)

Tools for dumping/loading a SQLite database to diffable directory structure

## Installation

    pip install sqlite-diffable

## Demo

The repository at [simonw/simonwillisonblog-backup](https://github.com/simonw/simonwillisonblog-backup) contains a backup of the database on my blog, https://simonwillison.net/ - created using this tool.

## Dumping a database

Given a SQLite database called `fixtures.db` containing a table `facetable`, the following will dump out that table to the `dump/` directory:
```bash
sqlite-diffable dump fixtures.db dump/ facetable
```
To dump out every table in that database, use `--all`:
```bash
sqlite-diffable dump fixtures.db dump/ --all
```
To dump all table except some specific ones, use `--exclude` one or more times:
```bash
sqlite-diffable dump fixtures.db dump/ --all \
  --exclude unwanted_first_table \
  --exclude unwanted_second_table
```
## Loading a database

To load a previously dumped database, run the following:
```bash
sqlite-diffable load restored.db dump/
```
This will show an error if any of the tables that are being restored already exist in the database file.

You can replace those tables (dropping them before restoring them) using the `--replace` option:
```bash
sqlite-diffable load restored.db dump/ --replace
```

## Selectively restoring into an existing database

`load` creates tables from scratch. When the target database already exists
and only a subset of an archive should come back, use `restore-filtered`. It
restores one table's filtered rows (the **anchor** documents) together with
everything they reference, and nothing else:

```bash
sqlite-diffable restore-filtered shop.db archive/ \
    --table receipt --filter branch_id=B1
```

Rules applied by the restore:

- **Anchor filtering** — only `receipt` rows matching every `--filter`
  (`column=value`, repeatable, combined with AND) are considered.
- **Reference closure** — suppliers, contracts and other rows the selected
  documents actually reference are pulled in too, including rows that
  reference the documents back (such as receipt lines). Data belonging to
  other branches that is never referenced is not imported.
- **Composite foreign keys are judged as a group** — a row only satisfies a
  multi-column foreign key when *all* referenced columns match; a partial
  single-column match never counts.
- **No overwrites** — a row already present in the target with the same key
  but different content is a conflict. It is left untouched and every anchor
  document depending on it is reported as blocked instead of being restored.
- **Closed cycles restore** — groups of rows that reference each other in a
  cycle are inserted as a set (foreign keys are checked afterwards with
  `PRAGMA foreign_key_check`).
- **Blocked documents are listed** — the report names each anchor document
  that could not be restored and why (`content_mismatch` or
  `missing_dependency`). The command exits `2` when any document is blocked,
  `0` otherwise. Re-running it is safe and inserts nothing twice.

Pass `--json` to get the full machine-readable report
(`imported`, `skipped_identical`, `blocked`, `conflicts`) instead of the
human-readable summary.
## Converting to JSON objects

Table rows are stored in the `.ndjson` files as newline-delimited JSON arrays, like this:

```
["a", "a", "a-a", 63, null, 0.7364712141640124, "$null"]
["a", "b", "a-b", 51, null, 0.6020187290499803, "$null"]
```

Sometimes it can be more convenient to work with a list of JSON objects.

The `sqlite-diffable objects` command can read a `.ndjson` file and its accompanying `.metadata.json` file and output JSON objects to standard output:
```bash
sqlite-diffable objects fixtures.db dump/sortable.ndjson
```
The output of that command looks something like this:
```
{"pk1": "a", "pk2": "a", "content": "a-a", "sortable": 63, "sortable_with_nulls": null, "sortable_with_nulls_2": 0.7364712141640124, "text": "$null"}
{"pk1": "a", "pk2": "b", "content": "a-b", "sortable": 51, "sortable_with_nulls": null, "sortable_with_nulls_2": 0.6020187290499803, "text": "$null"}
```

Add `-o` to write that output to a file:
```bash
sqlite-diffable objects fixtures.db dump/sortable.ndjson -o output.txt
```
Add `--array` to output a JSON array of objects, as opposed to a newline-delimited file:
```bash
sqlite-diffable objects fixtures.db dump/sortable.ndjson --array
```
Output:
```
[
{"pk1": "a", "pk2": "a", "content": "a-a", "sortable": 63, "sortable_with_nulls": null, "sortable_with_nulls_2": 0.7364712141640124, "text": "$null"},
{"pk1": "a", "pk2": "b", "content": "a-b", "sortable": 51, "sortable_with_nulls": null, "sortable_with_nulls_2": 0.6020187290499803, "text": "$null"}
]
```

## Storage format

Each table is represented as two files. The first, `table_name.metadata.json`, contains metadata describing the structure of the table. For a table called `redirects_redirect` that file might look like this:

```json
{
    "name": "redirects_redirect",
    "columns": [
        "id",
        "domain",
        "path",
        "target",
        "created"
    ],
    "schema": "CREATE TABLE [redirects_redirect] (\n   [id] INTEGER PRIMARY KEY,\n   [domain] TEXT,\n   [path] TEXT,\n   [target] TEXT,\n   [created] TEXT\n)"
}
```

It is an object with three keys: `name` is the name of the table, `columns` is an array of column strings and `schema` is the SQL schema text used for tha table.

The second file, `table_name.ndjson`, contains newline-delimited JSON (aka [JSON Lines](https://jsonlines.org/)) for every row in the table. Each row is represented as a JSON array with items corresponding to each of the columns defined in the metadata.

That file for the `redirects_redirect.ndjson` table might look like this:

```
[1, "feeds.simonwillison.net", "swn-everything", "https://simonwillison.net/atom/everything/", "2017-10-01T21:11:36.440537+00:00"]
[2, "feeds.simonwillison.net", "swn-entries", "https://simonwillison.net/atom/entries/", "2017-10-01T21:12:32.478849+00:00"]
[3, "feeds.simonwillison.net", "swn-links", "https://simonwillison.net/atom/links/", "2017-10-01T21:12:54.820729+00:00"]
```
