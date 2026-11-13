"""Diagnostic validation that collects every independently confirmable bad
field in a single pass.

The regular :meth:`schema.Schema.validate` call fails fast: it raises on the
first problem.  The helpers in this module instead run the same validation once
and gather a *rejection slip*: one :class:`FieldDiagnostic` per field that can
be confirmed independently.

Design constraints respected here:

* Transformations (``Use`` and any other callable) are run exactly once.  Error
  collection never replays a value through a validator that may have consumed a
  one-shot input.
* Checks that never ran because a parent field already failed are reported as
  ``unknown`` rather than turned into fabricated child failures.
* Every conclusion carries the path of the original field and the message of
  the check that actually raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # avoid a runtime circular import with the schema package
    from . import SchemaError

FAILED = "failed"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class FieldDiagnostic:
    """A single conclusion about one field.

    :param path: Path from the validated entry to the field, e.g.
        ``("address", "zip")`` or ``("items", 0, "sku")``. An empty tuple
        points at the whole entry.
    :param status: :data:`FAILED` for a confirmed failure or :data:`UNKNOWN`
        for a check that could not be executed after an earlier failure.
    :param message: Human-readable message produced by the failing check.
    :param check: Best-effort description of the check that raised (the
        deepest auto-generated message), or the unevaluated check for
        ``unknown`` conclusions.
    :param autos: Auto-generated error messages attached to the conclusion.
    :param errors: Custom ``error=`` messages attached to the conclusion.
    """

    path: Tuple[Any, ...]
    status: str
    message: str
    check: Optional[str]
    autos: Tuple[str, ...] = ()
    errors: Tuple[Optional[str], ...] = ()
    # Identity of the SchemaError this conclusion was built from. Used
    # internally to undo recording when an Or snapshot is restored.
    error_id: int = -1

    @property
    def path_text(self) -> str:
        """Render :attr:`path` as a dotted/bracketed field reference."""
        text = ""
        for segment in self.path:
            if isinstance(segment, int):
                text += "[%d]" % segment
            elif text:
                text += ".%s" % segment
            else:
                text = str(segment)
        return text


@dataclass(frozen=True)
class ValidationReport:
    """Outcome of a diagnostic validation run."""

    valid: bool
    value: Any
    failures: Tuple[FieldDiagnostic, ...] = ()
    unknown: Tuple[FieldDiagnostic, ...] = ()

    @property
    def diagnostics(self) -> Tuple[FieldDiagnostic, ...]:
        """All conclusions, failures first followed by unknown checks."""
        return self.failures + self.unknown


class DiagnosticCollector:
    """Accumulates conclusions while validation runs once.

    The collector also offers snapshot/restore so :class:`schema.Or` can discard
    conclusions produced by alternatives that ended up not matching.
    """

    def __init__(self) -> None:
        self.failures: List[FieldDiagnostic] = []
        self.unknown: List[FieldDiagnostic] = []
        # Hold the exception objects themselves (not just id()), so short-lived
        # exceptions whose ids get reused cannot be mistaken for duplicates.
        self._seen_errors: Dict[int, SchemaError] = {}

    def snapshot(self) -> Tuple[int, int]:
        return len(self.failures), len(self.unknown)

    def restore(self, snapshot: Tuple[int, int]) -> None:
        failure_count, unknown_count = snapshot
        del self.failures[failure_count:]
        del self.unknown[unknown_count:]

    def adopt(
        self,
        failures: List["FieldDiagnostic"],
        unknown: List["FieldDiagnostic"],
    ) -> None:
        """Re-attach conclusions captured before a snapshot restore.

        The seen-error registry keeps holding the original exception objects
        for the whole run, so adopted conclusions stay deduplicated safely.
        """
        self.failures.extend(failures)
        self.unknown.extend(unknown)

    def record_failure(self, path: Tuple[Any, ...], error: Any) -> None:
        """Record a confirmed ``SchemaError`` against the field at ``path``.

        The same exception object is never recorded twice, so re-raising an
        error through nested container boundaries cannot create duplicates.
        """
        error_id = id(error)
        if error_id in self._seen_errors:
            return
        self._seen_errors[error_id] = error
        autos = tuple(message for message in getattr(error, "autos", []) if message)
        errors = tuple(getattr(error, "errors", ()))
        check = next((message for message in reversed(autos) if message), None)
        self.failures.append(
            FieldDiagnostic(
                path=tuple(path),
                status=FAILED,
                message=error.code,
                check=check,
                autos=autos,
                errors=errors,
                error_id=error_id,
            )
        )

    def record_unknown(self, path: Tuple[Any, ...], check: str, message: str) -> None:
        """Record a check that was never executed for the given field."""
        self.unknown.append(
            FieldDiagnostic(
                path=tuple(path),
                status=UNKNOWN,
                message=message,
                check=check,
            )
        )


def validate_with_report(schema: Any, data: Any) -> ValidationReport:
    """Validate ``data`` against ``schema`` once and return a full report.

    The function never raises :class:`schema.SchemaError`: every confirmed bad
    field ends up in :attr:`ValidationReport.failures`, while checks that could
    not run after an earlier failure end up in :attr:`ValidationReport.unknown`.

    On success the transformed value is identical to what
    ``schema.validate(data)`` would return.
    """
    from . import SchemaError

    collector = DiagnosticCollector()
    try:
        value = schema.validate(data, _diagnostics=collector, _path=())
    except SchemaError as error:
        if not collector.failures:
            # Defensive net for validators that raise without recording:
            # never return an invalid report without a conclusion.
            collector.record_failure((), error)
        return ValidationReport(
            valid=False,
            value=None,
            failures=tuple(collector.failures),
            unknown=tuple(collector.unknown),
        )
    return ValidationReport(
        valid=True,
        value=value,
        failures=tuple(collector.failures),
        unknown=tuple(collector.unknown),
    )
