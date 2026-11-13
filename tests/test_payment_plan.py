from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from price_parser import CashFlow, PaymentParseError, PaymentPlan, parse_payment_plan

SIGNING = date(2026, 11, 14)

FULL_PLAN = (
    "首付 2000元；押金 3000元 可退；首期手续费 500元；"
    "促销月付 999元 前3期；月付 1299元 第4-12期；尾款买断 8000元"
)


def _kinds(flows: list[CashFlow]) -> list[str]:
    return [flow.kind for flow in flows]


def test_full_plan_split_and_totals() -> None:
    plan = PaymentPlan.parse(FULL_PLAN, signing_date=SIGNING)

    assert plan.currency == "元"
    assert plan.term_months == 12
    assert plan.is_lower_bound is False
    assert len(plan.mandatory) == 15
    assert _kinds(plan.optional) == ["balloon", "deposit_refund"]

    assert plan.mandatory_total == Decimal(20188)
    assert plan.total_lower_bound == Decimal(20188)
    assert plan.optional_total == Decimal(5000)


def test_promotion_only_covers_first_three_periods() -> None:
    plan = PaymentPlan.parse(FULL_PLAN, signing_date=SIGNING)
    promo = [flow for flow in plan.mandatory if flow.kind == "promo_installment"]
    regular = [flow for flow in plan.mandatory if flow.kind == "installment"]

    assert [flow.period for flow in promo] == [1, 2, 3]
    assert {flow.amount for flow in promo} == {Decimal(999)}
    assert [flow.period for flow in regular] == list(range(4, 13))
    assert {flow.amount for flow in regular} == {Decimal(1299)}


def test_dates_follow_signing_day_and_month_end_rules() -> None:
    plan = PaymentPlan.parse(FULL_PLAN, signing_date=SIGNING)

    upfront = [flow for flow in plan.mandatory if flow.period is None]
    assert {flow.due_date for flow in upfront} == {SIGNING}

    installments = [flow for flow in plan.mandatory if flow.period is not None]
    assert installments[0].due_date == date(2026, 12, 31)
    assert installments[1].due_date == date(2027, 1, 31)
    assert installments[2].due_date == date(2027, 2, 28)
    assert installments[-1].due_date == date(2027, 11, 30)

    assert {flow.due_date for flow in plan.optional} == {date(2027, 11, 30)}


def test_string_signing_date_and_alias() -> None:
    plan = parse_payment_plan(FULL_PLAN, signing_date="2026-11-14")
    assert plan.mandatory[0].due_date == SIGNING


def test_refundable_deposit_emits_negative_optional_flow() -> None:
    plan = PaymentPlan.parse("押金 3000元 可退；月付 1000元 ×6期", signing_date=SIGNING)
    deposit = next(flow for flow in plan.mandatory if flow.kind == "deposit")
    assert deposit.refundable is True

    refund = next(flow for flow in plan.optional if flow.kind == "deposit_refund")
    assert refund.amount == Decimal(-3000)
    assert refund.required is False
    assert refund.due_date == date(2027, 5, 31)


def test_non_refundable_deposit_has_no_refund_flow() -> None:
    plan = PaymentPlan.parse(
        "押金 3000元 不退还；月付 1000元 ×6期", signing_date=SIGNING
    )
    deposit = next(flow for flow in plan.mandatory if flow.kind == "deposit")
    assert deposit.refundable is False
    assert plan.optional == []


def test_deposit_refundability_is_not_guessed() -> None:
    plan = PaymentPlan.parse("押金 3000元；月付 1000元 ×6期", signing_date=SIGNING)
    deposit = next(flow for flow in plan.mandatory if flow.kind == "deposit")
    assert deposit.refundable is None
    assert plan.optional == []


def test_buyout_is_optional_and_dated_at_term_end() -> None:
    plan = PaymentPlan.parse("月付 1000元 ×6期；尾款买断 5000元", signing_date=SIGNING)
    balloon = next(flow for flow in plan.optional if flow.kind == "balloon")
    assert balloon.amount == Decimal(5000)
    assert balloon.required is False
    assert balloon.due_date == date(2027, 5, 31)


@pytest.mark.parametrize(
    "text",
    [
        "促销月付 99元 前4期；月付 199元 ×12期",
        "促销月付 99元 第2-4期；月付 199元 ×12期",
    ],
)
def test_promotion_beyond_third_period_is_rejected(text: str) -> None:
    with pytest.raises(PaymentParseError, match="first 3 installments"):
        PaymentPlan.parse(text, signing_date=SIGNING)


