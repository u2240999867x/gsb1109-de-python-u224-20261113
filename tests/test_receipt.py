"""
Tests for receipt tax reconciliation.

The numbers come from a hand-built receipt on which the three rounding
conventions produce different totals, so that each convention can be checked
independently. All arithmetic is verified against exact Decimal values.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from price_parser import (
    ReceiptLine,
    RoundingError,
    RoundingPolicy,
    minimal_discriminator,
    reconcile_tax,
    tax_per_bracket,
    tax_per_item,
    tax_per_line,
)

CENT = RoundingPolicy(Decimal("0.01"))
CENT_EVEN = RoundingPolicy(Decimal("0.01"), mode="half_even")


def make_lines() -> list[ReceiptLine]:
    return [
        ReceiptLine.from_strings("8,30 €", "3", "19 %"),
        ReceiptLine.from_strings("5,50 €", "1", "19 %"),
        ReceiptLine.from_strings("12,70 €", "2", "19 %"),
    ]


def test_line_parses_raw_strings_and_keeps_scale() -> None:
    line = ReceiptLine.from_strings("1 234,56 €", "2", "19 %")
    assert line.unit_price == Decimal("1234.56")
    assert line.quantity == Decimal(2)
    assert line.tax_rate == Decimal("0.19")
    assert line.net == Decimal("2469.12")


def test_rate_without_percent_is_not_divided() -> None:
    line = ReceiptLine.from_strings("10,00 €", "1", "0.07")
    assert line.tax_rate == Decimal("0.07")


def test_minus_and_parentheses_become_negative_refund_lines() -> None:
    assert ReceiptLine.from_strings("-8,30 €", "1", "19 %").net == Decimal("-8.30")
    assert ReceiptLine.from_strings("(8,30 €)", "1", "19 %").net == Decimal("-8.30")


def test_unparseable_price_raises() -> None:
    with pytest.raises(ValueError, match="could not parse"):
        ReceiptLine.from_strings("abc", "1", "19 %")


def test_three_conventions_on_the_discriminating_receipt() -> None:
    lines = make_lines()
    assert tax_per_item(lines, CENT) == Decimal("10.61")
    assert tax_per_line(lines, CENT) == Decimal("10.61")
    assert tax_per_bracket(lines, CENT) == Decimal("10.60")


def test_single_quantity_line_makes_item_and_line_coincide() -> None:
    line = ReceiptLine.from_strings("5,50 €", "1", "19 %")
    assert tax_per_item([line], CENT) == Decimal("1.05")
    assert tax_per_line([line], CENT) == Decimal("1.05")
    assert tax_per_bracket([line], CENT) == Decimal("1.05")


def test_quantity_distinguishes_per_item_from_per_line() -> None:
    line = ReceiptLine.from_strings("8,30 €", "3", "19 %")
    # 8.30 * 19% = 1.577 -> 1.58 per item, 1.58 * 3 = 4.74
    assert tax_per_item([line], CENT) == Decimal("4.74")
    # 24.90 * 19% = 4.731 -> 4.73 once for the whole line
    assert tax_per_line([line], CENT) == Decimal("4.73")
    assert tax_per_bracket([line], CENT) == Decimal("4.73")


def test_per_bracket_aggregates_net_before_rounding() -> None:
    first = ReceiptLine.from_strings("8,30 €", "1", "19 %")
    second = ReceiptLine.from_strings("5,50 €", "1", "19 %")
    # per line: 1.58 + 1.05 = 2.63
    assert tax_per_line([first, second], CENT) == Decimal("2.63")
    # bracket: 13.80 * 19% = 2.622 -> 2.62
    assert tax_per_bracket([first, second], CENT) == Decimal("2.62")


def test_per_bracket_groups_by_tax_rate_without_merging_rates() -> None:
    lines = [
        ReceiptLine(Decimal("8.30"), Decimal(1), Decimal("0.19")),
        ReceiptLine(Decimal("5.50"), Decimal(1), Decimal("0.19")),
        ReceiptLine(Decimal("10.00"), Decimal(1), Decimal("0.07")),
    ]
    # 19% bracket: 13.80 * 19% = 2.622 -> 2.62; 7% bracket: 10 * 7% = 0.70
    assert tax_per_bracket(lines, CENT) == Decimal("3.32")
    # per line: 1.58 + 1.05 + 0.70 = 3.33
    assert tax_per_line(lines, CENT) == Decimal("3.33")


def test_refund_negative_tie_half_up_rounds_away_from_zero() -> None:
    refund = ReceiptLine.from_strings("-5,50 €", "1", "19 %")
    # -5.50 * 19% = -1.045
    assert tax_per_line([refund], CENT) == Decimal("-1.05")
    assert tax_per_item([refund], CENT) == Decimal("-1.05")


def test_tie_rounding_modes_differ_on_both_signs() -> None:
    refund = ReceiptLine.from_strings("-5,50 €", "1", "19 %")
    sale = ReceiptLine.from_strings("5,50 €", "1", "19 %")
    # exact tie at 1.045 / -1.045: half-up goes away from zero, half-even
    # picks the even hundredth (104) on both sides of zero.
    assert tax_per_line([refund], CENT) == Decimal("-1.05")
    assert tax_per_line([refund], CENT_EVEN) == Decimal("-1.04")
    assert tax_per_line([sale], CENT) == Decimal("1.05")
    assert tax_per_line([sale], CENT_EVEN) == Decimal("1.04")


def test_refund_line_keeps_sign_through_all_conventions() -> None:
    sale = ReceiptLine.from_strings("8,30 €", "3", "19 %")
    refund = ReceiptLine.from_strings("-8,30 €", "1", "19 %")
    lines = [sale, refund]
    assert tax_per_item(lines, CENT) == Decimal("3.16")
    assert tax_per_line(lines, CENT) == Decimal("3.15")
    assert tax_per_bracket(lines, CENT) == Decimal("3.15")


def test_negative_quantity_refund_is_equivalent_to_negative_price() -> None:
    by_price = ReceiptLine.from_strings("-8,30 €", "1", "19 %")
    by_quantity = ReceiptLine(Decimal("8.30"), Decimal(-1), Decimal("0.19"))
    for calculator in (tax_per_item, tax_per_line, tax_per_bracket):
        assert calculator([by_price], CENT) == calculator([by_quantity], CENT)
    assert tax_per_line([by_quantity], CENT) == Decimal("-1.58")


def test_yen_has_no_minor_unit() -> None:
    yen = RoundingPolicy(Decimal(1))
    line = ReceiptLine(Decimal(210), Decimal(3), Decimal("0.05"))
    # 210 * 5% = 10.5 -> 11 per item, 11 * 3 = 33
    assert tax_per_item([line], yen) == Decimal(33)
    # 630 * 5% = 31.5 -> 32 once per line / bracket
    assert tax_per_line([line], yen) == Decimal(32)
    assert tax_per_bracket([line], yen) == Decimal(32)


def test_swiss_fanc_rounds_to_five_cent_unit() -> None:
    franc = RoundingPolicy(Decimal("0.05"))
    line = ReceiptLine(Decimal("20.60"), Decimal(2), Decimal("0.05"))
    # 20.60 * 5% = 1.03 -> 1.05 per item, 1.05 * 2 = 2.10
    assert tax_per_item([line], franc) == Decimal("2.10")
    # 41.20 * 5% = 2.06 -> 2.05 per line / bracket
    assert tax_per_line([line], franc) == Decimal("2.05")
    assert tax_per_bracket([line], franc) == Decimal("2.05")


def test_rounding_policy_validates_arguments() -> None:
    with pytest.raises(ValueError, match="currency unit"):
        RoundingPolicy(Decimal(0))
    with pytest.raises(ValueError, match="rounding mode"):
        RoundingPolicy(Decimal("0.01"), mode="up")


def test_rounding_non_finite_value_raises() -> None:
    with pytest.raises(RoundingError):
        CENT.round(Decimal("Infinity"))


def test_reconcile_identifies_unique_bracket_convention() -> None:
    result = reconcile_tax(make_lines(), Decimal("10.60"), CENT)
    assert result.taxes == {
        "per_item": Decimal("10.61"),
        "per_line": Decimal("10.61"),
        "per_bracket": Decimal("10.60"),
    }
    assert result.matching_methods == ["per_bracket"]
    assert result.is_unambiguous
    assert result.is_explained


def test_reconcile_keeps_tie_between_two_conventions() -> None:
    result = reconcile_tax(make_lines(), Decimal("10.61"), CENT)
    assert result.matching_methods == ["per_item", "per_line"]
    assert not result.is_unambiguous
    assert result.is_explained


def test_reconcile_rejects_amount_no_convention_produces() -> None:
    result = reconcile_tax(make_lines(), Decimal("9.99"), CENT)
    assert result.matching_methods == []
    assert not result.is_unambiguous
    assert not result.is_explained


def test_reconcile_accepts_raw_negative_printed_tax_string() -> None:
    refund = ReceiptLine.from_strings("-5,50 €", "1", "19 %")
    result = reconcile_tax([refund], "(1,05 €)", CENT)
    assert result.printed_tax == Decimal("-1.05")
    assert result.matching_methods == ["per_item", "per_line", "per_bracket"]


def test_minimal_discriminator_is_the_first_single_line() -> None:
    witness = minimal_discriminator(make_lines(), CENT)
    assert witness is not None
    subset, taxes = witness
    assert len(subset) == 1
    assert subset[0] == make_lines()[0]
    assert taxes == {
        "per_item": Decimal("4.74"),
        "per_line": Decimal("4.73"),
        "per_bracket": Decimal("4.73"),
    }


def test_minimal_discriminator_none_when_conventions_coincide() -> None:
    lines = [
        ReceiptLine(Decimal("100.00"), Decimal(1), Decimal("0.19")),
        ReceiptLine(Decimal("50.00"), Decimal(1), Decimal("0.19")),
    ]
    assert minimal_discriminator(lines, CENT) is None


def test_minimal_discriminator_finds_two_line_bracket_witness() -> None:
    # Every single line has quantity 1, so item == line everywhere; only the
    # bracket aggregation separates after two lines.
    lines = [
        ReceiptLine(Decimal("8.30"), Decimal(1), Decimal("0.19")),
        ReceiptLine(Decimal("5.50"), Decimal(1), Decimal("0.19")),
        ReceiptLine(Decimal("10.00"), Decimal(1), Decimal("0.07")),
    ]
    witness = minimal_discriminator(lines, CENT)
    assert witness is not None
    subset, taxes = witness
    assert len(subset) == 2
    assert taxes["per_item"] == taxes["per_line"] == Decimal("2.63")
    assert taxes["per_bracket"] == Decimal("2.62")
