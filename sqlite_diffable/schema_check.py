"""Behavioural comparison of two SQLite export definitions.

The input is two sets of object definitions (the contents of one or more
``CREATE`` statements, for example the ``*.metadata.json`` files produced
by ``sqlite-diffable dump``) plus the set of operations the business
allows against the database.  Each definition is materialised in a
throwaway database and the operations are replayed against both; a
difference in returned rows, raised constraint or resulting rows is a
behavioural difference.

Purely cosmetic spelling changes (quote style, keyword case, whitespace)
are normalised away.  Differences that can change results - collations,
partial or unique index predicates, trigger ``WHEN`` clauses, checks and
defaults - are found by executing the operations, not by comparing SQL
text.

Statements that cannot be replayed are reported separately and never
silently counted as equivalent.  A difference demonstrated by a replayed
operation is still reported as different; anything that would depend on
a statement left out of the replay is not.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sqlite3


class DefinitionError(Exception):
    """Raised when a definition cannot be split or read."""


class OperationError(Exception):
    """Raised when an operation specification is malformed."""


# ---------------------------------------------------------------------------
# Splitting and canonicalising definitions


def split_statements(sql):
    """Split a SQL script into complete statements.

    ``sqlite3.complete_statement`` only reports completion after the
    ``END;`` of a trigger body, so semicolons inside ``BEGIN ... END`` do
    not split the statement.  SQLite stores a trigger in ``sqlite_master``
    without a trailing semicolon, so a trigger followed by another
    statement is also recognised at the line boundary where appending a
    semicolon would complete it.
    """
    statements = []
    buffer = ""
    for char in sql:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            _emit(statements, buffer)
            buffer = ""
        elif char == "\n" and buffer.lstrip().upper().startswith("CREATE TRIGGER"):
            candidate = buffer.strip()
            if sqlite3.complete_statement(candidate + ";"):
                _emit(statements, candidate)
                buffer = ""
    if buffer.strip():
        if sqlite3.complete_statement(buffer):
            _emit(statements, buffer)
        elif sqlite3.complete_statement(buffer + ";"):
            _emit(statements, buffer.rstrip() + ";")
        elif _strip_comments(buffer).strip():
            raise DefinitionError("incomplete SQL statement: %r" % _short(buffer))
    return statements


def _emit(statements, statement):
    statement = statement.strip()
    if statement and _strip_comments(statement).strip():
        statements.append(statement)


def _short(text, limit=100):
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit] + "..."


def _strip_comments(sql):
    """Remove ``--`` and ``/* */`` comments, preserving quoted text."""
    out = []
    i = 0
    length = len(sql)
    quote = None
    while i < length:
        char = sql[i]
        if quote:
            out.append(char)
            if quote == "]" and char == "]":
                quote = None
            elif quote != "]" and char == quote:
                if i + 1 < length and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char == "'" or char == '"' or char == "`":
            quote = char
            out.append(char)
            i += 1
            continue
        if char == "[":
            quote = "]"
            out.append(char)
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = length if end == -1 else end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = length if end == -1 else end + 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


_SIMPLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _identifier_value(content):
    content = content.replace('""', '"')
    if _SIMPLE_IDENTIFIER.match(content):
        return content.lower()
    return '"' + content.lower().replace('"', '""') + '"'


def _literal_or_identifier(char, sql, index):
    """Return ``(token, next_index)`` for one quoted token."""
    if char == "'":
        end = index + 1
        while end < len(sql):
            if sql[end] == "'":
                if end + 1 < len(sql) and sql[end + 1] == "'":
                    end += 2
                    continue
                return sql[index:end + 1], end + 1
            end += 1
        return sql[index:], len(sql)
    if char in ('"', "`"):
        end = index + 1
        while end < len(sql):
            if sql[end] == char:
                if end + 1 < len(sql) and sql[end + 1] == char:
                    end += 2
                    continue
                content = sql[index + 1:end]
                return _identifier_value(content), end + 1
            end += 1
        return sql[index:], len(sql)
    end = sql.find("]", index + 1)
    if end == -1:
        return sql[index:], len(sql)
    return _identifier_value(sql[index + 1:end]), end + 1


def semantic_key(statement):
    """A key that treats cosmetic SQL spelling as identical.

    Single-quoted string literals keep their exact content.  Identifier
    quoting (``[...]``, backticks, double quotes) and case are erased, as
    SQLite resolves all three spellings to the same identifier.
    """
    sql = _strip_comments(statement).strip().rstrip(";").strip()
    if not sql:
        return ""
    tokens = []
    i = 0
    while i < len(sql):
        char = sql[i]
        if char in ("'", '"', "`", "["):
            token, i = _literal_or_identifier(char, sql, i)
            tokens.append(("literal", token) if char == "'" else ("word", token))
        elif char.isspace():
            tokens.append(("space", " "))
            i += 1
        else:
            tokens.append(("char", char))
            i += 1
    # Fold case only outside string literals, collapse whitespace and
    # whitespace adjacent to punctuation/operators.
    text = []
    for kind, value in tokens:
        if kind == "literal":
            text.append(value)
        elif kind == "space":
            text.append(" ")
        else:
            text.append(value.lower())
    result = "".join(text)
    result = re.sub(r"\s+", " ", result)
    result = re.sub(r"\s*([(),;.=<>!|+*/-])\s*", r"\1", result)
    result = result.replace(";", "")
    return result.strip()


def spelling_key(statement):
    """Whitespace/comments removed, but quote style and case preserved."""
    sql = _strip_comments(statement).strip()
    sql = re.sub(r"\s+", " ", sql).strip()
    return sql.rstrip(";").strip()


# ---------------------------------------------------------------------------
# Reading definitions from the files sqlite-diffable produces


def _join_definitions(pieces):
    """Join separately-stored definitions without merging statements.

    ``sqlite_master`` keeps no trailing semicolon, so a stored trigger or
    table followed directly by the next definition would otherwise be
    read as one malformed statement.
    """
    cleaned = []
    for piece in pieces:
        statement = piece.strip().rstrip(";").rstrip()
        if statement:
            cleaned.append(statement)
    return "\n".join(piece + ";" for piece in cleaned)


def _definition_from_json(value):
    items = value if isinstance(value, list) else [value]
    pieces = []
    for item in items:
        if isinstance(item, str):
            pieces.append(item)
            continue
        if isinstance(item, dict):
            for key in ("schema", "sql", "definition"):
                if isinstance(item.get(key), str):
                    pieces.append(item[key])
                    break
            else:
                raise DefinitionError(
                    "JSON definition needs a 'schema' or 'sql' key"
                )
            continue
        raise DefinitionError("unsupported JSON definition entry")
    return _join_definitions(pieces)


def load_definition(path):
    """Load a definition from a directory, SQL file or metadata JSON file.

    A directory may be a ``sqlite-diffable dump`` output directory; every
    ``*.metadata.json`` is read in sorted filename order.  If there are
    none, ``*.sql`` files are used instead.
    """
    path = pathlib.Path(path)
    if path.is_dir():
        metadata = sorted(path.glob("*.metadata.json"))
        if metadata:
            return _join_definitions(
                _definition_from_json(
                    json.loads(item.read_text(encoding="utf-8"))
                )
                for item in metadata
            )
        scripts = sorted(path.glob("*.sql"))
        if scripts:
            return _join_definitions(
                item.read_text(encoding="utf-8") for item in scripts
            )
        raise DefinitionError("no *.metadata.json or *.sql files in %s" % path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json" or text.lstrip().startswith(("{", "[")):
        return _definition_from_json(json.loads(text))
    return text


def load_operations(path):
    """Load the allowed operations from JSON or a one-statement-per-line file."""
    path = pathlib.Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json" or text.lstrip().startswith(("[", "{")):
        value = json.loads(text)
        if isinstance(value, dict) and "operations" in value:
            value = value["operations"]
        return value
    operations = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("--"):
            operations.append(line)
    return operations


# ---------------------------------------------------------------------------
# Operations


def normalise_operations(raw):
    """Validate the operations the business allows.

    Accepted shapes:

    * ``"INSERT INTO t VALUES (1, 'a')"``
    * ``{"sql": "...", "params": [...]}``
    * ``{"steps": [ ... ]}`` - a group that is replayed in order and
      stops at the first error
    * ``{"operations": [ ... ]}`` - unwrapped from a JSON file
    """
    if raw is None:
        return []
    if isinstance(raw, dict) and "operations" in raw:
        raw = raw["operations"]
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise OperationError("operations must be a list")
    operations = []
    for index, entry in enumerate(raw):
        if isinstance(entry, str):
            steps = _steps_from_sql(entry, index)
        elif isinstance(entry, dict) and "steps" in entry:
            if not isinstance(entry["steps"], (list, tuple)) or not entry["steps"]:
                raise OperationError("operation %d has no steps" % index)
            steps = []
            for step in entry["steps"]:
                steps.extend(_normalise_step(step, index))
        elif isinstance(entry, dict):
            steps = _normalise_step(entry, index)
        else:
            raise OperationError("operation %d must be a string or object" % index)
        operations.append({"index": index, "steps": steps})
    return operations


def _steps_from_sql(sql, index):
    statements = split_statements(sql)
    if not statements:
        raise OperationError("operation %d is empty" % index)
    return [{"sql": statement, "params": []} for statement in statements]


def _normalise_step(step, index):
    if isinstance(step, str):
        return _steps_from_sql(step, index)
    if isinstance(step, dict) and isinstance(step.get("sql"), str):
        params = step.get("params", [])
        if not isinstance(params, (list, tuple)):
            raise OperationError("operation %d params must be a list" % index)
        statements = split_statements(step["sql"])
        if len(statements) != 1:
            raise OperationError(
                "operation %d step must contain exactly one statement" % index
            )
        return [{"sql": statements[0], "params": list(params)}]
    raise OperationError("operation %d step must be SQL or {'sql': ...}" % index)


_READ_ONLY = re.compile(r"^\s*(SELECT|PRAGMA|EXPLAIN|VALUES)\b", re.IGNORECASE)


def _read_only(sql):
    return bool(_READ_ONLY.match(_strip_comments(sql)))


# ---------------------------------------------------------------------------
# Preparing a definition


_UNSUPPORTED_PATTERNS = (
    (r"\bCREATE\s+(?:TEMP|TEMPORARY)\b", "TEMP OBJECT",
     "temporary objects live outside the main database and are not "
     "carried into the throwaway replay"),
)

# Expressions whose value depends on the wall clock or an unseeded random
# source are flagged because replaying the two versions at different times
# would manufacture a difference that is not in the definitions.
_UNSTABLE = re.compile(
    r"\bCURRENT_(?:TIMESTAMP|DATE|TIME)\b"
    r"|\b(?:RANDOM|RANDOMBLOB)\s*\("
    r"|\b(?:DATE|TIME|DATETIME|JULIANDAY|STRFTIME|UNIXEPOCH)\s*\([^)]*"
    r"['\"]now['\"]",
    re.IGNORECASE,
)


def _audit_statement(statement):
    stripped = _strip_comments(statement).strip()
    if not re.match(r"(?is)(CREATE|PRAGMA)\b", stripped):
        return "NON-SCHEMA STATEMENT", (
            "only object definitions are compared, not seed data or scripts"
        )
    for pattern, reason, explanation in _UNSUPPORTED_PATTERNS:
        if re.search(pattern, statement, re.IGNORECASE):
            return reason, explanation
    match = _UNSTABLE.search(statement)
    if match:
        return "UNSTABLE EXPRESSION", (
            "an expression depending on the clock or randomness (%s) would "
            "differ between the two replays regardless of the schema"
            % " ".join(match.group(0).split())
        )
    return None


def _object_name(statement):
    """A short name for the object a statement declares.

    For non-schema statements the leading keyword is used instead, so a
    report never shows a raw SQL fragment as an object name.
    """
    match = re.search(
        r"CREATE\s+(?:UNIQUE\s+)?(?:VIRTUAL\s+)?(?:TEMP(?:ORARY)?\s+)?"
        r"(?:TABLE|INDEX|TRIGGER|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(\"[^\"]*\"|`[^`]*`|\[[^\]]*\]|[^\s(]+)",
        statement,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip('"`[]')
    keyword = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", statement)
    if keyword:
        return keyword.group(1).upper()
    return _short(statement, 40)


class Prepared:
    """A definition split into statements that could and could not be replayed.

    Statements are executed in dependency order: a statement that refers
    to a table created later in the file is deferred and retried, which
    is how metadata files can be supplied in any filename order.
    """

    def __init__(self, definition, side):
        self.side = side
        self.definition = definition
        self.statements = split_statements(definition)
        if not self.statements:
            raise DefinitionError("definition is empty")
        self.supported = []
        self.skipped = []
        self.conn = sqlite3.connect(":memory:")
        self.conn.isolation_level = None
        pending = []
        for statement in self.statements:
            audit = _audit_statement(statement)
            if audit:
                self._skip(statement, audit[0], audit[1])
            else:
                pending.append(statement)
        while pending:
            deferred = []
            progress = False
            for statement in pending:
                try:
                    self.conn.execute(statement)
                except sqlite3.Error as error:
                    deferred.append((statement, error))
                    continue
                self.supported.append(statement)
                progress = True
            if not progress:
                for statement, error in deferred:
                    self._skip(
                        statement,
                        "CANNOT MATERIALISE",
                        "%s: %s" % (
                            getattr(error, "sqlite_errorname", type(error).__name__),
                            _short(str(error)),
                        ),
                    )
                break
            pending = [statement for statement, _ in deferred]
        self.blob = self.conn.serialize()
        self.statement_keys = {
            semantic_key(statement): statement for statement in self.statements
        }
        self.supported_keys = {
            semantic_key(statement): statement for statement in self.supported
        }
        self.skipped_keys = {item["key"] for item in self.skipped}
        self.skipped_by_key = {item["key"]: item for item in self.skipped}

    def _skip(self, statement, reason, explanation):
        self.skipped.append({
            "side": self.side,
            "object": _object_name(statement),
            "reason": reason,
            "explanation": explanation,
            "statement": statement,
            "key": semantic_key(statement),
        })

    def connection(self):
        conn = sqlite3.connect(":memory:")
        conn.isolation_level = None
        conn.deserialize(self.blob)
        return conn

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Observing behaviour


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _virtual_tables(conn):
    names = set()
    for (sql,) in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL"
    ):
        match = re.search(
            r"CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"(\"[^\"]*\"|`[^`]*`|\[[^\]]*\]|[^\s(]+)",
            sql,
            re.IGNORECASE,
        )
        if match:
            name = match.group(1)
            if name[:1] in ('"', "`", "["):
                name = name[1:-1]
            names.add(name)
    return names


def _tables(conn):
    virtual = _virtual_tables(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "UNION SELECT name FROM sqlite_temp_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    names = []
    for (name,) in rows:
        # Shadow tables (``docs_data`` for ``CREATE VIRTUAL TABLE docs``)
        # hold extension-internal bytes.  Differences there reflect the
        # storage format, not the rows the business reads and writes.
        if any(name.startswith(table + "_") for table in virtual):
            continue
        names.append(name)
    return names


def _normalise_cell(value):
    if isinstance(value, float):
        return round(value, 12)
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    return value


def _sort_rows(rows):
    def key(row):
        return tuple((type(value).__name__, repr(value)) for value in row)
    return sorted(rows, key=key)


def _snapshot(conn):
    """Comparable observable state: the rows in every table.

    The schema text itself is deliberately absent: the two definitions
    are known to differ there.  Connection-level counters are also
    absent, since a trigger writing to a private table would move them
    without changing anything a read or write of a shared table returns.
    """
    state = {"tables": {}}
    for table in _tables(conn):
        try:
            rows = conn.execute("SELECT * FROM %s" % _quote(table)).fetchall()
        except sqlite3.Error:
            rows = None
        state["tables"][table] = {
            "rows": None if rows is None else _sort_rows(
                tuple(_normalise_cell(value) for value in row) for row in rows
            ),
        }
    return state


_ABSENT = {"absent": True}


def _state_effects(state, baseline, shared_tables):
    """Describe what the operations made visible on the shared object set.

    Tables present in only one version's baseline belong to that
    version's own schema, so their content is not attributed to the
    operations.  Tables created by an operation appear in neither
    baseline and are compared normally.
    """
    tables = {}
    names = set(state["tables"]) | set(baseline["tables"])
    for name in sorted(names):
        if name in shared_tables or name not in baseline["tables"]:
            tables[name] = state["tables"].get(name, _ABSENT)
    return {"tables": tables}


_ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)


def _run_step(conn, step):
    try:
        cursor = conn.execute(step["sql"], step["params"])
        if cursor.description:
            columns = [item[0] for item in cursor.description]
            rows = [
                tuple(_normalise_cell(value) for value in row)
                for row in cursor.fetchall()
            ]
            # Without ORDER BY, SQLite does not promise row order, so the
            # same rows in a different order are the same result.
            ordered = bool(_ORDER_BY.search(_strip_comments(step["sql"])))
            if not ordered:
                rows = _sort_rows(rows)
        else:
            columns, rows = None, None
        return {"status": "ok", "columns": columns, "rows": rows,
                "rowcount": cursor.rowcount if cursor.rowcount != -1 else None}
    except sqlite3.Error as error:
        return {
            "status": "error",
            "error": getattr(error, "sqlite_errorname", type(error).__name__),
            "message": re.sub(r"\s+", " ", str(error)).strip(),
        }


def _run_operation(conn, operation, baseline, shared_tables):
    steps = []
    for step in operation["steps"]:
        steps.append(_run_step(conn, step))
        if steps[-1]["status"] == "error":
            break
    effects = _state_effects(_snapshot(conn), baseline, shared_tables)
    return {"steps": steps, "state": effects}


def _comparable(result, read_only):
    """The part of an outcome the business can observe.

    A read is judged by its columns, rows and any error.  A write is
    judged by its effects on the database, the constraint it violates,
    and any rows it returns (``INSERT ... RETURNING``); a change count is
    not part of the promise.  A step's returned rows are compared in both
    cases.
    """
    steps = []
    for step in result["steps"]:
        entry = {"status": step["status"], "error": step.get("error")}
        if read_only or step.get("rows") is not None:
            entry["columns"] = step.get("columns")
            entry["rows"] = step.get("rows")
        steps.append(entry)
    if read_only:
        return steps
    return {"steps": steps, "state": result["state"]}


def _same_outcome(conn_a, conn_b, operation, baselines, shared_tables):
    read_only = all(_read_only(step["sql"]) for step in operation["steps"])
    result_a = _run_operation(conn_a, operation, baselines["a"], shared_tables)
    result_b = _run_operation(conn_b, operation, baselines["b"], shared_tables)
    same = _comparable(result_a, read_only) == _comparable(result_b, read_only)
    return same, result_a, result_b


# ---------------------------------------------------------------------------
# Finding the shortest distinguishing sequence


class _SearchBudgetExceeded(Exception):
    pass


def _sequence_overview(operations, sequence):
    steps = []
    for index in sequence:
        for step in operations[index]["steps"]:
            steps.append({"sql": step["sql"], "params": step["params"]})
    return steps


def find_distinguishing_sequence(template_a, template_b, operations, max_length=3,
                                  max_states=200000):
    """Breadth-first search for the shortest sequence with different outcomes.

    Every candidate starts from a fresh clone of each definition, so a
    candidate's outcome cannot depend on earlier candidates.  A prefix is
    not expanded twice when both versions reach the same observable
    state, because the rest of the outcome depends only on that state.
    """
    if max_length < 1:
        raise ValueError("max_length must be at least 1")

    start_a = template_a.connection()
    start_b = template_b.connection()
    try:
        baselines = {"a": _snapshot(start_a), "b": _snapshot(start_b)}
        shared_tables = set(baselines["a"]["tables"]) & set(baselines["b"]["tables"])
        start = (start_a.serialize(), start_b.serialize())
    finally:
        start_a.close()
        start_b.close()

    frontier = [(start, [])]
    seen = set()
    expanded = 0
    for depth in range(1, max_length + 1):
        next_frontier = []
        for (blob_a, blob_b), prefix in frontier:
            for index, operation in enumerate(operations):
                conn_a = _connection_from(blob_a)
                conn_b = _connection_from(blob_b)
                try:
                    same, result_a, result_b = _same_outcome(
                        conn_a, conn_b, operation, baselines, shared_tables
                    )
                    if not same:
                        sequence = prefix + [index]
                        return {
                            "sequence": sequence,
                            "steps": _sequence_overview(operations, sequence),
                            "failed_at": index,
                            "depth": depth,
                            "a": result_a,
                            "b": result_b,
                        }
                    new_state = (conn_a.serialize(), conn_b.serialize())
                finally:
                    conn_a.close()
                    conn_b.close()
                expanded += 1
                if expanded > max_states:
                    raise _SearchBudgetExceeded(
                        "state budget exhausted before the search finished"
                    )
                key = (hashlib.sha1(new_state[0]).hexdigest(),
                       hashlib.sha1(new_state[1]).hexdigest())
                if key in seen:
                    continue
                seen.add(key)
                next_frontier.append((new_state, prefix + [index]))
        frontier = next_frontier
        if not frontier:
            break
    return None


def _connection_from(blob):
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.deserialize(blob)
    return conn


# ---------------------------------------------------------------------------
# Statement diff


def _spellings_identical(prepared_a, prepared_b):
    """True when both definitions contain the same statements up to spelling."""
    return set(prepared_a.statement_keys) == set(prepared_b.statement_keys)


def _plain_object_name(statement):
    name = _object_name(statement)
    if name[:1] in ('"', "`", "["):
        name = name[1:-1]
    return name.lower()


def _trigger_target(statement):
    """The table a ``CREATE TRIGGER`` fires on, if it names one."""
    match = re.search(
        r"(?:BEFORE|AFTER|INSTEAD\s+OF)\s+(?:INSERT|UPDATE|DELETE)\s+"
        r"(?:OF\s+[^\n]+?\s+)?ON\s+"
        r"(\"[^\"]*\"|`[^`]*`|\[[^\]]*\]|[^\s(]+)",
        statement,
        re.IGNORECASE,
    )
    if not match:
        return None
    name = match.group(1)
    if name[:1] in ('"', "`", "["):
        name = name[1:-1]
    return name.lower()


def _affected_objects(statement):
    """Objects whose behaviour a definition statement can change.

    A trigger's own name is not a table an operation can touch, so the
    table it fires on is what matters; a foreign key's target is likewise
    affected by the referencing definition.
    """
    names = {_plain_object_name(statement)}
    if re.match(r"(?is)CREATE\s+(?:TEMP(?:ORARY)?\s+)?TRIGGER\b", statement):
        names.discard(_plain_object_name(statement))
        target = _trigger_target(statement)
        if target:
            names.add(target)
    for match in re.finditer(
        r"REFERENCES\s+(\"[^\"]*\"|`[^`]*`|\[[^\]]*\]|[^\s(]+)",
        statement,
        re.IGNORECASE,
    ):
        name = match.group(1)
        if name[:1] in ('"', "`", "["):
            name = name[1:-1]
        names.add(name.lower())
    return {name for name in names if name}


def _tainted_objects(blocking):
    """Objects whose materialisation differs between the versions by omission.

    Only asymmetric gaps taint: a statement skipped in both versions is
    absent from both replays, so it cannot make them look different.  A
    statement replayed in one version while its same-named counterpart
    was skipped in the other can, so operations touching that object are
    flagged.
    """
    names = set()
    for entry in blocking:
        names |= _affected_objects(entry["statement"])
    return names


def _sequence_taint(sequence, prepared_a, prepared_b):
    """Names from the sequence's SQL that have an asymmetric replay gap."""
    skipped = {
        "a": set(),
        "b": set(),
    }
    for item in prepared_a.skipped:
        skipped["a"] |= _affected_objects(item["statement"])
    for item in prepared_b.skipped:
        skipped["b"] |= _affected_objects(item["statement"])
    supported = {
        "a": set(),
        "b": set(),
    }
    for statement in prepared_a.supported:
        supported["a"] |= _affected_objects(statement)
    for statement in prepared_b.supported:
        supported["b"] |= _affected_objects(statement)
    asymmetric = (skipped["a"] & supported["b"]) | (skipped["b"] & supported["a"])
    if not asymmetric:
        return set()
    touched = set()
    for step in sequence["steps"]:
        sql = _strip_comments(step["sql"]).lower()
        for name in asymmetric:
            pattern = r"(?<![A-Za-z0-9_$])%s(?![A-Za-z0-9_$])"
            if re.search(pattern % re.escape(name), sql):
                touched.add(name)
    return touched