def test_promotion_accepts_discount_synonym_and_explicit_range() -> None:
    plan = PaymentPlan.parse(
        "优惠月付 99元 第1-3期；月付 199元 第4-6期", signing_date=SIGNING
    )
    assert [
        flow.period for flow in plan.mandatory if flow.kind == "promo_installment"
    ] == [1, 2, 3]


def test_minimum_monthly_payment_keeps_lower_bound_with_term() -> None:
    plan = PaymentPlan.parse(
        "首付 2000元；最低月供 999元；共12期", signing_date=SIGNING
    )
    assert plan.is_lower_bound is True
    assert plan.term_months == 12
    assert plan.minimum_monthly_payment == Decimal(999)
    assert plan.mandatory == [
        CashFlow(
            description="首付",
            amount=Decimal(2000),
            currency="元",
            due_date=SIGNING,
            required=True,
            kind="down_payment",
            refundable=False,
        )
    ]
    assert plan.optional == []
    assert plan.total_lower_bound == Decimal(13988)
    assert plan.mandatory_total == Decimal(2000)


def test_minimum_monthly_payment_without_term_uses_single_payment_bound() -> None:
    plan = PaymentPlan.parse("最低月供 999元", signing_date=SIGNING)
    assert plan.term_months is None
    assert plan.is_lower_bound is True
    assert plan.total_lower_bound == Decimal(999)
    assert plan.mandatory == []


def test_minimum_payment_conflicts_with_fixed_installments() -> None:
    with pytest.raises(PaymentParseError, match="cannot be combined"):
        PaymentPlan.parse("最低月供 99元；月付 199元 ×6期", signing_date=SIGNING)


def test_schedule_must_be_contiguous() -> None:
    with pytest.raises(PaymentParseError, match="not contiguous"):
        PaymentPlan.parse("月付 1299元 第4-12期", signing_date=SIGNING)


def test_fullwidth_characters_are_supported() -> None:
    plan = PaymentPlan.parse(
        "首付　２０００元；月付　１，２９９元　×３期", signing_date=SIGNING
    )
    assert plan.mandatory_total == Decimal(5897)
    assert [flow.amount for flow in plan.mandatory] == [
        Decimal(2000),
        Decimal(1299),
        Decimal(1299),
        Decimal(1299),
    ]


def test_existing_currency_symbol_is_preserved() -> None:
    plan = PaymentPlan.parse("首付 ￥2000；月付 ￥1299 ×3期", signing_date=SIGNING)
    assert plan.currency == "￥"


def test_conflicting_currencies_are_rejected() -> None:
    with pytest.raises(PaymentParseError, match="conflicting currencies"):
        PaymentPlan.parse("首付 2000元；月付 100美元 ×3期", signing_date=SIGNING)


def test_global_term_marker_does_not_become_component_count() -> None:
    plan = PaymentPlan.parse(
        "促销月付 999元 前3期；月付 1299元 ×9期；共12期",
        signing_date=SIGNING,
    )
    assert plan.term_months == 12
    assert len([f for f in plan.mandatory if f.period is not None]) == 12


def test_balloon_requires_known_term() -> None:
    with pytest.raises(PaymentParseError, match="requires a known term"):
        PaymentPlan.parse("尾款买断 8000元", signing_date=SIGNING)


def test_duplicate_component_is_rejected() -> None:
    with pytest.raises(PaymentParseError, match="duplicate payment component"):
        PaymentPlan.parse(
            "首付 1000元；首付 2000元；月付 100元 ×3期", signing_date=SIGNING
        )


@pytest.mark.parametrize("text", [None, "", "   "])
def test_empty_input_is_rejected(text: str | None) -> None:
    with pytest.raises(PaymentParseError, match="empty"):
        PaymentPlan.parse(text, signing_date=SIGNING)


def test_unrecognized_text_is_rejected() -> None:
    with pytest.raises(PaymentParseError, match="no recognizable payment component"):
        PaymentPlan.parse("联系客服获取报价", signing_date=SIGNING)


def test_bad_signing_date_is_rejected() -> None:
    with pytest.raises(PaymentParseError, match="signing_date"):
        PaymentPlan.parse("月付 100元 ×3期", signing_date="14/11/2026")


def test_missing_amount_is_rejected() -> None:
    with pytest.raises(PaymentParseError, match="no amount found"):
        PaymentPlan.parse("月付 ×3期", signing_date=SIGNING)


def test_minimum_payment_after_installments_is_rejected_too() -> None:
    with pytest.raises(PaymentParseError, match="cannot be combined"):
        PaymentPlan.parse("月付 199元 ×3期；最低月供 99元", signing_date=SIGNING)


