"""Reconcile the cleaning results of an :class:`Or` rule across supplier rows.

A quality platform may feed the same supplier row through several candidate
rules held inside an :class:`Or`. ``Or.validate`` only returns the first
candidate that passes, so a row that two rules both accept but clean
differently (for example one rule keeps degrees Celsius while another emits
degrees Fahrenheit) is silently normalised to whichever candidate is listed
first.

The helpers in this module evaluate *every* candidate against a row, keep each
distinct cleaned result together with the candidate rule(s) and validation
basis that produced it, and flag the row as conflicting when those results are
not equivalent.

Two rules whose schemas only differ in key order inside a mapping can never
raise a conflict on their own: mapping key order is semantically irrelevant,
so cleaned results are compared through an order-insensitive projection.

Each candidate is validated against its own deep copy of the row, so a
candidate whose :class:`Use` transformation mutates its input can neither
corrupt the original row nor make a later candidate fail. The untouched
original row is kept on the report and can be fed back into
:func:`reconcile_row` to replay it on its own.
"""

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import And, Or, Schema, SchemaError, Use

__all__ = [
    "CandidateOutcome",
    "ReconciledResult",
    "RowReconciliation",
    "reconcile_row",
    "reconcile_rows",
    "find_conflicts",
]


def _normalise(obj: Any) -> Tuple[Any, ...]:
    """Return a hashable, mapping-order-insensitive projection of ``obj``.

    The projection works both on cleaned results and on schema expressions.
    Dictionaries collapse to frozensets so two values that only differ by
    mapping key order compare equal; list order is preserved because it is
    semantically meaningful.
    """
    if isinstance(obj, dict):
        return (
            "dict",
            frozenset((key, _normalise(value)) for key, value in obj.items()),
        )
    if isinstance(obj, (list, tuple)):
        return (type(obj).__name__, tuple(_normalise(value) for value in obj))
    if isinstance(obj, (set, frozenset)):
        return ("set", frozenset(_normalise(value) for value in obj))
    # ``Or`` is a subclass of ``And``, so it must be checked first.
    if isinstance(obj, Or):
        return (
            "Or",
            obj.only_one,
            tuple(_normalise(arg) for arg in obj.args),
        )
    if isinstance(obj, And):
        return ("And", tuple(_normalise(arg) for arg in obj.args))
    if isinstance(obj, Use):
        return ("Use", obj._callable)
    if isinstance(obj, Schema):
        return ("Schema", _normalise(obj.schema))
    try:
        hash(obj)
    except TypeError:
        obj = repr(obj)
    return ("value", obj)


@dataclass
class CandidateOutcome:
    """What a single candidate rule did when replayed against one row."""

    index: int
    rule: Any
    basis: str
    passed: bool
    result: Any = None
    error: Optional[str] = None


@dataclass
class ReconciledResult:
    """One distinct cleaned result with every candidate that produced it."""

    result: Any
    outcomes: List[CandidateOutcome] = field(default_factory=list)


@dataclass
class RowReconciliation:
    """The full reconciliation report for one supplier row."""

    row: Any
    outcomes: List[CandidateOutcome]
    reconciled: List[ReconciledResult]

    @property
    def conflict(self) -> bool:
        """True when passing rules produced more than one distinct result."""
        return len(self.reconciled) > 1


def _candidate_rules(rule: Any) -> List[Any]:
    """Flatten an ``Or`` (possibly wrapped in a ``Schema``) into its rules."""
    if isinstance(rule, Or):
        return list(rule.args)
    if isinstance(rule, Schema) and isinstance(rule.schema, Or):
        return list(rule.schema.args)
    return [rule]


def _as_validator(candidate: Any) -> Any:
    return candidate if hasattr(candidate, "validate") else Schema(candidate)


def reconcile_row(rule: Any, row: Any) -> RowReconciliation:
    """Validate every candidate rule against ``row`` and reconcile results.

    The original ``row`` is never modified: each candidate receives its own
    deep copy, so an in-place transformation inside one candidate cannot leak
    into the next one. The returned report keeps the original row, allowing
    it to be replayed later by calling this function again with that row.
    """
    candidates = _candidate_rules(rule)
    outcomes: List[CandidateOutcome] = []
    groups: Dict[Tuple[Any, ...], ReconciledResult] = {}
    reconciled: List[ReconciledResult] = []

    for index, candidate in enumerate(candidates):
        basis = "candidate #%d of %r: %r" % (index, rule, candidate)
        sandbox = copy.deepcopy(row)
        try:
            result = _as_validator(candidate).validate(sandbox)
        except SchemaError as exc:
            outcomes.append(
                CandidateOutcome(index, candidate, basis, False, error=exc.code)
            )
            continue

        outcome = CandidateOutcome(index, candidate, basis, True, result=result)
        outcomes.append(outcome)

        key = _normalise(result)
        group = groups.get(key)
        if group is None:
            group = ReconciledResult(result=result)
            groups[key] = group
            reconciled.append(group)
        group.outcomes.append(outcome)

    return RowReconciliation(row=row, outcomes=outcomes, reconciled=reconciled)


def reconcile_rows(
    rule: Any, rows: Iterable[Any]
) -> List[RowReconciliation]:
    """Reconcile a batch of supplier rows, preserving row order."""
    return [reconcile_row(rule, row) for row in rows]


def find_conflicts(rule: Any, rows: Iterable[Any]) -> List[RowReconciliation]:
    """Return the reports of rows whose passing rules disagree on the result."""
    return [report for report in reconcile_rows(rule, rows) if report.conflict]
