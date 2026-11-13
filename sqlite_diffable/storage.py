"""Typed cell encoding for the NDJSON storage format.

The original format serialised every SQLite value with ``json.dumps`` using
``default=repr``. A BLOB such as ``b"\\x00"`` therefore became the JSON string
``"b'\\\\x00'"``, indistinguishable from a TEXT value containing those exact
characters.

The typed format keeps JSON null, numbers and strings for SQLite NULL,
INTEGER/REAL and TEXT. BLOBs are written as tagged objects::

    {"$sqlite": "blob", "hex": "00ff"}

Legacy values whose text could have been either a BLOB repr or actual TEXT
are written as pending cells during migration and must be confirmed before
they can be loaded.
"""

import ast
import json
import pathlib
from typing import Any, Iterable, Optional, Sequence

FORMAT = "sqlite-diffable-typed"
VERSION = 2
MANIFEST_NAME = ".sqlite-diffable.json"

TAG_KEY = "$sqlite"
TAG_BLOB = "blob"
TAG_PENDING = "pending"
TAG_TEXT = "text"
TAG_REPR = "repr"


class PendingCellError(ValueError):
    def __init__(self, table: str, line_number: int, column: str, cell: dict):
        self.table = table
        self.line_number = line_number
        self.column = column
        self.cell = cell
        super().__init__(
            "Table {!r} row {} column {!r} is pending confirmation".format(
                table, line_number, column
            )
        )


class UnknownCellError(ValueError):
    pass


class MigrationError(ValueError):
    pass


def encode_cell(value: Any) -> Any:
    """Encode one value returned by sqlite3 as a JSON-safe typed cell."""
    if isinstance(value, bytes):
        return {TAG_KEY: TAG_BLOB, "hex": value.hex()}
    if isinstance(value, (bytearray, memoryview)):
        return {TAG_KEY: TAG_BLOB, "hex": bytes(value).hex()}
    if value is None or isinstance(value, (int, float, str)):
        return value
    # sqlite3 only returns the types above, but keeping a tagged value is
    # safer than silently turning an unexpected object into readable text.
    return {TAG_KEY: TAG_REPR, "repr": repr(value)}


def decode_cell(
    cell: Any,
    table: Optional[str] = None,
    line_number: Optional[int] = None,
    column: Optional[str] = None,
) -> Any:
    """Decode a typed cell to the value that should be inserted into SQLite."""
    if not isinstance(cell, dict):
        return cell

    tag = cell.get(TAG_KEY)
    if tag == TAG_BLOB:
        return _blob_bytes(cell, ("hex",))
    if tag == TAG_PENDING:
        raise PendingCellError(table, line_number, column, cell)
    if tag == TAG_REPR:
        raise UnknownCellError(
            "Cannot restore tagged repr value {!r} without a storage class".format(
                cell.get("repr")
            )
        )
    if tag == TAG_TEXT:
        value = cell.get("value")
        if not isinstance(value, str):
            raise UnknownCellError(
                "Tagged text cell is missing a string value: {!r}".format(cell)
            )
        return value

    # Tolerate the small variations a hand-edited typed cell might use.
    if cell.get("$blob") is not None:
        return _blob_bytes(cell, ("$blob",))
    if TAG_BLOB in (cell.get("type"), cell.get("$type")):
        return _blob_bytes(cell, ("hex", "value"))

    raise UnknownCellError("Unknown typed cell: {!r}".format(cell))


def _blob_bytes(cell: dict, keys: Sequence[str]) -> bytes:
    for key in keys:
        if key in cell:
            value = cell[key]
            if not isinstance(value, str):
                break
            try:
                return bytes.fromhex(value)
            except ValueError:
                raise UnknownCellError("Invalid BLOB hex value: {!r}".format(value))
    raise UnknownCellError("BLOB cell is missing a hex string: {!r}".format(cell))


def is_pending(cell: Any) -> bool:
    return isinstance(cell, dict) and cell.get(TAG_KEY) == TAG_PENDING


def is_blob(cell: Any) -> bool:
    if not isinstance(cell, dict):
        return False
    return (
        cell.get(TAG_KEY) == TAG_BLOB
        or cell.get("$blob") is not None
        or TAG_BLOB in (cell.get("type"), cell.get("$type"))
    )