def statement_diff(prepared_a, prepared_b):
    """Partition statements into cosmetic-only and structurally different."""
    keys_a = {key: statement for key, statement in prepared_a.statement_keys.items()}
    keys_b = {key: statement for key, statement in prepared_b.statement_keys.items()}
    only_a = [keys_a[key] for key in keys_a.keys() - keys_b.keys()]
    only_b = [keys_b[key] for key in keys_b.keys() - keys_a.keys()]
    cosmetic = []
    for key in keys_a.keys() & keys_b.keys():
        if spelling_key(keys_a[key]) != spelling_key(keys_b[key]):
            cosmetic.append({
                "a": keys_a[key],
                "b": keys_b[key],
            })
    return {
        "only_a": sorted(only_a),
        "only_b": sorted(only_b),
        "cosmetic": cosmetic,
    }


def _skipped_pairs(prepared_a, prepared_b):
    """Yield ``(side, skipped item, other prepared)`` for each skipped item."""
    for item in prepared_a.skipped:
        yield "a", item, prepared_b
    for item in prepared_b.skipped:
        yield "b", item, prepared_a


def _unsupported_overlap(prepared_a, prepared_b):
    """Classify skipped statements as shared, or as blocking the comparison.

    A statement skipped in both versions is identical in both, so it
    cannot distinguish them, but it is still outside the replay and is
    reported so that a verdict is never read as covering it.  A statement
    skipped in one version while the other version has a materialised
    counterpart is a genuine blocking gap.
    """
    skipped_a = {item["key"]: item for item in prepared_a.skipped}
    skipped_b = {item["key"]: item for item in prepared_b.skipped}
    shared = []
    blocking = []
    for side, item, other in _skipped_pairs(prepared_a, prepared_b):
        if item["key"] in other.skipped_keys:
            shared.append({"a": item, "b": other.skipped_by_key[item["key"]]})
            continue
        if item["key"] in other.supported_keys:
            detail = "only version %s could not be materialised" % side.upper()
        else:
            detail = (
                "version %s statement has no materialised counterpart in %s"
                % (side.upper(), "B" if side == "a" else "A")
            )
        blocking.append(
            {
                "side": side,
                "statement": item["statement"],
                "reason": item["reason"],
                "detail": detail,
            }
        )
    return shared, blocking


