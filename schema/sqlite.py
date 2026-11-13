"""Mirror a ``schema`` definition as a SQLite staging table.

``create_table_ddl`` compiles a dict schema plus explicit cross-field
:class:`~schema.Rule` objects into a ``CREATE TABLE`` statement with
``CHECK`` constraints, so writers that insert directly into the database
follow the same convention as events validated in Python.

Only a subset of ``schema`` constructs maps to SQL. Custom Python-side
transformations cannot run inside SQLite and are reported through
:class:`UnmigratableError`:

* ``Use(...)`` -- arbitrary Python callables that transform values.
* lambda / function predicates -- opaque Python code.
* ``Regex`` -- Python ``re`` has no built-in SQLite equivalent.
* ``Hook`` / ``Forbidden`` value handlers -- Python callbacks (a
  ``Forbidden`` key is enforced implicitly: no column is created for it).
* callable ``Optional`` defaults -- Python code, not a SQL ``DEFAULT``.
* nested dicts / lists / sets -- need JSON storage, not a scalar column.

Tri-state optional keys (absent vs explicit null vs value) are preserved
with a generated ``<key>__present`` flag column; use ``event_to_row`` and
``row_to_event`` so the three business states never collapse into one
NULL.
"""

from . import (
    And,
    Const,
    Forbidden,
    Hook,
    Literal,
    Optional,
    Or,
    Regex,
    Rule,
    Schema,
    Use,
    _sql_literal,
)

__all__ = ["UnmigratableError", "create_table_ddl", "event_to_row", "row_to_event"]


class UnmigratableError(Exception):
    """Raised when parts of a schema cannot be expressed as SQLite constraints.

    The ``reasons`` attribute lists every unmigratable piece.
    """

    def __init__(self, reasons):
        self.reasons = list(reasons)
        message = "Schema cannot be fully migrated to SQLite:\n" + "\n".join(
            "- %s" % reason for reason in self.reasons
        )
        super().__init__(message)


_NO_DEFAULT = object()

_SIMPLE_SQL_TYPES = {str: "TEXT", int: "INTEGER", float: "REAL", bool: "INTEGER"}


def _quote(identifier):
    return '"%s"' % str(identifier).replace('"', '""')


class _Column:
    def __init__(self, key, optional, sql_type, py_type, admits_none, checks, default):
        self.key = key
        self.name = str(key)
        self.optional = optional
        self.sql_type = sql_type
        self.py_type = py_type
        self.admits_none = admits_none
        self.checks = checks
        self.default = default

    @property
    def tri_state(self):
        """Optional keys whose value may be None need a presence flag column."""
        return self.optional and self.admits_none

    @property
    def presence_name(self):
        return self.name + "__present"


class _Plan:
    def __init__(self, columns, rules):
        self.columns = columns
        self.rules = rules
        self.by_key = {c.key: c for c in columns}

    def presence_sql(self, key):
        column = self.by_key[key]
        if column.tri_state:
            return "%s = 1" % _quote(column.presence_name)
        if column.optional:
            return "%s IS NOT NULL" % _quote(column.name)
        return "1"


def _unify_types(parts, path, reasons):
    pairs = {(sql_type, py_type) for sql_type, py_type, _, _ in parts if sql_type}
    distinct = {sql_type for sql_type, _ in pairs}
    if not distinct:
        return None, None
    if len(distinct) == 1:
        sql_type = distinct.pop()
        py_types = {py_type for st, py_type in pairs if st == sql_type}
        return sql_type, py_types.pop() if len(py_types) == 1 else None
    if distinct == {"INTEGER", "REAL"}:
        return "REAL", None
    reasons.append(
        "%s: mixes SQL types %s in one column" % (path, ", ".join(sorted(distinct)))
    )
    return None, None


