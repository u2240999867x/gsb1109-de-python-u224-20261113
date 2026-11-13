"""Shelf-pack parsing and price-per-unit calculations.

The module separates three things which are easy to confuse in shelf data:

* the paid pack, e.g. ``12 x 250 mL``;
* free units, e.g. ``加赠 2 瓶``, whose size may be known, equal to the paid
  unit, bounded, or completely unknown;
* the minimum order quantity, e.g. ``两箱起购``, which is not the capacity of
  one box.

Unit prices are calculated for a target amount, for example one litre or
100 mL, and can include or exclude free units.

>>> pack = parse_pack("12×250 mL 加赠 2 瓶")
>>> pack.count, pack.size, pack.unit, pack.gift_count
(12, Decimal('250'), 'mL', 2)
>>> pack.min_order_boxes
1
>>> pack = parse_pack("两箱起购 12×250 mL")
>>> pack.count, pack.min_order_boxes
(12, 2)
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Optional, Union

import attr

from .parser import Price

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class QuantityError(ValueError):
    """Base class for quantity and unit-price errors."""


class UnknownGiftSizeError(QuantityError):
    """Raised when an exact gift quantity cannot be determined."""


class UnitMismatchError(QuantityError):
    """Raised when quantities use incompatible measurement units."""


class UnknownUnitError(QuantityError):
    """Raised when a quantity has no known or requested measurement unit."""


# Canonical unit -> (family, factor to the family's base unit).
_UNIT_TABLE: dict[str, tuple[str, Decimal]] = {
    "ml": ("volume", Decimal(1)),
    "milliliter": ("volume", Decimal(1)),
    "millilitre": ("volume", Decimal(1)),
    "milliliters": ("volume", Decimal(1)),
    "millilitres": ("volume", Decimal(1)),
    "毫升": ("volume", Decimal(1)),
    "cc": ("volume", Decimal(1)),
    "cl": ("volume", Decimal(10)),
    "厘升": ("volume", Decimal(10)),
    "dl": ("volume", Decimal(100)),
    "分升": ("volume", Decimal(100)),
    "l": ("volume", Decimal(1000)),
    "liter": ("volume", Decimal(1000)),
    "litre": ("volume", Decimal(1000)),
    "liters": ("volume", Decimal(1000)),
    "litres": ("volume", Decimal(1000)),
    "升": ("volume", Decimal(1000)),
    "g": ("mass", Decimal(1)),
    "gram": ("mass", Decimal(1)),
    "grams": ("mass", Decimal(1)),
    "克": ("mass", Decimal(1)),
    "mg": ("mass", Decimal("0.001")),
    "毫克": ("mass", Decimal("0.001")),
    "kg": ("mass", Decimal(1000)),
    "kilogram": ("mass", Decimal(1000)),
    "kilograms": ("mass", Decimal(1000)),
    "千克": ("mass", Decimal(1000)),
    "公斤": ("mass", Decimal(1000)),
}

_CANONICAL_VOLUME = {
    "ml": "mL",
    "milliliter": "mL",
    "millilitre": "mL",
    "milliliters": "mL",
    "millilitres": "mL",
    "毫升": "mL",
    "cc": "mL",
    "cl": "cL",
    "厘升": "cL",
    "dl": "dL",
    "分升": "dL",
    "l": "L",
    "liter": "L",
    "litre": "L",
    "liters": "L",
    "litres": "L",
    "升": "L",
}

_CANONICAL_MASS = {
    "g": "g",
    "gram": "g",
    "grams": "g",
    "克": "g",
    "mg": "mg",
    "毫克": "mg",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "千克": "kg",
    "公斤": "kg",
}

# Count units are physical selling units.  They are canonicalised so that
# "bottles" and "瓶" can be compared.
_COUNT_CANONICAL = {
    "瓶": "瓶",
    "支": "支",
    "罐": "罐",
    "听": "听",
    "盒": "盒",
    "包": "包",
    "袋": "袋",
    "个": "个",
    "片": "片",
    "粒": "粒",
    "桶": "桶",
    "卷": "卷",
    "双": "双",
    "套": "套",
    "板": "板",
    "排": "排",
    "件": "件",
    "只": "只",
    "条": "条",
    "块": "块",
    "箱": "箱",
    "瓶装": "瓶",
    "bottle": "瓶",
    "bottles": "瓶",
    "can": "罐",
    "cans": "罐",
    "pack": "包",
    "packs": "包",
    "piece": "件",
    "pieces": "件",
    "pcs": "件",
    "pc": "件",
    "unit": "件",
    "units": "件",
    "tablet": "片",
    "tablets": "片",
    "capsule": "粒",
    "capsules": "粒",
    "bag": "袋",
    "bags": "袋",
    "box": "盒",
    "boxes": "盒",
    "carton": "箱",
    "cartons": "箱",
    "case": "箱",
    "cases": "箱",
}

_CASE_UNITS = {"箱"}
_COUNT_UNITS = set(_COUNT_CANONICAL)
_UNIT_ALTS = sorted(
    list(_UNIT_TABLE) + list(_COUNT_UNITS),
    key=len,
    reverse=True,
)
_UNIT_PATTERN = "|".join(re.escape(unit) for unit in _UNIT_ALTS)
_COUNT_CONTAINER_PATTERN = "|".join(
    re.escape(unit) for unit in sorted(_COUNT_UNITS, key=len, reverse=True)
)
_UNIT_END = r"(?![A-Za-z])"
_NUMBER = r"\d+(?:\.\d+)?"
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_NUMBER = r"[零〇一二两三四五六七八九十百]+"
_QUANTITY_NUMBER = rf"(?:\d+(?:\.\d+)?|{_CN_NUMBER})"


def normalize_unit(unit: str | None) -> str | None:
    """Return a canonical unit symbol, or ``None`` if it is unknown.

    Count nouns are preserved as canonical count units.

    >>> normalize_unit("ml")
    'mL'
    >>> normalize_unit("毫升")
    'mL'
    >>> normalize_unit("公斤")
    'kg'
    >>> normalize_unit("bottles")
    '瓶'
    """
    if unit is None:
        return None
    cleaned = unicodedata.normalize("NFKC", unit).strip().strip(".").lower()
    if cleaned in _CANONICAL_VOLUME:
        return _CANONICAL_VOLUME[cleaned]
    if cleaned in _CANONICAL_MASS:
        return _CANONICAL_MASS[cleaned]
    if cleaned in _COUNT_CANONICAL:
        return _COUNT_CANONICAL[cleaned]
    return None


def unit_family(unit: str) -> str:
    """Return ``"volume"``, ``"mass"``, ``"count"`` or ``"unknown"``."""
    canonical = normalize_unit(unit)
    if canonical is None:
        return "unknown"
    lower = canonical.lower()
    if lower in _UNIT_TABLE:
        return _UNIT_TABLE[lower][0]
    return "count"


def _unit_factor(unit: str) -> Decimal:
    canonical = normalize_unit(unit)
    if canonical is None:
        raise UnknownUnitError(f"Unknown unit: {unit!r}")
    lower = canonical.lower()
    if lower not in _UNIT_TABLE:
        raise UnknownUnitError(f"{unit!r} is a count unit, not a measurement unit")
    return _UNIT_TABLE[lower][1]


def convert_amount(amount: Decimal, from_unit: str, to_unit: str) -> Decimal:
    """Convert a measurement between compatible units.

    >>> convert_amount(Decimal("1.5"), "L", "mL")
    Decimal('1500.0')
    >>> convert_amount(Decimal("250"), "mL", "L")
    Decimal('0.25')
    """
    from_canonical = normalize_unit(from_unit)
    to_canonical = normalize_unit(to_unit)
    if from_canonical is None:
        raise UnknownUnitError(f"Unknown unit: {from_unit!r}")
    if to_canonical is None:
        raise UnknownUnitError(f"Unknown unit: {to_unit!r}")
    from_family = unit_family(from_canonical)
    to_family = unit_family(to_canonical)
    if from_family != to_family:
        raise UnitMismatchError(
            f"Cannot convert {from_unit!r} to {to_unit!r}: different unit families"
        )
    if from_family == "count":
        if from_canonical != to_canonical:
            raise UnitMismatchError(
                f"Cannot convert count unit {from_unit!r} to {to_canonical!r}"
            )
        return amount
    return amount * _unit_factor(from_canonical) / _unit_factor(to_canonical)


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for char in ("×", "✕", "✖", "╳", "＊", "*"):
        text = text.replace(char, "x")
    for char in ("–", "—", "至", "~", "〜", "～"):
        text = text.replace(char, "-")
    return re.sub(r"\s+", " ", text).strip()


def _parse_int(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def _parse_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


@attr.s(auto_attribs=True, frozen=True)
class PackSize:
    """Contents of one paid pack, plus free units and order metadata.

    ``count`` is the number of paid units in one box and ``size`` is the size
    of each paid unit.  ``min_order_boxes`` is only an order multiple; it is
    never folded into ``count``.

    ``gift_count_bounds`` and ``gift_size_bounds`` describe uncertainty in
    the free units.  Bounds are inclusive and are expressed per free unit.
    """

    count: int
    size: Decimal | None = None
    unit: str | None = None
    container: str | None = None
    gift_count: int = 0
    gift_count_bounds: tuple[int, int] | None = None
    gift_size: Decimal | None = None
    gift_unit: str | None = None
    gift_container: str | None = None
    gift_size_bounds: tuple[Decimal, Decimal] | None = None
    gift_same_as_base: bool = False
    min_order_boxes: int = 1
    raw: str = attr.ib(default="", repr=False)

    # Common alternative names, kept as properties for compatibility.
    @property
    def pack_count(self) -> int:
        """Number of paid units in one pack."""
        return self.count

    @property
    def units_per_pack(self) -> int:
        """Number of paid units in one pack."""
        return self.count

    @property
    def unit_size(self) -> Decimal | None:
        """Size of one paid unit."""
        return self.size

    @property
    def pack_size(self) -> Decimal | None:
        """Size of one paid unit."""
        return self.size

    @property
    def gift_units(self) -> int:
        """Number of free units (the upper end if a count range was given)."""
        return self.gift_count

    @property
    def free_count(self) -> int:
        """Number of free units (the upper end if a count range was given)."""
        return self.gift_count

    @property
    def minimum_order_boxes(self) -> int:
        """Number of boxes that must be ordered at once."""
        return self.min_order_boxes

    @property
    def moq(self) -> int:
        """Minimum order quantity, in boxes."""
        return self.min_order_boxes

    @property
    def quantity_unit(self) -> str:
        """Natural unit for quantity calculations."""
        if self.unit is not None:
            return self.unit
        if self.container is not None:
            return self.container
        return "件"

    @property
    def gift_size_unit(self) -> str:
        """Unit in which gift sizes are expressed."""
        return self.gift_unit or self.unit or self.gift_container or self.container or "件"

    @property
    def total_size(self) -> Decimal | None:
        """Paid quantity in one box, gifts excluded."""
        if self.size is None:
            return None
        return self.size * self.count

    @property
    def paid_quantity(self) -> Decimal | None:
        """Paid quantity in one box, gifts excluded."""
        return self.total_size

    @property
    def order_size(self) -> Decimal | None:
        """Paid measurement in the minimum order, gifts excluded."""
        total = self.total_size
        if total is None:
            return None
        return total * self.min_order_boxes

    @property
    def assumed_gift_size(self) -> Decimal | None:
        """Exact per-free-unit size, if one is known without bounds."""
        if self.gift_count == 0:
            return Decimal(0)
        if self.gift_same_as_base:
            return self.size
        return self.gift_size

    def _count_range(self) -> tuple[int, int]:
        if self.gift_count == 0:
            return 0, 0
        if self.gift_count_bounds is not None:
            return self.gift_count_bounds
        return self.gift_count, self.gift_count

    def base_quantity(self, target_unit: str | None = None) -> Decimal:
        """Paid quantity delivered by one pack, in ``target_unit``."""
        unit = target_unit or self.quantity_unit
        canonical = normalize_unit(unit)
        if canonical is None:
            raise UnknownUnitError(f"Unknown target unit: {unit!r}")
        family = unit_family(canonical)
        if family == "count":
            if canonical in _CASE_UNITS:
                return Decimal(1)
            if (
                self.container is not None
                and canonical not in {"件", "个"}
                and canonical != self.container
            ):
                # A pack of boxes cannot be priced per bottle without an
                # explicit conversion.
                raise UnitMismatchError(
                    f"Cannot count {self.container!r} as {canonical!r}"
                )
            return Decimal(self.count)
        if self.size is None or self.unit is None:
            raise UnknownUnitError(
                "The pack has no measurement size; use a count unit instead"
            )
        total = self.total_size
        assert total is not None
        return convert_amount(total, self.unit, canonical)

    def _gift_size_range(
        self,
        gift_size: Decimal | None = None,
        gift_size_bounds: tuple[Decimal, Decimal] | None = None,
        assume_unknown: bool = False,
    ) -> tuple[Decimal, Decimal]:
        if self.gift_count == 0:
            return Decimal(0), Decimal(0)
        if gift_size is not None:
            if gift_size < 0:
                raise QuantityError("Gift size must be non-negative")
            return gift_size, gift_size
        exact = self.assumed_gift_size
        if exact is not None:
            return exact, exact
        bounds = gift_size_bounds or self.gift_size_bounds
        if bounds is not None:
            low, high = bounds
            if low < 0 or high < low:
                raise QuantityError(f"Invalid gift size bounds: {bounds!r}")
            return low, high
        if not assume_unknown:
            raise UnknownGiftSizeError(
                "Gift size is unknown; pass gift_size, gift_size_bounds, "
                "or set gift_same_as_base"
            )
        # Without an external bound, use the paid unit size as a practical
        # ceiling.  This is reported explicitly in UnitPriceRange.assumptions.
        if self.size is not None:
            return Decimal(0), self.size
        return Decimal(0), Decimal(1)

    def gift_quantity(
        self,
        target_unit: str | None = None,
        gift_size: Decimal | None = None,
        gift_size_bounds: tuple[Decimal, Decimal] | None = None,
    ) -> Decimal:
        """Free quantity delivered by one pack, in ``target_unit``.

        If a gift size is unknown, this uses the upper end of the declared
        bounds (or the documented worst-case assumption).
        """
        if self.gift_count == 0:
            return Decimal(0)
        unit = target_unit or self.quantity_unit
        canonical = normalize_unit(unit)
        if canonical is None:
            raise UnknownUnitError(f"Unknown target unit: {unit!r}")
        family = unit_family(canonical)
        if family == "count":
            if canonical in _CASE_UNITS:
                return Decimal(0)
            if (
                self.gift_container is not None
                and canonical not in {"件", "个"}
                and canonical != self.gift_container
            ):
                raise UnitMismatchError(
                    f"Cannot count {self.gift_container!r} as {canonical!r}"
                )
            _, high_count = self._count_range()
            return Decimal(high_count)
        low_size, high_size = self._gift_size_range(
            gift_size=gift_size,
            gift_size_bounds=gift_size_bounds,
        )
        per_gift = low_size if gift_size is not None else high_size
        _, high_count = self._count_range()
        gift_unit = self.gift_size_unit
        return convert_amount(per_gift * high_count, gift_unit, canonical)

    def deliverable_quantity(
        self,
        target_unit: str | None = None,
        include_gifts: bool = True,
        gift_size: Decimal | None = None,
        gift_size_bounds: tuple[Decimal, Decimal] | None = None,
    ) -> Decimal:
        """Quantity delivered by one paid pack, gifts optional."""
        quantity = self.base_quantity(target_unit)
        if include_gifts:
            quantity += self.gift_quantity(
                target_unit,
                gift_size=gift_size,
                gift_size_bounds=gift_size_bounds,
            )
        return quantity

    def quantity_range(
        self,
        target_unit: str | None = None,
        include_gifts: bool = True,
        gift_size: Decimal | None = None,
        gift_size_bounds: tuple[Decimal, Decimal] | None = None,
    ) -> tuple[Decimal, Decimal]:
        """Return ``(minimum_quantity, maximum_quantity)`` per pack.

        The range accounts for both gift count and gift size bounds.  A
        count-only target unit needs no gift size, because free units can be
        counted directly.
        """
        base = self.base_quantity(target_unit)
        if not include_gifts or self.gift_count == 0:
            return base, base
        unit = target_unit or self.quantity_unit
        canonical = normalize_unit(unit)
        if canonical is None:
            raise UnknownUnitError(f"Unknown target unit: {unit!r}")
        family = unit_family(canonical)
        count_min, count_max = self._count_range()
        if family == "count":
            if canonical in _CASE_UNITS:
                return base, base
            if (
                self.gift_container is not None
                and canonical not in {"件", "个"}
                and canonical != self.gift_container
            ):
                raise UnitMismatchError(
                    f"Cannot count {self.gift_container!r} as {canonical!r}"
                )
            return base + count_min, base + count_max
        low_size, high_size = self._gift_size_range(
            gift_size=gift_size,
            gift_size_bounds=gift_size_bounds,
            assume_unknown=True,
        )
        gift_unit = self.gift_size_unit
        low_gift = convert_amount(low_size * count_min, gift_unit, canonical)
        high_gift = convert_amount(high_size * count_max, gift_unit, canonical)
        return base + low_gift, base + high_gift

    def price_per_unit(
        self,
        price: Decimal,
        target_unit: str | None = None,
        target_amount: Decimal = Decimal(1),
        include_gifts: bool = True,
        gift_size: Decimal | None = None,
        gift_size_bounds: tuple[Decimal, Decimal] | None = None,
    ) -> Decimal:
        """Price for ``target_amount`` of ``target_unit``."""
        quantity = self.deliverable_quantity(
            target_unit,
            include_gifts=include_gifts,
            gift_size=gift_size,
            gift_size_bounds=gift_size_bounds,
        )
        if quantity <= 0:
            raise QuantityError("Deliverable quantity must be positive")
        return price * target_amount / quantity

    def with_gift_bounds(
        self, low: Decimal | int | str, high: Decimal | int | str
    ) -> PackSize:
        """Return a copy with explicit per-free-unit size bounds."""
        return attr.evolve(
            self,
            gift_size=None,
            gift_size_bounds=(Decimal(str(low)), Decimal(str(high))),
        )


@attr.s(auto_attribs=True, frozen=True)
class UnitPriceRange:
    """A unit-price interval caused by uncertain free-unit quantities.

    ``low`` is the lowest possible price per target amount (the largest
    deliverable quantity) and ``high`` is the highest possible price.
    """

    low: Decimal
    high: Decimal
    target_unit: str
    target_amount: Decimal = Decimal(1)
    quantity_low: Decimal = Decimal(0)
    quantity_high: Decimal = Decimal(0)
    gift_size_min: Decimal | None = None
    gift_size_max: Decimal | None = None
    gift_size_unit: str | None = None
    assumptions: tuple[str, ...] = ()

    @property
    def minimum(self) -> Decimal:
        """Lowest possible unit price."""
        return self.low

    @property
    def maximum(self) -> Decimal:
        """Highest possible unit price."""
        return self.high

    @property
    def min(self) -> Decimal:
        """Lowest possible unit price."""
        return self.low

    @property
    def max(self) -> Decimal:
        """Highest possible unit price."""
        return self.high

    @property
    def midpoint(self) -> Decimal:
        """Midpoint of the interval."""
        return (self.low + self.high) / 2

    @property
    def is_exact(self) -> bool:
        """Whether the interval collapses to one price."""
        return self.low == self.high

    @property
    def exact(self) -> bool:
        """Whether the interval collapses to one price."""
        return self.low == self.high

    def as_tuple(self) -> tuple[Decimal, Decimal]:
        """Return ``(low, high)``."""
        return self.low, self.high


@attr.s(auto_attribs=True, frozen=True)
class RankChange:
    """A gift quantity at which one quote catches another quote.

    ``gift_size`` is per free unit and ``gift_total`` is the total free
    measurement.  ``within_bounds`` is true when the crossover lies inside
    the declaring quote's gift bounds.
    """

    quote: str
    other: str
    gift_size: Decimal
    gift_total: Decimal
    gift_size_unit: str
    other_unit_price: Decimal
    within_bounds: bool
    message: str

    @property
    def total(self) -> Decimal:
        """Total gift quantity at the break-even point."""
        return self.gift_total


@attr.s(auto_attribs=True, frozen=True)
class QuoteComparison:
    """Comparison of quotes with optional gift-size uncertainty."""

    ranges: dict[str, UnitPriceRange]
    ordering: tuple[str, ...]
    certain: bool
    rank_changes: tuple[RankChange, ...] = ()
    winner: str | None = None

    @property
    def ranking(self) -> tuple[str, ...]:
        """Quotes ordered by midpoint unit price."""
        return self.ordering

    @property
    def changes(self) -> tuple[RankChange, ...]:
        """Break-even gift quantities."""
        return self.rank_changes

    @property
    def possible_winners(self) -> tuple[str, ...]:
        """Quotes that can be cheapest under some allowed gift size."""
        if not self.ranges:
            return ()
        if len(self.ranges) == 1:
            return tuple(self.ranges)
        return tuple(
            label
            for label, price_range in self.ranges.items()
            if price_range.low
            <= min(
                other.high
                for other_label, other in self.ranges.items()
                if other_label != label
            )
        )


@attr.s(auto_attribs=True, frozen=True)
class Quote:
    """A price attached to one shelf pack."""

    price: Price
    pack: PackSize
    label: str = ""

    @property
    def amount(self) -> Decimal | None:
        """Numeric price amount."""
        return self.price.amount

    @property
    def currency(self) -> str | None:
        """Currency symbol, as it appeared in the input."""
        return self.price.currency

    @classmethod
    def fromstring(
        cls,
        text: str,
        price: str | Decimal | Price | None = None,
        currency_hint: str | None = None,
        decimal_separator: str | None = None,
        label: str = "",
    ) -> Quote:
        """Parse a quote containing both a price and a shelf-pack description.

        >>> q = Quote.fromstring("¥59.90 12×250 mL 加赠 2 瓶")
        >>> q.amount, q.pack.count, q.pack.gift_count
        (Decimal('59.90'), 12, 2)
        """
        parsed_price: Price | None
        if isinstance(price, Price):
            parsed_price = price
        elif price is not None:
            parsed_price = Price.fromstring(
                str(price),
                currency_hint=currency_hint,
                decimal_separator=decimal_separator,
            )
        else:
            parsed_price = _extract_price(
                text,
                currency_hint=currency_hint,
                decimal_separator=decimal_separator,
            )
        return cls(
            price=parsed_price or Price(None, None, None),
            pack=parse_pack(text),
            label=label,
        )

    def unit_price(
        self,
        target_unit: str | None = None,
        target_amount: Decimal | int | str = Decimal(1),
        include_gifts: bool = True,
        gift_size: Decimal | None = None,
        gift_size_bounds: tuple[Decimal, Decimal] | None = None,
    ) -> Decimal:
        """Price per ``target_amount`` of ``target_unit``.

        >>> q = Quote.fromstring("¥59.90 12×250 mL", price="¥59.90")
        >>> q.unit_price("L", include_gifts=False)
        Decimal('19.96666666666666666666666667')
        """
        if self.amount is None:
            raise QuantityError("The quote has no price")
        amount, unit = _resolve_target(self.pack, target_unit, target_amount)
        return self.pack.price_per_unit(
            self.amount,
            target_unit=unit,
            target_amount=amount,
            include_gifts=include_gifts,
            gift_size=gift_size,
            gift_size_bounds=gift_size_bounds,
        )

    price_per_unit = unit_price

    def unit_price_range(
        self,
        target_unit: str | None = None,
        target_amount: Decimal | int | str = Decimal(1),
        include_gifts: bool = True,
        gift_size: Decimal | None = None,
        gift_size_bounds: tuple[Decimal, Decimal] | None = None,
    ) -> UnitPriceRange:
        """Return the unit-price interval implied by gift uncertainty."""
        if self.amount is None:
            raise QuantityError("The quote has no price")
        amount, unit = _resolve_target(self.pack, target_unit, target_amount)
        quantity_low, quantity_high = self.pack.quantity_range(
            target_unit=unit,
            include_gifts=include_gifts,
            gift_size=gift_size,
            gift_size_bounds=gift_size_bounds,
        )
        if quantity_low <= 0:
            raise QuantityError("Deliverable quantity must be positive")
        assumptions: list[str] = []
        gift_size_min: Decimal | None = None
        gift_size_max: Decimal | None = None
        target_family = unit_family(normalize_unit(unit) or unit)
        if include_gifts and self.pack.gift_count > 0:
            if gift_size is not None:
                gift_size_min = gift_size_max = gift_size
            elif self.pack.assumed_gift_size is not None:
                gift_size_min = gift_size_max = self.pack.assumed_gift_size
            elif target_family != "count":
                low_size, high_size = self.pack._gift_size_range(
                    gift_size_bounds=gift_size_bounds,
                    assume_unknown=True,
                )
                gift_size_min, gift_size_max = low_size, high_size
                if gift_size_bounds is not None:
                    assumptions.append("caller-supplied gift size bounds")
                elif self.pack.gift_size_bounds is None:
                    assumptions.append(
                        "gift size unknown; assumed between 0 and the paid unit size"
                    )
        if not include_gifts:
            assumptions.append("gifts excluded")
        low_price = self.amount * amount / quantity_high
        high_price = self.amount * amount / quantity_low
        return UnitPriceRange(
            low=low_price,
            high=high_price,
            target_unit=normalize_unit(unit) or unit,
            target_amount=amount,
            quantity_low=quantity_low,
            quantity_high=quantity_high,
            gift_size_min=gift_size_min,
            gift_size_max=gift_size_max,
            gift_size_unit=self.pack.gift_size_unit,
            assumptions=tuple(dict.fromkeys(assumptions)),
        )

    price_range = unit_price_range

    def gift_size_for_unit_price(
        self,
        target_price: Decimal | int | str,
        target_unit: str | None = None,
        target_amount: Decimal | int | str = Decimal(1),
        include_gifts: bool = True,
    ) -> Decimal | None:
        """Per-free-unit gift size needed to reach ``target_price``.

        Returns ``None`` when the quote has no free units.  The result is in
        :attr:`PackSize.gift_size_unit`.
        """
        if self.amount is None:
            raise QuantityError("The quote has no price")
        if not include_gifts or self.pack.gift_count == 0:
            return None
        amount, unit = _resolve_target(self.pack, target_unit, target_amount)
        target = Decimal(str(target_price))
        if target <= 0:
            raise QuantityError("Target unit price must be positive")
        base = self.pack.base_quantity(unit)
        required = self.amount * amount / target
        if required <= base:
            return Decimal(0)
        per_gift = (required - base) / self.pack.gift_count
        canonical = normalize_unit(unit)
        if canonical is None:
            raise UnknownUnitError(f"Unknown target unit: {unit!r}")
        if unit_family(canonical) == "count":
            return per_gift
        return convert_amount(per_gift, canonical, self.pack.gift_size_unit)

    def gift_total_for_unit_price(
        self,
        target_price: Decimal | int | str,
        target_unit: str | None = None,
        target_amount: Decimal | int | str = Decimal(1),
        include_gifts: bool = True,
    ) -> Decimal | None:
        """Total free quantity needed to reach ``target_price``."""
        per_gift = self.gift_size_for_unit_price(
            target_price,
            target_unit=target_unit,
            target_amount=target_amount,
            include_gifts=include_gifts,
        )
        if per_gift is None:
            return None
        return per_gift * self.pack.gift_count

    def break_even_gift_size(
        self,
        other: Quote,
        target_unit: str | None = None,
        target_amount: Decimal | int | str = Decimal(1),
        include_gifts: bool = True,
        other_gift_size: Decimal | None = None,
        other_include_gifts: bool = True,
    ) -> Decimal | None:
        """Gift size at which this quote matches ``other``'s unit price."""
        other_price = other.unit_price(
            target_unit=target_unit,
            target_amount=target_amount,
            include_gifts=other_include_gifts,
            gift_size=other_gift_size,
        )
        return self.gift_size_for_unit_price(
            other_price,
            target_unit=target_unit,
            target_amount=target_amount,
            include_gifts=include_gifts,
        )


