"""Tests for reconciling ambiguous quote lines against a confirmed total."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

import attr
import pytest

from price_parser import LineReading, QuoteLine, resolve_quote


def test_unique_resolution_determines_every_line() -> None:
    result = resolve_quote([(1, "1.250"), (2, "2,500")], Decimal("5001.25"))

    assert result.exact is True
    assert result.is_unique is True
    assert result.combinations == 4
    assert result.determined_lines == (0, 1)
    assert result.ambiguous_lines == ()
    solution = result.solutions[0]
    assert solution.total == Decimal("5001.25")
    assert [reading.unit_price for reading in solution.readings] == [
        Decimal("1.250"),
        Decimal(2500),
    ]
    assert solution.readings[0].decimal_separator == "."
    assert solution.readings[0].thousands_separator is None
    assert solution.readings[1].decimal_separator is None
    assert solution.readings[1].thousands_separator == ","
    assert solution.readings[1].line_total == Decimal("5000.00")


def test_total_can_force_the_decimal_reading_against_heuristics() -> None:
    # A naive heuristic would read both as thousands-grouped integers;
    # the confirmed total proves both must be decimal numbers.
    result = resolve_quote([(1, "1.250"), (1, "2.500")], Decimal("3.75"))

    assert result.is_unique is True
    assert [reading.unit_price for reading in result.solutions[0].readings] == [
        Decimal("1.250"),
        Decimal("2.500"),
    ]


def test_remaining_ambiguity_lists_genuinely_different_readings() -> None:
    result = resolve_quote([(1, "1.250"), (1, "1.250")], Decimal("1251.25"))

    assert result.exact is True
    assert result.is_unique is False
    assert len(result.solutions) == 2
    assert result.determined_lines == ()
    assert result.ambiguous_lines == (0, 1)
    differing = result.differing_readings(0)
    assert [reading.unit_price for reading in differing] == [
        Decimal("1.250"),
        Decimal(1250),
    ]
    totals = {solution.total for solution in result.solutions}
    assert totals == {Decimal("1251.25")}


def test_some_lines_can_be_determined_while_others_stay_ambiguous() -> None:
    result = resolve_quote([(1, "1.250"), (1, "1.250"), (1, "100")], Decimal("1351.25"))

    assert result.exact is True
    assert len(result.solutions) == 2
    assert result.determined_lines == (2,)
    assert result.ambiguous_lines == (0, 1)
    assert result.solutions[0].readings[2].unit_price == Decimal(100)


def test_no_exact_match_returns_closest_achievable_total() -> None:
    result = resolve_quote([(1, "1.250")], Decimal(100))

    assert result.exact is False
    assert result.is_unique is True
    assert result.achieved_totals == (Decimal("1.25"),)
    assert result.solutions[0].total == Decimal("1.25")
    assert result.solutions[0].readings[0].unit_price == Decimal("1.250")
    assert result.determined_lines == (0,)


def test_closest_totals_can_be_equidistant_on_both_sides() -> None:
    result = resolve_quote(
        [(1, "1.250")], Decimal("625.625"), precision=Decimal("0.001")
    )

    assert result.exact is False
    assert len(result.solutions) == 2
    assert result.achieved_totals == (Decimal("1.250"), Decimal("1250.000"))
    assert result.ambiguous_lines == (0,)


def test_quantities_are_multiplied_before_rounding() -> None:
    result = resolve_quote([(4, "1.250")], Decimal("5.00"))

    assert result.is_unique is True
    reading = result.solutions[0].readings[0]
    assert reading.unit_price == Decimal("1.250")
    assert reading.line_total == Decimal("5.00")
    assert result.solutions[0].total == Decimal("5.00")


def test_integer_precision_rounds_line_totals_and_grand_total() -> None:
    result = resolve_quote([(3, "1.250")], Decimal(4), precision=0)

    assert result.precision == Decimal(1)
    assert result.is_unique is True
    assert result.solutions[0].total == Decimal(4)
    assert result.solutions[0].readings[0].line_total == Decimal(4)


def test_rounding_mode_is_configurable() -> None:
    half_up = resolve_quote(
        [(2, "1.250")], Decimal(3), precision=0, rounding=ROUND_HALF_UP
    )
    half_even = resolve_quote(
        [(2, "1.250")], Decimal(2), precision=0, rounding=ROUND_HALF_EVEN
    )

    assert half_up.solutions[0].readings[0].unit_price == Decimal("1.250")
    assert half_up.solutions[0].total == Decimal(3)
    assert half_even.solutions[0].readings[0].unit_price == Decimal("1.250")
    assert half_even.solutions[0].total == Decimal(2)


def test_mixed_and_repeated_separators_are_unambiguous() -> None:
    dot_grouping = resolve_quote([(1, "1.250,50")], Decimal("1250.50"))
    comma_grouping = resolve_quote([(1, "1,250.50")], Decimal("1250.50"))
    repeated = resolve_quote([(1, "1.250.000")], Decimal(1250000))

    assert dot_grouping.solutions[0].readings[0].unit_price == Decimal("1250.50")
    assert comma_grouping.solutions[0].readings[0].unit_price == Decimal("1250.50")
    assert repeated.solutions[0].readings[0].unit_price == Decimal(1250000)
    assert all(
        resolution.combinations == 1
        for resolution in (dot_grouping, comma_grouping, repeated)
    )


def test_integer_valued_decimals_keep_their_decimal_separator() -> None:
    whole = resolve_quote([(1, "1.00")], Decimal("1.00"))
    leading_zero = resolve_quote([(1, "0.500")], Decimal("0.500"))

    whole_reading = whole.solutions[0].readings[0]
    leading_reading = leading_zero.solutions[0].readings[0]
    assert whole_reading.unit_price == Decimal("1.00")
    assert whole_reading.decimal_separator == "."
    assert whole_reading.thousands_separator is None
    assert leading_reading.unit_price == Decimal("0.500")
    assert leading_reading.decimal_separator == "."
    assert leading_reading.thousands_separator is None


def test_currency_symbols_are_stripped_from_price_text() -> None:
    result = resolve_quote([(1, "€1.250"), (1, "$2,500")], Decimal("2501.25"))

    assert result.is_unique is True
    assert [reading.unit_price for reading in result.solutions[0].readings] == [
        Decimal("1.250"),
        Decimal(2500),
    ]


def test_quote_line_objects_and_decimal_quantities_are_accepted() -> None:
    line = QuoteLine(
        quantity=Decimal(2),
        price_text="1.250",
        readings=(),
    )
    result = resolve_quote([line], Decimal("2.50"))

    assert result.is_unique is True
    assert result.solutions[0].readings[0].unit_price == Decimal("1.250")


def test_total_accepts_strings_and_integers() -> None:
    from_string = resolve_quote([(1, "1.250")], "1.25")
    from_integer = resolve_quote([(1, "1.250")], 1250)

    assert from_string.solutions[0].readings[0].unit_price == Decimal("1.250")
    assert from_integer.solutions[0].readings[0].unit_price == Decimal(1250)


def test_differing_readings_raises_for_determined_line() -> None:
    result = resolve_quote([(1, "1.250")], Decimal("1.25"))

    with pytest.raises(ValueError, match="not ambiguous"):
        result.differing_readings(0)


def test_line_reading_is_a_frozen_value_object() -> None:
    reading = LineReading(
        unit_price=Decimal("1.250"),
        line_total=Decimal("1.25"),
        decimal_separator=".",
        thousands_separator=None,
    )
    with pytest.raises(attr.exceptions.FrozenInstanceError):
        reading.unit_price = Decimal(1250)  # type: ignore[misc]


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        resolve_quote([], Decimal(1))
    with pytest.raises(ValueError, match="unparseable"):
        resolve_quote([(1, "abc")], Decimal(1))
    with pytest.raises(ValueError, match="quantity"):
        resolve_quote([(0, "1.250")], Decimal(0))
    with pytest.raises(ValueError, match="precision"):
        resolve_quote([(1, "1.250")], Decimal(1), precision=-1)
    with pytest.raises(ValueError, match="precision"):
        resolve_quote([(1, "1.250")], Decimal(1), precision=Decimal(0))
    with pytest.raises(ValueError, match="empty price"):
        resolve_quote([(1, "")], Decimal(1))
