"""Reconciliation of ambiguous quote lines against a confirmed total.

A quoted number such as ``1.250`` is ambiguous on its own: depending on the
regional convention it may mean ``1.25`` (dot as a decimal separator) or
``1250`` (dot as a thousands separator). Different suppliers on the same
quote are free to use different conventions, so the separators can not be
guessed globally.

This module enumerates *every* plausible reading of every line, combines them
exhaustively and keeps the assignments whose rounded grand total matches the
confirmed total. A line reading is only reported as determined when it is the
same in *all* matching assignments - a single lucky match is never enough.

>>> from decimal import Decimal
>>> result = resolve_quote([(1, "1.250"), (2, "2,500")], Decimal("5001.25"))
>>> result.exact, result.is_unique
(True, True)
>>> result.determined_lines
(0, 1)
>>> result.solutions[0].readings[0].unit_price
Decimal('1.250')
>>> result.solutions[0].readings[1].unit_price
Decimal('2500')

When no assignment reproduces the confirmed total, the closest achievable
totals are returned instead:

>>> closest = resolve_quote([(1, "1.250")], Decimal("100"))
>>> closest.exact
False
>>> closest.achieved_totals
(Decimal('1.25'),)
"""

from __future__ import annotations

import itertools
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import TYPE_CHECKING

import attr

from .parser import extract_price_text

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "LineReading",
    "QuoteLine",
    "QuoteResolution",
    "QuoteSolution",
    "resolve_quote",
]

_NUMERIC_CHARS = set("0123456789.,")


@attr.s(auto_attribs=True, frozen=True)
class LineReading:
    """One plausible interpretation of a quoted unit price.

    ``unit_price`` is the interpreted price, ``line_total`` is
    ``quantity * unit_price`` rounded to the requested precision,
    ``decimal_separator`` is the symbol treated as a decimal mark (``None``
    when the reading is an integer) and ``thousands_separator`` is the symbol
    treated as a grouping mark (``None`` when no grouping is assumed).
    """

    unit_price: Decimal
    line_total: Decimal
    decimal_separator: str | None
    thousands_separator: str | None


@attr.s(auto_attribs=True, frozen=True)
class QuoteLine:
    """A quote line: a ``quantity`` and the raw quoted ``price_text``."""

    quantity: Decimal
    price_text: str
    readings: tuple[LineReading, ...] = attr.ib(eq=False, repr=False)


@attr.s(auto_attribs=True, frozen=True)
class QuoteSolution:
    """A full assignment of one reading per line and its rounded total."""

    readings: tuple[LineReading, ...]
    total: Decimal


@attr.s(auto_attribs=True, frozen=True)
class QuoteResolution:
    """Result of reconciling quote lines against a confirmed total."""

    target_total: Decimal
    precision: Decimal
    rounding: str
    exact: bool
    fixed_total: Decimal
    combinations: int
    solutions: tuple[QuoteSolution, ...]

    @property
    def is_unique(self) -> bool:
        """Return ``True`` when exactly one assignment matches the total."""
        return len(self.solutions) == 1

    @property
    def achieved_totals(self) -> tuple[Decimal, ...]:
        """Distinct totals reached by ``solutions``, sorted ascending.

        When :attr:`exact` is ``False`` these are the closest achievable
        totals; two different totals may be equally close to the target.
        """
        return tuple(sorted({solution.total for solution in self.solutions}))

    @property
    def determined_lines(self) -> tuple[int, ...]:
        """Indices of lines whose reading is fixed across every solution."""
        return tuple(
            index
            for index in self._line_indices()
            if len(self._unit_prices(index)) == 1
        )

    @property
    def ambiguous_lines(self) -> tuple[int, ...]:
        """Indices of lines with genuinely different readings in play."""
        return tuple(
            index for index in self._line_indices() if len(self._unit_prices(index)) > 1
        )

    def differing_readings(self, line_index: int) -> tuple[LineReading, ...]:
        """Distinct readings of a line across the returned solutions.

        Raises ``ValueError`` if the line is not ambiguous, so callers can
        rely on getting at least two genuinely different readings.
        """
        readings = {solution.readings[line_index] for solution in self.solutions}
        if len(readings) < 2:
            raise ValueError(f"line {line_index} is not ambiguous")
        return tuple(sorted(readings, key=lambda reading: reading.unit_price))

    def _line_indices(self) -> range:
        if not self.solutions:
            return range(0)
        return range(len(self.solutions[0].readings))

    def _unit_prices(self, line_index: int) -> set[Decimal]:
        return {solution.readings[line_index].unit_price for solution in self.solutions}