def _walk(node, column, path, reasons):
    """Analyze a value schema.

    Returns (sql_type, py_type, admits_none, checks); unmigratable pieces are
    appended to ``reasons`` instead of raising, so every problem is reported
    at once.
    """
    if isinstance(node, (Const, Literal)):
        return _walk(node.schema, column, path, reasons)
    if isinstance(node, Use):
        reasons.append(
            "%s: Use(%s) runs a Python-side transformation that SQLite CHECK "
            "constraints cannot execute"
            % (path, getattr(node._callable, "__name__", repr(node._callable)))
        )
        return None, None, False, []
    if isinstance(node, Regex):
        reasons.append(
            "%s: Regex(%r) uses Python re; SQLite has no built-in REGEXP operator"
            % (path, node.pattern_str)
        )
        return None, None, False, []
    if isinstance(node, (Hook, Forbidden)):
        reasons.append(
            "%s: %s relies on a Python handler callback" % (path, type(node).__name__)
        )
        return None, None, False, []
    if isinstance(node, Or):
        parts = [_walk(arg, column, path, reasons) for arg in node.args]
        sql_type, py_type = _unify_types(parts, path, reasons)
        admits_none = any(part[2] for part in parts)
        checks = []
        # An Or only constrains values if every branch constrains them
        # (a bare type branch accepts anything of that type).
        if all(part[3] or part[2] for part in parts):
            or_checks = [check for part in parts for check in part[3]]
            if or_checks:
                checks = ["(%s)" % " OR ".join(or_checks)]
        return sql_type, py_type, admits_none, checks
    if isinstance(node, And):
        parts = [_walk(arg, column, path, reasons) for arg in node.args]
        sql_type, py_type = _unify_types(parts, path, reasons)
        admits_none = any(part[2] for part in parts)
        checks = [check for part in parts for check in part[3]]
        return sql_type, py_type, admits_none, checks
    if isinstance(node, Schema):
        return _walk(node.schema, column, path, reasons)
    if node is None:
        return None, None, True, []
    if isinstance(node, type):
        if node in _SIMPLE_SQL_TYPES:
            checks = ["%s IN (0, 1)" % column] if node is bool else []
            return _SIMPLE_SQL_TYPES[node], node, False, checks
        reasons.append("%s: type %r has no SQLite column equivalent" % (path, node))
        return None, None, False, []
    if callable(node):
        reasons.append(
            "%s: callable predicate %s is opaque Python code and cannot become "
            "a CHECK constraint"
            % (path, getattr(node, "__name__", repr(node)))
        )
        return None, None, False, []
    if isinstance(node, bool):
        return "INTEGER", bool, False, ["%s = %s" % (column, _sql_literal(node))]
    if isinstance(node, (str, int, float)):
        py_type = type(node)
        return (
            _SIMPLE_SQL_TYPES[py_type],
            py_type,
            False,
            ["%s = %s" % (column, _sql_literal(node))],
        )
    if isinstance(node, (dict, list, tuple, set, frozenset)):
        reasons.append(
            "%s: nested %s structures need JSON storage, not a scalar column"
            % (path, type(node).__name__)
        )
        return None, None, False, []
    reasons.append("%s: %r cannot be migrated" % (path, node))
    return None, None, False, []


def _extract(schema, rules, reasons):
    """Split the supported schema forms into (dict_schema, rules)."""
    rules = list(rules)
    while isinstance(schema, Schema):
        schema = schema.schema
    if isinstance(schema, And):
        dicts = [arg for arg in schema.args if isinstance(arg, dict)]
        rules += [arg for arg in schema.args if isinstance(arg, Rule)]
        for arg in schema.args:
            if not isinstance(arg, (dict, Rule)):
                reasons.append("%r: And(...) argument is not migratable" % (arg,))
        if len(dicts) != 1:
            reasons.append("expected exactly one dict inside And(...)")
            return {}, rules
        schema = dicts[0]
    if not isinstance(schema, dict):
        reasons.append("top-level schema %r is not a dict" % (schema,))
        return {}, rules
    return schema, rules


