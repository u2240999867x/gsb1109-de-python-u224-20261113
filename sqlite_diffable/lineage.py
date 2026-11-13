import json
import pathlib


class LineageError(ValueError):
    pass


_RENAME = "rename"
_SPLIT = "split"
_KNOWN_TYPES = {_RENAME, _SPLIT}
_METADATA_SUFFIX = ".metadata.json"


def _read_json(path):
    with open(str(path), "r", encoding="utf-8") as fp:
        return json.load(fp)


def _public_change(change):
    if change is None:
        return None
    return {
        key: value for key, value in change.items() if not key.startswith("_")
    }


class Version:
    """One dumped table archive: a .metadata.json plus its positional .ndjson."""

    def __init__(self, path, metadata_path, ndjson_path, table, columns, schema):
        self.path = str(path)
        self.metadata_path = str(metadata_path)
        self.ndjson_path = str(ndjson_path)
        self.directory = str(pathlib.Path(metadata_path).parent)
        self.table = table
        self.columns = list(columns)
        self.schema = schema

    @property
    def label(self):
        # One table is dumped into one directory per version, so the enclosing
        # directory name distinguishes versions even when table names repeat.
        return pathlib.Path(self.metadata_path).parent.name

    def rows(self):
        path = pathlib.Path(self.ndjson_path)
        if not path.exists():
            return []
        rows = []
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            values = json.loads(line)
            if len(values) != len(self.columns):
                raise LineageError(
                    "{}:{} has {} values but {} declares {} columns".format(
                        self.ndjson_path,
                        line_number,
                        len(values),
                        self.metadata_path,
                        len(self.columns),
                    )
                )
            rows.append(values)
        return rows


class Position:
    """A field's occurrence in one version, fixed by its positional column index."""

    __slots__ = ("version_id", "directory", "table", "index", "column", "change")

    def __init__(self, version, index, column, change=None):
        self.version_id = version.path
        self.directory = version.directory
        self.table = version.table
        self.index = index
        self.column = column
        self.change = change

    def as_dict(self):
        return {
            "version": self.version_id,
            "directory": self.directory,
            "table": self.table,
            "index": self.index,
            "column": self.column,
            "change": _public_change(self.change),
        }


class Field:
    """A business field tracked across versions. Identity never follows a name."""

    def __init__(self, field_id, canonical, version, index, column):
        self.field_id = field_id
        self.canonical = canonical
        self.key = canonical
        self.positions = [Position(version, index, column)]
        self.events = []
        self.parent = None

    def add_position(self, version, index, column, change=None):
        self.positions.append(Position(version, index, column, change))

    def rename(self, new_name, version, change):
        self.events.append(
            {
                "version": version.path,
                "type": _RENAME,
                "from": self.canonical,
                "to": new_name,
                "change": _public_change(change),
            }
        )
        self.canonical = new_name
        self.positions[-1].change = change

    def as_dict(self):
        return {
            "field": self.key,
            "canonical": self.canonical,
            "field_id": self.field_id,
            "positions": [position.as_dict() for position in self.positions],
            "events": list(self.events),
            "derived_from": self.parent.as_dict() if self.parent else None,
        }


def load_version(path):
    path = pathlib.Path(path)
    if path.is_dir():
        metadatas = sorted(path.glob("*" + _METADATA_SUFFIX))
        if not metadatas:
            raise LineageError("No .metadata.json file found in {}".format(path))
        if len(metadatas) > 1:
            raise LineageError(
                "Multiple .metadata.json files in {}; pass one explicitly".format(path)
            )
        metadata_path = metadatas[0]
    else:
        metadata_path = path
    info = _read_json(metadata_path)
    columns = info.get("columns")
    if not isinstance(columns, list) or not columns:
        raise LineageError("{} has no usable columns list".format(metadata_path))
    if len(columns) != len(set(columns)):
        raise LineageError(
            "{} declares duplicate column names: {}".format(metadata_path, columns)
        )
    stem = metadata_path.name
    table = info.get("name")
    if not table and stem.endswith(_METADATA_SUFFIX):
        table = stem[: -len(_METADATA_SUFFIX)]
    ndjson_path = metadata_path.parent / (
        stem[: -len(_METADATA_SUFFIX)] + ".ndjson"
        if stem.endswith(_METADATA_SUFFIX)
        else metadata_path.stem + ".ndjson"
    )
    return Version(
        path=path,
        metadata_path=metadata_path,
        ndjson_path=ndjson_path,
        table=table,
        columns=columns,
        schema=info.get("schema"),
    )


