"""Dependency discovery and restore helpers for views.

The functions in this module only ever store a view's ``CREATE VIEW``
statement.  Rows are read from the base tables that the view depends on, which
means a restored view keeps following future changes to those tables.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


# ---------------------------------------------------------------------------
# SQL tokenising / table-reference discovery
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
    |(?P<line_comment>--[^\n]*)
    |(?P<block_comment>/\*.*?\*/)
    |(?P<string>'(?:[^']|'')*')
    |(?P<quoted>"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\])
    |(?P<word>[A-Za-z_][A-Za-z0-9_$]*)
    |(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
    |(?P<punct>[(),.;])
    |(?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

# Keywords that end a FROM list / are never table names unless quoted.
_SQL_KEYWORDS = {
    "abort",
    "action",
    "add",
    "after",
    "all",
    "alter",
    "always",
    "analyze",
    "and",
    "as",
    "asc",
    "attach",
    "autoincrement",
    "before",
    "begin",
    "between",
    "by",
    "cascade",
    "case",
    "cast",
    "check",
    "collate",
    "column",
    "commit",
    "conflict",
    "constraint",
    "create",
    "cross",
    "current",
    "current_date",
    "current_time",
    "current_timestamp",
    "database",
    "default",
    "deferrable",
    "deferred",
    "delete",
    "desc",
    "detach",
    "distinct",
    "do",
    "drop",
    "each",
    "else",
    "end",
    "escape",
    "except",
    "exclude",
    "exclusive",
    "exists",
    "explain",
    "fail",
    "filter",
    "first",
    "following",
    "for",
    "foreign",
    "from",
    "full",
    "generated",
    "glob",
    "group",
    "groups",
    "having",
    "if",
    "ignore",
    "immediate",
    "in",
    "index",
    "indexed",
    "initially",
    "inner",
    "insert",
    "instead",
    "intersect",
    "into",
    "is",
    "isnull",
    "join",
    "key",
    "last",
    "left",
    "like",
    "limit",
    "match",
    "materialized",
    "natural",
    "no",
    "not",
    "nothing",
    "notnull",
    "null",
    "nulls",
    "of",
    "offset",
    "on",
    "or",
    "order",
    "others",
    "outer",
    "over",
    "partition",
    "plan",
    "pragma",
    "preceding",
    "primary",
    "query",
    "raise",
    "range",
    "recursive",
    "references",
    "regexp",
    "reindex",
    "release",
    "rename",
    "replace",
    "restrict",
    "returning",
    "right",
    "rollback",
    "row",
    "rows",
    "savepoint",
    "select",
    "set",
    "table",
    "temp",
    "temporary",
    "then",
    "ties",
    "to",
    "transaction",
    "trigger",
    "unbounded",
    "union",
    "unique",
    "update",
    "using",
    "vacuum",
    "values",
    "view",
    "virtual",
    "when",
    "where",
    "window",
    "with",
    "without",
}

_FROM_TERMINATORS = {
    "where",
    "group",
    "order",
    "having",
    "limit",
    "union",
    "except",
    "intersect",
    "window",
    "returning",
    "values",
    "set",
}


@dataclass(frozen=True)
class _Token:
    kind: str
    raw: str
    value: str
    start: int
    end: int


def _unquote_identifier(raw: str) -> str:
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1].replace('""', '"')
    if raw.startswith("`") and raw.endswith("`"):
        return raw[1:-1].replace("``", "`")
    if raw.startswith("[") and raw.endswith("]"):
        return raw[1:-1].replace("]]", "]")
    return raw


def _tokens(sql: str) -> List[_Token]:
    tokens: List[_Token] = []
    for match in _TOKEN_RE.finditer(sql):
        kind = match.lastgroup
        raw = match.group(0)
        if kind in ("space", "line_comment", "block_comment"):
            continue
        if kind == "quoted":
            value = _unquote_identifier(raw)
        else:
            value = raw
        tokens.append(_Token(kind, raw, value, match.start(), match.end()))
    return tokens


def _is_identifier(token: _Token) -> bool:
    if token.kind == "quoted":
        return True
    if token.kind == "word":
        return token.value.lower() not in _SQL_KEYWORDS
    return False


def _qualified_name(
    tokens: Sequence[_Token], index: int
) -> Tuple[Optional[str], str, int]:
    """Return (schema, object, index-of-last-consumed-token)."""
    token = tokens[index]
    schema: Optional[str] = None
    name = token.value
    last = index
    if (
        index + 2 < len(tokens)
        and tokens[index + 1].kind == "punct"
        and tokens[index + 1].value == "."
        and _is_identifier(tokens[index + 2])
    ):
        schema = name
        name = tokens[index + 2].value
        last = index + 2
    return schema, name, last


def table_references(sql: str) -> List[Tuple[Optional[str], str]]:
    """Best-effort ordered table/view references found in a SELECT statement.

    This is deliberately only used to decide which placeholder tables to create
    in the scratch database.  The final dependency list comes from SQLite's
    authorizer, so CTE names that shadow a real table are handled by SQLite
    itself rather than by this small tokeniser.
    """
    tokens = _tokens(sql)
    references: List[Tuple[Optional[str], str]] = []
    seen: Set[Tuple[str, str]] = set()
    in_from: Dict[int, bool] = {}
    expect_depth: Optional[int] = None
    depth = 0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "punct" and token.value == "(":
            depth += 1
            expect_depth = None
            i += 1
            continue
        if token.kind == "punct" and token.value == ")":
            in_from.pop(depth, None)
            depth = max(0, depth - 1)
            expect_depth = None
            i += 1
            continue

        upper = token.value.lower() if token.kind == "word" else None

        if upper == "from" or upper == "join":
            in_from[depth] = True
            expect_depth = depth
            i += 1
            continue

        if expect_depth is not None and depth == expect_depth:
            if _is_identifier(token):
                schema, name, last = _qualified_name(tokens, i)
                # A table-valued function is not a database object.
                if not (
                    last + 1 < len(tokens)
                    and tokens[last + 1].kind == "punct"
                    and tokens[last + 1].value == "("
                ):
                    key = ((schema or "").lower(), name.lower())
                    if key not in seen:
                        seen.add(key)
                        references.append((schema, name))
                expect_depth = None
                i = last + 1
                continue
            # A parenthesised subquery or something unrecognised.
            expect_depth = None

        if token.kind == "punct" and token.value == "," and in_from.get(depth):
            expect_depth = depth
            i += 1
            continue

        if upper in _FROM_TERMINATORS and depth in in_from:
            in_from[depth] = False

        i += 1
    return references


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _dummy_columns(sql: str) -> List[str]:
    columns: List[str] = []
    seen: Set[str] = set()
    for token in _tokens(sql):
        if not _is_identifier(token):
            continue
        name = token.value
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        columns.append(name)
        if len(columns) >= 1500:
            break
    if not columns:
        columns = ["__value__"]
    return columns


def _view_name_from_sql(sql: str) -> Optional[str]:
    match = re.match(
        r"\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?VIEW\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?"
        r'("(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]|[^\s(]+)',
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return _unquote_identifier(match.group(1))


def object_type(sql: Optional[str], declared_type: Optional[str] = None) -> str:
    if declared_type:
        declared = str(declared_type).lower()
        if declared in ("table", "view", "trigger"):
            return declared
    if not sql:
        return "table"
    if re.match(r"\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?VIEW\b", sql, re.IGNORECASE):
        return "view"
    if re.match(r"\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?TRIGGER\b", sql, re.IGNORECASE):
        return "trigger"
    return "table"


def cte_names(sql: str) -> Set[str]:
    """Best-effort collection of CTE names visible in a statement."""
    tokens = _tokens(sql)
    names: Set[str] = set()
    depth = 0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "punct" and token.value == "(":
            depth += 1
        elif token.kind == "punct" and token.value == ")":
            depth = max(0, depth - 1)
        elif token.kind == "word" and token.value.lower() == "with":
            j = i + 1
            if (
                j < len(tokens)
                and tokens[j].kind == "word"
                and tokens[j].value.lower() == "recursive"
            ):
                j += 1
            while j < len(tokens) and _is_identifier(tokens[j]):
                names.add(tokens[j].value.lower())
                j += 1
                # Optional column list for this CTE.
                if (
                    j < len(tokens)
                    and tokens[j].kind == "punct"
                    and tokens[j].value == "("
                ):
                    level = 0
                    while j < len(tokens):
                        if tokens[j].kind == "punct" and tokens[j].value == "(":
                            level += 1
                        elif tokens[j].kind == "punct" and tokens[j].value == ")":
                            level -= 1
                            if level == 0:
                                j += 1
                                break
                        j += 1
                if (
                    j < len(tokens)
                    and tokens[j].kind == "word"
                    and tokens[j].value.lower() == "as"
                ):
                    j += 1
                    if (
                        j < len(tokens)
                        and tokens[j].kind == "word"
                        and tokens[j].value.lower() not in ("not", "materialized")
                    ):
                        continue
                    if (
                        j < len(tokens)
                        and tokens[j].kind == "word"
                        and tokens[j].value.lower() in ("not", "materialized")
                    ):
                        while (
                            j < len(tokens)
                            and tokens[j].kind == "word"
                            and tokens[j].value.lower() in ("not", "materialized")
                        ):
                            j += 1
                    if (
                        j < len(tokens)
                        and tokens[j].kind == "punct"
                        and tokens[j].value == "("
                    ):
                        level = 0
                        while j < len(tokens):
                            if tokens[j].kind == "punct" and tokens[j].value == "(":
                                level += 1
                            elif tokens[j].kind == "punct" and tokens[j].value == ")":
                                level -= 1
                                if level == 0:
                                    j += 1
                                    break
                            j += 1
                if (
                    j < len(tokens)
                    and tokens[j].kind == "punct"
                    and tokens[j].value == ","
                ):
                    j += 1
                    continue
                break
            i = j
            continue
        i += 1
    return names


def direct_dependencies(
    sql: str,
    view_name: Optional[str] = None,
    extra_functions: Optional[Iterable[str]] = None,
) -> List[Tuple[Optional[str], str]]:
    """Return the objects directly read by a view's SQL.

    SQLite's authorizer reports the tables that are actually read after all
    name resolution.  Scratch placeholder tables are created for every
    candidate reference, which lets planning proceed even when a base table is
    absent.  A CTE name with the same spelling as a real table is resolved by
    SQLite as a CTE, so it does not leak into the dependency list.
    """
    actual_name = _view_name_from_sql(sql)
    if actual_name is None:
        raise ValueError("Could not determine view name from schema")
    if view_name is None:
        view_name = actual_name
    is_temp = bool(re.match(r"\s*CREATE\s+TEMP(?:ORARY)?\s+VIEW\b", sql, re.IGNORECASE))

    candidates = table_references(sql)
    conn = sqlite3.connect(":memory:")
    try:
        columns = _dummy_columns(sql)
        created: Set[Tuple[str, str]] = set()
        for schema, name in candidates:
            schema_name = (schema or "main").lower()
            if schema_name not in ("main", "temp"):
                try:
                    conn.execute(
                        "ATTACH DATABASE ':memory:' AS {}".format(
                            _quote_identifier(schema_name)
                        )
                    )
                except sqlite3.Error:
                    pass
            if name.lower() == actual_name.lower() and schema_name in (
                "main",
                "temp",
            ):
                # Never create a placeholder that would hide the view itself.
                continue
            key = (schema_name, name.lower())
            if key in created:
                continue
            created.add(key)
            # A unique marker column prevents NATURAL/USING joins over
            # identical dummy schemas from being optimised into a single scan.
            sql_columns = ", ".join(
                [_quote_identifier(c) for c in columns]
                + [_quote_identifier("__diffable_marker_{}".format(len(created)))]
            )
            conn.execute(
                "CREATE TABLE {}.{} ({})".format(
                    _quote_identifier(schema_name),
                    _quote_identifier(name),
                    sql_columns,
                )
            )

        for function_name in extra_functions or ():
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", function_name):
                conn.create_function(function_name, -1, lambda *args: None)
        conn.execute(sql)
        target = (
            "temp.{}".format(_quote_identifier(actual_name))
            if is_temp
            else _quote_identifier(actual_name)
        )

        found: Set[Tuple[str, str]] = set()

        def authorizer(action, arg1, arg2, dbname, source):
            if action == sqlite3.SQLITE_READ and arg1:
                found.add(((dbname or "main").lower(), str(arg1).lower()))
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        try:
            try:
                conn.execute("SELECT * FROM {} LIMIT 0".format(target)).fetchall()
            except sqlite3.OperationalError:
                # Functions provided by the application or enabled extensions
                # may be unavailable in this scratch connection.  Fall back to
                # lexical references while still removing CTE names, which are
                # exactly the "temporary alias" case that shadows a real table.
                ctes = cte_names(sql)
                found = set()
                for schema, name in candidates:
                    schema_name = (schema or "main").lower()
                    if name.lower() in ctes:
                        continue
                    found.add((schema_name, name.lower()))
        finally:
            conn.set_authorizer(None)
    except sqlite3.Error:
        # The view could not even be created in the scratch database (for
        # example it relies on a function or module).  Keep the lexical
        # references, but never let a CTE alias masquerade as a real table.
        ctes = cte_names(sql)
        found = {
            ((schema or "main").lower(), name.lower())
            for schema, name in candidates
            if name.lower() not in ctes
        }
    finally:
        conn.close()

    result: List[Tuple[Optional[str], str]] = []
    for schema, name in candidates:
        schema_name = (schema or "main").lower()
        if (schema_name, name.lower()) in found:
            result.append((schema, name))
    return result


# ---------------------------------------------------------------------------
# Dependency graph / diffable directory index
# ---------------------------------------------------------------------------


class MissingDependencyError(Exception):
    def __init__(self, chain: Sequence[str]):
        self.chain = list(chain)
        super().__init__(
            "Missing database object for view dependency chain: "
            + " -> ".join(self.chain)
        )


class CyclicDependencyError(Exception):
    def __init__(self, chain: Sequence[str]):
        self.chain = list(chain)
        super().__init__("Cyclic view dependency chain: " + " -> ".join(self.chain))


@dataclass
class DatabaseObject:
    name: str
    type: str
    schema: str
    columns: List[str] = field(default_factory=list)
    ndjson_path: Optional[pathlib.Path] = None

    @property
    def is_view(self) -> bool:
        return self.type == "view"

    @property
    def is_trigger(self) -> bool:
        return self.type == "trigger"


def index_from_connection(conn: sqlite3.Connection) -> Dict[str, DatabaseObject]:
    objects: Dict[str, DatabaseObject] = {}
    rows = conn.execute(
        "SELECT name, type, sql FROM main.sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    for name, kind, sql in rows:
        objects[name.lower()] = DatabaseObject(name=name, type=kind, schema=sql or "")
    return objects


def index_from_directory(directory: pathlib.Path) -> Dict[str, DatabaseObject]:
    directory = pathlib.Path(directory)
    objects: Dict[str, DatabaseObject] = {}
    for metadata_path in sorted(directory.glob("*.metadata.json")):
        try:
            info = json.loads(metadata_path.read_text())
        except (OSError, ValueError) as ex:
            raise ValueError(
                "Invalid metadata file {}: {}".format(metadata_path, ex)
            ) from ex
        if not isinstance(info, dict):
            raise ValueError(
                "Invalid metadata file {}: expected a JSON object".format(metadata_path)
            )
        name = info.get("name")
        if not name:
            name = metadata_path.name[: -len(".metadata.json")]
        schema = info.get("schema") or ""
        kind = object_type(schema, info.get("type"))
        expected_ndjson = metadata_path.with_name(
            metadata_path.name[: -len(".metadata.json")] + ".ndjson"
        )
        ndjson_path = expected_ndjson
        if not expected_ndjson.exists():
            # Accept directories where a "/" was stripped from the file name
            # but kept in the metadata "name".
            alternative = metadata_path.parent / "{}.ndjson".format(
                str(name).replace("/", "")
            )
            if alternative.exists():
                ndjson_path = alternative
        objects[name.lower()] = DatabaseObject(
            name=name,
            type=kind,
            schema=schema,
            columns=list(info.get("columns") or []),
            ndjson_path=ndjson_path,
        )
    return objects


def _dependency_name(schema: Optional[str], name: str) -> str:
    if schema and schema.lower() not in ("main", "temp"):
        return "{}.{}".format(schema, name)
    return name


def dependency_closure(
    objects: Dict[str, DatabaseObject],
    view_name: str,
    satisfied_names: Optional[Iterable[str]] = None,
) -> List[DatabaseObject]:
    """Return view dependencies in creation order (dependencies first).

    Only tables and views take part.  Triggers are maintenance objects and are
    never needed to recompute a read-only view.  ``satisfied_names`` lists
    objects that already exist in the destination database; a dependency that
    is absent from the diffable directory but present there is left alone.
    """
    satisfied = {name.lower() for name in (satisfied_names or ())}
    target = objects.get(view_name.lower())
    if target is None:
        raise MissingDependencyError([view_name])
    if not target.is_view:
        raise ValueError("{} is not a view".format(view_name))

    ordered: List[DatabaseObject] = []
    state: Dict[str, str] = {}

    def visit(obj: DatabaseObject, stack: List[str]) -> None:
        key = obj.name.lower()
        if state.get(key) == "done":
            return
        if state.get(key) == "visiting":
            raise CyclicDependencyError(stack + [obj.name])
        state[key] = "visiting"
        stack.append(obj.name)
        try:
            if obj.is_view:
                for schema, name in direct_dependencies(obj.schema, obj.name):
                    display = _dependency_name(schema, name)
                    dependency = objects.get(name.lower())
                    if dependency is None:
                        if name.lower() in satisfied:
                            continue
                        raise MissingDependencyError(stack + [display])
                    visit(dependency, stack)
            ordered.append(obj)
            state[key] = "done"
        finally:
            stack.pop()

    visit(target, [])
    return ordered


# ---------------------------------------------------------------------------
# Dump / restore helpers
# ---------------------------------------------------------------------------


def metadata_for_object(conn: sqlite3.Connection, name: str, kind: str) -> dict:
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA main.table_info({})".format(_quote_identifier(name))
        ).fetchall()
    ]
    schema = conn.execute(
        "SELECT sql FROM main.sqlite_master WHERE type = ? AND lower(name) = ?",
        (kind, name.lower()),
    ).fetchone()[0]
    info = {"name": name, "columns": columns, "schema": schema}
    if kind == "view":
        info["type"] = "view"
    return info


def write_table_dump(conn: sqlite3.Connection, output: pathlib.Path, name: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    ndjson_path = output / "{}.ndjson".format(name.replace("/", ""))
    metadata_path = output / "{}.metadata.json".format(name.replace("/", ""))
    cursor = conn.execute("SELECT * FROM main.{}".format(_quote_identifier(name)))
    with ndjson_path.open("w") as fp:
        for row in cursor:
            fp.write(json.dumps(list(row), default=repr) + "\n")
    info = metadata_for_object(conn, name, "table")
    metadata_path.write_text(json.dumps(info, indent=4))


def write_view_dump(conn: sqlite3.Connection, output: pathlib.Path, name: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    stem = name.replace("/", "")
    metadata_path = output / "{}.metadata.json".format(stem)
    ndjson_path = output / "{}.ndjson".format(stem)
    info = metadata_for_object(conn, name, "view")
    metadata_path.write_text(json.dumps(info, indent=4))
    # A view must never be restored from a stored snapshot of its current rows.
    if ndjson_path.exists():
        ndjson_path.unlink()


def write_trigger_dump(
    conn: sqlite3.Connection, output: pathlib.Path, name: str
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    stem = name.replace("/", "")
    metadata_path = output / "{}.metadata.json".format(stem)
    ndjson_path = output / "{}.ndjson".format(stem)
    schema = conn.execute(
        "SELECT sql FROM main.sqlite_master WHERE type = 'trigger' AND lower(name) = ?",
        (name.lower(),),
    ).fetchone()[0]
    metadata_path.write_text(
        json.dumps(
            {
                "name": name,
                "columns": [],
                "type": "trigger",
                "schema": schema,
            },
            indent=4,
        )
    )
    if ndjson_path.exists():
        ndjson_path.unlink()


def write_closure_dump(
    conn: sqlite3.Connection,
    output: pathlib.Path,
    view_name: str,
) -> List[DatabaseObject]:
    """Dump a view's transitive table/view closure to a diffable directory."""
    objects = index_from_connection(conn)
    closure = dependency_closure(objects, view_name)
    for obj in closure:
        if obj.is_view:
            write_view_dump(conn, output, obj.name)
        else:
            write_table_dump(conn, output, obj.name)
    return closure