def pending_cell(old_value: Any, blob_bytes: bytes) -> dict:
    """Build the marker used for an unresolved legacy cell.

    Both legal interpretations are kept in the marker so a reviewer can
    confirm the cell by hand without consulting any other file.
    """
    return {
        TAG_KEY: TAG_PENDING,
        "status": "needs-review",
        "reason": "legacy-value-is-valid-bytes-literal",
        "legacy": old_value,
        "candidates": [
            {TAG_KEY: TAG_BLOB, "hex": blob_bytes.hex()},
            {TAG_KEY: TAG_TEXT, "value": old_value},
        ],
    }


def pending_legacy_value(cell: dict) -> Any:
    "The old JSON value retained by a pending cell marker."
    if "legacy" in cell:
        return cell["legacy"]
    if "old" in cell:
        return cell["old"]
    raise UnknownCellError(
        "Pending cell is missing its legacy value: {!r}".format(cell)
    )


def ambiguous_bytes(value: Any) -> Optional[bytes]:
    """Return bytes when a legacy string is exactly ``repr()`` of a BLOB.

    The old exporter used ``repr()`` for BLOB values, so only the canonical
    bytes repr has two legal interpretations. A JSON string such as
    ``b"abc"`` is a valid Python bytes literal but its repr is ``b'abc'``,
    so it was never produced by the old exporter and is TEXT.
    """
    if not isinstance(value, str):
        return None
    if len(value) < 3 or value[0] != "b" or value[1] not in ("'", '"'):
        return None
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError, MemoryError):
        return None
    if isinstance(parsed, bytes) and repr(parsed) == value:
        return parsed
    return None


def legacy_value(type_name: str, value: Any) -> Any:
    """How the old exporter represented a value with this SQLite type."""
    if type_name == "null":
        return None
    if type_name == "integer":
        return int(value)
    if type_name == "real":
        return float(value)
    if type_name == "text":
        return value
    if type_name == "blob":
        return repr(bytes(value))
    raise MigrationError("Unknown SQLite storage class: {!r}".format(type_name))


def manifest_path(directory: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(directory) / MANIFEST_NAME


def read_manifest(directory: pathlib.Path) -> dict:
    path = manifest_path(directory)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return {}
    return data


def write_manifest(directory: pathlib.Path, tables: Iterable[str] = ()) -> None:
    directory = pathlib.Path(directory)
    manifest = read_manifest(directory)
    known = manifest.get("tables")
    if not isinstance(known, dict):
        known = {}
    for table in tables:
        known[table] = VERSION
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "tables": dict(sorted(known.items())),
    }
    manifest_path(directory).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8"
    )


def manifest_says_typed(directory: pathlib.Path, table: str) -> bool:
    manifest = read_manifest(directory)
    tables = manifest.get("tables")
    if isinstance(tables, dict):
        return tables.get(table) == VERSION
    return manifest.get("version") == VERSION


def ndjson_path(directory: pathlib.Path, table: str) -> pathlib.Path:
    return pathlib.Path(directory) / "{}.ndjson".format(table.replace("/", ""))


def row_uses_typed_cells(row: Sequence[Any]) -> bool:
    for cell in row:
        if is_blob(cell) or is_pending(cell):
            return True
        if isinstance(cell, dict) and cell.get(TAG_KEY) in (
            TAG_BLOB,
            TAG_PENDING,
            TAG_REPR,
            TAG_TEXT,
        ):
            return True
    return False


def file_uses_typed_cells(path: pathlib.Path) -> bool:
    """Detect typed cells even if the manifest is missing.

    Old exports only ever contained JSON scalars in a row array, so a tagged
    object anywhere in the file is a reliable marker. This lets hand-created
    typed fixtures load without requiring a manifest.
    """
    try:
        with path.open() as fp:
            for line in fp:
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, list) and row_uses_typed_cells(row):
                    return True
    except (OSError, ValueError):
        return False
    return False


def table_is_typed(directory: pathlib.Path, table: str) -> bool:
    return manifest_says_typed(directory, table) or file_uses_typed_cells(
        ndjson_path(directory, table)
    )
