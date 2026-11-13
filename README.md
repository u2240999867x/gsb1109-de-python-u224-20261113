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
## Anonymized exports for external partners

Use the `anonymize` command to hand a backup to an outside partner without
revealing customer identity, while keeping the original equi-joins (for
example contracts joined to settlement statements) working inside that one
export:

```bash
sqlite-diffable anonymize billing.db partner-export/ \
  --spec anonymization-spec.json \
  --partner-id partner-a \
  --salt "$SQLITE_DIFFABLE_SALT"
```

The spec is a JSON file naming, per table, the key columns that carry
joinable identities (`keys`) and the columns holding direct customer
identity (`identity`). Key groups may be composite. When a foreign key has
a different column name from the parent key, give both groups the same
explicit `name` so their surrogate namespaces align:

```json
{
    "tables": {
        "contracts": {
            "keys": [
                {"name": "customer_ref", "columns": ["customer_id"]},
                ["region_code", "contract_no"]
            ],
            "identity": ["customer_name", "contact_phone"]
        },
        "settlements": {
            "keys": [
                {"name": "customer_ref", "columns": ["cust_id"]},
                ["region_code", "contract_no"]
            ]
        }
    }
}
```

Two different treatments are applied, and every export directory contains
an `anonymization-declaration.json` statement describing which is which:

- **Surrogate identity fields** (`keys`): each value is replaced with an
  HMAC-SHA256 pseudonym derived from the value, the key-group namespace and
  a per-partner secret salt. Equal source values map to equal surrogates
  within the same export, including across tables, so the original
  equi-joins keep working on these columns.
- **Isolated identity fields** (`identity`): direct customer identity such
  as names or phone numbers is pseudonymized with a partner- and
  column-specific key. Even when two columns hold the same raw value, their
  pseudonyms differ, and these columns must never be used for matching.

Matching semantics follow the source database:

- Composite keys are hashed as a tuple; each component is exported with its
  position marker so it can only join back to the corresponding component.
- If any component of a composite key tuple is NULL, every component of that
  tuple is exported as NULL. Such a row never matches a parent row, just as
  SQL NULL never equals NULL.
- Surrogates are namespaced by partner id and salt. Directories produced
  for different partners cannot be cross-recognised by their surrogate
  values, even for identical source data. Reuse the same salt when you
  re-export for the same partner and need stable pseudonyms.
- Columns listed nowhere in the spec are non-identifying business data and
  are exported unchanged.

### Verifying that joins are preserved

The `verify-joins` command loads an anonymized export into a temporary
database and compares it against the source database for one join. The
total number of rows returned by the join and the number of times each
left-table row matches (including rows that match zero times) must be
identical before and after export:

```bash
sqlite-diffable verify-joins billing.db partner-export/ \
  --spec anonymization-spec.json \
  --left-table settlements \
  --right-table contracts \
  --on customer_id=customer_id
```

Repeat `--on LEFT=RIGHT` once per column for composite keys. The command
fails with a non-zero exit code if the join statistics differ.

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
