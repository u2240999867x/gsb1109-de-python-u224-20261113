"""
Tests for promotional payment component extraction
(``price_parser.extract_payments``).

The canonical example is a Chinese presale offer such as
"定金20抵50，尾款149": the deposit (20) is cash paid, the deduction (50)
is a marketing value which is never paid in cash, and the balance (149)
may be stated either before or after the deduction is applied.
"""

from __future__ import annotations

from decimal import Decimal

from price_parser import (
    BALANCE,
    BALANCE_POST_DEDUCTION,
    BALANCE_PRE_DEDUCTION,
    DEDUCTION,
    DEPOSIT,
    DEPOSIT_ONLY,
    IGNORE_PERCENT,
    IGNORE_REFERENCE_PRICE,
    IGNORE_UNASSOCIATED,
    PaymentBreakdown,
    PaymentComponent,
    extract_payments,
)


def _component(breakdown: PaymentBreakdown, kind: str) -> PaymentComponent:
    matches = [
        component for component in breakdown.components if component.kind == kind
    ]
    assert len(matches) == 1
    return matches[0]


def test_extracts_deposit_deduction_and_balance_with_sources() -> None:
    text = "定金20抵50，尾款149"
    breakdown = extract_payments(text)

    assert [component.kind for component in breakdown.components] == [
        DEPOSIT,
        DEDUCTION,
        BALANCE,
    ]
    deposit = _component(breakdown, DEPOSIT)
    deduction = _component(breakdown, DEDUCTION)
    balance = _component(breakdown, BALANCE)

    assert deposit.amount == Decimal(20)
    assert deduction.amount == Decimal(50)
    assert balance.amount == Decimal(149)

    # Every component points back to its slice of the source text.
    assert deposit.source_text == "定金20"
    assert deduction.source_text == "抵50"
    assert balance.source_text == "尾款149"
    for component in breakdown.components:
        start, end = component.source_span
        assert text[start:end] == component.source_text


def test_balance_keeps_both_interpretations_when_unexplained() -> None:
    breakdown = extract_payments("定金20抵50，尾款149")
    balance = _component(breakdown, BALANCE)

    assert [(item.interpretation, item.amount) for item in balance.interpretations] == [
        (BALANCE_POST_DEDUCTION, Decimal(149)),
        (BALANCE_PRE_DEDUCTION, Decimal(99)),
    ]

    # Two tenable cash totals; the 50 deduction is not added on top.
    assert [
        (total.interpretation, total.amount) for total in breakdown.payment_totals
    ] == [
        (BALANCE_POST_DEDUCTION, Decimal(169)),
        (BALANCE_PRE_DEDUCTION, Decimal(119)),
    ]
    total_amounts = {total.amount for total in breakdown.payment_totals}
    assert Decimal(219) not in total_amounts  # 20 + 50 + 149 would be wrong
    assert all(total.complete for total in breakdown.payment_totals)


def test_every_total_traces_back_to_the_source_text() -> None:
    text = "定金20抵50，尾款149"
    breakdown = extract_payments(text)
    for total in breakdown.payment_totals:
        assert total.sources
        for source in total.sources:
            assert source in text


def test_stated_total_confirms_post_deduction_reading() -> None:
    breakdown = extract_payments("定金 20 元抵 50，尾款 149 元，到手价169")
    balance = _component(breakdown, BALANCE)

    assert len(breakdown.payment_totals) == 1
    total = breakdown.payment_totals[0]
    assert total.interpretation == BALANCE_POST_DEDUCTION
    assert total.amount == Decimal(169)
    assert total.matches_stated_total is True
    assert [item.interpretation for item in balance.interpretations] == [
        BALANCE_POST_DEDUCTION
    ]

    assert len(breakdown.stated_totals) == 1
    stated = breakdown.stated_totals[0]
    assert stated.amount == Decimal(169)
    assert stated.source_text == "到手价169"


def test_stated_total_confirms_pre_deduction_reading() -> None:
    breakdown = extract_payments("定金20抵50，尾款149，到手价119")
    balance = _component(breakdown, BALANCE)

    assert len(breakdown.payment_totals) == 1
    total = breakdown.payment_totals[0]
    assert total.interpretation == BALANCE_PRE_DEDUCTION
    assert total.amount == Decimal(119)
    assert total.matches_stated_total is True
    assert [(item.interpretation, item.amount) for item in balance.interpretations] == [
        (BALANCE_PRE_DEDUCTION, Decimal(99))
    ]


def test_stated_total_matching_another_value_does_not_silently_drop() -> None:
    # 到手价 999 matches neither reading; ambiguity is preserved.
    breakdown = extract_payments("定金20抵50，尾款149，到手价999")
    assert {total.interpretation for total in breakdown.payment_totals} == {
        BALANCE_POST_DEDUCTION,
        BALANCE_PRE_DEDUCTION,
    }
    assert all(
        total.matches_stated_total is False for total in breakdown.payment_totals
    )


def test_deposit_without_balance_only_counts_cash_paid() -> None:
    breakdown = extract_payments("定金20抵50")

    assert [component.kind for component in breakdown.components] == [
        DEPOSIT,
        DEDUCTION,
    ]
    assert len(breakdown.payment_totals) == 1
    total = breakdown.payment_totals[0]
    assert total.amount == Decimal(20)  # not 20 + 50
    assert total.interpretation == DEPOSIT_ONLY
    assert total.complete is False
    assert total.sources == ["定金20"]