def _resolve_target(
    pack: PackSize,
    target_unit: str | None,
    target_amount: Decimal | int | str,
) -> tuple[Decimal, str]:
    """Resolve ``target_unit`` and allow forms such as ``"100mL"``."""
    amount = Decimal(str(target_amount))
    if target_unit is None:
        return amount, pack.quantity_unit
    cleaned = re.sub(r"^(?:每|per\s+)", "", target_unit.strip(), flags=re.IGNORECASE)
    target_unit = cleaned
    match = re.fullmatch(
        rf"\s*(?P<amount>{_NUMBER})?\s*(?P<unit>{_UNIT_PATTERN})\s*",
        unicodedata.normalize("NFKC", target_unit),
        re.IGNORECASE,
    )
    if match and match.group("amount"):
        return Decimal(match.group("amount")), normalize_unit(match.group("unit")) or match.group(
            "unit"
        )
    canonical = normalize_unit(target_unit)
    if canonical is None:
        raise UnknownUnitError(f"Unknown target unit: {target_unit!r}")
    return amount, canonical


def _extract_price(
    text: str,
    currency_hint: str | None = None,
    decimal_separator: str | None = None,
) -> Price | None:
    normalized = _normalize_text(text)
    patterns = [
        rf"(?:[¥￥$€£]|US\$|RMB|CNY|USD|EUR)\s*(?P<amount>{_NUMBER})",
        rf"(?P<amount>{_NUMBER})\s*(?:元|块|円|원|€|£|\$|USD|EUR|RMB|CNY)",
        rf"(?:价格|售价|单价|到手价|现价|price|only)\s*[:：]?\s*(?P<amount>{_NUMBER})",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            parsed = Price.fromstring(
                match.group(0),
                currency_hint=currency_hint,
                decimal_separator=decimal_separator,
            )
            if parsed.amount is not None:
                return parsed
    # Accept a bare amount outside the pack expression, e.g.
    # "59.90 12x250 mL".  Numbers inside the pack must never become the price.
    specs = _find_specs(normalized)
    occupied = [(spec.start, spec.end) for spec in specs]
    for match in re.finditer(_NUMBER, normalized):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        parsed = Price.fromstring(
            match.group(0),
            currency_hint=currency_hint,
            decimal_separator=decimal_separator,
        )
        if parsed.amount is not None:
            return parsed
    return Price(None, None, None)


_MOQ_PATTERNS = [
    rf"(?P<n>\d+|{_CN_NUMBER})\s*(?:箱|盒|件|包|提|组)\s*"
    rf"(?:起购|起订|起批|起售|起送|起|以上)",
    rf"(?:起购|起订|起批|起售|起送|最低(?:购买)?|至少)\s*"
    rf"(?P<n>\d+|{_CN_NUMBER})\s*(?:箱|盒|件|包|提|组)",
    r"(?P<n>\d+)\s*(?:boxes|cartons|cases|packs)\s*"
    r"(?:minimum|min\.?|or more|at least)",
    r"(?:minimum(?:\s+order)?|min\.?\s*order|at least)\s*"
    r"(?P<n>\d+)\s*(?:boxes|cartons|cases|packs)",
]
_MOQ_REGEXES = [re.compile(pattern, re.IGNORECASE) for pattern in _MOQ_PATTERNS]


def _extract_moq(text: str) -> tuple[int, str]:
    match = next(
        (candidate for regex in _MOQ_REGEXES for candidate in [regex.search(text)] if candidate),
        None,
    )
    if not match:
        return 1, text
    value = _parse_int(match.group("n"))
    if not value:
        return 1, text
    return value, (text[: match.start()] + " " + text[match.end() :]).strip()


@attr.s(auto_attribs=True)
class _Spec:
    count: int = 1
    size: Decimal | None = None
    unit: str | None = None
    container: str | None = None
    start: int = 0
    end: int = 0


def _find_specs(text: str) -> list[_Spec]:
    """Find all quantity expressions in ``text``, longest forms first."""
    specs: list[_Spec] = []
    occupied: list[tuple[int, int]] = []

    def add(
        match: re.Match[str],
        count: int | None = None,
        size: Decimal | None = None,
        unit: str | None = None,
        container: str | None = None,
    ) -> None:
        if any(not (match.end() <= a or match.start() >= b) for a, b in occupied):
            return
        occupied.append((match.start(), match.end()))
        specs.append(
            _Spec(
                count=count or 1,
                size=size,
                unit=unit,
                container=container,
                start=match.start(),
                end=match.end(),
            )
        )

    number = _NUMBER
    unit = _UNIT_PATTERN
    container = _COUNT_CONTAINER_PATTERN
    patterns: list[str] = [
        # 12 x 250 mL, 12瓶×250mL
        rf"(?P<count>\d+)\s*(?:(?P<container>{container})\s*)?x\s*"
        rf"(?P<size>{number})\s*(?P<unit>{unit}){_UNIT_END}",
        # 250 mL x 12, 250mL×12瓶
        rf"(?P<size>{number})\s*(?P<unit>{unit}){_UNIT_END}\s*x\s*"
        rf"(?P<count>\d+)\s*(?P<container>{container})?",
        # 12 bottles 250 mL, 12瓶装250毫升
        rf"(?P<count>\d+)\s*(?P<container>{container})装?\s*"
        rf"(?P<size>{number})\s*(?P<unit>{unit}){_UNIT_END}",
        # 250 mL 12 bottles
        rf"(?P<size>{number})\s*(?P<unit>{unit}){_UNIT_END}\s*"
        rf"(?P<count>\d+)\s*(?P<container>{container})",
        # 12 / 250 mL
        rf"(?P<count>\d+)\s*/\s*(?P<size>{number})\s*(?P<unit>{unit}){_UNIT_END}",
        # 12 bottles
        rf"(?P<count>\d+)\s*(?P<container>{container}){_UNIT_END}",
        # A single 250 mL pack.
        rf"(?P<size>{number})\s*(?P<unit>{unit}){_UNIT_END}",
    ]

    # Try more specific patterns first, and prefer earlier matches when two
    # expressions have the same specificity.
    candidates = [
        (priority, match)
        for priority, pattern in enumerate(patterns)
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ]
    for _, match in candidates:
        groups = match.groupdict()
        add(
            match,
            count=_parse_int(groups["count"]) if groups.get("count") else None,
            size=_parse_decimal(groups["size"]) if groups.get("size") else None,
            unit=normalize_unit(groups["unit"]) if groups.get("unit") else None,
            container=(
                normalize_unit(groups["container"]) if groups.get("container") else None
            ),
        )

    return sorted(specs, key=lambda spec: spec.start)


_GIFT_MARKERS = (
    "加赠",
    "附赠",
    "赠送",
    "赠品",
    "免费送",
    "买一赠",
    "赠",
    "送",
    "free",
    "gift",
    "bonus",
    "plus",
    "+",
)
_GIFT_MARKER_RE = re.compile(
    "|".join(re.escape(marker) for marker in sorted(_GIFT_MARKERS, key=len, reverse=True)),
    re.IGNORECASE,
)
_GIFT_STOP = re.compile(r"[,，;；。\[\]【】/|+]")


_MEASURE_UNITS = tuple(_UNIT_TABLE)
_MEASURE_PATTERN = "|".join(
    re.escape(unit) for unit in sorted(_MEASURE_UNITS, key=len, reverse=True)
)


@attr.s(auto_attribs=True, frozen=True)
class _Gift:
    count: int = 0
    count_bounds: tuple[int, int] | None = None
    size: Decimal | None = None
    unit: str | None = None
    container: str | None = None
    size_bounds: tuple[Decimal, Decimal] | None = None
    same_as_base: bool = False


def _parse_gift_clause(clause: str) -> _Gift:
    """Parse the quantity expression in a gift clause."""
    text = _normalize_text(clause)
    number = r"\d+(?:\.\d+)?"
    unit = _UNIT_PATTERN
    measure = _MEASURE_PATTERN
    container = _COUNT_CONTAINER_PATTERN

    gift_count: int | None = None
    count_bounds: tuple[int, int] | None = None
    gift_size: Decimal | None = None
    gift_unit: str | None = None
    gift_container: str | None = None
    size_bounds: tuple[Decimal, Decimal] | None = None
    same_as_base = bool(re.search(r"同款|相同|同规格|same", text, re.IGNORECASE))

    # A size range describes the size of each free unit.
    bounds_match = re.search(
        rf"(?P<low>{number})\s*-\s*(?P<high>{number})\s*"
        rf"(?P<unit>{measure}){_UNIT_END}",
        text,
        re.IGNORECASE,
    )
    if bounds_match:
        low = _parse_decimal(bounds_match.group("low"))
        high = _parse_decimal(bounds_match.group("high"))
        if low is not None and high is not None and low <= high:
            size_bounds = (low, high)
            gift_unit = normalize_unit(bounds_match.group("unit"))

    # A count range describes how many free units there are.
    count_range = re.search(
        rf"(?P<low>\d+)\s*-\s*(?P<high>\d+)\s*(?P<container>{container})",
        text,
        re.IGNORECASE,
    )
    if count_range:
        low_count = int(count_range.group("low"))
        high_count = int(count_range.group("high"))
        if high_count >= low_count > 0:
            gift_count = high_count
            count_bounds = (low_count, high_count)
            gift_container = normalize_unit(count_range.group("container"))

    # count x size, e.g. "2×100 mL".
    match = re.search(
        rf"(?P<count>\d+)\s*x\s*(?P<size>{number})\s*(?P<unit>{unit}){_UNIT_END}",
        text,
        re.IGNORECASE,
    )
    if not match:
        # size x count, e.g. "100 mL × 2".
        match = re.search(
            rf"(?P<size>{number})\s*(?P<unit>{unit}){_UNIT_END}\s*x\s*(?P<count>\d+)",
            text,
            re.IGNORECASE,
        )
    if match:
        gift_count = int(match.group("count"))
        size = _parse_decimal(match.group("size"))
        if size is not None:
            gift_size = size
        gift_unit = normalize_unit(match.group("unit"))

    if gift_count is None:
        # count plus container, e.g. "2 瓶".
        match = re.search(
            rf"(?P<count>\d+)\s*(?P<container>{container}){_UNIT_END}",
            text,
            re.IGNORECASE,
        )
        if match:
            gift_count = int(match.group("count"))
            gift_container = normalize_unit(match.group("container"))

    # A size with no count means one free unit, e.g. "赠 100 mL".
    if gift_size is None:
        match = re.search(
            rf"(?P<size>{number})\s*(?P<unit>{measure}){_UNIT_END}",
            text,
            re.IGNORECASE,
        )
        if match:
            size = _parse_decimal(match.group("size"))
            if size is not None:
                if gift_count is None:
                    gift_count = 1
                gift_size = size
                if gift_unit is None:
                    gift_unit = normalize_unit(match.group("unit"))

    if gift_count is None:
        # A bare count, e.g. "赠 2", implies the same kind of unit as the pack.
        match = re.search(r"(?P<count>\d+)", text)
        if match:
            gift_count = int(match.group("count"))

    if same_as_base:
        gift_size = None
        size_bounds = None
    return _Gift(
        count=gift_count or 0,
        count_bounds=count_bounds,
        size=gift_size,
        unit=gift_unit,
        container=gift_container,
        size_bounds=size_bounds,
        same_as_base=same_as_base,
    )


def _extract_gift(text: str) -> tuple[_Gift, str]:
    """Split the first gift clause from the base pack description."""
    for marker_match in _GIFT_MARKER_RE.finditer(text):
        tail = text[marker_match.end() :]
        stop = _GIFT_STOP.search(tail)
        clause_length = stop.start() if stop else len(tail)
        clause = tail[: min(clause_length, 48)]
        if not re.search(r"\d|同款|相同|同规格|same", clause, re.IGNORECASE):
            continue
        gift = _parse_gift_clause(clause)
        if not gift.count:
            continue
        removal_end = marker_match.end() + clause_length
        same_match = re.search(
            r"同款|相同|同规格|same",
            text[marker_match.end() : min(len(text), marker_match.end() + 32)],
            re.IGNORECASE,
        )
        if same_match:
            gift = attr.evolve(
                gift,
                same_as_base=True,
                size=None,
                size_bounds=None,
            )
            removal_end = max(removal_end, marker_match.end() + same_match.end())
        cleaned = (text[: marker_match.start()] + " " + text[removal_end:]).strip()
        return gift, cleaned
    return _Gift(), text

def parse_pack(text: str) -> PackSize:
    """Parse a shelf-pack description.

    >>> p = parse_pack("12×250 mL 加赠 2 瓶")
    >>> p.count, p.size, p.unit, p.gift_count, p.gift_size
    (12, Decimal('250'), 'mL', 2, None)
    >>> p = parse_pack("两箱起购 12×250mL 赠2×100mL")
    >>> p.count, p.min_order_boxes, p.gift_count, p.gift_size
    (12, 2, 2, Decimal('100'))
    """
    if not text:
        raise QuantityError("Empty pack description")
    normalized = _normalize_text(text)
    moq, without_moq = _extract_moq(normalized)
    gift, base_text = _extract_gift(without_moq)
    specs = _find_specs(base_text)
    if not specs:
        raise QuantityError(f"Could not parse a paid pack from {text!r}")
    base = specs[0]
    if base.size is not None and base.unit is None:
        raise QuantityError(f"Could not determine the unit in {text!r}")
    if base.size is None and base.container is None:
        raise QuantityError(f"Could not determine what one pack contains in {text!r}")

    return PackSize(
        count=base.count,
        size=base.size,
        unit=base.unit,
        container=base.container,
        gift_count=gift.count,
        gift_count_bounds=gift.count_bounds,
        gift_size=gift.size,
        gift_unit=gift.unit,
        gift_container=gift.container,
        gift_size_bounds=gift.size_bounds,
        gift_same_as_base=gift.same_as_base,
        min_order_boxes=moq,
        raw=text,
    )


parse_pack_size = parse_pack
parse_quantity = parse_pack
parse_shelf_pack = parse_pack


def parse_quote(
    text: str,
    price: str | Decimal | Price | None = None,
    currency_hint: str | None = None,
    decimal_separator: str | None = None,
    label: str = "",
) -> Quote:
    """Parse a complete quote, e.g. ``"¥59.9 12×250 mL 加赠 2 瓶"``."""
    # Be forgiving about argument order: a caller may pass the price text
    # first and the pack description second.
    if (
        price is not None
        and isinstance(price, str)
        and _looks_like_pack(price)
        and not _looks_like_pack(text)
    ):
        text, price = price, text
    return Quote.fromstring(
        text,
        price=price,
        currency_hint=currency_hint,
        decimal_separator=decimal_separator,
        label=label,
    )


parse_shelf_quote = parse_quote
parse_price_quote = parse_quote


def _looks_like_pack(text: str) -> bool:
    return bool(re.search(r"\d+\s*(?:x|×|瓶|包|盒|袋|罐|L|mL|升|毫升)", text, re.IGNORECASE))


def _as_quote(value: Quote | str) -> Quote:
    return parse_quote(value) if isinstance(value, str) else value


Bounds = tuple[Decimal, Decimal]
BoundsArg = Optional[Union[Bounds, dict[str, Bounds]]]


def _as_bounds(bounds: BoundsArg, label: str) -> Bounds | None:
    if bounds is None:
        return None
    if isinstance(bounds, dict):
        return bounds.get(label)
    return bounds


def compare_quotes(
    quotes: Sequence[Quote | str],
    target_unit: str | None = None,
    target_amount: Decimal | int | str = Decimal(1),
    include_gifts: bool = True,
    gift_size_bounds: BoundsArg = None,
    labels: Iterable[str] | None = None,
) -> QuoteComparison:
    """Compare quotes and find gift quantities that can change the ranking.

    Each quote becomes a unit-price interval.  A crossover is reported for
    every pair of quotes whose intervals overlap, with ``within_bounds``
    indicating that the required gift size lies inside the declaring quote's
    bounds.
    """
    parsed = [_as_quote(quote) for quote in quotes]
    if not parsed:
        raise QuantityError("At least one quote is required")
    label_list = list(labels) if labels is not None else []
    resolved: list[Quote] = []
    for index, quote in enumerate(parsed):
        label = quote.label or (
            label_list[index] if index < len(label_list) else f"Q{index + 1}"
        )
        resolved.append(attr.evolve(quote, label=label))
    resolved_labels = [quote.label for quote in resolved]
    if len(set(resolved_labels)) != len(resolved_labels):
        raise QuantityError("Quote labels must be unique")

    ranges: dict[str, UnitPriceRange] = {}
    for quote in resolved:
        bounds = _as_bounds(gift_size_bounds, quote.label)
        ranges[quote.label] = quote.unit_price_range(
            target_unit=target_unit,
            target_amount=target_amount,
            include_gifts=include_gifts,
            gift_size_bounds=bounds,
        )

    ordering = tuple(
        quote.label
        for quote in sorted(resolved, key=lambda item: ranges[item.label].midpoint)
    )
    certain = False
    if len(ordering) == 1:
        certain = True
    else:
        first = ranges[ordering[0]]
        second = ranges[ordering[1]]
        certain = first.high < second.low

    changes: list[RankChange] = []
    for quote in resolved:
        own = ranges[quote.label]
        if own.is_exact:
            continue
        for other in resolved:
            if quote.label == other.label:
                continue
            other_range = ranges[other.label]
            for reference in sorted({other_range.low, other_range.high}):
                gift_size = quote.gift_size_for_unit_price(
                    reference,
                    target_unit=target_unit,
                    target_amount=target_amount,
                    include_gifts=include_gifts,
                )
                if gift_size is None or gift_size <= 0:
                    continue
                within = False
                if own.gift_size_min is not None and own.gift_size_max is not None:
                    within = own.gift_size_min <= gift_size <= own.gift_size_max
                if any(
                    change.quote == quote.label
                    and change.other == other.label
                    and change.gift_size == gift_size
                    for change in changes
                ):
                    continue
                changes.append(
                    RankChange(
                        quote=quote.label,
                        other=other.label,
                        gift_size=gift_size,
                        gift_total=gift_size * quote.pack.gift_count,
                        gift_size_unit=quote.pack.gift_size_unit,
                        other_unit_price=reference,
                        within_bounds=within,
                        message=(
                            f"{quote.label} matches {other.label} at "
                            f"{gift_size} {quote.pack.gift_size_unit} per free unit "
                            f"({gift_size * quote.pack.gift_count} "
                            f"{quote.pack.gift_size_unit} total)"
                        ),
                    )
                )

    changes.sort(key=lambda change: (not change.within_bounds, change.gift_size))
    winner = ordering[0] if certain else None
    return QuoteComparison(
        ranges=ranges,
        ordering=ordering,
        certain=certain,
        rank_changes=tuple(changes),
        winner=winner,
    )


compare_unit_prices = compare_quotes
analyze_quotes = compare_quotes
rank_quotes = compare_quotes


def _quote_from_parts(
    price: Decimal | str | Quote,
    pack: PackSize | str | None,
) -> Quote:
    if isinstance(price, Quote):
        return price
    if pack is None and isinstance(price, str):
        return parse_quote(price)
    parsed_price: Price
    if isinstance(price, str):
        parsed_price = Price.fromstring(price)
    else:
        parsed_price = Price(Decimal(str(price)), None, str(price))
    parsed_pack = parse_pack(pack) if isinstance(pack, str) else pack
    if parsed_pack is None:
        raise QuantityError("A pack description is required")
    return Quote(parsed_price, parsed_pack)


def unit_price(
    price: Decimal | str | Quote,
    pack: PackSize | str | None = None,
    target_unit: str | None = None,
    target_amount: Decimal | int | str = Decimal(1),
    include_gifts: bool = True,
    gift_size: Decimal | None = None,
    gift_size_bounds: Bounds | None = None,
) -> Decimal:
    """Convenience wrapper for a one-off unit-price calculation.

    >>> unit_price("¥59.90", "12×250 mL", "L", include_gifts=False)
    Decimal('19.96666666666666666666666667')
    """
    quote = _quote_from_parts(price, pack)
    return quote.unit_price(
        target_unit=target_unit,
        target_amount=target_amount,
        include_gifts=include_gifts,
        gift_size=gift_size,
        gift_size_bounds=gift_size_bounds,
    )


price_per_unit = unit_price
calculate_unit_price = unit_price


def unit_price_range(
    price: Decimal | str | Quote,
    pack: PackSize | str | None = None,
    target_unit: str | None = None,
    target_amount: Decimal | int | str = Decimal(1),
    include_gifts: bool = True,
    gift_size: Decimal | None = None,
    gift_size_bounds: Bounds | None = None,
) -> UnitPriceRange:
    """Convenience wrapper returning a unit-price interval."""
    quote = _quote_from_parts(price, pack)
    return quote.unit_price_range(
        target_unit=target_unit,
        target_amount=target_amount,
        include_gifts=include_gifts,
        gift_size=gift_size,
        gift_size_bounds=gift_size_bounds,
    )


price_range = unit_price_range


def break_even_gift_size(
    quote: Quote | str,
    other: Quote | str,
    target_unit: str | None = None,
    target_amount: Decimal | int | str = Decimal(1),
) -> Decimal | None:
    """Per-free-unit gift size at which ``quote`` matches ``other``."""
    return _as_quote(quote).break_even_gift_size(
        _as_quote(other),
        target_unit=target_unit,
        target_amount=target_amount,
    )


rank_break_even = break_even_gift_size


# Friendly aliases used by callers coming from different naming conventions.
Pack = PackSize
Quantity = PackSize
ShelfQuote = Quote
ShelfPack = PackSize
QuoteRange = UnitPriceRange
GiftSizeRange = UnitPriceRange
Comparison = QuoteComparison

__all__ = [
    "Bounds",
    "Comparison",
    "GiftSizeRange",
    "Pack",
    "PackSize",
    "Quantity",
    "QuantityError",
    "Quote",
    "QuoteComparison",
    "QuoteRange",
    "RankChange",
    "ShelfPack",
    "ShelfQuote",
    "UnitMismatchError",
    "UnitPriceRange",
    "UnknownGiftSizeError",
    "UnknownUnitError",
    "analyze_quotes",
    "break_even_gift_size",
    "calculate_unit_price",
    "compare_quotes",
    "compare_unit_prices",
    "convert_amount",
    "normalize_unit",
    "parse_pack",
    "parse_pack_size",
    "parse_price_quote",
    "parse_quantity",
    "parse_quote",
    "parse_shelf_pack",
    "parse_shelf_quote",
    "price_per_unit",
    "price_range",
    "rank_break_even",
    "rank_quotes",
    "unit_family",
    "unit_price",
    "unit_price_range",
]