class Lineage:
    """Reconstruct one unified table from several versioned positional dumps.

    Cross-version identity is taken only from proven rename/split changes.
    Names and cell contents are never used to guess a column's ownership, so a
    reused name (e.g. a freshly added ``code``) stays a separate field, and an
    empty column with no evidence is never attached to another field.
    """

    def __init__(self, versions, changes=None):
        self.versions = list(versions)
        if not self.versions:
            raise LineageError("At least one version is required")
        paths = [version.path for version in self.versions]
        if len(set(paths)) != len(paths):
            raise LineageError("Duplicate versions supplied: {}".format(paths))
        self._version_by_id = {version.path: version for version in self.versions}
        self.changes = self._validate_changes(changes or [])
        self.fields = []
        self._field_at = {}
        self._next_id = 0
        self._build()
        self._assign_keys()

    @classmethod
    def from_paths(cls, version_paths, changes=None):
        return cls([load_version(path) for path in version_paths], changes)

    def _new_field(self, canonical, version, index, column):
        self._next_id += 1
        field = Field("f{}".format(self._next_id), canonical, version, index, column)
        self.fields.append(field)
        return field

    def _resolve_version(self, token):
        if isinstance(token, Version):
            if token.path not in self._version_by_id:
                raise LineageError("Unknown version {}".format(token.path))
            return token
        if token in self._version_by_id:
            return self._version_by_id[token]
        matches = [
            version
            for version in self.versions
            if version.label == token
            or pathlib.Path(version.path).name == token
        ]
        if not matches:
            known = [version.label for version in self.versions]
            raise LineageError(
                "effective_version {!r} does not match any version (known: {})".format(
                    token, known
                )
            )
        if len(matches) > 1:
            raise LineageError(
                "effective_version {!r} is ambiguous; use the full path".format(token)
            )
        return matches[0]

    def _validate_changes(self, raw_changes):
        validated = []
        for ordinal, change in enumerate(raw_changes):
            if not isinstance(change, dict):
                raise LineageError("Change #{} must be an object".format(ordinal))
            kind = change.get("type")
            if kind not in _KNOWN_TYPES:
                raise LineageError(
                    "Change #{} has unknown type {!r}; expected one of {}".format(
                        ordinal, kind, sorted(_KNOWN_TYPES)
                    )
                )
            version = self._resolve_version(change.get("effective_version"))
            if version is self.versions[0]:
                raise LineageError(
                    "Change #{} cannot take effect on the first version {}".format(
                        ordinal, version.label
                    )
                )
            if kind == _RENAME:
                allowed = {"type", "effective_version", "from", "to"}
                self._reject_extra(change, allowed, ordinal)
                source = change.get("from")
                target = change.get("to")
                self._require_name(source, "from", ordinal)
                self._require_name(target, "to", ordinal)
                if source == target:
                    raise LineageError(
                        "Change #{} rename has identical from/to {!r}".format(
                            ordinal, source
                        )
                    )
                record = dict(change)
            else:
                allowed = {"type", "effective_version", "from", "into", "sources"}
                self._reject_extra(change, allowed, ordinal)
                source = change.get("from")
                targets = change.get("into")
                self._require_name(source, "from", ordinal)
                if not isinstance(targets, list) or len(targets) < 2:
                    raise LineageError(
                        "Change #{} split needs an 'into' list of >= 2 names".format(
                            ordinal
                        )
                    )
                if any(not isinstance(name, str) or not name for name in targets):
                    raise LineageError(
                        "Change #{} split 'into' names must be strings".format(
                            ordinal
                        )
                    )
                if len(targets) != len(set(targets)):
                    raise LineageError(
                        "Change #{} split 'into' contains duplicates: {}".format(
                            ordinal, targets
                        )
                    )
                sources = change.get("sources")
                if sources is not None:
                    if not isinstance(sources, list) or len(sources) != len(targets):
                        raise LineageError(
                            "Change #{} 'sources' must align with 'into'".format(
                                ordinal
                            )
                        )
                    if any(not isinstance(index, int) for index in sources):
                        raise LineageError(
                            "Change #{} 'sources' must be integer indexes".format(
                                ordinal
                            )
                        )
                record = dict(change)
            record["_version_path"] = version.path
            record["_ordinal"] = ordinal
            validated.append(record)
        return validated

    @staticmethod
    def _reject_extra(change, allowed, ordinal):
        extra = set(change) - allowed
        if extra:
            raise LineageError(
                "Change #{} has unsupported keys: {}".format(
                    ordinal, sorted(extra)
                )
            )

    @staticmethod
    def _require_name(value, key, ordinal):
        if not isinstance(value, str) or not value:
            raise LineageError(
                "Change #{} needs a non-empty string {!r}".format(ordinal, key)
            )

    def _changes_for(self, version):
        return [
            change
            for change in self.changes
            if change["_version_path"] == version.path
        ]

    def _build(self):
        first = self.versions[0]
        first_fields = []
        for index, column in enumerate(first.columns):
            first_fields.append(self._new_field(column, first, index, column))
        self._field_at[first.path] = first_fields

        for previous, current in zip(self.versions, self.versions[1:]):
            prev_fields = self._field_at[previous.path]
            current_fields = [None] * len(current.columns)
            consumed_prev = set()

            for change in self._changes_for(current):
                if change["type"] == _RENAME:
                    self._apply_rename(
                        previous, current, prev_fields, current_fields,
                        consumed_prev, change,
                    )
                else:
                    self._apply_split(
                        previous, current, prev_fields, current_fields,
                        consumed_prev, change,
                    )

            for index, column in enumerate(current.columns):
                if current_fields[index] is not None:
                    continue
                candidates = [
                    prev_index
                    for prev_index, field in enumerate(prev_fields)
                    if prev_index not in consumed_prev
                    and previous.columns[prev_index] == column
                ]
                if len(candidates) == 1:
                    prev_index = candidates[0]
                    field = prev_fields[prev_index]
                    consumed_prev.add(prev_index)
                    field.add_position(current, index, column)
                    current_fields[index] = field
                else:
                    current_fields[index] = self._new_field(
                        column, current, index, column
                    )
            self._field_at[current.path] = current_fields

    def _find_prev(self, prev_fields, previous, consumed, name, change):
        matches = [
            index
            for index, field in enumerate(prev_fields)
            if index not in consumed and previous.columns[index] == name
        ]
        if not matches:
            raise LineageError(
                "Change #{} references source column {!r} absent in {}".format(
                    change["_ordinal"], name, previous.label
                )
            )
        if len(matches) > 1:
            raise LineageError(
                "Change #{} source column {!r} is ambiguous in {}".format(
                    change["_ordinal"], name, previous.label
                )
            )
        return matches[0]

    def _find_current(self, current_fields, current, name, change):
        matches = [
            index
            for index, column in enumerate(current.columns)
            if current_fields[index] is None and column == name
        ]
        if not matches:
            raise LineageError(
                "Change #{} target column {!r} not found in {}".format(
                    change["_ordinal"], name, current.label
                )
            )
        if len(matches) > 1:
            raise LineageError(
                "Change #{} target column {!r} is ambiguous in {}".format(
                    change["_ordinal"], name, current.label
                )
            )
        return matches[0]

    def _apply_rename(
        self, previous, current, prev_fields, current_fields, consumed, change
    ):
        source_index = self._find_prev(
            prev_fields, previous, consumed, change["from"], change
        )
        target_index = self._find_current(
            current_fields, current, change["to"], change
        )
        field = prev_fields[source_index]
        consumed.add(source_index)
        field.add_position(current, target_index, change["to"], change)
        field.rename(change["to"], current, change)
        current_fields[target_index] = field

    def _apply_split(
        self, previous, current, prev_fields, current_fields, consumed, change
    ):
        source_index = self._find_prev(
            prev_fields, previous, consumed, change["from"], change
        )
        parent = prev_fields[source_index]
        consumed.add(source_index)
        targets = change["into"]
        if change.get("sources") is not None:
            target_indexes = change["sources"]
            for index, name in zip(target_indexes, targets):
                if index < 0 or index >= len(current.columns):
                    raise LineageError(
                        "Change #{} source index {} out of range in {}".format(
                            change["_ordinal"], index, current.label
                        )
                    )
                if current.columns[index] != name:
                    raise LineageError(
                        "Change #{} source index {} is {!r}, expected {!r}".format(
                            change["_ordinal"],
                            index,
                            current.columns[index],
                            name,
                        )
                    )
                if current_fields[index] is not None:
                    raise LineageError(
                        "Change #{} source index {} already claimed".format(
                            change["_ordinal"], index
                        )
                    )
        else:
            target_indexes = [
                self._find_current(current_fields, current, name, change)
                for name in targets
            ]
        split_event = {
            "version": current.path,
            "type": _SPLIT,
            "from": change["from"],
            "into": list(targets),
            "change": _public_change(change),
        }
        parent.events.append(split_event)
        for index, name in zip(target_indexes, targets):
            child = self._new_field(name, current, index, name)
            child.parent = parent
            child.positions[0].change = change
            current_fields[index] = child

    def _assign_keys(self):
        used = set()
        for field in self.fields:
            key = field.canonical
            if key in used:
                key = "{}#{}".format(field.canonical, field.field_id)
            field.key = key
            used.add(key)

    def field_at(self, version, index):
        version = self._resolve_version(version)
        return self._field_at[version.path][index]

    def effective_columns(self, version):
        """Unified field key occupying each physical column of this version."""
        version = self._resolve_version(version)
        return [field.key for field in self._field_at[version.path]]

    def fields_named(self, name):
        return [
            field
            for field in self.fields
            if field.canonical == name
            or any(position.column == name for position in field.positions)
        ]

    def history(self, name):
        """Provenance of every field that ever used the given column name.

        Two unrelated fields can share a name; each is returned separately with
        its original directory, positional column order and change notes.
        """
        return [field.as_dict() for field in self.fields_named(name)]

    @property
    def unified_columns(self):
        """Stable unified field order: the order each field first appeared."""
        return [field.key for field in self.fields]

    def reconstruct(self, version=None, provenance=True):
        """Build unified rows, preserving archive row order and column position.

        Every unified field is present in each row; fields that did not exist in
        a row's version are filled with ``None`` rather than guessed.
        """
        if version is not None:
            targets = [self._resolve_version(version)]
        else:
            targets = self.versions
        unified = []
        for current in targets:
            fields = self._field_at[current.path]
            for values in current.rows():
                by_field = {field.key: None for field in self.fields}
                for index, value in enumerate(values):
                    by_field[fields[index].key] = value
                row = {}
                if provenance:
                    row["_version"] = current.label
                    row["_source_directory"] = current.directory
                for key in self.unified_columns:
                    row[key] = by_field[key]
                unified.append(row)
        return unified

    def describe(self):
        return {
            "versions": [
                {
                    "version": version.label,
                    "path": version.path,
                    "directory": version.directory,
                    "table": version.table,
                    "columns": list(version.columns),
                    "effective_columns": self.effective_columns(version),
                }
                for version in self.versions
            ],
            "changes": [
                {
                    key: value
                    for key, value in change.items()
                    if not key.startswith("_")
                }
                for change in self.changes
            ],
            "fields": [field.as_dict() for field in self.fields],
        }