def _build_plan(schema, rules):
    reasons = []
    dict_schema, rules = _extract(schema, rules, reasons)
    columns = []
    for skey, svalue in dict_schema.items():
        if isinstance(skey, (Hook, Forbidden)):
            # No column is created, so direct writers cannot set the key.
            continue
        optional = isinstance(skey, Optional)
        raw_key = skey.schema if optional else skey
        default = getattr(skey, "default", _NO_DEFAULT) if optional else _NO_DEFAULT
        if isinstance(raw_key, Literal):
            raw_key = raw_key.schema
        if not isinstance(raw_key, str):
            reasons.append("key %r: non-literal keys cannot become columns" % (skey,))
            continue
        if default is not _NO_DEFAULT and callable(default):
            reasons.append(
                "key %r: callable Optional default is Python code and cannot "
                "become a SQL DEFAULT" % (raw_key,)
            )
            default = _NO_DEFAULT
        column = _quote(raw_key)
        sql_type, py_type, admits_none, checks = _walk(
            svalue, column, "key %r" % raw_key, reasons
        )
        if sql_type is None:
            sql_type = "TEXT"
            if admits_none and not checks:
                # The only valid value is NULL.
                checks = ["%s IS NULL" % column]
        columns.append(
            _Column(raw_key, optional, sql_type, py_type, admits_none, checks, default)
        )
    known_keys = {c.key for c in columns}
    for rule in rules:
        for key in rule.keys:
            if key not in known_keys:
                reasons.append(
                    "rule %r references key %r which has no column" % (rule, key)
                )
    if reasons:
        raise UnmigratableError(reasons)
    return _Plan(columns, rules)


def create_table_ddl(table, schema, rules=()):
    """Return a CREATE TABLE statement enforcing the schema and rules.

    :param table: name of the staging table.
    :param schema: a dict, a ``Schema`` wrapping a dict, or
        ``And({...}, rule, ...)`` combining a dict schema with rules.
    :param rules: explicit cross-field rules, if not already inside an ``And``.
    :raises UnmigratableError: listing every construct that has no SQLite
        equivalent.
    """
    plan = _build_plan(schema, rules)
    lines = []
    for column in plan.columns:
        line = "  %s %s" % (_quote(column.name), column.sql_type)
        if not column.optional and not column.admits_none:
            line += " NOT NULL"
        if column.default is not _NO_DEFAULT:
            line += " DEFAULT " + _sql_literal(column.default)
        lines.append(line)
        if column.tri_state:
            lines.append(
                "  %s INTEGER NOT NULL DEFAULT 0" % _quote(column.presence_name)
            )
    checks = []
    for column in plan.columns:
        if column.tri_state:
            checks.append("%s IN (0, 1)" % _quote(column.presence_name))
            checks.append(
                "%s = 1 OR %s IS NULL"
                % (_quote(column.presence_name), _quote(column.name))
            )
        nullable = column.optional or column.admits_none
        for check in column.checks:
            if nullable:
                checks.append("%s IS NULL OR (%s)" % (_quote(column.name), check))
            else:
                checks.append(check)
    for rule in plan.rules:
        checks.append(rule.to_sql_check(_quote, plan.presence_sql))
    lines += ["  CHECK (%s)" % check for check in checks]
    return "CREATE TABLE %s (\n%s\n);" % (_quote(table), ",\n".join(lines))


def _validate(schema, rules, event):
    if hasattr(schema, "validate"):
        validated = schema.validate(event)
    else:
        validated = Schema(schema).validate(event)
    for rule in rules:
        validated = rule.validate(validated)
    return validated


def _adapt(value):
    if isinstance(value, bool):
        return int(value)
    return value


def _restore(value, py_type):
    if value is None:
        return None
    if py_type is bool:
        return bool(value)
    return value


def event_to_row(event, schema, rules=()):
    """Validate an event and convert it to a row dict for the staging table.

    Tri-state optional keys set their ``<key>__present`` flag, so a missing
    key and an explicit ``None`` stay distinguishable in the database.
    """
    plan = _build_plan(schema, rules)
    validated = _validate(schema, rules, event)
    row = {}
    for column in plan.columns:
        present = column.key in validated
        if column.tri_state:
            row[column.presence_name] = 1 if present else 0
            row[column.name] = _adapt(validated[column.key]) if present else None
        elif column.optional:
            row[column.name] = _adapt(validated[column.key]) if present else None
        else:
            row[column.name] = _adapt(validated[column.key])
    return row


def row_to_event(row, schema, rules=()):
    """Convert a staging-table row back to an event dict.

    Keys whose presence flag is 0 are omitted, keys flagged present with a
    NULL value come back as explicit ``None``: the three business states
    never collapse into a single null.
    """
    plan = _build_plan(schema, rules)
    event = {}
    for column in plan.columns:
        if column.tri_state:
            if row[column.presence_name]:
                event[column.key] = _restore(row[column.name], column.py_type)
        elif column.optional:
            value = row[column.name]
            if value is not None:
                event[column.key] = _restore(value, column.py_type)
        else:
            event[column.key] = _restore(row[column.name], column.py_type)
    return event
