"""Build comparable price histories from scraped price tags.

A merchant can keep a product id stable while changing the net content
(500 g -> 450 g) or switching the displayed price between tax-inclusive
and tax-exclusive.  This module normalizes every scraped price tag to a
net-of-tax unit price interval and only then looks for changes, so such
presentation changes are not mistaken for price changes.

When the tax rate or the net content is only known as a range, the
normalized price is an interval as well, and a change is reported as
``Verdict.DEFINITE_INCREASE`` only if every value of the new interval is
above every value of the old one.

Tags captured on the same day are never ordered by scrape sequence: a
date boundary compares *all* tags of the earlier day with *all* tags of
the later day, and every reported increase keeps references to the two
price tags supporting it.

>>> from datetime import date
>>> from decimal import Decimal
>>> from price_parser.history import (
...     PriceTag, TaxCalendar, TaxMode, TaxRateChange, analyze_price_history,
... )
>>> vat = TaxCalendar({"DE": [TaxRateChange(date(2020, 1, 1), Decimal("0.19"))]})
>>> tags = [
...     PriceTag("t1", "p1", date(2026, 1, 10), "5,99 €", region="DE",
...              tax_mode=TaxMode.INCLUSIVE, net_grams_min=Decimal("500")),
...     PriceTag("t2", "p1", date(2026, 3, 1), "5,49 €", region="DE",
...              tax_mode=TaxMode.EXCLUSIVE, net_grams_min=Decimal("450")),
... ]
>>> events = analyze_price_history(tags, vat)
>>> [(event.from_date.isoformat(), event.to_date.isoformat(), event.verdict.value)
...  for event in events]
[('2026-01-10', '2026-03-01', 'definite_increase')]
>>> [(pair.before_tag.tag_id, pair.after_tag.tag_id)
...  for pair in events[0].supporting_pairs]
[('t1', 't2')]
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .parser import Price

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import date
    from decimal import Decimal


class TaxMode(str, enum.Enum):
    """How the scraped price text relates to tax."""

    INCLUSIVE = "inclusive"  # displayed price includes tax
    EXCLUSIVE = "exclusive"  # displayed price is before tax
    UNKNOWN = "unknown"  # not known which of the two


class Verdict(str, enum.Enum):
    """Classification of a price change between two capture dates."""

    DEFINITE_INCREASE = "definite_increase"
    POSSIBLE_INCREASE = "possible_increase"
    NO_INCREASE = "no_increase"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class TaxRateChange:
    """A tax rate (or rate range) taking effect on a given date.

    ``rate_min``/``rate_max`` are fractions, e.g. ``Decimal("0.19")`` for
    19 %.  Leave ``rate_max`` as ``None`` when the rate is known exactly.
    """

    effective: date
    rate_min: Decimal
    rate_max: Decimal | None = None

    def __post_init__(self) -> None:
        rate_max = self.rate_min if self.rate_max is None else self.rate_max
        if rate_max < self.rate_min:
            raise ValueError("rate_max must be >= rate_min")
        object.__setattr__(self, "rate_max", rate_max)


class TaxCalendar:
    """Effective-dated tax rates per region.

    >>> from datetime import date
    >>> from decimal import Decimal
    >>> cal = TaxCalendar({
    ...     "DE": [TaxRateChange(date(2020, 1, 1), Decimal("0.16")),
    ...            TaxRateChange(date(2021, 1, 1), Decimal("0.19"))],
    ... })
    >>> cal.rate_range("DE", date(2020, 6, 1))
    (Decimal('0.16'), Decimal('0.16'))
    >>> cal.rate_range("DE", date(2021, 6, 1))
    (Decimal('0.19'), Decimal('0.19'))
    >>> cal.rate_range("FR", date(2021, 6, 1)) is None
    True
    """

    def __init__(self, changes: Mapping[str, Iterable[TaxRateChange]]) -> None:
        self._changes: dict[str, list[TaxRateChange]] = {
            region: sorted(region_changes, key=lambda change: change.effective)
            for region, region_changes in changes.items()
        }

    def rate_range(
        self, region: str | None, day: date
    ) -> tuple[Decimal, Decimal] | None:
        """Return the (min, max) tax rate in effect in ``region`` on ``day``.

        Return ``None`` if the region is unknown or no rate is known to be
        in effect on that date yet.
        """
        if region is None:
            return None
        rate: tuple[Decimal, Decimal] | None = None
        for change in self._changes.get(region, ()):
            if change.effective > day:
                break
            rate_max = change.rate_max
            rate = (
                change.rate_min,
                rate_max if rate_max is not None else change.rate_min,
            )
        return rate


@dataclass(frozen=True)
class PriceTag:
    """A single scraped price tag: one observation of a product.

    ``net_grams_min``/``net_grams_max`` describe the net content; pass only
    ``net_grams_min`` when it is known exactly, or both when only a range
    is known.  ``tax_mode`` describes whether ``price_text`` includes tax.
    """

    tag_id: str
    product_id: str
    captured: date
    price_text: str
    currency_hint: str | None = None
    region: str | None = None
    tax_mode: TaxMode = TaxMode.UNKNOWN
    net_grams_min: Decimal | None = None
    net_grams_max: Decimal | None = None

    def __post_init__(self) -> None:
        if self.net_grams_max is None and self.net_grams_min is not None:
            object.__setattr__(self, "net_grams_max", self.net_grams_min)
        if (
            self.net_grams_min is not None
            and self.net_grams_max is not None
            and self.net_grams_max < self.net_grams_min
        ):
            raise ValueError("net_grams_max must be >= net_grams_min")


@dataclass(frozen=True)
class NormalizedPrice:
    """A price tag normalized to a net-of-tax price-per-gram interval.

    ``problem`` is ``None`` when normalization succeeded; otherwise it
    explains why the tag cannot be compared.
    """

    tag: PriceTag
    currency: str | None
    unit_min: Decimal | None
    unit_max: Decimal | None
    problem: str | None = None

    @property
    def comparable(self) -> bool:
        return self.problem is None


def normalize_tag(tag: PriceTag, tax_calendar: TaxCalendar) -> NormalizedPrice:
    """Normalize one tag to a net-of-tax unit price interval.

    The interval is wide when the tax rate or the net content is only
    known as a range, or when the tax mode of the tag is unknown.
    """
    price = Price.fromstring(tag.price_text, tag.currency_hint)
    if price.amount is None:
        return NormalizedPrice(
            tag, price.currency, None, None, f"no price found in {tag.price_text!r}"
        )
    if not price.currency:
        return NormalizedPrice(
            tag, None, None, None, f"no currency found in {tag.price_text!r}"
        )
    if price.amount < 0:
        return NormalizedPrice(tag, price.currency, None, None, "negative price")
    amount = price.amount
    if tag.tax_mode is TaxMode.EXCLUSIVE:
        net_min = net_max = amount
    else:
        rate = tax_calendar.rate_range(tag.region, tag.captured)
        if rate is None:
            return NormalizedPrice(
                tag,
                price.currency,
                None,
                None,
                f"no tax rate known for region {tag.region!r} on {tag.captured}",
            )
        rate_min, rate_max = rate
        if tag.tax_mode is TaxMode.INCLUSIVE:
            net_min = amount / (1 + rate_max)
            net_max = amount / (1 + rate_min)
        else:  # TaxMode.UNKNOWN: the displayed price may be gross or net
            net_min = amount / (1 + rate_max)
            net_max = amount
    grams_min = tag.net_grams_min
    grams_max = tag.net_grams_max
    if grams_min is None or grams_max is None or grams_min <= 0:
        return NormalizedPrice(
            tag, price.currency, None, None, "net content in grams is unknown"
        )
    return NormalizedPrice(
        tag, price.currency, net_min / grams_max, net_max / grams_min
    )


@dataclass(frozen=True)
class PairComparison:
    """Verdict for one pair of tags; keeps both tags for traceability."""

    before: NormalizedPrice
    after: NormalizedPrice
    verdict: Verdict
    reason: str | None = None

    @property
    def before_tag(self) -> PriceTag:
        return self.before.tag

    @property
    def after_tag(self) -> PriceTag:
        return self.after.tag


def compare_pair(before: NormalizedPrice, after: NormalizedPrice) -> PairComparison:
    """Classify the change between two normalized price tags."""
    if before.problem is not None:
        return PairComparison(
            before,
            after,
            Verdict.INSUFFICIENT_EVIDENCE,
            f"earlier tag {before.tag.tag_id}: {before.problem}",
        )
    if after.problem is not None:
        return PairComparison(
            before,
            after,
            Verdict.INSUFFICIENT_EVIDENCE,
            f"later tag {after.tag.tag_id}: {after.problem}",
        )
    if before.currency != after.currency:
        return PairComparison(
            before,
            after,
            Verdict.INSUFFICIENT_EVIDENCE,
            f"currency mismatch: {before.currency!r} vs {after.currency!r}",
        )
    assert before.unit_min is not None
    assert before.unit_max is not None
    assert after.unit_min is not None
    assert after.unit_max is not None
    if after.unit_min > before.unit_max:
        return PairComparison(before, after, Verdict.DEFINITE_INCREASE)
    if after.unit_max > before.unit_min:
        return PairComparison(before, after, Verdict.POSSIBLE_INCREASE)
    return PairComparison(before, after, Verdict.NO_INCREASE)


@dataclass(frozen=True)
class PriceChangeEvent:
    """Aggregated verdict for a boundary between two capture dates."""

    product_id: str
    from_date: date
    to_date: date
    verdict: Verdict
    pairs: tuple[PairComparison, ...]

    @property
    def supporting_pairs(self) -> tuple[PairComparison, ...]:
        """The tag pairs that evidence an increase.

        For a definite increase these are the pairs where every value of
        the new interval is above every value of the old one; for a
        possible increase, the pairs whose intervals still allow one.
        """
        if self.verdict is Verdict.DEFINITE_INCREASE:
            return tuple(
                pair for pair in self.pairs if pair.verdict is Verdict.DEFINITE_INCREASE
            )
        if self.verdict is Verdict.POSSIBLE_INCREASE:
            definite = tuple(
                pair for pair in self.pairs if pair.verdict is Verdict.DEFINITE_INCREASE
            )
            return definite or tuple(
                pair for pair in self.pairs if pair.verdict is Verdict.POSSIBLE_INCREASE
            )
        return ()


def _aggregate_verdict(pairs: tuple[PairComparison, ...]) -> Verdict:
    comparable = [
        pair for pair in pairs if pair.verdict is not Verdict.INSUFFICIENT_EVIDENCE
    ]
    if not comparable:
        return Verdict.INSUFFICIENT_EVIDENCE
    if all(pair.verdict is Verdict.DEFINITE_INCREASE for pair in comparable):
        return Verdict.DEFINITE_INCREASE
    if all(pair.verdict is Verdict.NO_INCREASE for pair in comparable):
        return Verdict.NO_INCREASE
    return Verdict.POSSIBLE_INCREASE


def analyze_price_history(
    tags: Iterable[PriceTag],
    tax_calendar: TaxCalendar,
) -> list[PriceChangeEvent]:
    """Build the price-change history for every product in ``tags``.

    Tags captured on the same day are treated as coexisting versions: the
    scrape order is never used to pick a "main" version.  Instead, a date
    boundary compares every tag of the earlier day with every tag of the
    later day, and the event verdict is aggregated over all comparable
    pairs.  Events are returned sorted by product id and date.
    """
    by_product: dict[str, dict[date, list[PriceTag]]] = {}
    for tag in tags:
        by_product.setdefault(tag.product_id, {}).setdefault(tag.captured, []).append(
            tag
        )
    events: list[PriceChangeEvent] = []
    for product_id in sorted(by_product):
        by_day = by_product[product_id]
        days = sorted(by_day)
        for from_day, to_day in zip(days, days[1:]):
            befores = [
                normalize_tag(tag, tax_calendar)
                for tag in sorted(by_day[from_day], key=lambda tag: tag.tag_id)
            ]
            afters = [
                normalize_tag(tag, tax_calendar)
                for tag in sorted(by_day[to_day], key=lambda tag: tag.tag_id)
            ]
            pairs = tuple(
                compare_pair(before, after) for before in befores for after in afters
            )
            events.append(
                PriceChangeEvent(
                    product_id, from_day, to_day, _aggregate_verdict(pairs), pairs
                )
            )
    return events