def test_balance_without_deduction_has_single_reading() -> None:
    breakdown = extract_payments("定金20，尾款149")
    assert len(breakdown.payment_totals) == 1
    total = breakdown.payment_totals[0]
    assert total.interpretation == BALANCE_POST_DEDUCTION
    assert total.amount == Decimal(169)
    balance = _component(breakdown, BALANCE)
    assert [item.interpretation for item in balance.interpretations] == [
        BALANCE_POST_DEDUCTION
    ]


def test_pre_deduction_reading_requires_balance_above_deduction() -> None:
    breakdown = extract_payments("定金10抵100，尾款50")
    assert len(breakdown.payment_totals) == 1
    total = breakdown.payment_totals[0]
    assert total.interpretation == BALANCE_POST_DEDUCTION
    assert total.amount == Decimal(60)
    balance = _component(breakdown, BALANCE)
    assert [item.interpretation for item in balance.interpretations] == [
        BALANCE_POST_DEDUCTION
    ]


def test_missing_deposit_amount_keeps_total_incomplete() -> None:
    # 定金 is promised but no amount follows it; only the balance is parseable.
    breakdown = extract_payments("定金抵50，尾款149")
    assert [component.kind for component in breakdown.components] == [
        DEDUCTION,
        BALANCE,
    ]
    assert [
        (total.interpretation, total.amount) for total in breakdown.payment_totals
    ] == [
        (BALANCE_POST_DEDUCTION, Decimal(149)),
        (BALANCE_PRE_DEDUCTION, Decimal(99)),
    ]
    assert all(total.complete is False for total in breakdown.payment_totals)


def test_currency_is_extracted() -> None:
    assert extract_payments("定金￥50抵￥100，尾款￥899").currency == "￥"
    assert extract_payments(
        "定金$50抵$100，尾款$899", currency_hint="USD"
    ).currency in {"$", "USD"}


def test_percentages_are_ignored() -> None:
    breakdown = extract_payments("定金20抵50，尾款149，立减20%")
    percent_numbers = [
        item for item in breakdown.ignored_amounts if item.reason == IGNORE_PERCENT
    ]
    assert len(percent_numbers) == 1
    assert percent_numbers[0].source_text == "20%"
    assert percent_numbers[0].amount == Decimal(20)
    # Percentages do not become components or totals.
    assert all(
        component.amount != Decimal(20) or component.kind == DEPOSIT
        for component in breakdown.components
    )


def test_reference_prices_are_ignored() -> None:
    breakdown = extract_payments("原价399，定金20抵50，尾款149")
    reference = [
        item
        for item in breakdown.ignored_amounts
        if item.reason == IGNORE_REFERENCE_PRICE
    ]
    assert len(reference) == 1
    assert reference[0].amount == Decimal(399)
    assert reference[0].source_text == "原价399"
    assert all(total.amount != Decimal(399) for total in breakdown.payment_totals)


def test_unassociated_numbers_are_ignored() -> None:
    breakdown = extract_payments("定金20抵50，尾款149，共2件")
    unassociated = [
        item for item in breakdown.ignored_amounts if item.reason == IGNORE_UNASSOCIATED
    ]
    assert len(unassociated) == 1
    assert unassociated[0].amount == Decimal(2)
    assert unassociated[0].source_text == "2"


def test_plain_price_string_returns_empty_breakdown() -> None:
    breakdown = extract_payments("22,90 €")
    assert breakdown.currency == "€"
    assert breakdown.components == []
    assert breakdown.payment_totals == []
    assert breakdown.stated_totals == []
    assert breakdown.ignored_amounts == []


def test_none_and_empty_input() -> None:
    for value in (None, ""):
        breakdown = extract_payments(value)
        assert breakdown.currency is None
        assert breakdown.components == []
        assert breakdown.payment_totals == []


def test_whitespace_is_normalized_in_sources() -> None:
    breakdown = extract_payments("定金 20 抵 50 , 尾款 149")
    assert _component(breakdown, DEPOSIT).source_text == "定金 20"
    assert _component(breakdown, DEDUCTION).source_text == "抵 50"
    assert _component(breakdown, BALANCE).source_text == "尾款 149"


def test_separator_arguments_are_passed_through() -> None:
    breakdown = extract_payments(
        "定金1.000抵2.000，尾款3.000",
        decimal_separator=",",
        digit_group_separator=".",
    )
    assert _component(breakdown, DEPOSIT).amount == Decimal(1000)
    assert _component(breakdown, DEDUCTION).amount == Decimal(2000)
    assert _component(breakdown, BALANCE).amount == Decimal(3000)


def test_amount_text_is_preserved_as_written() -> None:
    breakdown = extract_payments("定金 20.00 抵 50.00，尾款 149.00")
    assert _component(breakdown, DEPOSIT).amount_text == "20.00"
    assert _component(breakdown, DEDUCTION).amount_text == "50.00"
    assert _component(breakdown, BALANCE).amount_text == "149.00"


def test_full_promotional_sentence() -> None:
    text = "预售：定金100元 抵扣150元 尾款850元 到手价950 原价1299"
    breakdown = extract_payments(text)

    assert _component(breakdown, DEPOSIT).amount == Decimal(100)
    assert _component(breakdown, DEDUCTION).amount == Decimal(150)
    assert _component(breakdown, BALANCE).amount == Decimal(850)

    assert len(breakdown.payment_totals) == 1
    total = breakdown.payment_totals[0]
    assert total.amount == Decimal(950)
    assert total.interpretation == BALANCE_POST_DEDUCTION
    assert total.matches_stated_total is True

    ignored = {item.amount: item.reason for item in breakdown.ignored_amounts}
    assert ignored == {Decimal(1299): IGNORE_REFERENCE_PRICE}