def test_refundable_deposit_without_term_is_rejected() -> None:
    with pytest.raises(PaymentParseError, match="requires a known term"):
        PaymentPlan.parse("押金 3000元 可退", signing_date=SIGNING)


def test_promotion_without_count_falls_back_to_term() -> None:
    plan = PaymentPlan.parse("共3期；促销月付 99元", signing_date=SIGNING)
    assert plan.term_months == 3
    assert [flow.kind for flow in plan.mandatory] == ["promo_installment"] * 3


def test_promotion_without_count_or_term_is_rejected() -> None:
    with pytest.raises(PaymentParseError, match="needs a period count"):
        PaymentPlan.parse("促销月付 99元", signing_date=SIGNING)


def test_invalid_promotional_range_is_rejected() -> None:
    with pytest.raises(PaymentParseError, match="invalid installment period range"):
        PaymentPlan.parse(
            "促销月付 99元 第3-1期；月付 199元 ×5期", signing_date=SIGNING
        )


def test_zero_or_negative_count_is_rejected() -> None:
    with pytest.raises(PaymentParseError, match="positive number"):
        PaymentPlan.parse("月付 100元 ×0期", signing_date=SIGNING)


def test_invalid_regular_range_is_rejected() -> None:
    with pytest.raises(PaymentParseError, match="invalid installment period range"):
        PaymentPlan.parse("月付 100元 第3-1期", signing_date=SIGNING)


def test_overlapping_ranges_are_rejected() -> None:
    with pytest.raises(PaymentParseError, match="defined more than once"):
        PaymentPlan.parse(
            "月付 100元 第1-3期；月付 200元 第3-5期", signing_date=SIGNING
        )


def test_explicit_term_must_match_schedule() -> None:
    with pytest.raises(PaymentParseError, match="not contiguous"):
        PaymentPlan.parse("月付 100元 ×3期；共5期", signing_date=SIGNING)


def test_count_based_segments_continue_after_promotion() -> None:
    plan = PaymentPlan.parse(
        "促销月付 99元 前3期；月付 199元 ×3期", signing_date=SIGNING
    )
    assert plan.term_months == 6
    regular = [flow for flow in plan.mandatory if flow.kind == "installment"]
    assert [flow.period for flow in regular] == [4, 5, 6]


def test_foreign_yuan_currency_is_detected() -> None:
    plan = PaymentPlan.parse("首付 100美元；月付 50美元 ×3期", signing_date=SIGNING)
    assert plan.currency == "USD"


def test_missing_currency_stays_none() -> None:
    plan = PaymentPlan.parse("月付 100 ×3期", signing_date=SIGNING)
    assert plan.currency is None
    assert plan.mandatory_total == 300


def test_separator_arguments_are_passed_through() -> None:
    plan = PaymentPlan.parse(
        "月付 1.299,99元 ×2期",
        signing_date=SIGNING,
        decimal_separator=",",
        digit_group_separator=".",
    )
    assert [flow.amount for flow in plan.mandatory] == [
        Decimal("1299.99"),
        Decimal("1299.99"),
    ]


@pytest.mark.parametrize(
    ("keyword", "expected_kind"),
    [
        ("定金", "down_payment"),
        ("手续费", "fee"),
        ("最低月付", "minimum_monthly"),
        ("买断", "balloon"),
    ],
)
def test_keyword_aliases(keyword: str, expected_kind: str) -> None:
    if expected_kind == "minimum_monthly":
        plan = PaymentPlan.parse(f"{keyword} 99元；共6期", signing_date=SIGNING)
        assert plan.minimum_monthly_payment == 99
    elif expected_kind == "balloon":
        plan = PaymentPlan.parse(
            f"月付 100元 ×3期；{keyword} 500元", signing_date=SIGNING
        )
        assert plan.optional[0].kind == "balloon"
    else:
        plan = PaymentPlan.parse(
            f"{keyword} 500元；月付 100元 ×3期", signing_date=SIGNING
        )
        assert plan.mandatory[0].kind == expected_kind


def test_refund_wording_variants() -> None:
    refundable = PaymentPlan.parse(
        "押金 1000元 退还；月付 100元 ×3期", signing_date=SIGNING
    )
    assert any(flow.kind == "deposit_refund" for flow in refundable.optional)

    non_refundable = PaymentPlan.parse(
        "押金 1000元 不退；月付 100元 ×3期", signing_date=SIGNING
    )
    deposit = next(flow for flow in non_refundable.mandatory if flow.kind == "deposit")
    assert deposit.refundable is False
    assert non_refundable.optional == []
