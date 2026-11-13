import hashlib
import hmac
import json
import os
import pathlib


ALGORITHM = "HMAC-SHA256"
SURROGATE_PREFIX = "SUR_"
ISOLATED_PREFIX = "ISO_"
DECLARATION_FILENAME = "anonymization-declaration.json"

_SEPARATOR = b"\x1f"


class SpecError(Exception):
    pass


def load_spec(path):
    with open(path) as fp:
        spec = json.load(fp)
    if not isinstance(spec, dict) or not isinstance(spec.get("tables"), dict):
        raise SpecError("spec must be an object containing a 'tables' object")
    return spec


def normalize_key_group(group):
    """Return (namespace name, columns tuple) for one key group in the spec."""
    if isinstance(group, list):
        columns = tuple(group)
        namespace = ".".join(columns)
    elif isinstance(group, dict):
        columns = tuple(group["columns"])
        namespace = group.get("name", ".".join(columns))
    else:
        raise SpecError("each key group must be a list of columns or an object")
    if not columns:
        raise SpecError("key groups must contain at least one column")
    return namespace, columns


def table_rules(table_spec):
    surrogate_groups = [
        normalize_key_group(group) for group in table_spec.get("keys", [])
    ]
    isolated_columns = tuple(table_spec.get("identity", []))
    return surrogate_groups, isolated_columns


class Pseudonymizer:
    """Per-partner HMAC pseudonymizer.

    Surrogates are stable within one partner export (so equi-joins keep
    working) but differ across partners, so directories handed to different
    partners cannot be matched against each other even when source values
    coincide.
    """

    def __init__(self, partner_id, salt):
        self.partner_id = partner_id
        if isinstance(salt, str):
            salt = bytes.fromhex(salt)
        self.salt = salt

    @staticmethod
    def _encode(value):
        # ensure_ascii=True keeps the 0x1f separator unambiguous
        return json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), default=repr
        ).encode("utf-8")

    def _digest(self, *parts):
        message = _SEPARATOR.join(self._encode(part) for part in parts)
        return hmac.new(self.salt, message, hashlib.sha256).hexdigest()

    def surrogate(self, namespace, values, position=None):
        digest = self._digest(
            "surrogate", self.partner_id, namespace, list(values)
        )
        token = SURROGATE_PREFIX + digest[:16]
        if position is not None:
            token = "{}:{}".format(token, position)
        return token

    def isolated(self, table, column, value):
        digest = self._digest(
            "isolated", self.partner_id, table, column, value
        )
        return ISOLATED_PREFIX + digest[:16]


def validate_columns(table, columns, rules):
    surrogate_groups, isolated_columns = rules
    known = set(columns)
    seen = set()
    for namespace, key_columns in surrogate_groups:
        for column in key_columns:
            if column not in known:
                raise SpecError(
                    "table {!r} has no key column {!r}".format(table, column)
                )
            if column in seen:
                raise SpecError(
                    "column {!r} of table {!r} appears in more than one key "
                    "group".format(column, table)
                )
            seen.add(column)
    for column in isolated_columns:
        if column not in known:
            raise SpecError(
                "table {!r} has no identity column {!r}".format(table, column)
            )
        if column in seen:
            raise SpecError(
                "column {!r} of table {!r} cannot be both a key and an "
                "identity column".format(column, table)
            )


def transform_row(row, columns, rules, pseudonymizer, table):
    """Return the anonymized JSON-ready values for one row."""
    output = list(row)
    surrogate_groups, isolated_columns = rules
    column_index = {column: index for index, column in enumerate(columns)}
    for namespace, key_columns in surrogate_groups:
        indexes = [column_index[column] for column in key_columns]
        values = tuple(output[index] for index in indexes)
        if any(value is None for value in values):
            # SQL NULL never equals anything, including another NULL: a
            # partially NULL foreign key must not match any parent row.
            for index in indexes:
                output[index] = None
            continue
        for position, index in enumerate(indexes):
            output[index] = pseudonymizer.surrogate(
                namespace,
                values,
                position if len(key_columns) > 1 else None,
            )
    for column in isolated_columns:
        index = column_index[column]
        if output[index] is not None:
            output[index] = pseudonymizer.isolated(
                table, column, output[index]
            )
    return output


def build_declaration(partner_id, spec):
    surrogate_fields = []
    isolated_fields = []
    for table, table_spec in spec["tables"].items():
        for group in table_spec.get("keys", []):
            namespace, columns = normalize_key_group(group)
            surrogate_fields.append(
                {
                    "table": table,
                    "columns": list(columns),
                    "namespace": namespace,
                    "treatment": "surrogate identity shared within this "
                    "partner export only",
                }
            )
        for column in table_spec.get("identity", []):
            isolated_fields.append(
                {
                    "table": table,
                    "column": column,
                    "treatment": "direct customer identity; pseudonymized per "
                    "partner and per column, never use for matching",
                }
            )
    return {
        "partner_id": partner_id,
        "algorithm": ALGORITHM,
        "surrogate_fields": surrogate_fields,
        "isolated_fields": isolated_fields,
        "rules": [
            "Fields listed in surrogate_fields carry replacement identities. "
            "Equal source values map to equal surrogates inside this export, "
            "so the original equi-joins (e.g. contracts to settlement "
            "statements) keep working on these columns.",
            "Fields listed in isolated_fields held direct customer identity. "
            "Their values are pseudonymized with a partner- and "
            "column-specific key, so equal source values must not be treated "
            "as the same identity across columns or partners.",
            "For composite keys, every component of a tuple containing NULL "
            "is exported as NULL. Such rows never match, reproducing the "
            "source database SQL matching semantics (NULL is not equal to "
            "NULL).",
            "Surrogates are derived with a per-partner secret salt. "
            "Identical source values in directories produced for different "
            "partners yield different surrogates; partner directories must "
            "not be cross-recognised by surrogate values.",
            "Columns not listed here are non-identifying business data and "
            "are exported unchanged.",
        ],
    }


def write_declaration(output_directory, declaration):
    path = pathlib.Path(output_directory) / DECLARATION_FILENAME
    with path.open("w") as fp:
        json.dump(declaration, fp, indent=4, ensure_ascii=False)
    return path


def random_salt():
    return os.urandom(32).hex()
