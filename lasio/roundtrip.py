"""Pre-export round-trip verification for fixed-width numeric columns.

Lasio writes numeric columns using a format string and a numeric field width.
Downstream systems that read those columns as fixed-width decimal fields can
silently lose sampling identity: two distinct depths may round to the same
text and therefore the same value after reading.  This module checks an
in-memory :class:`lasio.LASFile` before it is exported and reports

* which columns can be represented within the requested field width,
* the maximum absolute error introduced by the decimal formatting,
* duplicate sample identities that already existed in the source data, and
* duplicate sample identities that are introduced only by rounding.

The verification is read-only.  It never drops or rewrites rows.  When the
requested width cannot satisfy the error or identity requirements, a bounded
set of concrete conflicting samples is returned (and included in the exception
message when ``raise_on_failure=True``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from . import exceptions


_NUMERIC_KINDS = frozenset("iufc")
_MAX_USEFUL_PRECISION = 15


@dataclass
class RoundTripConflict:
    """One piece of evidence that a fixed-width round trip is unsafe.

    ``kind`` is one of ``"rounding_collision"``, ``"tolerance_exceeded"``,
    ``"width_overflow"`` or ``"source_duplicate"``.
    """

    kind: str
    rows: Tuple[int, ...]
    identities: Tuple[Tuple[Any, ...], ...] = ()
    formatted_identities: Tuple[Tuple[Any, ...], ...] = ()
    curve: Optional[str] = None
    values: Tuple[Any, ...] = ()
    formatted_values: Tuple[Any, ...] = ()
    errors: Tuple[Optional[float], ...] = ()
    detail: str = ""
    affected_rows: Optional[int] = None

    @property
    def truncated(self) -> bool:
        """True when more rows were involved than are listed in ``rows``."""
        return self.affected_rows is not None and self.affected_rows > len(self.rows)

    @property
    def row_index(self) -> Optional[int]:
        """First row index, for the common single-sample case."""
        return self.rows[0] if self.rows else None

    @property
    def row_indices(self) -> Tuple[int, ...]:
        return self.rows

    @property
    def rows_involved(self) -> Tuple[int, ...]:
        return self.rows

    @property
    def source_identities(self) -> Tuple[Tuple[Any, ...], ...]:
        return self.identities

    @property
    def original_identities(self) -> Tuple[Tuple[Any, ...], ...]:
        return self.identities

    @property
    def rounded_identities(self) -> Tuple[Tuple[Any, ...], ...]:
        return self.formatted_identities

    @property
    def original_values(self) -> Tuple[Any, ...]:
        return self.values

    @property
    def rounded_values(self) -> Tuple[Any, ...]:
        return self.formatted_values

    @property
    def identity(self) -> Optional[Tuple[Any, ...]]:
        return self.identities[0] if self.identities else None

    @property
    def formatted_identity(self) -> Optional[Tuple[Any, ...]]:
        return self.formatted_identities[0] if self.formatted_identities else None

    @property
    def value(self) -> Any:
        return self.values[0] if self.values else None

    @property
    def formatted_value(self) -> Any:
        return self.formatted_values[0] if self.formatted_values else None

    @property
    def error(self) -> Optional[float]:
        return self.errors[0] if self.errors else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "rows": list(self.rows),
            "identities": [list(identity) for identity in self.identities],
            "formatted_identities": [
                list(identity) for identity in self.formatted_identities
            ],
            "curve": self.curve,
            "values": list(self.values),
            "formatted_values": list(self.formatted_values),
            "errors": list(self.errors),
            "detail": self.detail,
        }

    def __str__(self) -> str:
        if self.kind == "rounding_collision":
            pieces = []
            for row, source, formatted in zip(
                self.rows, self.identities, self.formatted_identities
            ):
                pieces.append(
                    "row {row}: {source} -> {formatted}".format(
                        row=row,
                        source=_format_identity(source),
                        formatted=_format_identity(formatted),
                    )
                )
            return "rounding collision ({rows}): {pieces}".format(
                rows=", ".join(str(row) for row in self.rows),
                pieces="; ".join(pieces),
            )
        if self.kind == "source_duplicate":
            return "source duplicate at {identity} (rows {rows})".format(
                identity=_format_identity(self.identity or ()),
                rows=", ".join(str(row) for row in self.rows),
            )
        if self.kind in ("tolerance_exceeded", "width_overflow"):
            return (
                "{kind} at row {row}, curve {curve}: {value!r} -> {formatted!r}"
                " (error {error}, field width {width})".format(
                    kind=self.kind,
                    row=self.row_index,
                    curve=self.curve,
                    value=self.value,
                    formatted=self.formatted_value,
                    error=self.error,
                    width=self.detail,
                )
            )
        return "{kind} at rows {rows}: {detail}".format(
            kind=self.kind,
            rows=", ".join(str(row) for row in self.rows),
            detail=self.detail,
        )


@dataclass
class RoundTripReport:
    """Result of :func:`verify_roundtrip`.

    A report is falsy when the round trip is unsafe.  Inspect ``conflicts`` for
    bounded evidence, ``source_duplicates`` for same-depth samples that already
    existed in the input, and ``rounding_collisions`` for collisions that only
    appear after formatting.
    """

    column_width: Any
    tolerance: Any
    identity: Tuple[str, ...]
    precision: Dict[str, Optional[int]] = field(default_factory=dict)
    required_precision: Dict[str, Optional[int]] = field(default_factory=dict)
    source_row_count: int = 0
    formatted_row_count: int = 0
    verified_curves: Tuple[str, ...] = ()
    source_duplicates: List[RoundTripConflict] = field(default_factory=list)
    rounding_collisions: List[RoundTripConflict] = field(default_factory=list)
    tolerance_violations: List[RoundTripConflict] = field(default_factory=list)
    width_overflows: List[RoundTripConflict] = field(default_factory=list)
    total_rounding_collisions: int = 0
    total_tolerance_violations: int = 0
    total_width_overflows: int = 0
    total_source_duplicate_rows: int = 0
    max_abs_error: Dict[str, float] = field(default_factory=dict)
    minimum_width: Dict[str, Optional[int]] = field(default_factory=dict)
    minimum_common_width: Optional[int] = None
    max_examples: int = 5

    @property
    def conflicts(self) -> List[RoundTripConflict]:
        """Bounded evidence for all failures (collisions first)."""
        return (
            list(self.rounding_collisions)
            + list(self.tolerance_violations)
            + list(self.width_overflows)
        )

    @property
    def samples(self) -> List[RoundTripConflict]:
        return self.conflicts

    @property
    def evidence(self) -> List[RoundTripConflict]:
        return self.conflicts

    @property
    def rounding_collisions_count(self) -> int:
        return self.total_rounding_collisions

    @property
    def source_duplicate_count(self) -> int:
        return len(self.source_duplicates)

    @property
    def source_duplicate_row_count(self) -> int:
        return sum(len(conflict.rows) for conflict in self.source_duplicates)

    @property
    def induced_collisions(self) -> List[RoundTripConflict]:
        return self.rounding_collisions

    @property
    def identity_collisions(self) -> List[RoundTripConflict]:
        return self.rounding_collisions

    @property
    def preexisting_duplicates(self) -> List[RoundTripConflict]:
        return self.source_duplicates

    @property
    def original_duplicates(self) -> List[RoundTripConflict]:
        return self.source_duplicates

    @property
    def duplicate_depths(self) -> List[RoundTripConflict]:
        return self.source_duplicates

    @property
    def row_count_preserved(self) -> bool:
        return self.source_row_count == self.formatted_row_count

    @property
    def ok(self) -> bool:
        return (
            self.total_rounding_collisions == 0
            and self.total_tolerance_violations == 0
            and self.total_width_overflows == 0
            and self.row_count_preserved
        )

    @property
    def has_source_duplicates(self) -> bool:
        """True when the input already contained repeated sample identities."""
        return self.source_duplicate_count > 0

    @property
    def has_induced_collisions(self) -> bool:
        """True when formatting/rounding created new identity collisions."""
        return self.total_rounding_collisions > 0

    @property
    def failed_curves(self) -> Tuple[str, ...]:
        names = []
        for conflict in self.tolerance_violations + self.width_overflows:
            if conflict.curve is not None and conflict.curve not in names:
                names.append(conflict.curve)
        return tuple(names)

    # Common spellings for a passing/failing report.
    @property
    def passed(self) -> bool:
        return self.ok

    @property
    def is_ok(self) -> bool:
        return self.ok

    def __bool__(self) -> bool:
        return self.ok

    def raise_for_status(self) -> "RoundTripReport":
        """Raise :class:`lasio.exceptions.LASRoundTripError` if unsafe."""
        if not self.ok:
            raise exceptions.LASRoundTripError(self)
        return self

    def summary(self) -> str:
        lines = [
            "round-trip width={width}, tolerance={tolerance}, identity={identity}".format(
                width=self.column_width,
                tolerance=self.tolerance,
                identity=",".join(self.identity) if self.identity else "<none>",
            ),
            "rows: {source} source / {formatted} formatted ({state})".format(
                source=self.source_row_count,
                formatted=self.formatted_row_count,
                state="preserved" if self.row_count_preserved else "CHANGED",
            ),
            "source duplicate identity groups: {} ({} rows)".format(
                self.source_duplicate_count, self.total_source_duplicate_rows
            ),
            "rounding-induced identity collisions: {}".format(
                self.total_rounding_collisions
            ),
            "tolerance violations: {}".format(self.total_tolerance_violations),
            "width overflows: {}".format(self.total_width_overflows),
        ]
        if self.precision:
            lines.append(
                "precision: "
                + ", ".join(
                    "{}={}".format(name, value)
                    for name, value in self.precision.items()
                )
            )
        if self.max_abs_error:
            lines.append(
                "max abs error: "
                + ", ".join(
                    "{}={:.6g}".format(name, value)
                    for name, value in self.max_abs_error.items()
                )
            )
        if self.minimum_width:
            lines.append(
                "width needed for tolerance: "
                + ", ".join(
                    "{}={}".format(name, value)
                    for name, value in self.minimum_width.items()
                )
            )
        if self.conflicts:
            lines.append("conflicting samples (showing up to {}):".format(self.max_examples))
            lines.extend("  - {}".format(conflict) for conflict in self.conflicts)
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "column_width": self.column_width,
            "tolerance": self.tolerance,
            "identity": list(self.identity),
            "precision": dict(self.precision),
            "required_precision": dict(self.required_precision),
            "source_row_count": self.source_row_count,
            "formatted_row_count": self.formatted_row_count,
            "row_count_preserved": self.row_count_preserved,
            "verified_curves": list(self.verified_curves),
            "minimum_width": dict(self.minimum_width),
            "minimum_common_width": self.minimum_common_width,
            "source_duplicates": [
                conflict.to_dict() for conflict in self.source_duplicates
            ],
            "rounding_collisions": [
                conflict.to_dict() for conflict in self.rounding_collisions
            ],
            "tolerance_violations": [
                conflict.to_dict() for conflict in self.tolerance_violations
            ],
            "width_overflows": [
                conflict.to_dict() for conflict in self.width_overflows
            ],
            "max_abs_error": dict(self.max_abs_error),
            "counts": {
                "source_duplicates": self.source_duplicate_count,
                "source_duplicate_rows": self.total_source_duplicate_rows,
                "rounding_collisions": self.total_rounding_collisions,
                "tolerance_violations": self.total_tolerance_violations,
                "width_overflows": self.total_width_overflows,
            },
        }


def verify_roundtrip(
    las,
    column_width: Any = None,
    tolerance: Any = None,
    identity: Any = None,
    *,
    width: Any = None,
    target_width: Any = None,
    field_width: Any = None,
    len_numeric_field: Any = None,
    tol: Any = None,
    acceptable_error: Any = None,
    max_error: Any = None,
    sample_identity: Any = None,
    identity_columns: Any = None,
    key_columns: Any = None,
    depth_columns: Any = None,
    columns: Any = None,
    precision: Any = None,
    fmt: Any = None,
    max_examples: int = 5,
    raise_on_failure: bool = False,
    strict: bool = False,
    **kwargs,
) -> RoundTripReport:
    """Check fixed-width decimal formatting before exporting *las*.

    Arguments:
        las (:class:`lasio.LASFile`): file to verify.  It is not modified.
        column_width (int, dict, sequence): target width of each numeric field,
            excluding the inter-column spacer.  A scalar applies to every
            curve; a dict maps mnemonic or column index to a width; a sequence
            gives one width per curve.  The aliases ``width`` and
            ``len_numeric_field`` are accepted through :func:`lasio.writer.write`.
        tolerance (float, dict): maximum acceptable absolute difference between
            a source value and the value obtained after formatting and reading
            it back.  A scalar applies to every verified curve; a dict maps
            mnemonic or column index to a tolerance.  ``tol`` and
            ``acceptable_error`` are accepted aliases.
        identity (str, int, sequence): curve mnemonic(s) or column index/indices
            whose values define a sample's identity.  These are normally the
            depth/index curve.  Defaults to the first curve.  ``sample_identity``
            and ``identity_columns`` are accepted aliases.

    Keyword Arguments:
        columns: curve mnemonics/indices to verify for numeric error and width.
            Defaults to all curves.  Identity curves are always checked for
            collisions even if omitted here.
        precision (int, dict): explicit number of decimal places.  By default
            the maximum precision that fits ``column_width`` is used.
        fmt (str, dict): explicit Python numeric format string, as used by
            :func:`lasio.writer.write`.  When supplied this overrides
            ``precision`` for the affected columns.
        max_examples (int): maximum number of conflicting samples retained for
            each failure category (default 5).
        raise_on_failure (bool): raise
            :class:`lasio.exceptions.LASRoundTripError` instead of only
            returning a failing report.  ``strict`` is an alias.
        strict (bool): alias for ``raise_on_failure``.

    Returns:
        :class:`RoundTripReport`.  Use ``report.ok`` (or ``bool(report)``) to
        decide whether export is safe.  A failing report contains at most
        ``max_examples`` concrete conflicts of each kind.  Same-depth samples
        already present in the input are reported separately in
        ``report.source_duplicates`` and do not by themselves fail the check.
    """
    column_width = _coalesce(
        "column_width", column_width, width, target_width, field_width, len_numeric_field
    )
    tolerance = _coalesce(
        "tolerance", tolerance, tol, acceptable_error, max_error
    )
    identity = _coalesce(
        "identity", identity, sample_identity, identity_columns, key_columns,
        depth_columns,
    )
    if kwargs:
        raise TypeError(
            "unexpected keyword arguments: {}".format(", ".join(sorted(kwargs)))
        )
    if column_width is None:
        raise ValueError("column_width (or width) must be provided")
    if tolerance is None:
        raise ValueError(
            "tolerance (or tol/acceptable_error) must be provided"
        )

    curve_names = [curve.mnemonic for curve in las.curves]
    if not curve_names:
        raise ValueError("LAS file has no curves to verify")

    identity_names = _normalize_identity(las, identity)
    if columns is not None:
        requested_names = _normalize_columns(las, columns)
    else:
        requested_names = list(curve_names)
    verified_names = list(requested_names)
    # Identity curves always take part in collision checking.
    for name in identity_names:
        if name not in verified_names:
            verified_names.append(name)

    widths = _per_curve_values(
        column_width, curve_names, "column_width", allow_unbounded=True
    )
    tolerances = _per_curve_values(
        tolerance, curve_names, "tolerance", require_non_negative=True
    )
    precisions = _per_curve_values(
        precision, curve_names, "precision", allow_none=True, require_non_negative=True
    )
    formats = _per_curve_values(
        fmt, curve_names, "fmt", allow_none=True, as_string=True
    )
    # Only the selected curves need width/tolerance entries.  Identity curves
    # added outside that selection inherit the widest width and strictest
    # tolerance supplied, so collision checking still uses the same budget.
    _require_complete_mapping(
        {name: widths[name] for name in requested_names},
        requested_names,
        "column_width",
    )
    _require_complete_mapping(
        {name: tolerances[name] for name in requested_names},
        requested_names,
        "tolerance",
    )
    supplied_widths = [
        widths[name] for name in requested_names if widths[name] not in (None, -1)
    ]
    supplied_tolerances = [
        tolerances[name] for name in requested_names if tolerances[name] is not None
    ]
    fallback_width = max(supplied_widths) if supplied_widths else -1
    fallback_tolerance = min(supplied_tolerances) if supplied_tolerances else 0.0
    for name in curve_names:
        if name in requested_names:
            continue
        if name in identity_names:
            widths[name] = fallback_width
            tolerances[name] = fallback_tolerance
        else:
            widths[name] = -1
            tolerances[name] = 0.0

    data_by_name = {
        curve.mnemonic: np.asarray(curve.data) for curve in las.curves
    }
    numeric_by_name = {
        name: _is_numeric_array(data_by_name[name]) for name in curve_names
    }

    source_rows = _row_count(data_by_name)
    report = RoundTripReport(
        column_width=column_width,
        tolerance=tolerance,
        identity=tuple(identity_names),
        source_row_count=source_rows,
        formatted_row_count=source_rows,
        verified_curves=tuple(verified_names),
        max_examples=max_examples,
    )

    column_precision: Dict[str, Optional[int]] = {}
    column_required_precision: Dict[str, Optional[int]] = {}

    for name in curve_names:
        if not numeric_by_name[name]:
            column_precision[name] = None
            column_required_precision[name] = None
            continue
        array = data_by_name[name]
        finite = _finite_values(array)
        required = _required_precision(tolerances[name])
        column_required_precision[name] = required
        fit_width = _MAX_USEFUL_PRECISION if widths[name] == -1 else widths[name]
        if name in formats and formats[name] is not None:
            column_precision[name] = _precision_from_format(formats[name])
        elif name in precisions and precisions[name] is not None:
            column_precision[name] = precisions[name]
        else:
            column_precision[name] = _max_fitting_precision(finite, fit_width)
    report.precision = column_precision
    report.required_precision = column_required_precision

    # Curves outside the selected budget are still formatted so identity
    # columns can be compared, but they do not raise width/error evidence.
    budgeted_names = set(verified_names)

    # ------------------------------------------------------------------
    # Format every row once, retaining both the source and read-back keys.
    # No rows are removed; this is deliberately a 1:1 mapping.
    # ------------------------------------------------------------------
    parsed_values: Dict[str, np.ndarray] = {}
    overflow_counts = {name: 0 for name in curve_names}
    error_max = {name: 0.0 for name in curve_names}
    tolerance_counts = {name: 0 for name in curve_names}
    tolerance_samples = {name: [] for name in curve_names}
    overflow_samples = {name: [] for name in curve_names}

    null_value = las.well["NULL"].value if "NULL" in las.well else None
    null_text = str(null_value) if null_value is not None else ""

    for name in curve_names:
        array = data_by_name[name]
        width = widths[name]
        if numeric_by_name[name]:
            rendered, parsed, overflow_rows = _format_numeric_column(
                array,
                precision=column_precision[name],
                fmt=formats.get(name),
                null_text=null_text,
                width=width,
            )
        else:
            rendered, parsed, overflow_rows = _format_other_column(
                array, null_text=null_text, width=width
            )
        parsed_values[name] = parsed

        if name in budgeted_names:
            overflow_counts[name] = len(overflow_rows)
            for row in overflow_rows[:max_examples]:
                overflow_samples[name].append((row, array[row], rendered[row]))

        if name in verified_names and numeric_by_name[name]:
            values = array.astype(float)
            valid = np.isfinite(values) & np.isfinite(parsed)
            if valid.any():
                errors = np.abs(parsed[valid] - values[valid])
                error_max[name] = float(errors.max())
                local_rows = np.nonzero(valid)[0]
                violating_mask = errors > float(tolerances[name])
                violating = local_rows[violating_mask]
            else:
                error_max[name] = 0.0
                violating = np.array([], dtype=int)
            tolerance_counts[name] = len(violating)
            for row in violating[:max_examples]:
                tolerance_samples[name].append(
                    (
                        row,
                        array[row],
                        rendered[row],
                        parsed[row],
                        abs(float(parsed[row]) - float(array[row])),
                    )
                )

    report.max_abs_error = {
        name: error for name, error in error_max.items() if name in verified_names
    }

    # ------------------------------------------------------------------
    # Width and tolerance evidence.
    # ------------------------------------------------------------------
    for name in curve_names:
        if name not in budgeted_names:
            continue
        for row, value, rendered in overflow_samples[name]:
            report.width_overflows.append(
                RoundTripConflict(
                    kind="width_overflow",
                    rows=(row,),
                    curve=name,
                    values=(_as_evidence_value(value),),
                    formatted_values=(rendered,),
                    detail=str(widths[name]),
                )
            )
        report.total_width_overflows += overflow_counts[name]

    for name in verified_names:
        for row, value, rendered, parsed, error in tolerance_samples[name]:
            report.tolerance_violations.append(
                RoundTripConflict(
                    kind="tolerance_exceeded",
                    rows=(row,),
                    curve=name,
                    values=(_as_evidence_value(value),),
                    formatted_values=(rendered,),
                    errors=(error,),
                    detail="required precision {}, used {}".format(
                        column_required_precision[name], column_precision[name]
                    ),
                )
            )
        report.total_tolerance_violations += tolerance_counts[name]

    # ------------------------------------------------------------------
    # Identity checks.  Distinguish equal identity keys present in the
    # source from distinct keys which only become equal after rounding.
    # ------------------------------------------------------------------
    identity_rows = _valid_identity_rows(
        data_by_name, identity_names, source_rows
    )
    source_groups: Dict[Tuple[Any, ...], List[int]] = defaultdict(list)
    source_keys: Dict[int, Tuple[Any, ...]] = {}
    for row in identity_rows:
        key = _identity_key(data_by_name, identity_names, row)
        source_keys[row] = key
        source_groups[key].append(row)

    source_duplicate_rows = 0
    for key, rows in source_groups.items():
        if len(rows) > 1:
            source_duplicate_rows += len(rows)
            if len(report.source_duplicates) < max_examples:
                report.source_duplicates.append(
                    RoundTripConflict(
                        kind="source_duplicate",
                        rows=tuple(rows[:max_examples]),
                        identities=tuple(key for _ in rows[:max_examples]),
                        formatted_identities=tuple(
                            _formatted_identity_key(
                                parsed_values, identity_names, row
                            )
                            for row in rows[:max_examples]
                        ),
                        detail=(
                            "same identity already exists in source data at "
                            "{} row(s)".format(len(rows))
                        ),
                        affected_rows=len(rows),
                    )
                )
    report.total_source_duplicate_rows = source_duplicate_rows

    formatted_groups: Dict[Tuple[Any, ...], List[int]] = defaultdict(list)
    for row in identity_rows:
        formatted_key = _formatted_identity_key(parsed_values, identity_names, row)
        formatted_groups[formatted_key].append(row)

    collision_count = 0
    for formatted_key, rows in formatted_groups.items():
        distinct_source_keys = {source_keys[row] for row in rows}
        if len(distinct_source_keys) <= 1:
            # Either a unique row or source duplicate(s) that remain together.
            continue
        collision_count += 1
        if len(report.rounding_collisions) < max_examples:
            # Keep at most one row per distinct source identity, and no more
            # than ``max_examples`` rows, so the evidence shows *why* the keys
            # differ without growing with the dataset.
            shown = []
            seen = set()
            for row in rows:
                if source_keys[row] in seen:
                    continue
                if len(shown) >= max_examples:
                    break
                seen.add(source_keys[row])
                shown.append(row)
            report.rounding_collisions.append(
                RoundTripConflict(
                    kind="rounding_collision",
                    rows=tuple(shown),
                    identities=tuple(source_keys[row] for row in shown),
                    formatted_identities=tuple(formatted_key for _ in shown),
                    detail=(
                        "{} distinct source identities collapse to one "
                        "formatted identity; all rows are retained, not "
                        "deduplicated".format(len(distinct_source_keys))
                    ),
                    affected_rows=len(rows),
                )
            )
    report.total_rounding_collisions = collision_count

    report.minimum_width = _minimum_widths_for_tolerance(
        curve_names,
        data_by_name,
        numeric_by_name,
        verified_names,
        tolerances,
        formats,
        precisions,
    )
    if identity_names and identity_rows:
        report.minimum_common_width = _minimum_identity_width(
            identity_names, data_by_name, identity_rows, source_keys
        )

    if raise_on_failure or strict:
        report.raise_for_status()
    return report


# ``check_roundtrip`` is a convenient alias used by some callers.
check_roundtrip = verify_roundtrip
roundtrip_check = verify_roundtrip
verify = verify_roundtrip
check = verify_roundtrip


def _coalesce(label, *values):
    """Return the first non-None value; raise if more than one was supplied."""
    supplied = [value for value in values if value is not None]
    if not supplied:
        return None
    first = supplied[0]
    for other in supplied[1:]:
        if other != first:
            raise ValueError(
                "conflicting values supplied for {}".format(label)
            )
    return first


def _normalize_identity(las, identity) -> List[str]:
    names = [curve.mnemonic for curve in las.curves]
    if identity is None:
        if not names:
            return []
        return [names[0]]
    if isinstance(identity, (str, np.str_)):
        requested = [str(identity)]
    elif isinstance(identity, (int, np.integer)):
        requested = [int(identity)]
    else:
        requested = list(identity)
    result = []
    for item in requested:
        result.append(_resolve_curve_name(las, item))
    return result


def _normalize_columns(las, columns) -> List[str]:
    if isinstance(columns, (str, np.str_, int, np.integer)):
        columns = [columns]
    return [_resolve_curve_name(las, item) for item in columns]


def _resolve_curve_name(las, item) -> str:
    names = [curve.mnemonic for curve in las.curves]
    if isinstance(item, (int, np.integer)):
        index = int(item)
        try:
            return names[index]
        except IndexError:
            raise ValueError("curve index {} is out of range".format(index))
    text = str(item)
    for name in names:
        if name == text:
            return name
    lowered = text.lower()
    matches = [name for name in names if name.lower() == lowered]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            "curve {!r} not found in curves ({})".format(text, names)
        )
    raise ValueError(
        "curve {!r} is ambiguous in curves ({})".format(text, names)
    )


def _per_curve_values(
    value,
    curve_names,
    label,
    *,
    allow_none: bool = False,
    allow_unbounded: bool = False,
    as_string: bool = False,
    require_positive: bool = False,
    require_non_negative: bool = False,
) -> Dict[str, Any]:
    result = {name: None for name in curve_names}
    if value is None:
        if allow_none:
            return result
        raise ValueError("{} must be provided".format(label))
    if isinstance(value, Mapping):
        for key, entry in value.items():
            name = _resolve_name_from_list(curve_names, key, label)
            result[name] = _validate_number(
                entry,
                label,
                allow_none=allow_none,
                allow_unbounded=allow_unbounded,
                as_string=as_string,
                require_positive=require_positive,
                require_non_negative=require_non_negative,
            )
        return result
    if isinstance(value, (int, float, np.integer, np.floating, str)):
        # A string is only treated as a scalar format value, never as a
        # sequence of widths/tolerances.
        entry = _validate_number(
            value,
            label,
            allow_none=allow_none,
            allow_unbounded=allow_unbounded,
            as_string=as_string,
            require_positive=require_positive,
            require_non_negative=require_non_negative,
        )
        for name in curve_names:
            result[name] = entry
        return result
    try:
        sequence = list(value)
    except TypeError:
        raise ValueError(
            "{} must be a scalar, mapping or sequence".format(label)
        )
    if len(sequence) != len(curve_names):
        raise ValueError(
            "{} sequence must have one entry per curve ({} != {})".format(
                label, len(sequence), len(curve_names)
            )
        )
    for name, entry in zip(curve_names, sequence):
        result[name] = _validate_number(
            entry,
            label,
            allow_none=allow_none,
            allow_unbounded=allow_unbounded,
            as_string=as_string,
            require_positive=require_positive,
            require_non_negative=require_non_negative,
        )
    return result


def _require_complete_mapping(values, curve_names, label):
    missing = [name for name in curve_names if values.get(name) is None]
    if missing:
        raise ValueError(
            "{} mapping is missing entries for: {}".format(
                label, ", ".join(missing)
            )
        )


def _resolve_name_from_list(curve_names, key, label):
    if isinstance(key, (int, np.integer)):
        try:
            return curve_names[int(key)]
        except IndexError:
            raise ValueError("{} curve index {} is out of range".format(label, key))
    text = str(key)
    if text in curve_names:
        return text
    matches = [name for name in curve_names if name.lower() == text.lower()]
    if len(matches) == 1:
        return matches[0]
    raise ValueError("{} curve {!r} not found in curves ({})".format(label, text, curve_names))


def _validate_number(
    value,
    label,
    *,
    allow_none: bool,
    allow_unbounded: bool,
    as_string: bool,
    require_positive: bool,
    require_non_negative: bool,
):
    if value is None and allow_none:
        return None
    if as_string:
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("{} entries must be numeric, got {!r}".format(label, value))
    if allow_unbounded and number == -1:
        return -1
    if require_positive and number <= 0:
        raise ValueError("{} must be greater than zero".format(label))
    if require_non_negative and number < 0:
        raise ValueError("{} must be zero or greater".format(label))
    # Widths and precisions are integral; accept 8.0 as 8.
    if label in ("column_width", "precision"):
        if number != int(number):
            raise ValueError("{} entries must be whole numbers".format(label))
        return int(number)
    return number


def _row_count(data_by_name) -> int:
    if not data_by_name:
        return 0
    lengths = {len(array) for array in data_by_name.values()}
    if len(lengths) > 1:
        raise ValueError("curves do not all have the same number of samples")
    return lengths.pop()


def _is_numeric_array(array) -> bool:
    dtype = getattr(array, "dtype", None)
    if dtype is None:
        return False
    return dtype.kind in _NUMERIC_KINDS


def _is_finite_number(value) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _finite_values(array) -> List[float]:
    values = []
    for value in np.asarray(array):
        if _is_finite_number(value):
            values.append(float(value))
    return values


def _required_precision(tolerance: float) -> Optional[int]:
    tolerance = float(tolerance)
    if tolerance == 0:
        return _MAX_USEFUL_PRECISION
    if not math.isfinite(tolerance):
        return 0
    required = math.ceil(-math.log10(2.0 * tolerance) - 1e-12)
    return max(0, int(required))


def _minimum_widths_for_tolerance(
    curve_names,
    data_by_name,
    numeric_by_name,
    verified_names,
    tolerances,
    formats,
    precisions,
):
    """Smallest width per curve that meets the requested tolerance.

    The figure assumes the number of decimal places can be raised as needed;
    an explicit ``fmt`` pins the precision and is therefore reported through
    the conflict detail instead of here.
    """
    widths: Dict[str, Optional[int]] = {}
    for name in verified_names:
        if not numeric_by_name[name]:
            widths[name] = None
            continue
        finite = _finite_values(data_by_name[name])
        if not finite:
            widths[name] = 0
            continue
        required = _required_precision(tolerances[name])
        if name in precisions and precisions[name] is not None:
            required = max(required, precisions[name])
        if name in formats and formats[name] is not None:
            explicit = _precision_from_format(formats[name])
            if explicit is not None:
                required = max(required, explicit)
        template = "%." + str(required) + "f"
        widths[name] = max(len(template % value) for value in finite)
    return widths


def _minimum_identity_width(identity_names, data_by_name, identity_rows, source_keys):
    """Smallest common width that keeps distinct identities distinct.

    Rows that already share an identity cannot be separated by formatting and
    are represented once, so the figure describes what width would be needed
    if the source duplicates are acceptable.
    """
    representatives = []
    seen = set()
    for row in identity_rows:
        key = source_keys[row]
        if key in seen:
            continue
        seen.add(key)
        representatives.append(row)
    if len(representatives) < 2:
        return None

    for precision in range(0, _MAX_USEFUL_PRECISION + 1):
        templates = ["%." + str(precision) + "f" for _ in identity_names]
        keys = []
        for row in representatives:
            key = []
            for name, template in zip(identity_names, templates):
                value = data_by_name[name][row]
                try:
                    key.append(template % float(value))
                except (TypeError, ValueError):
                    key.append(str(value))
            keys.append(tuple(key))
        if len(set(keys)) == len(keys):
            return max(len(key_value) for key in keys for key_value in key)
    return None


def _max_fitting_precision(finite_values: Sequence[float], width: int) -> Optional[int]:
    if width == -1:
        return _MAX_USEFUL_PRECISION
    if width <= 0:
        return None
    if not finite_values:
        return 0
    max_integer_chars = max(_integer_char_count(value) for value in finite_values)
    if max_integer_chars > width:
        return None
    candidate = min(width - max_integer_chars, _MAX_USEFUL_PRECISION)
    while candidate >= 0:
        template = "%." + str(candidate) + "f"
        fits = True
        for value in finite_values:
            if len(template % value) > width:
                fits = False
                break
        if fits:
            return candidate
        candidate -= 1
    return None


def _integer_char_count(value: float) -> int:
    if value == 0:
        return 1
    digits = len(str(int(abs(value))))
    if value < 0:
        digits += 1
    return digits


def _precision_from_format(fmt: str) -> Optional[int]:
    try:
        text = str(fmt)
    except Exception:
        return None
    # Handles common printf-style formats such as %.3f and %8.3f.
    import re

    match = re.search(r"\.(\d+)[fFeEgG]", text)
    if match:
        return int(match.group(1))
    return None


def _format_numeric_column(array, *, precision, fmt, null_text, width):
    """Vectorised formatting of a numeric curve.

    Returns ``(rendered, parsed, overflow_rows)``.  ``rendered`` and ``parsed``
    have one entry per row; ``overflow_rows`` is an integer index array.
    """
    values = np.asarray(array, dtype=float)
    count = values.shape[0]
    rendered = np.empty(count, dtype=object)
    parsed = np.empty(count, dtype=float)

    null_mask = np.isnan(values)
    if null_mask.any():
        rendered[null_mask] = null_text
        # The NULL indicator is a legitimate fixed-width token; it is not
        # treated as numeric evidence.
        parsed[null_mask] = np.nan

    valid = ~null_mask
    if valid.any():
        if fmt:
            rendered[valid] = [fmt % value for value in values[valid]]
        elif precision is None:
            rendered[valid] = [str(value) for value in values[valid]]
        else:
            rendered[valid] = [
                ("%." + str(precision) + "f") % value for value in values[valid]
            ]
        parsed[valid] = [
            _parse_float(text, fallback=float(value))
            for text, value in zip(rendered[valid], values[valid])
        ]

    if width != -1:
        lengths = np.array(
            [len(text) if text is not None else 0 for text in rendered],
            dtype=int,
        )
        overflow_rows = np.nonzero(valid & (lengths > width))[0]
    else:
        overflow_rows = np.array([], dtype=int)

    return rendered, parsed, overflow_rows


def _format_other_column(array, *, null_text, width):
    """Format a non-numeric curve for width checking only."""
    count = array.shape[0]
    rendered = np.empty(count, dtype=object)
    parsed = np.empty(count, dtype=object)
    overflow = []
    for row in range(count):
        value = array[row]
        text = null_text if _is_null_like(value) else str(value)
        rendered[row] = text
        parsed[row] = text
        if width != -1 and len(text) > width:
            overflow.append(row)
    return rendered, parsed, np.asarray(overflow, dtype=int)


def _is_null_like(value) -> bool:
    if value is None:
        return True
    try:
        return bool(np.isnan(value))
    except (TypeError, ValueError):
        return False


def _parse_float(rendered, *, fallback):
    try:
        return float(rendered)
    except (TypeError, ValueError):
        return fallback


def _identity_key(data_by_name, identity_names, row):
    key = []
    for name in identity_names:
        value = data_by_name[name][row]
        key.append(_canonical_value(value))
    return tuple(key)


def _formatted_identity_key(parsed_values, identity_names, row):
    return tuple(_canonical_value(parsed_values[name][row]) for name in identity_names)


def _canonical_value(value):
    if _is_null_like(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(number)
    if number == 0:
        number = 0.0
    return number


def _valid_identity_rows(data_by_name, identity_names, source_rows) -> List[int]:
    if not identity_names:
        return []
    rows = []
    for row in range(source_rows):
        valid = True
        for name in identity_names:
            value = data_by_name[name][row]
            if _is_null_like(value):
                valid = False
                break
        if valid:
            rows.append(row)
    return rows


def _as_evidence_value(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if math.isfinite(number):
        return number
    return str(number)


def _format_identity(identity):
    return "(" + ", ".join(repr(value) for value in identity) + ")"