def resolve_quote(
    lines: Sequence[QuoteLine | tuple[int | str | Decimal, str]],
    total: int | str | Decimal,
    precision: int | Decimal = Decimal("0.01"),
    rounding: str = ROUND_HALF_UP,
) -> QuoteResolution:
    """Find the line readings forced by the confirmed quote total.

    ``lines`` is a sequence of ``(quantity, price_text)`` pairs (or
    :class:`QuoteLine` instances), ``total`` is the confirmed grand total.
    ``precision`` is either a :class:`~decimal.Decimal` quantum such as
    ``Decimal("0.01")`` or an integer number of decimal places. Every line
    total and the grand total are rounded using ``rounding``.

    All combinations of the plausible per-line readings are evaluated, so the
    result never relies on a single lucky match. If one or more combinations
    reproduce ``total`` they are all returned; otherwise the combinations
    closest to ``total`` are returned.

    >>> resolution = resolve_quote(
    ...     [(1, "1.250"), (1, "1.250")], Decimal("1251.25")
    ... )
    >>> resolution.exact
    True
    >>> resolution.is_unique
    False
    >>> resolution.ambiguous_lines
    (0, 1)
    >>> [reading.unit_price for reading in resolution.differing_readings(0)]
    [Decimal('1.250'), Decimal('1250')]
    """
    quantum = _as_quantum(precision)
    target = Decimal(total).quantize(quantum, rounding=rounding)
    quote_lines = [_build_line(line, quantum, rounding) for line in lines]
    if not quote_lines:
        raise ValueError("at least one quote line is required")

    fixed_total = Decimal(0).quantize(quantum)
    ambiguous_indices: list[int] = []
    for index, line in enumerate(quote_lines):
        if len(line.readings) == 1:
            fixed_total += line.readings[0].line_total
        else:
            ambiguous_indices.append(index)
    fixed_total = fixed_total.quantize(quantum, rounding=rounding)

    ambiguous_choices = [quote_lines[index].readings for index in ambiguous_indices]
    combinations = 1
    for choices in ambiguous_choices:
        combinations *= len(choices)

    exact: list[QuoteSolution] = []
    closest: list[QuoteSolution] = []
    closest_distance: Decimal | None = None
    seen: set[tuple[str, ...]] = set()

    for choice in itertools.product(*ambiguous_choices):
        assignment = [line.readings[0] for line in quote_lines]
        running_total = fixed_total
        for ambiguous_index, reading in zip(ambiguous_indices, choice):
            assignment[ambiguous_index] = reading
            running_total += reading.line_total
        running_total = running_total.quantize(quantum, rounding=rounding)
        key = tuple(str(reading.unit_price) for reading in assignment)
        if key in seen:
            continue
        seen.add(key)
        solution = QuoteSolution(tuple(assignment), running_total)
        if running_total == target:
            exact.append(solution)
            continue
        distance = abs(running_total - target)
        if closest_distance is None or distance < closest_distance:
            closest_distance = distance
            closest = [solution]
        elif distance == closest_distance:
            closest.append(solution)

    matched_solutions = exact or closest
    solutions = tuple(
        sorted(
            matched_solutions,
            key=lambda solution: tuple(
                str(reading.unit_price) for reading in solution.readings
            ),
        )
    )
    return QuoteResolution(
        target_total=target,
        precision=quantum,
        rounding=rounding,
        exact=bool(exact),
        fixed_total=fixed_total,
        combinations=combinations,
        solutions=solutions,
    )


def _as_quantum(precision: int | Decimal) -> Decimal:
    if isinstance(precision, int):
        if precision < 0:
            raise ValueError("precision must be non-negative")
        quantum = Decimal(10) ** -precision
    else:
        quantum = precision
    if quantum <= 0:
        raise ValueError("precision must be positive")
    return quantum