# ---------------------------------------------------------------------------
# Top level


def _outcome(result):
    steps = []
    for step in result["steps"]:
        if step["status"] == "ok":
            if step.get("rows") is not None:
                steps.append({"rows": step["rows"]})
            else:
                steps.append({"ok": True})
        else:
            steps.append({"error": step["error"], "message": step.get("message")})
    outcome = {"steps": steps}
    state = result.get("state")
    if state is not None:
        changes = {
            name: table.get("rows") if table != _ABSENT else None
            for name, table in state["tables"].items()
        }
        if changes:
            outcome["tables"] = changes
    return outcome


def _probe(template_a, template_b, operations):
    conn_a = template_a.connection()
    conn_b = template_b.connection()
    try:
        baselines = {"a": _snapshot(conn_a), "b": _snapshot(conn_b)}
        shared_tables = set(baselines["a"]["tables"]) & set(baselines["b"]["tables"])
    finally:
        conn_a.close()
        conn_b.close()
    probes = []
    for operation in operations:
        conn_a = template_a.connection()
        conn_b = template_b.connection()
        try:
            same, result_a, result_b = _same_outcome(
                conn_a, conn_b, operation, baselines, shared_tables
            )
        finally:
            conn_a.close()
            conn_b.close()
        probes.append({
            "index": operation["index"],
            "sql": [step["sql"] for step in operation["steps"]],
            "params": [step["params"] for step in operation["steps"]],
            "same": same,
            "a": _outcome(result_a),
            "b": _outcome(result_b),
        })
    return probes