# ---------------------------------------------------------------------------
# Restore helpers
# ---------------------------------------------------------------------------


def _unique_objects(objects: Sequence[DatabaseObject]) -> List[DatabaseObject]:
    seen: Set[str] = set()
    result: List[DatabaseObject] = []
    for obj in objects:
        if obj.name.lower() in seen:
            continue
        seen.add(obj.name.lower())
        result.append(obj)
    return result


def objects_for_restore(
    objects: Dict[str, DatabaseObject],
    requested: Optional[Iterable[str]] = None,
    satisfied_names: Optional[Iterable[str]] = None,
) -> List[DatabaseObject]:
    """Pick the objects needed to restore ``requested`` names.

    With no requested names every table and view in the directory is selected;
    views are still ordered after their dependencies.  Triggers are always
    excluded because they are optional maintenance objects, not inputs to a
    read-only view query.
    """
    satisfied = {name.lower() for name in (satisfied_names or ())}
    if requested:
        selected: List[DatabaseObject] = []
        for name in requested:
            obj = objects.get(name.lower())
            if obj is None:
                raise MissingDependencyError([name])
            if obj.is_trigger:
                continue
            if obj.is_view:
                selected.extend(
                    dependency_closure(objects, obj.name, satisfied_names=satisfied)
                )
            else:
                selected.append(obj)
        return _unique_objects(selected)

    selected = []
    tables = sorted(
        (obj for obj in objects.values() if obj.type == "table"),
        key=lambda obj: obj.name.lower(),
    )
    selected.extend(tables)
    views = sorted(
        (obj for obj in objects.values() if obj.is_view),
        key=lambda obj: obj.name.lower(),
    )
    for view in views:
        selected.extend(
            dependency_closure(objects, view.name, satisfied_names=satisfied)
        )
    # Plain loads may include triggers, but they are appended by the caller.
    return _unique_objects(selected)


