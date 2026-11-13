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

For tables declared with `AUTOINCREMENT`, two extra keys are archived:

```json
{
    "name": "invoices",
    "columns": ["id", "no"],
    "schema": "CREATE TABLE [invoices] (\n   [id] INTEGER PRIMARY KEY AUTOINCREMENT,\n   [no] TEXT\n)",
    "autoincrement": true,
    "sequence": 3
}
```

`sequence` is the high-water mark from SQLite's internal `sqlite_sequence`
table at dump time, i.e. the largest id ever allocated (including ids whose
rows have since been deleted). On load that value is written back so the
restored table keeps allocating ids past numbers that were already used or
voided. A `null` value means the table had never held a row.

The internal `sqlite_sequence` table itself is never dumped as rows; its
state is carried inside these metadata files. Asking for it explicitly just
prints a skip message.

## AUTOINCREMENT semantics and restoring old dumps

The three common integer-key shapes behave differently, and only the rows
plus the archived sequence can be restored exactly:

- **Ordinary `INTEGER PRIMARY KEY`** (no `AUTOINCREMENT`): SQLite computes
  the next id from `max(rowid) + 1` of the surviving rows. Restoring the rows
  is enough; nothing else is archived.
- **Explicitly written large numbers on an ordinary key**: if those rows were
  deleted before the dump, SQLite itself has forgotten them, so the gap
  cannot be preserved by any backup. The next allocation follows
  `max(rowid) + 1` again — declare the column `AUTOINCREMENT` if voided
  numbers must never be reused.
- **`AUTOINCREMENT` tables, including emptied ones**: the high-water mark
  lives in `sqlite_sequence`, not in the rows, so it is archived in the
  metadata even when the table contains no rows at all.

When loading a directory produced by an older version of this tool:

- If it contains a `sqlite_sequence.ndjson` file, those high-water marks are
  applied automatically (the reserved table name is no longer loaded
  directly).
- If the directory contains only the row files and basic metadata, every row
  is still restored exactly, but sequence state is limited to
  `max(rowid)` of the surviving rows. A warning is printed for each affected
  table: numbers assigned to rows deleted before the dump cannot be
  reconstructed and may be reissued, so an emptied `AUTOINCREMENT` table will
  restart at 1. Re-dump with the current version to capture full sequence
  state.

The second file, `table_name.ndjson`, contains newline-delimited JSON (aka [JSON Lines](https://jsonlines.org/)) for every row in the table. Each row is represented as a JSON array with items corresponding to each of the columns defined in the metadata.

That file for the `redirects_redirect.ndjson` table might look like this:

```
[1, "feeds.simonwillison.net", "swn-everything", "https://simonwillison.net/atom/everything/", "2017-10-01T21:11:36.440537+00:00"]
[2, "feeds.simonwillison.net", "swn-entries", "https://simonwillison.net/atom/entries/", "2017-10-01T21:12:32.478849+00:00"]
[3, "feeds.simonwillison.net", "swn-links", "https://simonwillison.net/atom/links/", "2017-10-01T21:12:54.820729+00:00"]
```