def check(definition_a, definition_b, operations, max_length=3):
    """Compare two definitions under an allowed operation set.

    The returned ``verdict`` is one of:

    ``different``
        ``distinguishing_sequence`` holds a shortest distinguishing
        sequence (up to ``max_length``).  This holds even when some
        statements could not be replayed, because the difference was
        demonstrated rather than inferred.
    ``equivalent``
        Both definitions were materialised in full and no sequence up to
        ``max_length`` distinguished them.
    ``undecided``
        A statement could not be replayed (see ``blocking`` and
        ``unsupported``), or the search budget was exhausted, and no
        conclusive difference was found.  No equivalence is claimed.

    ``unsupported`` lists every statement left out of the replay, on
    either side, whether or not the versions share it.  A difference
    found on an object with an asymmetric replay gap is returned with
    ``conclusive: false`` and does not change the verdict.
    """
    prepared_a = Prepared(definition_a, "a")
    prepared_b = Prepared(definition_b, "b")
    try:
        operations = normalise_operations(operations)
        diff = statement_diff(prepared_a, prepared_b)
        shared_unsupported, blocking = _unsupported_overlap(prepared_a, prepared_b)
        unsupported = prepared_a.skipped + prepared_b.skipped
        identical_spellings = _spellings_identical(prepared_a, prepared_b)

        sequence = None
        search_error = None
        if not identical_spellings:
            # Identical statements cannot be told apart by any operation.
            try:
                sequence = find_distinguishing_sequence(
                    prepared_a, prepared_b, operations, max_length
                )
            except _SearchBudgetExceeded as error:
                search_error = str(error)

        taint = (
            _sequence_taint(sequence, prepared_a, prepared_b)
            if sequence is not None
            else set()
        )
        sequence_trusted = sequence is not None and not taint
        if sequence_trusted:
            verdict = "different"
        elif blocking or unsupported or search_error:
            verdict = "undecided"
        else:
            verdict = "equivalent"

        report = {
            "verdict": verdict,
            "equivalent": {
                "different": False,
                "equivalent": True,
            }.get(verdict),
            "search_budget": {
                "max_length": max_length,
                "operations": len(operations),
                "completed": search_error is None,
                "skipped_identical": identical_spellings,
            },
            "statement_diff": diff,
            "unsupported": unsupported,
            "unsupported_shared": shared_unsupported,
            "blocking": blocking,
            "distinguishing_sequence": None,
            "operations": _probe(prepared_a, prepared_b, operations),
        }
        if blocking:
            report["asymmetric_gaps"] = sorted(_tainted_objects(blocking))
        if search_error:
            report["search_error"] = search_error
        if sequence is not None:
            report["distinguishing_sequence"] = {
                "length": len(sequence["sequence"]),
                "steps": sequence["steps"],
                "failed_at": sequence["failed_at"],
                "a": _outcome(sequence["a"]),
                "b": _outcome(sequence["b"]),
                "conclusive": sequence_trusted,
            }
            if not sequence_trusted:
                report["distinguishing_sequence"]["caveat"] = (
                    "the sequence touches an object whose definition was "
                    "left out of the replay, so its difference is not "
                    "attributable to the definitions alone"
                )
        return report
    finally:
        prepared_a.close()
        prepared_b.close()