def _coerce_row_value(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=repr, ensure_ascii=False)
    return value


def restore_table(conn: sqlite3.Connection, obj: DatabaseObject, replace: bool) -> None:
    existing = conn.execute(
        "SELECT type FROM main.sqlite_master WHERE lower(name) = ?",
        (obj.name.lower(),),
    ).fetchone()
    if existing:
        if not replace:
            raise sqlite3.OperationalError("table {} already exists".format(obj.name))
        conn.execute(
            "DROP {} {}".format(
                "VIEW" if existing[0] == "view" else "TABLE",
                _quote_identifier(obj.name),
            )
        )
    conn.execute(obj.schema)
    columns = obj.columns
    if obj.ndjson_path is None or not obj.ndjson_path.exists():
        return
    if not columns:
        columns = [
            row[1]
            for row in conn.execute(
                "PRAGMA main.table_info({})".format(_quote_identifier(obj.name))
            ).fetchall()
        ]
    with obj.ndjson_path.open() as fp:
        for line in fp:
            if not line.strip():
                continue
            row = json.loads(line)
            # Match the historical loader: zip values with declared columns,
            # so a short row leaves later columns to their defaults.
            record = dict(zip(columns, row))
            if not record:
                continue
            keys = [column for column in columns if column in record]
            insert = "INSERT INTO main.{} ({}) VALUES ({})".format(
                _quote_identifier(obj.name),
                ", ".join(_quote_identifier(key) for key in keys),
                ", ".join("?" for _ in keys),
            )
            conn.execute(insert, [_coerce_row_value(record[key]) for key in keys])


def restore_view(conn: sqlite3.Connection, obj: DatabaseObject, replace: bool) -> None:
    existing = conn.execute(
        "SELECT type FROM main.sqlite_master WHERE lower(name) = ?",
        (obj.name.lower(),),
    ).fetchone()
    if existing:
        if not replace:
            raise sqlite3.OperationalError("view {} already exists".format(obj.name))
        conn.execute(
            "DROP {} {}".format(
                "VIEW" if existing[0] == "view" else "TABLE",
                _quote_identifier(obj.name),
            )
        )
    conn.execute(obj.schema)


def restore_trigger(
    conn: sqlite3.Connection, obj: DatabaseObject, replace: bool
) -> None:
    existing = conn.execute(
        "SELECT type FROM main.sqlite_master WHERE lower(name) = ?",
        (obj.name.lower(),),
    ).fetchone()
    if existing:
        if not replace:
            raise sqlite3.OperationalError("trigger {} already exists".format(obj.name))
        conn.execute("DROP TRIGGER {}".format(_quote_identifier(obj.name)))
    conn.execute(obj.schema)
