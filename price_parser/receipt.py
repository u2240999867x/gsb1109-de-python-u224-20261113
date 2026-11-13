"""
Tax reconciliation for cash-register receipts.

A receipt line exposes a unit price, a quantity and a tax rate, and the
receipt prints a single total tax amount, but vendors rarely document *when*
the rounding to the smallest currency unit happens. Three conventions are
common in practice:

* **per item** -- the tax on every single physical item is rounded, then
  multiplied by the quantity;
* **per line** -- the tax on every printed line (unit price * quantity) is
  rounded once;
* **per tax bracket** -- all net amounts sharing a tax rate are summed first,
  and the tax is rounded only once for the whole bracket.

This module recomputes all three conventions directly from the original price
strings (never from pre-rounded floats), using :class:`~decimal.Decimal`, so
that they can be compared against the tax amount printed on the receipt.
Negative (return/refund) lines keep their sign and are rounded with the exact
same rounding convention as positive lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from itertools import combinations

from .parser import Price

#: Rounding modes accepted by :class:`RoundingPolicy`.
#:
#: ``half_up`` is the commercial rounding most receipts use (0.5 always goes
#: away from zero); ``half_even`` is banker's rounding. Both are sign
#: preserving, so a negative half is rounded away from / towards zero the same
#: way a positive half is.
ROUNDING_MODES = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
}

#: The three supported tax conventions, in a fixed order.
TAX_METHODS = ("per_item", "per_line", "per_bracket")


class RoundingError(ValueError):
    """Raised when a value cannot be rounded to the currency unit."""


@dataclass(frozen=True)
class RoundingPolicy:
    """How to round an exact tax amount to the smallest currency unit.

    ``unit`` is the smallest representable monetary amount, e.g. ``0.01`` for
    the euro, ``1`` for the yen or ``0.05`` for the Swiss franc. ``mode``
    selects the rounding rule applied at that unit.

    >>> policy = RoundingPolicy(Decimal("0.01"))
    >>> policy.round(Decimal("1.045"))
    Decimal('1.05')
    >>> policy.round(Decimal("-1.045"))
    Decimal('-1.05')
    >>> RoundingPolicy(Decimal("1")).round(Decimal("31.5"))
    Decimal('32')
    >>> RoundingPolicy(Decimal("0.05")).round(Decimal("1.03"))
    Decimal('1.05')
    """

    unit: Decimal = Decimal("0.01")
    mode: str = "half_up"

    def __post_init__(self) -> None:
        if self.unit <= 0:
            raise ValueError("currency unit must be positive")
        if self.mode not in ROUNDING_MODES:
            raise ValueError(
                f"unsupported rounding mode {self.mode!r}, "
                f"expected one of {sorted(ROUNDING_MODES)}"
            )

    def round(self, value: Decimal) -> Decimal:
        """Round ``value`` to a whole multiple of :attr:`unit`.

        Rounding is done on ``value / unit`` and multiplied back, which works
        for any unit, including 0.05 and 1, unlike a plain
        :meth:`Decimal.quantize` that only fixes the number of decimals.
        """
        rounding = ROUNDING_MODES[self.mode]
        with localcontext() as context:
            context.prec = max(context.prec, 60)
            scaled = value / self.unit
            if not scaled.is_finite():
                raise RoundingError(f"cannot round non-finite value {value}")
            return scaled.to_integral_value(rounding=rounding) * self.unit


#: Default policy: whole cents with commercial half-up rounding. A module-level
#: singleton avoids a mutable-looking function-call default (ruff B008).
DEFAULT_POLICY = RoundingPolicy()


def _signed_price(raw: str | None, decimal_separator: str | None = None) -> Decimal:
    """Parse a raw price string, keeping a leading minus or parentheses.

    The base :class:`~price_parser.Price` parser extracts the magnitude, so the
    sign of refund lines (``"-8,30 €"`` or ``"(8,30 €)"``) is restored here.

    >>> _signed_price("8,30 €")
    Decimal('8.30')
    >>> _signed_price("-8,30 €")
    Decimal('-8.30')
    >>> _signed_price("(8,30 €)")
    Decimal('-8.30')
    """
    if raw is None:
        raise ValueError("price string is required")
    text = raw.strip()
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    parsed = Price.fromstring(text, decimal_separator=decimal_separator)
    if parsed.amount is None:
        raise ValueError(f"could not parse a price from {raw!r}")
    return -parsed.amount if negative else parsed.amount


@dataclass(frozen=True)
class ReceiptLine:
    """One printed receipt line.

    Amounts are exact :class:`~decimal.Decimal` values parsed from the raw
    strings shown on the receipt. ``quantity`` may be fractional (weighted
    goods) and negative (returns); ``unit_price`` and ``tax_rate`` carry their
    own sign as printed.

    >>> line = ReceiptLine.from_strings("8,30 €", "3", "19 %")
    >>> line.unit_price, line.quantity, line.tax_rate
    (Decimal('8.30'), Decimal('3'), Decimal('0.19'))
    >>> line.net
    Decimal('24.90')
    >>> ReceiptLine.from_strings("(8,30 €)", "1", "19 %").net
    Decimal('-8.30')
    """

    unit_price: Decimal
    quantity: Decimal
    tax_rate: Decimal

    @classmethod
    def from_strings(
        cls,
        unit_price: str,
        quantity: str,
        tax_rate: str,
        decimal_separator: str | None = None,
    ) -> ReceiptLine:
        """Build a line from the raw unit-price, quantity and tax-rate text."""
        price = _signed_price(unit_price, decimal_separator)
        quantity_decimal = _signed_price(quantity, decimal_separator)
        rate_text = tax_rate.replace("%", " ")
        rate_parsed = Price.fromstring(rate_text, decimal_separator=decimal_separator)
        if rate_parsed.amount is None:
            raise ValueError(f"could not parse a tax rate from {tax_rate!r}")
        rate = (
            rate_parsed.amount / Decimal(100) if "%" in tax_rate else rate_parsed.amount
        )
        return cls(price, quantity_decimal, rate)

    @property
    def net(self) -> Decimal:
        """Net line amount: unit price * quantity (sign preserved)."""
        return self.unit_price * self.quantity

    @property
    def exact_tax(self) -> Decimal:
        """Exact, unrounded tax on this line (net * tax rate)."""
        return self.net * self.tax_rate

    @property
    def per_item_exact_tax(self) -> Decimal:
        """Exact tax for one single item (unit price * tax rate)."""
        return self.unit_price * self.tax_rate


def _round_sum(policy: RoundingPolicy, values: list[Decimal]) -> Decimal:
    return sum((policy.round(value) for value in values), Decimal(0))


def tax_per_item(lines: list[ReceiptLine], policy: RoundingPolicy) -> Decimal:
    """Tax rounded on every single item, then multiplied by the quantity.

    >>> policy = RoundingPolicy(Decimal("0.01"))
    >>> line = ReceiptLine.from_strings("8,30 €", "3", "19 %")
    >>> tax_per_item([line], policy)
    Decimal('4.74')
    """
    total = Decimal(0)
    for line in lines:
        item_tax = policy.round(line.per_item_exact_tax)
        total += item_tax * line.quantity
    return total


def tax_per_line(lines: list[ReceiptLine], policy: RoundingPolicy) -> Decimal:
    """Tax rounded once per printed line, then summed."""
    return _round_sum(policy, [line.exact_tax for line in lines])


def tax_per_bracket(lines: list[ReceiptLine], policy: RoundingPolicy) -> Decimal:
    """Net amounts grouped by tax rate, with tax rounded once per bracket."""
    net_by_rate: dict[Decimal, Decimal] = {}
    for line in lines:
        net_by_rate[line.tax_rate] = (
            net_by_rate.get(line.tax_rate, Decimal(0)) + line.net
        )
    return _round_sum(policy, [net * rate for rate, net in net_by_rate.items()])


_TAX_CALCULATORS = {
    "per_item": tax_per_item,
    "per_line": tax_per_line,
    "per_bracket": tax_per_bracket,
}


@dataclass(frozen=True)
class TaxReconciliation:
    """Outcome of comparing the printed tax with the three conventions."""

    printed_tax: Decimal
    policy: RoundingPolicy
    taxes: dict[str, Decimal]

    @property
    def matching_methods(self) -> list[str]:
        """Conventions whose recomputed tax equals the printed tax."""
        return [
            method for method in TAX_METHODS if self.taxes[method] == self.printed_tax
        ]

    @property
    def is_unambiguous(self) -> bool:
        """True if exactly one convention reproduces the printed tax."""
        return len(self.matching_methods) == 1

    @property
    def is_explained(self) -> bool:
        """True if at least one convention reproduces the printed tax."""
        return bool(self.matching_methods)


def reconcile_tax(
    lines: list[ReceiptLine],
    printed_tax: Decimal | str,
    policy: RoundingPolicy = DEFAULT_POLICY,
) -> TaxReconciliation:
    """Recompute all three conventions and compare them with the printed tax.

    ``printed_tax`` may be an exact :class:`~decimal.Decimal` or the raw tax
    string from the receipt (refund totals keep their sign).

    >>> lines = [
    ...     ReceiptLine.from_strings("8,30 €", "3", "19 %"),
    ...     ReceiptLine.from_strings("5,50 €", "1", "19 %"),
    ...     ReceiptLine.from_strings("12,70 €", "2", "19 %"),
    ... ]
    >>> result = reconcile_tax(lines, Decimal("10.60"))
    >>> result.taxes
    {'per_item': Decimal('10.61'), 'per_line': Decimal('10.61'), 'per_bracket': Decimal('10.60')}
    >>> result.matching_methods
    ['per_bracket']
    """
    if isinstance(printed_tax, str):
        printed = _signed_price(printed_tax)
    else:
        printed = printed_tax
    taxes = {
        method: calculator(lines, policy)
        for method, calculator in _TAX_CALCULATORS.items()
    }
    return TaxReconciliation(printed, policy, taxes)


def minimal_discriminator(
    lines: list[ReceiptLine],
    policy: RoundingPolicy = DEFAULT_POLICY,
) -> tuple[list[ReceiptLine], dict[str, Decimal]] | None:
    """Return the fewest lines whose three tax totals are not all equal.

    The search is exhaustive over line subsets, smallest first, so the first
    subset found is a minimal witness. It returns ``(subset, taxes)`` where
    ``taxes`` maps each convention to its total for that subset, or ``None``
    if no subset distinguishes the conventions (they are mathematically
    indistinguishable on this receipt).

    Use it together with :func:`reconcile_tax`: the printed tax for the same
    subset shows which of the still-possible conventions a single datum rules
    out, without forcing a choice when two conventions tie.
    """
    for size in range(1, len(lines) + 1):
        for subset in combinations(lines, size):
            subset_list = list(subset)
            taxes = {
                method: calculator(subset_list, policy)
                for method, calculator in _TAX_CALCULATORS.items()
            }
            if len(set(taxes.values())) > 1:
                return subset_list, taxes
    return None
