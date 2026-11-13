"""Tests for structured price-range extraction.

These cover quoted intervals ("10-12 /kg"), one-sided quotes
("from 10", "10 以下"), endpoint inclusivity, units, the rule that range
endpoints must never be stitched together across separate products or
currencies, and separating certainly-cheap from possibly-cheap quotes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from price_parser import Bound, Price, PriceRange, parse_price_range


def _range(
    raw: str,
    decimal_separator: str | None = None,
    digit_group_separator: str | None = None,
) -> PriceRange:
    price_range = Price.fromstring(
        raw,
        decimal_separator=decimal_separator,
        digit_group_separator=digit_group_separator,
    ).price_range
    assert price_range is not None
    return price_range


TWO_SIDED_EXAMPLES = [
    ("10–12 /kg", "10", "12", "kg"),
    ("10-12", "10", "12", None),
    ("10 — 12", "10", "12", None),
    ("10~12", "10", "12", None),
    ("10 to 12", "10", "12", None),
    ("10 bis 12", "10", "12", None),
    ("$29.99 – $74.99", "29.99", "74.99", None),
    ("£6.95 - £9.95", "6.95", "9.95", None),
    ("841,00 zł – 995,00 zł", "841.00", "995.00", None),
    ("€ 8.00 – € 10.00", "8.00", "10.00", None),
]


@pytest.mark.parametrize(
    ("raw", "lower", "upper", "unit"),
    TWO_SIDED_EXAMPLES,
)
def test_two_sided_ranges(raw: str, lower: str, upper: str, unit: str | None) -> None:
    price_range = _range(raw)
    assert price_range.values == (Decimal(lower), Decimal(upper))
    assert price_range.lower is not None
    assert price_range.upper is not None
    lower_text = price_range.lower.text
    upper_text = price_range.upper.text
    assert price_range.lower == Bound(
        value=Decimal(lower), inclusive=True, text=lower_text
    )
    assert price_range.upper == Bound(
        value=Decimal(upper), inclusive=True, text=upper_text
    )
    assert price_range.lower.inclusive is True
    assert price_range.upper.inclusive is True
    assert price_range.unit == unit
    assert price_range.is_exact is False
    assert price_range.source_text is not None


def test_range_keeps_raw_source_text() -> None:
    price_range = _range("10–12 /kg")
    assert price_range.source_text == "10–12"
    assert _range("$29.99 – $74.99").source_text == "29.99 – $74.99"


LOWER_BOUND_EXAMPLES = [
    ("from 10", True),
    ("ab 10", True),
    ("vanaf 10", True),
    ("10 以上", True),
    ("10+", True),
    ("≥10", True),
    (">= 10", True),
    ("over 10", False),
    ("above 10", False),
    ("more than 10", False),
    (">10", False),
]


@pytest.mark.parametrize(("raw", "inclusive"), LOWER_BOUND_EXAMPLES)
def test_lower_bounds(raw: str, inclusive: bool) -> None:
    price_range = _range(raw)
    assert price_range.upper is None
    assert price_range.lower is not None
    assert price_range.lower.value == Decimal(10)
    assert price_range.lower.inclusive is inclusive
    assert price_range.is_exact is False


UPPER_BOUND_EXAMPLES = [
    ("10 以下", True),
    ("10 以内", True),
    ("up to 10", True),
    ("max 10", True),
    ("≤10", True),
    ("<= 10", True),
    ("10 or less", True),
    ("under 10", False),
    ("below 10", False),
    ("less than 10", False),
    ("<10", False),
]


@pytest.mark.parametrize(("raw", "inclusive"), UPPER_BOUND_EXAMPLES)
def test_upper_bounds(raw: str, inclusive: bool) -> None:
    price_range = _range(raw)
    assert price_range.lower is None
    assert price_range.upper is not None
    assert price_range.upper.value == Decimal(10)
    assert price_range.upper.inclusive is inclusive
    assert price_range.is_exact is False


EXACT_EXAMPLES = [
    ("8", None),
    ("$9.99", None),
    ("22,90 €", None),
    ("10.00 /kg", "kg"),
    ("5 per kg", "kg"),
    ("5 pro kg", "kg"),
]


@pytest.mark.parametrize(("raw", "unit"), EXACT_EXAMPLES)
def test_exact_quotes(raw: str, unit: str | None) -> None:
    price = Price.fromstring(raw)
    price_range = price.price_range
    assert price_range is not None
    assert price_range.is_exact is True
    assert price_range.lower is price_range.upper
    assert price_range.lower is not None
    assert price_range.lower.value == price.amount
    assert price_range.unit == unit


def test_unit_is_never_guessed() -> None:
    # A unit-less one-sided quote stays unit-less; it must not be turned into
    # a per-kilogram upper bound.
    price_range = _range("from 10")
    assert price_range.unit is None
    assert price_range.lower is not None
    assert price_range.upper is None

    price_range = _range("10 以下")
    assert price_range.unit is None
    assert price_range.upper is not None
    assert price_range.lower is None


def test_one_sided_quote_keeps_bound_when_unit_follows() -> None:
    # The CJK limit marker precedes the unit; the bound must not collapse to
    # an exact quote just because "/kg" comes after "以下".
    upper = _range("100 以下 /kg")
    assert upper.lower is None
    assert upper.upper is not None
    assert upper.upper.value == Decimal(100)
    assert upper.upper.inclusive is True
    assert upper.unit == "kg"

    lower = _range("10+ /kg")
    assert lower.upper is None
    assert lower.lower is not None
    assert lower.lower.value == Decimal(10)
    assert lower.unit == "kg"


# Adjacent prices (current price next to a struck-through original) and
# discount labels must NOT be merged into a single two-sided range.
NO_MERGE_EXAMPLES = [
    "$19.99 $9.99",
    "99,99 EUR (-30,00%) 69,99 EUR",
    "5 833,00 € -5% 6 140,00 €",
    "3200 руб. 1500 руб.",
    "65 000 руб. 75 000 руб.",
]


@pytest.mark.parametrize("raw", NO_MERGE_EXAMPLES)
def test_adjacent_prices_are_not_merged(raw: str) -> None:
    price_range = _range(raw)
    assert price_range.is_exact is True
    assert price_range.lower is price_range.upper


def test_cross_currency_endpoints_are_not_merged() -> None:
    # A dollar lower bound and a euro upper bound describe different prices;
    # the second number belongs to a different currency, so no range is built.
    price_range = _range("$5 – €8")
    assert price_range.is_exact is True
    assert price_range.lower is price_range.upper
    assert price_range.lower is not None
    assert price_range.lower.value == Decimal(5)


# (raw, certainly <= 10, possibly <= 10)
CERTAINTY_EXAMPLES = [
    ("8", True, True),
    ("$9.99", True, True),
    ("10", True, True),
    ("12", False, False),
    ("10 以下", True, True),
    ("under 10", True, True),
    ("up to 10", True, True),
    ("from 10", False, True),
    ("10 以上", False, True),
    ("over 10", False, False),
    ("10–12 /kg", False, True),
    ("8 – 10", True, True),
    ("8 – 9", True, True),
    ("11 – 20", False, False),
    ("$29.99 – $74.99", False, False),
]


@pytest.mark.parametrize(
    ("raw", "certain", "possible"),
    CERTAINTY_EXAMPLES,
)
def test_certain_vs_possible_at_most_ten(
    raw: str, certain: bool, possible: bool
) -> None:
    price_range = _range(raw)
    assert price_range.certainly_at_most(Decimal(10)) is certain
    assert price_range.possibly_at_most(Decimal(10)) is possible


def test_certainly_cheap_is_a_subset_of_possibly_cheap() -> None:
    for raw, certain, possible in CERTAINTY_EXAMPLES:
        if certain:
            assert possible is True, raw


def test_amount_and_range_are_present_together() -> None:
    for raw in ["8", "from 10", "10–12", "Free", "50% OFF", "Foo", ""]:
        price = Price.fromstring(raw)
        assert (price.amount is not None) == (price.price_range is not None), raw


def test_free_becomes_exact_zero_range() -> None:
    price_range = _range("Free")
    assert price_range.is_exact is True
    assert price_range.values == (Decimal(0), Decimal(0))


def test_no_price_has_no_range() -> None:
    assert Price.fromstring("50% OFF").price_range is None
    assert Price.fromstring("Foo").price_range is None
    assert Price.fromstring("").price_range is None
    assert Price.fromstring(None).price_range is None


def test_parse_price_range_helper() -> None:
    price_range = parse_price_range("10–12 /kg")
    assert isinstance(price_range, PriceRange)
    assert price_range is not None
    assert price_range.values == (Decimal(10), Decimal(12))
    assert parse_price_range("Foo") is None


def test_decimal_separator_hint_applies_to_both_endpoints() -> None:
    price_range = _range("10,5 - 12,5")
    assert price_range.values == (Decimal("10.5"), Decimal("12.5"))

    hinted = _range("10.5 - 12.5", decimal_separator=",")
    assert hinted.values == (Decimal(105), Decimal(125))


def test_currency_is_recorded_on_range() -> None:
    price_range = _range("$29.99 – $74.99")
    assert price_range.currency == "$"
    assert _range("841,00 zł – 995,00 zł").currency == "zł"


def test_bound_repr_carries_text() -> None:
    price_range = _range("10–12 /kg")
    assert price_range.lower is not None
    assert price_range.upper is not None
    assert price_range.lower.text == "10"
    assert price_range.upper.text == "12"
    assert isinstance(price_range.lower, Bound)