def _build_line(
    line: QuoteLine | tuple[int | str | Decimal, str],
    quantum: Decimal,
    rounding: str,
) -> QuoteLine:
    if isinstance(line, QuoteLine):
        quantity = line.quantity
        price_text = line.price_text
    else:
        raw_quantity, price_text = line
        quantity = Decimal(str(raw_quantity))
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    numeric_text = _numeric_text(price_text)
    readings = tuple(
        _build_reading(value, numeric_text, quantum, quantity, rounding)
        for value in _candidate_prices(numeric_text)
    )
    return QuoteLine(quantity=quantity, price_text=price_text, readings=readings)


def _build_reading(
    unit_price: Decimal,
    numeric_text: str,
    quantum: Decimal,
    quantity: Decimal,
    rounding: str,
) -> LineReading:
    decimal_separator, thousands_separator = _separators_for(numeric_text, unit_price)
    with localcontext() as context:
        context.prec = 50
        line_total = (quantity * unit_price).quantize(quantum, rounding=rounding)
    return LineReading(
        unit_price=unit_price,
        line_total=line_total,
        decimal_separator=decimal_separator,
        thousands_separator=thousands_separator,
    )


def _separators_for(
    numeric_text: str, unit_price: Decimal
) -> tuple[str | None, str | None]:
    has_dot = "." in numeric_text
    has_comma = "," in numeric_text
    if not has_dot and not has_comma:
        return None, None
    if has_dot and has_comma:
        if numeric_text.rfind(",") > numeric_text.rfind("."):
            return ",", "."
        return ".", ","
    separator = "." if has_dot else ","
    parts = numeric_text.split(separator)
    if len(parts) > 2:
        return None, separator
    head, tail = parts
    grouped_value = Decimal(numeric_text.replace(separator, ""))
    if head and len(tail) == 3 and grouped_value == unit_price:
        return None, separator
    return separator, None


def _numeric_text(price_text: str) -> str:
    cleaned = price_text.replace(" ", "").replace("'", "")
    if not cleaned:
        raise ValueError(f"empty price text: {price_text!r}")
    if any(char not in _NUMERIC_CHARS for char in cleaned):
        extracted = extract_price_text(price_text)
        if extracted is None:
            raise ValueError(f"unparseable price text: {price_text!r}")
        cleaned = extracted.replace(" ", "").replace("'", "")
    if any(char not in _NUMERIC_CHARS for char in cleaned) or not any(
        char.isdigit() for char in cleaned
    ):
        raise ValueError(f"unparseable price text: {price_text!r}")
    return cleaned


def _candidate_prices(numeric_text: str) -> tuple[Decimal, ...]:
    has_dot = "." in numeric_text
    has_comma = "," in numeric_text
    if not has_dot and not has_comma:
        return (Decimal(numeric_text),)
    if has_dot and has_comma:
        return (_mixed_separator_price(numeric_text),)

    separator = "." if has_dot else ","
    parts = numeric_text.split(separator)
    if len(parts) > 2:
        # The separator repeats, so it can only be a grouping mark.
        return (Decimal(numeric_text.replace(separator, "")),)
    head, tail = parts
    if not tail:
        # A trailing separator ("1250.") carries no fractional part.
        return (Decimal(head),)
    candidates = [Decimal(_decimal_form(numeric_text, separator))]
    if len(tail) == 3 and head:
        grouping_value = Decimal(numeric_text.replace(separator, ""))
        if grouping_value != candidates[0]:
            candidates.append(grouping_value)
    return tuple(candidates)


def _mixed_separator_price(numeric_text: str) -> Decimal:
    if numeric_text.rfind(",") > numeric_text.rfind("."):
        decimal_separator, grouping_separator = ",", "."
    else:
        decimal_separator, grouping_separator = ".", ","
    normalized = numeric_text.replace(grouping_separator, "").replace(
        decimal_separator, "."
    )
    return Decimal(normalized)


def _decimal_form(numeric_text: str, separator: str) -> str:
    if separator == ".":
        return numeric_text
    return numeric_text.replace(",", ".")
